# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Simple-mode large-document warnings (#726 follow-up, measured 2026-09-10).

With over-splitting fixed, a Simple-mode section is ONE request: an 800-row statement
returned 43 rows with COMPLETED and no processing issue, and 25+ pages failed with
Bedrock's bare "Input is too long". This pins:

* ``extraction_rows_below_ocr_estimate`` — rows extracted vs the OCR tables SHAPED like
  the list (column count EQUAL to the item's property count; tables segmented on gaps AND
  on width changes; same-width lists judged as a group), so a second table, a form's
  key/value blocks, a prose "|" or a list of scalars never count against it.
* the pre-flight estimate (log + remembered figures, NOT a processing issue) and
  ``ExtractionInputTooLarge`` — the mode-aware, remedy-carrying failure.
* what that shortfall COSTS: ``extraction.row_shortfall_action`` and
  ``ExtractionOutputIncomplete`` (#1032). Detection and consequence are separate
  concerns here — the action moves the severity and the outcome, never the floor or
  the ratio, and ``TestRowShortfallOutcome`` asserts both halves of that.
* why ``fail`` is OPT-IN rather than the default: ``TestWhyFailIsOptIn`` drives the
  **shipped** default preset's ``Bank-Statement`` class through the real check with a
  100%-correct extraction and shows it flagged, because a statement's 31-row
  two-column Daily Balance table is summed into the evidence for a 5-row
  two-property ``account_summary``. That test fails if the default is ever flipped
  without narrowing the attribution first.
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from idp_common.config.models import IDPConfig
from idp_common.extraction.service import (
    ExtractionImageRejected,
    ExtractionInputTooLarge,
    ExtractionOutputIncomplete,
    ExtractionResult,
    ExtractionService,
    SectionInfo,
)
from idp_common.models import Document, Section, Status
from idp_common.utils.bedrock_utils import (
    is_image_request_rejection,
    is_input_token_overflow,
)
from idp_common.utils.transient_errors import is_transient_error

ROW = {
    "type": "object",
    "properties": {
        "Date": {"type": "string"},
        "Description": {"type": "string"},
        "Amount": {"type": "number"},
    },
}
SCHEMA = {
    "type": "object",
    "properties": {
        "Account Number": {"type": "string"},
        "Transactions": {"type": "array", "items": ROW},
    },
}


def _svc(
    *,
    agentic: bool = False,
    schema: dict | None = None,
    row_shortfall_action: str | None = None,
) -> ExtractionService:
    extraction: dict = {
        "mode": "advanced" if agentic else "simple",
        "agentic": {"enabled": agentic},
    }
    if row_shortfall_action is not None:
        extraction["row_shortfall_action"] = row_shortfall_action
    cfg = IDPConfig(**{"extraction": extraction})
    svc = ExtractionService(config=cfg)
    svc._reset_context()
    svc._class_schema = schema or SCHEMA
    return svc


def _table(
    rows: int, cols: int = 3, *, pages: int = 1, heading: str | None = None
) -> str:
    """OCR-like Markdown: one ``cols``-column table across ``pages`` blocks with the
    heading reprinted per page, blocks separated by a short page break."""
    per = max(1, -(-rows // pages))
    head = heading or "| " + " | ".join(f"H{c}" for c in range(cols)) + " |"
    sep = "|" + "---|" * cols
    blocks = []
    for p in range(pages):
        lines = [head, sep]
        for i in range(p * per, min(rows, (p + 1) * per)):
            lines.append("| " + " | ".join(f"c{c}_{i}" for c in range(cols)) + " |")
        blocks.append("\n".join(lines))
    return "\n\nPage break text.\n\n".join(blocks)


def _rows(n: int) -> list[dict]:
    return [
        {"Date": "01/01/2024", "Description": f"SEQ{i:05d}", "Amount": float(i)}
        for i in range(n)
    ]


def _codes(issues) -> list[str]:
    return [i.code for i in issues]


def _issues(svc, fields):
    return svc._build_extraction_issues(
        extracted_fields=fields, metadata={}, section_id="1"
    )


CODE = "extraction_rows_below_ocr_estimate"


class TestRowShortfall:
    def test_43_of_800_rows_is_reported(self):
        svc = _svc()
        svc._document_text = _table(800, pages=17)
        issues = _issues(svc, {"Account Number": "1", "Transactions": _rows(43)})
        assert CODE in _codes(issues)
        issue = next(i for i in issues if i.code == CODE)
        # warning under the SHIPPED default: row_shortfall_action is 'warn', so the
        # detection is advisory unless a deployment opts in. The error severity and
        # the section failure are asserted under 'fail' in TestRowShortfallOutcome.
        assert issue.severity == "warning"
        assert issue.details["row_shortfall_action"] == "warn"
        assert issue.details["list_fields"] == ["Transactions"]
        assert issue.details["extracted_rows"] == 43
        assert (
            800 <= issue.details["ocr_estimated_rows"] <= 800 + 17
        )  # reprinted headings
        assert "Advanced" in issue.message
        assert "extraction_incomplete" not in _codes(issues)

    def test_a_complete_list_is_not_reported(self):
        svc = _svc()
        svc._document_text = _table(400, pages=9)
        assert CODE not in _codes(
            _issues(svc, {"Account Number": "1", "Transactions": _rows(400)})
        )

    def test_a_second_table_of_another_shape_does_not_count(self):
        """100 Transactions complete, next to a 100-row two-column Daily Balances table."""
        svc = _svc()
        svc._document_text = (
            _table(100, cols=3)
            + "\n\n\n\n\n\n\n\n"
            + _table(100, cols=2, heading="| Date | Balance |")
        )
        assert CODE not in _codes(
            _issues(svc, {"Account Number": "1", "Transactions": _rows(100)})
        )

    def test_key_value_blocks_do_not_count_against_a_short_list(self):
        """A form pack: 60 key/value rows (2 columns) and a 3-row list of 3-column rows."""
        svc = _svc()
        svc._document_text = "\n".join(f"| Field {i} | value {i} |" for i in range(60))
        assert CODE not in _codes(
            _issues(svc, {"Account Number": "1", "Transactions": _rows(3)})
        )

    def test_prose_lines_with_a_pipe_are_not_a_table(self):
        svc = _svc()
        svc._document_text = "\n".join(
            f"Call 1-800-000-{i:04d} | Visit us online\n\nMore prose here.\n\nAnd more.\n\nStill more.\n\nEnd."
            for i in range(40)
        )
        assert CODE not in _codes(
            _issues(svc, {"Account Number": "1", "Transactions": _rows(2)})
        )

    def test_a_list_of_scalars_is_never_compared(self):
        svc = _svc(
            schema={
                "type": "object",
                "properties": {
                    "CheckNumbers": {"type": "array", "items": {"type": "string"}}
                },
            }
        )
        svc._document_text = _table(40, cols=1, heading="| Check |")
        assert CODE not in _codes(
            _issues(svc, {"CheckNumbers": [str(i) for i in range(40)]})
        )

    def test_an_empty_list_is_left_to_extraction_incomplete(self):
        svc = _svc()
        svc._document_text = _table(800, pages=17)
        codes = _codes(_issues(svc, {"Account Number": "1", "Transactions": []}))
        assert "extraction_incomplete" in codes and CODE not in codes

    def test_instances_are_compared_through_their_inner_lists(self):
        """Multi-instance wrapper: one instance holding a complete 400-row list."""
        svc = _svc(
            schema={
                "type": "object",
                "properties": {"instances": {"type": "array", "items": SCHEMA}},
            }
        )
        svc._document_text = _table(400, pages=9)
        assert CODE not in _codes(
            _issues(
                svc,
                {"instances": [{"Account Number": "1", "Transactions": _rows(400)}]},
            )
        )

    def test_a_truncated_inner_list_across_instances_is_reported(self):
        svc = _svc(
            schema={
                "type": "object",
                "properties": {"instances": {"type": "array", "items": SCHEMA}},
            }
        )
        svc._document_text = _table(800, pages=17)
        issues = _issues(
            svc,
            {
                "instances": [
                    {"Account Number": "1", "Transactions": _rows(20)},
                    {"Account Number": "2", "Transactions": _rows(23)},
                ]
            },
        )
        issue = next(i for i in issues if i.code == CODE)
        assert issue.details["list_fields"] == ["instances[].Transactions"]
        assert issue.details["extracted_rows"] == 43

    def test_form_style_instances_with_key_value_blocks_are_quiet(self):
        """3 payslip-like instances, each 12 key/value rows and a complete 3-row list."""
        svc = _svc(
            schema={
                "type": "object",
                "properties": {"instances": {"type": "array", "items": SCHEMA}},
            }
        )
        svc._document_text = "\n\n\n\n\n\n\n\n".join(
            "\n".join(f"| Field {i} | value {i} |" for i in range(12)) for _ in range(3)
        )
        assert CODE not in _codes(
            _issues(
                svc,
                {
                    "instances": [
                        {"Account Number": str(k), "Transactions": _rows(3)}
                        for k in range(3)
                    ]
                },
            )
        )

    def test_advanced_mode_wording_does_not_recommend_advanced(self):
        svc = _svc(agentic=True)
        svc._document_text = _table(800, pages=17)
        issue = next(
            i
            for i in _issues(svc, {"Account Number": "1", "Transactions": _rows(43)})
            if i.code == CODE
        )
        assert "Advanced (agentic) extraction, which shards" not in issue.message
        assert issue.details["agentic"] is True

    def test_no_document_text_means_no_estimate_and_no_issue(self):
        svc = _svc()
        assert CODE not in _codes(
            _issues(svc, {"Account Number": "1", "Transactions": _rows(3)})
        )

    @pytest.mark.parametrize(
        "ocr_rows,extracted,fires",
        [
            (29, 5, False),
            (30, 14, True),
            (30, 15, False),
            (100, 49, True),
            (100, 50, False),
        ],
    )
    def test_the_floor_and_the_half_ratio_boundaries(self, ocr_rows, extracted, fires):
        svc = _svc()
        # the heading row counts as a row, so ocr_rows-1 body rows = ocr_rows exactly
        svc._document_text = _table(ocr_rows - 1)
        assert (
            CODE
            in _codes(
                _issues(svc, {"Account Number": "1", "Transactions": _rows(extracted)})
            )
        ) is fires


class TestRowShortfallOutcome:
    """#1032: a materially incomplete list must not be reported as COMPLETED.

    ``extraction.row_shortfall_action`` decides what the SAME detection COSTS.
    The detection itself — floor 30, ratio 0.5, same-width OCR evidence — is
    untouched, and ``test_the_action_never_moves_the_detection_boundary`` is what
    holds that.
    """

    def _saved(self, svc, fields, *, rows_in_ocr=800, pages=17):
        """Drive the real ``_save_results`` tail. Returns the S3 write mock.

        Uses the production path rather than calling ``_fail_on_row_shortfall``
        directly, because the contract under test is an ORDERING one: the result
        must already be persisted when the section fails.
        """
        svc._document_text = _table(rows_in_ocr, pages=pages)
        doc = Document(
            id="d",
            input_key="d.pdf",
            input_bucket="in",
            output_bucket="out",
            status=Status.EXTRACTING,
        )
        section = Section(section_id="1", classification="Statement", page_ids=["1"])
        doc.sections = [section]
        info = SectionInfo(
            class_label="Statement",
            sorted_page_ids=["1"],
            page_indices=[0],
            output_bucket="out",
            output_key="d.pdf/sections/1/result.json",
            output_uri="s3://out/d.pdf/sections/1/result.json",
            start_page=1,
            end_page=1,
        )
        result = ExtractionResult(
            extracted_fields=fields,
            metering={},
            parsing_succeeded=True,
            total_duration=1.0,
        )
        with patch("idp_common.extraction.service.s3.write_content") as write:
            try:
                svc._save_results(doc, section, result, info, "1", 0.0)
            except ExtractionOutputIncomplete as e:
                return write, doc, section, e
        return write, doc, section, None

    def test_fail_fails_the_section_after_persisting_the_partial(self):
        svc = _svc(row_shortfall_action="fail")
        write, doc, section, exc = self._saved(
            svc, {"Account Number": "1", "Transactions": _rows(43)}
        )
        assert exc is not None, "43 of 800 rows must not resolve to success"
        # Ordering is the contract: the partial rows and the diagnosis are durable
        # BEFORE the section fails, so the failure costs visibility, not data.
        assert write.called, "the partial result must be written before failing"
        written = write.call_args.args[0]
        assert len(written["inference_result"]["Transactions"]) == 43
        persisted = written["metadata"]["processing_issues"]
        assert [i for i in persisted if i["code"] == CODE and i["severity"] == "error"]
        # The preserved artifact must not claim the section completed. This report
        # is what a reader opens to find out what happened, and it is written one
        # statement before the section fails, so "COMPLETED WITH ERRORS" would have
        # contradicted the outcome it exists to explain.
        assert (
            "Status: FAILED — EXTRACTION MATERIALLY INCOMPLETE"
            in (written["processing_report"])
        )
        assert "COMPLETED" not in written["processing_report"].split("\n")[4]
        # and document.errors carries it, as the sibling overflow/image handlers
        # do. Ordinarily this exception fails the Step Functions execution before
        # processresults_function (which fails a document on a non-empty
        # document.errors) is reached, and the Step Functions cause is what the
        # reader sees; the append is what carries the text if it is ever caught.
        assert any("materially incomplete" in e for e in doc.errors)
        assert "43 row(s)" in str(exc) and "row_shortfall_action" in str(exc)

    def test_the_section_fails_with_exactly_one_recorded_error(self):
        """Pins the ``except ExtractionOutputIncomplete: raise`` pass-through.

        Without it, control reaches the generic handler, which appends a SECOND,
        differently-prefixed entry to ``document.errors`` for one failure. Not
        visible in a Step Functions cause, but a duplicated error list is what a
        report or an alarm counts, so the pass-through is observable here.
        """
        svc = _svc(row_shortfall_action="fail")
        doc = Document(
            id="d",
            input_key="d.pdf",
            input_bucket="in",
            output_bucket="out",
            status=Status.EXTRACTING,
        )
        section = Section(section_id="1", classification="Statement", page_ids=["1"])
        doc.sections = [section]
        svc._document_text = _table(800, pages=17)
        with pytest.raises(ExtractionOutputIncomplete):
            svc._fail_on_row_shortfall(
                doc,
                SimpleNamespace(
                    processing_issues=[
                        SimpleNamespace(
                            code=CODE, severity="error", message="Extracted 43 row(s)."
                        )
                    ]
                ),
                "1",
            )
        assert len(doc.errors) == 1, doc.errors

    def test_warn_is_the_default_and_reports_success(self):
        svc = _svc()  # no explicit action: the shipped default
        write, doc, section, exc = self._saved(
            svc, {"Account Number": "1", "Transactions": _rows(43)}
        )
        assert exc is None, "'warn' is the documented opt-out and must not raise"
        issue = next(i for i in section.processing_issues if i.code == CODE)
        assert issue.severity == "warning"
        assert issue.details["row_shortfall_action"] == "warn"
        assert "The run still reports success" in issue.message
        assert doc.errors == []
        assert "COMPLETED WITH WARNINGS" in write.call_args.args[0]["processing_report"]

    def test_a_complete_list_is_unaffected_by_either_action(self):
        for action in ("fail", "warn"):
            svc = _svc(row_shortfall_action=action)
            _w, doc, _s, exc = self._saved(
                svc,
                {"Account Number": "1", "Transactions": _rows(400)},
                rows_in_ocr=400,
                pages=9,
            )
            assert exc is None, action
            assert doc.errors == [], action

    def test_advanced_mode_is_held_to_the_same_rule(self):
        """The defect is mode-independent, so the fix is too (#1032).

        Agentic extraction shards and so rarely truncates, but when it does the
        outcome must not be success either — a fix applied only to Simple mode
        would leave the same lie reachable through the other mode.
        """
        svc = _svc(agentic=True, row_shortfall_action="fail")
        _w, _d, _s, exc = self._saved(
            svc, {"Account Number": "1", "Transactions": _rows(43)}
        )
        assert exc is not None

    @pytest.mark.parametrize(
        "ocr_rows,extracted,fires",
        [(29, 5, False), (30, 14, True), (30, 15, False), (100, 49, True)],
    )
    def test_the_action_never_moves_the_detection_boundary(
        self, ocr_rows, extracted, fires
    ):
        """Same floor and same ratio under both actions — no new threshold ships.

        A second, lower threshold for "bad enough to fail" would be a number
        picked to fit the observed cases. The set of runs that FAIL under 'fail'
        is exactly the set that WARNED before.
        """
        seen = {}
        for action in ("fail", "warn"):
            svc = _svc(row_shortfall_action=action)
            svc._document_text = _table(ocr_rows - 1)
            issues = _issues(
                svc, {"Account Number": "1", "Transactions": _rows(extracted)}
            )
            seen[action] = CODE in _codes(issues)
        assert seen == {"fail": fires, "warn": fires}

    def test_a_blank_or_null_action_resolves_to_the_shipped_default(self):
        """The config editor has persisted nulls for scalar fields before — and an
        UPGRADE is the same shape: every stored config predating this field has the
        key absent, so the resolved value is what decides whether upgrading changes
        any document's outcome. It must be 'warn'."""
        for raw in (None, "", "   ", "WARN"):
            cfg = IDPConfig(**{"extraction": {"row_shortfall_action": raw}})
            assert cfg.extraction.row_shortfall_action == "warn", raw

    def test_an_unknown_action_is_rejected_rather_than_silently_accepted(self):
        with pytest.raises(Exception) as ei:
            IDPConfig(**{"extraction": {"row_shortfall_action": "escalate"}})
        assert "row_shortfall_action" in str(ei.value)

    def test_the_failure_is_deterministic_and_in_no_retry_list(self):
        """Derived from the state machine, not restated.

        A retried row shortfall re-sends a request that will stop early again, so
        the class name must appear in no ``Retry.ErrorEquals``. The expectation is
        read out of the ASL itself — a hardcoded list of retriers would go stale
        the moment one is added.
        """
        import json
        import re
        import subprocess

        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        rel = "patterns/unified/statemachine/workflow.asl.json"
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", rel],
            cwd=root,
            capture_output=True,
            text=True,
        )
        assert tracked.returncode == 0, f"{rel} is not tracked by git"
        raw = (Path(root) / rel).read_text(encoding="utf-8")
        # The ASL is a CloudFormation template body: ${Placeholder} appears
        # unquoted, so neutralise those before parsing rather than hand-rolling
        # a second parser.
        asl = json.loads(re.sub(r"\$\{[^}]+\}", "0", raw))

        retried: set[str] = set()

        def walk(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "Retry" and isinstance(value, list):
                        for rule in value:
                            retried.update(rule.get("ErrorEquals") or [])
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(asl)
        assert retried, "found no retriers at all — the walk is broken, not the ASL"
        assert ExtractionOutputIncomplete.__name__ not in retried
        # and the shared classifier agrees, independently of the ASL
        assert not is_transient_error(ExtractionOutputIncomplete("Section 1 ..."))

    def test_the_message_is_not_mistaken_for_a_bedrock_input_or_image_failure(self):
        """Run the OLD detectors over the NEW failure's text.

        ``ExtractionInputTooLarge`` and ``ExtractionImageRejected`` are chosen by
        substring matchers over the error text, and ``process_document_section``
        consults both. If a row-shortfall message happened to match one, the
        section would fail with a remedy — shrink the images, switch to Advanced —
        that has nothing to do with why it failed.
        """
        svc = _svc()
        svc._document_text = _table(800, pages=17)
        issue = next(
            i
            for i in _issues(svc, {"Account Number": "1", "Transactions": _rows(43)})
            if i.code == CODE
        )
        for text in (issue.message, issue.root_cause):
            err = ExtractionOutputIncomplete(text)
            assert not is_input_token_overflow(err), text
            assert not is_image_request_rejection(err), text


class TestWhyFailIsOptIn:
    """The evidence model cannot support `fail` as a DEFAULT, and this pins why.

    ``_expected_rows_for_width`` sums every OCR table of the list's width over the
    whole section. For a 2- or 3-property array that models an entity GROUP rather
    than table rows — and such an array is structurally identical to one that
    models rows — that evidence has no legitimate contribution, so a completely
    correct extraction can score below the ratio. Nine such fields ship in the
    config library.

    These tests are the reason ``row_shortfall_action`` defaults to ``warn``. If a
    later change makes ``fail`` the default, the first one below fails and names
    the shipped field it would have broken.
    """

    @staticmethod
    def _shipped_bank_statement() -> dict:
        """The Bank-Statement class from the template's DEFAULT preset.

        Read from the config library rather than restated, so the test tracks what
        actually ships; skipped rather than silently passing if it moves.
        """
        import subprocess

        import yaml

        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        rel = "config_library/unified/lending-package-sample/config.yaml"
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", rel],
            cwd=root,
            capture_output=True,
            text=True,
        )
        if tracked.returncode != 0:
            pytest.skip(f"{rel} is not tracked by git")
        cfg = yaml.safe_load((Path(root) / rel).read_text(encoding="utf-8")) or {}
        for cls in cfg.get("classes") or []:
            if isinstance(cls, dict) and cls.get("$id") == "Bank-Statement":
                return cls
        pytest.skip("Bank-Statement is no longer in the default preset")

    @staticmethod
    def _monthly_statement_ocr() -> str:
        """OCR for an ordinary statement: a 5-row 2-column Account Summary, a
        60-row 5-column transaction table, and a 31-row 2-column Daily Balance
        table — the last being the one the shortfall check misattributes."""
        return "\n\n".join(
            [
                "ACCOUNT SUMMARY",
                _table(5, 2, heading="| Description | Amount |"),
                "TRANSACTION DETAILS",
                _table(
                    60,
                    5,
                    heading="| Date | Description | Deposits | Withdrawals | Balance |",
                ),
                "DAILY BALANCE SUMMARY",
                _table(31, 2, heading="| Date | Balance |"),
            ]
        )

    def test_a_correct_extraction_of_the_default_preset_is_flagged(self):
        schema = self._shipped_bank_statement()
        svc = _svc(schema=schema)
        svc._document_text = self._monthly_statement_ocr()
        # A 100% CORRECT extraction: every summary row and every transaction.
        issues = _issues(
            svc,
            {
                "account_summary": [
                    {"summary_desc": f"d{i}", "summary_amount": str(i)}
                    for i in range(5)
                ],
                "transaction_details": [
                    {
                        "date": "01/01/2024",
                        "description": f"t{i}",
                        "balance": "1",
                        "deposits": "1",
                        "withdrawals": "0",
                    }
                    for i in range(60)
                ],
            },
        )
        flagged = [i for i in issues if i.code == CODE]
        assert flagged, (
            "the misattribution this test exists to pin is gone — if "
            "_expected_rows_for_width was narrowed, that is good news: delete this "
            "test and reconsider the default"
        )
        issue = flagged[0]
        # The long list the check exists for is NOT flagged; the 5-row summary is.
        assert issue.details["list_fields"] == ["account_summary"]
        assert issue.details["item_property_count"] == 2
        assert issue.details["extracted_rows"] == 5
        assert issue.details["ocr_estimated_rows"] == 38  # 6 summary + 32 daily
        assert issue.details["ratio"] < 0.2
        # THEREFORE the default must be advisory. Asserted against the config
        # default, not a literal, so flipping the default fails here.
        assert IDPConfig().extraction.row_shortfall_action == "warn"
        assert issue.severity == "warning"

    def test_under_fail_that_correct_extraction_would_lose_the_document(self):
        """The same schema and OCR, opted in — this is what the default avoids."""
        svc = _svc(schema=self._shipped_bank_statement(), row_shortfall_action="fail")
        svc._document_text = self._monthly_statement_ocr()
        issues = _issues(
            svc,
            {
                "account_summary": [
                    {"summary_desc": f"d{i}", "summary_amount": str(i)}
                    for i in range(5)
                ],
                "transaction_details": [
                    {
                        "date": "01/01/2024",
                        "description": f"t{i}",
                        "balance": "1",
                        "deposits": "1",
                        "withdrawals": "0",
                    }
                    for i in range(60)
                ],
            },
        )
        issue = next(i for i in issues if i.code == CODE)
        assert issue.severity == "error"
        doc = Document(
            id="d",
            input_key="d.pdf",
            input_bucket="in",
            output_bucket="out",
            status=Status.EXTRACTING,
        )
        with pytest.raises(ExtractionOutputIncomplete):
            svc._fail_on_row_shortfall(
                doc, SimpleNamespace(processing_issues=issues), "1"
            )

    def test_the_shipped_default_preset_really_does_enable_table_ocr(self):
        """The check is inert without Markdown tables, so this is its precondition.

        Derived from the system default plus the preset, because the docs asserted
        the opposite (`ocr.features: []`) and that is what made the misattribution
        above invisible.
        """
        import subprocess

        import yaml

        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        base = yaml.safe_load(
            (
                Path(root)
                / "lib/idp_common_pkg/idp_common/config/system_defaults/base-ocr.yaml"
            ).read_text(encoding="utf-8")
        )
        preset = yaml.safe_load(
            (
                Path(root) / "config_library/unified/lending-package-sample/config.yaml"
            ).read_text(encoding="utf-8")
        )
        features = (preset.get("ocr") or {}).get("features") or (
            base.get("ocr") or {}
        ).get("features")
        names = {(f or {}).get("name") for f in (features or [])}
        assert "TABLES" in names, (
            f"the default preset resolves to ocr.features={sorted(n for n in names if n)}; "
            "if TABLES is gone the check is inert by construction and both doc tiers "
            "need updating again"
        )


class TestSiblingsRefsAndWrappers:
    def test_same_width_sibling_tables_complete_do_not_warn(self):
        """Deposits, Withdrawals and Fees — three complete (Date, Description, Amount) tables."""
        schema = {
            "type": "object",
            "properties": {
                k: {"type": "array", "items": ROW}
                for k in ("Deposits", "Withdrawals", "Fees")
            },
        }
        svc = _svc(schema=schema)
        svc._document_text = "\n\n## Withdrawals\n\n".join(
            _table(100, cols=3) for _ in range(3)
        )
        assert CODE not in _codes(
            _issues(svc, {k: _rows(100) for k in ("Deposits", "Withdrawals", "Fees")})
        )

    def test_same_width_siblings_truncated_in_total_warn_once_naming_them(self):
        schema = {
            "type": "object",
            "properties": {
                k: {"type": "array", "items": ROW} for k in ("Deposits", "Withdrawals")
            },
        }
        svc = _svc(schema=schema)
        svc._document_text = "\n\n## Withdrawals\n\n".join(
            _table(100, cols=3) for _ in range(2)
        )
        issues = [
            i
            for i in _issues(svc, {"Deposits": _rows(20), "Withdrawals": _rows(23)})
            if i.code == CODE
        ]
        assert len(issues) == 1
        assert issues[0].details["list_fields"] == ["Deposits", "Withdrawals"]
        assert issues[0].details["extracted_rows"] == 43

    def test_items_defined_by_ref_are_resolved(self):
        """Every shipped preset defines its rows in $defs; the check must see through it."""
        schema = {
            "type": "object",
            "$defs": {"Transaction": ROW},
            "properties": {
                "Account Number": {"type": "string"},
                "Transactions": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/Transaction"},
                },
            },
        }
        svc = _svc(schema=schema)
        svc._document_text = _table(800, pages=17)
        assert CODE in _codes(
            _issues(svc, {"Account Number": "1", "Transactions": _rows(43)})
        )
        assert CODE not in _codes(
            _issues(svc, {"Account Number": "1", "Transactions": _rows(800)})
        )

    def test_a_bare_multi_instance_wrapper_is_not_compared_to_table_rows(self):
        """Instances are documents: 5 payee instances next to a 100-row 3-column table."""
        item = {
            "type": "object",
            "properties": {
                "Payee": {"type": "string"},
                "Amount": {"type": "number"},
                "Date": {"type": "string"},
            },
        }
        schema = {
            "type": "object",
            "x-aws-idp-instance-array": "instances",
            "properties": {"instances": {"type": "array", "items": item}},
        }
        svc = _svc(schema=schema)
        svc._document_text = _table(100, cols=3)
        assert CODE not in _codes(
            _issues(
                svc, {"instances": [{"Payee": "p", "Amount": 1.0, "Date": "d"}] * 5}
            )
        )

    def test_a_footer_line_with_pipes_does_not_change_the_table_width(self):
        svc = _svc()
        svc._document_text = (
            _table(800, pages=17)
            + "\nPage 17 of 17 | Member FDIC | Equal Housing Lender | Routing 000000000\n"
        )
        assert CODE in _codes(
            _issues(svc, {"Account Number": "1", "Transactions": _rows(43)})
        )


class TestOcrTables:
    @pytest.mark.parametrize("empty_lines,rows", [(5, [11]), (6, [6])])
    def test_the_gap_boundary_tolerates_empty_lines_inside_a_table(
        self, empty_lines, rows
    ):
        """Up to 5 intervening empty lines keep one table; at 6 the second run stands
        alone, and without its own separator it is not a table at all."""
        body = "\n".join(f"| a{i} | b{i} | c{i} |" for i in range(5))
        text = _table(5, cols=3) + "\n" * (empty_lines + 1) + body
        assert [t["rows"] for t in ExtractionService._ocr_tables(text)] == rows

    @pytest.mark.parametrize("rows,expect", [(2, 0), (3, 1)])
    def test_the_minimum_rows(self, rows, expect):
        text = "| a | b |\n|---|---|\n" + "\n".join(
            f"| a{i} | b{i} |" for i in range(rows - 1)
        )
        assert len(ExtractionService._ocr_tables(text)) == expect

    def test_pipe_lines_without_a_separator_row_are_not_a_table(self):
        """A 2-column key/value BLOCK rendered with pipes but no ``|---|`` row, and a
        footer whose pipes are inside the text: neither is a Markdown table."""
        kv = "\n".join(f"| Field {i} | value {i} |" for i in range(60))
        assert ExtractionService._ocr_tables(kv) == []
        footer = "\n".join(
            "Member Services | 1-800-555-0100 | www.example.com" for _ in range(40)
        )
        assert ExtractionService._ocr_tables(footer) == []

    def test_a_footer_block_after_prose_does_not_join_the_table(self):
        """The reviewer's case: 17 pages, each a 2-row Charges table, one prose line,
        then a 3-line 2-column footer block with leading pipes. Prose ends the table
        and the footer run has no separator, so a complete 34-row extraction is not
        warned (before this rule the footer inflated the estimate to 102 rows)."""
        page = (
            _table(2, cols=2, heading="| Description | Amount |")
            + "\nQuestions?\n"
            + "| Member Services | 1-800-555-0100 |\n| Claims | PO Box 1 |\n| Web | x.com |"
        )
        text = "\n\n".join(page for _ in range(17))
        tables = ExtractionService._ocr_tables(text)
        assert [(t["rows"], t["cols"]) for t in tables] == [(3, 2)] * 17
        schema = {
            "type": "object",
            "properties": {
                "Charges": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "Description": {"type": "string"},
                            "Amount": {"type": "number"},
                        },
                    },
                }
            },
        }
        svc = _svc(schema=schema)
        svc._document_text = text
        rows = [{"Description": "d", "Amount": 1.0}] * 34
        assert CODE not in _codes(_issues(svc, {"Charges": rows}))

    def test_a_real_two_column_table_next_to_a_two_property_list_counts(self):
        """The trade the exact-width rule makes: a 35-row 2-column TABLE (heading and
        separator) is evidence for a 2-property list, so 3 extracted rows warn."""
        schema = {
            "type": "object",
            "properties": {
                "Deductions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "Name": {"type": "string"},
                            "Amount": {"type": "number"},
                        },
                    },
                }
            },
        }
        svc = _svc(schema=schema)
        svc._document_text = _table(35, cols=2, heading="| Field | Value |")
        rows = [{"Name": "a", "Amount": 1.0}]
        assert CODE in _codes(_issues(svc, {"Deductions": rows * 3}))
        assert CODE not in _codes(_issues(svc, {"Deductions": rows * 20}))

    def test_a_reprinted_heading_starts_a_new_table_of_the_same_width(self):
        tables = ExtractionService._ocr_tables(_table(20, pages=2))
        assert [(t["rows"], t["cols"]) for t in tables] == [(11, 3), (11, 3)]

    def test_a_heading_wider_than_its_body_still_counts_the_body(self):
        text = "| A | B | C | D |\n|---|---|---|---|\n" + "\n".join(
            f"| a{i} | b{i} | c{i} |" for i in range(10)
        )
        assert ExtractionService._ocr_tables(text) == [{"rows": 10, "cols": 3}]

    def test_a_width_change_starts_a_new_table_and_trailing_empty_cells_are_ignored(
        self,
    ):
        text = (
            _table(10, cols=3)
            + "\n"
            + _table(5, cols=2, heading="| K | V |")
            + "\n"
            + "| a | b | c |  |\n|---|---|---|---|\n"
            + "\n".join("| a | b | c |  |" for _ in range(3))
        )
        tables = ExtractionService._ocr_tables(text)
        assert [(t["rows"], t["cols"]) for t in tables] == [(11, 3), (6, 2), (4, 3)]

    def test_segments_by_gap_and_measures_columns(self):
        text = (
            _table(10, cols=3)
            + "\n" * 8
            + _table(5, cols=2, heading="| A | B |")
            + "\n" * 8
            + "x | y\n"
        )
        tables = ExtractionService._ocr_tables(text)
        assert [(t["rows"], t["cols"]) for t in tables] == [
            (11, 3),
            (6, 2),
        ]  # headings counted; lone 'x | y' dropped

    def test_expected_rows_matches_shape(self):
        tables = [
            {"rows": 101, "cols": 3},
            {"rows": 100, "cols": 2},
            {"rows": 4, "cols": 4},
        ]
        # only the exact 3-column table matches a 3-property item
        assert ExtractionService._expected_rows_for_width(3, tables) == 101
        # a 2-property item matches only the 2-column table
        assert ExtractionService._expected_rows_for_width(2, tables) == 100


class TestInputPreflight:
    def test_logs_and_remembers_but_records_no_issue(self, monkeypatch, caplog):
        svc = _svc()
        monkeypatch.setattr(
            svc, "_get_sizing_plan", lambda: SimpleNamespace(max_input_tokens=10_000)
        )
        svc._page_images = [b"x"] * 25
        content = [
            {"text": "x" * 60_000},
            *(
                [{"image": {"format": "jpeg", "source": {"bytes": b"not-an-image"}}}]
                * 25
            ),
        ]
        with caplog.at_level("WARNING"):
            est = svc._simple_mode_input_preflight(
                content=content, system_prompt="sys", model_id="m", section_id="7"
            )
        # chars/4 ("sys" rounds to 0) + the fallback per unreadable image
        assert est == 15_000 + 25 * 1600
        assert svc._last_simple_input_estimate["max_input_tokens"] == 10_000
        assert svc._last_simple_input_estimate["pages"] == 25
        assert any("Input is too long" in r.message for r in caplog.records)
        assert (
            _codes(_issues(svc, {"Account Number": "1", "Transactions": _rows(3)}))
            == []
        )

    def test_image_tokens_come_from_pixels_when_readable(self):
        """Claude prices an image in 28px patches, CAPPED by the model's
        resolution tier — 1,568 tokens on Sonnet 4.6, 4,784 on Sonnet 5. The
        older uncapped (w*h)/750 figure returned 6,000 for this page, ~4x the
        real cost, which is what made an image-heavy request look like a
        context-window overflow when Bedrock had actually rejected the images
        themselves (#994)."""
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (1500, 3000)).save(buf, format="PNG")
        block = {"format": "png", "source": {"bytes": buf.getvalue()}}

        # Uncapped patch count for 1500x3000 is 54*108 = 5,832, so both tiers cap.
        assert (
            ExtractionService._image_token_estimate(
                block, 1600, "us.anthropic.claude-sonnet-4-6"
            )
            == 1568
        )
        assert (
            ExtractionService._image_token_estimate(
                block, 1600, "us.anthropic.claude-sonnet-5"
            )
            == 4784
        )
        # Non-Claude families keep the deliberately generous legacy figure.
        assert (
            ExtractionService._image_token_estimate(
                block, 1600, "us.amazon.nova-pro-v1"
            )
            == 6000
        )
        assert (
            ExtractionService._image_token_estimate(
                {"source": {"bytes": b"garbage"}}, 1600
            )
            == 1600
        )
        assert ExtractionService._image_token_estimate("not a dict", 1600) == 1600

    def test_preflight_records_the_image_shape_for_the_failure_message(self):
        """A request can be rejected for its image COUNT or per-image SIZE rather
        than its token total, so both are remembered (#994)."""
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (2550, 3301)).save(buf, format="PNG")
        svc = _svc()
        content = [
            {"image": {"format": "png", "source": {"bytes": buf.getvalue()}}}
        ] * 21
        svc._simple_mode_input_preflight(
            content=content,
            system_prompt="sys",
            model_id="us.anthropic.claude-sonnet-5",
            section_id="3",
        )
        est = svc._last_simple_input_estimate
        assert est["images"] == 21
        assert est["max_image_dimension"] == 3301

    def test_silent_when_the_request_fits(self, monkeypatch, caplog):
        svc = _svc()
        monkeypatch.setattr(
            svc, "_get_sizing_plan", lambda: SimpleNamespace(max_input_tokens=200_000)
        )
        with caplog.at_level("WARNING"):
            svc._simple_mode_input_preflight(
                content=[{"text": "short"}],
                system_prompt="sys",
                model_id="m",
                section_id="1",
            )
        assert not any("Input is too long" in r.message for r in caplog.records)

    def test_reset_clears_the_estimate_and_sizing_failure_never_blocks(
        self, monkeypatch
    ):
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
        assert est == 250 and svc._last_simple_input_estimate["max_input_tokens"] == 0
        svc._reset_context()
        assert svc._last_simple_input_estimate is None


class TestShardWrapperAndMatcher:
    def test_the_shard_wrapper_awaits_the_coroutine_and_explains_overflow(self):
        import asyncio

        from botocore.exceptions import ClientError

        svc = _svc()

        async def shard(**kw):
            raise ClientError(
                {
                    "Error": {
                        "Code": "ValidationException",
                        "Message": "Input is too long for requested model.",
                    }
                },
                "Converse",
            )

        with pytest.raises(ExtractionInputTooLarge) as ei:
            asyncio.run(svc._run_shard_or_explain_overflow(shard, section_id="s1"))
        assert "max_pages_per_shard" in str(ei.value)
        assert isinstance(ei.value.__cause__, ClientError)

    def test_the_shard_wrapper_passes_other_errors_and_results_through(self):
        import asyncio

        svc = _svc()

        async def ok(**kw):
            return {"status": "ok", **kw}

        async def boom(**kw):
            raise RuntimeError("shard exploded")

        assert asyncio.run(svc._run_shard_or_explain_overflow(ok, x=1)) == {
            "status": "ok",
            "x": 1,
        }
        with pytest.raises(RuntimeError):
            asyncio.run(svc._run_shard_or_explain_overflow(boom))

    def test_a_client_error_is_judged_by_its_code(self):
        from botocore.exceptions import ClientError

        throttle = ClientError(
            {
                "Error": {
                    "Code": "ThrottlingException",
                    "Message": "Too many input tokens per minute",
                }
            },
            "Converse",
        )
        assert not is_input_token_overflow(throttle)
        overflow = ClientError(
            {
                "Error": {
                    "Code": "ValidationException",
                    "Message": "Input is too long for requested model.",
                }
            },
            "Converse",
        )
        assert is_input_token_overflow(overflow)
        assert is_input_token_overflow(
            ValueError("input token count 210000 exceeds the maximum")
        )

    @pytest.mark.parametrize(
        "message",
        [
            "image exceed max allowed size for many-image requests: 2000 pixels",
            "The image exceeds 5 MB maximum: 7167852 bytes > 5242880 bytes",
            "image dimensions exceed the maximum allowed",
            "too many images in request",
        ],
    )
    def test_an_image_rejection_is_not_classified_as_an_overflow(self, message):
        """These get their own class and their own remedy. The two families share
        vocabulary ("exceeds", "too large"), and overflow advice — fewer pages per
        shard, switch extraction mode — cannot fix an oversized image, so the
        image branch is matched separately and consulted first (#994).

        Note what this does NOT claim: measured against the wording as it stands,
        ``is_input_token_overflow`` does not match these strings either, so the
        image matcher is what gives them an explanation, not a correction of a
        misrouting. See
        ``test_the_overflow_matcher_never_claimed_the_pixel_wording``.
        """
        from botocore.exceptions import ClientError

        exc = ClientError(
            {"Error": {"Code": "ValidationException", "Message": message}}, "Converse"
        )
        assert is_image_request_rejection(exc)
        assert not is_input_token_overflow(exc)

    def test_the_overflow_matcher_never_claimed_the_pixel_wording(self):
        """Pins the honest version of the #994 story.

        The overflow matcher requires input/context/prompt vocabulary, which
        Bedrock's many-image pixel rejection does not carry — so that message was
        never *misclassified*; it simply had no explanation. What WAS misdiagnosed
        is the other half: an oversized request PAYLOAD comes back as "Input is
        too long for requested model", a genuine overflow match, and the old
        uncapped token estimate then over-stated the page images 2.4x, so the
        message compared an inflated estimate against a window the request had not
        actually exceeded. Keeping this assertion stops the narrative drifting
        back to the wrong one.
        """
        pixel_rejection = RuntimeError(
            "image exceed max allowed size for many-image requests: 2000 pixels"
        )
        assert is_input_token_overflow(pixel_rejection) is False
        payload_rejection = RuntimeError("Input is too long for requested model.")
        assert is_input_token_overflow(payload_rejection) is True
        assert is_image_request_rejection(payload_rejection) is False

    def test_a_non_validation_error_code_settles_it_before_the_markers(self):
        """The image verdict is deterministic — it short-circuits the retry ladder
        and raises a non-retryable error — so a transient fault must not be
        converted into a permanent failure just because its message happens to
        carry image vocabulary.

        The fixture message must contain a marker that IS still in the list, or
        this asserts nothing: an earlier version used "image size", which was then
        removed from the markers, leaving the test green while exercising no
        guard at all."""
        from botocore.exceptions import ClientError

        from idp_common.utils.bedrock_utils import _IMAGE_REJECTION_MARKERS

        message = "Rate exceeded: too many images in flight for this account"
        assert any(m in message.lower() for m in _IMAGE_REJECTION_MARKERS), (
            "fixture no longer exercises the code guard"
        )
        throttle = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": message}},
            "Converse",
        )
        assert is_image_request_rejection(throttle) is False
        # Same wording, ValidationException: now it IS an image rejection.
        rejection = ClientError(
            {"Error": {"Code": "ValidationException", "Message": message}},
            "Converse",
        )
        assert is_image_request_rejection(rejection) is True

    def test_the_many_image_note_is_a_complete_sentence(self):
        """The note is appended before the remedy advice, so an unterminated
        clause runs straight into the next sentence and the reader sees
        "...more than 20 Simple extraction sends...". It is the fix's primary
        user-facing output in the scenario #994 reports."""
        svc = _svc()
        svc._last_simple_input_estimate = {
            "estimated_input_tokens": 330_126,
            "max_input_tokens": 1_000_000,
            "pages": 29,
            "images": 29,
            "max_image_dimension": 3301,
        }
        msg = svc._explain_input_overflow(
            ValueError("Input is too long for requested model."), "s1", is_agentic=False
        )
        note_end = msg.index(" Simple extraction sends")
        assert msg[note_end - 1] == "."

    def test_a_genuine_overflow_is_not_claimed_by_the_image_matcher(self):
        assert not is_image_request_rejection(
            ValueError("Input is too long for requested model.")
        )
        assert not is_image_request_rejection(
            ValueError("input token count 210000 exceeds the maximum")
        )

    def test_the_shard_wrapper_explains_an_image_rejection_separately(self):
        import asyncio

        from botocore.exceptions import ClientError

        svc = _svc()

        async def shard(**kw):
            raise ClientError(
                {
                    "Error": {
                        "Code": "ValidationException",
                        "Message": (
                            "image exceed max allowed size for many-image "
                            "requests: 2000 pixels"
                        ),
                    }
                },
                "Converse",
            )

        with pytest.raises(ExtractionImageRejected) as ei:
            asyncio.run(svc._run_shard_or_explain_overflow(shard, section_id="s1"))
        msg = str(ei.value)
        # The remedy must be about image size, not about shard/page budgets.
        assert "target_width" in msg
        assert "max_pages_per_shard" not in msg
        assert isinstance(ei.value.__cause__, ClientError)

    def test_an_overflow_on_a_many_image_request_names_the_image_cap_too(self):
        """When the failing request also had the shape that Bedrock rejects on
        image dimensions, the overflow message says so — otherwise the reader
        lowers a page budget that may not be the binding limit."""
        svc = _svc()
        svc._last_simple_input_estimate = {
            "estimated_input_tokens": 250_000,
            "max_input_tokens": 200_000,
            "pages": 29,
            "images": 29,
            "max_image_dimension": 3301,
        }
        msg = svc._explain_input_overflow(
            ValueError("Input is too long"), "s1", is_agentic=False
        )
        assert "29 image(s)" in msg and "3301px" in msg and "2000px" in msg

    def test_an_already_explained_agentic_overflow_gets_no_second_remedy(self):
        svc = _svc()
        inner = ValueError(
            "Extraction input exceeds the model's context window. Remedies: enable concurrent sharding ... (underlying error: Input is too long)"
        )
        msg = svc._explain_input_overflow(inner, "s1", is_agentic=True)
        assert "max_pages_per_shard" not in msg and "Remedies:" in msg


class TestOverflowFailure:
    @pytest.mark.parametrize(
        "text",
        [
            "An error occurred (ValidationException) when calling the Converse operation: Input is too long for requested model.",
            "Input Tokens Exceeded",
            "input token count 210000 exceeds the maximum of 200000",
            "The context window was exceeded",
            "The model returned the following errors: prompt is too long: 213551 tokens > 200000 maximum",
        ],
    )
    def test_every_bedrock_phrasing_is_recognised(self, text):
        assert is_input_token_overflow(RuntimeError(text)) is True

    def test_other_errors_are_not(self):
        assert is_input_token_overflow(ValueError("bad schema")) is False

    def test_simple_wording_carries_size_and_remedy(self):
        svc = _svc()
        svc._last_simple_input_estimate = {
            "estimated_input_tokens": 280_000,
            "max_input_tokens": 200_000,
            "pages": 25,
            "images": 25,
        }
        msg = svc._explain_input_overflow(
            RuntimeError("Input is too long for requested model."),
            "3",
            is_agentic=False,
        )
        assert msg.startswith("Error processing section 3: ")
        assert (
            "280,000" in msg
            and "25 page(s)" in msg
            and "Advanced (agentic) extraction" in msg
        )

    def test_agentic_wording_does_not_tell_an_agentic_user_to_go_agentic(self):
        svc = _svc(agentic=True)
        msg = svc._explain_input_overflow(
            RuntimeError("Input is too long"), "3", is_agentic=True
        )
        assert "max_pages_per_shard" in msg and "Use Advanced" not in msg

    def test_the_raised_class_is_new_and_therefore_not_retried(self):
        from idp_common.utils.transient_errors import is_transient_error

        exc = ExtractionInputTooLarge(
            "Error processing section 1: Input is too long ..."
        )
        assert is_transient_error(exc) is False
        assert type(exc).__name__ == "ExtractionInputTooLarge"
