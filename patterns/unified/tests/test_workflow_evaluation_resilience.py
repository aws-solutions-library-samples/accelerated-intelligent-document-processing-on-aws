# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Structural assertions on the EvaluationStep failure path in workflow.asl.json.

These pin behaviour that is expressed in the state machine rather than in Python,
where no other test can see it. The invariants exist because of a live incident:
an evaluation Lambda that timed out deterministically was retried 8 times at 2.5x
backoff, so each affected document occupied a workflow-concurrency slot for ~5.2
hours and then hard-failed, discarding its already-completed OCR, extraction,
assessment and summarization output. A batch of such documents stopped the stack
accepting any new work.

Pure JSON parsing on purpose — no imports from the Lambda source, which builds AWS
clients at module scope.
"""

import json
import re
from pathlib import Path

import pytest

ASL_PATH = Path(__file__).resolve().parents[1] / "statemachine" / "workflow.asl.json"

# CloudFormation `DefinitionSubstitutions` placeholders. Most sit inside JSON
# strings ("arn:${Partition}:states:::…") and parse fine, but a numeric ASL field
# — `TimeoutSeconds` — needs its placeholder UNQUOTED to become an integer after
# substitution, which makes the raw file invalid JSON. SAM never parses the body
# (with `DefinitionUri` it sets `DefinitionS3Location`; CloudFormation performs
# the substitution at deploy time), so this only affects reading it here.
# Resolving them first means these tests inspect the shape that actually deploys.
_UNQUOTED_PLACEHOLDER_RE = re.compile(r":\s*\$\{[A-Za-z0-9_]+\}")


def load_asl() -> dict:
    """Parse the ASL with unquoted numeric substitutions resolved to a number."""
    raw = ASL_PATH.read_text()
    return json.loads(_UNQUOTED_PLACEHOLDER_RE.sub(": 1", raw))


@pytest.fixture(scope="module")
def states():
    return load_asl()["States"]


@pytest.mark.unit
def test_timeout_is_not_retried_like_a_transient_error(states):
    """A deterministic timeout must not share the 8-attempt transient policy."""
    retries = states["EvaluationStep"]["Retry"]
    timeout_policies = [
        r for r in retries if "Sandbox.Timedout" in r.get("ErrorEquals", [])
    ]
    assert timeout_policies, "Sandbox.Timedout must have its own retry policy"
    for policy in timeout_policies:
        assert policy["MaxAttempts"] <= 1, (
            "Retrying a deterministic evaluation timeout cannot succeed; it only "
            "converts a ~15-minute failure into a multi-hour one while holding a "
            "workflow-concurrency slot"
        )
        # And it must not have quietly re-absorbed the transient error classes.
        assert "ThrottlingException" not in policy["ErrorEquals"]


@pytest.mark.unit
def test_transient_errors_still_get_generous_retries(states):
    retries = states["EvaluationStep"]["Retry"]
    transient = [
        r for r in retries if "ThrottlingException" in r.get("ErrorEquals", [])
    ]
    assert transient, "transient Bedrock/Lambda faults must still be retried"
    assert transient[0]["MaxAttempts"] >= 5
    assert "Sandbox.Timedout" not in transient[0]["ErrorEquals"]


@pytest.mark.unit
def test_evaluation_failure_does_not_discard_the_document(states):
    """Evaluation is a measurement step and runs after all expensive work."""
    catch = states["EvaluationStep"].get("Catch")
    assert catch, "EvaluationStep must catch its errors"
    assert catch[0]["ErrorEquals"] == ["States.ALL"]
    assert catch[0]["Next"] == "RecordEvaluationFailure"


@pytest.mark.unit
def test_failure_recorder_records_then_continues_to_the_normal_tail(states):
    rec = states["RecordEvaluationFailure"]
    assert rec["Type"] == "Task"
    assert rec["Parameters"]["record_failure_only"] is True
    # Same document plumbing as EvaluationStep, so the handler sees the same shape.
    assert (
        rec["Parameters"]["document.$"]
        == states["EvaluationStep"]["Parameters"]["document.$"]
    )
    assert rec["Resource"] == states["EvaluationStep"]["Resource"]
    # Continues to the normal tail rather than a Fail state...
    assert rec["Next"] == "PostprocessingHook"
    # ...and its own failure must not take the document down either.
    assert rec["Catch"][0]["Next"] == "PostprocessingHook"


@pytest.mark.unit
def test_no_path_from_evaluation_leads_to_the_fail_state(states):
    """Nothing in the evaluation failure path may terminate the execution."""
    for name in ("EvaluationStep", "RecordEvaluationFailure"):
        targets = {c.get("Next") for c in states[name].get("Catch", [])}
        targets.add(states[name].get("Next"))
        assert "FailState" not in targets, f"{name} must not route to FailState"


# --------------------------------------------------------------------------- #
# The BDA callback wait must be bounded (#755)
# --------------------------------------------------------------------------- #
#
# BDA_InvokeDataAutomation is the ONLY waitForTaskToken state in the workflow, so
# it is the only place an execution can block on something outside Step
# Functions. The callback chain is: BDA job -> EventBridge (BDAEventRule) ->
# BDACompletionFunction -> reads the task token from S3 -> SendTaskSuccess. Every
# hop in that chain can break, and an unreturned token is unrecoverable: the
# execution emits neither ExecutionsFailed (so WorkflowErrorsAlarm cannot see it)
# nor ExecutionTime (so the slow-execution alarm cannot either), while holding a
# workflow-concurrency slot. Same class of damage as the retry-storm incident
# above, reached a different way.


@pytest.mark.unit
def test_every_wait_for_task_token_state_is_bounded():
    """No state may block on an external callback without a timeout."""
    raw = ASL_PATH.read_text()
    states = load_asl()["States"]
    unbounded = []
    for name, state in states.items():
        if "waitForTaskToken" not in str(state.get("Resource", "")):
            continue
        if not (
            "TimeoutSeconds" in state
            or "TimeoutSecondsPath" in state
            or "HeartbeatSeconds" in state
            or "HeartbeatSecondsPath" in state
        ):
            unbounded.append(name)
    assert not unbounded, (
        f"waitForTaskToken state(s) with no timeout: {unbounded}. A lost callback "
        "hangs the execution forever, invisible to every CloudWatch alarm."
    )
    # Guard the substitution too: a literal here would silently ignore the
    # BDACallbackTimeoutSeconds template parameter.
    assert "${BDACallbackTimeoutSeconds}" in raw, (
        "BDA_InvokeDataAutomation's TimeoutSeconds must come from the "
        "BDACallbackTimeoutSeconds substitution, not a hardcoded literal"
    )


@pytest.mark.unit
def test_a_timed_out_bda_callback_fails_the_execution(states):
    """States.Timeout must reach FailState, so the failure becomes alarmable.

    A bound is only useful if tripping it produces a FAILED execution: that is
    what emits ExecutionsFailed and lets the EventBridge rule fire the workflow
    tracker, which decrements the concurrency counter and marks the document
    FAILED. Catching States.ALL is what routes the timeout there.
    """
    bda = states["BDA_InvokeDataAutomation"]
    catches = bda.get("Catch") or []
    assert any(
        "States.ALL" in c.get("ErrorEquals", []) and c.get("Next") == "FailState"
        for c in catches
    ), "a timed-out BDA callback must route to FailState, not be swallowed"


@pytest.mark.unit
def test_a_timed_out_bda_callback_is_not_retried(states):
    """A timeout must not re-invoke BDA.

    The retry policy covers transient AWS faults only. Re-invoking on
    States.Timeout would start a second BDA job — paying twice and risking
    double-processing — for a callback that may still be in flight.
    """
    for policy in states["BDA_InvokeDataAutomation"].get("Retry") or []:
        errors = policy.get("ErrorEquals", [])
        assert "States.Timeout" not in errors
        assert "States.ALL" not in errors, (
            "States.ALL in a Retry policy would retry a timeout as if transient"
        )
