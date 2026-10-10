# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Edge cases across the SDK's input-handling surfaces: a manifest that is not a
mapping, a UI bundle that is not text, the seller-service subprocess wrapper, the
template-version scan, the deployed-registry read-back, and
`ensure_artifacts_bucket`'s create-vs-reuse decision.

These are collected rather than scattered because they share a property: each is
the point where something arrives from outside the SDK — a file an author wrote, a
subprocess's exit code, an environment variable read back off a deployed Lambda,
an S3 bucket that may or may not exist — and the wrong handling of it produces a
misleading success rather than a crash. A `feature.yaml` that YAML parses as a
list would otherwise reach the schema validator and be reported as a dozen
missing-property errors; a bundle that is a binary blob would be reported as
"does not reference window.IdpFeatures", which sends the author looking at their
entry point instead of their build config; a registry read back as a JSON array
would pass an `if deployed` check and serve no products.

`ensure_artifacts_bucket` is here for the security decision it encodes, which is
asymmetric on purpose: Block Public Access is enabled on a bucket it creates and
**never** touched on one that already exists, because weakening the BPA of an
operator's bucket would silently revert a security remediation. The additive
TLS-only policy is applied either way, and is only fatal on a bucket we own.
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import boto3
import pytest
from moto import mock_aws
from rich.console import Console

from idp_feature_sdk.bundle import BundleValidationError, validate_bundle
from idp_feature_sdk.manifest import ManifestError, load_manifest
from idp_feature_sdk.pack import PackPublisher, ensure_artifacts_bucket
from idp_feature_sdk.seller_service import (
    SellerServiceError,
    build_sam_deploy_command,
    read_service_version,
    run_command,
    utc_now_iso,
    verify_deployed_registry,
)

pytestmark = pytest.mark.unit


def _console() -> Console:
    return Console(record=True, width=400)


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


# ---------------------------------------------------------------------------
# manifest: a feature.yaml that is not a mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "type_name"),
    [
        ("- featureId: demo\n- version: 1.0.0\n", "list"),
        ("just a string\n", "str"),
        ("42\n", "int"),
    ],
)
def test_a_feature_yaml_that_is_not_a_mapping_says_so(
    tmp_path: Path, body: str, type_name: str
) -> None:
    """Valid YAML, wrong shape. Reaching the JSON-schema validator with a list
    produces a wall of "is not of type 'object'" noise; naming the type the file
    actually parsed to points straight at the mistake — usually a stray leading
    `- ` or a file that is a fragment of a larger document."""
    (tmp_path / "feature.yaml").write_text(body, encoding="utf-8")
    with pytest.raises(ManifestError) as exc:
        load_manifest(tmp_path)
    assert "must be a mapping" in str(exc.value)
    assert type_name in str(exc.value)


def test_an_empty_feature_yaml_is_a_mapping_error_not_a_crash(tmp_path: Path) -> None:
    """An empty file YAML-parses to `None`, which is neither a mapping nor an
    error — so it has to be caught here rather than by `raw["featureId"]`."""
    (tmp_path / "feature.yaml").write_text("", encoding="utf-8")
    with pytest.raises(ManifestError, match="must be a mapping"):
        load_manifest(tmp_path)


def test_a_comment_only_feature_yaml_is_treated_the_same(tmp_path: Path) -> None:
    (tmp_path / "feature.yaml").write_text("# nothing here yet\n", encoding="utf-8")
    with pytest.raises(ManifestError, match="must be a mapping"):
        load_manifest(tmp_path)


# ---------------------------------------------------------------------------
# bundle: a UI bundle that is not UTF-8 text
# ---------------------------------------------------------------------------


def test_a_binary_bundle_is_reported_as_an_encoding_problem(tmp_path: Path) -> None:
    """Every later check reads the bundle as text. Without this the author gets
    "does not reference window.IdpFeatures" for a file that is raw WASM or a
    mis-configured binary output — which sends them to their entry point instead
    of their build config."""
    bundle = tmp_path / "ui-bundle.js"
    bundle.write_bytes(b"\x00asm\x01\x00\x00\x00\xff\xfe\xfd")
    with pytest.raises(BundleValidationError) as exc:
        validate_bundle(bundle, "demo", "1.0.0")
    assert "not UTF-8" in str(exc.value)
    assert str(bundle) in str(exc.value)


def test_a_utf8_bundle_with_non_ascii_content_is_accepted(tmp_path: Path) -> None:
    """The check is about *encoding*, not about ASCII. A bundle carrying a
    non-ASCII string literal — a display label, an em dash — must still pass."""
    bundle = tmp_path / "ui-bundle.js"
    bundle.write_text(
        "window.IdpFeatures.register('demo', {version: '1.0.0', label: 'Übersicht — ok'});",
        encoding="utf-8",
    )
    info = validate_bundle(bundle, "demo", "1.0.0")
    assert info.size_bytes == bundle.stat().st_size
    assert len(info.sha256) == 64


# ---------------------------------------------------------------------------
# seller_service: run_command
# ---------------------------------------------------------------------------


def test_run_command_passes_the_working_directory_through(tmp_path: Path) -> None:
    """`sam build` and `sam deploy` both have to run from the service directory —
    SAM resolves `template.yaml` and the build cache relative to the cwd, so a
    missing cwd builds the wrong thing or nothing."""
    marker = tmp_path / "ran-here"
    run_command(["sh", "-c", "touch ran-here"], cwd=tmp_path)
    assert marker.exists()


def test_run_command_with_no_cwd_runs_in_the_current_directory(tmp_path: Path) -> None:
    run_command(["true"])


def test_a_missing_executable_names_the_sam_cli_prerequisite() -> None:
    """A bare FileNotFoundError says "No such file or directory: 'sam'", which
    reads like a missing template. The prerequisite is what the operator needs."""
    with pytest.raises(SellerServiceError) as exc:
        run_command(["definitely-not-on-path-xyz", "build"])
    assert "AWS SAM CLI" in str(exc.value)
    assert "definitely-not-on-path-xyz" in str(exc.value)


def test_a_nonzero_exit_carries_the_exit_code_and_the_command() -> None:
    """The deploy must stop, and the message has to say which of the two `sam`
    invocations failed — a failed build and a failed deploy need different
    responses."""
    with pytest.raises(SellerServiceError) as exc:
        run_command(["sh", "-c", "exit 7"])
    assert "exit code 7" in str(exc.value)
    assert "sh -c" in str(exc.value)


def test_a_command_failure_is_not_silently_swallowed() -> None:
    """The guard that matters: `check=True` must be in effect. A subprocess wrapper
    that ignored the exit code would let a failed `sam build` fall through to
    `sam deploy`, which then deploys the previous build's artifacts."""
    with pytest.raises(SellerServiceError):
        run_command(["false"])


def test_run_command_does_not_use_a_shell(tmp_path: Path) -> None:
    """argv is exec'd directly. If a shell were involved, this metacharacter-laden
    single argument would be re-tokenised and the `touch` would run — the
    injection surface the fixed-argv form exists to avoid."""
    run_command(["sh", "-c", "true"], cwd=tmp_path)
    with pytest.raises(SellerServiceError):
        # `true; touch pwned` as ONE argv element is not a valid program name.
        run_command(["true; touch pwned"], cwd=tmp_path)
    assert not (tmp_path / "pwned").exists()


# ---------------------------------------------------------------------------
# seller_service: build_sam_deploy_command edges
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_service_dir(tmp_path: Path) -> Path:
    (tmp_path / "template.yaml").write_text(
        dedent("""
            AWSTemplateFormatVersion: '2010-09-09'
            Parameters:
              ProductRegistryJson:
                Type: String
              AgreementRegion:
                Type: String
        """).strip()
        + "\n",
        encoding="utf-8",
    )
    return tmp_path


def test_a_registry_that_is_not_json_is_refused_before_the_deploy(
    stub_service_dir: Path,
) -> None:
    """The compaction step is also the validation step. An unparseable registry
    passed through verbatim would deploy a function that serves no products and
    refuses every activation with the ordinary not-entitled body."""
    with pytest.raises(SellerServiceError) as exc:
        build_sam_deploy_command(
            service_dir=stub_service_dir,
            stack_name="s",
            region="us-east-1",
            product_registry_json="{not json",
        )
    assert "not valid JSON" in str(exc.value)


def test_extra_args_are_appended_after_the_parameter_overrides(
    stub_service_dir: Path,
) -> None:
    """`--parameter-overrides` consumes following non-flag tokens, so anything
    appended has to be a flag. Asserting the position keeps a later addition from
    being swallowed as another override."""
    cmd = build_sam_deploy_command(
        service_dir=stub_service_dir,
        stack_name="s",
        region="us-east-1",
        product_registry_json=json.dumps({"prod-a": {}}),
        extra_args=["--no-confirm-changeset"],
    )
    assert cmd[-1] == "--no-confirm-changeset"
    overrides_at = cmd.index("--parameter-overrides")
    assert any(a.startswith("ProductRegistryJson=") for a in cmd[overrides_at:])
    assert all(a.startswith("-") for a in cmd[-1:])


# ---------------------------------------------------------------------------
# seller_service: read_service_version
# ---------------------------------------------------------------------------


def test_an_unreadable_template_yields_no_version_rather_than_raising(
    tmp_path: Path,
) -> None:
    """The version is echoed for the operator's benefit only. Raising here would
    make a cosmetic line able to block a deploy."""
    assert read_service_version(tmp_path) is None


def test_the_version_is_read_from_the_mapping_value(tmp_path: Path) -> None:
    (tmp_path / "template.yaml").write_text(
        dedent("""
            Mappings:
              Service:
                Version:
                  ServiceVersion:
                    Value: '0.4.1'
        """).strip()
        + "\n",
        encoding="utf-8",
    )
    assert read_service_version(tmp_path) == "0.4.1"


def test_an_unquoted_version_is_read_the_same(tmp_path: Path) -> None:
    (tmp_path / "template.yaml").write_text(
        "  ServiceVersion:\n    Value: 0.4.1\n", encoding="utf-8"
    )
    assert read_service_version(tmp_path) == "0.4.1"


def test_a_value_further_than_three_lines_below_is_not_claimed(tmp_path: Path) -> None:
    """The scan is bounded to the next three lines, which is what keeps it from
    picking up an unrelated `Value:` from a *different* mapping entry further
    down and echoing it as the service version."""
    (tmp_path / "template.yaml").write_text(
        dedent("""
            ServiceVersion:
              Description: the version
              Type: String
              Comment: padding
              Value: '9.9.9'
        """).strip()
        + "\n",
        encoding="utf-8",
    )
    assert read_service_version(tmp_path) is None


def test_a_template_without_the_shape_returns_none(tmp_path: Path) -> None:
    (tmp_path / "template.yaml").write_text(
        "Resources:\n  Fn:\n    Type: AWS::Serverless::Function\n", encoding="utf-8"
    )
    assert read_service_version(tmp_path) is None


# ---------------------------------------------------------------------------
# seller_service: verify_deployed_registry
# ---------------------------------------------------------------------------


class _Cfn:
    def __init__(self, physical_id: str = "fn-abc", error: Exception | None = None):
        self._physical_id = physical_id
        self._error = error

    def describe_stack_resource(self, **_kw):
        if self._error:
            raise self._error
        return {"StackResourceDetail": {"PhysicalResourceId": self._physical_id}}


class _Lambda:
    def __init__(self, registry_raw, error: Exception | None = None):
        self._raw = registry_raw
        self._error = error

    def get_function_configuration(self, **_kw):
        if self._error:
            raise self._error
        if self._raw is None:
            return {}
        return {"Environment": {"Variables": {"PRODUCT_REGISTRY_JSON": self._raw}}}


def test_a_function_that_cannot_be_read_back_is_reported_not_assumed_good() -> None:
    """The read-back exists because a mangled registry is invisible from outside.
    If the read itself fails, reporting success would restore exactly the blind
    spot it was added to close."""
    with pytest.raises(SellerServiceError) as exc:
        verify_deployed_registry(
            cfn_client=_Cfn(error=RuntimeError("AccessDenied")),
            lambda_client=_Lambda("{}"),
            stack_name="s",
            expected_product_ids=["prod-a"],
        )
    assert "could not read the activation function back" in str(exc.value)
    assert "Check it by hand" in str(exc.value)


def test_a_lambda_configuration_read_failure_is_reported_too() -> None:
    with pytest.raises(SellerServiceError, match="could not read"):
        verify_deployed_registry(
            cfn_client=_Cfn(),
            lambda_client=_Lambda("{}", error=RuntimeError("Throttling")),
            stack_name="s",
            expected_product_ids=["prod-a"],
        )


def test_a_registry_that_deployed_as_a_json_array_is_refused() -> None:
    """A JSON array parses, and it is truthy, so an `if deployed:` check would
    accept it — then every product lookup misses and every activation is refused
    as an unknown product, which the caller cannot distinguish from "not
    subscribed"."""
    with pytest.raises(SellerServiceError) as exc:
        verify_deployed_registry(
            cfn_client=_Cfn(),
            lambda_client=_Lambda('["prod-a"]'),
            stack_name="s",
            expected_product_ids=["prod-a"],
        )
    assert "expected a JSON object" in str(exc.value)
    assert "got list" in str(exc.value)
    assert "would not show up in testing" in str(exc.value)


def test_a_registry_truncated_to_a_single_brace_is_refused() -> None:
    """The exact shape SAM's override re-tokenizer produced from unquoted JSON."""
    with pytest.raises(SellerServiceError) as exc:
        verify_deployed_registry(
            cfn_client=_Cfn(),
            lambda_client=_Lambda("{"),
            stack_name="s",
            expected_product_ids=["prod-a"],
        )
    assert "did not survive deployment" in str(exc.value)
    assert "'{'" in str(exc.value)


def test_an_absent_environment_block_is_refused_naming_what_arrived() -> None:
    """Absent, not empty: a function with no Environment at all reads as `""`,
    which must be refused rather than treated as an empty registry."""
    with pytest.raises(SellerServiceError) as exc:
        verify_deployed_registry(
            cfn_client=_Cfn(),
            lambda_client=_Lambda(None),
            stack_name="s",
            expected_product_ids=["prod-a"],
        )
    assert "the function received: ''" in str(exc.value)


def test_a_good_deployment_returns_the_parsed_registry() -> None:
    deployed = verify_deployed_registry(
        cfn_client=_Cfn(),
        lambda_client=_Lambda(json.dumps({"prod-a": {"productCode": "x"}})),
        stack_name="s",
        expected_product_ids=["prod-a"],
    )
    assert deployed == {"prod-a": {"productCode": "x"}}


# ---------------------------------------------------------------------------
# seller_service: utc_now_iso
# ---------------------------------------------------------------------------


def test_the_timestamp_matches_the_shape_latest_json_uses() -> None:
    """Deliberately the same format `publisher._now_iso` writes, so an operator can
    compare an activation pointer and a latest.json by eye. A `+00:00` suffix would
    be the same instant and not comparable at a glance."""
    from datetime import datetime

    stamp = utc_now_iso()
    assert stamp.endswith("Z")
    assert "+00:00" not in stamp
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


# ---------------------------------------------------------------------------
# pack: PackPublisher's two up-front guards
# ---------------------------------------------------------------------------


def test_publishing_a_feature_with_no_pack_section_says_it_is_not_a_pack(
    demo_feature_project: Path, aws_env
) -> None:
    """`publish-pack` on a plain feature has no wrapper to bake. Failing later —
    at the point the wrapper path is read — would already have uploaded the whole
    feature artifact set."""
    with mock_aws():
        with pytest.raises(RuntimeError) as exc:
            PackPublisher(demo_feature_project, console=_console()).publish(
                artifacts_bucket="artifacts-bkt",
                artifacts_prefix="",
                host_template_url="https://h/idp-main.yaml",
                region="us-east-1",
            )
    assert "no `pack:` section" in str(exc.value)
    assert "wrapperTemplatePath" in str(exc.value)


def test_a_missing_wrapper_template_is_refused_before_any_upload(
    demo_feature_project: Path, aws_env
) -> None:
    """The guard has to fire before the feature publish: reaching it afterwards
    leaves a published feature and no wrapper, which is a half-published pack with
    a latest.json already flipped."""
    manifest = demo_feature_project / "feature.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + "\npack:\n  wrapperTemplatePath: deploy.yaml\n",
        encoding="utf-8",
    )
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="artifacts-bkt")
        with pytest.raises(FileNotFoundError) as exc:
            PackPublisher(demo_feature_project, console=_console()).publish(
                artifacts_bucket="artifacts-bkt",
                artifacts_prefix="",
                host_template_url="https://h/idp-main.yaml",
                region="us-east-1",
            )
        assert "wrapperTemplatePath" in str(exc.value)
        assert s3.list_objects_v2(Bucket="artifacts-bkt").get("KeyCount") == 0


# ---------------------------------------------------------------------------
# pack: _assert_publicly_readable
# ---------------------------------------------------------------------------


def test_a_403_on_the_published_wrapper_names_both_likely_causes(
    demo_feature_project: Path, monkeypatch
) -> None:
    """A Quick-Create deploy fetches the wrapper anonymously. Catching the 403 here
    is the difference between a clear message and CloudFormation failing minutes
    later in the *deploying* account — and the two causes (a rejected object ACL,
    account-level Block Public Access) need different fixes."""
    import urllib.error

    def _urlopen(_req, timeout=None):
        raise urllib.error.HTTPError(
            "https://b/deploy.yaml", 403, "Forbidden", {}, None
        )  # type: ignore[arg-type]

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    with pytest.raises(RuntimeError) as exc:
        PackPublisher(demo_feature_project)._assert_publicly_readable(
            "https://b/deploy.yaml"
        )
    assert "HTTP 403" in str(exc.value)
    assert "Block Public Access" in str(exc.value)
    assert "--bucket-basename" in str(exc.value)


def test_a_network_failure_checking_the_wrapper_is_also_fatal(
    demo_feature_project: Path, monkeypatch
) -> None:
    """`--public` was asked for, and an unverifiable wrapper is not a verified one.
    Treating an unreachable check as a pass would publish artifacts believed
    world-readable."""
    import urllib.error

    def _urlopen(_req, timeout=None):
        raise urllib.error.URLError("dns failure")

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    with pytest.raises(RuntimeError, match="Couldn't HEAD the published wrapper"):
        PackPublisher(demo_feature_project)._assert_publicly_readable(
            "https://b/deploy.yaml"
        )


def test_a_reachable_public_wrapper_passes_the_check(
    demo_feature_project: Path, monkeypatch
) -> None:
    """The positive case, and the HEAD must be a HEAD — a GET of every published
    wrapper would download the whole object to answer a yes/no question."""
    seen: list = []

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def _urlopen(req, timeout=None):
        seen.append(req.get_method())
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    PackPublisher(demo_feature_project)._assert_publicly_readable(
        "https://b/deploy.yaml"
    )
    assert seen == ["HEAD"]


# ---------------------------------------------------------------------------
# pack: ensure_artifacts_bucket
# ---------------------------------------------------------------------------


def test_the_bucket_name_is_derived_from_the_account_and_region(aws_env) -> None:
    """One convention shared with `idp-cli deploy --from-code`, so the same bucket
    serves host-only and pack deploys. A different name would silently split the
    artifacts across two buckets and a pack's baked host URL would point at the
    one the host was not published to."""
    with mock_aws():
        account = boto3.client("sts", region_name="us-east-1").get_caller_identity()[
            "Account"
        ]
        bucket = ensure_artifacts_bucket(region="us-east-1", console=_console())
        assert bucket == f"idp-accelerator-artifacts-{account}-us-east-1"
        assert boto3.client("s3", region_name="us-east-1").head_bucket(Bucket=bucket)


def test_a_bucket_outside_us_east_1_is_created_with_a_location_constraint(
    aws_env,
) -> None:
    """`CreateBucket` without a LocationConstraint always makes a us-east-1 bucket.
    Getting this wrong publishes every region's artifacts into one region, and a
    stack elsewhere reads them cross-region or not at all."""
    with mock_aws():
        bucket = ensure_artifacts_bucket(region="eu-west-1", console=_console())
        assert bucket.endswith("-eu-west-1")
        location = boto3.client("s3", region_name="eu-west-1").get_bucket_location(
            Bucket=bucket
        )
        assert location["LocationConstraint"] == "eu-west-1"


def test_a_new_bucket_gets_block_public_access_and_the_tls_policy(aws_env) -> None:
    """Private by default. Both halves matter: BPA stops an accidental public
    object, and the TLS-only statement denies plain-HTTP reads of artifacts that
    include Lambda code."""
    with mock_aws():
        bucket = ensure_artifacts_bucket(region="us-east-1", console=_console())
        s3 = boto3.client("s3", region_name="us-east-1")
        pab = s3.get_public_access_block(Bucket=bucket)[
            "PublicAccessBlockConfiguration"
        ]
        assert pab == {
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        }
        policy = json.loads(s3.get_bucket_policy(Bucket=bucket)["Policy"])
    assert [s["Sid"] for s in policy["Statement"]] == ["EnforceSSLOnly"]


def test_an_existing_bucket_keeps_its_own_block_public_access_settings(
    aws_env,
) -> None:
    """The asymmetry the docstring calls out. An operator who deliberately relaxed
    BPA on their own bucket must not have it re-tightened by a publish, and one who
    tightened it must not have it loosened — so a pre-existing bucket is left
    exactly as found."""
    with mock_aws():
        account = boto3.client("sts", region_name="us-east-1").get_caller_identity()[
            "Account"
        ]
        name = f"idp-accelerator-artifacts-{account}-us-east-1"
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=name)
        s3.put_public_access_block(
            Bucket=name,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": False,
                "IgnorePublicAcls": False,
                "BlockPublicPolicy": False,
                "RestrictPublicBuckets": False,
            },
        )
        console = _console()
        assert ensure_artifacts_bucket(region="us-east-1", console=console) == name
        pab = s3.get_public_access_block(Bucket=name)["PublicAccessBlockConfiguration"]
    assert pab["BlockPublicPolicy"] is False
    assert pab["RestrictPublicBuckets"] is False
    assert "left unchanged" in " ".join(console.export_text().split())


def test_make_public_relaxes_the_policy_flags_on_an_owned_bucket(aws_env) -> None:
    """The opt-in path. Only the two *policy* flags are relaxed; ACL blocking stays
    on, because modern buckets default to BucketOwnerEnforced and reject ACLs
    entirely — the public read has to come from the bucket policy."""
    with mock_aws():
        bucket = ensure_artifacts_bucket(
            region="us-east-1", console=_console(), make_public=True
        )
        s3 = boto3.client("s3", region_name="us-east-1")
        pab = s3.get_public_access_block(Bucket=bucket)[
            "PublicAccessBlockConfiguration"
        ]
        policy = json.loads(s3.get_bucket_policy(Bucket=bucket)["Policy"])
    assert pab["BlockPublicPolicy"] is False
    assert pab["BlockPublicAcls"] is True
    assert sorted(s["Sid"] for s in policy["Statement"]) == [
        "EnforceSSLOnly",
        "PackPublicArtifactsRead",
    ]


def test_a_creation_failure_is_reported_with_the_bucket_name(aws_env) -> None:
    """Naming the bucket is what tells the operator whether the failure is a name
    collision with another account's bucket (a global namespace) or a permissions
    problem."""
    with mock_aws():

        class _CannotCreate:
            def head_bucket(self, **_kw):
                raise RuntimeError("404 Not Found")

            def create_bucket(self, **_kw):
                raise RuntimeError("BucketAlreadyExists")

        import idp_feature_sdk.pack as pack_mod

        real_client = boto3.client

        def _client(service, **kwargs):
            return (
                _CannotCreate() if service == "s3" else real_client(service, **kwargs)
            )

        original = pack_mod.boto3.client
        pack_mod.boto3.client = _client  # type: ignore[assignment]
        try:
            with pytest.raises(RuntimeError) as exc:
                ensure_artifacts_bucket(region="us-east-1", console=_console())
        finally:
            pack_mod.boto3.client = original  # type: ignore[assignment]
    assert "Failed to create artifacts bucket" in str(exc.value)
    assert "idp-accelerator-artifacts-" in str(exc.value)


def test_an_access_error_on_head_bucket_is_not_mistaken_for_absence(aws_env) -> None:
    """Only a 404 means "create it". A 403 means the name is taken by another
    account, and attempting a create would fail with a different, less helpful
    error — so the access failure is surfaced as-is."""
    with mock_aws():

        class _Forbidden:
            def head_bucket(self, **_kw):
                raise RuntimeError("An error occurred (403) when calling HeadBucket")

            def create_bucket(self, **_kw):
                raise AssertionError("must not attempt to create")

        import idp_feature_sdk.pack as pack_mod

        real_client = boto3.client

        def _client(service, **kwargs):
            return _Forbidden() if service == "s3" else real_client(service, **kwargs)

        original = pack_mod.boto3.client
        pack_mod.boto3.client = _client  # type: ignore[assignment]
        try:
            with pytest.raises(RuntimeError, match="Cannot access artifacts bucket"):
                ensure_artifacts_bucket(region="us-east-1", console=_console())
        finally:
            pack_mod.boto3.client = original  # type: ignore[assignment]


def test_reusing_an_existing_bucket_is_idempotent(aws_env) -> None:
    """Publishing repeatedly must not accumulate policy statements or flip any
    setting — the second call is the normal case."""
    with mock_aws():
        first = ensure_artifacts_bucket(region="us-east-1", console=_console())
        console = _console()
        second = ensure_artifacts_bucket(region="us-east-1", console=console)
        assert first == second
        policy = json.loads(
            boto3.client("s3", region_name="us-east-1").get_bucket_policy(
                Bucket=second
            )["Policy"]
        )
    assert len(policy["Statement"]) == 1
    assert "using existing artifacts bucket" in " ".join(console.export_text().split())


def test_a_non_200_head_of_the_wrapper_is_treated_as_not_public(
    demo_feature_project: Path, monkeypatch
) -> None:
    """A redirect or a 204 is not a readable object. `urlopen` does not raise for
    every non-200, so the status has to be checked explicitly — a permissive check
    would report a wrapper public that CloudFormation cannot fetch."""

    class _Redirected:
        status = 302

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda _req, timeout=None: _Redirected()
    )
    with pytest.raises(RuntimeError) as exc:
        PackPublisher(demo_feature_project)._assert_publicly_readable(
            "https://b/deploy.yaml"
        )
    assert "HTTP 302" in str(exc.value)


def test_a_failure_to_enable_block_public_access_on_a_new_bucket_is_fatal(
    aws_env,
) -> None:
    """A freshly created artifacts bucket with BPA not enabled is a public bucket
    holding Lambda code. Warning and carrying on would publish into it."""
    import idp_feature_sdk.pack as pack_mod

    class _NoBpa:
        def head_bucket(self, **_kw):
            raise RuntimeError("404 Not Found")

        def create_bucket(self, **_kw):
            return {}

        def get_bucket_policy(self, **_kw):
            raise RuntimeError("NoSuchBucketPolicy")

        def put_bucket_policy(self, **_kw):
            return {}

        def put_public_access_block(self, **_kw):
            raise RuntimeError("AccessDenied: s3:PutBucketPublicAccessBlock")

    with mock_aws():
        real_client = boto3.client

        def _client(service, **kwargs):
            return _NoBpa() if service == "s3" else real_client(service, **kwargs)

        original = pack_mod.boto3.client
        pack_mod.boto3.client = _client  # type: ignore[assignment]
        try:
            with pytest.raises(RuntimeError) as exc:
                ensure_artifacts_bucket(region="us-east-1", console=_console())
        finally:
            pack_mod.boto3.client = original  # type: ignore[assignment]
    assert "Block Public Access" in str(exc.value)
