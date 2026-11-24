# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""From a run plan to the ``runPlan`` block of the execution input."""

import pytest

from idp_common.run_plan.asl import (
    ENTRY_IDS,
    SKIP_FLAGS,
    Entry,
    entry_for,
    to_execution_input,
)
from idp_common.run_plan.models import RunKind, RunPlan
from idp_common.run_plan.steps import BDA, PIPELINE, optional_step_ids

pytestmark = pytest.mark.unit

_ALL_KEYS = {
    "v",
    "kind",
    "startStep",
    "startAt",
    "skipAssessment",
    "skipPolicyRuleValidation",
    "skipSummarization",
    "skipEvaluation",
    "policyRuleValidationEnabled",
    "sections",
    "reviewed",
    "mode",
    "cycleId",
}

#: (start, mode) -> (entry id, forced skips), for every start in both modes.
_ENTRIES = {
    ("beginning", PIPELINE): ("beginning", ()),
    ("ocr", PIPELINE): ("beginning", ()),
    ("classification", PIPELINE): ("pipeline.classification", ()),
    ("extraction", PIPELINE): ("pipeline.extraction", ()),
    ("assessment", PIPELINE): ("pipeline.assessment", ()),
    ("policy_rule_validation", PIPELINE): ("policy_rule_validation", ()),
    ("summarization", PIPELINE): (
        "policy_rule_validation",
        ("policy_rule_validation",),
    ),
    ("evaluation", PIPELINE): (
        "policy_rule_validation",
        ("policy_rule_validation", "summarization"),
    ),
    ("apply_review_changes", PIPELINE): ("apply_review_changes", ()),
    ("rejected", PIPELINE): ("rejected", ()),
    ("beginning", BDA): ("beginning", ()),
    ("ocr", BDA): ("beginning", ()),
    ("classification", BDA): ("beginning", ()),
    ("extraction", BDA): ("beginning", ()),
    ("policy_rule_validation", BDA): ("policy_rule_validation", ()),
    ("summarization", BDA): ("policy_rule_validation", ("policy_rule_validation",)),
    ("evaluation", BDA): (
        "policy_rule_validation",
        ("policy_rule_validation", "summarization"),
    ),
    ("apply_review_changes", BDA): ("apply_review_changes", ()),
    ("rejected", BDA): ("rejected", ()),
}


def _block(mode=PIPELINE, enabled=True, **fields):
    fields.setdefault("kind", RunKind.REPROCESS)
    return to_execution_input(
        RunPlan(**fields), mode=mode, policy_rule_validation_enabled=enabled
    )


class TestEntryFor:
    @pytest.mark.parametrize(("start", "mode"), sorted(_ENTRIES))
    def test_every_start_in_both_modes(self, start, mode):
        entry = entry_for(start, mode)

        assert (entry.start_at, entry.forced_skips) == _ENTRIES[(start, mode)]
        assert entry.fell_back is False
        assert entry.start_at in ENTRY_IDS

    def test_extraction_with_sections_has_its_own_entry(self):
        assert entry_for("extraction", PIPELINE, has_sections=True) == Entry(
            "pipeline.extraction_sections", "extraction", ()
        )

    def test_sections_change_no_other_entry(self):
        assert entry_for("assessment", PIPELINE, has_sections=True).start_at == (
            "pipeline.assessment"
        )
        assert entry_for("extraction", BDA, has_sections=True).start_at == "beginning"

    def test_the_logical_step_survives_an_alias(self):
        assert entry_for("ocr", PIPELINE).start_step == "beginning"
        assert entry_for("classification", BDA).start_step == "beginning"
        assert entry_for("summarization", BDA).start_step == "summarization"

    def test_assessment_in_bda_mode_falls_back_to_the_beginning(self):
        assert entry_for("assessment", BDA) == Entry(
            "beginning", "beginning", (), fell_back=True
        )

    @pytest.mark.parametrize("start", ["resume", "nope", ""])
    def test_a_start_that_is_not_a_step_falls_back_to_the_beginning(self, start):
        assert entry_for(start, PIPELINE) == Entry(
            "beginning", "beginning", (), fell_back=True
        )

    def test_every_entry_id_is_reachable(self):
        reached = {entry for entry, _ in _ENTRIES.values()} | {
            entry_for("extraction", PIPELINE, has_sections=True).start_at
        }

        assert reached == set(ENTRY_IDS)


class TestToExecutionInput:
    @pytest.mark.parametrize(("start", "mode"), sorted(_ENTRIES))
    def test_every_output_has_all_keys(self, start, mode):
        kind = {
            "apply_review_changes": RunKind.REVIEW_COMPLETED,
            "rejected": RunKind.REVIEW_REJECTED,
        }.get(start, RunKind.REPROCESS)

        block = _block(mode=mode, kind=kind, start=start)

        assert set(block) == _ALL_KEYS
        assert block["startAt"] == _ENTRIES[(start, mode)][0]
        assert block["mode"] == mode

    def test_the_documented_example(self):
        assert _block(start="summarization") == {
            "v": 1,
            "kind": "reprocess",
            "startStep": "summarization",
            "startAt": "policy_rule_validation",
            "skipAssessment": False,
            "skipPolicyRuleValidation": True,
            "skipSummarization": False,
            "skipEvaluation": False,
            "policyRuleValidationEnabled": True,
            "sections": [],
            "reviewed": False,
            "mode": "pipeline",
            "cycleId": "",
        }

    def test_a_summarization_start_skips_policy_rule_validation(self):
        block = _block(start="summarization")

        assert block["skipPolicyRuleValidation"] is True
        assert block["skipSummarization"] is False

    def test_an_evaluation_start_skips_summarization_too(self):
        block = _block(start="evaluation")

        assert (block["skipPolicyRuleValidation"], block["skipSummarization"]) == (
            True,
            True,
        )
        assert block["skipEvaluation"] is False

    def test_a_user_skip_list_and_forced_skips_combine(self):
        block = _block(start="summarization", skip=["evaluation", "assessment"])

        assert {flag for flag in SKIP_FLAGS.values() if block[flag]} == {
            "skipAssessment",
            "skipPolicyRuleValidation",
            "skipEvaluation",
        }

    def test_no_skip_gives_four_false_flags(self):
        block = _block(start="beginning")

        assert [block[flag] for flag in SKIP_FLAGS.values()] == [False] * 4

    def test_every_optional_step_has_a_flag(self):
        assert set(SKIP_FLAGS) == set(optional_step_ids())

    def test_bda_with_classification_starts_at_the_beginning(self):
        block = _block(mode=BDA, start="classification")

        assert (block["startAt"], block["startStep"]) == ("beginning", "beginning")

    def test_bda_with_assessment_falls_back_to_the_beginning(self):
        block = _block(mode=BDA, start="assessment", skip=["evaluation"])

        assert (block["startAt"], block["startStep"]) == ("beginning", "beginning")
        # What the user asked to skip is still skipped.
        assert block["skipEvaluation"] is True

    def test_sections_travel_only_with_the_sections_entry(self):
        with_sections = _block(start="extraction", sections=["2", "5"])
        in_bda = _block(mode=BDA, start="extraction", sections=["2", "5"])

        assert with_sections["startAt"] == "pipeline.extraction_sections"
        assert with_sections["sections"] == ["2", "5"]
        assert in_bda["startAt"] == "beginning"
        assert in_bda["sections"] == []

    def test_policy_rule_validation_enabled_is_carried(self):
        assert (
            _block(start="beginning", enabled=False)["policyRuleValidationEnabled"]
            is False
        )
        assert (
            _block(start="beginning", enabled=True)["policyRuleValidationEnabled"]
            is True
        )

    def test_the_review_fields_are_carried(self):
        block = _block(
            kind=RunKind.REVIEW_COMPLETED,
            start="apply_review_changes",
            reviewed=True,
            cycle_id="01J0000000000000000000",
        )

        assert block["kind"] == "review_completed"
        assert block["reviewed"] is True
        assert block["cycleId"] == "01J0000000000000000000"

    def test_a_completed_review_that_extracts_named_sections(self):
        """The run after a review that regrouped: its sections are extracted again."""
        block = _block(
            kind=RunKind.REVIEW_COMPLETED,
            start="extraction",
            sections=["2", "5"],
            cycle_id="01J0000000000000000000",
        )

        assert block["kind"] == "review_completed"
        assert block["startAt"] == "pipeline.extraction_sections"
        assert block["startStep"] == "extraction"
        assert block["sections"] == ["2", "5"]
        # The new values are not ones the reviewer confirmed.
        assert block["reviewed"] is False
        assert block["cycleId"] == "01J0000000000000000000"

    def test_a_completed_review_that_extracts_every_section(self):
        block = _block(
            kind=RunKind.REVIEW_COMPLETED,
            start="extraction",
            cycle_id="01J0000000000000000000",
        )

        assert (block["startAt"], block["startStep"]) == (
            "pipeline.extraction",
            "extraction",
        )
        assert block["sections"] == []
        assert block["reviewed"] is False

    def test_a_completed_review_that_extracts_cannot_claim_to_be_reviewed(self):
        with pytest.raises(ValueError):
            RunPlan(
                kind=RunKind.REVIEW_COMPLETED,
                start="extraction",
                reviewed=True,
                cycle_id="01J0000000000000000000",
            )
