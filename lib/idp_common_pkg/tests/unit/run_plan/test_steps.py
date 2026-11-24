# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The declared list of pipeline steps a run can start at or skip, and the display
steps a run does between them."""

import json
import re
from pathlib import Path

import pytest

from idp_common.run_plan import steps
from idp_common.run_plan.steps import (
    BDA,
    DISPLAY_STEPS,
    PIPELINE,
    RUN_STEPS,
    DisplayStep,
    StepNotInMode,
    UnknownStep,
    display_order,
    optional_step_ids,
    resolve_start,
    step,
    step_label,
    steps_for_mode,
)

pytestmark = pytest.mark.unit

_ASL = (
    Path(__file__).resolve().parents[5]
    / "patterns"
    / "unified"
    / "statemachine"
    / "workflow.asl.json"
)


#: The definition is not plain JSON: a few values are unquoted CloudFormation
#: placeholders (``"TimeoutSeconds": ${...}``), substituted when the stack deploys.
_UNQUOTED_PLACEHOLDER_RE = re.compile(r'":\s*\$\{[A-Za-z0-9_]+\}')


def _state_names() -> set[str]:
    """Every state name in the definition, including those inside Maps."""
    names: set[str] = set()

    def walk(states: dict) -> None:
        for name, state in states.items():
            names.add(name)
            for key in ("Iterator", "ItemProcessor"):
                if isinstance(state.get(key), dict):
                    walk(state[key].get("States", {}))
            for branch in state.get("Branches", []):
                walk(branch.get("States", {}))

    walk(json.loads(_UNQUOTED_PLACEHOLDER_RE.sub('": 1', _ASL.read_text()))["States"])
    return names


class TestTheList:
    def test_the_steps_are_in_pipeline_order(self):
        assert [run_step.id for run_step in RUN_STEPS] == [
            "beginning",
            "classification",
            "extraction",
            "assessment",
            "policy_rule_validation",
            "summarization",
            "evaluation",
        ]
        assert [steps.position(s) for s in steps.step_ids()] == list(range(7))

    def test_pipeline_mode_has_every_step_and_bda_mode_has_four(self):
        assert [s.id for s in steps_for_mode(PIPELINE)] == list(steps.step_ids())
        assert [s.id for s in steps_for_mode(BDA)] == [
            "beginning",
            "policy_rule_validation",
            "summarization",
            "evaluation",
        ]

    def test_the_required_and_the_optional_steps(self):
        assert [s.id for s in RUN_STEPS if s.required] == [
            "beginning",
            "classification",
            "extraction",
        ]
        assert optional_step_ids() == (
            "assessment",
            "policy_rule_validation",
            "summarization",
            "evaluation",
        )

    def test_every_step_names_states_for_each_of_its_modes(self):
        for run_step in RUN_STEPS:
            assert set(run_step.states) == set(run_step.modes), run_step.id
            for mode in run_step.modes:
                assert run_step.states[mode], (run_step.id, mode)

    def test_every_step_has_a_label(self):
        assert step("policy_rule_validation").label == "Policy Rule Validation"
        assert all(run_step.label for run_step in RUN_STEPS)

    def test_start_values_are_the_ids_the_aliases_and_resume(self):
        assert steps.start_values() == (*steps.step_ids(), "ocr", "resume")

    def test_an_unknown_step_is_named(self):
        with pytest.raises(UnknownStep, match="'ocrr'"):
            step("ocrr")

    @pytest.mark.skipif(
        "CheckRunPlan" not in _state_names(),
        reason="the state machine definition has no CheckRunPlan state yet",
    )
    def test_every_state_a_step_names_exists_in_the_definition(self):
        defined = _state_names()

        for run_step in RUN_STEPS:
            for mode, names in run_step.states.items():
                missing = set(names) - defined
                assert not missing, (
                    f"step {run_step.id} ({mode}) names states the definition "
                    f"does not have: {sorted(missing)}"
                )


class TestResolveStart:
    @pytest.mark.parametrize("mode", [PIPELINE, BDA])
    def test_ocr_is_the_beginning_in_both_modes(self, mode):
        assert resolve_start("ocr", mode) == "beginning"
        assert resolve_start("beginning", mode) == "beginning"

    @pytest.mark.parametrize("start", ["classification", "extraction", "assessment"])
    def test_pipeline_mode_keeps_its_own_steps(self, start):
        assert resolve_start(start, PIPELINE) == start

    @pytest.mark.parametrize("start", ["classification", "extraction"])
    def test_in_bda_mode_classification_and_extraction_are_the_beginning(self, start):
        assert resolve_start(start, BDA) == "beginning"

    def test_assessment_does_not_exist_in_bda_mode(self):
        with pytest.raises(StepNotInMode, match="BDA mode"):
            resolve_start("assessment", BDA)

    @pytest.mark.parametrize(
        "start", ["policy_rule_validation", "summarization", "evaluation"]
    )
    @pytest.mark.parametrize("mode", [PIPELINE, BDA])
    def test_the_later_steps_exist_in_both_modes(self, start, mode):
        assert resolve_start(start, mode) == start

    @pytest.mark.parametrize("start", ["apply_review_changes", "rejected"])
    def test_an_internal_start_is_returned_as_it_is(self, start):
        assert resolve_start(start, BDA) == start

    @pytest.mark.parametrize("start", ["resume", "nope", ""])
    def test_resume_and_unknown_values_are_refused(self, start):
        with pytest.raises(UnknownStep):
            resolve_start(start, PIPELINE)


class TestDisplaySteps:
    """The steps a run does that it can neither start at nor skip."""

    def test_the_display_steps(self):
        assert DISPLAY_STEPS == (
            DisplayStep(
                "apply_review_changes",
                "Apply Review Changes",
                "policy_rule_validation",
                True,
            ),
            DisplayStep("review_routing", "Review Routing", "summarization", False),
        )
        assert steps.REVIEW_ROUTING == "review_routing"

    def test_a_display_step_is_not_a_step_a_run_starts_at_or_skips(self):
        for display in DISPLAY_STEPS:
            assert display.id not in steps.step_ids()
            assert display.id not in steps.start_values()
            assert display.id not in optional_step_ids()
            with pytest.raises(UnknownStep):
                step(display.id)

    def test_review_routing_is_not_an_internal_start(self):
        assert steps.REVIEW_ROUTING not in steps.INTERNAL_STARTS
        with pytest.raises(UnknownStep):
            resolve_start(steps.REVIEW_ROUTING, PIPELINE)

    @pytest.mark.parametrize("mode", [PIPELINE, BDA])
    def test_a_display_step_comes_before_a_step_both_modes_have(self, mode):
        mode_ids = [run_step.id for run_step in steps_for_mode(mode)]

        for display in DISPLAY_STEPS:
            assert display.before in mode_ids, (display.id, mode)

    def test_the_order_of_a_pipeline_run(self):
        assert display_order(PIPELINE) == (
            "beginning",
            "classification",
            "extraction",
            "assessment",
            "policy_rule_validation",
            "review_routing",
            "summarization",
            "evaluation",
        )

    def test_the_order_of_a_bda_run(self):
        assert display_order(BDA) == (
            "beginning",
            "policy_rule_validation",
            "review_routing",
            "summarization",
            "evaluation",
        )

    def test_apply_review_changes_is_listed_only_after_a_completed_review(self):
        assert display_order(PIPELINE, review_completed=True) == (
            "beginning",
            "classification",
            "extraction",
            "assessment",
            "apply_review_changes",
            "policy_rule_validation",
            "review_routing",
            "summarization",
            "evaluation",
        )
        assert display_order(BDA, review_completed=True) == (
            "beginning",
            "apply_review_changes",
            "policy_rule_validation",
            "review_routing",
            "summarization",
            "evaluation",
        )

    @pytest.mark.parametrize("mode", [PIPELINE, BDA])
    @pytest.mark.parametrize("review_completed", [False, True])
    def test_the_run_steps_keep_their_order(self, mode, review_completed):
        order = display_order(mode, review_completed=review_completed)
        display_ids = {display.id for display in DISPLAY_STEPS}

        assert [step_id for step_id in order if step_id not in display_ids] == [
            run_step.id for run_step in steps_for_mode(mode)
        ]
        assert len(set(order)) == len(order)

    def test_a_label_for_every_step_of_either_kind(self):
        assert step_label("policy_rule_validation") == "Policy Rule Validation"
        assert step_label("apply_review_changes") == "Apply Review Changes"
        assert step_label("review_routing") == "Review Routing"
        for run_step in RUN_STEPS:
            assert step_label(run_step.id) == run_step.label

    @pytest.mark.parametrize("step_id", ["ocr", "resume", "rejected", "nope", ""])
    def test_a_value_that_is_neither_has_no_label(self, step_id):
        with pytest.raises(UnknownStep):
            step_label(step_id)
