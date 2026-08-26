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
from pathlib import Path

import pytest

ASL_PATH = (
    Path(__file__).resolve().parents[1] / "statemachine" / "workflow.asl.json"
)


@pytest.fixture(scope="module")
def states():
    return json.loads(ASL_PATH.read_text())["States"]


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
    assert rec["Parameters"]["document.$"] == states["EvaluationStep"]["Parameters"]["document.$"]
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
