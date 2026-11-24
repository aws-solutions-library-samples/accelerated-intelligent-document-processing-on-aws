"""Unit tests for the progressive-summary callback in deploy_and_test_stack.

A run once showed "No deployment summary found" in GitLab even though it
finished: the ONLY S3 upload happened after the whole primary suite, and a slow
Step 12 pushed that past the ~45-min monitor handoff. deploy_and_test_stack now
takes a progress_cb it calls at each milestone (after the parallel pool drains,
and after each sequential step) so a current snapshot is in S3 well before the
handoff. These tests pin: the callback fires progressively, sequential-step
results accumulate between calls, and a callback exception never fails the
suite.
"""

import pytest

pytestmark = pytest.mark.unit


class _Completed:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _stub_suite(
    cbd, monkeypatch, *, parallel_ok=True, seq_ok=True, order=None, upload_rc=0
):
    """Stub IAM/deploy/step functions so deploy_and_test_stack runs fast, no AWS.

    Replaces PARALLEL_TEST_STEPS / SEQUENTIAL_TEST_STEPS with tiny fake steps.
    `order`, a list, receives each command and each step as it runs. `upload_rc`
    is the exit code of the configuration upload.
    """
    monkeypatch.setattr(
        cbd, "create_iam_resources", lambda s: ("role-arn", "boundary-arn")
    )

    def fake_run_command(cmd, check=True, timeout=cbd.DEFAULT_COMMAND_TIMEOUT):
        if order is not None:
            order.append(cmd)
        if "describe-stacks" in cmd:
            return _Completed(stdout="CREATE_COMPLETE")
        if "config-upload" in cmd:
            return _Completed(
                returncode=upload_rc,
                stderr="✗ Error: no such stack\n" if upload_rc else "",
            )
        return _Completed(stdout="")

    monkeypatch.setattr(cbd, "run_command", fake_run_command)
    # Avoid the fail-fast kill machinery touching anything real.
    monkeypatch.setattr(cbd, "_kill_running_commands", lambda: None)

    def mk(ok, err="boom"):
        def step(stack_name):
            if order is not None:
                order.append("a step")
            return {"success": ok} if ok else {"success": False, "error": err}

        return step

    monkeypatch.setattr(
        cbd,
        "PARALLEL_TEST_STEPS",
        [
            (mk(parallel_ok), "Step 3", "Default config", "d"),
            (mk(parallel_ok), "Step 4", "BDA mode", "b"),
        ],
    )
    monkeypatch.setattr(
        cbd,
        "SEQUENTIAL_TEST_STEPS",
        [
            (mk(seq_ok), "Step 11", "test-compare", "c"),
            (mk(seq_ok), "Step 12", "API RBAC", "r"),
        ],
    )


def test_progress_cb_fires_progressively(cbd, monkeypatch):
    _stub_suite(cbd, monkeypatch)
    snapshots = []
    # capture a shallow copy of statuses at each callback
    cbd_deploy = cbd.deploy_and_test_stack

    def cb(step_results):
        snapshots.append(
            {k: v["status"] for k, v in step_results.items()}
        )

    result = cbd_deploy("idp-x", "a@b.com", "https://t", progress_cb=cb)

    assert result["success"] is True
    # At least: once after parallel pool + once per sequential step (2) = 3.
    assert len(snapshots) >= 3
    # First snapshot (post-parallel): parallel steps passed, sequential still
    # cancelled (not yet run).
    first = snapshots[0]
    assert first["Step 3: Default config"] == "passed"
    assert first["Step 4: BDA mode"] == "passed"
    assert first["Step 11: test-compare"] == "cancelled"
    assert first["Step 12: API RBAC"] == "cancelled"
    # Last snapshot: everything passed.
    last = snapshots[-1]
    assert last["Step 11: test-compare"] == "passed"
    assert last["Step 12: API RBAC"] == "passed"


def test_progress_cb_fires_on_sequential_failure(cbd, monkeypatch):
    _stub_suite(cbd, monkeypatch, seq_ok=False)
    snapshots = []

    def cb(step_results):
        snapshots.append({k: v["status"] for k, v in step_results.items()})

    result = cbd.deploy_and_test_stack("idp-x", "a@b.com", "https://t", progress_cb=cb)

    assert result["success"] is False
    assert result["failure_type"] == "test"
    # The failing sequential step's status is captured in the final snapshot.
    assert snapshots[-1]["Step 11: test-compare"] == "failed"


def test_progress_cb_fires_after_parallel_failure(cbd, monkeypatch):
    _stub_suite(cbd, monkeypatch, parallel_ok=False)
    snapshots = []

    def cb(step_results):
        snapshots.append({k: v["status"] for k, v in step_results.items()})

    result = cbd.deploy_and_test_stack("idp-x", "a@b.com", "https://t", progress_cb=cb)

    assert result["success"] is False
    # Even on fail-fast, the post-parallel snapshot was published.
    assert len(snapshots) >= 1
    statuses = snapshots[-1]
    assert "failed" in statuses.values()


def test_progress_cb_exception_does_not_fail_suite(cbd, monkeypatch):
    _stub_suite(cbd, monkeypatch)

    def boom(step_results):
        raise RuntimeError("s3 down")

    # A raising callback must be swallowed — the suite still passes.
    result = cbd.deploy_and_test_stack("idp-x", "a@b.com", "https://t", progress_cb=boom)
    assert result["success"] is True


def test_no_progress_cb_is_fine(cbd, monkeypatch):
    _stub_suite(cbd, monkeypatch)
    # Omitting the callback (default None) must work unchanged.
    result = cbd.deploy_and_test_stack("idp-x", "a@b.com", "https://t")
    assert result["success"] is True


# --------------------------------------------------------------------------- #
# Review Routing is switched off before the first step. A new stack has it on,
# and a document that needs review would then wait for a reviewer the suite
# does not have.
# --------------------------------------------------------------------------- #
SWITCH_OFF_COMMAND = (
    "idp-cli config-upload --stack-name idp-x "
    "--config-file scripts/sdlc/config/review-routing-off.yaml "
    "--config-version default"
)


def test_review_routing_is_switched_off_before_the_first_step(cbd, monkeypatch):
    order = []
    _stub_suite(cbd, monkeypatch, order=order)

    result = cbd.deploy_and_test_stack("idp-x", "a@b.com", "https://t")

    assert result["success"] is True
    assert order.count(SWITCH_OFF_COMMAND) == 1, order
    assert order.count("a step") == 4, order
    switched = order.index(SWITCH_OFF_COMMAND)
    # After the stack is up, and ahead of every step.
    assert any("describe-stacks" in entry for entry in order[:switched]), order
    assert "a step" not in order[:switched], order


def test_a_failed_switch_off_stops_before_the_steps(cbd, monkeypatch):
    order = []
    _stub_suite(cbd, monkeypatch, order=order, upload_rc=1)
    snapshots = []

    result = cbd.deploy_and_test_stack(
        "idp-x", "a@b.com", "https://t", progress_cb=snapshots.append
    )

    error = (
        "Could not switch Review Routing off in the default profile: "
        "the configuration upload exited 1: ✗ Error: no such stack"
    )
    assert result == {
        "stack_name": "idp-x",
        "success": False,
        "failure_type": "deploy",
        "error": error,
        "convergence_failed": False,
        "parity_failed": False,
        # A failed row of its own, which the summary reads.
        "step_results": {
            cbd.REVIEW_ROUTING_OFF_ROW: {"status": "failed", "error": error}
        },
    }
    assert SWITCH_OFF_COMMAND in order
    assert "a step" not in order, order
    assert snapshots == []


# --------------------------------------------------------------------------- #
# Primary-suite deploy retry on the transient CloudWatch Logs create race
# (mirrors the probe retry; the primary suite previously had none).
# --------------------------------------------------------------------------- #
def _cwl_race_events():
    # Shape _is_transient_logs_race matches: a CREATE_FAILED on an
    # AWS::Logs::LogGroup whose reason contains "does not exist".
    return [
        {
            "resource_type": "AWS::Logs::LogGroup",
            "logical_id": "SomeFunctionLogGroup",
            "status": "CREATE_FAILED",
            "reason": "The specified log group does not exist.",
        }
    ]


def test_primary_deploy_retries_transient_logs_race(cbd, monkeypatch):
    _stub_suite(cbd, monkeypatch)  # tests all pass once deploy succeeds
    calls = {"deploy": 0, "cleanup": []}
    monkeypatch.setattr(
        cbd, "cleanup_stack", lambda r: calls["cleanup"].append(r["stack_name"])
    )
    monkeypatch.setattr(cbd, "_capture_cf_events", lambda r, *n: None)

    # First deploy attempt hits the CWL race; second succeeds.
    def fake_attempt(stack_name, admin_email, template_url):
        calls["deploy"] += 1
        if calls["deploy"] == 1:
            return {
                "stack_name": stack_name,
                "success": False,
                "failure_type": "deploy",
                "error": "Stack deployment failed: log group does not exist",
                "cf_events": _cwl_race_events(),
            }
        return {"stack_name": stack_name, "success": True}

    monkeypatch.setattr(cbd, "_deploy_primary_stack_attempt", fake_attempt)

    result = cbd.deploy_and_test_stack("idp-x", "a@b.com", "https://t")
    assert result["success"] is True
    assert calls["deploy"] == 2  # retried exactly once
    assert calls["cleanup"] == ["idp-x"]  # rolled-back stack torn down before retry


def test_primary_deploy_no_retry_on_non_transient_failure(cbd, monkeypatch):
    _stub_suite(cbd, monkeypatch)
    calls = {"deploy": 0}
    monkeypatch.setattr(cbd, "cleanup_stack", lambda r: None)

    # A genuine (non-race) deploy failure must NOT be retried.
    def fake_attempt(stack_name, admin_email, template_url):
        calls["deploy"] += 1
        return {
            "stack_name": stack_name,
            "success": False,
            "failure_type": "deploy",
            "error": "CREATE_FAILED: IAM role policy invalid",
            "cf_events": [
                {
                    "resource_type": "AWS::IAM::Role",
                    "status": "CREATE_FAILED",
                    "reason": "MalformedPolicyDocument",
                }
            ],
        }

    monkeypatch.setattr(cbd, "_deploy_primary_stack_attempt", fake_attempt)

    result = cbd.deploy_and_test_stack("idp-x", "a@b.com", "https://t")
    assert result["success"] is False
    assert calls["deploy"] == 1  # no retry
    assert result["failure_type"] == "deploy"
