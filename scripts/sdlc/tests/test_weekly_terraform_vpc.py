# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The weekly Terraform VPC run (IDP_IAC=terraform plus IDP_TERRAFORM_VPC=true).

A Terraform run whose primary stack is deployed into the pipeline's persistent
test VPC. What is pinned here, offline:

* the switch on the GitLab side: honoured on a schedule only, refused without
  IDP_IAC=terraform, and carried to the build in ci_run_options.json;
* the switch on the build side: read from the environment or the run options,
  and refused rather than half-honoured when the test VPC is not configured or
  the run is not a Terraform run;
* the deploy command: the VPC pairs reach `idp-cli deploy`'s own parameter dict
  intact, subnet lists included, as the CloudFormation probes' VPC pairs do;
* the labels on the build side and in both GitLab status posts.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest
from test_malware_gate_step import (
    _cli_authorities,
    assert_deploy_command_is_well_formed,
)
from test_terraform_iac_mode import (  # noqa: F401 - pytest fixtures
    BOUNDARY_ARN,
    faked_deploy,
    itd,
    run,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_CONFIG = REPO_ROOT / ".gitlab-ci.yml"

TEST_VPC_ENV = {
    "IDP_TEST_VPC_ID": "vpc-0123456789abcdef0",
    "IDP_TEST_PRIVATE_SUBNET_IDS": "subnet-0aaaaaaaaaaaaaaa1,subnet-0bbbbbbbbbbbbbbb2",
    "IDP_TEST_LAMBDA_SG_ID": "sg-0123456789abcdef0",
    "IDP_TEST_APIGW_VPCE_ID": "vpce-0123456789abcdef0",
}
SCHEDULE = {"CI_PIPELINE_SOURCE": "schedule"}


# ---------------------------------------------------------------------------
# GitLab side
# ---------------------------------------------------------------------------


def test_the_job_honours_the_vpc_run_on_a_terraform_schedule(itd):  # noqa: F811
    environ = {**SCHEDULE, "IDP_IAC": "terraform", "IDP_TERRAFORM_VPC": "true"}
    assert itd.terraform_vpc_requested(environ) is True


@pytest.mark.parametrize(
    "environ",
    [
        {"IDP_IAC": "terraform", "IDP_TERRAFORM_VPC": "true"},
        {
            "CI_PIPELINE_SOURCE": "merge_request_event",
            "IDP_IAC": "terraform",
            "IDP_TERRAFORM_VPC": "true",
        },
        {**SCHEDULE, "IDP_IAC": "terraform"},
        {**SCHEDULE, "IDP_IAC": "terraform", "IDP_TERRAFORM_VPC": "false"},
    ],
)
def test_nothing_else_is_a_vpc_run(itd, environ):  # noqa: F811
    assert itd.terraform_vpc_requested(environ) is False


@pytest.mark.parametrize("iac", ["", "cloudformation"])
def test_the_job_refuses_a_vpc_run_that_is_not_terraform(itd, iac):  # noqa: F811
    environ = {**SCHEDULE, "IDP_IAC": iac, "IDP_TERRAFORM_VPC": "true"}
    with pytest.raises(ValueError, match="IDP_IAC=terraform"):
        itd.terraform_vpc_requested(environ)


def test_the_job_refuses_a_vpc_run_with_the_parity_check(itd):  # noqa: F811
    environ = {
        **SCHEDULE,
        "IDP_IAC": "terraform",
        "IDP_TERRAFORM_VPC": "true",
        "IDP_TERRAFORM_PARITY": "true",
    }
    with pytest.raises(ValueError, match="IDP_TERRAFORM_PARITY"):
        itd.terraform_vpc_requested(environ)


def test_the_run_options_carry_the_vpc_run_to_the_build(
    itd,  # noqa: F811
    cbd,
    tmp_path,
):
    environ = {**SCHEDULE, "IDP_IAC": "terraform", "IDP_TERRAFORM_VPC": "true"}
    path = tmp_path / "ci_run_options.json"
    itd.write_ci_run_options(path=str(path), environ=environ)
    options = json.loads(path.read_text(encoding="utf-8"))
    assert options["terraform_vpc"] is True
    assert cbd.terraform_vpc_mode({}, options) is True
    itd.write_ci_run_options(path=str(path), environ={**SCHEDULE})
    options = json.loads(path.read_text(encoding="utf-8"))
    assert options["terraform_vpc"] is False
    assert cbd.terraform_vpc_mode({}, options) is False


def test_the_job_exits_on_a_refused_vpc_run(itd, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.chdir(tmp_path)
    for key, value in {
        **SCHEDULE,
        "IDP_TERRAFORM_VPC": "true",
        "AWS_DEFAULT_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("IDP_IAC", raising=False)
    packaged = []
    monkeypatch.setattr(itd, "create_deployment_package", lambda: packaged.append(1))
    monkeypatch.setattr(itd, "upload_to_s3", lambda bucket: "version-1")
    monkeypatch.setattr(itd, "monitor_pipeline", lambda *a, **k: True)
    with pytest.raises(SystemExit) as excinfo:
        itd.main()
    assert excinfo.value.code == 1
    assert not packaged


# ---------------------------------------------------------------------------
# Build side
# ---------------------------------------------------------------------------


def test_the_environment_beats_the_run_options(cbd):
    assert cbd.terraform_vpc_mode({"IDP_TERRAFORM_VPC": "true"}, {}) is True
    assert (
        cbd.terraform_vpc_mode({"IDP_TERRAFORM_VPC": "false"}, {"terraform_vpc": True})
        is False
    )
    assert cbd.terraform_vpc_mode({}, {"terraform_vpc": "true"}) is False


def test_a_run_that_is_not_a_vpc_run_adds_nothing(cbd):
    assert cbd.terraform_vpc_param_pairs({"IDP_IAC": "terraform", **TEST_VPC_ENV}) == []


def test_a_vpc_run_without_the_test_vpc_is_refused(cbd):
    environ = {"IDP_IAC": "terraform", "IDP_TERRAFORM_VPC": "true"}
    with pytest.raises(ValueError, match="test VPC is not configured"):
        cbd.terraform_vpc_param_pairs(environ)


def test_a_vpc_run_that_is_not_terraform_is_refused(cbd):
    environ = {"IDP_TERRAFORM_VPC": "true", **TEST_VPC_ENV}
    with pytest.raises(ValueError, match="IDP_IAC=terraform"):
        cbd.terraform_vpc_param_pairs(environ)


def test_a_vpc_run_with_the_parity_check_is_refused(cbd):
    environ = {
        "IDP_IAC": "terraform",
        "IDP_TERRAFORM_VPC": "true",
        "IDP_TERRAFORM_PARITY": "true",
        **TEST_VPC_ENV,
    }
    with pytest.raises(ValueError, match="IDP_TERRAFORM_PARITY"):
        cbd.terraform_vpc_param_pairs(environ)


def test_the_vpc_pairs_are_the_probes_vpc_parameters(cbd):
    environ = {"IDP_IAC": "terraform", "IDP_TERRAFORM_VPC": "true", **TEST_VPC_ENV}
    pairs = cbd.terraform_vpc_param_pairs(environ)
    assert pairs == [f"{k}={v}" for k, v in cbd._test_vpc_params(environ).items()]
    assert "DeployInVPC=true" in pairs


def test_the_deploy_command_carries_the_vpc_parameters_intact(
    cbd,
    faked_deploy,  # noqa: F811
    run,  # noqa: F811
    monkeypatch,
):
    for key, value in {
        "IDP_IAC": "terraform",
        "IDP_TERRAFORM_VPC": "true",
        **TEST_VPC_ENV,
    }.items():
        monkeypatch.setenv(key, value)
    result = cbd._deploy_primary_terraform_attempt("stk", "a@example.test", run)
    assert result["success"] is True
    (command,) = [c for c in faked_deploy["commands"] if c.startswith("idp-cli deploy")]
    parsed = assert_deploy_command_is_well_formed(command)
    subnets = TEST_VPC_ENV["IDP_TEST_PRIVATE_SUBNET_IDS"]
    assert parsed == {
        "PermissionsBoundaryArn": BOUNDARY_ARN,
        "MalwareScanning": "CREATE_PLAN",
        "EnableMCP": "true",
        "DeployInVPC": "true",
        "VpcId": TEST_VPC_ENV["IDP_TEST_VPC_ID"],
        "PrivateSubnetIds": subnets,
        "LambdaSubnetIds": subnets,
        "LambdaSecurityGroupId": TEST_VPC_ENV["IDP_TEST_LAMBDA_SG_ID"],
        "ApiGatewayVpcEndpointId": TEST_VPC_ENV["IDP_TEST_APIGW_VPCE_ID"],
    }
    _, deploy, _ = _cli_authorities()
    params = deploy.make_context("deploy", shlex.split(command)[2:]).params
    assert params["terraform"] is True
    # The root configuration is staged with the same pairs the deploy carries.
    ((_, staged_pairs),) = faked_deploy["staged"]
    assert "DeployInVPC=true" in staged_pairs


def test_a_vpc_run_is_labelled_as_one(cbd):
    environ = {"IDP_IAC": "terraform", "IDP_TERRAFORM_VPC": "true"}
    assert cbd.label_ci_run("body", environ).startswith(
        f"🧱 {cbd.TERRAFORM_VPC_RUN_LABEL}"
    )
    assert cbd.failure_subject("stk", environ) == (
        "IDP CI failure (weekly terraform vpc run): stk"
    )
    assert cbd.label_ci_run("body", {"IDP_IAC": "terraform"}).startswith(
        f"🧱 {cbd.TERRAFORM_RUN_LABEL}"
    )


@pytest.mark.parametrize(
    "environ, options, refusal",
    [
        # The test VPC is not configured.
        (
            {"IDP_IAC": "terraform", "IDP_TERRAFORM_VPC": "true"},
            {},
            "test VPC is not configured",
        ),
        # A CloudFormation build carrying the flag.
        ({"IDP_TERRAFORM_VPC": "true", **TEST_VPC_ENV}, {}, "IDP_IAC=terraform"),
        # The schedule's choices, carried in the run options only: the parity
        # choice reaches the environment late in main(), so a check made before
        # it would pass this combination.
        (
            TEST_VPC_ENV,
            {"iac": "terraform", "terraform_vpc": True, "terraform_parity": True},
            "IDP_TERRAFORM_PARITY",
        ),
    ],
)
def test_main_refuses_a_vpc_run_before_any_sweep_or_deploy(
    cbd, monkeypatch, environ, options, refusal
):
    # Set, not deleted: main() writes these to os.environ itself, and monkeypatch
    # restores only a variable it recorded, which delenv does not do for an absent
    # one. An empty value reads as unset everywhere.
    for key in (
        "IDP_IAC",
        "IDP_TERRAFORM_VPC",
        "IDP_TERRAFORM_PARITY",
        "IDP_RUN_PROBES",
        "IDP_RUN_TERRAFORM_MIGRATION",
        "IDP_RUN_TERRAFORM_UPGRADE",
        *TEST_VPC_ENV,
    ):
        monkeypatch.setenv(key, "")
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    # The primary region, so the only refusal left to reach is the one under test.
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setattr(cbd, "install_suite_transcript", lambda: None)
    monkeypatch.setattr(cbd, "read_ci_run_options", lambda *a: dict(options))
    published = []
    monkeypatch.setattr(cbd, "publish_summary_to_s3", published.append)
    monkeypatch.setattr(cbd, "send_failure_notification", lambda *a: None)

    class _RanPastTheRefusal(Exception):
        pass

    def _must_not_run(*_a, **_k):
        raise _RanPastTheRefusal()

    for name in [sweep.func for sweep in cbd.STARTUP_SWEEPS] + [
        "publish_artifacts",
        "publish_templates",
        "deploy_and_test_stack",
        "create_iam_resources",
    ]:
        monkeypatch.setattr(cbd, name, _must_not_run)
    # Local preparation, not a sweep or a deploy: allowed before the refusal.
    monkeypatch.setattr(cbd, "ensure_terraform_binary", lambda *a, **k: "terraform")
    monkeypatch.setattr(cbd, "install_terraform_stack_hook", lambda: None)

    class _Exit(Exception):
        pass

    def _exit(code):
        raise _Exit(code)

    monkeypatch.setattr(cbd.os, "_exit", _exit)
    with pytest.raises(_Exit) as excinfo:
        cbd.main()
    assert excinfo.value.args == (1,)
    assert published and refusal in published[0]


# ---------------------------------------------------------------------------
# GitLab status posts
# ---------------------------------------------------------------------------


def test_both_gitlab_status_posts_name_the_vpc_run():
    text = CI_CONFIG.read_text(encoding="utf-8")
    assert text.count('"🧱 WEEKLY TERRAFORM VPC RUN · " if vpc_run') == 2
    assert text.count('os.environ.get("IDP_TERRAFORM_VPC", "")') == 2
