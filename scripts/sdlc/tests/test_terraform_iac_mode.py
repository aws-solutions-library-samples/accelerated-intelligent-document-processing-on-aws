# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The integration driver's Terraform mode (IDP_IAC=terraform).

A nightly schedule carrying IDP_IAC=terraform, beside the CloudFormation nightly,
deploys the accelerator with `idp-cli deploy --terraform` instead of CloudFormation
and runs the primary suite against it unchanged. What is pinned here, offline:

* the switch on both sides: the GitLab job honours it on a schedule only, the build
  reads it from its environment or ci_run_options.json, and an unknown value or a
  combination with the probe or region runs is refused rather than half-honoured;
* the command lines each mode builds, handed to `idp-cli`'s own Click commands
  rather than matched as strings;
* the health check, the teardown dispatch and the sweep of what the Terraform
  delete retains;
* the pinned Terraform install, which refuses an archive whose checksum differs;
* the labels, the UX review stack staying put, and the GitLab rules.
"""

from __future__ import annotations

import fnmatch
import io
import json
import os
import re
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from test_malware_gate_step import (
    _cli_authorities,
    assert_deploy_command_is_well_formed,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_CONFIG = REPO_ROOT / ".gitlab-ci.yml"
BOUNDARY_ARN = "arn:aws:iam::1:policy/stk-PermissionsBoundary"
ROLE_ARN = "arn:aws:iam::1:role/idp-sdlc-service-role"
TF_ROLE_ARN = "arn:aws:iam::123456789012:role/stk-iam-TFDeployRole"
DEPLOYER_ARN = "arn:aws:iam::1:role/codebuild-role"
MODULE_URL = (
    "https://s3.us-east-1.amazonaws.com/genaiic-sdlc-sourcecode-1-us-east-1/"
    "codebuild-20261106-010203/0.6.14/terraform/idp-terraform-0.6.14.zip"
)
TEMPLATE_URL = (
    "https://s3.us-east-1.amazonaws.com/genaiic-sdlc-sourcecode-1-us-east-1/"
    "codebuild-20261106-010203/idp-main.yaml"
)


@pytest.fixture
def itd():
    import integration_test_deployment as module

    return module


@pytest.fixture
def run(cbd, tmp_path):
    return cbd.TerraformRun(
        module_url=MODULE_URL,
        directory=str(tmp_path / "tf" / "idp-1106-010203"),
        binary="/opt/terraform/terraform",
    )


class _Result:
    def __init__(self, cmd="", returncode=0, stdout="", stderr=""):
        self.args = cmd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# The switch, on the build side
# ---------------------------------------------------------------------------


def test_cloudformation_is_the_default(cbd):
    assert cbd.iac_mode({}, {}) == "cloudformation"
    assert cbd.iac_mode({}, None) == "cloudformation"


@pytest.mark.parametrize("value", ["terraform", " Terraform ", "TERRAFORM"])
def test_the_environment_selects_terraform(cbd, value):
    assert cbd.iac_mode({"IDP_IAC": value}, {}) == "terraform"


def test_the_run_options_select_it_when_the_environment_does_not(cbd):
    assert cbd.iac_mode({}, {"iac": "terraform"}) == "terraform"


def test_the_environment_beats_the_run_options(cbd):
    assert cbd.iac_mode({"IDP_IAC": "cloudformation"}, {"iac": "terraform"}) == (
        "cloudformation"
    )


@pytest.mark.parametrize(
    ("environ", "options"),
    [({"IDP_IAC": "terrafrom"}, {}), ({}, {"iac": "cdk"}), ({"IDP_IAC": "tofu"}, {})],
)
def test_an_unknown_mode_is_refused_not_defaulted(cbd, environ, options):
    with pytest.raises(ValueError, match="IDP_IAC must be one of"):
        cbd.iac_mode(environ, options)


@pytest.mark.parametrize(
    "environ",
    [
        {"IDP_RUN_PROBES": "true"},
        {"IDP_TEST_APIGW_HOSTING": "true"},
        {"AWS_REGION": "eu-central-1"},
        {"AWS_DEFAULT_REGION": "ca-central-1"},
    ],
    ids=["probes", "legacy-probe-alias", "region-run", "region-run-default-var"],
)
def test_terraform_refuses_the_probe_and_region_runs(cbd, environ):
    reason = cbd.terraform_unsupported_reason(environ)
    assert reason and "IDP_IAC=terraform" in reason


def test_terraform_runs_the_primary_suite_in_the_primary_region(cbd):
    assert cbd.terraform_unsupported_reason({"AWS_REGION": "us-east-1"}) is None
    assert cbd.terraform_unsupported_reason({}) is None


def test_the_work_directory_is_absolute_and_outside_the_checkout(cbd):
    path = Path(cbd.terraform_work_directory("idp-1106-010203", {}))
    assert path.is_absolute() and path.name == "idp-1106-010203"
    assert not path.is_relative_to(REPO_ROOT)
    assert cbd.terraform_work_directory("s", {"IDP_TERRAFORM_WORK_ROOT": "/w"}) == (
        "/w/s"
    )


class _RanPastTheRefusal(BaseException):
    """BaseException, so no `except Exception` in main() can swallow it: a stub
    raising this fails the test wherever the refusal has moved to."""


def test_main_refuses_before_any_sweep_publish_or_deploy(cbd, monkeypatch):
    """A refused combination must not cost a sweep, a publish or a deploy."""
    monkeypatch.setenv("IDP_IAC", "terraform")
    monkeypatch.setenv("IDP_RUN_PROBES", "true")
    monkeypatch.setattr(cbd, "install_suite_transcript", lambda: None)
    monkeypatch.setattr(cbd, "read_ci_run_options", lambda *a: {})
    published, notified = [], []
    monkeypatch.setattr(cbd, "publish_summary_to_s3", published.append)
    monkeypatch.setattr(cbd, "send_failure_notification", lambda *a: notified.append(a))

    def _must_not_run(*_a, **_k):
        raise _RanPastTheRefusal()

    stubbed = [sweep.func for sweep in cbd.STARTUP_SWEEPS] + [
        "publish_artifacts",
        "publish_templates",
        "deploy_and_test_stack",
        "create_iam_resources",
        "ensure_terraform_binary",
    ]
    for name in stubbed:
        monkeypatch.setattr(cbd, name, _must_not_run)

    class _Exit(Exception):
        pass

    def _exit(code):
        raise _Exit(code)

    monkeypatch.setattr(cbd.os, "_exit", _exit)
    with pytest.raises(_Exit) as excinfo:
        cbd.main()
    assert excinfo.value.args == (1,)
    assert published and "deployment-variant probes" in published[0]
    assert notified and "terraform" in notified[0][0].lower()


# ---------------------------------------------------------------------------
# The switch, on the GitLab side
# ---------------------------------------------------------------------------


def test_the_job_honours_terraform_on_a_schedule(itd):
    assert itd.iac_requested(
        {"CI_PIPELINE_SOURCE": "schedule", "IDP_IAC": " Terraform "}
    ) == ("terraform")


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"CI_PIPELINE_SOURCE": "schedule"},
        {"CI_PIPELINE_SOURCE": "web", "IDP_IAC": "terraform"},
        {"CI_PIPELINE_SOURCE": "merge_request_event", "IDP_IAC": "terraform"},
        {"CI_PIPELINE_SOURCE": "web", "IDP_IAC": "nonsense"},
    ],
)
def test_nothing_else_is_a_terraform_run(itd, environ):
    assert itd.iac_requested(environ) == "cloudformation"


@pytest.mark.parametrize(
    ("extra", "match"),
    [
        ({"IDP_IAC": "terrafrom"}, "is not a deployment mode"),
        ({"IDP_IAC": "terraform", "IDP_RUN_PROBES": "true"}, "IDP_RUN_PROBES"),
        (
            {"IDP_IAC": "terraform", "IDP_TEST_REGION": "eu-central-1"},
            "IDP_TEST_REGION",
        ),
    ],
)
def test_the_job_refuses_what_the_build_cannot_run(itd, extra, match):
    with pytest.raises(ValueError, match=match):
        itd.iac_requested({"CI_PIPELINE_SOURCE": "schedule", **extra})


def test_the_two_mode_lists_agree(itd, cbd):
    assert itd.IAC_MODES == cbd.IAC_MODES
    assert itd.IAC_VARIABLE == cbd.IAC_VARIABLE


def test_the_run_options_carry_the_mode_to_the_build(itd, cbd, tmp_path):
    path = tmp_path / "ci_run_options.json"
    options = itd.write_ci_run_options(
        str(path), environ={"CI_PIPELINE_SOURCE": "schedule", "IDP_IAC": "terraform"}
    )
    assert options["iac"] == "terraform"
    assert cbd.iac_mode({}, cbd.read_ci_run_options(str(path))) == "terraform"


def test_the_job_fails_on_a_refused_combination(itd, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CI_PIPELINE_SOURCE", "schedule")
    monkeypatch.setenv("IDP_IAC", "terraform")
    monkeypatch.setenv("IDP_RUN_PROBES", "true")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

    def _must_not_run(*_a, **_k):
        raise AssertionError("packaged or uploaded a refused run")

    monkeypatch.setattr(itd, "create_deployment_package", _must_not_run)
    monkeypatch.setattr(itd, "upload_to_s3", _must_not_run)
    with pytest.raises(SystemExit) as excinfo:
        itd.main()
    assert excinfo.value.code == 1


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------


def test_publish_adds_terraform_only_in_terraform_mode(cbd):
    cfn = cbd.build_publish_command("b", "p", "us-east-1", "cloudformation")
    tf = cbd.build_publish_command("b", "p", "us-east-1", "terraform")
    assert "--terraform" not in shlex.split(cfn)
    assert shlex.split(tf) == shlex.split(cfn) + ["--terraform"]


def _real_publish_output(capsys, *, terraform_module_url=None, variants=None):
    """What `idp-cli publish` prints, produced by the SDK's own printer."""
    from idp_sdk import IDPClient

    IDPClient(region="us-east-1").publish.print_deployment_urls(
        template_url=TEMPLATE_URL,
        region="us-east-1",
        terraform_module_url=terraform_module_url,
        terraform_variant_modules=variants,
        terraform_pointer_url=(
            TEMPLATE_URL.rsplit("/", 1)[0] + "/terraform/idp-terraform-latest.json"
            if terraform_module_url
            else None
        ),
    )
    return capsys.readouterr().out


def test_publish_output_yields_the_module_url(cbd, capsys):
    out = _real_publish_output(capsys, terraform_module_url=MODULE_URL)
    artifacts = cbd.parse_publish_output(out, "terraform")
    assert artifacts == {
        "template_url": TEMPLATE_URL,
        "terraform_module_url": MODULE_URL,
    }


def test_the_main_module_is_picked_beside_the_variant_modules(cbd, capsys):
    variant = MODULE_URL.replace(
        "idp-terraform-0.6.14", "idp-terraform-headless-0.6.14"
    )
    out = _real_publish_output(
        capsys,
        terraform_module_url=MODULE_URL,
        variants={"aaa-headless": {"moduleUrl": variant}},
    )
    assert variant in out
    assert cbd.parse_publish_output(out, "terraform")["terraform_module_url"] == (
        MODULE_URL
    )


def test_a_terraform_publish_without_a_module_url_fails(cbd, capsys):
    out = _real_publish_output(capsys)
    with pytest.raises(Exception, match="Terraform module URL"):
        cbd.parse_publish_output(out, "terraform")
    assert cbd.parse_publish_output(out, "cloudformation")["template_url"] == (
        TEMPLATE_URL
    )


# ---------------------------------------------------------------------------
# The deploy command, in each mode
# ---------------------------------------------------------------------------


@pytest.fixture
def faked_deploy(cbd, monkeypatch):
    """Fake IAM and every command; answer the health reads as healthy."""
    state = {
        "iam_calls": [],
        "staged": [],
        "deploy_rc": 0,
        "label": "CREATE_COMPLETE\tIDP (v1) (Terraform) read-only",
        "commands": [],
    }

    def _iam(stack_name, *a, **k):
        state["iam_calls"].append(k)
        role = TF_ROLE_ARN if k.get("terraform_deployer_arn") else ROLE_ARN
        return role, BOUNDARY_ARN

    monkeypatch.setattr(cbd, "create_iam_resources", _iam)
    monkeypatch.setattr(cbd, "_caller_principal_arn", lambda: DEPLOYER_ARN)
    monkeypatch.setattr(cbd, "_wait_until_assumable", lambda arn: True)
    monkeypatch.setattr(
        cbd,
        "stage_terraform_root",
        lambda *a: state["staged"].append((a[2].role_arn, list(a[3]))),
    )
    monkeypatch.delenv("IDP_INTEGRATION_MALWARE_MODE", raising=False)

    def _run(cmd, *a, **k):
        state["commands"].append(cmd)
        if cmd.startswith("idp-cli deploy"):
            return _Result(
                cmd, state["deploy_rc"], "", "Error: boom on aws_s3_bucket.x\n"
            )
        if cmd.startswith("aws cloudformation describe-stacks"):
            return _Result(cmd, 0, state["label"] + "\n")
        if "output -json" in cmd:
            return _Result(cmd, 0, "{}")
        if cmd.startswith("aws cloudformation"):
            return _Result(cmd, 0, "CREATE_COMPLETE")
        return _Result(cmd)

    monkeypatch.setattr(cbd, "run_command", _run)
    return state


def _deploys(state):
    return [c for c in state["commands"] if c.startswith("idp-cli deploy")]


def test_both_modes_pass_the_same_parameters(cbd, faked_deploy, run):
    cbd._deploy_primary_stack_attempt("stk", "a@example.test", TEMPLATE_URL)
    cbd._deploy_primary_terraform_attempt("stk", "a@example.test", run)
    cfn, tf = _deploys(faked_deploy)
    assert assert_deploy_command_is_well_formed(cfn) == (
        assert_deploy_command_is_well_formed(tf)
    )


def test_the_terraform_command_parses_as_a_terraform_deploy(cbd, faked_deploy, run):
    result = cbd._deploy_primary_terraform_attempt("stk", "a@example.test", run)
    assert result == {"stack_name": "stk", "success": True, "iac": "terraform"}
    (command,) = _deploys(faked_deploy)
    assert_deploy_command_is_well_formed(command)
    _, deploy, _ = _cli_authorities()
    params = deploy.make_context("deploy", shlex.split(command)[2:]).params
    assert params["terraform"] is True
    assert params["stack_name"] == "stk"
    assert params["admin_email"] == "a@example.test"
    assert params["terraform_module_url"] == MODULE_URL
    assert params["terraform_dir"] == run.directory
    assert params["terraform_bin"] == run.binary
    # The CloudFormation service role does not apply to Terraform; the providers
    # assume the run's Terraform deploy role, created for this build's principal.
    assert not params.get("role_arn")
    assert not params.get("template_url")
    assert params["terraform_assume_role_arn"] == TF_ROLE_ARN
    assert faked_deploy["iam_calls"] == [{"terraform_deployer_arn": DEPLOYER_ARN}]
    # Staged with the role and the same pairs, before the deploy runs.
    assert faked_deploy["staged"] == [
        (
            TF_ROLE_ARN,
            [
                f"PermissionsBoundaryArn={BOUNDARY_ARN}",
                "MalwareScanning=CREATE_PLAN",
                "EnableMCP=true",
            ],
        )
    ]


def test_the_cloudformation_command_is_unchanged(cbd, faked_deploy):
    cbd._deploy_primary_stack_attempt("stk", "a@example.test", TEMPLATE_URL)
    (command,) = _deploys(faked_deploy)
    _, deploy, _ = _cli_authorities()
    params = deploy.make_context("deploy", shlex.split(command)[2:]).params
    assert params["terraform"] is False
    assert params["role_arn"] == ROLE_ARN
    assert params["template_url"] == TEMPLATE_URL


def test_the_terraform_deploy_keeps_its_log(cbd, faked_deploy, run):
    cbd._deploy_primary_terraform_attempt("stk", "a@example.test", run)
    log = Path(run.directory, cbd.TERRAFORM_DEPLOY_LOG).read_text()
    assert "idp-cli deploy --terraform" in log and "[exit 0]" in log


def test_a_failed_terraform_deploy_carries_the_log_tail(cbd, faked_deploy, run):
    faked_deploy["deploy_rc"] = 1
    result = cbd._deploy_primary_terraform_attempt("stk", "a@example.test", run)
    assert result["success"] is False and result["failure_type"] == "deploy"
    assert result["iac"] == "terraform"
    assert "exit 1" in result["error"]
    assert "Error: boom on aws_s3_bucket.x" in result["terraform_log_tail"]
    assert "cf_events" not in result


def test_a_stack_without_the_terraform_marker_is_not_healthy(cbd, faked_deploy, run):
    faked_deploy["label"] = "CREATE_COMPLETE\tAWS GenAI IDP Accelerator (v1)"
    result = cbd._deploy_primary_terraform_attempt("stk", "a@example.test", run)
    assert result["success"] is False
    assert "(Terraform)" in result["error"]


@pytest.mark.parametrize(
    ("label", "output_rc", "healthy"),
    [
        ("CREATE_COMPLETE\tIDP (v1) (Terraform) label", 0, True),
        ("UPDATE_COMPLETE\tIDP (v1) (Terraform) label", 0, True),
        ("CREATE_COMPLETE\tIDP (v1) (Terraform) label", 1, False),
        ("CREATE_COMPLETE\tIDP (v1)", 0, False),
        ("ROLLBACK_COMPLETE\tIDP (v1) (Terraform) label", 0, False),
        ("CREATE_IN_PROGRESS\tIDP (v1) (Terraform) label", 0, False),
        (
            "An error occurred (ValidationError): Stack with id s does not exist",
            0,
            False,
        ),
        ("", 0, False),
    ],
)
def test_the_health_judgement(cbd, label, output_rc, healthy):
    assert cbd.judge_terraform_health(label, output_rc)[0] is healthy


def test_a_terraform_deploy_is_not_retried(cbd, monkeypatch, run):
    calls = []
    monkeypatch.setattr(
        cbd,
        "_deploy_primary_terraform_attempt",
        lambda *a: (
            calls.append(a) or {"stack_name": "stk", "success": False, "error": "x"}
        ),
    )

    def _cfn(*_a, **_k):
        raise AssertionError("the CloudFormation deploy ran in Terraform mode")

    monkeypatch.setattr(cbd, "_deploy_primary_stack_attempt", _cfn)
    monkeypatch.setattr(cbd, "_is_transient_logs_race", lambda r: True)
    result = cbd.deploy_and_test_stack(
        "stk", "a@example.test", TEMPLATE_URL, terraform=run
    )
    assert len(calls) == 1 and result["success"] is False


def test_the_delete_command_parses_as_a_terraform_delete(cbd, run):
    from idp_cli.cli import delete

    command = cbd.build_terraform_delete_command("stk", run)
    argv = shlex.split(command)
    assert argv[:2] == ["idp-cli", "delete"]
    params = delete.make_context("delete", argv[2:]).params
    assert params["terraform"] is True and params["force"] is True
    assert params["terraform_dir"] == run.directory
    assert params["terraform_bin"] == run.binary
    # Refused with --terraform by the CLI; the driver sweeps instead.
    assert params["force_delete_all"] is False


# ---------------------------------------------------------------------------
# Teardown dispatch
# ---------------------------------------------------------------------------


@pytest.fixture
def teardown_spy(cbd, monkeypatch):
    calls = []
    monkeypatch.setattr(cbd, "cleanup_stack", lambda r: calls.append("cfn") or [])
    monkeypatch.setattr(
        cbd,
        "cleanup_terraform_deployment",
        lambda r, t: calls.append(("terraform", t)) or ["a finding"],
    )
    monkeypatch.setattr(
        cbd, "retain_failed_stack", lambda s: calls.append("retain") or True
    )
    return calls


@pytest.mark.parametrize("success", [True, False])
def test_terraform_teardown_dispatches_and_never_retains(
    cbd, teardown_spy, run, success
):
    outcome = cbd.teardown_primary_stack(
        {"stack_name": "stk", "success": success}, "stk", terraform=run
    )
    assert teardown_spy == [("terraform", run)]
    assert outcome == {"retained": False, "findings": ["a finding"]}


def test_cloudformation_teardown_is_unchanged(cbd, teardown_spy):
    assert cbd.teardown_primary_stack({"stack_name": "s", "success": True}, "s") == {
        "retained": False,
        "findings": [],
    }
    assert cbd.teardown_primary_stack({"stack_name": "s", "success": False}, "s") == {
        "retained": True,
        "findings": [],
    }
    assert teardown_spy == ["cfn", "retain"]


@pytest.fixture
def faked_cleanup(cbd, monkeypatch):
    state = {
        "commands": [],
        "delete_rc": 0,
        "retained": {"dynamodb_tables": [], "log_groups": [], "s3_buckets": []},
        "swept": [],
        "sweep_errors": [],
    }

    def _run(cmd, *a, **k):
        state["commands"].append(cmd)
        return _Result(
            cmd, state["delete_rc"] if cmd.startswith("idp-cli delete") else 0
        )

    def _retained(_t):
        if isinstance(state["retained"], Exception):
            raise state["retained"]
        return state["retained"]

    monkeypatch.setattr(cbd, "run_command", _run)
    monkeypatch.setattr(cbd, "cancel_bedrock_ingestion_jobs", lambda s: None)
    monkeypatch.setattr(cbd, "_terraform_retained_resources", _retained)
    monkeypatch.setattr(
        cbd,
        "_sweep_terraform_retained",
        lambda s, r: state["swept"].append(r) or list(state["sweep_errors"]),
    )
    monkeypatch.setattr(cbd, "cleanup_iam_resources", lambda s: ["iam finding"])
    return state


def test_terraform_cleanup_deletes_with_terraform_and_skips_cloudformation_reads(
    cbd, faked_cleanup, run
):
    findings = cbd.cleanup_terraform_deployment({"stack_name": "stk"}, run)
    assert findings == ["iam finding"]
    assert faked_cleanup["commands"] == [cbd.build_terraform_delete_command("stk", run)]
    assert faked_cleanup["swept"] == [faked_cleanup["retained"]]
    assert Path(run.directory, cbd.TERRAFORM_DELETE_LOG).is_file()


def test_a_failed_destroy_is_a_finding(cbd, faked_cleanup, run):
    faked_cleanup["delete_rc"] = 1
    findings = cbd.cleanup_terraform_deployment({"stack_name": "stk"}, run)
    assert any("idp-cli delete --terraform exited 1" in f for f in findings)
    assert findings[-1] == "iam finding"


def test_an_unread_state_is_a_finding_not_a_skipped_sweep(cbd, faked_cleanup, run):
    faked_cleanup["retained"] = RuntimeError("no state")
    findings = cbd.cleanup_terraform_deployment({"stack_name": "stk"}, run)
    assert any("could not be read" in f for f in findings)
    assert faked_cleanup["swept"] == []


def test_a_retained_resource_of_another_kind_is_a_finding(cbd, faked_cleanup, run):
    faked_cleanup["retained"] = {
        "other": [{"type": "aws_kms_key", "physical_id": "k-1", "logical_id": "x"}]
    }
    findings = cbd.cleanup_terraform_deployment({"stack_name": "stk"}, run)
    assert any("aws_kms_key k-1" in f for f in findings)


def test_the_state_is_removed_only_after_a_clean_destroy(
    cbd, faked_cleanup, run, monkeypatch
):
    removed = []
    monkeypatch.setattr(
        cbd, "remove_terraform_state", lambda s, t: removed.append(s) or None
    )
    run.state_bucket = "ci-bucket"
    cbd.cleanup_terraform_deployment({"stack_name": "stk"}, run)
    assert removed == ["stk"]
    faked_cleanup["delete_rc"] = 1
    findings = cbd.cleanup_terraform_deployment({"stack_name": "stk"}, run)
    assert removed == ["stk"]
    assert any("cleanup_stale_terraform_deployments" in f for f in findings)


def test_the_reaper_leaves_iam_to_the_stack_sweep(cbd, faked_cleanup, run):
    assert cbd.cleanup_terraform_deployment({"stack_name": "stk"}, run, iam=False) == []


def test_the_sweep_runs_as_the_deploy_role(cbd, faked_cleanup, run, monkeypatch):
    roles = []

    class _Session:
        def __init__(self, role_arn):
            roles.append(role_arn)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(cbd, "_assumed_role_default_session", _Session)
    run.role_arn = TF_ROLE_ARN
    cbd.cleanup_terraform_deployment({"stack_name": "stk"}, run)
    assert roles == [TF_ROLE_ARN]
    assert f"--terraform-assume-role-arn {TF_ROLE_ARN}" in faked_cleanup["commands"][0]


def test_stackdeployer_still_has_what_the_sweeps_call():
    """The sweeps call these private StackDeployer methods (no public API deletes
    a resource outside a stack). A rename in the SDK must fail here, not as an
    AttributeError in a CI teardown that then leaks the resources."""
    from idp_sdk._core.stack import StackDeployer

    for name in (
        "_delete_dynamodb_table",
        "_delete_log_group",
        "_empty_and_delete_bucket",
        "_discover_auto_created_log_groups",
    ):
        assert callable(getattr(StackDeployer, name, None)), name


def test_an_unswept_retained_resource_is_a_finding(cbd, faked_cleanup, run):
    faked_cleanup["sweep_errors"] = ["S3 bucket b: AccessDenied"]
    findings = cbd.cleanup_terraform_deployment({"stack_name": "stk"}, run)
    assert "retained resource not deleted: S3 bucket b: AccessDenied" in findings


def test_the_retained_sweep_order_and_logging_bucket_last(cbd):
    calls = []
    deployer = SimpleNamespace(
        _delete_dynamodb_table=lambda n: calls.append(("table", n)),
        _discover_auto_created_log_groups=lambda s: ["/aws/lambda/stk-a", "lg-1"],
        _delete_log_group=lambda n: calls.append(("log", n)),
        _empty_and_delete_bucket=lambda n: calls.append(("bucket", n)),
    )
    retained = {
        "dynamodb_tables": [{"physical_id": "t1"}],
        "log_groups": [{"physical_id": "lg-1"}],
        "s3_buckets": [
            {
                "logical_id": "module.idp.aws_s3_bucket.LoggingBucket",
                "physical_id": "logs",
            },
            {"logical_id": "module.idp.aws_s3_bucket.InputBucket", "physical_id": "in"},
        ],
    }
    assert cbd._sweep_terraform_retained("stk", retained, deployer) == []
    assert calls == [
        ("table", "t1"),
        ("log", "lg-1"),
        ("log", "/aws/lambda/stk-a"),
        ("bucket", "in"),
        ("bucket", "logs"),
    ]


def test_a_sweep_failure_is_reported_and_the_rest_continue(cbd):
    def _boom(name):
        raise RuntimeError("AccessDenied")

    deleted = []
    deployer = SimpleNamespace(
        _delete_dynamodb_table=_boom,
        _discover_auto_created_log_groups=lambda s: [],
        _delete_log_group=deleted.append,
        _empty_and_delete_bucket=deleted.append,
    )
    errors = cbd._sweep_terraform_retained(
        "stk",
        {
            "dynamodb_tables": [{"physical_id": "t1"}],
            "s3_buckets": [{"physical_id": "b"}],
        },
        deployer,
    )
    assert errors == ["DynamoDB table t1: AccessDenied"]
    assert deleted == ["b"]


def test_the_retained_list_is_read_with_the_sdks_own_helpers(cbd, monkeypatch, run):
    from idp_sdk._core import terraform_deploy as tf

    state = {
        "values": {
            "root_module": {
                "child_modules": [
                    {
                        "resources": [
                            {
                                "address": "module.idp.aws_s3_bucket.LoggingBucket",
                                "mode": "managed",
                                "type": "aws_s3_bucket",
                                "values": {"bucket": "stk-logs"},
                            },
                            {
                                "address": "module.idp.aws_sqs_queue.Q",
                                "mode": "managed",
                                "type": "aws_sqs_queue",
                                "values": {"name": "q"},
                            },
                        ]
                    }
                ]
            }
        }
    }

    class _Runner:
        def __init__(self, binary, directory):
            assert binary == run.binary and str(directory) == run.directory

        def show_state(self):
            return state

        def state_list(self):
            return [
                "module.idp.aws_s3_bucket.LoggingBucket",
                "module.idp.aws_sqs_queue.Q",
            ]

    monkeypatch.setattr(tf, "TerraformRunner", _Runner)
    monkeypatch.setattr(
        tf,
        "read_manifest_from_dir",
        lambda d: {"retainedAddresses": ["aws_s3_bucket.LoggingBucket"]},
    )
    report = cbd._terraform_retained_resources(run)
    assert [b["physical_id"] for b in report["s3_buckets"]] == ["stk-logs"]
    assert report["other"] == []


# ---------------------------------------------------------------------------
# Failure evidence
# ---------------------------------------------------------------------------


def test_the_log_tail_reads_both_logs_newest_last(cbd, tmp_path):
    (tmp_path / cbd.TERRAFORM_DEPLOY_LOG).write_text(
        "".join(f"line {i}\n" for i in range(500))
    )
    (tmp_path / cbd.TERRAFORM_DELETE_LOG).write_text("destroyed\n")
    tail = cbd.terraform_log_tail(str(tmp_path), lines=3)
    assert "line 499" in tail and "line 496" not in tail
    assert tail.index("line 499") < tail.index("destroyed")


def test_a_missing_log_says_so(cbd, tmp_path):
    assert "no Terraform log found" in cbd.terraform_log_tail(str(tmp_path / "x"))


def test_a_terraform_deploy_failure_is_analysed_from_the_log(cbd, monkeypatch):
    prompts = []
    monkeypatch.setattr(cbd, "_invoke_bedrock", lambda p: prompts.append(p) or "ok")

    def _no_stack_events(*_a, **_k):
        raise AssertionError("read CloudFormation events for a Terraform deploy")

    monkeypatch.setattr(cbd, "get_cloudformation_logs", _no_stack_events)
    monkeypatch.setattr(cbd, "get_codebuild_failure_details", _no_stack_events)
    result = {
        "stack_name": "stk",
        "success": False,
        "failure_type": "deploy",
        "iac": "terraform",
        "error": "Terraform deployment failed",
        "terraform_log_tail": "Error: creating Lambda Function (stk-X)",
    }
    assert cbd.generate_deployment_summary(result, "stk", TEMPLATE_URL) == "ok"
    assert "Error: creating Lambda Function (stk-X)" in prompts[0]
    assert "Terraform" in prompts[0]


def test_the_evidence_bundle_carries_the_log_tail(cbd, monkeypatch):
    for name in (
        "get_workflow_failure_details",
        "snapshot_execution_inventory",
        "snapshot_tracking_rows",
    ):
        monkeypatch.setattr(cbd, name, lambda *a, **k: [])
    tf = cbd.collect_failure_evidence(
        "stk", {"iac": "terraform", "terraform_log_tail": "Error: x"}
    )
    cfn = cbd.collect_failure_evidence("stk", {})
    assert tf["terraform_log_tail"] == "Error: x"
    assert "terraform_log_tail" not in cfn


# ---------------------------------------------------------------------------
# The pinned Terraform install
# ---------------------------------------------------------------------------


@pytest.fixture
def no_terraform_on_path(monkeypatch, tmp_path):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    return {"IDP_TERRAFORM_WORK_ROOT": str(tmp_path / "work")}


def test_a_binary_on_path_is_used_as_is(cbd, monkeypatch, tmp_path):
    binary = tmp_path / "bin" / "tofu"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary.parent))

    def _no_install(*_a):
        raise AssertionError("installed although a binary was named")

    found = cbd.ensure_terraform_binary(
        {"IDP_TERRAFORM_BIN": "tofu"}, installer=_no_install
    )
    assert found == str(binary)


def test_a_named_binary_that_is_missing_is_refused(cbd, no_terraform_on_path):
    with pytest.raises(RuntimeError, match="not found"):
        cbd.ensure_terraform_binary(
            {**no_terraform_on_path, "IDP_TERRAFORM_BIN": "tofu"}
        )


def test_the_default_is_installed_by_the_gates_installer(cbd, no_terraform_on_path):
    calls = []
    path = cbd.ensure_terraform_binary(
        no_terraform_on_path,
        installer=lambda binary, cache, plat: (
            calls.append((str(binary), cache, plat)) or Path("/x/terraform")
        ),
    )
    assert path == "/x/terraform"
    ((spec, cache, _plat),) = calls
    assert spec == cbd.TERRAFORM_BINARY_SPEC
    assert cache == Path(no_terraform_on_path["IDP_TERRAFORM_WORK_ROOT"]) / "tools"


def test_an_installer_refusal_is_raised(cbd, no_terraform_on_path):
    check = cbd._terraform_check()

    def _refuse(*_a):
        raise check.GateError("downloaded archive hashes to x, expected y")

    with pytest.raises(RuntimeError, match="hashes to x"):
        cbd.ensure_terraform_binary(no_terraform_on_path, installer=_refuse)


def test_the_pinned_binary_is_in_the_lock_for_every_linux_platform(cbd):
    check = cbd._terraform_check()
    binary = check.Binary.parse(cbd.TERRAFORM_BINARY_SPEC)
    for plat in ("linux_amd64", "linux_arm64"):
        assert len(check.locked_sha(binary, plat)) == 64


def test_the_provider_pins_are_the_gates(cbd, tmp_path):
    check = cbd._terraform_check()
    path = Path(cbd.write_terraform_pins(str(tmp_path)))
    assert path.name == check.OVERRIDE_FILE
    text = path.read_text()
    assert text == check.override_tf(check.gate_providers([]))
    for source, version in check.gate_providers([]).items():
        assert f'source  = "{source}"' in text and f'"= {version}"' in text


def test_the_backend_is_s3_encrypted_and_keyed_by_stack(cbd, run):
    text = cbd.render_terraform_backend(
        "ci-bucket", run.state_prefix("stk") + cbd.TERRAFORM_STATE_KEY, "us-east-1"
    )
    assert 'backend "s3"' in text
    assert 'bucket  = "ci-bucket"' in text
    assert 'key     = "terraform-runs/stk/terraform.tfstate"' in text
    assert "encrypt = true" in text
    assert "use_lockfile = true" in text


# ---------------------------------------------------------------------------
# Labels and the UX review stack
# ---------------------------------------------------------------------------


def test_a_terraform_run_is_labelled(cbd):
    environ = {"IDP_IAC": "terraform"}
    assert cbd.label_ci_run("body", environ).startswith(f"🧱 {cbd.TERRAFORM_RUN_LABEL}")
    assert cbd.label_ci_run("body", {}) == "body"
    assert cbd.failure_subject("stk", environ) == (
        "IDP CI failure (nightly terraform run): stk"
    )
    assert cbd.failure_subject("stk", {}) == "IDP CI failure: stk"


_SCHEDULED_DEVELOP = {"pipeline_source": "schedule", "ref": "develop"}


def test_a_terraform_run_leaves_the_ux_review_stack_alone(cbd):
    assert cbd.ux_nightly_update_requested(_SCHEDULED_DEVELOP, {})
    assert not cbd.ux_nightly_update_requested(
        {**_SCHEDULED_DEVELOP, "iac": "terraform"}, {}
    )
    assert not cbd.ux_nightly_update_requested(
        _SCHEDULED_DEVELOP, {"IDP_IAC": "terraform"}
    )


# ---------------------------------------------------------------------------
# GitLab
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def config():
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_constructor(
        "!reference", lambda loader, node: loader.construct_sequence(node)
    )
    return yaml.load(CI_CONFIG.read_text(encoding="utf-8"), Loader=_Loader)


def _script_text(job):
    parts = []
    for key in ("before_script", "script", "after_script"):
        for line in job.get(key, []) or []:
            parts.extend(line if isinstance(line, list) else [line])
    return "\n".join(str(p) for p in parts)


def _after_script_payload(config, tmp_path, environ):
    """Run integration_tests' Slack payload builder as the job runs it."""
    body = next(
        line
        for line in config["integration_tests"]["after_script"]
        if isinstance(line, str) and "slack-payload.json" in line
    )
    code = textwrap.dedent(re.search(r"<<'PY'\n(.*?)\n\s*PY\n", body, re.S).group(1))
    (tmp_path / "deployment_summary.txt").write_text("all good")
    env = {"PATH": os.environ.get("PATH", ""), **environ}
    out = subprocess.run(
        [sys.executable, "-", "success", "https://job.example/1", "a" * 40],
        input=code,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        check=True,
    )
    return json.loads(out.stdout)


@pytest.mark.parametrize(
    ("environ", "labelled"),
    [
        ({"CI_PIPELINE_SOURCE": "schedule", "IDP_IAC": " Terraform "}, True),
        ({"CI_PIPELINE_SOURCE": "schedule"}, False),
        ({"CI_PIPELINE_SOURCE": "web", "IDP_IAC": "terraform"}, False),
    ],
)
def test_the_job_post_renders_the_terraform_label(config, tmp_path, environ, labelled):
    status = _after_script_payload(config, tmp_path, environ)["status"]
    assert status.startswith("🧱 NIGHTLY TERRAFORM RUN · ") is labelled, status


@pytest.mark.parametrize(
    ("environ", "labelled"),
    [
        ({"CI_PIPELINE_SOURCE": "schedule", "IDP_IAC": "terraform"}, True),
        ({"CI_PIPELINE_SOURCE": "schedule"}, False),
    ],
)
def test_the_fallback_post_renders_the_terraform_label(
    config, tmp_path, monkeypatch, environ, labelled
):
    script = config["nightly_notification"]["script"][0]
    code = textwrap.dedent(
        re.search(r"<<'PY'\n(.*?)\n\s*PY\s*$", script, re.S).group(1)
    )
    namespace: dict[str, Any] = {"__name__": "nightly_notification"}
    exec(compile(code, "nightly_notification", "exec"), namespace)  # noqa: S102  # nosec B102 - this repo's own CI script under test
    monkeypatch.chdir(tmp_path)
    (tmp_path / "deployment_summary.txt").write_text("summary")
    for name in ("IDP_IAC", "IDP_RUN_PROBES", "IDP_TEST_REGION", "GITLAB_REVIEW_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    sent = []

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    import urllib.request

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda req, timeout=None: sent.append(req) or _Response(),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["x", "https://pipeline.example/1", "a" * 40, "https://hook.example"],
    )
    namespace["main"]()
    (request,) = sent
    status = json.loads(request.data)["status"]
    assert status.startswith("🧱 NIGHTLY TERRAFORM RUN · ") is labelled, status


def test_no_new_job_is_added_for_it(config):
    """The Terraform run is the integration_tests job on its own schedule, as the
    probe and region runs are; a second deploy job would race the first for the
    same resource group on the same schedule and break nightly_notification's
    reading of which job ran."""
    deploy_jobs = [
        name
        for name, job in config.items()
        if isinstance(job, dict)
        and "integration_test_deployment.py" in _script_text(job)
    ]
    assert deploy_jobs == ["integration_tests"]


# ---------------------------------------------------------------------------
# The per-run Terraform deploy role
# ---------------------------------------------------------------------------

SERVICE_ROLE_TEMPLATE = (
    REPO_ROOT
    / "iam-roles/cloudformation-management/IDP-Cloudformation-Service-Role.yaml"
)


@pytest.fixture
def role_template(cbd):
    text = SERVICE_ROLE_TEMPLATE.read_text(encoding="utf-8")
    return cbd.terraform_deploy_role_template(text), yaml.load(
        text, Loader=cbd._CfnLongFormLoader
    )


def test_the_customer_template_is_kept_whole(role_template):
    derived, original = role_template
    assert (
        derived["Resources"]["CloudFormationServiceRole"]
        == (original["Resources"]["CloudFormationServiceRole"])
    )
    assert (
        derived["Resources"]["PassRolePolicy"]
        == original["Resources"]["PassRolePolicy"]
    )
    for name, output in original["Outputs"].items():
        assert derived["Outputs"][name] == output


def test_the_deploy_role_has_the_service_roles_policies_and_cloud_control(
    cbd, role_template
):
    derived, original = role_template
    service = original["Resources"]["CloudFormationServiceRole"]["Properties"]
    role = derived["Resources"]["TerraformDeployRole"]["Properties"]
    # By value: every policy the service role has, unchanged, first.
    assert role["Policies"][: len(service["Policies"])] == service["Policies"]
    (extra,) = role["Policies"][len(service["Policies"]) :]
    statements = {st["Sid"]: st for st in extra["PolicyDocument"]["Statement"]}
    assert statements["TerraformCloudControl"]["Action"] == ["cloudcontrol:*"]
    deny = statements["DenyModifyingThisTerraformDeployRole"]
    assert deny["Effect"] == "Deny"
    assert "iam:UpdateAssumeRolePolicy" in deny["Action"]
    assert deny["Resource"]["Fn::Sub"].endswith("${AWS::StackName}-TFDeployRole")
    assert role["RoleName"] == {"Fn::Sub": "${AWS::StackName}-TFDeployRole"}
    # The same own-boundary choice as the service role.
    assert role["PermissionsBoundary"] == service["PermissionsBoundary"]


def test_the_deploy_role_trusts_only_the_named_principal(role_template):
    derived, _ = role_template
    trust = derived["Resources"]["TerraformDeployRole"]["Properties"][
        "AssumeRolePolicyDocument"
    ]
    assert trust["Statement"] == [
        {
            "Sid": "AllowOnlyThisRunsTerraformDeployer",
            "Effect": "Allow",
            "Principal": {"AWS": {"Ref": "TerraformDeployerPrincipalArn"}},
            "Action": "sts:AssumeRole",
        }
    ]
    assert "TerraformDeployerPrincipalArn" in derived["Parameters"]
    assert derived["Outputs"]["TerraformDeployRoleArn"]["Value"] == {
        "Fn::GetAtt": ["TerraformDeployRole", "Arn"]
    }


def test_the_derived_template_fits_a_template_body(cbd):
    body = json.dumps(
        cbd.terraform_deploy_role_template(SERVICE_ROLE_TEMPLATE.read_text())
    )
    assert len(body.encode()) < 51_200
    assert "!Sub" not in body and "!Ref" not in body


def test_the_codebuild_role_may_assume_the_run_role_and_no_other(cbd):
    """Read the grant as IAM would: resolve the template's ARN pattern, then match
    the role name the pre-stack creates for a CI run against it."""
    template = yaml.load(
        (REPO_ROOT / "scripts/sdlc/cfn/codepipeline-s3.yml").read_text(),
        Loader=cbd._CfnLongFormLoader,
    )
    statements = [
        statement
        for policy in template["Resources"]["CodeBuildRole"]["Properties"]["Policies"]
        # A conditional policy (Fn::If) is a VPC network grant, not an assume.
        if "PolicyDocument" in policy
        for statement in policy["PolicyDocument"]["Statement"]
        if "sts:AssumeRole" in statement.get("Action", [])
    ]
    patterns = [
        st["Resource"]["Fn::Sub"]
        .replace("${AWS::Partition}", "aws")
        .replace("${AWS::AccountId}", "123456789012")
        for st in statements
    ]
    derived = cbd.terraform_deploy_role_template(SERVICE_ROLE_TEMPLATE.read_text())
    role_name = derived["Resources"]["TerraformDeployRole"]["Properties"]["RoleName"][
        "Fn::Sub"
    ].replace("${AWS::StackName}", "idp-0101-000000-iam")
    arn = f"arn:aws:iam::123456789012:role/{role_name}"

    def allowed(candidate):
        return any(fnmatch.fnmatchcase(candidate, p) for p in patterns)

    assert allowed(arn)
    for other in (
        "idp-0101-000000-iam-CFServiceRole",
        "idp-0101-000000-OCRFunctionRole-abc",
        "admin-TFDeployRole",
    ):
        assert not allowed(f"arn:aws:iam::123456789012:role/{other}"), other


@pytest.mark.parametrize(
    ("arn", "principal"),
    [
        (
            "arn:aws:sts::123456789012:assumed-role/codebuild-role/AWSCodeBuild-x",
            "arn:aws:iam::123456789012:role/codebuild-role",
        ),
        ("arn:aws:iam::123456789012:user/dev", "arn:aws:iam::123456789012:user/dev"),
    ],
)
def test_the_caller_principal(cbd, arn, principal):
    sts = SimpleNamespace(get_caller_identity=lambda: {"Arn": arn})
    assert cbd._caller_principal_arn(sts) == principal


def test_the_terraform_pre_stack_uses_the_derived_template(cbd, monkeypatch, tmp_path):
    monkeypatch.chdir(REPO_ROOT)
    created = {}

    class _Cfn:
        class exceptions:
            AlreadyExistsException = type("AlreadyExists", (Exception,), {})

        def create_stack(self, **kw):
            created.update(kw)

        def get_waiter(self, _name):
            return SimpleNamespace(wait=lambda **kw: None)

        def describe_stacks(self, **kw):
            return {
                "Stacks": [
                    {
                        "Outputs": [
                            {"OutputKey": "ServiceRoleArn", "OutputValue": ROLE_ARN},
                            {
                                "OutputKey": "TerraformDeployRoleArn",
                                "OutputValue": TF_ROLE_ARN,
                            },
                        ]
                    }
                ]
            }

    clients = {
        "cloudformation": _Cfn(),
        "iam": object(),
        "sts": SimpleNamespace(get_caller_identity=lambda: {"Account": "1"}),
    }
    monkeypatch.setattr(cbd.boto3, "client", lambda name, **kw: clients[name])
    monkeypatch.setattr(cbd, "_ensure_boundary_policy", lambda *a: BOUNDARY_ARN)
    assert cbd.create_iam_resources("stk", terraform_deployer_arn=DEPLOYER_ARN) == (
        TF_ROLE_ARN,
        BOUNDARY_ARN,
    )
    body = json.loads(created["TemplateBody"])
    assert "TerraformDeployRole" in body["Resources"]
    assert {
        "ParameterKey": "TerraformDeployerPrincipalArn",
        "ParameterValue": DEPLOYER_ARN,
    } in created["Parameters"]
    created.clear()
    assert cbd.create_iam_resources("stk") == (ROLE_ARN, BOUNDARY_ARN)
    assert created["TemplateBody"] == SERVICE_ROLE_TEMPLATE.read_text()


# ---------------------------------------------------------------------------
# Durable state: staging, removal, and the startup reaper
# ---------------------------------------------------------------------------

MANIFEST = {
    "formatVersion": 1,
    "parameters": {
        "AdminEmail": {"variable": "admin_email", "type": "String", "required": True},
        "PermissionsBoundaryArn": {
            "variable": "permissions_boundary_arn",
            "type": "String",
        },
        "MalwareScanning": {"variable": "malware_scanning", "type": "String"},
    },
    "retainedAddresses": [],
}


class _FakeS3:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.puts = []
        self.deleted = []

    def put_object(self, **kw):
        self.puts.append(kw)
        self.objects[kw["Key"]] = kw["Body"]

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, **kw):
        self.deleted.append((kw["Key"], kw.get("VersionId")))

    def get_paginator(self, name):
        objects = self.objects

        class _P:
            def paginate(self, Bucket, Prefix):
                keys = [
                    k for k in objects if isinstance(k, str) and k.startswith(Prefix)
                ]
                if name == "list_object_versions":
                    yield {"Versions": [{"Key": k, "VersionId": "v1"} for k in keys]}
                else:
                    yield {
                        "Contents": [
                            {"Key": k, "LastModified": objects.get(("mtime", k))}
                            for k in keys
                        ]
                    }

        return _P()


def test_the_root_is_staged_beside_the_state_before_init(cbd, monkeypatch, run):
    from idp_sdk._core import terraform_deploy as tf

    monkeypatch.setattr(tf, "load_module_manifest", lambda url: MANIFEST)
    run.state_bucket = "ci-bucket"
    run.role_arn = TF_ROLE_ARN
    s3 = _FakeS3()
    cbd.stage_terraform_root(
        "stk",
        "a@example.test",
        run,
        [f"PermissionsBoundaryArn={BOUNDARY_ARN}", "MalwareScanning=GATE_ONLY"],
        s3=s3,
    )
    keys = {put["Key"] for put in s3.puts}
    check = cbd._terraform_check()
    assert keys == {
        f"terraform-runs/stk/{name}"
        for name in (*cbd.TERRAFORM_ROOT_FILES, check.OVERRIDE_FILE, "run.json")
    }
    assert all(put["ServerSideEncryption"] == "AES256" for put in s3.puts)
    main = Path(run.directory, "main.tf").read_text()
    assert main.count(TF_ROLE_ARN) == 2
    tfvars = Path(run.directory, "terraform.tfvars").read_text()
    assert BOUNDARY_ARN in tfvars and "GATE_ONLY" in tfvars
    record = json.loads(s3.objects["terraform-runs/stk/run.json"])
    assert record["role_arn"] == TF_ROLE_ARN and record["module_url"] == MODULE_URL


def test_removing_the_state_deletes_every_version(cbd, run):
    run.state_bucket = "ci-bucket"
    s3 = _FakeS3(
        {"terraform-runs/stk/terraform.tfstate": b"{}", "terraform-runs/other/x": b""}
    )
    assert cbd.remove_terraform_state("stk", run, s3=s3) is None
    assert s3.deleted == [("terraform-runs/stk/terraform.tfstate", "v1")]


def _aged(hours):
    from datetime import datetime, timedelta, timezone

    return datetime.now(tz=timezone.utc) - timedelta(hours=hours)


@pytest.fixture
def reaper(cbd, monkeypatch):
    objects = {
        "terraform-runs/idp-0101-000000/terraform.tfstate": b"{}",
        ("mtime", "terraform-runs/idp-0101-000000/terraform.tfstate"): _aged(5),
        "terraform-runs/idp-0101-000000/main.tf": b"# main",
        ("mtime", "terraform-runs/idp-0101-000000/main.tf"): _aged(5),
        "terraform-runs/idp-0202-000000/terraform.tfstate": b"{}",
        ("mtime", "terraform-runs/idp-0202-000000/terraform.tfstate"): _aged(0.5),
    }
    s3 = _FakeS3(objects)
    s3.objects = {k: v for k, v in objects.items()}
    monkeypatch.setattr(cbd.boto3, "client", lambda name, **kw: s3)
    monkeypatch.setattr(cbd, "_terraform_state_bucket", lambda: "ci-bucket")
    state = {"reaped": [], "result": [], "timed_out": False, "s3": s3}

    def _reap(stack_name, bucket, keys, s3=None):
        state["reaped"].append(stack_name)
        return {"findings": list(state["result"]), "timed_out": state["timed_out"]}

    monkeypatch.setattr(cbd, "_reap_terraform_run", _reap)
    return state


ATTEMPTS = "terraform-runs/idp-0101-000000/reap-attempts.json"


def _set_attempts(reaper, failures):
    reaper["s3"].objects[ATTEMPTS] = json.dumps({"failures": failures}).encode()
    # An attempt record is written later than the state, and must not make the
    # prefix look young.
    reaper["s3"].objects[("mtime", ATTEMPTS)] = _aged(0.1)


def test_a_failed_reap_below_the_threshold_is_fatal_and_counted(cbd, reaper):
    reaper["result"] = ["idp-0101-000000: destroy failed"]
    _set_attempts(reaper, cbd.TERRAFORM_REAP_MAX_ATTEMPTS - 2)
    abandoned = []
    findings = cbd.cleanup_stale_terraform_deployments(set(), abandoned)
    assert findings and abandoned == []
    assert reaper["reaped"] == ["idp-0101-000000"]
    recorded = json.loads(reaper["s3"].objects[ATTEMPTS])
    assert recorded["failures"] == cbd.TERRAFORM_REAP_MAX_ATTEMPTS - 1


def test_the_failure_that_reaches_the_threshold_is_not_fatal(cbd, reaper):
    reaper["result"] = ["idp-0101-000000: destroy failed"]
    _set_attempts(reaper, cbd.TERRAFORM_REAP_MAX_ATTEMPTS - 1)
    abandoned, pending = [], set()
    assert cbd.cleanup_stale_terraform_deployments(pending, abandoned) == []
    (note,) = abandoned
    assert "s3://ci-bucket/terraform-runs/idp-0101-000000/" in note
    assert "idp-cli delete --terraform --stack-name idp-0101-000000" in note
    assert "idp-0101-000000" in pending  # its stacks are still kept


def test_a_run_past_the_threshold_is_not_retried(cbd, reaper):
    _set_attempts(reaper, cbd.TERRAFORM_REAP_MAX_ATTEMPTS)
    abandoned = []
    assert cbd.cleanup_stale_terraform_deployments(set(), abandoned) == []
    assert reaper["reaped"] == []
    assert len(abandoned) == 1


def test_a_first_timeout_is_fatal_and_a_repeated_one_is_not(cbd, reaper):
    reaper["result"] = ["idp-0101-000000: idp-cli delete --terraform exited -1"]
    reaper["timed_out"] = True
    abandoned = []
    assert cbd.cleanup_stale_terraform_deployments(set(), abandoned)
    assert abandoned == []
    # The first failure was recorded, so the next build's timeout is the second.
    assert cbd.cleanup_stale_terraform_deployments(set(), abandoned) == []
    assert len(abandoned) == 1


def test_the_attempt_record_does_not_reset_the_age_gate(cbd, reaper):
    _set_attempts(reaper, 1)
    cbd.cleanup_stale_terraform_deployments(set(), [])
    assert reaper["reaped"] == ["idp-0101-000000"]


def test_only_a_stale_run_is_reaped_and_a_young_one_is_kept(cbd, reaper):
    pending = set()
    assert cbd.cleanup_stale_terraform_deployments(pending) == []
    assert reaper["reaped"] == ["idp-0101-000000"]
    assert pending == {"idp-0202-000000"}


def test_a_failed_reap_is_a_finding_and_its_stacks_are_kept(cbd, reaper):
    reaper["result"] = ["idp-0101-000000: destroy failed"]
    pending = set()
    findings = cbd.cleanup_stale_terraform_deployments(pending)
    assert findings and findings[0].startswith(cbd.SWEEP_NOT_COMPLETED)
    assert pending == {"idp-0101-000000", "idp-0202-000000"}


def test_an_unlisted_state_bucket_is_a_finding(cbd, monkeypatch):
    def _boom():
        raise RuntimeError("AccessDenied")

    monkeypatch.setattr(cbd, "_terraform_state_bucket", _boom)
    findings = cbd.cleanup_stale_terraform_deployments(set())
    assert findings and "AccessDenied" in findings[0]


def test_the_reaper_is_a_registered_fatal_sweep_before_the_stack_sweep(cbd):
    names = [sweep.func for sweep in cbd.STARTUP_SWEEPS]
    assert names.index("cleanup_stale_terraform_deployments") < names.index(
        "cleanup_stale_idp_stacks"
    )
    (row,) = [
        s for s in cbd.STARTUP_SWEEPS if s.func == "cleanup_stale_terraform_deployments"
    ]
    assert row.fails_run is True


class _StopAfterTheSweeps(BaseException):
    pass


def test_main_runs_the_reaper_first_and_hands_its_runs_to_the_stack_sweep(
    cbd, monkeypatch
):
    monkeypatch.setattr(cbd, "install_suite_transcript", lambda: None)
    monkeypatch.setattr(cbd, "read_ci_run_options", lambda *a: {})
    calls = []
    for sweep in cbd.STARTUP_SWEEPS:
        monkeypatch.setattr(
            cbd,
            sweep.func,
            lambda *a, _n=sweep.func, **k: calls.append((_n, a, k)) or [],
        )

    def _reaper(pending, abandoned):
        calls.append(("cleanup_stale_terraform_deployments", (pending,), {}))
        pending.add("idp-0101-000000")
        return []

    monkeypatch.setattr(cbd, "cleanup_stale_terraform_deployments", _reaper)

    def _stop(*_a, **_k):
        raise _StopAfterTheSweeps()

    monkeypatch.setattr(cbd, "publish_artifacts", _stop)
    with pytest.raises(_StopAfterTheSweeps):
        cbd.main()
    names = [name for name, _, _ in calls]
    assert names.index("cleanup_stale_terraform_deployments") < names.index(
        "cleanup_stale_idp_stacks"
    )
    (_, _, kwargs) = calls[names.index("cleanup_stale_idp_stacks")]
    assert kwargs == {"terraform_runs": {"idp-0101-000000"}}


def test_a_failed_state_listing_reaches_the_stack_sweep_as_unknown(cbd, monkeypatch):
    monkeypatch.setattr(cbd, "install_suite_transcript", lambda: None)
    monkeypatch.setattr(cbd, "read_ci_run_options", lambda *a: {})
    seen = {}
    for sweep in cbd.STARTUP_SWEEPS:
        monkeypatch.setattr(cbd, sweep.func, lambda *a, **k: [])
    monkeypatch.setattr(
        cbd,
        "cleanup_stale_terraform_deployments",
        lambda p, a: ["SWEEP NOT COMPLETED: x"],
    )
    monkeypatch.setattr(
        cbd, "cleanup_stale_idp_stacks", lambda **k: seen.update(k) or []
    )

    def _stop(*_a, **_k):
        raise _StopAfterTheSweeps()

    monkeypatch.setattr(cbd, "publish_artifacts", _stop)
    with pytest.raises(_StopAfterTheSweeps):
        cbd.main()
    assert seen == {"terraform_runs": None}


def test_a_dead_run_is_destroyed_from_its_saved_configuration(cbd, monkeypatch):
    s3 = _FakeS3(
        {
            "terraform-runs/stk/main.tf": b"# main",
            "terraform-runs/stk/terraform.tfvars": b"x = 1",
            "terraform-runs/stk/terraform.tfstate": b"{}",
            "terraform-runs/stk/run.json": json.dumps(
                {"module_url": MODULE_URL, "role_arn": TF_ROLE_ARN}
            ).encode(),
        }
    )
    monkeypatch.setattr(cbd, "ensure_terraform_binary", lambda: "/bin/terraform")
    seen = {}

    def _run(cmd, *a, **k):
        seen["init"] = cmd
        return _Result(cmd, 0)

    def _cleanup(result, terraform, iam=True, delete_timeout=None):
        seen["files"] = sorted(p.name for p in Path(terraform.directory).iterdir())
        seen["role"], seen["iam"] = terraform.role_arn, iam
        seen["timeout"] = delete_timeout
        result["delete_returncode"] = seen.get("delete_rc", 0)
        return [] if not seen.get("delete_rc") else ["destroy failed"]

    monkeypatch.setattr(cbd, "run_command", _run)
    monkeypatch.setattr(cbd, "cleanup_terraform_deployment", _cleanup)
    keys = [k for k in s3.objects if isinstance(k, str)]
    assert cbd._reap_terraform_run("stk", "ci-bucket", keys, s3=s3) == {
        "findings": [],
        "timed_out": False,
    }
    assert seen["files"] == ["main.tf", "terraform.tfvars"]
    assert seen["role"] == TF_ROLE_ARN and seen["iam"] is False
    assert " init " in seen["init"]
    # The destroy is bounded well under the build budget.
    assert seen["timeout"] == cbd.TERRAFORM_REAP_TIMEOUT_SECONDS
    assert cbd.TERRAFORM_REAP_TIMEOUT_SECONDS <= 45 * 60
    # A destroy killed at that bound (run_command's -1) is reported as a timeout.
    seen["delete_rc"] = -1
    outcome = cbd._reap_terraform_run("stk", "ci-bucket", keys, s3=s3)
    assert outcome["timed_out"] is True and outcome["findings"]


def test_a_dead_run_without_its_configuration_is_a_finding(cbd):
    s3 = _FakeS3({"terraform-runs/stk/terraform.tfstate": b"{}"})
    outcome = cbd._reap_terraform_run(
        "stk", "ci-bucket", ["terraform-runs/stk/terraform.tfstate"], s3=s3
    )
    assert outcome["findings"] and "no main.tf" in outcome["findings"][0]


class _StackListing:
    def __init__(self, summaries, outputs=None):
        self.summaries = summaries
        self.deleted = []
        self.outputs = outputs or {}

    def get_paginator(self, _name):
        summaries = self.summaries

        class _P:
            def paginate(self, **kw):
                yield {"StackSummaries": summaries}

        return _P()

    def delete_stack(self, StackName):
        self.deleted.append(StackName)

    def describe_stacks(self, StackName):
        return {"Stacks": [{"Outputs": self.outputs.get(StackName, [])}]}


def _summary(name, hours, description="IDP"):
    return {
        "StackName": name,
        "CreationTime": _aged(hours),
        "TemplateDescription": description,
    }


@pytest.fixture
def stack_sweep(cbd, monkeypatch):
    def _install(summaries):
        cf = _StackListing(summaries)
        monkeypatch.setattr(cbd.boto3, "client", lambda name, **kw: cf)
        monkeypatch.setattr(cbd, "_diagnostics_bucket", lambda: None)
        return cf

    return _install


def test_the_stack_sweep_keeps_every_stack_of_a_run_with_state(cbd, stack_sweep):
    cf = stack_sweep(
        [
            _summary("idp-0101-000000", 5, "IDP (v1) (Terraform) label"),
            _summary("idp-0101-000000-iam", 5),
            _summary("idp-0303-000000", 5),
        ]
    )
    assert cbd.cleanup_stale_idp_stacks(terraform_runs={"idp-0101-000000"}) == []
    assert cf.deleted == ["idp-0303-000000"]


def test_an_unknown_state_listing_keeps_every_labelled_run(cbd, stack_sweep):
    cf = stack_sweep(
        [
            _summary("idp-0101-000000", 5, "IDP (v1) (Terraform) label"),
            _summary("idp-0101-000000-iam", 5),
        ]
    )
    assert cbd.cleanup_stale_idp_stacks(terraform_runs=None) == []
    assert cf.deleted == []


def test_a_stateless_label_is_swept_from_its_directory_first(
    cbd, stack_sweep, monkeypatch
):
    cf = stack_sweep([_summary("idp-0101-000000", 5, "IDP (v1) (Terraform) label")])
    order = []
    monkeypatch.setattr(
        cbd,
        "_sweep_terraform_resource_directory",
        lambda c, name: order.append(("sweep", name)) or ["AWS::SQS::Queue q"],
    )
    original = cf.delete_stack
    cf.delete_stack = lambda StackName: (
        order.append(("delete", StackName)) or original(StackName)
    )
    findings = cbd.cleanup_stale_idp_stacks(terraform_runs=set())
    assert order == [("sweep", "idp-0101-000000"), ("delete", "idp-0101-000000")]
    assert findings and "AWS::SQS::Queue q" in findings[0]


def test_the_directory_sweep_deletes_what_it_can_and_names_the_rest(cbd):
    directory = {
        "root": {
            "resources": [
                {"ResourceType": "AWS::S3::Bucket", "PhysicalResourceId": "b"},
                {"ResourceType": "AWS::SQS::Queue", "PhysicalResourceId": "q"},
                {
                    "ResourceType": "AWS::CloudFormation::Stack",
                    "PhysicalResourceId": "n",
                },
            ],
            "nested": {
                "N": {
                    "resources": [
                        {
                            "ResourceType": "AWS::Lambda::Function",
                            "PhysicalResourceId": "f",
                        }
                    ]
                }
            },
        }
    }
    cf = _StackListing(
        [],
        outputs={
            "lbl": [
                {
                    "OutputKey": "TerraformResourceDirectory",
                    "OutputValue": "s3://bk/dir.json",
                }
            ]
        },
    )
    s3 = _FakeS3({"dir.json": json.dumps(directory).encode()})
    deleted = []
    deployer = SimpleNamespace(
        _empty_and_delete_bucket=lambda n: deleted.append(("bucket", n)),
        _delete_dynamodb_table=lambda n: deleted.append(("table", n)),
    )
    lam = SimpleNamespace(
        delete_function=lambda FunctionName: deleted.append(("fn", FunctionName))
    )
    left = cbd._sweep_terraform_resource_directory(
        cf,
        "lbl",
        deployer,
        clients={"s3": s3, "lambda": lam, "stepfunctions": object()},
    )
    assert deleted == [("bucket", "b"), ("fn", "f")]
    assert left == ["AWS::SQS::Queue q"]


# ---------------------------------------------------------------------------
# The teardown time reserve
# ---------------------------------------------------------------------------


def _codebuild(minutes):
    return SimpleNamespace(
        batch_get_builds=lambda ids: {"builds": [{"timeoutInMinutes": minutes}]}
    )


def test_enough_time_left_starts_the_steps(cbd, monkeypatch):
    monkeypatch.setenv("CODEBUILD_BUILD_ID", "b:1")
    assert cbd.terraform_suite_time_shortfall(0, _codebuild(180), now=60 * 60) is None


def test_too_little_time_left_is_refused(cbd, monkeypatch):
    monkeypatch.setenv("CODEBUILD_BUILD_ID", "b:1")
    reason = cbd.terraform_suite_time_shortfall(0, _codebuild(180), now=90 * 60)
    assert reason and "90 min" in reason


def test_an_unreadable_timeout_is_not_a_shortfall(cbd, monkeypatch):
    monkeypatch.delenv("CODEBUILD_BUILD_ID", raising=False)
    assert cbd.terraform_suite_time_shortfall(0, _codebuild(1), now=10**6) is None


def test_a_shortfall_stops_before_the_steps(cbd, monkeypatch, run):
    monkeypatch.setattr(
        cbd,
        "_deploy_primary_terraform_attempt",
        lambda *a: {"stack_name": "stk", "success": True, "iac": "terraform"},
    )
    monkeypatch.setattr(
        cbd, "terraform_suite_time_shortfall", lambda started: "no time"
    )

    def _step(_stack):
        raise AssertionError("a step ran")

    monkeypatch.setattr(cbd, "PARALLEL_TEST_STEPS", [(_step, "Step 3", "x", "d")])
    result = cbd.deploy_and_test_stack(
        "stk", "a@example.test", TEMPLATE_URL, terraform=run
    )
    assert result["success"] is False and "no time" in result["error"]


def test_the_options_write_is_fatal_on_a_terraform_schedule(itd, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CI_PIPELINE_SOURCE", "schedule")
    monkeypatch.setenv("IDP_IAC", "terraform")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setattr(itd, "write_ci_run_options", lambda *a, **k: {})

    def _must_not_run(*_a, **_k):
        raise AssertionError("packaged a run the build would read as CloudFormation")

    monkeypatch.setattr(itd, "create_deployment_package", _must_not_run)
    with pytest.raises(SystemExit) as excinfo:
        itd.main()
    assert excinfo.value.code == 1


def test_retained_iam_roles_of_the_run_are_swept_and_no_others(cbd):
    retained = {
        "other": [
            {
                "type": "aws_iam_role",
                "physical_id": "idp-1113-051052-DOCUMENTKB-StartIngestionJob-891d5ee",
            },
            {"type": "aws_iam_role", "physical_id": "idp-9999-000000-SomeoneElse"},
            {"type": "aws_kms_alias", "physical_id": "idp-1113-051052-alias"},
        ]
    }
    assert cbd.retained_iam_roles("idp-1113-051052", retained) == [
        "idp-1113-051052-DOCUMENTKB-StartIngestionJob-891d5ee"
    ]
