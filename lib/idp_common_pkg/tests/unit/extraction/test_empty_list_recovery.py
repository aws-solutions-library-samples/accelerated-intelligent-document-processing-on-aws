# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An agent that declines the table tool must not be allowed to discard the table.

Observed live (benchmark cell `core-tt-adv-int`, `longdesc_100.pdf`, Sonnet 5): the
OCR pre-flight found 3 table regions with ~103 rows and rated the deterministic
parser STRONGLY_RECOMMENDED. The agent declined it — stated reason: *"The table's
Amount column was OCR-corrupted with jumbled text instead of numbers, so
parse_table/map_table_to_schema couldn't cleanly map usable numeric values"* — and
then returned `Transactions: null`. All 100 rows gone.

Two things let that through:

1. The retry loop that exists for exactly this purpose never fired, because an
   empty list breaks no JSON Schema constraint unless the config sets `minItems`,
   and this schema did not (`max_min_items: 0`).
2. The processing report printed `✓ Completeness Validation: All schema
   constraints satisfied` immediately above the warning that the list was empty —
   a clean bill of health next to the defect.

`scalar_accuracy` was 1.000 and the section status was COMPLETED, so nothing else
in the pipeline disagreed either.
"""

# ruff: noqa: I001

from typing import Any

import pytest

from idp_common.extraction.service import ExtractionService
from idp_common.extraction.validation import (
    build_empty_list_feedback,
    find_empty_declared_lists,
)

pytestmark = pytest.mark.unit


# Deliberately WITHOUT minItems — that is the condition under which the bug was
# reachable, and the shape of the benchmark schema that hit it.
SCHEMA: dict[str, Any] = {
    "type": "object",
    "$id": "BankStatement",
    "properties": {
        "Account Number": {"type": "string"},
        "Transactions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "Date": {"type": "string"},
                    "Description": {"type": "string"},
                    "Amount": {"type": "number"},
                },
            },
        },
    },
}

# The pre-flight numbers from the live failure.
OCR_WITH_TABLE = {
    "tables_detected": 3,
    "estimated_row_count": 103,
    "tool_usage_recommended": True,
    "recommendation_strength": "STRONGLY_RECOMMENDED",
}
OCR_NO_TABLE = {
    "tables_detected": 0,
    "estimated_row_count": 0,
    "tool_usage_recommended": False,
    "recommendation_strength": "OPTIONAL",
}


class TestFindEmptyDeclaredLists:
    """Absent, null and [] are the three ways rows go missing, and they are
    indistinguishable to every consumer, so they are grouped."""

    @pytest.mark.parametrize("value", [None, []])
    def test_null_and_empty_are_both_empty(self, value):
        empty, populated = find_empty_declared_lists(
            {"Account Number": "123", "Transactions": value}, SCHEMA
        )
        assert empty == ["Transactions"]
        assert populated == []

    def test_absent_counts_as_empty(self):
        empty, populated = find_empty_declared_lists({"Account Number": "123"}, SCHEMA)
        assert empty == ["Transactions"]

    def test_populated_list_is_not_empty(self):
        empty, populated = find_empty_declared_lists(
            {"Transactions": [{"Date": "2026-01-01"}]}, SCHEMA
        )
        assert empty == []
        assert populated == ["Transactions"]

    def test_scalar_fields_are_never_reported(self):
        """Only *declared array* fields are in scope; a null scalar is normal and
        must never appear in either list."""
        schema = {
            "type": "object",
            "properties": {"Account Number": {"type": "string"}},
        }
        empty, populated = find_empty_declared_lists({"Account Number": None}, schema)
        assert empty == []
        assert populated == []

    def test_an_array_declared_through_a_ref_is_still_found(self):
        """No shipped config declares a top-level array via $ref today, but missing
        one would silently disable the check instead of failing loudly."""
        schema = {
            "type": "object",
            "$defs": {"Txns": {"type": "array", "items": {"type": "object"}}},
            "properties": {"Transactions": {"$ref": "#/$defs/Txns"}},
        }
        empty, populated = find_empty_declared_lists({"Transactions": None}, schema)
        assert empty == ["Transactions"]
        assert populated == []

    def test_an_unresolvable_ref_is_skipped_not_crashed(self):
        schema = {
            "type": "object",
            "properties": {"Transactions": {"$ref": "#/$defs/Missing"}},
        }
        assert find_empty_declared_lists({"Transactions": None}, schema) == ([], [])


class TestEmptyListFeedback:
    def test_feedback_names_the_field_and_the_evidence(self):
        fb = build_empty_list_feedback(["Transactions"], 3, 103)
        assert "'Transactions'" in fb
        assert "103" in fb and "3 table region" in fb

    def test_feedback_forbids_dropping_rows_over_one_bad_column(self):
        """This is the sentence aimed at the reasoning that caused the loss."""
        fb = build_empty_list_feedback(["Transactions"], 1, 100)
        assert "NEVER return null or an empty list" in fb
        assert "ONE column" in fb
        assert "Do not drop the row" in fb


class TestInLoopValidatorRejectsEmptyList:
    def _service(self, validation: dict[str, Any] | None = None) -> ExtractionService:
        config = {
            "extraction": {
                "model": "us.amazon.nova-pro-v1:0",
                "agentic": {
                    "enabled": True,
                    "validation": validation or {"enabled": True},
                },
            },
            "classes": [SCHEMA],
        }
        svc = ExtractionService(region="us-west-2", config=config)
        svc._class_schema = SCHEMA
        return svc

    def test_empty_list_with_table_evidence_is_rejected(self):
        """The regression: schema-valid, but 100 rows are missing."""
        svc = self._service()
        validator = svc._build_schema_validator(ocr_analysis=OCR_WITH_TABLE)
        assert validator is not None
        is_valid, feedback = validator({"Account Number": "1234", "Transactions": None})
        assert not is_valid, (
            "an empty declared list plus ~103 detected table rows must consume a "
            "retry — it violates no schema constraint, which is why it did not"
        )
        assert "Transactions" in feedback

    def test_same_result_passes_without_table_evidence(self):
        """No table in the OCR means an empty list is plausibly correct. The check
        must not fire on documents that genuinely have no rows."""
        svc = self._service()
        validator = svc._build_schema_validator(ocr_analysis=OCR_NO_TABLE)
        assert validator is not None
        is_valid, _ = validator({"Account Number": "1234", "Transactions": None})
        assert is_valid

    def test_no_ocr_analysis_behaves_as_before(self):
        """Callers that do not supply evidence (e.g. the escalation path) keep the
        original pure-schema semantics."""
        svc = self._service()
        validator = svc._build_schema_validator()
        assert validator is not None
        is_valid, _ = validator({"Account Number": "1234", "Transactions": None})
        assert is_valid

    def test_the_check_is_not_gated_on_validation_enabled(self):
        """THE fix that live verification forced. `validation.enabled` defaults to
        FALSE — and the config that produced the bug has it false — so gating this
        check on it left the safety net dead on exactly the configurations that
        needed it. A guard against silent data loss cannot be off by default."""
        svc = self._service(validation={"enabled": False})
        validator = svc._build_schema_validator(ocr_analysis=OCR_WITH_TABLE)
        assert validator is not None, (
            "with schema validation off but table evidence present, the empty-list "
            "check must still run — this returned None and did nothing"
        )
        is_valid, feedback = validator({"Account Number": "1234", "Transactions": None})
        assert not is_valid
        assert "Transactions" in feedback

    def test_schema_violations_stay_gated_on_validation_enabled(self):
        """Only the empty-list check is ungated. Full JSON-Schema validation keeps
        its opt-in contract, so upgrading still changes no behaviour there."""
        svc = self._service(validation={"enabled": False})
        validator = svc._build_schema_validator(ocr_analysis=OCR_WITH_TABLE)
        assert validator is not None
        # A populated list satisfies the empty-list check; the bad `Amount` type
        # would be a schema violation, but schema checks are off.
        is_valid, _ = validator({"Transactions": [{"Amount": "not-a-number"}]})
        assert is_valid, "schema validation must stay opt-in"

        svc_on = self._service(validation={"enabled": True})
        validator_on = svc_on._build_schema_validator(ocr_analysis=OCR_WITH_TABLE)
        assert validator_on is not None
        is_valid, _ = validator_on({"Transactions": [{"Amount": "not-a-number"}]})
        assert not is_valid, "with validation on, the same result must fail"

    def test_a_populated_sibling_list_suppresses_the_check(self):
        """With one list populated, the detected tables plausibly belong to it and
        an empty sibling may be genuinely absent. Only the all-empty case fires."""
        schema = {
            "type": "object",
            "$id": "Two",
            "properties": {
                "Transactions": {"type": "array", "items": {"type": "object"}},
                "Fees": {"type": "array", "items": {"type": "object"}},
            },
        }
        svc = self._service()
        svc._class_schema = schema
        validator = svc._build_schema_validator(ocr_analysis=OCR_WITH_TABLE)
        assert validator is not None
        is_valid, _ = validator({"Transactions": [{"a": 1}], "Fees": None})
        assert is_valid

    def test_populated_list_passes(self):
        svc = self._service()
        validator = svc._build_schema_validator(ocr_analysis=OCR_WITH_TABLE)
        assert validator is not None
        is_valid, _ = validator({"Transactions": [{"Date": "2026-01-01"}]})
        assert is_valid

    def test_returns_none_when_neither_check_applies(self):
        """Nothing to check: schema validation off AND no table evidence. Building a
        callback here would cost a pointless call per agent turn."""
        svc = self._service(validation={"enabled": False})
        assert svc._build_schema_validator() is None
        assert svc._build_schema_validator(ocr_analysis=OCR_NO_TABLE) is None


class TestCompletenessReportStopsContradictingItself:
    def _service(self) -> ExtractionService:
        config = {
            "extraction": {
                "model": "us.amazon.nova-pro-v1:0",
                "agentic": {"enabled": True},
            },
            "classes": [SCHEMA],
        }
        return ExtractionService(region="us-west-2", config=config)

    def test_empty_list_no_longer_reports_all_satisfied(self):
        check = self._service()._check_completeness_detailed(
            extracted_fields={"Account Number": "1234", "Transactions": None},
            schema=SCHEMA,
            tool_used=False,
            ocr_analysis=OCR_WITH_TABLE,
        )
        # No schema constraint IS broken — minItems is not set — and saying
        # otherwise would be wrong. But the summary must not read as healthy.
        assert check["schema_constraints_met"] is True
        assert check["violations"] == []
        assert check["unexplained_empty_lists"] == ["Transactions"]
        assert check["complete"] is False
        assert "All schema constraints satisfied" not in check["summary"]
        assert "NO rows" in check["summary"]
        assert "103" in check["summary"]
        assert "did not use the table parsing tool" in check["summary"]
        assert "minItems" in check["summary"], (
            "the summary should tell the operator how to make this a hard "
            "constraint next time"
        )

    def test_tool_ran_but_produced_nothing_says_so(self):
        check = self._service()._check_completeness_detailed(
            extracted_fields={"Transactions": []},
            schema=SCHEMA,
            tool_used=True,
            ocr_analysis=OCR_WITH_TABLE,
        )
        assert "table parsing tool ran but produced no rows" in check["summary"]

    def test_clean_result_is_still_clean(self):
        check = self._service()._check_completeness_detailed(
            extracted_fields={"Transactions": [{"Date": "2026-01-01"}]},
            schema=SCHEMA,
            tool_used=True,
            ocr_analysis=OCR_WITH_TABLE,
        )
        assert check["summary"] == "All schema constraints satisfied"
        assert check["complete"] is True
        assert check["unexplained_empty_lists"] == []

    def test_no_evidence_means_no_claim(self):
        """Without OCR evidence the check has nothing to go on and stays quiet,
        so behaviour for non-agentic/no-table sections is unchanged."""
        check = self._service()._check_completeness_detailed(
            extracted_fields={"Transactions": None},
            schema=SCHEMA,
            tool_used=False,
            ocr_analysis=None,
        )
        assert check["summary"] == "All schema constraints satisfied"
        assert check["unexplained_empty_lists"] == []

    def test_minitems_violation_still_takes_precedence(self):
        schema = {
            "type": "object",
            "$id": "WithMin",
            "properties": {
                "Transactions": {
                    "type": "array",
                    "minItems": 100,
                    "items": {"type": "object"},
                }
            },
        }
        check = self._service()._check_completeness_detailed(
            extracted_fields={"Transactions": []},
            schema=schema,
            tool_used=False,
            ocr_analysis=OCR_WITH_TABLE,
        )
        assert check["schema_constraints_met"] is False
        assert len(check["violations"]) == 1
        assert "constraint violation" in check["summary"]


class TestPromptForbidsDroppingTheList:
    """The instruction gap is the root cause: the prompt told the agent to fall
    back to direct extraction when mapping was hard, but never told it that
    returning nothing was not an option.

    Read from source rather than imported: ``agentic_idp`` needs the optional
    ``strands`` dependency, and a prompt-content regression test that silently
    skips wherever strands is absent (including CI) would protect nothing.
    """

    @staticmethod
    def _source() -> str:
        from pathlib import Path

        import idp_common.extraction as extraction_pkg

        path = Path(extraction_pkg.__file__).parent / "agentic_idp.py"
        return path.read_text(encoding="utf-8")

    def test_base_prompt_forbids_null_lists(self):
        assert "NEVER return null or an empty list" in self._source()

    def test_table_addendum_covers_the_declined_tool_case(self):
        src = self._source()
        assert "DECLINING THE TOOL IS NOT DECLINING THE TABLE" in src
        assert "OCR-corrupted" in src
