# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Terraform run's re-plan after apply (issue #2007).

A Terraform nightly re-plans its deployment straight after the apply, with
`terraform plan -detailed-exitcode`, and the plan must be empty: exit 0 passes,
exit 2 fails the run naming the changed addresses (a bounded list), and exit 1 is
an error reported as such rather than as drift. No change is accepted as a known
defect. Pinned here, offline, with a fake Terraform binary: the verdicts, the step
row and the run's result, and that the saved plan's values never reach the build
log.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

STAGE = "module.idp.module.apiresolverstack.awscc_apigateway_stage.http_api_stage"
LOG_GROUP_ARN = "arn:aws:logs:us-east-1:123456789012:log-group:stk-HttpApiAccessLogs"
#: A value only the saved plan carries; it must never be printed.
PLANNED_VALUE = "planned-value-that-must-stay-out-of-the-build-log"


def _update(address, before, after, after_unknown=None, actions=("update",)):
    return {
        "address": address,
        "change": {
            "actions": list(actions),
            "before": before,
            "after": after,
            "after_unknown": after_unknown or {},
        },
    }


def _stage_change(before_arn=LOG_GROUP_ARN, after_arn=None, **extra_after):
    after_arn = f"{LOG_GROUP_ARN}:*" if after_arn is None else after_arn
    before = {
        "stage_name": "prod",
        "access_log_setting": {"destination_arn": before_arn, "format": "{}"},
    }
    after = {
        "stage_name": "prod",
        "access_log_setting": {"destination_arn": after_arn, "format": "{}"},
        **extra_after,
    }
    return _update(STAGE, before, after)


def _plan(*changes, outputs=None):
    return json.dumps(
        {"resource_changes": list(changes), "output_changes": outputs or {}}
    )


# --------------------------------------------------------------- the verdicts


def test_exit_0_converges(cbd):
    verdict = cbd.judge_terraform_replan(0, None)
    assert verdict["status"] == "converged"
    assert verdict["changes"] == []


@pytest.mark.parametrize("returncode", [1, -1, 127])
def test_any_other_exit_is_an_error_not_drift(cbd, returncode):
    verdict = cbd.judge_terraform_replan(
        returncode,
        _plan(_update("aws_s3_bucket.b", {"a": 1}, {"a": 2})),
        "Error: reading the state lock",
    )
    assert verdict["status"] == "error"
    assert f"exited {returncode}" in verdict["detail"]
    assert "unknown" in verdict["detail"]
    assert "not empty" not in verdict["detail"]
    assert "Error: reading the state lock" in verdict["detail"]


def test_exit_2_is_drift_naming_the_address_and_the_changed_path(cbd):
    change = _update(
        "module.idp.awscc_lambda_function.f",
        {"timeout": 30, "memory_size": 128},
        {"timeout": 60, "memory_size": 128},
    )
    verdict = cbd.judge_terraform_replan(2, _plan(change))
    assert verdict["status"] == "drift"
    assert verdict["changes"] == [
        "module.idp.awscc_lambda_function.f (update): timeout"
    ]
    assert "module.idp.awscc_lambda_function.f" in verdict["detail"]


def test_a_replacement_and_a_create_are_drift(cbd):
    verdict = cbd.judge_terraform_replan(
        2,
        _plan(
            _update("a.x", {"k": 1}, {"k": 2}, actions=("delete", "create")),
            _update("b.y", None, {"k": 1}, actions=("create",)),
            _update("c.z", {"k": 1}, {"k": 1}, actions=("no-op",)),
        ),
    )
    assert verdict["status"] == "drift"
    assert verdict["changes"] == ["a.x (delete/create)", "b.y (create)"]


def test_an_output_only_change_is_drift(cbd):
    verdict = cbd.judge_terraform_replan(
        2, _plan(outputs={"web_url": {"actions": ["update"]}})
    )
    assert verdict["status"] == "drift"
    assert verdict["changes"] == ["output.web_url (update)"]


def test_exit_2_with_an_unreadable_plan_is_an_error(cbd):
    verdict = cbd.judge_terraform_replan(2, None)
    assert verdict["status"] == "error"
    assert "could not be read" in verdict["detail"]


def test_exit_2_with_nothing_in_the_plan_is_still_drift(cbd):
    assert cbd.judge_terraform_replan(2, _plan())["status"] == "drift"


def test_the_failure_detail_is_bounded(cbd):
    changes = [
        _update(f"module.idp.aws_s3_bucket.b{n:03d}", {"k": 1}, {"k": 2})
        for n in range(100)
    ]
    verdict = cbd.judge_terraform_replan(2, _plan(*changes))
    assert len(verdict["changes"]) == 100
    listed = cbd.TERRAFORM_REPLAN_LISTED_CHANGES
    assert f"b{listed - 1:03d}" in verdict["detail"]
    assert f"b{listed:03d}" not in verdict["detail"]
    assert f"and {100 - listed} more" in verdict["detail"]
    assert len(verdict["detail"]) < 4000


def test_an_unknown_after_value_counts_as_a_change(cbd):
    change = _update(
        "module.idp.awscc_iam_role.r",
        # Both null: only after_unknown says the value will change.
        {"arn": None, "description": "d"},
        {"arn": None, "description": "d"},
        after_unknown={"arn": True},
    )
    verdict = cbd.judge_terraform_replan(2, _plan(change))
    assert verdict["changes"] == ["module.idp.awscc_iam_role.r (update): arn"]


def test_values_never_reach_the_detail(cbd):
    change = _update(
        "module.idp.aws_ssm_parameter.p", {"value": "old"}, {"value": PLANNED_VALUE}
    )
    verdict = cbd.judge_terraform_replan(2, _plan(change))
    assert verdict["status"] == "drift"
    assert PLANNED_VALUE not in json.dumps(verdict)


# ------------------------------------------------- no change is accepted


def test_the_stage_destination_gaining_a_suffix_is_drift(cbd):
    """The shape #2006 produced, which the translator no longer writes, fails the
    run like any other change rather than being accepted."""
    verdict = cbd.judge_terraform_replan(2, _plan(_stage_change()))
    assert verdict["status"] == "drift"
    assert verdict["changes"] == [
        f"{STAGE} (update): access_log_setting.destination_arn"
    ]
    assert not hasattr(cbd, "TERRAFORM_REPLAN_EXEMPT_CHANGES")


# ------------------------------------- the real command, with a fake binary


FAKE_TERRAFORM = """#!/bin/sh
for arg in "$@"; do last="$arg"; done
case " $* " in
  *" plan "*)
    echo "$*" >> "$FAKE_TF_CALLS"
    echo "Terraform will perform the following actions: planned output"
    touch "$FAKE_TF_DIR/replan.tfplan"
    exit "$FAKE_TF_PLAN_RC"
    ;;
  *" show "*)
    echo "$*" >> "$FAKE_TF_CALLS"
    cat "$FAKE_TF_SHOW"
    exit "${FAKE_TF_SHOW_RC:-0}"
    ;;
esac
exit 99
"""


@pytest.fixture
def fake_terraform(tmp_path, monkeypatch, cbd):
    binary = tmp_path / "bin" / "terraform"
    binary.parent.mkdir()
    binary.write_text(FAKE_TERRAFORM)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    directory = tmp_path / "run"
    directory.mkdir()
    calls = tmp_path / "calls"
    show = tmp_path / "show.json"
    monkeypatch.setenv("FAKE_TF_DIR", str(directory))
    monkeypatch.setenv("FAKE_TF_CALLS", str(calls))
    monkeypatch.setenv("FAKE_TF_SHOW", str(show))
    run = cbd.TerraformRun(
        module_url="https://example.test/m.zip",
        directory=str(directory),
        binary=str(binary),
    )

    def configure(rc, plan_text=""):
        monkeypatch.setenv("FAKE_TF_PLAN_RC", str(rc))
        show.write_text(plan_text)

    return run, configure, calls


@pytest.mark.parametrize(
    ("rc", "status"), [(0, "converged"), (1, "error"), (2, "drift")]
)
def test_the_command_judges_the_exit_status(cbd, fake_terraform, rc, status, capsys):
    run, configure, calls = fake_terraform
    secret = _update(
        "module.idp.aws_ssm_parameter.p", {"value": "old"}, {"value": PLANNED_VALUE}
    )
    configure(rc, _plan(secret))
    verdict = cbd.check_terraform_convergence(run)
    assert verdict["status"] == status
    lines = calls.read_text().splitlines()
    assert "plan" in lines[0] and "-detailed-exitcode" in lines[0]
    assert ("show" in lines[-1]) is (rc == 2)
    # The saved plan holds every value in clear; it is read and removed.
    assert not os.path.exists(os.path.join(run.directory, "replan.tfplan"))
    log = Path(run.directory, cbd.TERRAFORM_REPLAN_LOG).read_text()
    assert "planned output" in log
    printed = capsys.readouterr()
    assert PLANNED_VALUE not in printed.out + printed.err
    if rc == 2:
        assert "module.idp.aws_ssm_parameter.p (update): value" in printed.out
    # The replan log is part of the failure evidence.
    assert cbd.TERRAFORM_REPLAN_LOG in cbd.terraform_log_tail(run.directory)


# ------------------------------------------------- the run's result and table


@pytest.fixture
def suite(cbd, monkeypatch, tmp_path):
    ran = []

    def _step(stack):
        ran.append(stack)
        return {"success": True}

    steps = [(_step, "Step 3", "x", "d")]
    monkeypatch.setattr(cbd, "PARALLEL_TEST_STEPS", steps)
    monkeypatch.setattr(cbd, "SEQUENTIAL_TEST_STEPS", [])
    monkeypatch.setattr(cbd, "ALL_TEST_STEPS", steps)
    monkeypatch.setattr(cbd, "terraform_suite_time_shortfall", lambda started: None)
    run = cbd.TerraformRun(
        module_url="https://example.test/m.zip",
        directory=str(tmp_path / "run"),
        binary="terraform",
    )

    def deploy(convergence):
        monkeypatch.setattr(
            cbd,
            "_deploy_primary_terraform_attempt",
            lambda *a: {
                "stack_name": "stk",
                "success": True,
                "iac": "terraform",
                "convergence": convergence,
            },
        )
        return cbd.deploy_and_test_stack(
            "stk", "a@example.test", "https://example.test/t.yaml", terraform=run
        )

    return deploy, ran


@pytest.mark.parametrize("status", ["drift", "error"])
def test_a_failed_replan_fails_the_run_after_the_suite(cbd, suite, status):
    deploy, ran = suite
    verdict = {
        "status": status,
        "detail": f"the {status} detail",
        "changes": [],
    }
    result = deploy(verdict)
    assert ran == ["stk"], "the suite still runs beside a failed re-plan"
    assert result["success"] is False and result["failure_type"] == "deploy"
    assert result["convergence_failed"] is True
    assert f"the {status} detail" in result["error"]
    row = result["step_results"][cbd.TERRAFORM_CONVERGENCE_STEP]
    assert row == {"status": "failed", "error": f"the {status} detail"}
    assert list(result["step_results"])[0] == cbd.TERRAFORM_CONVERGENCE_STEP
    summary = cbd.build_consolidated_summary("stk", result, [], True, {})
    assert "OVERALL: FAIL" in summary
    assert cbd.TERRAFORM_CONVERGENCE_STEP in summary


def test_a_converged_replan_passes(cbd, suite):
    deploy, ran = suite
    verdict = cbd.judge_terraform_replan(0, None)
    result = deploy(verdict)
    assert result["success"] is True
    row = result["step_results"][cbd.TERRAFORM_CONVERGENCE_STEP]
    assert row["status"] == "passed"
    summary = cbd.build_consolidated_summary("stk", result, [], True, {})
    assert "OVERALL: PASS" in summary


def test_a_cloudformation_run_has_no_replan_row(cbd, suite, monkeypatch):
    deploy, _ = suite
    result = deploy(None)
    assert cbd.TERRAFORM_CONVERGENCE_STEP not in result["step_results"]
    assert result["success"] is True


def test_a_failed_show_is_an_error_even_with_output(cbd, fake_terraform, monkeypatch):
    run, configure, _ = fake_terraform
    configure(2, _plan())
    monkeypatch.setenv("FAKE_TF_SHOW_RC", "1")
    verdict = cbd.check_terraform_convergence(run)
    assert verdict["status"] == "error"
    assert "could not be read" in verdict["detail"]


def test_the_replan_takes_no_state_lock(cbd, fake_terraform):
    """A plan killed by its timeout cannot remove an S3 lock file, which would
    then block the run's own destroy; the re-plan writes no state, so it takes
    no lock."""
    run, configure, calls = fake_terraform
    configure(0)
    cbd.check_terraform_convergence(run)
    assert "-lock=false" in calls.read_text().splitlines()[0].split()


def test_a_deferred_read_is_drift(cbd):
    read = {
        "address": "module.idp.data.aws_region.current",
        "change": {"actions": ["read"], "before": None, "after": {}},
    }
    verdict = cbd.judge_terraform_replan(2, _plan(read))
    assert verdict["status"] == "drift"
    assert verdict["changes"] == ["module.idp.data.aws_region.current (read)"]


def test_an_appended_list_item_is_a_changed_path(cbd):
    change = _update(
        "module.idp.awscc_iam_role.r",
        {"managed_policy_arns": ["a"]},
        {"managed_policy_arns": ["a", "b"]},
    )
    verdict = cbd.judge_terraform_replan(2, _plan(change))
    assert verdict["changes"] == [
        "module.idp.awscc_iam_role.r (update): managed_policy_arns"
    ]


def test_a_key_only_in_after_unknown_is_named(cbd):
    change = _update(
        "module.idp.awscc_iam_role.r", {"a": 1}, {"a": 1}, after_unknown={"arn": True}
    )
    verdict = cbd.judge_terraform_replan(2, _plan(change))
    assert verdict["changes"] == ["module.idp.awscc_iam_role.r (update): arn"]


def test_a_shortfall_keeps_the_replan_row(cbd, suite, monkeypatch):
    deploy, ran = suite
    monkeypatch.setattr(cbd, "terraform_suite_time_shortfall", lambda s: "no time")
    verdict = {
        "status": "drift",
        "detail": "the drift detail",
        "changes": ["x"],
    }
    result = deploy(verdict)
    assert ran == []
    assert result["success"] is False and result["convergence_failed"] is True
    row = result["step_results"][cbd.TERRAFORM_CONVERGENCE_STEP]
    assert row == {"status": "failed", "error": "the drift detail"}
    summary = cbd.build_consolidated_summary("stk", result, [], True, {})
    assert "the drift detail" in summary and "OVERALL: FAIL" in summary


def test_a_shortfall_without_a_replan_reports_only_the_refusal(cbd, suite, monkeypatch):
    deploy, _ = suite
    monkeypatch.setattr(cbd, "terraform_suite_time_shortfall", lambda s: "no time")
    result = deploy(None)
    assert result["convergence_failed"] is False
    assert result["step_results"] == {
        cbd.TERRAFORM_STEPS_REFUSED_ROW: {"status": "failed", "error": "no time"}
    }


def test_a_converged_run_refused_for_time_still_fails_the_summary(
    cbd, suite, monkeypatch
):
    deploy, ran = suite
    monkeypatch.setattr(cbd, "terraform_suite_time_shortfall", lambda s: "no time")
    verdict = cbd.judge_terraform_replan(0, None)
    result = deploy(verdict)
    assert ran == [] and result["success"] is False
    assert result["convergence_failed"] is False
    rows = result["step_results"]
    assert rows[cbd.TERRAFORM_CONVERGENCE_STEP]["status"] == "passed"
    assert rows[cbd.TERRAFORM_STEPS_REFUSED_ROW] == {
        "status": "failed",
        "error": "no time",
    }
    summary = cbd.build_consolidated_summary("stk", result, [], True, {})
    assert "OVERALL: FAIL" in summary and "no time" in summary


@pytest.mark.parametrize("failed", [True, False])
def test_the_failure_prompt_says_when_the_apply_succeeded(cbd, failed):
    prompt = cbd.terraform_failure_prompt(
        {"error": "e", "terraform_log_tail": "t", "convergence_failed": failed}, "stk"
    )
    assert ("does not converge" in prompt) is failed
    assert ("The apply itself succeeded" in prompt) is failed
    assert "Deployment name: stk" in prompt
