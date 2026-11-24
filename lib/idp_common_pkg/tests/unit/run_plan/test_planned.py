# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The steps a run will do, each with a state, and what a start clears and keeps."""

import json
import os
import subprocess  # nosec B404 - fixed argv, no shell
import sys

import pytest
from pydantic import ValidationError

from idp_common.run_plan import planned, steps
from idp_common.run_plan.models import RunKind
from idp_common.run_plan.planned import (
    PLANNED_NOT_ENABLED,
    PLANNED_RUN,
    PLANNED_SKIPPED,
    PlannedRun,
    PlannedSection,
    PlannedStep,
    StartDescription,
    describe_start,
    planned_run,
    planned_steps,
)
from idp_common.run_plan.steps import (
    BDA,
    PIPELINE,
    RunPlanError,
    StepNotInMode,
    UnknownStep,
)

pytestmark = pytest.mark.unit

_PIPELINE_ORDER = [
    "beginning",
    "classification",
    "extraction",
    "assessment",
    "policy_rule_validation",
    "review_routing",
    "summarization",
    "evaluation",
]
_BDA_ORDER = [
    "beginning",
    "policy_rule_validation",
    "review_routing",
    "summarization",
    "evaluation",
]
_ORDER = {PIPELINE: _PIPELINE_ORDER, BDA: _BDA_ORDER}

#: (start, mode) to the first step the run does. Every step before it is
#: skipped and every step from it on runs.
_FIRST_STEP = [
    ("beginning", PIPELINE, "beginning"),
    ("ocr", PIPELINE, "beginning"),
    ("classification", PIPELINE, "classification"),
    ("extraction", PIPELINE, "extraction"),
    ("assessment", PIPELINE, "assessment"),
    ("policy_rule_validation", PIPELINE, "policy_rule_validation"),
    ("summarization", PIPELINE, "summarization"),
    ("evaluation", PIPELINE, "evaluation"),
    ("beginning", BDA, "beginning"),
    ("ocr", BDA, "beginning"),
    # One call does these in BDA mode, so they are the beginning.
    ("classification", BDA, "beginning"),
    ("extraction", BDA, "beginning"),
    ("policy_rule_validation", BDA, "policy_rule_validation"),
    ("summarization", BDA, "summarization"),
    ("evaluation", BDA, "evaluation"),
]


def _states(planned_list):
    return {step.id: step.state for step in planned_list}


def _before_start(planned_list):
    return [step.id for step in planned_list if step.before_start]


class TestTheStateOfEveryStep:
    @pytest.mark.parametrize(("start", "mode", "first"), _FIRST_STEP)
    def test_the_steps_before_the_start_are_skipped_and_the_rest_run(
        self, start, mode, first
    ):
        order = _ORDER[mode]
        index = order.index(first)

        result = planned_steps(start, mode, kind=RunKind.REPROCESS)

        # A summarization or evaluation start enters at policy rule validation,
        # so Review Routing runs although it comes before the start.
        passed_through = (
            ["review_routing"] if first in ("summarization", "evaluation") else []
        )
        skipped = [s for s in order[:index] if s not in passed_through]
        assert [step.id for step in result] == order
        assert _states(result) == {
            **dict.fromkeys(skipped, PLANNED_SKIPPED),
            **dict.fromkeys(passed_through, PLANNED_RUN),
            **dict.fromkeys(order[index:], PLANNED_RUN),
        }
        # The outputs of the steps the run does not do before the start are kept
        # and read.
        assert _before_start(result) == skipped

    @pytest.mark.parametrize("mode", [PIPELINE, BDA])
    def test_every_step_carries_its_one_label(self, mode):
        for step in planned_steps("beginning", mode, kind=RunKind.REPROCESS):
            assert step.label == steps.step_label(step.id)

    def test_the_labels_of_a_pipeline_run(self):
        result = planned_steps("beginning", PIPELINE, kind=RunKind.REPROCESS)

        assert [step.label for step in result] == [
            "Beginning",
            "Classification",
            "Extraction",
            "Assessment",
            "Policy Rule Validation",
            "Review Routing",
            "Summarization",
            "Evaluation",
        ]

    def test_an_extraction_start_for_named_sections_runs_the_same_steps(self):
        whole = planned_steps("extraction", PIPELINE, kind=RunKind.SAVED_EDITS)
        named = planned_steps(
            "extraction", PIPELINE, kind=RunKind.SAVED_EDITS, has_sections=True
        )

        assert named == whole


class TestApplyReviewChanges:
    @pytest.mark.parametrize("mode", [PIPELINE, BDA])
    @pytest.mark.parametrize(
        "kind", [kind for kind in RunKind if kind != RunKind.REVIEW_COMPLETED]
    )
    def test_it_is_not_listed_for_a_run_that_follows_no_completed_review(
        self, mode, kind
    ):
        start = "rejected" if kind == RunKind.REVIEW_REJECTED else "beginning"

        result = planned_steps(start, mode, kind=kind)

        assert "apply_review_changes" not in _states(result)

    def test_a_completed_review_starts_at_it(self):
        result = planned_steps(
            "apply_review_changes", PIPELINE, kind=RunKind.REVIEW_COMPLETED
        )

        assert [(step.id, step.state) for step in result] == [
            ("beginning", PLANNED_SKIPPED),
            ("classification", PLANNED_SKIPPED),
            ("extraction", PLANNED_SKIPPED),
            ("assessment", PLANNED_SKIPPED),
            ("apply_review_changes", PLANNED_RUN),
            ("policy_rule_validation", PLANNED_RUN),
            ("review_routing", PLANNED_RUN),
            ("summarization", PLANNED_RUN),
            ("evaluation", PLANNED_RUN),
        ]
        assert _before_start(result) == [
            "beginning",
            "classification",
            "extraction",
            "assessment",
        ]

    def test_a_completed_review_in_bda_mode(self):
        result = planned_steps(
            "apply_review_changes", BDA, kind=RunKind.REVIEW_COMPLETED
        )

        assert [(step.id, step.state) for step in result] == [
            ("beginning", PLANNED_SKIPPED),
            ("apply_review_changes", PLANNED_RUN),
            ("policy_rule_validation", PLANNED_RUN),
            ("review_routing", PLANNED_RUN),
            ("summarization", PLANNED_RUN),
            ("evaluation", PLANNED_RUN),
        ]

    def test_a_completed_review_that_extracts_again_applies_the_changes_after(self):
        result = planned_steps(
            "extraction",
            PIPELINE,
            kind=RunKind.REVIEW_COMPLETED,
            has_sections=True,
        )

        assert [(step.id, step.state) for step in result] == [
            ("beginning", PLANNED_SKIPPED),
            ("classification", PLANNED_SKIPPED),
            ("extraction", PLANNED_RUN),
            ("assessment", PLANNED_RUN),
            ("apply_review_changes", PLANNED_RUN),
            ("policy_rule_validation", PLANNED_RUN),
            ("review_routing", PLANNED_RUN),
            ("summarization", PLANNED_RUN),
            ("evaluation", PLANNED_RUN),
        ]

    def test_its_label(self):
        result = planned_steps(
            "apply_review_changes", PIPELINE, kind=RunKind.REVIEW_COMPLETED
        )

        labels = {step.id: step.label for step in result}
        assert labels["apply_review_changes"] == "Apply Review Changes"

    @pytest.mark.parametrize("kind", [RunKind.REPROCESS, RunKind.SENT_BACK])
    def test_no_other_run_starts_at_it(self, kind):
        with pytest.raises(RunPlanError, match="apply_review_changes"):
            planned_steps("apply_review_changes", PIPELINE, kind=kind)


class TestSkips:
    def test_a_step_the_request_skips_is_skipped_and_not_before_the_start(self):
        result = planned_steps(
            "extraction",
            PIPELINE,
            kind=RunKind.REPROCESS,
            skip=["summarization", "assessment"],
        )

        assert _states(result) == {
            "beginning": PLANNED_SKIPPED,
            "classification": PLANNED_SKIPPED,
            "extraction": PLANNED_RUN,
            "assessment": PLANNED_SKIPPED,
            "policy_rule_validation": PLANNED_RUN,
            "review_routing": PLANNED_RUN,
            "summarization": PLANNED_SKIPPED,
            "evaluation": PLANNED_RUN,
        }
        assert _before_start(result) == ["beginning", "classification"]

    @pytest.mark.parametrize("mode", [PIPELINE, BDA])
    def test_a_summarization_start_is_forced_to_skip_policy_rule_validation(self, mode):
        """The run enters at policy rule validation with that stage skipped."""
        result = planned_steps("summarization", mode, kind=RunKind.REPROCESS)

        states = _states(result)
        assert states["policy_rule_validation"] == PLANNED_SKIPPED
        # Routing still runs: the state machine goes through it on the way.
        assert states["review_routing"] == PLANNED_RUN
        assert states["summarization"] == PLANNED_RUN
        assert states["evaluation"] == PLANNED_RUN

    @pytest.mark.parametrize("mode", [PIPELINE, BDA])
    def test_an_evaluation_start_is_forced_to_skip_the_two_stages_before_it(self, mode):
        result = planned_steps("evaluation", mode, kind=RunKind.REPROCESS)

        states = _states(result)
        assert states["policy_rule_validation"] == PLANNED_SKIPPED
        assert states["review_routing"] == PLANNED_RUN
        assert states["summarization"] == PLANNED_SKIPPED
        assert states["evaluation"] == PLANNED_RUN

    @pytest.mark.parametrize("start", ["summarization", "evaluation"])
    def test_review_routing_that_is_off_reads_not_enabled_for_a_late_start(self, start):
        result = planned_steps(
            start, PIPELINE, kind=RunKind.REPROCESS, enabled={"review_routing": False}
        )

        assert _states(result)["review_routing"] == PLANNED_NOT_ENABLED

    @pytest.mark.parametrize("start", ["summarization", "evaluation"])
    def test_a_late_start_matches_the_state_machine_entry(self, start):
        """The planned list and the execution input agree on what a late start
        skips: the entry is policy rule validation and its forced skips are the
        skipped steps before the start."""
        from idp_common.run_plan.asl import entry_for

        entry = entry_for(start, PIPELINE)
        result = planned_steps(start, PIPELINE, kind=RunKind.REPROCESS)

        skipped_before_start = [
            step.id
            for step in result
            if step.state == PLANNED_SKIPPED and step.id in entry.forced_skips
        ]
        assert skipped_before_start == list(entry.forced_skips)

    def test_the_forced_skips_are_read_from_the_entry_of_the_start(self, monkeypatch):
        """A skip the entry forces on a step the run reaches is a skip."""
        from idp_common.run_plan.asl import ENTRY_POLICY_RULE_VALIDATION, Entry

        seen = []

        def entry_for(start, mode, has_sections=False):
            seen.append((start, mode, has_sections))
            return Entry(
                ENTRY_POLICY_RULE_VALIDATION, "policy_rule_validation", ("evaluation",)
            )

        monkeypatch.setattr(planned, "entry_for", entry_for)

        result = planned_steps(
            "policy_rule_validation", BDA, kind=RunKind.REPROCESS, has_sections=True
        )

        assert seen == [("policy_rule_validation", BDA, True)]
        assert _states(result) == {
            "beginning": PLANNED_SKIPPED,
            "policy_rule_validation": PLANNED_RUN,
            "review_routing": PLANNED_RUN,
            "summarization": PLANNED_RUN,
            "evaluation": PLANNED_SKIPPED,
        }
        assert _before_start(result) == ["beginning"]


class TestNotEnabled:
    def test_a_step_the_configuration_switches_off_is_not_enabled(self):
        result = planned_steps(
            "beginning",
            PIPELINE,
            kind=RunKind.REPROCESS,
            enabled={
                "assessment": True,
                "policy_rule_validation": False,
                "review_routing": False,
                "summarization": True,
                "evaluation": False,
            },
        )

        assert _states(result) == {
            "beginning": PLANNED_RUN,
            "classification": PLANNED_RUN,
            "extraction": PLANNED_RUN,
            "assessment": PLANNED_RUN,
            "policy_rule_validation": PLANNED_NOT_ENABLED,
            "review_routing": PLANNED_NOT_ENABLED,
            "summarization": PLANNED_RUN,
            "evaluation": PLANNED_NOT_ENABLED,
        }

    def test_a_step_the_answer_does_not_name_is_enabled(self):
        result = planned_steps(
            "beginning", BDA, kind=RunKind.REPROCESS, enabled={"evaluation": False}
        )

        assert _states(result) == {
            "beginning": PLANNED_RUN,
            "policy_rule_validation": PLANNED_RUN,
            "review_routing": PLANNED_RUN,
            "summarization": PLANNED_RUN,
            "evaluation": PLANNED_NOT_ENABLED,
        }

    def test_a_step_before_the_start_is_skipped_whatever_the_configuration(self):
        result = planned_steps(
            "policy_rule_validation",
            PIPELINE,
            kind=RunKind.REPROCESS,
            enabled={"assessment": False, "summarization": False},
        )

        states = _states(result)
        assert states["assessment"] == PLANNED_SKIPPED
        assert states["summarization"] == PLANNED_NOT_ENABLED

    def test_a_skipped_step_is_skipped_whatever_the_configuration(self):
        result = planned_steps(
            "beginning",
            PIPELINE,
            kind=RunKind.REPROCESS,
            skip=["evaluation"],
            enabled={"evaluation": False},
        )

        assert _states(result)["evaluation"] == PLANNED_SKIPPED

    def test_the_keys_of_the_enabled_answer_are_steps_of_the_list(self):
        """``step_enabled`` and the planned list name the same steps."""
        from idp_common.config.feature_flags import step_enabled

        assert set(step_enabled(None)) <= set(_PIPELINE_ORDER)


class TestARejectedRun:
    @pytest.mark.parametrize("mode", [PIPELINE, BDA])
    def test_it_does_no_step(self, mode):
        result = planned_steps("rejected", mode, kind=RunKind.REVIEW_REJECTED)

        assert [step.id for step in result] == _ORDER[mode]
        assert set(_states(result).values()) == {PLANNED_SKIPPED}

    def test_a_switched_off_step_is_still_skipped(self):
        result = planned_steps(
            "rejected",
            PIPELINE,
            kind=RunKind.REVIEW_REJECTED,
            enabled={"summarization": False},
        )

        assert _states(result)["summarization"] == PLANNED_SKIPPED


class TestAStartTheModeLacks:
    def test_assessment_in_bda_mode_runs_from_the_beginning(self):
        """As the run does: the entry of such a start is the beginning."""
        result = planned_steps("assessment", BDA, kind=RunKind.REPROCESS)

        assert _states(result) == dict.fromkeys(_BDA_ORDER, PLANNED_RUN)
        assert _before_start(result) == []

    def test_a_value_that_is_not_a_step_runs_from_the_beginning(self):
        result = planned_steps("nope", PIPELINE, kind=RunKind.REPROCESS)

        assert _states(result) == dict.fromkeys(_PIPELINE_ORDER, PLANNED_RUN)


class TestPlannedRun:
    def test_it_carries_the_mode_the_start_and_the_steps(self):
        result = planned_run(
            "policy_rule_validation",
            PIPELINE,
            kind=RunKind.SAVED_EDITS,
            skip=["evaluation"],
            enabled={"summarization": False},
        )

        assert isinstance(result, PlannedRun)
        assert (result.mode, result.start_step, result.sections) == (
            PIPELINE,
            "policy_rule_validation",
            [],
        )
        assert result.steps == planned_steps(
            "policy_rule_validation",
            PIPELINE,
            kind=RunKind.SAVED_EDITS,
            skip=["evaluation"],
            enabled={"summarization": False},
        )

    def test_the_start_is_the_step_after_aliases(self):
        assert (
            planned_run("ocr", PIPELINE, kind=RunKind.REPROCESS).start_step
            == "beginning"
        )
        assert (
            planned_run("classification", BDA, kind=RunKind.SENT_BACK).start_step
            == "beginning"
        )
        assert (
            planned_run("assessment", BDA, kind=RunKind.REPROCESS).start_step
            == "beginning"
        )

    def test_the_sections_extracted_again_carry_their_cause(self):
        result = planned_run(
            "extraction",
            PIPELINE,
            kind=RunKind.REVIEW_COMPLETED,
            sections=["2", "4"],
            causes={"2": "pages_changed", "4": "added"},
        )

        assert result.start_step == "extraction"
        assert result.sections == [
            PlannedSection(section_id="2", cause="pages_changed"),
            PlannedSection(section_id="4", cause="added"),
        ]
        assert _states(result.steps)["apply_review_changes"] == PLANNED_RUN

    def test_a_section_with_no_cause_given_has_no_result(self):
        result = planned_run(
            "extraction", PIPELINE, kind=RunKind.SAVED_EDITS, sections=["7"]
        )

        assert result.sections == [PlannedSection(section_id="7", cause="no_result")]

    @pytest.mark.parametrize(
        ("start", "mode"),
        [
            pytest.param("extraction", BDA, id="bda-runs-from-the-beginning"),
            pytest.param("assessment", PIPELINE, id="assessment-reads-no-list"),
        ],
    )
    def test_sections_are_listed_only_when_the_run_extracts_named_sections(
        self, start, mode
    ):
        """The same rule as the execution input: only that entry reads the list."""
        result = planned_run(start, mode, kind=RunKind.REPROCESS, sections=["1"])

        assert result.sections == []

    def test_the_wire_form_is_camel_case_without_the_internal_flag(self):
        result = planned_run(
            "extraction",
            PIPELINE,
            kind=RunKind.SAVED_EDITS,
            sections=["3"],
            enabled={"evaluation": False},
        )

        dumped = result.model_dump(by_alias=True)

        assert set(dumped) == {"mode", "startStep", "steps", "sections"}
        assert dumped["startStep"] == "extraction"
        assert dumped["sections"] == [{"sectionId": "3", "cause": "no_result"}]
        assert dumped["steps"][0] == {
            "id": "beginning",
            "label": "Beginning",
            "state": "skipped",
        }
        assert dumped["steps"][-1] == {
            "id": "evaluation",
            "label": "Evaluation",
            "state": "not_enabled",
        }
        assert json.loads(result.model_dump_json(by_alias=True)) == dumped

    def test_the_models_are_frozen(self):
        step = PlannedStep(id="evaluation", label="Evaluation", state=PLANNED_RUN)

        with pytest.raises(ValidationError):
            step.state = PLANNED_SKIPPED  # type: ignore[misc]

    def test_a_state_outside_the_three_is_refused(self):
        with pytest.raises(ValidationError):
            PlannedStep(id="evaluation", label="Evaluation", state="maybe")  # type: ignore[arg-type]

    def test_the_states(self):
        assert (PLANNED_RUN, PLANNED_SKIPPED, PLANNED_NOT_ENABLED) == (
            "run",
            "skipped",
            "not_enabled",
        )


class TestDescribeStart:
    """What a run clears and keeps is read off the planned steps."""

    def test_a_pipeline_run_from_extraction(self):
        description = describe_start("extraction", PIPELINE)

        assert isinstance(description, StartDescription)
        assert description.cleared == [
            "Extraction",
            "Assessment",
            "Policy Rule Validation",
            "Summarization",
            "Evaluation",
        ]
        assert description.kept == ["OCR", "Classification"]
        assert description.left_out == []

    @pytest.mark.parametrize("start", ["beginning", "ocr"])
    def test_a_run_from_the_beginning_keeps_nothing(self, start):
        cleared, kept, left_out = describe_start(start, PIPELINE)

        assert kept == [] and left_out == []
        assert cleared[0] == "OCR" and len(cleared) == len(
            steps.steps_for_mode(PIPELINE)
        )

    def test_the_last_step_clears_only_itself(self):
        cleared, kept, _ = describe_start("evaluation", PIPELINE)

        assert cleared == ["Evaluation"]
        assert len(kept) == len(steps.steps_for_mode(PIPELINE)) - 1

    @pytest.mark.parametrize(
        "start", ["beginning", "ocr", "classification", "extraction"]
    )
    def test_in_bda_mode_the_first_three_steps_are_one(self, start):
        cleared, kept, _ = describe_start(start, BDA)

        assert kept == []
        assert cleared == [
            "Bedrock Data Automation results",
            "Policy Rule Validation",
            "Summarization",
            "Evaluation",
        ]

    def test_a_bda_run_from_a_later_step_keeps_the_bda_results(self):
        cleared, kept, left_out = describe_start("summarization", BDA)

        assert cleared == ["Summarization", "Evaluation"]
        # Policy rule validation is skipped by the entry and comes before the
        # start: its output is kept.
        assert kept == ["Bedrock Data Automation results", "Policy Rule Validation"]
        assert left_out == []

    @pytest.mark.parametrize("mode", [PIPELINE, BDA])
    def test_every_step_of_a_mode_is_cleared_or_kept_and_in_order(self, mode):
        for run_step in steps.steps_for_mode(mode):
            cleared, kept, left_out = describe_start(run_step.id, mode)

            assert len(kept) == [s.id for s in steps.steps_for_mode(mode)].index(
                run_step.id
            )
            assert len(kept) + len(cleared) == len(steps.steps_for_mode(mode))
            assert not set(kept) & set(cleared)
            assert left_out == []

    @pytest.mark.parametrize("mode", [PIPELINE, BDA])
    def test_only_the_outputs_of_run_steps_are_named(self, mode):
        """Apply Review Changes and Review Routing produce no output a run keeps
        or clears by name."""
        cleared, kept, left_out = describe_start("policy_rule_validation", mode)

        named = set(cleared) | set(kept) | set(left_out)
        assert "Review Routing" not in named
        assert "Apply Review Changes" not in named

    def test_a_later_step_the_run_skips_is_left_out(self):
        description = describe_start(
            "extraction", PIPELINE, skip=["summarization", "assessment"]
        )

        assert description.cleared == [
            "Extraction",
            "Policy Rule Validation",
            "Evaluation",
        ]
        assert description.kept == ["OCR", "Classification"]
        assert description.left_out == ["Assessment", "Summarization"]

    def test_a_skipped_step_before_the_start_is_kept(self):
        description = describe_start("summarization", PIPELINE, skip=["assessment"])

        assert "Assessment" in description.kept
        assert description.left_out == []

    def test_a_step_the_mode_does_not_have_is_refused(self):
        with pytest.raises(StepNotInMode, match="BDA mode"):
            describe_start("assessment", BDA)

    @pytest.mark.parametrize(
        "start", ["resume", "nope", "apply_review_changes", "rejected"]
    )
    def test_a_value_that_is_not_a_step_a_run_starts_at_is_refused(self, start):
        with pytest.raises(UnknownStep):
            describe_start(start, PIPELINE)


_IMPORT_PROBE = r"""
import json
import sys

before = set(sys.modules)
import idp_common.run_plan.planned

added = set(sys.modules) - before
print(json.dumps(sorted(m for m in added if m.split(".")[0] == "idp_common")))
"""


def test_the_module_loads_the_run_plan_modules_only():
    """A planned list is built without loading the review code."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    run = subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, "-c", _IMPORT_PROBE],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    loaded = json.loads(run.stdout.strip().splitlines()[-1])

    assert loaded == [
        "idp_common",
        "idp_common.run_plan",
        "idp_common.run_plan.asl",
        "idp_common.run_plan.models",
        "idp_common.run_plan.planned",
        "idp_common.run_plan.restart",
        "idp_common.run_plan.steps",
    ]
