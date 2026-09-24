# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Everything in ``idp_sdk._core.stack`` that decides *what gets deployed*.

The module under test is the SDK's CloudFormation deployer. This file covers the
half of it that runs **before** any stack operation: turning CLI-shaped arguments
into a CloudFormation parameter set (``build_parameters``), deciding whether a
config argument is a local file or an S3 URI (``is_local_file_path``,
``validate_s3_uri``), staging a local file or an oversized template into S3
(``get_or_create_config_bucket``, ``upload_local_config``,
``StackDeployer._upload_template_to_s3``, ``StackDeployer._read_template``),
reading a live stack's current parameters (``_get_stack_parameters``) and asking
a template which parameters it accepts (``_get_template_parameters``).

What shaped the tests: every wrong answer here is silent and expensive. A
parameter name this module emits that the template does not declare fails the
whole deploy at CreateStack with "Parameters: [X] do not exist in the template"
and reaches no resource — that is the ``EnableHITL`` regression recorded in
``build_parameters``' own comments, which shipped for several releases because
the tests asserted the name the function produced rather than the names the
template accepts. A creation-fixed parameter that slips through can leave a
stack in ``UPDATE_ROLLBACK_FAILED`` (#835). A merge that silently falls back to
the unmerged user config deploys a configuration the operator did not ask for.

So the assertions here are on **values and stored state**, not on calls: where a
bucket or object is involved the test reads it back out of moto and checks the
region, the versioning, the encryption, the lifecycle rule, the exact key and the
exact bytes. ``botocore.stub.Stubber`` is used where moto's CloudFormation is too
thin (its ``validate_template`` reports no parameters at all), because a Stubber
still validates the request against the real service model — a ``MagicMock``
would accept a malformed one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import boto3
import pytest
import yaml
from botocore.stub import ANY, Stubber
from moto import mock_aws

from idp_sdk._core.stack import (
    CREATION_FIXED_PARAMETERS,
    StackDeployer,
    build_parameters,
    get_or_create_config_bucket,
    guard_creation_fixed_parameters,
    is_local_file_path,
    upload_local_config,
    validate_s3_uri,
)

pytestmark = pytest.mark.unit

FIXED = "ExternalIdPEmailMutable"

# A template small enough to pass inline, whose every parameter is referenced —
# moto's validate_template rejects a template with an unused parameter.
SMALL_TEMPLATE = {
    "AWSTemplateFormatVersion": "2010-09-09",
    "Parameters": {
        "AdminEmail": {"Type": "String"},
        "LogLevel": {"Type": "String", "Default": "INFO"},
    },
    "Resources": {
        "Topic": {
            "Type": "AWS::SNS::Topic",
            "Properties": {
                "DisplayName": {"Ref": "AdminEmail"},
                "TopicName": {"Ref": "LogLevel"},
            },
        }
    },
}


def _deployer(region: str | None = "us-east-1") -> StackDeployer:
    return StackDeployer(region=region)


@pytest.fixture
def ssl_only_hardening_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record — and neutralise — the ``EnforceSSLOnly`` bucket policy.

    This exists for a **moto limitation**, not for anything about the code.
    ``get_or_create_config_bucket`` puts a bucket policy denying ``s3:*`` when
    ``aws:SecureTransport`` is false, and moto never populates that condition key,
    so its policy evaluator reads the key as absent, considers the condition
    satisfied and applies the Deny to every subsequent request. Any real
    ``put_object`` into a bucket the code just hardened therefore answers 403
    under moto, which would make every upload test below untestable.

    The replacement records ``(bucket, region)`` so a test can still assert the
    hardening was applied to the right bucket, and the policy's actual content is
    covered end to end against moto in ``test_s3_enforce_ssl_only.py``.
    """
    calls: list[tuple[str, str]] = []

    def record(
        _s3_client: Any, bucket: str, region: str, raise_on_error: bool = True
    ) -> bool:
        assert raise_on_error in (True, False)
        calls.append((bucket, region))
        return True

    monkeypatch.setattr("idp_sdk._core.stack.apply_enforce_ssl_only", record)
    return calls


# ---------------------------------------------------------------------------
# is_local_file_path / validate_s3_uri
#
# These two decide whether `--custom-config X` is uploaded or passed through. A
# false "local" on an S3 URI tries to open it as a file and fails; a false "S3"
# on a local path hands CloudFormation a CustomConfigPath it cannot read, and the
# stack deploys with the wrong configuration. So the near-misses matter as much
# as the happy paths.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected_local"),
    [
        ("s3://bucket/key.yaml", False),
        ("s3://bucket/a/b/c.yaml", False),
        ("s3://bucket", False),  # malformed S3 URI is still not a local path
        ("s3:/bucket/key.yaml", True),  # one slash: not an S3 URI at all
        ("s3:bucket/key.yaml", True),
        ("/abs/path/config.yaml", True),
        ("./relative.yaml", True),
        ("config.yaml", True),
        (r"C:\configs\config.yaml", True),  # Windows path
        ("https://example.com/config.yaml", True),
        ("S3://Bucket/Key.yaml", True),  # scheme is case-sensitive here
    ],
)
def test_is_local_file_path_classifies_each_shape(path: str, expected_local: bool):
    """`S3://` counts as *local* because the check is a literal `s3://` prefix.

    That matches the AWS CLI, which also rejects an uppercase scheme, so the
    uppercase form fails loudly at open() rather than being silently uploaded.
    """
    assert is_local_file_path(path) is expected_local


@pytest.mark.parametrize(
    ("uri", "valid"),
    [
        ("s3://bucket/key.yaml", True),
        ("s3://bucket/nested/key.yaml", True),
        ("s3://bucket/", False),  # bucket but no key
        ("s3://bucket", False),  # no key separator at all
        ("s3://", False),
        ("s3:///key.yaml", False),  # empty bucket name
        ("s3:/bucket/key.yaml", False),  # single slash
        ("/local/path.yaml", False),
        ("", False),
        ("https://s3.amazonaws.com/bucket/key", False),
    ],
)
def test_validate_s3_uri_accepts_only_bucket_and_key(uri: str, valid: bool):
    assert validate_s3_uri(uri) is valid


def test_validate_s3_uri_accepts_a_double_slash_as_a_key():
    """`s3://bucket//` passes: the key is the single character `/`.

    Pinning current behaviour. The split is on the first `/` only, so the
    remainder `"/"` is non-empty and the URI validates. A leading-slash key is
    legal in S3, so this is a loose validation rather than a wrong one — but a
    caller who typed a trailing slash by accident gets an object at key `/`
    instead of an error.
    """
    assert validate_s3_uri("s3://bucket//") is True


def test_validate_s3_uri_does_not_check_the_bucket_name():
    """Format only — bucket naming rules are S3's job, not this function's."""
    assert validate_s3_uri("s3://Not_A_Valid_Bucket_Name/key") is True


# ---------------------------------------------------------------------------
# build_parameters
# ---------------------------------------------------------------------------


def test_no_arguments_produces_no_parameters():
    """The regression this function's own comment records.

    `EnableHITL` was emitted here for several releases after the root template
    stopped declaring it, so `idp-cli deploy --enable-hitl true` failed at
    CreateStack with "Parameters: [EnableHITL] do not exist in the template" and
    reached no resource. Any parameter appearing unbidden has that consequence,
    so the assertion is that the set is empty, not that one name is absent.
    """
    assert build_parameters() == {}


def test_only_explicitly_provided_values_appear():
    assert build_parameters(admin_email="a@b.c") == {"AdminEmail": "a@b.c"}
    assert build_parameters(log_level="DEBUG") == {"LogLevel": "DEBUG"}


def test_zero_max_concurrent_is_kept_and_stringified():
    """`0` is falsy but explicitly provided, so it must survive.

    A `if max_concurrent:` in place of the `is not None` check would silently
    drop it and the stack would keep its previous concurrency limit — the
    opposite of what the operator asked for. CloudFormation parameters are
    strings, so the value must be `"0"`, not `0`.
    """
    assert build_parameters(max_concurrent=0) == {"MaxConcurrentWorkflows": "0"}
    assert build_parameters(max_concurrent=250) == {"MaxConcurrentWorkflows": "250"}


def test_empty_log_level_is_kept_rather_than_dropped():
    """An explicitly empty string is a value; only `None` means "not provided"."""
    assert build_parameters(log_level="") == {"LogLevel": ""}


def test_additional_params_are_merged_and_win_on_conflict():
    """`additional_params` is applied last, so it overrides a named argument."""
    out = build_parameters(
        admin_email="named@example.com",
        log_level="INFO",
        additional_params={"AdminEmail": "override@example.com", "Extra": "1"},
    )
    assert out == {
        "AdminEmail": "override@example.com",
        "LogLevel": "INFO",
        "Extra": "1",
    }


def test_an_s3_custom_config_is_passed_through_without_upload():
    out = build_parameters(custom_config="s3://my-bucket/my/config.yaml")
    assert out == {"CustomConfigPath": "s3://my-bucket/my/config.yaml"}


@pytest.mark.parametrize("bad", ["s3://bucket", "s3://", "s3://bucket/"])
def test_a_malformed_s3_custom_config_is_refused_before_deploying(bad: str):
    with pytest.raises(ValueError, match="Invalid S3 URI format"):
        build_parameters(custom_config=bad)


def test_a_local_custom_config_without_a_resolvable_region_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Uploading needs a region; failing here beats failing mid-upload.

    The message must name the two ways to supply one, because this is the only
    signal the operator gets.
    """
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("classification: {}\n")

    with pytest.raises(ValueError, match="Region could not be determined"):
        build_parameters(custom_config=str(cfg), region=None)


def test_a_local_custom_config_is_uploaded_and_the_s3_uri_returned(
    aws_credentials: str, tmp_path: Path, ssl_only_hardening_calls: list
):
    """End to end, with the real `idp_common` merge: the parameter value must
    point at bytes that exist, and the operator's own value must survive.

    `build_parameters` leaves `merge_with_defaults` at its default of True, so
    what lands in S3 is the user's minimal config merged over the system defaults.
    The assertion that matters is that the one value the operator set is the one
    in the uploaded document — a merge that applied the defaults *over* the user's
    config instead of under it would deploy the default model and nothing would
    say so.
    """
    cfg = tmp_path / "my-config.yaml"
    cfg.write_text("extraction:\n  model: nova-lite\n")

    with mock_aws():
        out = build_parameters(custom_config=str(cfg), region=aws_credentials)
        uri = out["CustomConfigPath"]
        assert validate_s3_uri(uri)
        bucket, key = uri[len("s3://") :].split("/", 1)
        body = (
            boto3.client("s3", region_name=aws_credentials)
            .get_object(Bucket=bucket, Key=key)["Body"]
            .read()
        )
    uploaded = yaml.safe_load(body)
    assert uploaded["extraction"]["model"] == "nova-lite"
    # The merge really happened: sections the user never wrote are present.
    assert len(uploaded) > 1


# ---------------------------------------------------------------------------
# guard_creation_fixed_parameters — extensions to test_creation_fixed_parameters.py
# ---------------------------------------------------------------------------


def test_none_parameters_yields_an_empty_dict():
    """`deploy_stack` passes its `parameters` straight through, and it defaults
    to None; returning None here would break the dict comprehension that builds
    the CloudFormation parameter list."""
    assert guard_creation_fixed_parameters(None, stack_exists=False) == {}
    assert guard_creation_fixed_parameters(None, stack_exists=True) == {}


def test_current_params_of_none_is_treated_as_the_template_default():
    """`_get_stack_parameters` returns {} on failure, but None is also reachable
    from a caller that did not look them up. Both must mean "the stack has the
    template default", not "anything goes"."""
    assert guard_creation_fixed_parameters(
        {FIXED: CREATION_FIXED_PARAMETERS[FIXED]},
        stack_exists=True,
        current_params=None,
    ) == {FIXED: CREATION_FIXED_PARAMETERS[FIXED]}

    with pytest.raises(ValueError):
        guard_creation_fixed_parameters(
            {FIXED: "true"}, stack_exists=True, current_params=None
        )


def test_the_refusal_names_both_values_and_the_consequence():
    """The operator's only recourse is this message, so it must carry the actual
    values and say what applying it would do."""
    with pytest.raises(ValueError) as excinfo:
        guard_creation_fixed_parameters(
            {FIXED: "true"}, stack_exists=True, current_params={FIXED: "false"}
        )
    message = str(excinfo.value)
    assert "the stack has 'false'" in message
    assert "asked for 'true'" in message
    assert "UPDATE_ROLLBACK_FAILED" in message
    assert "deploy a new stack" in message


def test_dropping_on_create_warns_with_the_parameter_name(
    caplog: pytest.LogCaptureFixture,
):
    """Silently dropping it would leave the operator believing the flag applied."""
    with caplog.at_level("WARNING", logger="idp_sdk._core.stack"):
        out = guard_creation_fixed_parameters(
            {FIXED: "true", "AdminEmail": "a@b.c"},
            stack_exists=False,
            declared_params={"AdminEmail"},
        )
    assert out == {"AdminEmail": "a@b.c"}
    assert FIXED in caplog.text


def test_other_parameters_are_never_examined():
    """Only the registered creation-fixed names are policed; a differing value
    for anything else is the ordinary case of an update."""
    out = guard_creation_fixed_parameters(
        {"LogLevel": "DEBUG", "MaxConcurrentWorkflows": "5"},
        stack_exists=True,
        current_params={"LogLevel": "INFO", "MaxConcurrentWorkflows": "100"},
    )
    assert out == {"LogLevel": "DEBUG", "MaxConcurrentWorkflows": "5"}


# ---------------------------------------------------------------------------
# _read_template
# ---------------------------------------------------------------------------


def test_read_template_returns_the_file_verbatim(aws_credentials: str, tmp_path: Path):
    template = tmp_path / "t.yaml"
    text = "Resources:\n  T:\n    Type: AWS::SNS::Topic\n# ünïcode ✓\n"
    template.write_text(text, encoding="utf-8")
    assert _deployer()._read_template(str(template)) == text


def test_read_template_names_the_missing_path(aws_credentials: str, tmp_path: Path):
    missing = tmp_path / "nope.yaml"
    with pytest.raises(FileNotFoundError, match=re.escape(str(missing))):
        _deployer()._read_template(str(missing))


def test_read_template_on_a_directory_raises_is_a_directory(
    aws_credentials: str, tmp_path: Path
):
    """Pinning a rough edge: `Path.exists()` is true for a directory, so the
    friendly "Template not found" message is skipped and the raw
    `IsADirectoryError` from `read_text()` surfaces instead."""
    with pytest.raises(IsADirectoryError):
        _deployer()._read_template(str(tmp_path))


# ---------------------------------------------------------------------------
# _upload_template_to_s3
# ---------------------------------------------------------------------------


def test_upload_template_stores_the_bytes_encrypted_under_a_dated_key(
    aws_credentials: str, ssl_only_hardening_calls: list
):
    """A template over CloudFormation's 51,200-byte inline limit goes to S3, and
    the returned URL is the only thing CloudFormation is given — so the object
    must really be at that key, byte-identical, and server-side encrypted."""
    body = "Resources:\n  T:\n    Type: AWS::SNS::Topic\n"
    with mock_aws():
        url = _deployer()._upload_template_to_s3(body, "MyStack")

        match = re.fullmatch(r"https://s3\.us-east-1\.amazonaws\.com/([^/]+)/(.+)", url)
        assert match, url
        bucket, key = match.group(1), match.group(2)
        assert re.fullmatch(r"idp-cli/templates/MyStack_\d{8}_\d{6}\.yaml", key), key

        s3 = boto3.client("s3", region_name="us-east-1")
        obj = s3.get_object(Bucket=bucket, Key=key)
        assert obj["Body"].read().decode("utf-8") == body
        assert obj["ServerSideEncryption"] == "AES256"


def test_upload_template_without_a_region_crashes_on_the_bucket_name(
    aws_credentials: str,
):
    """DEFECT, pinned as-is: a deployer built without an explicit region cannot
    stage an oversized template.

    `StackDeployer(region=None)` is the documented default and is what
    `IDPClient(stack_name=...)` produces when no region is passed
    (``idp_sdk/client.py`` sets ``self._region = region``). The staging path then
    calls ``get_or_create_config_bucket(None)``, whose first use of the value is
    ``region.replace("-", "")`` at ``_core/stack.py:2348`` — an ``AttributeError``
    on ``NoneType``, not a message an operator can act on.

    It is not an edge case: ``deploy_stack`` takes this path for *any* local
    template over 51,200 bytes, and the accelerator's own ``template.yaml`` is far
    over that. Had the AttributeError not fired, the URL built at line 331 would
    have read ``https://s3.None.amazonaws.com/...``.
    """
    with mock_aws():
        with pytest.raises(AttributeError, match="NoneType"):
            _deployer(region=None)._upload_template_to_s3("Resources: {}", "MyStack")


# ---------------------------------------------------------------------------
# _get_stack_parameters
# ---------------------------------------------------------------------------


def test_stack_parameters_are_read_back_from_a_live_stack(aws_credentials: str):
    """Defaults the caller never passed are included, because an update that
    omitted them would otherwise not know to send UsePreviousValue for them."""
    with mock_aws():
        deployer = _deployer()
        deployer.cfn.create_stack(
            StackName="S1",
            TemplateBody=json.dumps(SMALL_TEMPLATE),
            Parameters=[{"ParameterKey": "AdminEmail", "ParameterValue": "a@b.c"}],
        )
        assert deployer._get_stack_parameters("S1") == {
            "AdminEmail": "a@b.c",
            "LogLevel": "INFO",
        }


def test_stack_parameters_of_a_missing_stack_are_empty_not_an_error(
    aws_credentials: str,
):
    """`deploy_stack` calls this only when the stack exists, but a race (deleted
    between the two calls) must not crash the deploy."""
    with mock_aws():
        assert _deployer()._get_stack_parameters("never-existed") == {}


def test_a_parameter_without_a_key_is_skipped(aws_credentials: str):
    deployer = _deployer()
    with Stubber(deployer.cfn) as stub:
        stub.add_response(
            "describe_stacks",
            {
                "Stacks": [
                    {
                        "StackName": "S1",
                        "CreationTime": "2024-01-01T00:00:00Z",
                        "StackStatus": "CREATE_COMPLETE",
                        "Parameters": [
                            {"ParameterKey": "Good", "ParameterValue": "1"},
                            {"ParameterValue": "orphaned"},
                        ],
                    }
                ]
            },
            {"StackName": "S1"},
        )
        assert deployer._get_stack_parameters("S1") == {"Good": "1"}


def test_an_empty_stack_list_yields_no_parameters(aws_credentials: str):
    deployer = _deployer()
    with Stubber(deployer.cfn) as stub:
        stub.add_response("describe_stacks", {"Stacks": []}, {"StackName": "S1"})
        assert deployer._get_stack_parameters("S1") == {}


# ---------------------------------------------------------------------------
# _get_template_parameters
#
# This set is what decides which of a live stack's parameters are "deprecated"
# and dropped from an update. Returning too few drops parameters the new template
# still wants (they revert to defaults); returning a wrong set on error would
# drop everything, which is why the error path returns the empty set — the
# documented "preserve all" signal.
# ---------------------------------------------------------------------------


def test_template_parameters_come_from_a_template_body(aws_credentials: str):
    deployer = _deployer()
    with Stubber(deployer.cfn) as stub:
        stub.add_response(
            "validate_template",
            {
                "Parameters": [
                    {"ParameterKey": "AdminEmail"},
                    {"ParameterKey": "LogLevel"},
                ]
            },
            {"TemplateBody": "BODY"},
        )
        assert deployer._get_template_parameters(template_body="BODY") == {
            "AdminEmail",
            "LogLevel",
        }


def test_a_template_url_is_preferred_over_a_body(aws_credentials: str):
    """Both are set when an oversized template was staged to S3; the URL is the
    authoritative one, and sending both would be rejected by CloudFormation."""
    deployer = _deployer()
    with Stubber(deployer.cfn) as stub:
        stub.add_response(
            "validate_template",
            {"Parameters": [{"ParameterKey": "OnlyFromUrl"}]},
            {"TemplateURL": "https://s3/x.yaml"},
        )
        assert deployer._get_template_parameters(
            template_body="BODY", template_url="https://s3/x.yaml"
        ) == {"OnlyFromUrl"}


def test_no_template_yields_an_empty_set_and_a_warning(
    aws_credentials: str, caplog: pytest.LogCaptureFixture
):
    with caplog.at_level("WARNING", logger="idp_sdk._core.stack"):
        assert _deployer()._get_template_parameters() == set()
    assert "No template provided" in caplog.text


def test_a_validation_failure_yields_an_empty_set(aws_credentials: str):
    """Empty means "unknown", and every caller treats unknown as "change
    nothing" — so a transient validate_template failure must not be allowed to
    look like "the template declares no parameters"."""
    deployer = _deployer()
    with Stubber(deployer.cfn) as stub:
        stub.add_client_error("validate_template", service_error_code="ValidationError")
        assert deployer._get_template_parameters(template_body="BODY") == set()


def test_parameters_without_a_key_are_filtered_out(aws_credentials: str):
    deployer = _deployer()
    with Stubber(deployer.cfn) as stub:
        stub.add_response(
            "validate_template",
            {"Parameters": [{"ParameterKey": "Real"}, {"Description": "no key"}]},
            {"TemplateBody": "BODY"},
        )
        assert deployer._get_template_parameters(template_body="BODY") == {"Real"}


# ---------------------------------------------------------------------------
# get_or_create_config_bucket
# ---------------------------------------------------------------------------


def test_a_new_config_bucket_is_created_hardened_in_the_requested_region():
    """The staging bucket holds customer configuration, so every protection the
    function claims to apply is read back off the live bucket rather than
    inferred from the calls that were made."""
    region = "us-west-2"
    with mock_aws():
        name = get_or_create_config_bucket(region)
        s3 = boto3.client("s3", region_name=region)

        assert re.fullmatch(r"idp-cli-config-123456789012-uswest2-[a-z0-9]{8}", name), (
            name
        )
        assert s3.get_bucket_location(Bucket=name)["LocationConstraint"] == region
        assert s3.get_bucket_versioning(Bucket=name)["Status"] == "Enabled"

        rules = s3.get_bucket_encryption(Bucket=name)[
            "ServerSideEncryptionConfiguration"
        ]["Rules"]
        assert (
            rules[0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"
        )

        lifecycle = s3.get_bucket_lifecycle_configuration(Bucket=name)["Rules"]
        assert len(lifecycle) == 1
        assert lifecycle[0]["ID"] == "DeleteOldConfigs"
        assert lifecycle[0]["Status"] == "Enabled"
        assert lifecycle[0]["Expiration"]["Days"] == 30

        tags = {
            t["Key"]: t["Value"] for t in s3.get_bucket_tagging(Bucket=name)["TagSet"]
        }
        assert tags == {"CreatedBy": "idp-cli", "Purpose": "config-staging"}


def test_us_east_1_is_created_without_a_location_constraint(aws_credentials: str):
    """S3 rejects a CreateBucketConfiguration naming us-east-1, so that region
    takes a separate call. A regression here fails every us-east-1 deploy."""
    with mock_aws():
        name = get_or_create_config_bucket("us-east-1")
        assert name.startswith("idp-cli-config-123456789012-useast1-")
        location = boto3.client("s3", region_name="us-east-1").get_bucket_location(
            Bucket=name
        )["LocationConstraint"]
        assert location in (None, "", "us-east-1")


def test_an_existing_bucket_for_this_account_and_region_is_reused(aws_credentials: str):
    with mock_aws():
        first = get_or_create_config_bucket("us-east-1")
        assert get_or_create_config_bucket("us-east-1") == first


def test_a_bucket_belonging_to_another_account_or_region_is_not_reused(
    aws_credentials: str,
):
    """The prefix carries the account id and the region, and reuse keys on it.

    Matching loosely would either hand back a bucket in the wrong region — every
    subsequent CloudFormation TemplateURL then points at a region CloudFormation
    will not read from — or a bucket this account cannot write to.
    """
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="idp-cli-config-999999999999-useast1-deadbeef")
        s3.create_bucket(Bucket="idp-cli-config-123456789012-uswest2-deadbeef")

        chosen = get_or_create_config_bucket("us-east-1")
        assert chosen.startswith("idp-cli-config-123456789012-useast1-")
        assert chosen != "idp-cli-config-999999999999-useast1-deadbeef"


def test_a_failure_to_resolve_the_account_is_reported_not_swallowed(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch
):
    """Without an account id the bucket name is unpredictable, so the function
    must refuse rather than invent one."""
    real_client = boto3.client

    def fake_client(service: str, *args: Any, **kwargs: Any):
        client = real_client(service, *args, **kwargs)
        if service == "sts":
            stub = Stubber(client)
            stub.add_client_error(
                "get_caller_identity", service_error_code="ExpiredToken"
            )
            stub.activate()
        return client

    with mock_aws():
        monkeypatch.setattr(boto3, "client", fake_client)
        with pytest.raises(Exception, match="Failed to get AWS account ID"):
            get_or_create_config_bucket("us-east-1")


def test_a_failure_to_list_buckets_still_creates_one(
    aws_credentials: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """Reuse is an optimisation; a denied s3:ListAllMyBuckets must not stop a
    deploy, it must fall through to creating a fresh bucket."""
    real_client = boto3.client
    created: list[str] = []

    def fake_client(service: str, *args: Any, **kwargs: Any):
        client = real_client(service, *args, **kwargs)
        if service == "s3":
            original_list = client.list_buckets

            def boom(*_a: Any, **_k: Any):
                raise RuntimeError("AccessDenied: ListAllMyBuckets")

            client.list_buckets = boom  # type: ignore[method-assign]
            created.append("patched")
            assert original_list is not None
        return client

    with mock_aws():
        monkeypatch.setattr(boto3, "client", fake_client)
        with caplog.at_level("WARNING", logger="idp_sdk._core.stack"):
            name = get_or_create_config_bucket("us-east-1")
    assert name.startswith("idp-cli-config-123456789012-useast1-")
    assert "Error listing buckets" in caplog.text


def test_a_failure_to_create_the_bucket_is_wrapped_with_context(
    aws_credentials: str, monkeypatch: pytest.MonkeyPatch
):
    """An unwrapped botocore error here is indistinguishable from a dozen other
    S3 failures in the deploy log, and this one means the deploy cannot proceed
    at all."""
    real_client = boto3.client

    def fake_client(service: str, *args: Any, **kwargs: Any):
        client = real_client(service, *args, **kwargs)
        if service == "s3":

            def denied(*_a: Any, **_k: Any):
                raise RuntimeError("AccessDenied: s3:CreateBucket")

            client.create_bucket = denied  # type: ignore[method-assign]
        return client

    with mock_aws():
        monkeypatch.setattr(boto3, "client", fake_client)
        with pytest.raises(
            Exception, match="Failed to create config bucket"
        ) as excinfo:
            get_or_create_config_bucket("us-east-1")
    assert "s3:CreateBucket" in str(excinfo.value)


# ---------------------------------------------------------------------------
# upload_local_config
# ---------------------------------------------------------------------------


def _read_uploaded(uri: str, region: str) -> dict:
    bucket, key = uri[len("s3://") :].split("/", 1)
    body = (
        boto3.client("s3", region_name=region)
        .get_object(Bucket=bucket, Key=key)["Body"]
        .read()
    )
    return yaml.safe_load(body)


def test_a_missing_config_file_is_refused_before_touching_s3(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="Config file not found"):
        upload_local_config(str(tmp_path / "absent.yaml"), "us-east-1")


def test_unmerged_upload_stores_the_users_config_at_a_sanitised_key(
    aws_credentials: str, tmp_path: Path, ssl_only_hardening_calls: list
):
    """Hyphens in the filename become underscores, and the key carries a
    timestamp so two deploys of the same file do not overwrite each other."""
    cfg = tmp_path / "bank-statement-sample.yaml"
    config = {"extraction": {"model": "nova-pro"}, "classification": {}}
    cfg.write_text(yaml.dump(config))

    with mock_aws():
        uri = upload_local_config(str(cfg), "us-east-1", merge_with_defaults=False)
        assert _read_uploaded(uri, "us-east-1") == config
        bucket, key = uri[len("s3://") :].split("/", 1)
        assert bucket.startswith("idp-cli-config-123456789012-useast1-")
        assert re.fullmatch(
            r"idp-cli/custom-configurations/config_\d{8}_\d{6}_bank_statement_sample\.yaml",
            key,
        ), key
        assert (
            boto3.client("s3", region_name="us-east-1").get_object(
                Bucket=bucket, Key=key
            )["ServerSideEncryption"]
            == "AES256"
        )


def test_an_empty_config_file_uploads_an_empty_mapping(
    aws_credentials: str, tmp_path: Path, ssl_only_hardening_calls: list
):
    """`yaml.safe_load("")` is None; uploading None would write the literal
    `null` and the stack would read no configuration at all."""
    cfg = tmp_path / "empty.yaml"
    cfg.write_text("")
    with mock_aws():
        uri = upload_local_config(str(cfg), "us-east-1", merge_with_defaults=False)
        assert _read_uploaded(uri, "us-east-1") == {}


@pytest.mark.parametrize(
    ("classification_method", "expected_pattern"),
    [
        ("bda", "pattern-1"),
        ("multimodalPageLevelClassification", "pattern-2"),
        ("", "pattern-2"),
    ],
)
def test_the_pattern_is_auto_detected_from_the_classification_method(
    aws_credentials: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    classification_method: str,
    expected_pattern: str,
    ssl_only_hardening_calls: list,
):
    """Merging against the wrong pattern's defaults deploys a configuration the
    operator never wrote — a BDA config merged with pattern-2 defaults gains an
    OCR/Textract section that does not apply, and vice versa.

    The merge itself belongs to `idp_common`; what is under test here is which
    pattern this module asks for, and that the *merged* result is what gets
    uploaded.
    """
    import idp_common.config.merge_utils as merge_utils

    seen: dict[str, Any] = {}

    def fake_merge(user_config: dict, pattern: str, validate: bool = True) -> dict:
        seen["pattern"] = pattern
        seen["validate"] = validate
        return {**user_config, "merged": True}

    monkeypatch.setattr(merge_utils, "merge_config_with_defaults", fake_merge)

    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        yaml.dump({"classification": {"classificationMethod": classification_method}})
    )

    with mock_aws():
        uri = upload_local_config(str(cfg), "us-east-1")
        assert _read_uploaded(uri, "us-east-1")["merged"] is True

    assert seen["pattern"] == expected_pattern
    assert seen["validate"] is False


def test_an_explicit_pattern_overrides_auto_detection(
    aws_credentials: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ssl_only_hardening_calls: list,
):
    import idp_common.config.merge_utils as merge_utils

    seen: dict[str, Any] = {}

    def fake_merge(user_config: dict, pattern: str, validate: bool = True) -> dict:
        seen["pattern"] = pattern
        return user_config

    monkeypatch.setattr(merge_utils, "merge_config_with_defaults", fake_merge)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(yaml.dump({"classification": {"classificationMethod": "bda"}}))

    with mock_aws():
        upload_local_config(str(cfg), "us-east-1", pattern="pattern-2")
    assert seen["pattern"] == "pattern-2"


@pytest.mark.parametrize(
    "error",
    [
        ImportError("no idp_common"),
        FileNotFoundError("defaults missing"),
        RuntimeError("boom"),
    ],
)
def test_a_failed_merge_uploads_the_users_config_unmerged(
    aws_credentials: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    ssl_only_hardening_calls: list,
):
    """Each of the three handled failure classes must still produce a usable
    deploy. Uploading nothing, or half a merge, would leave the stack pointed at
    a CustomConfigPath that does not exist or is incomplete.
    """
    import idp_common.config.merge_utils as merge_utils

    def raiser(*_a: Any, **_k: Any) -> dict:
        raise error

    monkeypatch.setattr(merge_utils, "merge_config_with_defaults", raiser)
    config = {"extraction": {"model": "nova-lite"}}
    cfg = tmp_path / "c.yaml"
    cfg.write_text(yaml.dump(config))

    with mock_aws():
        uri = upload_local_config(str(cfg), "us-east-1")
        assert _read_uploaded(uri, "us-east-1") == config


def test_an_upload_failure_is_wrapped_with_context(
    aws_credentials: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ssl_only_hardening_calls: list,
):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("a: 1\n")
    real_client = boto3.client

    def fake_client(service: str, *args: Any, **kwargs: Any):
        client = real_client(service, *args, **kwargs)
        if service == "s3":

            def boom(*_a: Any, **_k: Any):
                raise RuntimeError("AccessDenied: PutObject")

            original = client.put_object
            assert original is not None
            client.put_object = boom  # type: ignore[method-assign]
        return client

    with mock_aws():
        bucket = get_or_create_config_bucket("us-east-1")
        assert bucket
        monkeypatch.setattr(boto3, "client", fake_client)
        with pytest.raises(Exception, match="Failed to upload config file"):
            upload_local_config(str(cfg), "us-east-1", merge_with_defaults=False)


def test_stack_name_is_accepted_but_does_not_change_the_destination(
    aws_credentials: str, tmp_path: Path, ssl_only_hardening_calls: list
):
    """`stack_name` is documented as kept for compatibility. Pinning that it is
    genuinely unused matters because the docstring above `build_parameters`
    claims an existing stack's own ConfigurationBucket is used — it is not, the
    staging bucket is always used, and a reader who trusts the docstring would
    look for the object in the wrong place.
    """
    cfg = tmp_path / "c.yaml"
    cfg.write_text("a: 1\n")
    with mock_aws():
        with_name = upload_local_config(
            str(cfg), "us-east-1", stack_name="SomeStack", merge_with_defaults=False
        )
        without = upload_local_config(str(cfg), "us-east-1", merge_with_defaults=False)
    assert with_name.split("/")[2] == without.split("/")[2]


def test_build_parameters_forwards_the_local_path_shape_to_the_uploader(
    aws_credentials: str, tmp_path: Path, ssl_only_hardening_calls: list
):
    """A local path that merely *looks* like a URI must still be uploaded, not
    validated as an S3 URI. `ANY` is unused here deliberately — the assertion is
    on the resulting object, not on the call."""
    assert ANY is not None
    cfg = tmp_path / "s3-like-name.yaml"
    cfg.write_text("a: 1\n")
    with mock_aws():
        out = build_parameters(
            custom_config=str(cfg), region="us-east-1", stack_name="S1"
        )
    assert out["CustomConfigPath"].startswith("s3://idp-cli-config-")
