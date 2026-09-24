# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`idp-feature-cli deploy`'s parameter set, and the two reporting commands
(`publish`, `publish-pack`) that print the URLs an operator acts on.

The deploy command's hardest job is deciding which CloudFormation parameters to
submit, and it has to get two opposite things right at once. `MainStackName` and
`FeatureBucket` are part of every feature template's contract and are sent
unconditionally — omitting either leaves the feature stack unable to resolve the
host's exports or its own artifacts. The three optional overrides
(`FeatureDisplayName`, `LogLevel`, `PermissionsBoundaryArn`) must be sent *only*
when the template declares them, because CloudFormation refuses a stack operation
outright on an unknown parameter name: a feature template that predates one of
those parameters would become undeployable through this CLI. And
`FeatureArtifactPrefix` / `FeatureVersion` must **never** be sent, because they
are baked into the template rather than parameterised — submitting them is the
same hard refusal.

The gate is also conditional in a second way: the template is only fetched and
validated when at least one override was passed, so a plain deploy costs no
`ValidateTemplate` call. That is worth pinning, since making it unconditional
would add a permission (`cloudformation:ValidateTemplate`) to the requirement list
for the common case.

`publish` and `publish-pack` are here for a narrower reason. Each ends by printing
a URL a human then pastes into the console — a Launch Stack URL and a Quick-Create
URL. Those are the whole output of the command, and a wrong one is followed. The
publishers themselves are substituted so these tests do not shell out to SAM;
what is asserted is that the URLs and keys the publisher returned are the ones
printed.
"""

from __future__ import annotations

from pathlib import Path

import boto3
import pytest
from click.testing import CliRunner
from moto import mock_aws

import idp_feature_sdk.cli as cli_mod
import idp_feature_sdk.pack as pack_mod
from idp_feature_sdk.cli import main

pytestmark = pytest.mark.unit

_HOST = "idp-main"
_FEATURE_ID = "demo-feature"
_STACK = f"{_HOST}-feature-{_FEATURE_ID}"
_BUCKET = "published-bkt"

_HOST_TEMPLATE = (
    '{"AWSTemplateFormatVersion":"2010-09-09",'
    '"Resources":{"T":{"Type":"AWS::SNS::Topic"}}}'
)

# A published feature template declaring only the two contract parameters plus
# LogLevel. FeatureDisplayName and PermissionsBoundaryArn are deliberately absent
# so the "declared" gate has something to exclude.
_FEATURE_TEMPLATE = (
    "AWSTemplateFormatVersion: '2010-09-09'\n"
    "Parameters:\n"
    "  MainStackName: {Type: String}\n"
    "  FeatureBucket: {Type: String}\n"
    "  LogLevel: {Type: String, Default: INFO}\n"
    "Resources:\n"
    "  Dummy:\n"
    "    Type: AWS::SNS::Topic\n"
)


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("COLUMNS", "400")


def _published(body: str = _FEATURE_TEMPLATE) -> str:
    """Put a feature template in S3 and create the host stack; return its URL."""
    s3 = boto3.client("s3", region_name="us-east-1")
    cfn = boto3.client("cloudformation", region_name="us-east-1")
    s3.create_bucket(Bucket=_BUCKET)
    key = f"extensions/{_FEATURE_ID}/template.yaml"
    s3.put_object(Bucket=_BUCKET, Key=key, Body=body)
    cfn.create_stack(StackName=_HOST, TemplateBody=_HOST_TEMPLATE)
    return f"https://{_BUCKET}.s3.us-east-1.amazonaws.com/{key}"


@pytest.fixture
def real_validate_template(monkeypatch):
    """Make ``ValidateTemplate`` report the parameters a published template really
    declares.

    moto answers ``ValidateTemplate(TemplateURL=...)`` with an empty ``Parameters``
    list — it does not parse the object it fetched. Left as-is, the override gate
    would see *nothing* declared, drop every optional override, and every
    assertion below would pass for the wrong reason (there would be no way to tell
    a working gate from one that drops everything). So the shim fetches the object
    from the mocked S3 and derives the declared names from its own
    ``Parameters:`` block: the answer comes from the published template, not from
    a list restated in the test.

    ``deploy_cmd`` does ``import boto3`` inside the function body, so the patch
    has to be on ``boto3.client`` itself rather than on a module attribute.
    """
    import re as _re

    import yaml as _yaml

    real_client = boto3.client
    calls: list[str] = []

    def _declared(url: str) -> list[str]:
        match = _re.match(r"https://([^.]+)\.s3\.[^/]+/(.+)$", url)
        assert match, f"test gave a URL this shim cannot resolve: {url}"
        bucket, key = match.group(1), match.group(2)
        body = (
            real_client("s3", region_name="us-east-1")
            .get_object(Bucket=bucket, Key=key)["Body"]
            .read()
        )
        parsed = _yaml.safe_load(body)
        return list((parsed or {}).get("Parameters") or {})

    def _client(service, **kwargs):
        client = real_client(service, **kwargs)
        if service != "cloudformation":
            return client

        def _validate(**kw):
            url = kw["TemplateURL"]
            calls.append(url)
            return {"Parameters": [{"ParameterKey": k} for k in _declared(url)]}

        client.validate_template = _validate  # type: ignore[method-assign]
        return client

    monkeypatch.setattr("boto3.client", _client)
    return calls


@pytest.fixture
def submitted(monkeypatch) -> list[dict[str, str]]:
    """Capture the parameter list the command hands to ``create_or_update_stack``.

    Reading the parameters back off the created stack is NOT good enough, and this
    fixture exists because of it: moto's ``CreateStack`` silently discards a
    parameter the template does not declare, and ``DescribeStacks`` then echoes
    only the declared ones. So an assertion of the form "the undeclared override
    is absent from the created stack" holds whether the gate dropped it or not —
    it cannot fail. (Measured: removing the `key in expected` condition from
    cli.py left the whole suite green.) The submitted list is the only place the
    gate's decision is observable.

    The real function is still called, so the stack is genuinely created and the
    rest of the command's behaviour is unchanged.
    """
    import idp_feature_sdk.pack as _pack

    captured: list[dict[str, str]] = []
    real = _pack.create_or_update_stack

    def _spy(**kwargs):
        captured.extend(kwargs["parameters"])
        return real(**kwargs)

    monkeypatch.setattr(_pack, "create_or_update_stack", _spy)
    return captured


def _as_map(parameters: list[dict[str, str]]) -> dict[str, str]:
    return {p["ParameterKey"]: p["ParameterValue"] for p in parameters}


def _params(stack_name: str = _STACK) -> dict[str, str]:
    cfn = boto3.client("cloudformation", region_name="us-east-1")
    desc = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    return {p["ParameterKey"]: p["ParameterValue"] for p in desc.get("Parameters", [])}


def _deploy(*extra: str):
    return CliRunner().invoke(
        main,
        [
            "deploy",
            "--host-stack-name",
            _HOST,
            "--region",
            "us-east-1",
            *extra,
        ],
    )


# ---------------------------------------------------------------------------
# The two unconditional parameters, and the two that are never sent
# ---------------------------------------------------------------------------


def test_the_contract_parameters_are_always_submitted(aws_env, submitted) -> None:
    """Both are part of every feature template's contract, and neither is
    conditional: without MainStackName the template's `Fn::ImportValue`s resolve
    nothing, and without FeatureBucket the stack cannot find its own artifacts."""
    with mock_aws():
        url = _published()
        result = _deploy("--template-url", url, "--wait")
        assert result.exit_code == 0, result.output
        # Present in the stack as well as in the submission, so the two agree.
        assert _params()["MainStackName"] == _HOST
    assert _as_map(submitted) == {"MainStackName": _HOST, "FeatureBucket": _BUCKET}


def test_the_baked_values_are_never_submitted_as_parameters(aws_env, submitted) -> None:
    """`FeatureArtifactPrefix` and `FeatureVersion` are baked into the template at
    publish time precisely because the console's Update-stack wizard drops
    parameters. Submitting them here would be refused outright by CloudFormation
    for a template that no longer declares them."""
    with mock_aws():
        url = _published()
        assert _deploy("--template-url", url, "--wait").exit_code == 0
    keys = set(_as_map(submitted))
    assert "FeatureArtifactPrefix" not in keys
    assert "FeatureVersion" not in keys


# ---------------------------------------------------------------------------
# The three optional overrides
# ---------------------------------------------------------------------------


def test_an_override_the_template_declares_is_submitted(
    aws_env, submitted, real_validate_template
) -> None:
    with mock_aws():
        url = _published()
        result = _deploy("--template-url", url, "--log-level", "DEBUG", "--wait")
        assert result.exit_code == 0, result.output
        assert _params()["LogLevel"] == "DEBUG"
    assert _as_map(submitted)["LogLevel"] == "DEBUG"
    assert real_validate_template == [url], "the gate must consult the template"


def test_an_override_the_template_does_not_declare_is_dropped(
    aws_env, submitted, real_validate_template
) -> None:
    """This template declares no `PermissionsBoundaryArn` and no
    `FeatureDisplayName`. Submitting either would make CloudFormation refuse the
    whole stack operation with "Parameters do not exist in the template" — so an
    older feature template would become undeployable the moment an operator used a
    newer flag."""
    with mock_aws():
        url = _published()
        result = _deploy(
            "--template-url",
            url,
            "--permissions-boundary-arn",
            "arn:aws:iam::123456789012:policy/Boundary",
            "--feature-display-name",
            "Renamed",
            "--wait",
        )
        assert result.exit_code == 0, result.output
    keys = set(_as_map(submitted))
    assert "PermissionsBoundaryArn" not in keys
    assert "FeatureDisplayName" not in keys
    # The gate dropped only the undeclared overrides, not everything.
    assert keys == {"MainStackName", "FeatureBucket"}


def test_declared_and_undeclared_overrides_passed_together_are_split(
    aws_env, submitted, real_validate_template
) -> None:
    """The gate is per-name, not all-or-nothing. One unrecognised flag must not
    discard a recognised one alongside it, and a recognised one must not smuggle an
    unrecognised one through."""
    with mock_aws():
        url = _published()
        result = _deploy(
            "--template-url",
            url,
            "--log-level",
            "WARN",
            "--feature-display-name",
            "Renamed",
            "--wait",
        )
        assert result.exit_code == 0, result.output
    assert _as_map(submitted) == {
        "MainStackName": _HOST,
        "FeatureBucket": _BUCKET,
        "LogLevel": "WARN",
    }


def test_no_overrides_means_the_template_is_never_fetched(
    aws_env, real_validate_template
) -> None:
    """The `ValidateTemplate` call is conditional on an override being passed.
    Making it unconditional would add a permission requirement — and a network
    round trip — to every plain deploy, including the `--from-code` path that has
    just uploaded the template itself."""
    with mock_aws():
        url = _published()
        assert _deploy("--template-url", url, "--wait").exit_code == 0
        assert real_validate_template == []

        assert (
            _deploy("--template-url", url, "--log-level", "DEBUG", "--wait").exit_code
            == 0
        )
    assert real_validate_template == [url]


def test_an_unvalidatable_template_stops_the_deploy(aws_env) -> None:
    """If the override gate cannot read the template it must not guess. Submitting
    the overrides blind risks the hard refusal; dropping them silently would
    report a deploy that ignored what was asked for."""
    with mock_aws():
        boto3.client("cloudformation", region_name="us-east-1").create_stack(
            StackName=_HOST, TemplateBody=_HOST_TEMPLATE
        )
        result = _deploy(
            "--template-url",
            "https://gone.s3.us-east-1.amazonaws.com/extensions/x/template.yaml",
            "--bucket-basename",
            "gone",
            "--log-level",
            "DEBUG",
        )
    assert result.exit_code == 1
    assert "validate feature template" in result.output


# ---------------------------------------------------------------------------
# Error paths around the deploy
# ---------------------------------------------------------------------------


def test_no_region_anywhere_is_refused_with_the_variables_to_set(
    monkeypatch, tmp_path: Path
) -> None:
    """Falling back to a hardcoded region would deploy a feature into a region the
    host stack is not in, where the `Fn::ImportValue` of its exports resolves to
    nothing."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    # `deploy_cmd` imports boto3 inside the function, so the patch goes on the
    # library rather than on a module attribute that does not exist.
    monkeypatch.setattr(
        "boto3.session.Session", lambda: type("S", (), {"region_name": None})()
    )
    result = CliRunner().invoke(
        main,
        [
            "deploy",
            "--template-url",
            "https://b.s3.us-east-1.amazonaws.com/extensions/x/template.yaml",
            "--host-stack-name",
            _HOST,
        ],
    )
    assert result.exit_code == 1
    assert "AWS_REGION" in result.output


def test_a_describe_error_other_than_not_found_is_reported_not_swallowed(
    aws_env, monkeypatch
) -> None:
    """A throttle or an AccessDenied on the host lookup must not read as "host
    stack does not exist" — that sends the operator checking a stack name that is
    correct."""

    def _boom(_cfn, _name):
        raise RuntimeError("Throttling: Rate exceeded")

    monkeypatch.setattr(pack_mod, "_describe_one", _boom)
    with mock_aws():
        result = _deploy(
            "--template-url",
            "https://b.s3.us-east-1.amazonaws.com/extensions/x/template.yaml",
        )
    assert result.exit_code == 1
    assert "Could not describe host stack" in result.output
    assert "Throttling" in result.output


def test_an_unparseable_feature_id_demands_an_explicit_stack_name(aws_env) -> None:
    """Without a feature id the default stack name would be
    `<host>-feature-None`, which is a *new* stack — so a redeploy would create a
    second copy of the feature rather than upgrading the first."""
    with mock_aws():
        boto3.client("cloudformation", region_name="us-east-1").create_stack(
            StackName=_HOST, TemplateBody=_HOST_TEMPLATE
        )
        result = _deploy(
            "--template-url",
            "https://b.s3.us-east-1.amazonaws.com/some/other/template.yaml",
        )
    assert result.exit_code == 1
    assert "--stack-name" in result.output
    assert "extensions/<id>" in result.output


def test_an_explicit_stack_name_makes_an_unparseable_url_deployable(
    aws_env,
) -> None:
    """The escape hatch the refusal names must actually work: with `--stack-name`
    given, the feature id is no longer needed."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        cfn = boto3.client("cloudformation", region_name="us-east-1")
        s3.create_bucket(Bucket=_BUCKET)
        s3.put_object(Bucket=_BUCKET, Key="odd/layout.yaml", Body=_FEATURE_TEMPLATE)
        cfn.create_stack(StackName=_HOST, TemplateBody=_HOST_TEMPLATE)
        result = _deploy(
            "--template-url",
            f"https://{_BUCKET}.s3.us-east-1.amazonaws.com/odd/layout.yaml",
            "--stack-name",
            "my-feature-stack",
            "--wait",
        )
        assert result.exit_code == 0, result.output
        assert _params("my-feature-stack")["MainStackName"] == _HOST
    # With no parsed feature id the summary says where the name came from.
    assert "(from --stack-name)" in result.output


def test_a_stack_operation_failure_exits_nonzero(aws_env, monkeypatch) -> None:
    def _boom(**_kw):
        raise RuntimeError("Stack is in ROLLBACK_COMPLETE; delete it first")

    monkeypatch.setattr(pack_mod, "create_or_update_stack", _boom)
    with mock_aws():
        url = _published()
        result = _deploy("--template-url", url)
    assert result.exit_code == 1
    assert "ROLLBACK_COMPLETE" in result.output


def test_a_publish_failure_in_the_from_code_path_stops_before_deploying(
    aws_env, demo_feature_project: Path, monkeypatch
) -> None:
    def _boom(self, **_kw):
        raise RuntimeError("sam build failed (exit 1)")

    monkeypatch.setattr(cli_mod.FeaturePublisher, "publish", _boom)
    with mock_aws():
        boto3.client("cloudformation", region_name="us-east-1").create_stack(
            StackName=_HOST, TemplateBody=_HOST_TEMPLATE
        )
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="fb-us-east-1")
        result = _deploy(
            "--from-code", str(demo_feature_project), "--bucket-basename", "fb"
        )
        assert result.exit_code == 1
        assert "Publish failed" in result.output
        with pytest.raises(Exception, match="does not exist"):
            boto3.client("cloudformation", region_name="us-east-1").describe_stacks(
                StackName=_STACK
            )


# ---------------------------------------------------------------------------
# publish — the Launch Stack URL it prints
# ---------------------------------------------------------------------------


class _FakePublishResult:
    feature_id = _FEATURE_ID
    version = "1.2.3"
    template_url = f"https://{_BUCKET}.s3.us-east-1.amazonaws.com/extensions/demo-feature/template.yaml"
    bundle_url = "https://b/extensions/demo-feature/1.2.3/ui-bundle.js"
    manifest_url = "https://b/extensions/demo-feature/1.2.3/manifest.json"
    latest_json_url = "https://b/extensions/demo-feature/latest.json"
    launch_url = (
        "https://console.aws.amazon.com/cloudformation/home?region=us-east-1"
        "#/stacks/quickcreate?templateURL=https%3A%2F%2Fb%2Ft.yaml"
        "&stackName=idp-feature-demo-feature&param_MainStackName=MAINSTACKNAME"
    )
    artifacts: list = []


def test_publish_prints_every_url_the_operator_needs(
    aws_env, demo_feature_project: Path, monkeypatch
) -> None:
    """These lines are the command's whole output. The Launch Stack URL in
    particular is pasted into the console, and one built against the wrong bucket
    creates a stack that fetches nothing."""
    captured: list = []

    def _fake_publish(self, **kw):
        captured.append(kw)
        return _FakePublishResult()

    monkeypatch.setattr(cli_mod.FeaturePublisher, "publish", _fake_publish)
    result = CliRunner().invoke(
        main,
        [
            "publish",
            str(demo_feature_project),
            "--bucket-basename",
            "artifacts",
            "--region",
            "us-east-1",
            "--prefix",
            "mkt",
            "--public",
        ],
    )
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert _FakePublishResult.template_url in flat
    assert _FakePublishResult.latest_json_url in flat
    assert "stacks/quickcreate" in flat
    assert "MAINSTACKNAME" in flat
    # And the flags reached the publisher rather than only the banner.
    assert captured == [
        {
            "feature_bucket": "artifacts-us-east-1",
            "region": "us-east-1",
            "s3_prefix": "mkt",
            "make_public": True,
            "register_with_simulator": None,
            "simulator_product_code": None,
        }
    ]


def test_publish_forwards_the_simulator_options(
    aws_env, demo_feature_project: Path, monkeypatch
) -> None:
    captured: list = []
    monkeypatch.setattr(
        cli_mod.FeaturePublisher,
        "publish",
        lambda self, **kw: captured.append(kw) or _FakePublishResult(),
    )
    result = CliRunner().invoke(
        main,
        [
            "publish",
            str(demo_feature_project),
            "--bucket-basename",
            "artifacts",
            "--register-with-simulator",
            "http://127.0.0.1:8080",
            "--simulator-product-code",
            "prod-custom",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured[0]["register_with_simulator"] == "http://127.0.0.1:8080"
    assert captured[0]["simulator_product_code"] == "prod-custom"


def test_publish_reports_a_failure_without_printing_a_launch_url(
    aws_env, demo_feature_project: Path, monkeypatch
) -> None:
    """A failed publish must not print a Launch Stack URL — it would point at a
    template that was never uploaded, and the console's error arrives much later."""

    def _boom(self, **_kw):
        raise RuntimeError("UI bundle does not contain the version literal")

    monkeypatch.setattr(cli_mod.FeaturePublisher, "publish", _boom)
    result = CliRunner().invoke(
        main,
        ["publish", str(demo_feature_project), "--bucket-basename", "artifacts"],
    )
    assert result.exit_code == 1
    assert "version literal" in result.output
    assert "quickcreate" not in result.output


# ---------------------------------------------------------------------------
# publish-pack — the Quick-Create URL and the deploy command it prints
# ---------------------------------------------------------------------------


def test_publish_pack_prints_the_quick_create_url_and_the_cli_equivalent(
    aws_env, demo_feature_project: Path, monkeypatch
) -> None:
    """Two ways to deploy the same wrapper, and both are copied verbatim by the
    reader. They must name the same wrapper URL the publish returned."""
    wrapper = "https://artifacts-us-east-1.s3.us-east-1.amazonaws.com/extensions/demo-feature/deploy.yaml"
    captured: list = []

    class _FakePackPublisher:
        def __init__(self, project_dir, console=None):
            pass

        def publish(self, **kw):
            captured.append(kw)
            return pack_mod.PackPublishResult(
                feature_id=_FEATURE_ID,
                version="1.2.3",
                artifact_bucket="artifacts-us-east-1",
                artifact_prefix="extensions/demo-feature",
                feature_template_url="https://x/template.yaml",
                host_template_url=kw["host_template_url"],
                wrapper_template_url=wrapper,
                quick_create_url=f"https://console/quickcreate?templateURL={wrapper}",
                deploy_command=f"idp-feature-cli deploy-pack --wrapper-url {wrapper}",
            )

    monkeypatch.setattr(pack_mod, "PackPublisher", _FakePackPublisher)
    result = CliRunner().invoke(
        main,
        [
            "publish-pack",
            str(demo_feature_project),
            "--bucket-basename",
            "artifacts",
            "--host-template-url",
            "https://h/idp-main.yaml",
        ],
    )
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert wrapper in flat
    assert "Quick-Create URL" in flat
    assert "deploy-pack --wrapper-url" in flat
    assert "https://h/idp-main.yaml" in flat
    assert captured[0]["artifacts_bucket"] == "artifacts-us-east-1"
    assert captured[0]["make_public"] is False


def test_publish_pack_reports_a_missing_wrapper_template(
    aws_env, demo_feature_project: Path, monkeypatch
) -> None:
    """A pack with no `pack.wrapperTemplatePath` on disk has nothing to publish.
    The message names the manifest field rather than the exception type."""

    class _Failing:
        def __init__(self, *_a, **_kw):
            pass

        def publish(self, **_kw):
            raise FileNotFoundError(
                "Wrapper template /x/deploy.yaml not found "
                "(feature.yaml -> pack.wrapperTemplatePath)."
            )

    monkeypatch.setattr(pack_mod, "PackPublisher", _Failing)
    result = CliRunner().invoke(
        main,
        [
            "publish-pack",
            str(demo_feature_project),
            "--bucket-basename",
            "artifacts",
            "--host-template-url",
            "https://h/idp-main.yaml",
        ],
    )
    assert result.exit_code == 1
    assert "wrapperTemplatePath" in result.output
    assert "Quick-Create" not in result.output


def test_publish_pack_requires_a_host_template_url(
    aws_env, demo_feature_project: Path
) -> None:
    """Unlike `deploy-pack`, there is no `--build accelerator` here: a pack wrapper
    always has to be told which host template to create."""
    result = CliRunner().invoke(
        main,
        ["publish-pack", str(demo_feature_project), "--bucket-basename", "artifacts"],
    )
    assert result.exit_code == 2
    assert "--host-template-url" in result.output
