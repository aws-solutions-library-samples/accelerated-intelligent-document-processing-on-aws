# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Simple-mode large-document warnings (#726 follow-up, measured 2026-09-10).

With over-splitting fixed, a Simple-mode section is ONE request: an 800-row statement
returned 43 rows with COMPLETED and no processing issue, and 25+ pages failed with
Bedrock's "Input is too long". Two warnings and one message make both loud:

* ``extraction_rows_below_ocr_estimate`` — rows extracted vs table rows in the OCR text.
* ``extraction_section_exceeds_model_input`` — pre-flight: the single request exceeds
  the model's input window.
* the ``document.errors`` entry for the "Input is too long" failure says what to change.
"""

from __future__ import annotations

from types import SimpleNamespace

from idp_common.config.models import IDPConfig
from idp_common.extraction.service import ExtractionService

SCHEMA = {
    "type": "object",
    "properties": {
        "Account Number": {"type": "string"},
        "Transactions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "Date": {"type": "string"},
                    "Amount": {"type": "number"},
                },
            },
        },
    },
}


def _svc(*, agentic: bool = False) -> ExtractionService:
    cfg = IDPConfig(
        **{
            "extraction": {
                "mode": "advanced" if agentic else "simple",
                "agentic": {"enabled": agentic},
            }
        }
    )
    svc = ExtractionService(config=cfg)
    svc._reset_context()
    svc._class_schema = SCHEMA
    return svc


def _table_text(rows: int, *, pages: int = 1) -> str:
    """OCR-like Markdown: one table split across ``pages`` blocks, headings repeated."""
    per = max(1, rows // pages)
    blocks = []
    for p in range(pages):
        lines = ["| Date | Description | Amount |", "|---|---|---|"]
        lines += [
            f"| 01/0{(i % 9) + 1}/2024 | SEQ{i:05d} Sample | {i}.00 |"
            for i in range(p * per, min(rows, (p + 1) * per))
        ]
        blocks.append("\n".join(lines))
    return ("\n\nSome prose between pages.\n\n" * 1).join(blocks)


def _rows(n: int) -> list[dict]:
    return [{"Date": "01/01/2024", "Amount": float(i)} for i in range(n)]


def _codes(issues) -> list[str]:
    return [i.code for i in issues]


class TestRowShortfall:
    def test_43_of_800_rows_is_reported(self):
        svc = _svc()
        svc._document_text = _table_text(800, pages=17)
        issues = svc._build_extraction_issues(
            extracted_fields={"Account Number": "1", "Transactions": _rows(43)},
            metadata={},
            section_id="1",
        )
        assert "extraction_rows_below_ocr_estimate" in _codes(issues)
        issue = next(
            i for i in issues if i.code == "extraction_rows_below_ocr_estimate"
        )
        assert issue.severity == "warning"
        assert issue.details["extracted_rows"] == 43
        assert 800 <= issue.details["ocr_estimated_rows"] <= 800 + 17  # heading rows
        assert "Advanced" in issue.message  # simple mode recommends switching
        assert "extraction_incomplete" not in _codes(issues)  # not double-reported

    def test_a_complete_list_is_not_reported(self):
        svc = _svc()
        svc._document_text = _table_text(400, pages=9)
        issues = svc._build_extraction_issues(
            extracted_fields={"Account Number": "1", "Transactions": _rows(400)},
            metadata={},
            section_id="1",
        )
        assert "extraction_rows_below_ocr_estimate" not in _codes(issues)

    def test_small_key_value_tables_do_not_trigger_it(self):
        """A payslip-like class: a short list next to a 20-row key/value table."""
        svc = _svc()
        svc._document_text = "\n".join(f"| Field {i} | value {i} |" for i in range(20))
        issues = svc._build_extraction_issues(
            extracted_fields={"Account Number": "1", "Transactions": _rows(2)},
            metadata={},
            section_id="1",
        )
        assert "extraction_rows_below_ocr_estimate" not in _codes(issues)

    def test_an_empty_list_is_left_to_extraction_incomplete(self):
        svc = _svc()
        svc._document_text = _table_text(800, pages=17)
        issues = svc._build_extraction_issues(
            extracted_fields={"Account Number": "1", "Transactions": []},
            metadata={},
            section_id="1",
        )
        codes = _codes(issues)
        assert "extraction_incomplete" in codes
        assert "extraction_rows_below_ocr_estimate" not in codes

    def test_multi_instance_wrapper_counts_nested_rows(self):
        """`instances: [{Transactions: [...400 rows]}]` is one top-level item but 400 rows."""
        svc = _svc()
        svc._class_schema = {
            "type": "object",
            "properties": {"instances": {"type": "array", "items": SCHEMA}},
        }
        svc._document_text = _table_text(400, pages=9)
        issues = svc._build_extraction_issues(
            extracted_fields={
                "instances": [{"Account Number": "1", "Transactions": _rows(400)}]
            },
            metadata={},
            section_id="1",
        )
        assert "extraction_rows_below_ocr_estimate" not in _codes(issues)

    def test_advanced_mode_wording_does_not_recommend_advanced(self):
        svc = _svc(agentic=True)
        svc._document_text = _table_text(800, pages=17)
        issues = svc._build_extraction_issues(
            extracted_fields={"Account Number": "1", "Transactions": _rows(43)},
            metadata={},
            section_id="1",
        )
        issue = next(
            i for i in issues if i.code == "extraction_rows_below_ocr_estimate"
        )
        assert "Advanced (agentic) extraction, which shards" not in issue.message
        assert issue.details["agentic"] is True

    def test_no_document_text_means_no_estimate_and_no_issue(self):
        svc = _svc()
        issues = svc._build_extraction_issues(
            extracted_fields={"Account Number": "1", "Transactions": _rows(3)},
            metadata={},
            section_id="1",
        )
        assert "extraction_rows_below_ocr_estimate" not in _codes(issues)


class TestRowLikeCounting:
    def test_counts_dict_items_at_any_depth_not_scalars(self):
        f = ExtractionService._count_row_like_items
        assert f({"Transactions": _rows(3)}) == 3
        assert f({"Tags": ["a", "b", "c"]}) == 0
        assert (
            f({"instances": [{"Transactions": _rows(4)}, {"Transactions": _rows(5)}]})
            == 2 + 9
        )
        assert f({"Group": {"Rows": _rows(2)}}) == 2
        assert f(None) == 0


class TestInputPreflight:
    def test_records_a_warning_when_the_request_exceeds_the_window(self, monkeypatch):
        svc = _svc()
        monkeypatch.setattr(
            svc, "_get_sizing_plan", lambda: SimpleNamespace(max_input_tokens=10_000)
        )
        svc._page_images = [b"x"] * 25
        content = [{"text": "x" * 60_000}, *([{"image": {"format": "jpeg"}}] * 25)]
        est = svc._simple_mode_input_preflight(
            content=content, system_prompt="sys", model_id="m", section_id="7"
        )
        assert est > 10_000
        assert len(svc._preflight_issues) == 1
        issue = svc._preflight_issues[0]
        assert issue.code == "extraction_section_exceeds_model_input"
        assert issue.severity == "warning" and issue.section_id == "7"
        assert (
            issue.details["max_input_tokens"] == 10_000
            and issue.details["images"] == 25
        )
        assert "Input is too long" in issue.message and "Advanced" in issue.message
        # ...and it is the FIRST issue the builder reports.
        svc._document_text = ""
        issues = svc._build_extraction_issues(
            extracted_fields={"Account Number": "1", "Transactions": _rows(3)},
            metadata={},
            section_id="7",
        )
        assert issues[0].code == "extraction_section_exceeds_model_input"

    def test_silent_when_the_request_fits(self, monkeypatch):
        svc = _svc()
        monkeypatch.setattr(
            svc, "_get_sizing_plan", lambda: SimpleNamespace(max_input_tokens=200_000)
        )
        svc._simple_mode_input_preflight(
            content=[{"text": "short"}],
            system_prompt="sys",
            model_id="m",
            section_id="1",
        )
        assert svc._preflight_issues == []

    def test_reset_clears_preflight_issues(self, monkeypatch):
        svc = _svc()
        monkeypatch.setattr(
            svc, "_get_sizing_plan", lambda: SimpleNamespace(max_input_tokens=10)
        )
        svc._simple_mode_input_preflight(
            content=[{"text": "x" * 1000}],
            system_prompt=None,
            model_id="m",
            section_id="1",
        )
        assert svc._preflight_issues
        svc._reset_context()
        assert svc._preflight_issues == []

    def test_sizing_failure_never_blocks(self, monkeypatch):
        svc = _svc()
        monkeypatch.setattr(
            svc,
            "_get_sizing_plan",
            lambda: (_ for _ in ()).throw(RuntimeError("no limits")),
        )
        est = svc._simple_mode_input_preflight(
            content=[{"text": "x" * 1000}],
            system_prompt=None,
            model_id="m",
            section_id="1",
        )
        assert est > 0 and svc._preflight_issues == []


class TestActionableError:
    def test_input_too_long_gets_the_explanation(self):
        msg = ExtractionService._actionable_section_error(
            RuntimeError(
                "An error occurred (ValidationException) when calling the Converse operation: Input is too long for requested model."
            ),
            "3",
            25,
        )
        assert msg.startswith("Error processing section 3: ")
        assert "25 page(s)" in msg and "Advanced (agentic) extraction" in msg

    def test_other_errors_are_unchanged(self):
        msg = ExtractionService._actionable_section_error(
            ValueError("bad schema"), "3", 25
        )
        assert msg == "Error processing section 3: bad schema"
