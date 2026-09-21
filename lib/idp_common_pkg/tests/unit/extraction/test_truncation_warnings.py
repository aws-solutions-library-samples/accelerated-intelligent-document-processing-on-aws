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
* the two things a user choosing ``fail`` has to be told, both of which are
  deliberate and therefore documented rather than fixed:
  ``TestZeroRowsStillCompletesUnderFail`` (a list losing EVERY row still completes,
  #1047) and ``TestMinItemsIsVisibilityNotAHardConstraint`` (``minItems`` makes a
  shortfall visible; nothing acts on it, #1048).
"""

from __future__ import annotations

import asyncio
import io
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from idp_common.config.models import IDPConfig
from idp_common.extraction import agentic_idp
from idp_common.extraction.service import (
    ExtractionImageRejected,
    ExtractionInputTooLarge,
    ExtractionOutputIncomplete,
    ExtractionResult,
    ExtractionService,
    SectionInfo,
)
from idp_common.extraction.validation import shard_validation_schema
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

        Driven through ``process_document_section``, because that is where the
        pass-through lives: without it control falls into the generic handler,
        which appends a SECOND, differently-prefixed entry to ``document.errors``
        for one failure, and runs the two Bedrock-error matchers over a message
        that is neither. One failure must record one error.
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

        # Stand in for the work before the tail: the contract under test is how
        # process_document_section ROUTES the exception _save_results raises, not
        # how the section was extracted.
        def _raise_like_save_results(document, *_a, **_kw):
            msg = "Section 1 extraction is materially incomplete: Extracted 43 row(s)."
            document.errors.append(msg)
            raise ExtractionOutputIncomplete(msg)

        with (
            patch.object(svc, "_prepare_section_context", return_value=([], "sys")),
            patch.object(svc, "_invoke_extraction_model", return_value=None),
            patch.object(svc, "_save_results", side_effect=_raise_like_save_results),
        ):
            with pytest.raises(ExtractionOutputIncomplete):
                svc.process_document_section(document=doc, section_id="1")

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
        # default, not a literal, so flipping the default fails here — and says
        # which shipped field it would have cost.
        assert IDPConfig().extraction.row_shortfall_action == "warn", (
            "extraction.row_shortfall_action now defaults to 'fail', which would "
            f"fail this document: {issue.details['list_fields']} in the template's "
            "default preset (lending-package-sample, Bank-Statement) was extracted "
            f"completely and still scores {issue.details['ratio']}, because "
            f"{issue.details['ocr_estimated_rows']} rows of 2-column tables in the "
            "section are summed into its evidence. Narrow _expected_rows_for_width "
            "first."
        )
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


def _repo_root() -> Path:
    import subprocess

    return Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )


class TestZeroRowsStillCompletesUnderFail:
    """#1047: under `fail`, losing 95% of the rows fails and losing 100% completes.

    ``_build_extraction_issues`` skips a width group in which every list is empty
    (``if not labels: continue``) and leaves it to ``extraction_incomplete``, a
    warning, which cannot change a document's status. The asymmetry is deliberate —
    a genuinely empty list is common and legitimate and indistinguishable from total
    loss, the over-attribution in ``TestWhyFailIsOptIn`` applies more strongly to an
    empty list (it scores 0 against whatever evidence is attributed to it), and the
    recorded corpus holds no legitimately-empty-list document that also carries a
    same-width table, so the case cannot be bounded from it.

    Since it is deliberate, a user choosing ``fail`` has to be told, and both doc
    tiers are part of the contract here rather than commentary on it.
    """

    def _saved(self, svc, fields, **kw):
        return TestRowShortfallOutcome._saved(self, svc, fields, **kw)

    def test_the_two_halves_of_the_asymmetry_in_one_place(self):
        partial = _svc(row_shortfall_action="fail")
        _w, _d, _s, exc = self._saved(
            partial, {"Account Number": "1", "Transactions": _rows(43)}
        )
        assert exc is not None, "43 of 800 rows fails — this is the covered half"

        total = _svc(row_shortfall_action="fail")
        write, doc, section, exc = self._saved(
            total, {"Account Number": "1", "Transactions": []}
        )
        assert exc is None, "0 of 800 rows completes — this is the uncovered half"
        assert doc.errors == []
        codes = [i.code for i in section.processing_issues]
        assert "extraction_incomplete" in codes and CODE not in codes
        assert (
            next(
                i
                for i in section.processing_issues
                if i.code == "extraction_incomplete"
            ).severity
            == "warning"
        )
        assert "COMPLETED WITH WARNINGS" in write.call_args.args[0]["processing_report"]

    @pytest.mark.parametrize(
        "path",
        (
            "docs/extraction-and-confidence.md",
            "lib/idp_common_pkg/idp_common/extraction/README.md",
        ),
    )
    def test_both_doc_tiers_state_the_asymmetry_and_its_size(self, path):
        # Markdown emphasis and line wrapping fall between the words of a phrase,
        # so flatten both away before looking for it.
        text = re.sub(
            r"[*`#\s]+", " ", (_repo_root() / path).read_text(encoding="utf-8")
        )
        for token in ("95%", "100%", "3,631", "99 returned zero rows"):
            assert token in text, (
                f"{path} no longer tells a reader choosing "
                f"extraction.row_shortfall_action='fail' that a list losing every "
                f"row still completes, or how large that population is (#1047)."
            )


class TestMinItemsIsVisibilityNotAHardConstraint:
    """#1048: what `minItems` costs is MODE-DEPENDENT, and the text must say so.

    In **Simple** extraction a ``minItems`` shortfall is advisory:
    ``extraction_list_truncated`` is ``severity="warning"``; the same shortfall is a
    JSON-Schema failure whose worst outcome under
    ``extraction.validation.fail_action: reject`` is ``parsing_succeeded=False``,
    read by ``_generate_processing_report``'s status line and the UI's report tab
    and by nothing in the status path; and no ``ProcessingIssue`` changes a
    document's status *by virtue of its severity*,  ``error`` included —
    ``extraction.row_shortfall_action: fail`` changes the outcome by **raising**
    (``_fail_on_row_shortfall``), not by being an error.

    In **Advanced** extraction it is a HARD floor.
    ``TestMinItemsIsAHardFloorInAdvancedMode`` below pins that end to end. So a
    blanket "minItems only makes a shortfall visible" is false for half the
    product, and the previous blanket "minItems makes a shortfall a hard
    constraint" was false for the other half. Both wordings have shipped; the scans
    here are what stop either from returning.
    """

    # Every surface the `minItems` claim reached, plus the scaling guide (which
    # recommends `minItems` for the same purpose) and the two copies a user reads at
    # the moment of choosing a value — `patterns/unified/template.yaml`, which the
    # Configuration editor renders, and the Schema Builder's own Min Items field,
    # which is the strongest instance of that argument because it is the control that
    # sets the number.
    _CLAIM_SURFACES = (
        "lib/idp_common_pkg/idp_common/extraction/service.py",
        "lib/idp_common_pkg/idp_common/extraction/README.md",
        "docs/extraction-and-confidence.md",
        "docs/extraction-scaling-guide.md",
        "patterns/unified/template.yaml",
        "src/ui/src/components/json-schema-builder/constraints/ArrayConstraints.tsx",
    )

    # The false claims, as regexes over the flattened text, banned outright rather
    # than matched in context: "not a hard constraint" is a retraction, and per
    # CLAUDE.md the text should state what the setting DOES instead.
    #
    # Slashes carry `\s*` because flattening collapses a line break to a space but
    # does not close up "downstream / HITL" — the spaced form is the wording in both
    # notebooks (#1063), so prose copied out of one would otherwise pass.
    #
    # ⚠️ This is a LITERAL-RECURRENCE ratchet, not a semantic one. It catches the
    # exact sentences that shipped and near-verbatim reuse of them; a paraphrase
    # ("makes the floor binding", "rejects the section") gets past it, and one
    # already did — `_check_population_completeness`'s docstring said "only flags
    # hard ``minItems`` constraint violations", green because "minItems" sat between
    # the two banned words. Do not read a green run as "no false promise anywhere in
    # these files".
    _BANNED_PATTERNS = (
        # #1048, the minItems claim
        r"hard constraints?",
        # #1048 adjacent, the fail_action: reject claim. Deliberately NOT
        # "section is marked failed" — that is TRUE of
        # `row_shortfall_action: fail`, and template.yaml says it about that.
        r"marked failed because",
        r"section as failed",
        r"failed for hitl",
        r"downstream\s*/\s*hitl",
        r"rout\w*\s+to\s+hitl",
    )

    def test_a_minitems_shortfall_is_only_a_warning_in_simple_mode(self):
        svc = _svc(
            schema={
                "type": "object",
                "properties": {
                    "Account Number": {"type": "string"},
                    "Transactions": {
                        "type": "array",
                        "minItems": 100,
                        "items": ROW,
                    },
                },
            }
        )
        # The floor is read off the class schema's own properties, so drive the
        # real builder rather than asserting on a constructed issue.
        issue = next(
            i
            for i in _issues(svc, {"Account Number": "1", "Transactions": _rows(43)})
            if i.code == "extraction_list_truncated"
        )
        assert issue.severity == "warning"
        assert "hard constraint" not in issue.message

    def test_the_simple_mode_recommendation_does_not_overpromise(self):
        """``extraction_rows_below_ocr_estimate``'s remedy clause, Simple mode.

        It must still recommend ``minItems`` — the one signal with no false
        positives — while saying that in Simple extraction it changes visibility
        and not the outcome.
        """
        svc = _svc()
        svc._document_text = _table(800, pages=17)
        message = next(
            i
            for i in _issues(svc, {"Account Number": "1", "Transactions": _rows(43)})
            if i.code == CODE
        ).message
        assert "minItems" in message
        assert "hard constraint" not in message
        assert "Simple extraction it makes the loss visible" in message
        assert "row_shortfall_action" in message

    def test_the_advanced_mode_recommendation_warns_that_the_floor_is_hard(self):
        """The same clause on the Advanced path must NOT read as advisory.

        Constructed, not reached: ``_build_extraction_issues`` is called directly
        because in Advanced mode a short list does not survive the tool boundary to
        be reported (see ``TestMinItemsIsAHardFloorInAdvancedMode``). The contract
        under test is the wording a reader gets, not that a flow produces it.
        """
        svc = _svc(agentic=True)
        svc._document_text = _table(800, pages=17)
        message = next(
            i
            for i in _issues(svc, {"Account Number": "1", "Transactions": _rows(43)})
            if i.code == CODE
        ).message
        assert "minItems is NOT advisory on this path" in message
        assert "fails the extraction with no rows kept" in message
        # …and it must point at the setting that keeps the rows.
        assert "row_shortfall_action" in message
        assert "hard constraint" not in message

    def test_the_agentic_completeness_summary_warns_before_recommending(self):
        """``_check_completeness_detailed`` runs on the AGENTIC path only.

        Its caller sits inside ``extraction_method == "agentic"``, so its
        ``minItems`` recommendation is an Advanced-mode one and has to carry the
        Advanced-mode cost.
        """
        summary = _svc(agentic=True)._check_completeness_detailed(
            extracted_fields={"Transactions": []},
            schema=SCHEMA,
            tool_used=False,
            ocr_analysis={
                "tool_usage_recommended": True,
                "tables_detected": 2,
                "estimated_row_count": 800,
            },
        )["summary"]
        assert "minItems" in summary
        assert "hard constraint" not in summary
        assert "binding rather than advisory" in summary
        assert "keeps no rows" in summary
        assert "row_shortfall_action" in summary

    @pytest.mark.parametrize("path", _CLAIM_SURFACES)
    def test_no_document_or_source_repeats_a_retired_claim(self, path):
        """Both doc tiers, the service source, the scaling guide, the template.

        Scanned as text because the claims are prose: each reached several files at
        once, and a fix applied to one file left the others promising a consequence
        that does not exist — which is how two copies of the ``reject`` claim
        survived the first pass through these same files.

        Whitespace, Markdown emphasis and comment leaders are flattened first, so a
        phrase split across a line break still counts — including inside a ``#``
        comment or a ``*``-continued block comment, where the leader lands between
        the two words and a whitespace-only flatten misses it (measured: the
        wrapped form evaded the scan in ``service.py`` and
        ``patterns/unified/template.yaml`` until ``#`` joined the class).

        ⚠️ Flattening ``#`` deletes a **structural** boundary, not decoration: in
        Markdown it is a heading marker, so text either side of it can be joined
        into a phrase that appears nowhere in the rendered page (a paragraph ending
        in "hard" immediately above a ``## Constraints`` heading reads as "hard
        constraints"). No such join exists in these files today and the trade is
        worth it — a comment leader hid a real recurrence — but if a spurious match
        does appear, fix it by anchoring the scan per line (flatten within a line,
        join only continuation lines) rather than by dropping ``#`` from the class,
        which would reopen the wrapped-comment hole this closed.
        """
        text = re.sub(
            r"[*`#\s]+", " ", (_repo_root() / path).read_text(encoding="utf-8")
        ).lower()
        for pattern in self._BANNED_PATTERNS:
            found = re.search(pattern, text)
            assert found is None, (
                f"{path} contains the retired claim {found.group(0)!r} "
                f"(pattern {pattern!r}). `minItems` is a hard floor in Advanced "
                "mode — per shard once the section shards — and advisory in Simple "
                "mode; and `validation.fail_action: reject` fails nothing: it "
                "records parsing_succeeded=false, which only the processing report "
                "and the UI report tab read, and it does not send the section to "
                "human review (#1048)."
            )

    def test_no_processing_issue_severity_fails_the_section(self):
        """The invariant the corrected documentation rests on.

        ``_save_results``' only failure is ``_fail_on_row_shortfall``, which fires
        on an ``error``-severity ``ROW_SHORTFALL_CODE`` issue and nothing else. So
        an ``error`` on any other code — a ``minItems`` violation under
        ``fail_action: reject`` included — leaves the section completed. Driven
        through ``_fail_on_row_shortfall`` with the real codes rather than a
        synthetic one, so a new blocking code has to be added here deliberately.
        """
        svc = _svc(row_shortfall_action="fail")
        others = (
            "extraction_list_truncated",
            "extraction_validation_failed",
            "extraction_incomplete",
            "extraction_sparse",
            "extraction_off_schema_fields",
        )
        for code in others:
            for severity in ("info", "warning", "error"):
                doc = Document(
                    id="d", input_key="d.pdf", input_bucket="in", output_bucket="out"
                )
                section = SimpleNamespace(
                    processing_issues=[
                        SimpleNamespace(code=code, severity=severity, message="m")
                    ]
                )
                svc._fail_on_row_shortfall(doc, section, "1")
                assert doc.errors == [], (code, severity)

        # Positive control, so the loop above cannot pass by doing nothing.
        doc = Document(
            id="d", input_key="d.pdf", input_bucket="in", output_bucket="out"
        )
        with pytest.raises(ExtractionOutputIncomplete):
            svc._fail_on_row_shortfall(
                doc,
                SimpleNamespace(
                    processing_issues=[
                        SimpleNamespace(code=CODE, severity="error", message="m")
                    ]
                ),
                "1",
            )

    def test_parsing_succeeded_false_reaches_the_report_and_nothing_else(self):
        """What ``fail_action: reject`` actually buys, end to end.

        The report's status line reads FAILED, and the section's own result is
        written with the extracted values intact — which is the whole of the
        consequence, and is why the issue message says 'the document's status is
        unchanged' rather than 'the section is marked FAILED'.
        """
        report = ExtractionService(config=IDPConfig())._generate_processing_report(
            {"parsing_succeeded": False, "extraction_method": "traditional"}
        )
        assert "Status: FAILED" in report
        ok = ExtractionService(config=IDPConfig())._generate_processing_report(
            {"parsing_succeeded": True, "extraction_method": "traditional"}
        )
        assert "Status: SUCCESS" in ok


class TestMinItemsIsAHardFloorInAdvancedMode:
    """#1048: in Advanced mode `minItems` is enforced where the rows are produced.

    This is the half of the product the "visibility only" reading gets wrong, and
    the reason the guidance is split by mode. ``_transport_model`` runs the class
    schema through ``nullable_leaves_for_transport``, which makes scalar **leaves**
    nullable and leaves the array bounds alone — so ``minItems`` reaches the
    ``extraction_tool`` boundary and a list under the floor is rejected there.

    The consequence chain from that rejection, which is what makes the guidance
    matter: the ``@tool`` call raises, ``current_extraction`` is never stored,
    ``_invoke_agent_for_extraction`` spends ``max_extraction_retries`` whole-section
    agent turns and returns ``None``, and ``structured_output_async`` raises
    ``ValueError("Failed to generate valid structured output.")``. Nothing catches
    it before the Lambda, so the section fails carrying no rows — strictly worse
    than ``row_shortfall_action: fail``, which persists the partial rows, the
    error-severity issue and the processing report first.

    This test pins the **enforcement**, which is the part a wording change could
    silently invalidate (a future decision to strip bounds for transport, the way
    scalar nullability already is stripped, would make the docs wrong again).
    """

    _SCHEMA = {
        "type": "object",
        "properties": {
            "Account Number": {"type": "string"},
            "Transactions": {"type": "array", "minItems": 100, "items": ROW},
        },
    }

    def _model(self):
        return _svc(agentic=True, schema=self._SCHEMA)._transport_model(
            self._SCHEMA, "Statement"
        )

    def test_the_floor_survives_into_the_transport_model(self):
        """`nullable_leaves_for_transport` nulls leaves; it does not drop bounds."""
        import json

        assert "minItems" in json.dumps(self._model().model_json_schema())

    @pytest.mark.parametrize(
        "label,rows,accepted",
        [
            ("a short list is rejected at the tool boundary", 43, False),
            ("an empty list is rejected too", 0, False),
            ("a list at the floor is accepted", 100, True),
        ],
    )
    def test_the_tool_boundary_enforces_the_floor(self, label, rows, accepted):
        import pydantic

        model = self._model()
        payload = {"Account Number": "1", "Transactions": _rows(rows)}
        if accepted:
            model(**payload)
            return
        with pytest.raises(pydantic.ValidationError) as ei:
            model(**payload)
        assert any(e["type"] == "too_short" for e in ei.value.errors()), label

    def test_the_web_uis_string_form_of_the_floor_is_enforced_too(self):
        """The Configuration table stores numeric schema fields as strings.

        ``minItems: "100"`` is what a class authored in the Web UI arrives as, and
        it is NOT a hole in the floor — the generator coerces it. Pinned because
        "the string form slips through" is the obvious guess about where an
        Advanced-mode `extraction_list_truncated` could come from, and it is wrong.
        """
        import pydantic

        schema = {
            "type": "object",
            "properties": {
                "Transactions": {"type": "array", "minItems": "100", "items": ROW},
            },
        }
        model = _svc(agentic=True, schema=schema)._transport_model(schema, "Statement")
        with pytest.raises(pydantic.ValidationError):
            model(**{"Transactions": _rows(43)})

    def test_the_failure_wording_the_agent_path_raises_is_unchanged(self):
        """The bare message the docs describe, read from the source that raises it.

        Quoted in both doc tiers as what a reader sees when an unreachable floor
        exhausts the correction rounds, so it is asserted rather than restated.
        """
        root = _repo_root()
        text = (
            root / "lib/idp_common_pkg/idp_common/extraction/agentic_idp.py"
        ).read_text(encoding="utf-8")
        assert 'raise ValueError("Failed to generate valid structured output.")' in text

    def test_each_shard_agent_gets_the_whole_sections_floor(self):
        """The case the guidance turns on: the floor is PER SHARD, not at the merge.

        Both fan-out sites pass the whole-section transport model as ``data_format``
        and ``_run_shard_agent`` forwards it unmodified, so
        ``structured_output_async`` builds each shard's ``extraction_tool`` from it.
        A section-sized floor is then unsatisfiable by a shard covering part of the
        pages — which is why the docs say not to put ``minItems`` on a list whose
        section shards.

        Driven through the real ``_run_shard_agent`` with a spy on the tool builder,
        stopping at agent construction: the identity of the model the tool is built
        from is the whole contract, and going further would need Bedrock.
        """
        model = self._model()
        seen: dict[str, Any] = {}
        real = agentic_idp.create_dynamic_extraction_tool_and_patch_tool

        def spy(model_class):
            seen["tool_model"] = model_class
            return real(model_class)

        async def drive():
            with (
                patch.object(
                    agentic_idp,
                    "create_dynamic_extraction_tool_and_patch_tool",
                    side_effect=spy,
                ),
                patch.object(agentic_idp, "Agent", side_effect=RuntimeError("stop")),
            ):
                with pytest.raises(RuntimeError):
                    await agentic_idp._run_shard_agent(
                        shard_index=0,
                        total_shards=6,
                        page_start=0,
                        page_end=3,
                        total_pages=17,
                        model_id="us.anthropic.claude-sonnet-4-20250514-v1:0",
                        data_format=model,
                        shard_prompt="pages 1-3 text",
                        config=_svc(agentic=True, schema=self._SCHEMA).config,
                        context="Extraction",
                        max_retries=1,
                        connect_timeout=10.0,
                        read_timeout=30.0,
                        max_tokens=None,
                        checkpoint_callback=None,
                        schema_validator=_svc(
                            agentic=True, schema=self._SCHEMA
                        )._shard_schema_validator(),
                    )

        asyncio.run(drive())
        assert seen.get("tool_model") is model, (
            "the shard's extraction_tool must be built from the whole-section "
            "transport model — if this changes to a per-shard model, the per-shard "
            "floor warning in both doc tiers is no longer true and should go"
        )

    def test_the_shipped_default_makes_the_per_shard_floor_the_default_case(self):
        """Choosing Advanced mode IS the opt-in; sharding needs no second one.

        ``max_concurrent_batches`` ships as ``10`` in ``base-extraction.yaml`` and in
        the Advanced extraction settings the Configuration editor renders from
        ``patterns/unified/template.yaml``, so the ``default=1`` on the Pydantic
        field is the fallback for an ABSENT key and a deployed stack never reads it.
        Both doc tiers state the per-shard floor as the ordinary case on that basis,
        which is only true while all three sources agree — so they are pinned
        together here rather than in prose. The neighbouring
        ``test_the_shipped_default_clamps_only_on_very_long_sections`` exists because
        three drafts printed the fallback as "the default"; this one covers the same
        mistake at the place it changes a reader's decision.

        The engagement condition is read off the real planner too: at the shipped
        ``max_pages_per_shard: 5`` a section of ordinary pages runs single-pass up to
        five pages and shards above that, so the 17-page worked example both tiers
        use really does shard, into four. Asserted under the module token budget and
        under a large auto-sized one, because the page cap is what binds for ordinary
        pages and the docs say so.
        """
        import yaml

        from idp_common.extraction.sharding import (
            DEFAULT_SHARD_TOKEN_BUDGET,
            plan_shards,
        )

        root = _repo_root()
        shipped = yaml.safe_load(
            (
                root
                / "lib/idp_common_pkg/idp_common/config/system_defaults"
                / "base-extraction.yaml"
            ).read_text(encoding="utf-8")
        )["extraction"]["agentic"]
        assert shipped["max_concurrent_batches"] == 10
        assert shipped["max_pages_per_shard"] == 5

        # The copy a user reads while choosing the value. Matched as text because the
        # template is CloudFormation and not loadable as plain YAML.
        block = re.search(
            r"\n( +)max_concurrent_batches:\n(?:\1 .*\n)+",
            (root / "patterns/unified/template.yaml").read_text(encoding="utf-8"),
        )
        assert block is not None and re.search(
            r"^\s*default: 10$", block.group(0), re.M
        ), (
            "the Configuration editor must offer the same default as "
            "base-extraction.yaml, or a user reads one number and gets another"
        )

        # Different on purpose, and why the docs must not quote it: this value is
        # reached only when the key is absent, which no deployed stack does.
        assert IDPConfig().extraction.agentic.max_concurrent_batches == 1

        def sparse(n: int) -> list[str]:
            return ["word " * 50] * n

        for budget in (DEFAULT_SHARD_TOKEN_BUDGET, 200_000):
            counts = {
                pages: len(
                    plan_shards(
                        sparse(pages),
                        token_budget=budget,
                        max_shards=shipped["max_concurrent_batches"],
                        max_pages_per_shard=shipped["max_pages_per_shard"],
                    )
                )
                for pages in (5, 6, 17)
            }
            assert counts == {5: 1, 6: 2, 17: 4}, (budget, counts)

    def test_no_document_frames_sharding_as_a_second_opt_in(self):
        """The wording the shipped default makes false, banned where it shipped.

        A reader told sharding is opt-in concludes the per-shard floor is an edge
        case and sets `minItems` anyway; at `max_concurrent_batches: 10` it applies
        to every multi-page Advanced section. Literal, like ``_BANNED_PATTERNS``: a
        paraphrase gets past it, so this is a recurrence ratchet and not a proof
        that no document frames it that way.
        """
        for path in (
            "docs/extraction-and-confidence.md",
            "lib/idp_common_pkg/idp_common/extraction/README.md",
            "src/ui/src/components/json-schema-builder/constraints/ArrayConstraints.tsx",
        ):
            text = re.sub(
                r"[*`#\s]+", " ", (_repo_root() / path).read_text(encoding="utf-8")
            ).lower()
            assert "sharding is opt-in" not in text, (
                f"{path} calls sharding opt-in; max_concurrent_batches ships at 10, "
                "so any Advanced-mode section over max_pages_per_shard pages shards "
                "and the per-shard minItems floor is the default case"
            )

    def test_the_relaxed_shard_schema_does_not_reach_the_tool_boundary(self):
        """The two shard-scoped boundaries disagree, and only one is relaxed.

        ``shard_validation_schema`` drops ``required`` and ``minItems`` because a
        shard legitimately holds neither — but it feeds the in-loop *feedback*
        validator, not the tool. So for the same 17-row shard of a
        ``minItems: 100`` section the feedback validator reports every constraint
        satisfied while the tool boundary rejects the call. That gap is what the
        docs now warn about, so it is pinned rather than described.
        """
        import pydantic

        svc = _svc(agentic=True, schema=self._SCHEMA)
        shard_rows = _rows(17)

        assert "minItems" not in json.dumps(shard_validation_schema(self._SCHEMA))
        ok, feedback = svc._shard_schema_validator()(
            {"Account Number": "1", "Transactions": shard_rows}
        )
        assert ok, feedback

        with pytest.raises(pydantic.ValidationError) as ei:
            self._model()(**{"Account Number": "1", "Transactions": shard_rows})
        assert any(e["type"] == "too_short" for e in ei.value.errors())

    def test_row_shortfall_action_is_the_alternative_that_keeps_the_rows(self):
        """The contrast the guidance rests on, asserted rather than asserted-about.

        Same section, same 43-of-800 shortfall: under `fail` the partial rows and
        the diagnosis are durable before the section fails. That is what makes
        `row_shortfall_action` the recommended lever over an Advanced-mode floor.
        """
        svc = _svc(row_shortfall_action="fail")
        write, _doc, _section, exc = TestRowShortfallOutcome._saved(
            self, svc, {"Account Number": "1", "Transactions": _rows(43)}
        )
        assert exc is not None
        written = write.call_args.args[0]
        assert len(written["inference_result"]["Transactions"]) == 43
        assert [
            i
            for i in written["metadata"]["processing_issues"]
            if i["code"] == CODE and i["severity"] == "error"
        ]


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
