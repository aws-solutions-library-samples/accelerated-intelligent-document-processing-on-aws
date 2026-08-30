"""Unit tests for the --headless / --govcloud transform DEPLOY test runner.

`transform_deploy_test.py` is the only tier that deploys a *transformed*
template. Everything below runs with no AWS and no subprocess: `run_command`,
the IAM helpers, `cleanup_stack` and `run_inference_test` are all monkeypatched.

The properties pinned here are the ones that decide whether a green run means
anything:

  * each variant's structural assertions actually FAIL when the thing the
    transform is supposed to remove is still present (issues #676/#677 shapes),
  * the sample-document test is not skipped silently,
  * teardown ALWAYS runs — including when validation fails or the deploy raises,
  * --headless is deployed WITHOUT --admin-email (the headless template has no
    AdminEmail parameter; passing one is a CloudFormation ValidationError),
  * a commercial --govcloud run overrides the GovCloud config preset, whose
    model IDs would otherwise fail the document test for the wrong reason.
"""

import json

import pytest

import codebuild_deployment as cbd
import transform_deploy_test as tdt

pytestmark = pytest.mark.unit


class _Out:
    def __init__(self, stdout=""):
        self.stdout = stdout


@pytest.fixture
def wired(monkeypatch):
    """Patch every AWS/subprocess touchpoint; record what the runner did."""
    calls = {
        "commands": [],
        "cleanups": [],
        "inference": [],
        "resources": {},
        "outputs": {},
        "parameters": {},
        "status": "CREATE_COMPLETE",
        "inference_ok": True,
        "deploy_raises": None,
    }

    def fake_run_command(cmd, **kwargs):
        calls["commands"].append(cmd)
        if "idp-cli deploy" in cmd:
            if calls["deploy_raises"]:
                raise RuntimeError(calls["deploy_raises"])
            return _Out("deployed")
        if "describe-stacks" in cmd and "StackStatus" in cmd:
            return _Out(calls["status"])
        if "list-stack-resources" in cmd:
            return _Out(json.dumps([[k, v] for k, v in calls["resources"].items()]))
        if "Outputs" in cmd:
            return _Out(json.dumps([[k, v] for k, v in calls["outputs"].items()]))
        if "Parameters" in cmd:
            return _Out(json.dumps([[k, v] for k, v in calls["parameters"].items()]))
        return _Out("")

    monkeypatch.setattr(tdt.cbd, "run_command", fake_run_command)
    monkeypatch.setattr(
        tdt.cbd, "create_iam_resources", lambda name, create_boundary=True: ("role-arn", "")
    )
    monkeypatch.setattr(
        tdt.cbd, "cleanup_stack", lambda r: calls["cleanups"].append(r["stack_name"])
    )
    monkeypatch.setattr(tdt.cbd, "_capture_cf_events", lambda result, *names: None)
    monkeypatch.setattr(tdt.cbd, "generate_stack_name", lambda: "idp-0101-000000")

    def fake_inference(stack_name, sample, batch_id, *a, **k):
        calls["inference"].append((stack_name, sample, batch_id))
        return calls["inference_ok"]

    monkeypatch.setattr(tdt.cbd, "run_inference_test", fake_inference)
    return calls


CORE = {
    "InputBucket": "AWS::S3::Bucket",
    "OutputBucket": "AWS::S3::Bucket",
    "TrackingTable": "AWS::DynamoDB::Table",
    "PATTERNSTACK": "AWS::CloudFormation::Stack",
}


def _run(variant_key, wired, **kwargs):
    variant = tdt._variants_by_key()[variant_key]
    defaults = dict(admin_email="me@example.com", region="us-east-1")
    defaults.update(kwargs)
    return tdt.run_variant(variant, **defaults)


# --- headless ---------------------------------------------------------------


def test_headless_passes_on_a_correctly_transformed_stack(wired):
    wired["resources"] = dict(CORE)
    result = _run("headless", wired)
    assert result["success"], result.get("error")
    assert wired["inference"], "the sample document test did not run"


def test_headless_fails_if_ui_survived(wired):
    """--headless means the UI is gone; a surviving UserPool is a real failure."""
    wired["resources"] = {**CORE, "UserPool": "AWS::Cognito::UserPool"}
    result = _run("headless", wired)
    assert not result["success"]
    assert "UserPool" in result["error"]


def test_headless_fails_if_processing_core_was_stripped(wired):
    """A deploy that succeeds but cannot process documents is still broken."""
    wired["resources"] = {k: v for k, v in CORE.items() if k != "PATTERNSTACK"}
    result = _run("headless", wired)
    assert not result["success"]
    assert "PATTERNSTACK" in result["error"]


def test_headless_fails_if_application_web_url_still_exported(wired):
    wired["resources"] = dict(CORE)
    wired["outputs"] = {"ApplicationWebURL": "https://example.com"}
    result = _run("headless", wired)
    assert not result["success"]
    assert "ApplicationWebURL" in result["error"]


def test_headless_deploys_without_admin_email(wired):
    """The headless template has no AdminEmail parameter — passing it is fatal."""
    wired["resources"] = dict(CORE)
    _run("headless", wired)
    deploy = next(c for c in wired["commands"] if "idp-cli deploy" in c)
    assert "--headless" in deploy
    assert "--admin-email" not in deploy


# --- govcloud ---------------------------------------------------------------


def test_govcloud_passes_on_a_correctly_transformed_stack(wired):
    wired["resources"] = {**CORE, "UserPool": "AWS::Cognito::UserPool"}
    wired["parameters"] = {"WebUIHosting": "APIGateway"}
    result = _run("govcloud", wired)
    assert result["success"], result.get("error")


def test_govcloud_fails_if_cloudfront_survived(wired):
    wired["resources"] = {
        **CORE,
        "UserPool": "AWS::Cognito::UserPool",
        "CloudFrontDistribution": "AWS::CloudFront::Distribution",
    }
    wired["parameters"] = {"WebUIHosting": "APIGateway"}
    result = _run("govcloud", wired)
    assert not result["success"]
    assert "CloudFront" in result["error"]


def test_govcloud_fails_if_lwa_chat_stream_handler_survived(wired):
    """Issue #677 exactly: the Function URL went but its LWA handler did not."""
    wired["resources"] = {
        **CORE,
        "UserPool": "AWS::Cognito::UserPool",
        "ChatStreamProcessorFunction": "AWS::Lambda::Function",
    }
    wired["parameters"] = {"WebUIHosting": "APIGateway"}
    result = _run("govcloud", wired)
    assert not result["success"]
    assert "ChatStreamProcessorFunction" in result["error"]


def test_govcloud_fails_if_lambda_function_url_survived(wired):
    wired["resources"] = {
        **CORE,
        "UserPool": "AWS::Cognito::UserPool",
        "SomeUrl": "AWS::Lambda::Url",
    }
    wired["parameters"] = {"WebUIHosting": "APIGateway"}
    result = _run("govcloud", wired)
    assert not result["success"]
    assert "Function URL" in result["error"]


def test_govcloud_keeps_the_ui(wired):
    """Unlike --headless, --govcloud RETAINS Cognito/the UI."""
    wired["resources"] = dict(CORE)  # no UserPool
    wired["parameters"] = {"WebUIHosting": "APIGateway"}
    result = _run("govcloud", wired)
    assert not result["success"]
    assert "UserPool" in result["error"]


def test_commercial_govcloud_run_overrides_the_govcloud_preset(wired):
    """Otherwise the doc test fails on GovCloud model IDs, not on the transform."""
    wired["resources"] = {**CORE, "UserPool": "AWS::Cognito::UserPool"}
    wired["parameters"] = {"WebUIHosting": "APIGateway"}
    _run("govcloud", wired, region="us-east-1")
    deploy = next(c for c in wired["commands"] if "idp-cli deploy" in c)
    assert "ConfigurationPreset=lending-package-sample" in deploy


def test_real_govcloud_run_keeps_the_govcloud_preset(wired):
    wired["resources"] = {**CORE, "UserPool": "AWS::Cognito::UserPool"}
    wired["parameters"] = {"WebUIHosting": "APIGateway"}
    _run("govcloud", wired, region="us-gov-west-1")
    deploy = next(c for c in wired["commands"] if "idp-cli deploy" in c)
    assert "ConfigurationPreset" not in deploy


# --- lifecycle --------------------------------------------------------------


def test_stack_is_torn_down_on_success(wired):
    wired["resources"] = dict(CORE)
    result = _run("headless", wired)
    assert wired["cleanups"] == [result["stack_name"]]


def test_stack_is_torn_down_when_validation_fails(wired):
    wired["resources"] = {**CORE, "UserPool": "AWS::Cognito::UserPool"}
    result = _run("headless", wired)
    assert not result["success"]
    assert wired["cleanups"] == [result["stack_name"]]


def test_stack_is_torn_down_when_the_deploy_raises(wired):
    wired["deploy_raises"] = "boom"
    result = _run("headless", wired)
    assert not result["success"]
    assert result["failure_type"] == "deploy"
    assert wired["cleanups"] == [result["stack_name"]]


def test_keep_leaves_the_stack_up(wired):
    wired["resources"] = dict(CORE)
    _run("headless", wired, keep=True)
    assert wired["cleanups"] == []


def test_failed_deploy_status_is_reported_and_not_validated(wired):
    wired["status"] = "ROLLBACK_COMPLETE"
    result = _run("headless", wired)
    assert not result["success"]
    assert result["failure_type"] == "deploy"
    assert "ROLLBACK_COMPLETE" in result["error"]
    assert wired["inference"] == [], "must not run the doc test on a failed deploy"


def test_skip_doc_test_does_not_run_inference(wired):
    wired["resources"] = dict(CORE)
    result = _run("headless", wired, skip_doc_test=True)
    assert result["success"]
    assert wired["inference"] == []
    assert not any("sample document processed" in c for c in result["checks"])


def test_doc_test_failure_fails_the_variant(wired):
    wired["resources"] = dict(CORE)
    wired["inference_ok"] = False
    result = _run("headless", wired)
    assert not result["success"]
    assert "did not process" in result["error"]


def test_existing_stack_mode_neither_deploys_nor_tears_down(wired):
    wired["resources"] = dict(CORE)
    result = _run("headless", wired, existing_stack="idp-someone-elses")
    assert result["stack_name"] == "idp-someone-elses"
    assert not any("idp-cli deploy" in c for c in wired["commands"])
    assert wired["cleanups"] == []


def test_report_returns_false_when_any_variant_failed():
    assert tdt._print_report([{"name": "a", "success": True}]) is True
    assert (
        tdt._print_report([{"name": "a", "success": True}, {"name": "b", "success": False}])
        is False
    )


def test_variants_reuse_the_shared_ci_machinery():
    """Guard the no-drift property: this runner must not fork the deploy logic."""
    for attr in (
        "generate_stack_name",
        "create_iam_resources",
        "cleanup_stack",
        "run_command",
        "run_inference_test",
    ):
        assert hasattr(cbd, attr), f"codebuild_deployment.{attr} went away"
