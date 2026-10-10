# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`pack._bake_wrapper_defaults` — the text edit that writes publish-time values
into a pack wrapper's CloudFormation parameter defaults.

This is where a pack's artifact-locating values become part of the published
template. `deploy-pack` submits only `AdminEmail` and `HostStackName`; everything
that tells the nested feature stack *where its artifacts are* — bucket, prefix,
version — arrives as a parameter `Default:` baked in here. The values cannot be
passed as parameters instead, because the CloudFormation console's "Update stack"
wizard blanks parameters when the template changes, which is how a feature stack
previously ended up reading `s3://bucket//1.2.3/ui-bundle.js`.

The edit is done on the YAML as *text*, not through a parser, because
CloudFormation's short-form intrinsics (`!Ref`, `!Sub`, `!GetAtt`) do not survive
a round-trip through most YAML libraries. That choice is sound and it is also
what makes the function worth testing carefully: a line-oriented rewrite has no
model of scope, so the whole correctness question is "did it edit the right
line". Every way of getting that wrong is silent. Writing the default into the
wrong parameter points the feature stack at the wrong bucket. Writing it into a
resource property outside the `Parameters:` block corrupts the template in a way
CloudFormation may still accept. Failing to replace an *existing* `Default:` —
rather than inserting a second one — leaves the wrapper's placeholder value in
place, so the pack deploys and reads its artifacts from wherever the wrapper's
author happened to write during development.

So the tests below are mostly about what must **not** change: the neighbouring
parameter whose name shares a prefix, the `Default:` inside a resource, the
nested keys within a parameter block, and the parameters the manifest declined to
name at all.
"""

from __future__ import annotations

from textwrap import dedent

import pytest

from idp_feature_sdk.manifest import MarketplaceSpec
from idp_feature_sdk.pack import _bake_wrapper_defaults, _bake_wrapper_tokens

pytestmark = pytest.mark.unit


def _params(text: str) -> dict[str, str]:
    """Every `Default:` in the Parameters block, keyed by its parameter name.

    Parsed from the rendered text rather than from the input, so an assertion
    about it cannot be satisfied by the test's own data.
    """
    found: dict[str, str] = {}
    current: str | None = None
    in_params = False
    params_indent = -1
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip())
        if stripped == "Parameters:":
            in_params = True
            params_indent = indent
            continue
        if in_params and indent <= params_indent:
            in_params = False
            continue
        if not in_params:
            continue
        if indent == params_indent + 2 and stripped.endswith(":"):
            current = stripped[:-1]
        elif stripped.startswith("Default:") and current:
            found[current] = stripped.split("Default:", 1)[1].strip().strip("'\"")
    return found


# ---------------------------------------------------------------------------
# Inserting vs replacing
# ---------------------------------------------------------------------------


_NO_DEFAULTS = (
    dedent("""
    AWSTemplateFormatVersion: '2010-09-09'
    Parameters:
      FeatureBucket:
        Type: String
      FeatureArtifactPrefix:
        Type: String
    Resources:
      Dummy:
        Type: AWS::SNS::Topic
""").strip()
    + "\n"
)


def test_a_parameter_with_no_default_gets_one_inserted_under_its_header() -> None:
    baked = _bake_wrapper_defaults(
        _NO_DEFAULTS,
        param_defaults={
            "FeatureBucket": "artifacts-us-east-1",
            "FeatureArtifactPrefix": "extensions/claims",
        },
    )
    assert _params(baked) == {
        "FeatureBucket": "artifacts-us-east-1",
        "FeatureArtifactPrefix": "extensions/claims",
    }
    # Inserted immediately after the header and at property indent (two deeper
    # than the parameter name). A `Default:` at the parameter's own indent would
    # read as a new, malformed parameter called `Default`.
    assert (
        "  FeatureBucket:\n    Default: 'artifacts-us-east-1'\n    Type: String\n"
    ) in baked


_WITH_PLACEHOLDER_DEFAULTS = (
    dedent("""
    AWSTemplateFormatVersion: '2010-09-09'
    Parameters:
      FeatureBucket:
        Type: String
        Default: my-dev-bucket
        Description: where the artifacts live
      FeatureArtifactPrefix:
        Type: String
        Default: extensions/scratch
      FeatureVersion:
        Type: String
        Default: '0.0.0-dev'
    Resources:
      Dummy:
        Type: AWS::SNS::Topic
""").strip()
    + "\n"
)


def test_an_existing_default_is_replaced_not_duplicated() -> None:
    """The failure this catches is the quietest one in the file. A wrapper written
    during development carries a `Default: my-dev-bucket`; if the bake appended a
    second `Default:` line instead of replacing it, CloudFormation would read one
    of the two — and a pack published for a customer would go looking for its
    artifacts in the pack author's development bucket."""
    baked = _bake_wrapper_defaults(
        _WITH_PLACEHOLDER_DEFAULTS,
        param_defaults={
            "FeatureBucket": "artifacts-us-east-1",
            "FeatureArtifactPrefix": "extensions/claims",
            "FeatureVersion": "1.4.0",
        },
    )
    assert _params(baked) == {
        "FeatureBucket": "artifacts-us-east-1",
        "FeatureArtifactPrefix": "extensions/claims",
        "FeatureVersion": "1.4.0",
    }
    # Exactly one Default line per parameter, and no trace of the dev values.
    assert baked.count("Default:") == 3
    assert "my-dev-bucket" not in baked
    assert "extensions/scratch" not in baked
    assert "0.0.0-dev" not in baked


def test_replacing_a_default_preserves_the_indent_and_the_lines_around_it() -> None:
    """A rewritten line at the wrong indent makes the `Description:` that follows
    a child of the Default, which CloudFormation rejects — loudly, but only at
    deploy time in the customer's account."""
    baked = _bake_wrapper_defaults(
        _WITH_PLACEHOLDER_DEFAULTS, param_defaults={"FeatureBucket": "b-us-east-1"}
    )
    assert (
        "  FeatureBucket:\n"
        "    Type: String\n"
        "    Default: 'b-us-east-1'\n"
        "    Description: where the artifacts live\n"
    ) in baked


def test_a_value_is_quoted_so_a_prefix_is_not_read_as_a_yaml_structure() -> None:
    """A version like `1.10` is a YAML float and a prefix beginning with `*` or
    `{` is a YAML alias or mapping. Quoting is what keeps every baked value a
    string, which is what the parameter's `Type: String` requires."""
    baked = _bake_wrapper_defaults(
        _NO_DEFAULTS, param_defaults={"FeatureBucket": "1.10"}
    )
    assert "Default: '1.10'" in baked


# ---------------------------------------------------------------------------
# What must not be touched
# ---------------------------------------------------------------------------


def test_a_parameter_whose_name_is_a_prefix_of_another_is_not_confused() -> None:
    """`FeatureBucket` and `FeatureBucketRegion` differ by a suffix. An unanchored
    match would write the bucket name into the region parameter as well, and the
    feature stack would resolve its artifacts against a region called
    `artifacts-us-east-1`."""
    template = (
        dedent("""
        Parameters:
          FeatureBucket:
            Type: String
          FeatureBucketRegion:
            Type: String
            Default: us-west-2
        Resources: {}
    """).strip()
        + "\n"
    )
    baked = _bake_wrapper_defaults(
        template, param_defaults={"FeatureBucket": "artifacts-us-east-1"}
    )
    assert _params(baked) == {
        "FeatureBucket": "artifacts-us-east-1",
        "FeatureBucketRegion": "us-west-2",
    }


def test_a_default_outside_the_parameters_block_is_left_alone() -> None:
    """`Default` is an ordinary word elsewhere in a template — a Lambda
    environment variable, an SSM parameter value, a Step Functions choice. The
    rewrite must stop at the end of the Parameters block, which it detects by
    indentation rather than by the word."""
    template = (
        dedent("""
        Parameters:
          FeatureBucket:
            Type: String
        Resources:
          Fn:
            Type: AWS::Serverless::Function
            Properties:
              Environment:
                Variables:
                  Default: keep-me
        Outputs:
          Thing:
            Value: !Ref FeatureBucket
    """).strip()
        + "\n"
    )
    baked = _bake_wrapper_defaults(
        template, param_defaults={"FeatureBucket": "artifacts-us-east-1"}
    )
    assert "Default: keep-me" in baked
    assert baked.count("Default: 'artifacts-us-east-1'") == 1
    # The Outputs block, and the short-form intrinsic in it, survive verbatim.
    assert "Value: !Ref FeatureBucket" in baked


def test_a_nested_key_inside_a_parameter_is_not_treated_as_a_parameter() -> None:
    """`AllowedValues:` and `ConstraintDescription:` sit one level deeper than a
    parameter header. Mistaking one for a header would make the next `Default:`
    belong to it, so the real parameter would never be baked — and the pack would
    deploy with the wrapper's own placeholder."""
    template = (
        dedent("""
        Parameters:
          LogLevel:
            Type: String
            AllowedValues:
              - INFO
              - DEBUG
            Default: INFO
          FeatureBucket:
            Type: String
            Default: placeholder
        Resources: {}
    """).strip()
        + "\n"
    )
    baked = _bake_wrapper_defaults(
        template, param_defaults={"FeatureBucket": "artifacts-us-east-1"}
    )
    assert _params(baked) == {
        "LogLevel": "INFO",
        "FeatureBucket": "artifacts-us-east-1",
    }
    assert "- DEBUG" in baked
    assert "placeholder" not in baked


def test_only_the_named_parameters_are_baked() -> None:
    """`featureBucketParam`, `prefixParam` and `versionParam` are all optional in
    `pack.wrapperParameters`. A wrapper that resolves one of those itself (via a
    Mappings lookup, say) declines to name it, and baking it anyway would
    overwrite a value the wrapper computes deliberately."""
    baked = _bake_wrapper_defaults(
        _WITH_PLACEHOLDER_DEFAULTS, param_defaults={"FeatureVersion": "1.4.0"}
    )
    assert _params(baked) == {
        "FeatureBucket": "my-dev-bucket",
        "FeatureArtifactPrefix": "extensions/scratch",
        "FeatureVersion": "1.4.0",
    }


def test_no_defaults_at_all_returns_the_template_unchanged() -> None:
    """The empty-mapping case has its own code path (the early return before the
    insertion pass). It must be a no-op, byte for byte — not a reflow."""
    assert _bake_wrapper_defaults(_WITH_PLACEHOLDER_DEFAULTS, param_defaults={}) == (
        _WITH_PLACEHOLDER_DEFAULTS
    )


def test_a_template_with_no_parameters_block_and_no_defaults_is_unchanged() -> None:
    template = "Resources:\n  Dummy:\n    Type: AWS::SNS::Topic\n"
    assert _bake_wrapper_defaults(template, param_defaults={}) == template


def test_naming_a_parameter_the_wrapper_does_not_declare_is_refused() -> None:
    """Silently ignoring it would publish a wrapper with none of the artifact
    values baked — which deploys, and reads its artifacts from whatever the
    wrapper's own defaults say."""
    with pytest.raises(ValueError, match="NoSuchParam") as exc:
        _bake_wrapper_defaults(
            _NO_DEFAULTS,
            param_defaults={
                "FeatureBucket": "b",
                "NoSuchParam": "v",
            },
        )
    # The message points at the manifest key the operator has to fix.
    assert "wrapperParameters" in str(exc.value)


def test_a_declared_parameter_and_an_undeclared_one_together_still_refuse() -> None:
    """Ordering must not matter: the undeclared name is refused whether it is
    processed before or after a valid one."""
    for defaults in (
        {"Nope": "v", "FeatureBucket": "b"},
        {"FeatureBucket": "b", "Nope": "v"},
    ):
        with pytest.raises(ValueError, match="Nope"):
            _bake_wrapper_defaults(_NO_DEFAULTS, param_defaults=defaults)


# ---------------------------------------------------------------------------
# Token substitution in the wrapper text
# ---------------------------------------------------------------------------


class _Manifest:
    def __init__(self, version: str, marketplace: MarketplaceSpec) -> None:
        self.version = version
        self.marketplace = marketplace


def test_wrapper_tokens_cover_the_places_intrinsics_are_forbidden() -> None:
    """A wrapper's top-level `Description` cannot use `!Sub`, so the version has
    to be substituted textually. The same five tokens the feature template gets,
    substituted by the same shared helper — they were once two independent
    replace chains and a newly-added token taught only one of them."""
    wrapper = dedent("""
        Description: Claims pack v<FEATURE_VERSION_TOKEN>
        Metadata:
          Bucket: '<FEATURE_BUCKET_TOKEN>'
          Prefix: '<FEATURE_ARTIFACT_PREFIX_TOKEN>'
          ProductCode: '<FEATURE_PRODUCT_CODE_TOKEN>'
          ListingUrl: '<FEATURE_LISTING_URL_TOKEN>'
          LicenseMode: '<FEATURE_LICENSE_MODE_TOKEN>'
    """).strip()
    baked = _bake_wrapper_tokens(
        wrapper,
        manifest=_Manifest(
            "1.4.0",
            MarketplaceSpec(
                productCode="prod-claims",
                listingUrl="https://aws.amazon.com/marketplace/pp/prodview-A",
                licenseMode="marketplace-live",
            ),
        ),  # type: ignore[arg-type]
        artifact_bucket="artifacts-us-east-1",
        artifact_prefix="extensions/claims",
    )
    assert "_TOKEN>" not in baked
    assert "Description: Claims pack v1.4.0" in baked
    assert "Bucket: 'artifacts-us-east-1'" in baked
    assert "Prefix: 'extensions/claims'" in baked
    assert "ProductCode: 'prod-claims'" in baked
    assert "LicenseMode: 'marketplace-live'" in baked


def test_a_wrapper_using_no_tokens_is_returned_untouched() -> None:
    """Tokens are optional. A substitution pass that rewrote anything here would
    be editing a wrapper that never asked for it."""
    wrapper = "Description: A plain wrapper\nResources: {}\n"
    assert (
        _bake_wrapper_tokens(
            wrapper,
            manifest=_Manifest("1.4.0", MarketplaceSpec()),  # type: ignore[arg-type]
            artifact_bucket="b",
            artifact_prefix="p",
        )
        == wrapper
    )


def test_an_undeclared_license_mode_bakes_to_none_in_the_wrapper_too() -> None:
    """Both publish paths must agree on the default, or a pack and a plain
    publish of the same feature register different licensing authorities with the
    host."""
    baked = _bake_wrapper_tokens(
        "LicenseMode: '<FEATURE_LICENSE_MODE_TOKEN>'\n",
        manifest=_Manifest("1.4.0", MarketplaceSpec()),  # type: ignore[arg-type]
        artifact_bucket="b",
        artifact_prefix="p",
    )
    assert baked == "LicenseMode: 'none'\n"
