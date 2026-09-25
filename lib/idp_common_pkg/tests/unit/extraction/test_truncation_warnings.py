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

        Both fan-out sites pass the **shard** transport model
        (``_shard_transport_model``) as ``data_format`` and ``_run_shard_agent``
        forwards it unmodified, so ``structured_output_async`` builds each shard's
        ``extraction_tool`` from it. That model relaxes presence for a required
        container (#1078) and **keeps every row-count bound**, so a section-sized
        floor is still unsatisfiable by a shard covering part of the pages — which is
        why the docs say not to put ``minItems`` on a list whose section shards.

        The floor's survival is asserted here and not left to the relaxation's own
        tests, because it is the one property of the shard model the guidance in both
        doc tiers depends on: a future decision to strip bounds for the shard tool
        would make those pages wrong with nothing else noticing.

        Driven through the real ``_run_shard_agent`` with a spy on the tool builder,
        stopping at agent construction: the identity of the model the tool is built
        from is the whole contract, and going further would need Bedrock.
        """
        import pydantic

        model = _svc(agentic=True, schema=self._SCHEMA)._shard_transport_model(
            self._SCHEMA, "Statement"
        )
        # The floor reaches the shard's tool: a short list is still rejected there.
        assert "minItems" in json.dumps(model.model_json_schema())
        with pytest.raises(pydantic.ValidationError) as floor:
            model(**{"Account Number": "1", "Transactions": _rows(43)})
        assert any(e["type"] == "too_short" for e in floor.value.errors())
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
            "the shard's extraction_tool must be built from the model the fan-out "
            "site passes — if a shard ever gets a model with the row-count bounds "
            "stripped, the per-shard floor warning in both doc tiers is no longer "
            "true and should go"
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


class TestARequiredContainerIsSatisfiableByAShard:
    """#1078: the two shard-scoped boundaries agree about presence.

    ``shard_validation_schema`` drops ``required`` because a shard sees only its own
    pages, and ``_run_shard_agent`` tells it "if a field does not appear in your
    pages, leave it null — another shard will provide it". For a required **array**
    or **nested object** that instruction used to name the one answer the shard's own
    ``extraction_tool`` rejected: ``nullable_leaves_for_transport`` widens scalar
    *leaves*, so a required scalar was rescued and a required container was not. The
    shard therefore spent a correction round discovering it had to emit ``[]`` or
    ``{}``, while the in-loop feedback validator reported the same payload as
    satisfying every constraint — so the feedback did not point at the cause.

    The fix scopes the **tool boundary** per shard rather than propagating ``required``
    into the feedback validator, because dropping ``required`` there was deliberate
    and correct. What must NOT relax is the structural half: an omitted key, an empty
    tool call and a misspelled key set still fail per shard, and row-count bounds
    still reach the tool.
    """

    _SCHEMA = {
        "type": "object",
        "$id": "Statement",
        "properties": {
            "Account Number": {"type": "string"},
            "Transactions": {"type": "array", "items": ROW},
            "Summary": {
                "type": "object",
                "properties": {"Total": {"type": "number"}},
                "required": ["Total"],
            },
            "Notes": {"type": "string"},
        },
        "required": ["Account Number", "Transactions", "Summary"],
    }

    _FULL = {
        "Account Number": "1",
        "Transactions": [{"Date": "2024-01-01", "Description": "x", "Amount": 1.0}],
        "Summary": {"Total": 1.0},
    }

    def _svc(self):
        return _svc(agentic=True, schema=self._SCHEMA)

    def _section_model(self):
        return self._svc()._transport_model(self._SCHEMA, "Statement")

    def _shard_model(self):
        return self._svc()._shard_transport_model(self._SCHEMA, "Statement")

    @pytest.mark.parametrize(
        "label,field,value",
        [
            ("required array", "Transactions", None),
            ("required nested object", "Summary", None),
        ],
    )
    def test_the_whole_section_model_rejects_what_a_shard_is_told_to_send(
        self, label, field, value
    ):
        """The measurement the round cost comes from.

        Kept as its own assertion because the relaxation is only worth having while
        this is true: the whole-section model — which a single agent that saw every
        page is still held to — refuses the null a shard is instructed to produce.
        """
        import pydantic

        with pytest.raises(pydantic.ValidationError) as ei:
            self._section_model()(**{**self._FULL, field: value})
        assert {e["type"] for e in ei.value.errors()} <= {"list_type", "model_type"}, (
            label
        )

    @pytest.mark.parametrize(
        "label,payload_key",
        [
            ("required array", "Transactions"),
            ("required nested object", "Summary"),
            ("required scalar, already rescued by #782", "Account Number"),
        ],
    )
    def test_the_shard_model_accepts_null_for_a_field_outside_its_pages(
        self, label, payload_key
    ):
        self._shard_model()(**{**self._FULL, payload_key: None})

    @pytest.mark.parametrize(
        "label,payload",
        [
            ("an empty tool call", {}),
            (
                "a misspelled key set",
                {"Acount Number": "1", "Transactons": None, "Summry": None},
            ),
            (
                "an omitted required array",
                {"Account Number": "1", "Summary": {"Total": 1.0}},
            ),
            (
                "an omitted required object",
                {
                    "Account Number": "1",
                    "Transactions": [
                        {"Date": "2024-01-01", "Description": "x", "Amount": 1.0}
                    ],
                },
            ),
        ],
    )
    def test_the_shard_model_still_refuses_a_missing_key(self, label, payload):
        """``required`` still means the key must be PRESENT, only not populated.

        This is the half that must not relax. Dropping ``required`` from the shard
        model — the other way to make the two boundaries agree — would render every
        field ``Optional[...] = None``, so ``{}`` becomes a valid tool call and a
        shard that answered nothing merges as a success: the #666 whole-list loss,
        once per shard.
        """
        import pydantic

        with pytest.raises(pydantic.ValidationError) as ei:
            self._shard_model()(**payload)
        assert any(e["type"] == "missing" for e in ei.value.errors()), label

    def test_the_shard_model_still_enforces_a_row_count_floor(self):
        """``minItems`` is untouched, so the documented per-shard floor is unchanged.

        Relaxing presence and relaxing row counts are different decisions with
        different consequences — a required array is satisfiable by a shard holding
        none of its rows (``[]`` and ``null`` are both fine), a section-sized
        ``minItems`` is satisfiable by no payload a short shard can produce. Only the
        first is relaxed here.
        """
        import pydantic

        schema = json.loads(json.dumps(self._SCHEMA))
        schema["properties"]["Transactions"]["minItems"] = 100
        model = _svc(agentic=True, schema=schema)._shard_transport_model(
            schema, "Statement"
        )
        model(**{**self._FULL, "Transactions": None})  # out-of-shard: fine
        for rows in (0, 43):
            with pytest.raises(pydantic.ValidationError) as ei:
                model(**{**self._FULL, "Transactions": _rows(rows)})
            assert any(e["type"] == "too_short" for e in ei.value.errors()), rows

    def test_the_relaxation_is_confined_to_required_containers(self):
        """An optional container and a row's own required scalars are untouched.

        The transform is applied at every level a ``required`` list appears, so a
        row's required scalars are in its scope — they are already nullable from
        ``nullable_leaves_for_transport`` and must not become optional.
        """
        import pydantic

        model = self._shard_model()
        # Every top-level field still has to be present except the one the class
        # schema never required.
        required_names = {n for n, f in model.model_fields.items() if f.is_required()}
        assert "Notes" not in required_names
        assert len(required_names) == 3

        # A row whose own schema requires its keys still cannot omit them, and a
        # required scalar inside it takes null rather than becoming optional.
        schema = json.loads(json.dumps(self._SCHEMA))
        schema["properties"]["Transactions"]["items"]["required"] = [
            "Date",
            "Description",
            "Amount",
        ]
        strict_rows = _svc(agentic=True, schema=schema)._shard_transport_model(
            schema, "Statement"
        )
        strict_rows(
            **{
                **self._FULL,
                "Transactions": [
                    {"Date": "2024-01-01", "Description": None, "Amount": None}
                ],
            }
        )
        with pytest.raises(pydantic.ValidationError) as ei:
            strict_rows(**{**self._FULL, "Transactions": [{"Date": "2024-01-01"}]})
        assert any(e["type"] == "missing" for e in ei.value.errors())

    def test_both_shard_fan_out_sites_take_the_shard_model(self):
        """One rule, both routes. The in-process runtime and the Step Functions shard
        plan are separate code paths, and a fix applied to one of them is the defect
        class this repository keeps finding.
        """
        import inspect

        from idp_common.extraction import service as svc_mod

        src = inspect.getsource(svc_mod)
        assert src.count("self._shard_transport_model(") == 2
        assert "data_format=shard_model," in src
        plan = inspect.getsource(svc_mod.ExtractionService._build_agentic_shard_plan)
        assert "self._shard_transport_model(" in plan
        assert (
            "return model_id, shard_model, shard_payloads, custom_instruction" in plan
        )

    def test_the_merge_still_judges_the_section_by_the_real_rules(self):
        """Presence moves to the merge, it does not disappear.

        The shard relaxation would be a hole rather than a fix if the merged section
        were also judged by it. Two things keep that from happening: the merge
        normalises a list field to ``[]`` whatever the shards returned, and
        ``extraction.validation`` validates the merged result against the **real**
        class schema, where a null property reads as absent.
        """
        from idp_common.extraction.runtime import _merge_shard_results
        from idp_common.extraction.validation import validate_extraction

        shard_model = self._shard_model()
        cover_page = shard_model(
            **{"Account Number": None, "Transactions": None, "Summary": None}
        )
        rows_page = shard_model(
            **{
                "Account Number": "1",
                "Transactions": [
                    {"Date": "2024-01-01", "Description": "x", "Amount": 1.0}
                ],
                "Summary": None,
            }
        )
        merged, _metering, _conflicts = _merge_shard_results(
            [(cover_page, {}), (rows_page, {})], shard_model
        )
        # A list field merges to a real list even though a shard answered null.
        assert merged["Transactions"] == [
            {"Date": "2024-01-01", "Description": "x", "Amount": 1.0}
        ]
        # The required object no shard saw is REPORTED against the real schema.
        report = validate_extraction(merged, self._SCHEMA)
        assert not report.valid
        assert any("Summary" in e.message for e in report.errors)


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


class TestMaxItemsBoundsTheEvidence:
    """A declared ``maxItems`` is a ceiling on the OCR evidence (#1046, item 4).

    ``maxItems: 15`` over a 40-row table, correctly capped at 15, used to score
    ``15/41`` — the schema said the list holds at most fifteen rows, extraction
    obeyed it, and the check called the result truncated. The ceiling now bounds
    ``expected``, so that case is quiet while every schema that declares no
    ``maxItems`` behaves exactly as before.

    Only item 4 is addressed here. The section-wide sum, siblings of differing
    width, the nested-sub-list target, declared subsets, and both under-attribution
    items are unchanged, and ``TestWhyFailIsOptIn`` above still pins the default.

    Two invariants these tests exist to hold:

    * The ceiling can only ever SHRINK the evidence, so every value this reader
      cannot read must resolve to "no ceiling" rather than to a small number. The
      parametrised cases below probe that with twelve spellings, four of which
      (``True``, ``False``, ``"0"``, ``0``) would read as a ceiling of 0 or 1 under
      a naive ``int()`` and silence the check for the whole width group.
    * The ceiling applies to a width GROUP only when every member declares one,
      because ``extracted`` is summed over the group and ``expected`` is shared.
    """

    #: Item 4's own numbers: a 40-row 3-column table renders as 41 OCR rows
    #: (`_ocr_tables` counts the heading), and `maxItems: 15` correctly obeyed
    #: scored 15/41 = 0.366, under the 0.5 ratio.
    OCR_ROWS_FOR_40 = 41

    @staticmethod
    def _schema(max_items: Any = ..., *, second: Any = ...) -> dict:
        """``SCHEMA`` with a ``maxItems`` on ``Transactions``, and optionally a
        same-width sibling. ``...`` means the keyword is absent, which is the shape
        every shipped preset has."""
        txns: dict[str, Any] = {"type": "array", "items": ROW}
        if max_items is not ...:
            txns["maxItems"] = max_items
        props: dict[str, Any] = {"Account Number": {"type": "string"}}
        props["Transactions"] = txns
        if second is not ...:
            sib: dict[str, Any] = {"type": "array", "items": ROW}
            if second is not None:
                sib["maxItems"] = second
            props["Withdrawals"] = sib
        return {"type": "object", "properties": props}

    @staticmethod
    def _instance_schema(outer: Any = ..., inner: Any = ...) -> dict:
        """An array of INSTANCES whose items carry their own list — the shape
        ``_object_list_targets`` descends into, where the compared rows are the
        concatenation across instances."""
        inner_spec: dict[str, Any] = {"type": "array", "items": ROW}
        if inner is not ...:
            inner_spec["maxItems"] = inner
        outer_spec: dict[str, Any] = {
            "type": "array",
            "items": {"type": "object", "properties": {"Txns": inner_spec}},
        }
        if outer is not ...:
            outer_spec["maxItems"] = outer
        return {"type": "object", "properties": {"Accounts": outer_spec}}

    # ---- the case the issue names -----------------------------------------

    def test_a_list_correctly_capped_at_its_maxitems_is_not_flagged(self):
        svc = _svc(schema=self._schema(15))
        svc._document_text = _table(40)
        assert CODE not in _codes(_issues(svc, {"Transactions": _rows(15)}))

    def test_the_same_extraction_without_the_ceiling_is_still_flagged(self):
        """The control that makes the test above mean something: identical OCR and
        identical rows, and the only difference is the declared ceiling."""
        svc = _svc(schema=self._schema())
        svc._document_text = _table(40)
        issue = next(
            i for i in _issues(svc, {"Transactions": _rows(15)}) if i.code == CODE
        )
        assert issue.details["ocr_estimated_rows"] == self.OCR_ROWS_FOR_40
        assert issue.details["declared_max_items"] is None
        assert issue.details["ratio"] == round(15 / self.OCR_ROWS_FOR_40, 3)

    def test_under_fail_the_capped_extraction_no_longer_loses_the_document(self):
        """The user-visible point of the change, driven through the same tail that
        `TestRowShortfallOutcome` uses: opted in to `fail`, a list extracted to its
        declared ceiling neither reports the issue nor raises."""
        svc = _svc(schema=self._schema(15), row_shortfall_action="fail")
        svc._document_text = _table(40)
        issues = _issues(svc, {"Transactions": _rows(15)})
        assert CODE not in _codes(issues)
        doc = Document(
            id="d",
            input_key="d.pdf",
            input_bucket="in",
            output_bucket="out",
            status=Status.EXTRACTING,
        )
        # No raise: _fail_on_row_shortfall has no error-severity shortfall to act on.
        svc._fail_on_row_shortfall(doc, SimpleNamespace(processing_issues=issues), "1")

    # ---- the ceiling does not weaken a real detection ----------------------

    def test_a_ceiling_above_the_evidence_changes_nothing(self):
        """`maxItems: 5000` over an 800-row statement: the check that #726 exists
        for still fires, on the unbounded figure, with no cap wording."""
        svc = _svc(schema=self._schema(5000))
        svc._document_text = _table(800, pages=17)
        issue = next(
            i for i in _issues(svc, {"Transactions": _rows(43)}) if i.code == CODE
        )
        assert 800 <= issue.details["ocr_estimated_rows"] <= 800 + 17
        assert (
            issue.details["ocr_estimated_rows"]
            == issue.details["ocr_matched_table_rows"]
        )
        assert issue.details["declared_max_items"] == 5000
        assert "maxItems of the" not in issue.message

    def test_a_binding_ceiling_that_still_leaves_a_shortfall_fires_on_it(self):
        """The ceiling moves the denominator; it does not switch the check off.
        200 OCR rows, a declared ceiling of 100, 20 rows extracted."""
        svc = _svc(schema=self._schema(100))
        svc._document_text = _table(200)
        issue = next(
            i for i in _issues(svc, {"Transactions": _rows(20)}) if i.code == CODE
        )
        assert issue.details["ocr_matched_table_rows"] == 201
        assert issue.details["ocr_estimated_rows"] == 100
        assert issue.details["declared_max_items"] == 100
        assert issue.details["ratio"] == 0.2
        # Both figures are stated, so the message is not read as an OCR count.
        assert "about 201 rows" in issue.message
        assert "bounded to 100 by the declared maxItems" in issue.message
        assert "(capped at maxItems 100)" in issue.root_cause

    @pytest.mark.parametrize(
        "ceiling,fires",
        [(..., True), (5000, True), (100, True), (15, False), (0, False)],
        ids=["absent", "above-evidence", "binding-but-live", "below-floor", "zero"],
    )
    def test_the_ceiling_never_raises_the_expected_figure(self, ceiling, fires):
        """The direction, over every class of ceiling: absent, above the evidence,
        binding-but-live, below the floor, and zero.

        Whether each class fires is parametrised rather than left to a loop that may
        not execute. It was written as `for issue in ...: if code: assert`, and for
        the last two rows that body ran zero times — a parametrisation whose two most
        interesting cases asserted nothing. Measured, not reasoned: the count of
        matching issues is 1, 1, 1, 0, 0 across these five rows.
        """
        svc = _svc(schema=self._schema(ceiling))
        svc._document_text = _table(200)
        matching = [
            i for i in _issues(svc, {"Transactions": _rows(20)}) if i.code == CODE
        ]
        assert bool(matching) is fires
        for issue in matching:
            assert (
                issue.details["ocr_estimated_rows"]
                <= issue.details["ocr_matched_table_rows"]
            )

    def test_a_ceiling_below_the_floor_drops_the_group_rather_than_clamping(self):
        """The sub-30 consequence, pinned where a shortfall against the CEILING is
        severe: 2 rows of a declared 15 over a 40-row table.

        Clamping the denominator up to `_OCR_ROW_ESTIMATE_MIN` instead of letting the
        group fall below the floor leaves every other test in this class green, and it
        would report "bounded to 30" — a figure no schema declares. So the cost of the
        opt-out is asserted here rather than only described: a 13-of-15 loss relative
        to the ceiling is invisible, which is the price of `maxItems` being able to
        take a group-shaped field out of the check at all.
        """
        svc = _svc(schema=self._schema(15))
        svc._document_text = _table(40)
        issues = _issues(svc, {"Transactions": _rows(2)})
        assert CODE not in _codes(issues)
        assert all(str(self.OCR_ROWS_FOR_40) not in (i.message or "") for i in issues)
        assert all("bounded to" not in (i.message or "") for i in issues)
        # And the control: without the ceiling the same 2-of-40 loss is reported.
        bare = _svc(schema=self._schema())
        bare._document_text = _table(40)
        assert CODE in _codes(_issues(bare, {"Transactions": _rows(2)}))

    # ---- what counts as a declared ceiling --------------------------------

    @pytest.mark.parametrize(
        "ceiling",
        [15, "15", " 15 ", 15.0, "15.0", "1.5e1"],
        ids=["int", "web-ui-string", "padded", "integral-float", "float-string", "sci"],
    )
    def test_the_forms_a_config_round_trip_produces_are_read(self, ceiling):
        """`ConfigurationRecord._stringify_values` stringifies every numeric scalar
        into the Configuration table, so a class authored in the Web UI arrives with
        `maxItems: "15"`. `_get_class_schema` coerces at that entry point, but
        `_class_schema` is reachable without it — as it is here — so the reader takes
        the string form itself."""
        svc = _svc(schema=self._schema(ceiling))
        svc._document_text = _table(40)
        assert CODE not in _codes(_issues(svc, {"Transactions": _rows(15)}))

    def test_a_decimal_is_read(self):
        """The shape a DynamoDB number takes on the way back out."""
        from decimal import Decimal

        svc = _svc(schema=self._schema(Decimal("15")))
        svc._document_text = _table(40)
        assert CODE not in _codes(_issues(svc, {"Transactions": _rows(15)}))

    @pytest.mark.parametrize(
        "ceiling",
        [
            True,
            False,
            "abc",
            "",
            "   ",
            15.5,
            "15.5",
            -1,
            "-5",
            float("inf"),
            float("nan"),
            [40],
            {"maxItems": 40},
            None,
        ],
        ids=[
            "true",
            "false",
            "words",
            "empty",
            "blank",
            "fractional",
            "fractional-string",
            "negative",
            "negative-string",
            "infinity",
            "nan",
            "list",
            "dict",
            "null",
        ],
    )
    def test_an_unreadable_ceiling_leaves_the_evidence_alone(self, ceiling):
        """Capability, not pattern-matching: the rule is that a value this reader
        cannot turn into a finite non-negative whole row count is NOT a ceiling, so
        the pre-existing behaviour stands.

        Four of these discriminate a real implementation choice rather than a
        hypothetical. `True`/`False` are `int`s in Python (`int(True) == 1`), and
        `int(float("nan"))` raises, so a reader that coerces before checking
        finiteness takes the section's whole issue list down with it — which is
        exactly the #797 failure mode. Each would show up here as the check going
        silent (or erroring), so the assertion is that it still fires.
        """
        svc = _svc(schema=self._schema(ceiling))
        svc._document_text = _table(40)
        issue = next(
            i for i in _issues(svc, {"Transactions": _rows(15)}) if i.code == CODE
        )
        assert issue.details["declared_max_items"] is None
        assert issue.details["ocr_estimated_rows"] == self.OCR_ROWS_FOR_40

    @pytest.mark.parametrize("ceiling", [0, "0"])
    def test_a_zero_ceiling_is_honoured_rather_than_discarded(self, ceiling):
        """`maxItems: 0` says the list holds no rows at all, so no OCR evidence is
        evidence about it.

        What this pins is the `bool` guard, and it is worth being exact about which
        half is observable. `False` and `0` are `==` in Python, and the difference
        between them here IS visible: `False` reads as no ceiling and the check
        fires, `0` reads as a ceiling and it does not. The precise VALUE of a ceiling
        under `_OCR_ROW_ESTIMATE_MIN` is not observable through any surface — every
        ceiling in 0..29 drops the group before a ratio or a `details` dict exists —
        so this asserts the boundary it can see rather than a number it cannot.
        """
        svc = _svc(schema=self._schema(ceiling))
        svc._document_text = _table(40)
        assert CODE not in _codes(_issues(svc, {"Transactions": _rows(15)}))

    def test_an_under_declared_ceiling_weakens_the_check_in_proportion(self):
        """The cost of the ceiling, stated as a test rather than left implicit.

        The ceiling IS the denominator, so a ceiling lower than the rows a document
        really holds shrinks what counts as a shortfall: 43 rows out of an 800-row
        statement fire against the OCR evidence and do not fire against a declared
        `maxItems: 80`. That is the semantics the config author asked for — 43 is
        more than half of 80 — and it is why the user documentation tells a reader to
        declare a ceiling their longest expected document can reach rather than to
        use `maxItems` as a tuning knob for this check.
        """
        svc = _svc(schema=self._schema(80))
        svc._document_text = _table(800, pages=17)
        assert CODE not in _codes(_issues(svc, {"Transactions": _rows(43)}))
        # The control: the same OCR and the same rows, no ceiling declared.
        bare = _svc(schema=self._schema())
        bare._document_text = _table(800, pages=17)
        assert CODE in _codes(_issues(bare, {"Transactions": _rows(43)}))

    @pytest.mark.parametrize(
        "ceiling",
        [10**400, str(10**400), "9" * 401],
        ids=["huge-int", "huge-int-string", "401-digit-string"],
    )
    def test_an_unconvertibly_large_ceiling_neither_raises_nor_binds(self, ceiling):
        """`float()` raises `OverflowError` on an integer too large to convert, and
        `OverflowError` is neither `TypeError` nor `ValueError`.

        That is reachable through the documented Web-UI round trip — the value is
        stringified into the Configuration table and `coerce_numeric_schema_keywords`
        turns it back into an unbounded Python `int` — and an exception here costs the
        section its whole processing-issue list, because `_save_results` calls
        `_build_extraction_issues` with no enclosing `try`. So the reader decides by
        `int()`, which has no such limit, and a ceiling far above any OCR row count
        simply does not bind.
        """
        svc = _svc(schema=self._schema(ceiling))
        svc._document_text = _table(800, pages=17)
        issue = next(
            i for i in _issues(svc, {"Transactions": _rows(43)}) if i.code == CODE
        )
        assert issue.details["declared_max_items"] == int(ceiling)
        assert (
            issue.details["ocr_estimated_rows"]
            == issue.details["ocr_matched_table_rows"]
        )

    def test_a_ceiling_whose_conversion_raises_is_not_a_ceiling(self):
        """Capability, stated as capability: the rule is not a list of known-bad
        values, it is "can this be read as a whole non-negative row count".

        A schema is JSON in production, so this object is not a shape the
        Configuration table can hold — it is here because it is the general case the
        broad `except` exists for, and because an exception escaping this method is
        the #797 failure mode rather than a lost warning.
        """

        class Hostile:
            def __int__(self):
                raise ZeroDivisionError("no")

            def __float__(self):
                raise ZeroDivisionError("no")

            def __eq__(self, other):
                raise ZeroDivisionError("no")

        assert ExtractionService._declared_max_items({"maxItems": Hostile()}) is None
        svc = _svc(schema=self._schema(Hostile()))
        svc._document_text = _table(40)
        issue = next(
            i for i in _issues(svc, {"Transactions": _rows(15)}) if i.code == CODE
        )
        assert issue.details["declared_max_items"] is None

    # ---- the group rule ---------------------------------------------------

    def test_the_group_ceiling_is_the_sum_of_its_members(self):
        """`extracted` is summed across a width group, so the ceiling must be too:
        two 20-row-max siblings holding 20 rows each are complete at 40."""
        svc = _svc(schema=self._schema(20, second=20))
        svc._document_text = _table(100)
        assert CODE not in _codes(
            _issues(svc, {"Transactions": _rows(20), "Withdrawals": _rows(20)})
        )

    def test_the_summed_ceiling_is_still_a_denominator(self):
        """Same two ceilings, a real shortfall against them."""
        svc = _svc(schema=self._schema(20, second=20))
        svc._document_text = _table(100)
        issue = next(
            i
            for i in _issues(svc, {"Transactions": _rows(5), "Withdrawals": _rows(5)})
            if i.code == CODE
        )
        assert issue.details["declared_max_items"] == 40
        assert issue.details["ocr_estimated_rows"] == 40
        assert issue.details["extracted_rows"] == 10
        assert issue.details["list_fields"] == ["Transactions", "Withdrawals"]

    def test_one_undeclared_sibling_leaves_the_group_unbounded(self):
        """A group is only bounded if every member is. The undeclared sibling may
        legitimately hold the rest of the shared table, so shrinking the evidence
        to the one declared ceiling would hide a real loss in the other."""
        svc = _svc(schema=self._schema(20, second=None))
        svc._document_text = _table(100)
        issue = next(
            i
            for i in _issues(svc, {"Transactions": _rows(20), "Withdrawals": _rows(20)})
            if i.code == CODE
        )
        assert issue.details["declared_max_items"] is None
        assert issue.details["ocr_estimated_rows"] == 101

    def test_an_empty_member_still_decides_whether_the_group_is_bounded(self):
        """The "every member declares one" rule is about the SCHEMA, not about which
        members happened to return rows.

        Reading `caps` only from the members that contributed rows leaves this class
        green everywhere else and masks a total loss: the declared sibling here came
        back empty, so restricting the ceiling to the contributing member would bound
        the group at that member's 20 and go silent, where the group is in fact
        unbounded because the empty member declares nothing.
        """
        # The discriminating orientation: the DECLARED member is the one that
        # contributed rows and the UNDECLARED one came back empty. Reading `caps`
        # from the contributors alone then sees only the 20, bounds the group there,
        # and goes silent below the floor — while the group is in fact unbounded,
        # because the empty sibling may hold the rest of the shared table.
        svc = _svc(schema=self._schema(20, second=None))
        svc._document_text = _table(100)
        issue = next(
            i
            for i in _issues(svc, {"Transactions": _rows(20), "Withdrawals": []})
            if i.code == CODE
        )
        assert issue.details["declared_max_items"] is None
        assert issue.details["ocr_estimated_rows"] == 101
        assert issue.details["list_fields"] == ["Transactions"]
        # And the other way round, which is unbounded for a different reason: the
        # contributor is the undeclared one.
        other = _svc(schema=self._schema(20, second=None))
        other._document_text = _table(100)
        flipped = next(
            i
            for i in _issues(other, {"Transactions": [], "Withdrawals": _rows(20)})
            if i.code == CODE
        )
        assert flipped.details["declared_max_items"] is None
        assert flipped.details["list_fields"] == ["Withdrawals"]

    def test_a_ceiling_on_a_list_of_another_width_does_not_bound_this_group(self):
        """Widths are separate groups, so a 2-property list's ceiling has nothing to
        say about a 3-property one."""
        schema = self._schema()
        schema["properties"]["Pairs"] = {
            "type": "array",
            "maxItems": 1,
            "items": {
                "type": "object",
                "properties": {"k": {"type": "string"}, "v": {"type": "string"}},
            },
        }
        svc = _svc(schema=schema)
        svc._document_text = _table(40)
        issue = next(
            i
            for i in _issues(svc, {"Transactions": _rows(15), "Pairs": [{"k": "a"}]})
            if i.code == CODE and i.details["item_property_count"] == 3
        )
        assert issue.details["declared_max_items"] is None

    # ---- the instance-array shape -----------------------------------------

    def test_a_per_instance_ceiling_alone_does_not_bound_the_concatenation(self):
        """The compared rows for `Accounts[].Txns` are every instance's rows
        concatenated, and nothing declares how many instances there are, so a
        per-instance `maxItems` is not a bound on that total. Reading it as one — or
        multiplying it by the instances that happen to have been extracted — shrinks
        the evidence in proportion to how many instances extraction LOST."""
        svc = _svc(schema=self._instance_schema(inner=5))
        svc._document_text = _table(40)
        fields = {"Accounts": [{"Txns": _rows(5)} for _ in range(3)]}
        issue = next(i for i in _issues(svc, fields) if i.code == CODE)
        assert issue.details["list_fields"] == ["Accounts[].Txns"]
        assert issue.details["extracted_rows"] == 15
        assert issue.details["declared_max_items"] is None
        assert issue.details["ocr_estimated_rows"] == self.OCR_ROWS_FOR_40

    def test_both_ceilings_declared_do_bound_the_concatenation(self):
        """With the instance count bounded as well, the product IS a declared bound
        on the concatenated rows: at most 3 accounts of at most 5 rows is 15."""
        svc = _svc(schema=self._instance_schema(outer=3, inner=5))
        svc._document_text = _table(40)
        fields = {"Accounts": [{"Txns": _rows(5)} for _ in range(3)]}
        assert CODE not in _codes(_issues(svc, fields))

    def test_the_targets_carry_the_product_and_the_walk_still_returns_them(self):
        """The bound is part of `_object_list_targets`' contract, asserted directly
        so the product rule is pinned at its source rather than only through the
        ratio."""
        targets = ExtractionService._object_list_targets(
            self._instance_schema(outer=3, inner=5), {"Accounts": []}
        )
        assert targets == [("Accounts[].Txns", 3, [], 15)]
        assert ExtractionService._object_list_targets(
            self._instance_schema(inner=5), {"Accounts": []}
        ) == [("Accounts[].Txns", 3, [], None)]
        assert ExtractionService._object_list_targets(
            self._schema(15), {"Transactions": []}
        ) == [("Transactions", 3, [], 15)]

    # ---- what ships ------------------------------------------------------

    def test_no_shipped_preset_declares_a_ceiling_so_nothing_shipped_changes(self):
        """Read from the config library rather than asserted from memory: if a preset
        ever declares `maxItems`, this change starts altering shipped behaviour and
        `TestWhyFailIsOptIn`'s account_summary case has to be re-read against it.

        The walk is over every array spec in every class of every tracked YAML file
        under `config_library`, including the ones behind a `$ref` in `$defs`, so it
        cannot miss one by looking only at the top level. It is every tracked `.yaml`
        rather than every `config.yaml`, and the last assertion here is what keeps it
        that way: the `config.yaml`-only glob a first version used reaches strictly
        fewer schema-carrying files, so narrowing it back fails. That comparison is
        relational rather than a count, because a count goes stale the next time a
        preset is added or removed.
        """
        import subprocess

        import yaml

        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        listed = subprocess.run(
            [
                "git",
                "ls-files",
                "config_library/*.yaml",
                "config_library/**/*.yaml",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        assert len(listed) >= 28, (
            "fewer tracked config_library YAML files than the 28 this walk was "
            f"written against ({len(listed)}): the glob has stopped matching them"
        )

        def _arrays(node: Any):
            if isinstance(node, dict):
                if node.get("type") == "array":
                    yield node
                for v in node.values():
                    yield from _arrays(v)
            elif isinstance(node, list):
                for v in node:
                    yield from _arrays(v)

        declared = []
        seen = 0
        classed = 0
        for rel in listed:
            cfg = yaml.safe_load((Path(root) / rel).read_text(encoding="utf-8"))
            if not isinstance(cfg, dict):
                continue
            classes = cfg.get("classes") or []
            if classes:
                classed += 1
            for cls in classes:
                for spec in _arrays(cls):
                    seen += 1
                    if ExtractionService._declared_max_items(spec) is not None:
                        declared.append(f"{rel}:{cls.get('$id')}")
        assert seen >= 9, (
            f"only {seen} array field(s) found across {len(listed)} preset file(s); "
            "the nine group-shaped arrays issue #1046 names are the floor, so this "
            "walk is no longer reaching the schemas"
        )
        assert classed >= 4, (
            f"only {classed} preset file(s) carry a `classes:` block; the walk is "
            "reading files without schemas and would pass vacuously"
        )
        narrow = subprocess.run(
            ["git", "ls-files", "config_library/**/config.yaml"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        narrow_classed = sum(
            1
            for rel in narrow
            if (
                isinstance(
                    (
                        c := yaml.safe_load(
                            (Path(root) / rel).read_text(encoding="utf-8")
                        )
                    ),
                    dict,
                )
                and (c.get("classes") or [])
            )
        )
        assert classed > narrow_classed, (
            "this walk no longer reaches more schema-carrying preset files than a "
            f"`config_library/**/config.yaml` glob would ({classed} vs "
            f"{narrow_classed}), so it has been narrowed back to the form that left "
            "the non-`config.yaml` presets unwatched"
        )
        assert not declared, (
            "a shipped preset now declares maxItems on an array field, so this "
            "change is no longer behaviour-neutral on shipped config: "
            f"{sorted(set(declared))}. Re-read TestWhyFailIsOptIn against it."
        )
