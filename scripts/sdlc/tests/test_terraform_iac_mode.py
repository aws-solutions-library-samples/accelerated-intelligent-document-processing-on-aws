# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The integration driver's Terraform mode (IDP_IAC=terraform).

A weekly schedule carrying IDP_IAC=terraform deploys the accelerator with
`idp-cli deploy --terraform` instead of CloudFormation and runs the primary suite
against it unchanged. What is pinned here, offline:

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

import hashlib
import io
import os
import shlex
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

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


def test_main_refuses_before_any_sweep_or_deploy(cbd, monkeypatch):
    """A refused combination must not cost a sweep, a publish or a deploy."""
    monkeypatch.setenv("IDP_IAC", "terraform")
    monkeypatch.setenv("IDP_RUN_PROBES", "true")
    monkeypatch.setattr(cbd, "install_suite_transcript", lambda: None)
    monkeypatch.setattr(cbd, "read_ci_run_options", lambda *a: {})
    published, notified = [], []
    monkeypatch.setattr(cbd, "publish_summary_to_s3", published.append)
    monkeypatch.setattr(cbd, "send_failure_notification", lambda *a: notified.append(a))

    def _must_not_run(*_a, **_k):
        raise AssertionError("ran past the refusal")

    for name in ("cleanup_stale_bda_blueprints", "publish_artifacts"):
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
    monkeypatch.setattr(
        cbd, "create_iam_resources", lambda *a, **k: (ROLE_ARN, BOUNDARY_ARN)
    )
    monkeypatch.delenv("IDP_INTEGRATION_MALWARE_MODE", raising=False)
    state = {
        "commands": [],
        "deploy_rc": 0,
        "label": "CREATE_COMPLETE\tIDP (v1) (Terraform) read-only",
    }

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
    # The CloudFormation service role does not apply to Terraform.
    assert not params.get("role_arn")
    assert not params.get("template_url")


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


def _fake_release():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("terraform", "#!/bin/sh\necho Terraform v0\n")
    return buf.getvalue()


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

    def _no_download(_url):
        raise AssertionError("downloaded although a binary was named")

    found = cbd.ensure_terraform_binary(
        {"IDP_TERRAFORM_BIN": "tofu"}, fetch=_no_download
    )
    assert found == str(binary)


def test_a_named_binary_that_is_missing_is_refused(cbd, no_terraform_on_path):
    with pytest.raises(RuntimeError, match="not found"):
        cbd.ensure_terraform_binary(
            {**no_terraform_on_path, "IDP_TERRAFORM_BIN": "tofu"}
        )


def test_the_pinned_release_is_installed_when_its_checksum_matches(
    cbd, monkeypatch, no_terraform_on_path
):
    archive = _fake_release()
    monkeypatch.setitem(
        cbd.TERRAFORM_SHA256, "linux_amd64", hashlib.sha256(archive).hexdigest()
    )
    urls = []
    path = cbd.ensure_terraform_binary(
        no_terraform_on_path,
        fetch=lambda url: urls.append(url) or archive,
        platform_name="linux_amd64",
    )
    assert urls == [
        f"https://releases.hashicorp.com/terraform/{cbd.TERRAFORM_VERSION}/"
        f"terraform_{cbd.TERRAFORM_VERSION}_linux_amd64.zip"
    ]
    assert os.access(path, os.X_OK)
    assert subprocess.run([path], capture_output=True, text=True).stdout.startswith(
        "Terraform"
    )
    # Installed once; a second call reuses it.
    assert (
        cbd.ensure_terraform_binary(
            no_terraform_on_path,
            fetch=lambda u: pytest.fail(u),
            platform_name="linux_amd64",
        )
        == path
    )


def test_an_archive_with_another_checksum_is_refused(cbd, no_terraform_on_path):
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        cbd.ensure_terraform_binary(
            no_terraform_on_path,
            fetch=lambda u: _fake_release(),
            platform_name="linux_amd64",
        )
    assert not list(
        Path(no_terraform_on_path["IDP_TERRAFORM_WORK_ROOT"]).rglob("terraform")
    )


def test_every_pinned_checksum_is_a_sha256(cbd):
    assert set(cbd.TERRAFORM_SHA256) == {"linux_amd64", "linux_arm64"}
    for value in cbd.TERRAFORM_SHA256.values():
        assert len(value) == 64 and int(value, 16) >= 0


@pytest.mark.parametrize(
    ("machine", "expected"), [("x86_64", "linux_amd64"), ("aarch64", "linux_arm64")]
)
def test_the_platform_is_mapped(cbd, monkeypatch, machine, expected):
    monkeypatch.setattr(sys, "platform", "linux")
    assert cbd._terraform_platform(machine) == expected


# ---------------------------------------------------------------------------
# Labels and the UX review stack
# ---------------------------------------------------------------------------


def test_a_terraform_run_is_labelled(cbd):
    environ = {"IDP_IAC": "terraform"}
    assert cbd.label_ci_run("body", environ).startswith(f"🧱 {cbd.TERRAFORM_RUN_LABEL}")
    assert cbd.label_ci_run("body", {}) == "body"
    assert cbd.failure_subject("stk", environ) == (
        "IDP CI failure (weekly terraform run): stk"
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


@pytest.mark.parametrize("job", ["integration_tests", "nightly_notification"])
def test_both_slack_posts_label_the_terraform_run(config, job):
    body = _script_text(config[job])
    assert 'os.environ.get("IDP_IAC", "").strip().lower() == "terraform"' in body
    assert "WEEKLY TERRAFORM RUN" in body


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
