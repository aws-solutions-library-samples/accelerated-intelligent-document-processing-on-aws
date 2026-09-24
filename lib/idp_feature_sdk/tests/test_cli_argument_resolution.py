# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The three pure resolvers every `idp-feature-cli` command routes its inputs
through: `parse_parameters`, `_resolve_bucket`, and
`_parse_published_template_url` (plus `_host_stack_value`, which reads a value
back off a describe-stacks payload).

Why these matter more than their size suggests. Each one turns an operator's
flag into a value that is then handed to CloudFormation or S3 without further
checking, and each has a wrong answer that is *accepted* rather than rejected.
`parse_parameters` mis-splitting a comma-bearing value (a subnet list is the
motivating case) yields a CFN parameter whose value is the first subnet, which
deploys. `_resolve_bucket` appending the region twice, or not at all, names a
bucket that either does not exist (loud) or exists and belongs to a different
release (quiet). `_parse_published_template_url` guessing the wrong bucket makes
the feature stack read its UI bundle and agent zip from somewhere else — the
stack comes up, the feature registers, and its assets 404. None of those raise
at the point the mistake is made.

So the assertions here are about the *values*, and about the three-way
distinction this repository's config bugs keep living in: a key that is absent,
a key present with an empty value, and a key present with a value. They are
different inputs and the code must keep them different.
"""

from __future__ import annotations

import pytest

from idp_feature_sdk.cli import (
    _host_stack_value,
    _parse_published_template_url,
    _resolve_bucket,
)
from idp_feature_sdk.parameters import parse_parameters

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# parse_parameters
# ---------------------------------------------------------------------------


def test_no_parameters_is_an_empty_dict() -> None:
    """Absent and empty must both mean "no overrides" — not a dict with one
    blank key, which `deploy_pack` would then submit to CloudFormation."""
    assert parse_parameters(None) == {}
    assert parse_parameters("") == {}


def test_a_key_with_an_empty_value_is_kept_as_an_empty_string() -> None:
    """`Key=` is the third case: the operator named the parameter and asked for
    the empty string. Dropping it would silently leave the template default in
    place — the opposite of what was asked for."""
    assert parse_parameters("LogLevel=") == {"LogLevel": ""}
    # And it is distinguishable from the key being absent altogether.
    assert "LogLevel" not in parse_parameters("Other=x")


def test_multiple_parameters_split_on_the_key_boundary() -> None:
    assert parse_parameters("LogLevel=DEBUG,MaxConcurrent=10") == {
        "LogLevel": "DEBUG",
        "MaxConcurrent": "10",
    }


def test_a_value_containing_commas_survives_intact() -> None:
    """The reason the parser is a regex over key boundaries rather than a
    `split(",")`: a subnet or security-group list is one value containing
    commas. Splitting on every comma would submit `subnet-aaa` alone, and the
    stack would deploy into one subnet instead of three."""
    parsed = parse_parameters(
        "VpcSubnetIds=subnet-aaa,subnet-bbb,subnet-ccc,LogLevel=INFO"
    )
    assert parsed == {
        "VpcSubnetIds": "subnet-aaa,subnet-bbb,subnet-ccc",
        "LogLevel": "INFO",
    }


def test_whitespace_around_a_pair_is_stripped() -> None:
    """Whitespace either side of a `key=value` pair is noise from a shell
    quoting, and is dropped from both the key and the value."""
    assert parse_parameters(" LogLevel=DEBUG ") == {"LogLevel": "DEBUG"}
    assert parse_parameters("A=1, B=2") == {"A": "1", "B": "2"}


def test_a_space_around_the_equals_is_tolerated() -> None:
    """Spacing a pair out for readability used to match nothing at all, so the
    deploy proceeded with every parameter at its publish-time default and nothing
    warned (#1220). It is read as the pair it plainly is, and reported through
    `on_warning` rather than either dropped or refused."""
    assert parse_parameters("LogLevel = DEBUG") == {"LogLevel": "DEBUG"}
    assert parse_parameters("LogLevel =DEBUG") == {"LogLevel": "DEBUG"}


def test_an_underscore_in_a_key_is_part_of_the_key() -> None:
    """`Log_Level=DEBUG` used to be submitted as `Level=DEBUG` — a wrapper
    parameter name the feature author never wrote (#1220)."""
    assert parse_parameters("Log_Level=DEBUG") == {"Log_Level": "DEBUG"}


def test_an_equals_inside_a_value_stays_in_the_value() -> None:
    """A value may contain `=` — base64 padding and a query string both do — and
    splitting on it used to yield an empty value plus an invented parameter."""
    assert parse_parameters("Tags=a=b") == {"Tags": "a=b"}
    assert parse_parameters("Query=a=b=c") == {"Query": "a=b=c"}


def test_text_that_forms_no_pair_is_reported_rather_than_dropped() -> None:
    """Nothing is refused, because refusing a shape the previous parser accepted
    would break a script that runs today. What changed is that the operator is
    told, which is the half that was missing."""
    collected: list[str] = []
    assert parse_parameters("JustAKey", on_warning=collected.append) == {}
    assert len(collected) == 1, collected
    assert "JustAKey" in collected[0]


def test_a_trailing_comma_does_not_become_part_of_the_value() -> None:
    """A trailing separator is the single most common paste artefact, and
    `LogLevel=INFO,` must not set the LogLevel to the string `INFO,` — CFN
    would accept it and the Lambda would read an unrecognised level."""
    assert parse_parameters("LogLevel=INFO,") == {"LogLevel": "INFO"}


def test_a_later_repeat_of_a_key_wins() -> None:
    """Last-one-wins, so a scripted base set can be overridden by appending."""
    assert parse_parameters("LogLevel=INFO,LogLevel=DEBUG") == {"LogLevel": "DEBUG"}


# ---------------------------------------------------------------------------
# _resolve_bucket
# ---------------------------------------------------------------------------


def test_an_explicit_basename_gets_exactly_one_region_suffix() -> None:
    """`idp-cli`'s contract: the flag is a BASENAME and the region is appended.
    Both directions are wrong in a way that deploys: no suffix names a bucket in
    no particular region, a doubled suffix names one that does not exist."""
    assert _resolve_bucket("my-artifacts", "us-west-2") == "my-artifacts-us-west-2"


def test_an_explicit_basename_is_not_de_duplicated() -> None:
    """A basename that already ends in the region still gets the suffix. That is
    the documented semantics (basename in, bucket out) and it is asserted here
    so nobody "fixes" it into a de-duplication that would change which bucket
    every existing script resolves to."""
    assert _resolve_bucket("my-artifacts-us-west-2", "us-west-2") == (
        "my-artifacts-us-west-2-us-west-2"
    )


def test_an_omitted_basename_auto_creates_the_per_account_bucket(monkeypatch) -> None:
    """With no basename the per-account artifacts bucket is created. The
    `make_public` flag must be forwarded: a public pack deploy against a bucket
    left private fails at download time in the *deploying* account, which the
    publisher never sees."""
    import idp_feature_sdk.pack as pack_mod

    seen: dict = {}

    def _fake_ensure(*, region, console, make_public):
        seen["region"] = region
        seen["make_public"] = make_public
        return f"idp-accelerator-artifacts-123456789012-{region}"

    monkeypatch.setattr(pack_mod, "ensure_artifacts_bucket", _fake_ensure)

    assert (
        _resolve_bucket(None, "eu-west-1", make_public=True)
        == "idp-accelerator-artifacts-123456789012-eu-west-1"
    )
    assert seen == {"region": "eu-west-1", "make_public": True}


def test_an_omitted_basename_defaults_to_private(monkeypatch) -> None:
    import idp_feature_sdk.pack as pack_mod

    seen: dict = {}
    monkeypatch.setattr(
        pack_mod,
        "ensure_artifacts_bucket",
        lambda *, region, console, make_public: (
            seen.update(make_public=make_public) or "bucket"
        ),
    )
    assert _resolve_bucket(None, "us-east-1") == "bucket"
    assert seen == {"make_public": False}


def test_a_bucket_creation_failure_exits_rather_than_returning_a_name(
    monkeypatch, capsys
) -> None:
    """The failure must stop the command. Returning a name (or None) would let
    publish carry on and upload nothing, reporting success."""
    import idp_feature_sdk.pack as pack_mod

    def _boom(**_kwargs):
        raise RuntimeError("Failed to create artifacts bucket 'x': AccessDenied")

    monkeypatch.setattr(pack_mod, "ensure_artifacts_bucket", _boom)

    with pytest.raises(SystemExit) as exc:
        _resolve_bucket(None, "us-east-1")
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# _parse_published_template_url
# ---------------------------------------------------------------------------


def test_virtual_hosted_url_yields_both_feature_id_and_bucket() -> None:
    feature_id, bucket = _parse_published_template_url(
        "https://my-bucket.s3.us-west-2.amazonaws.com/extensions/demo/template.yaml"
    )
    assert (feature_id, bucket) == ("demo", "my-bucket")


def test_virtual_hosted_url_without_a_region_label_still_parses() -> None:
    feature_id, bucket = _parse_published_template_url(
        "https://my-bucket.s3.amazonaws.com/extensions/demo/template.yaml"
    )
    assert (feature_id, bucket) == ("demo", "my-bucket")


def test_path_style_url_takes_the_bucket_from_the_first_path_segment() -> None:
    """Path-style is the form an operator copies out of the console. Reading the
    bucket off the host here would produce `s3` — a bucket name that exists
    somewhere, owned by someone else."""
    feature_id, bucket = _parse_published_template_url(
        "https://s3.us-east-1.amazonaws.com/my-bucket/extensions/demo/template.yaml"
    )
    assert (feature_id, bucket) == ("demo", "my-bucket")


def test_legacy_dash_region_host_forms_parse() -> None:
    """`s3-<region>` is the older host spelling; both the path-style and
    virtual-hosted variants of it must resolve the same bucket."""
    assert _parse_published_template_url(
        "https://s3-eu-west-1.amazonaws.com/my-bucket/extensions/demo/template.yaml"
    ) == ("demo", "my-bucket")
    assert _parse_published_template_url(
        "https://my-bucket.s3-eu-west-1.amazonaws.com/extensions/demo/template.yaml"
    ) == ("demo", "my-bucket")


def test_a_prefixed_key_still_finds_the_feature_id() -> None:
    """A non-empty `--prefix` nests the layout; the id is the segment after
    `extensions/`, not the first or last segment of the key."""
    feature_id, bucket = _parse_published_template_url(
        "https://b.s3.us-east-1.amazonaws.com/artifacts/idp/extensions/my-feat/template.yaml"
    )
    assert (feature_id, bucket) == ("my-feat", "b")


def test_a_percent_encoded_segment_is_decoded() -> None:
    feature_id, _ = _parse_published_template_url(
        "https://b.s3.us-east-1.amazonaws.com/extensions/my%2Dfeat/template.yaml"
    )
    assert feature_id == "my-feat"


def test_a_non_s3_host_yields_no_bucket_but_still_yields_the_id() -> None:
    """A CloudFront/custom-domain URL carries no bucket. Returning a guess would
    point the feature stack at a bucket that does not exist; returning None is
    what makes the CLI ask for `--bucket-basename`."""
    feature_id, bucket = _parse_published_template_url(
        "https://cdn.example.com/extensions/demo/template.yaml"
    )
    assert bucket is None
    assert feature_id == "demo"


def test_a_key_without_an_extensions_segment_yields_no_feature_id() -> None:
    feature_id, bucket = _parse_published_template_url(
        "https://my-bucket.s3.us-east-1.amazonaws.com/some/other/template.yaml"
    )
    assert feature_id is None
    assert bucket == "my-bucket"


def test_extensions_as_the_last_segment_yields_no_feature_id() -> None:
    """`.../extensions` with nothing after it must not return the empty string —
    the caller tests the result for truthiness, and `""` would pass through into
    a feature stack named `<host>-feature-`."""
    feature_id, _ = _parse_published_template_url(
        "https://my-bucket.s3.us-east-1.amazonaws.com/extensions"
    )
    assert feature_id is None


def test_a_port_in_the_host_does_not_become_part_of_the_bucket() -> None:
    _, bucket = _parse_published_template_url(
        "https://my-bucket.s3.us-east-1.amazonaws.com:443/extensions/demo/template.yaml"
    )
    assert bucket == "my-bucket"


# ---------------------------------------------------------------------------
# _host_stack_value
# ---------------------------------------------------------------------------


def test_host_value_is_read_from_parameters() -> None:
    host = {
        "Parameters": [
            {"ParameterKey": "FeaturePlatformSimulatorEndpoint", "ParameterValue": "u"}
        ]
    }
    assert _host_stack_value(host, "FeaturePlatformSimulatorEndpoint") == "u"


def test_host_value_is_read_from_outputs_too() -> None:
    """The same name may arrive as an Output rather than a Parameter depending on
    the host template's vintage; reading only one collection would make the
    simulator seed silently stop happening on the other."""
    host = {"Outputs": [{"OutputKey": "Endpoint", "OutputValue": "https://sim"}]}
    assert _host_stack_value(host, "Endpoint") == "https://sim"


def test_an_empty_host_value_reads_as_absent() -> None:
    """The distinction that matters to the caller: a declared-but-blank
    parameter must not trigger the simulator seed against the empty URL."""
    host = {"Parameters": [{"ParameterKey": "Endpoint", "ParameterValue": ""}]}
    assert _host_stack_value(host, "Endpoint") is None


def test_a_missing_key_and_a_stack_with_neither_collection_both_read_as_absent() -> (
    None
):
    assert _host_stack_value({"Parameters": [], "Outputs": []}, "Endpoint") is None
    assert _host_stack_value({}, "Endpoint") is None
