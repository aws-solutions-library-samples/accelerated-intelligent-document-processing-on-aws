# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A response the model did not finish is reported (#1361).

An extraction whose output was cut off mid-answer holds a PREFIX of the data: the
scalars and the rows that arrived are correct, the JSON is well formed, every
accuracy and confidence signal is computed over what came back, and the document
completes. ``output_truncated`` already recorded this — into section metadata that
no report, no status path and no UI read. This pins the two halves that make it
visible:

* the flag is set from the response's own ``stopReason``, not only from a JSON
  parse failure, so a ceiling hit whose text still parses is seen; and
* ``_build_extraction_issues`` emits ``extraction_output_truncated``, so the
  section carries the finding instead of completing with nothing recorded.

⚠️ **A clean ``stopReason`` is not evidence that the output is complete, and the
measurements behind #1361 are the reason to say so here.** On a 17-page, 800-row
statement the short Simple-mode runs spent ~2,290-2,477 extraction output tokens
where the complete runs on the SAME model spent ~38,333-38,535
(``benchmarks/results/v0.6.11/coresynth__extraction-model-sonnet5``), and
``_invoke_extraction_model`` passes ``max_tokens=None`` so the client requests the
model's maximum. No ceiling was involved in those runs: the model abbreviated and
stopped with ``end_turn``. ``extraction_rows_below_ocr_estimate`` (#843) is what
covers that shape, and ``TestAStopReasonIsNotACompletenessCheck`` holds both
statements in one place so neither check is read as doing the other's job.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from idp_common.bedrock.openai_responses import (
    _RESPONSES_API_MODELS,
    is_openai_responses_model,
    translate_response,
)
from idp_common.config.models import IDPConfig
from idp_common.discovery.rules_discovery import RulesDiscovery
from idp_common.extraction.service import (
    ExtractionResult,
    ExtractionService,
    SectionInfo,
)
from idp_common.models import Document, Section, Status
from idp_common.utils.bedrock_utils import (
    OUTPUT_TRUNCATION_STOP_REASONS,
    is_output_token_truncation,
)

CODE = ExtractionService.OUTPUT_TRUNCATED_CODE
ROW_SHORTFALL_CODE = ExtractionService.ROW_SHORTFALL_CODE

#: The stop reasons are read from the module that defines them rather than restated,
#: so a value added there is exercised here without an edit. That the derived set is
#: non-empty gets its own test
#: (``TestTheMatcher::test_the_stop_reason_vocabulary_is_not_empty``) rather than
#: being left to the parametrisations, because an empty ``parametrize`` collects as a
#: **skip** — it would void every assertion below while reporting no failure.

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


def _rows(n: int) -> list[dict]:
    return [
        {
            "Date": "2024-01-01",
            "Description": f"PURCHASE SEQ{i:05d}",
            "Amount": float(i),
        }
        for i in range(n)
    ]


def _ocr(rows: int, pages: int = 17) -> str:
    """Textract-``TABLES``-like Markdown: a 3-column ledger reprinted per page."""
    per = -(-rows // pages)
    blocks = []
    for p in range(pages):
        lines = ["| Date | Description | Amount |", "|---|---|---|"]
        for i in range(p * per, min(rows, (p + 1) * per)):
            lines.append(f"| 2024-01-01 | PURCHASE SEQ{i:05d} | {i}.00 |")
        blocks.append("\n".join(lines))
    return "\n\nPage break.\n\n".join(blocks)


def _section_info() -> SectionInfo:
    return SectionInfo(
        class_label="Statement",
        sorted_page_ids=["1"],
        page_indices=[0],
        output_bucket="out",
        output_key="d.pdf/sections/1/result.json",
        output_uri="s3://out/d.pdf/sections/1/result.json",
        start_page=1,
        end_page=1,
    )


def _response(text: str, stop_reason: str) -> dict:
    """The envelope ``bedrock.invoke_model`` returns, with a chosen stop reason."""
    return {
        "response": {
            "stopReason": stop_reason,
            "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
            "usage": {"inputTokens": 10, "outputTokens": 20},
        },
        "metering": {"Extraction/bedrock/m": {"outputTokens": 20, "requests": 1}},
    }


def _invoke(svc: ExtractionService, text: str, stop_reason: str) -> ExtractionResult:
    """Drive the REAL ``_invoke_extraction_model`` over a chosen response."""
    with patch(
        "idp_common.extraction.service.bedrock.invoke_model",
        return_value=_response(text, stop_reason),
    ):
        return svc._invoke_extraction_model(
            content=[{"text": "prompt"}],
            system_prompt="sys",
            section_info=_section_info(),
        )


def _save(svc: ExtractionService, result: ExtractionResult):
    """Persist ``result`` through the real tail; returns (write mock, doc, section)."""
    doc = Document(
        id="d",
        input_key="d.pdf",
        input_bucket="in",
        output_bucket="out",
        status=Status.EXTRACTING,
    )
    section = Section(section_id="1", classification="Statement", page_ids=["1"])
    doc.sections = [section]
    with patch("idp_common.extraction.service.s3.write_content") as write:
        svc._save_results(doc, section, result, _section_info(), "1", 0.0)
    return write, doc, section


def _codes(issues) -> list[str]:
    return [i.code for i in issues or []]


COMPLETE_JSON = json.dumps({"Account Number": "1", "Transactions": _rows(43)})
#: A response cut off mid-row: unparseable, and the repair reader can see why.
CUT_OFF_JSON = COMPLETE_JSON[: COMPLETE_JSON.index("SEQ00030")]


class TestTheStopReasonIsRead:
    """``_invoke_extraction_model`` must read the model's own verdict.

    Before this, ``output_truncated`` was set in ONE place — the ``except`` branch
    around ``json.loads``. A ceiling hit whose text still parses never reaches that
    branch, so the flag stayed False for a response the model had said it did not
    finish.
    """

    @pytest.mark.parametrize("stop_reason", list(OUTPUT_TRUNCATION_STOP_REASONS))
    def test_a_ceiling_stop_is_seen_even_when_the_text_parses(self, stop_reason):
        svc = _svc()
        result = _invoke(svc, COMPLETE_JSON, stop_reason)
        assert result.parsing_succeeded, (
            "the fixture must parse cleanly, or this test measures the repair "
            "branch instead of the stop-reason read"
        )
        assert result.output_truncated is True, (
            f"stopReason={stop_reason!r} with parseable text must still be "
            f"recorded as truncated"
        )

    def test_a_finished_response_is_not_marked_truncated(self):
        svc = _svc()
        result = _invoke(svc, COMPLETE_JSON, "end_turn")
        assert result.parsing_succeeded
        assert result.output_truncated is False

    def test_the_log_names_the_section_and_the_stop_reason(self, caplog):
        """The extraction log is the operator-facing half of this detection.

        Asserted because a random-sample mutation that turned the whole log
        branch off survived the suite: a warning nothing reads back is
        indistinguishable from no warning at all.

        ⚠️ The identifiers come from ``SectionInfo``, which carries **no**
        ``section_id`` — so a ``getattr(section_info, "section_id", None)`` here
        would log ``None`` on every call, which is what the pre-existing
        ``_simple_mode_input_preflight`` log does. The class label and the result
        URI are real fields and the URI contains the section id.
        """
        info = _section_info()
        assert not hasattr(info, "section_id"), (
            "SectionInfo has grown a section_id: log it directly instead of the URI"
        )
        svc = _svc()
        with caplog.at_level("WARNING", logger="idp_common.extraction.service"):
            _invoke(svc, COMPLETE_JSON, "max_tokens")
        lines = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        hit = [ln for ln in lines if "output token ceiling" in ln]
        assert hit, lines
        assert info.class_label in hit[0]
        assert info.output_uri in hit[0]
        assert "stopReason=max_tokens" in hit[0]

    def test_nothing_is_logged_for_a_finished_response(self, caplog):
        svc = _svc()
        with caplog.at_level("WARNING", logger="idp_common.extraction.service"):
            _invoke(svc, COMPLETE_JSON, "end_turn")
        assert not [
            r for r in caplog.records if "output token ceiling" in r.getMessage()
        ]

    def test_the_repair_readers_verdict_still_stands_on_its_own(self):
        """The pre-existing path: unparseable text, an ordinary stop reason."""
        svc = _svc()
        result = _invoke(svc, CUT_OFF_JSON, "end_turn")
        assert result.output_truncated is True
        assert result.output_repaired, "the fixture must exercise the repair reader"

    def test_a_ceiling_stop_survives_a_successful_repair(self):
        """Both sources agree; the OR must not let the second overwrite the first."""
        svc = _svc()
        result = _invoke(svc, CUT_OFF_JSON, "max_tokens")
        assert result.output_truncated is True

    def test_a_ceiling_stop_survives_a_repair_that_reports_nothing(self):
        """The direction the OR exists for: repair says ``was_truncated: False``.

        Assigning rather than ORing would erase the model's own verdict here, and
        this is the shape that does it — the text does not parse, so the branch
        runs, and the reader's answer is False.
        """
        svc = _svc()
        with patch(
            "idp_common.extraction.service.repair_truncated_json",
            return_value=({"Account Number": "1"}, {"was_truncated": False}),
        ):
            result = _invoke(svc, "not json at all", "max_tokens")
        assert result.output_truncated is True


class TestTheFindingReachesTheSection:
    """A flag nothing reads is not a signal. This is the half that reports it."""

    def test_a_truncated_result_carries_the_issue(self):
        svc = _svc()
        write, doc, section = _save(
            svc,
            ExtractionResult(
                extracted_fields={"Account Number": "1", "Transactions": _rows(43)},
                metering={},
                parsing_succeeded=True,
                total_duration=1.0,
                output_truncated=True,
            ),
        )
        assert CODE in _codes(section.processing_issues), _codes(
            section.processing_issues
        )
        issue = next(i for i in section.processing_issues if i.code == CODE)
        assert issue.severity == "warning"
        assert issue.stage == "extraction"
        assert issue.details["list_fields_present"] == ["Transactions"]
        # The section it belongs to. A random-sample mutation that dropped this
        # attribution survived the suite, which is how it came to be asserted:
        # an issue with no section is one the UI cannot place on a multi-section
        # document.
        assert issue.section_id == "1"
        # ``root_cause`` is what an operator groups by when the same thing happens
        # across a batch, so it has to name the mode, the model and the condition.
        # Asserted WHOLE rather than by fragments: three separate random-sample
        # mutations survived a fragment-wise version of this (`"with model "`, the
        # unrepaired suffix's `else ""`, and a clause of the remedy wording), all
        # of them the same weak class — a string between two asserted strings.
        # The expected value is BUILT from the service's own configured model, so
        # it does not pin whatever the shipped default happens to be today.
        assert issue.root_cause == (
            f"traditional extraction with model {self._model_of(svc)}; "
            f"response truncated before completion"
        )
        # Durable, not merely in memory: this is what a reader opens.
        persisted = write.call_args.args[0]["metadata"]["processing_issues"]
        assert [i for i in persisted if i["code"] == CODE]

    @staticmethod
    def _model_of(svc: ExtractionService) -> str:
        """The configured extraction model, read from the service under test.

        Derived rather than written out, so the assertion does not pin whatever
        the shipped default happens to be today.
        """
        model = svc.config.extraction.model
        assert model, "the service has no extraction model configured"
        return str(model)

    def test_the_root_cause_names_the_agentic_mode_when_that_is_the_mode(self):
        svc = _svc(agentic=True)
        _w, _d, section = _save(
            svc,
            ExtractionResult(
                extracted_fields={"Account Number": "1", "Transactions": _rows(43)},
                metering={},
                parsing_succeeded=True,
                total_duration=1.0,
                output_truncated=True,
            ),
        )
        issue = next(i for i in section.processing_issues if i.code == CODE)
        assert "agentic extraction" in issue.root_cause
        assert "traditional" not in issue.root_cause

    def test_a_repaired_result_says_so_and_names_the_repair(self):
        svc = _svc()
        _w, _d, section = _save(
            svc,
            ExtractionResult(
                extracted_fields={"Account Number": "1", "Transactions": _rows(43)},
                metering={},
                parsing_succeeded=True,
                total_duration=1.0,
                output_truncated=True,
                output_repaired=True,
                repair_method="close_structures",
            ),
        )
        issue = next(i for i in section.processing_issues if i.code == CODE)
        assert issue.details["output_repaired"] is True
        assert issue.details["repair_method"] == "close_structures"
        assert issue.root_cause.endswith(
            "response truncated before completion; repaired by close_structures"
        )

    def test_an_unrepaired_result_does_not_claim_a_repair(self):
        svc = _svc()
        _w, _d, section = _save(
            svc,
            ExtractionResult(
                extracted_fields={"Account Number": "1", "Transactions": _rows(43)},
                metering={},
                parsing_succeeded=True,
                total_duration=1.0,
                output_truncated=True,
            ),
        )
        issue = next(i for i in section.processing_issues if i.code == CODE)
        assert issue.details["output_repaired"] is False
        assert issue.details["repair_method"] is None
        # Ends there — an absence assertion alone let a mutation of the `else ""`
        # branch grow an arbitrary suffix and still pass.
        assert issue.root_cause.endswith("response truncated before completion")
        assert "repaired by" not in issue.root_cause

    def test_a_complete_result_carries_no_truncation_issue(self):
        svc = _svc()
        _write, _doc, section = _save(
            svc,
            ExtractionResult(
                extracted_fields={"Account Number": "1", "Transactions": _rows(43)},
                metering={},
                parsing_succeeded=True,
                total_duration=1.0,
                output_truncated=False,
            ),
        )
        assert CODE not in _codes(section.processing_issues)

    def test_the_message_names_a_remedy_and_says_why_nothing_else_flags_it(self):
        svc = _svc()
        _w, _d, section = _save(
            svc,
            ExtractionResult(
                extracted_fields={"Account Number": "1", "Transactions": _rows(43)},
                metering={},
                parsing_succeeded=True,
                total_duration=1.0,
                output_truncated=True,
            ),
        )
        msg = next(i for i in section.processing_issues if i.code == CODE).message
        assert "cut off before it finished" in msg
        # Both halves of the Simple-mode remedy: what went wrong and what to do.
        assert "one response per section" in msg
        assert "Advanced" in msg and "shards" in msg
        assert "split the document" in msg
        # Why no other signal flags it — the reason the issue exists at all.
        assert "confidence" in msg
        assert "Transactions" in msg

    def test_advanced_mode_is_not_told_to_switch_to_advanced_mode(self):
        svc = _svc(agentic=True)
        _w, _d, section = _save(
            svc,
            ExtractionResult(
                extracted_fields={"Account Number": "1", "Transactions": _rows(43)},
                metering={},
                parsing_succeeded=True,
                total_duration=1.0,
                output_truncated=True,
            ),
        )
        msg = next(i for i in section.processing_issues if i.code == CODE).message
        assert "use Advanced" not in msg
        # The whole clause. Pinning only one half left a mutation of the other
        # alive through the random sample, twice.
        assert msg.endswith(
            " Reduce the rows a single shard is asked for (extraction.agentic "
            "max_pages_per_shard) or split the document."
        )

    def test_the_warning_does_not_fail_the_section(self):
        """Severity is ``warning``, always — the scope line of this change.

        ``extraction.row_shortfall_action`` governs ``ROW_SHORTFALL_CODE`` only, so
        a deployment that has opted into ``fail`` must not start failing runs that
        complete today because of a code it never chose.
        """
        cfg = IDPConfig(
            **{
                "extraction": {
                    "mode": "simple",
                    "agentic": {"enabled": False},
                    "row_shortfall_action": "fail",
                }
            }
        )
        svc = ExtractionService(config=cfg)
        svc._reset_context()
        svc._class_schema = SCHEMA
        # No OCR text, so the row-shortfall check cannot fire and the only issue
        # under test is this one.
        _w, doc, section = _save(
            svc,
            ExtractionResult(
                extracted_fields={"Account Number": "1", "Transactions": _rows(43)},
                metering={},
                parsing_succeeded=True,
                total_duration=1.0,
                output_truncated=True,
            ),
        )
        issue = next(i for i in section.processing_issues if i.code == CODE)
        assert issue.severity == "warning"
        assert doc.errors == []


class TestAStopReasonIsNotACompletenessCheck:
    """The two checks answer different questions, and #1361's runs prove it.

    Measured on ``large_narrow.pdf`` (800 rows, 17 pages) at v0.6.11: the short
    Simple-mode runs used ~2.3k extraction output tokens against complete runs'
    ~38.5k on the same model, with ``max_tokens=None`` requesting the model's
    maximum — so those runs stopped voluntarily and no stop-reason check can see
    them. Each check must cover its own case and neither may be read as covering
    the other's.
    """

    def test_a_voluntary_stop_losing_most_rows_is_caught_by_the_row_check_only(self):
        svc = _svc()
        svc._document_text = _ocr(800)
        result = _invoke(svc, COMPLETE_JSON, "end_turn")
        assert result.output_truncated is False
        _w, _d, section = _save(svc, result)
        codes = _codes(section.processing_issues)
        assert CODE not in codes, (
            "a voluntary stop must not be reported as a ceiling hit — that would "
            "be a false cause on the one issue a reader opens for the cause"
        )
        assert ROW_SHORTFALL_CODE in codes, codes

    def test_both_fire_together_when_both_are_true(self):
        """Overlap is deliberate: one names the cause, the other counts the rows."""
        svc = _svc()
        svc._document_text = _ocr(800)
        result = _invoke(svc, COMPLETE_JSON, "max_tokens")
        _w, _d, section = _save(svc, result)
        codes = _codes(section.processing_issues)
        assert CODE in codes and ROW_SHORTFALL_CODE in codes, codes

    def test_truncation_is_reported_with_no_ocr_text_at_all(self):
        """The reach the row check does not have.

        ``extraction_rows_below_ocr_estimate`` needs OCR that emits Markdown tables
        (Textract ``TABLES`` or BDA) and a table whose width matches the list item.
        A LAYOUT-only or table-less OCR configuration leaves it inert; this check
        needs neither OCR nor a list.
        """
        svc = _svc()
        svc._document_text = ""
        result = _invoke(svc, COMPLETE_JSON, "max_tokens")
        _w, _d, section = _save(svc, result)
        codes = _codes(section.processing_issues)
        assert CODE in codes
        assert ROW_SHORTFALL_CODE not in codes, (
            "the row check must stay inert without table OCR, or this test is "
            "measuring that one instead"
        )


class TestTheListFieldsPresentAcrossSchemaShapes:
    """What ``details["list_fields_present"]`` says for each list shape.

    The field is a convenience for the reader, not the detection — the issue
    fires on the truncation itself — and the property worth pinning is that it
    does not **assert** more than it knows. A response is cut at one point, so
    every list emitted before the cut is complete and nothing here can say which
    one was interrupted; reporting presence is the strongest true statement
    available. The key and the message clause are named for that, which is a
    correction: both previously said "truncated" and "affected", which on a
    multi-list class pointed a reader at a field that was complete.
    """

    @staticmethod
    def _issue(schema: dict, fields: dict):
        svc = _svc()
        svc._class_schema = schema
        _w, _d, section = _save(
            svc,
            ExtractionResult(
                extracted_fields=fields,
                metering={},
                parsing_succeeded=True,
                total_duration=1.0,
                output_truncated=True,
            ),
        )
        issues = [i for i in section.processing_issues if i.code == CODE]
        assert len(issues) == 1, _codes(section.processing_issues)
        return issues[0]

    def test_a_declared_list_that_came_back_empty_is_not_named(self):
        """An empty list is ``extraction_incomplete``'s case, not a truncated one."""
        issue = self._issue(SCHEMA, {"Account Number": "1", "Transactions": []})
        assert issue.details["list_fields_present"] == []
        assert "List field(s) in the partial result" not in issue.message
        # ...and the truncation is still reported, which is the point: the output
        # was cut off whether or not a list came back with rows in it.
        assert issue.severity == "warning"

    def test_a_declared_list_absent_from_the_response_is_not_named(self):
        issue = self._issue(SCHEMA, {"Account Number": "1"})
        assert issue.details["list_fields_present"] == []

    def test_a_list_nested_under_an_object_property_is_not_named(self):
        """``array_fields`` is top-level only, so a nested list is not claimed.

        The issue still fires. Naming it would need the same descent
        ``_object_list_targets`` does for the row check, and claiming a field the
        reader cannot find in the result is worse than naming none.
        """
        schema = {
            "type": "object",
            "properties": {
                "Detail": {
                    "type": "object",
                    "properties": {"Transactions": {"type": "array", "items": ROW}},
                }
            },
        }
        issue = self._issue(schema, {"Detail": {"Transactions": _rows(43)}})
        assert issue.details["list_fields_present"] == []
        assert "List field(s) in the partial result" not in issue.message

    def test_a_multi_instance_wrapper_names_the_wrapper(self):
        schema = {
            "type": "object",
            "properties": {
                "instances": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"Transactions": {"type": "array", "items": ROW}},
                    },
                }
            },
        }
        issue = self._issue(schema, {"instances": [{"Transactions": _rows(43)}]})
        assert issue.details["list_fields_present"] == ["instances"]

    def test_a_complete_sibling_list_is_reported_as_present_not_as_truncated(self):
        """The wording correction, measured on the shape that exposed it.

        ``Deposits`` here is complete — 400 rows — and ``Withdrawals`` is the short
        one. Both are reported, because the check cannot tell them apart; what it
        must not do is call the complete one truncated or affected.
        """
        schema = {
            "type": "object",
            "properties": {
                "Deposits": {"type": "array", "items": ROW},
                "Withdrawals": {"type": "array", "items": ROW},
                "Fees": {"type": "array", "items": ROW},
            },
        }
        issue = self._issue(
            schema,
            {"Deposits": _rows(400), "Withdrawals": _rows(5), "Fees": []},
        )
        assert issue.details["list_fields_present"] == ["Deposits", "Withdrawals"]
        assert "List field(s) in the partial result: Deposits, Withdrawals." in (
            issue.message
        )
        for overclaim in ("Affected list", "truncated list field", "were truncated"):
            assert overclaim not in issue.message, overclaim
        assert "Fees" not in issue.message

    def test_a_declared_array_that_came_back_as_a_string_is_not_called_a_list(self):
        issue = self._issue(SCHEMA, {"Account Number": "1", "Transactions": "n/a"})
        assert issue.details["list_fields_present"] == []

    def test_a_non_string_schema_property_name_does_not_cost_the_issue_list(self):
        """``_build_extraction_issues`` runs with no enclosing ``try``.

        A schema read straight from YAML can carry a bare numeric key, which
        arrives as an ``int``. Schema names reach the **empty-list** join whatever
        the response holds, and an unguarded ``str.join`` over one raises
        ``TypeError`` — which, with no enclosing ``try`` in ``_save_results``,
        costs the section its whole processing-issue list and fails the save. That
        join is the one this pins, and the mutation that removes its coercion is
        caught here.

        It does **not** cover the truncation block's own list, because that list
        cannot reach a non-string name: ``extracted_fields`` comes from
        ``json.loads`` or from a forced-tool payload, both string-keyed, and
        ``ExtractionResult.extracted_fields`` is typed ``dict[str, Any]`` — so an
        ``int`` schema name always fails the lookup and lands in ``empty_lists``
        instead. A coercion there would have been dead defence, and this test
        claimed to cover it while measurably not doing so (the mutation survived),
        so the coercion was deleted rather than the claim kept.

        Driven through the builder directly, because pydantic refuses a non-string
        key on ``ExtractionResult`` and the point is the schema side.
        """
        svc = _svc()
        svc._class_schema = {
            "type": "object",
            "properties": {2024: {"type": "array", "items": ROW}},
        }
        issues = svc._build_extraction_issues(
            extracted_fields={"2024": _rows(5)},
            metadata={"output_truncated": True},
            section_id="1",
        )
        codes = _codes(issues)
        # Nothing raised, and both issues were produced rather than the list being
        # lost. The empty-list issue names the field, because an `int` schema name
        # is never a key in the response and so reads as an absent list; the
        # truncation issue claims nothing, for the same reason — which is also why
        # it needs no coercion of its own.
        assert CODE in codes, codes
        assert "extraction_incomplete" in codes, codes
        empty = next(i for i in issues if i.code == "extraction_incomplete")
        assert "2024" in empty.message
        truncated = next(i for i in issues if i.code == CODE)
        assert truncated.details["list_fields_present"] == []

    def test_an_int_schema_key_cannot_be_a_response_key(self):
        """Half one of the premise the deletion rests on, asserted not assumed.

        If ``ExtractionResult`` ever accepts a non-string key, that half is gone.
        It is not the load-bearing half — see the next test.
        """
        with pytest.raises(Exception):
            ExtractionResult(
                extracted_fields={2024: _rows(1)},  # type: ignore[dict-item]
                metering={},
                parsing_succeeded=True,
                total_duration=1.0,
            )
        # And the shape a response actually arrives in is string-keyed by
        # construction.
        assert all(isinstance(k, str) for k in json.loads(COMPLETE_JSON))

    def test_a_schema_keyed_none_is_excluded_by_the_isinstance_guard(self):
        """Half two, and the one that actually does the work.

        ``_save_results`` passes the builder ``_apply_missing_field_handling``'s
        output, not ``result.extracted_fields``, and that function writes
        ``fields_out[prop_name] = None`` keyed by the **schema** property name. So
        a non-string name *can* be a key in the dict the builder sees, and what
        keeps it out of the list is the ``isinstance(..., list)`` test rejecting
        ``None`` — not the typing of ``ExtractionResult``.

        Which means the list is **unreachable** with a non-string name rather than
        safe against one: handed ``{2024: [{...}]}`` directly the join would raise.
        That shape has no producer, so the coercion was deleted rather than kept;
        this is the assertion that the thing which replaces it holds.
        """
        svc = _svc()
        svc._class_schema = {
            "type": "object",
            "properties": {2024: {"type": "array", "items": ROW}},
        }
        issues = svc._build_extraction_issues(
            extracted_fields={2024: None},
            metadata={"output_truncated": True},
            section_id="1",
        )
        truncated = next(i for i in issues if i.code == CODE)
        assert truncated.details["list_fields_present"] == []

    def test_a_class_with_no_list_fields_at_all_is_still_reported(self):
        """A truncated form has lost scalars, and this is the only signal for it."""
        schema = {
            "type": "object",
            "properties": {"Account Number": {"type": "string"}},
        }
        issue = self._issue(schema, {"Account Number": "1"})
        assert issue.details["list_fields_present"] == []
        # The WHOLE message, which is what pins the empty branch of the
        # list-fields clause: an "is not in" assertion let a mutation of its
        # `else ""` put arbitrary text into the sentence and still pass.
        assert issue.message == (
            "The model's extraction response was cut off before it finished, so "
            "the result holds only the part that was emitted. Scalar fields and "
            "the rows that did arrive can still be perfectly correct, so no "
            "accuracy or confidence signal flags this. Simple extraction returns "
            "one response per section, so a section whose answer does not fit in "
            "one response loses the tail; use Advanced (agentic) extraction, "
            "which shards the section across requests, or split the document."
        )


class TestTheRemedyIsAvailableToTheDeploymentItIsGivenTo:
    """Do not recommend a mode the configured model cannot be run in.

    ``_invoke_extraction_model`` raises ``ValueError`` for ``agentic.enabled`` with
    an OpenAI Responses model, so "use Advanced (agentic) extraction" is advice
    that breaks the run for that family — and ``incomplete``, the stop reason those
    models report, is precisely the one that brings a reader to this issue.

    The model set is read from the authority
    (``bedrock.openai_responses._RESPONSES_API_MODELS``) rather than written out,
    so a model added there is exercised here without an edit.
    """

    MODELS = sorted(_RESPONSES_API_MODELS)

    def test_the_model_set_is_not_empty(self):
        assert len(self.MODELS) > 0

    @staticmethod
    def _issue_for(model: str):
        cfg = IDPConfig(
            **{
                "extraction": {
                    "mode": "simple",
                    "agentic": {"enabled": False},
                    "model": model,
                }
            }
        )
        svc = ExtractionService(config=cfg)
        svc._reset_context()
        svc._class_schema = SCHEMA
        _w, _d, section = _save(
            svc,
            ExtractionResult(
                extracted_fields={"Account Number": "1", "Transactions": _rows(43)},
                metering={},
                parsing_succeeded=True,
                total_duration=1.0,
                output_truncated=True,
            ),
        )
        return next(i for i in section.processing_issues if i.code == CODE)

    @pytest.mark.parametrize("model", MODELS)
    def test_advanced_is_not_recommended_to_a_model_that_cannot_run_it(self, model):
        assert is_openai_responses_model(model), (
            "the authority no longer calls this a Responses model; re-derive the set"
        )
        issue = self._issue_for(model)
        assert "use Advanced (agentic) extraction" not in issue.message
        # The whole alternative clause, not a fragment of it: a mutation of its
        # second half survived a `"split the document" in message` assertion.
        assert issue.message.endswith(
            "split the document, or process it with a model that supports "
            "Advanced (agentic) extraction, which shards the section across "
            "requests."
        )
        assert issue.details["advanced_mode_available"] is False

    def test_advanced_is_still_recommended_where_it_is_available(self):
        issue = self._issue_for("us.anthropic.claude-sonnet-5")
        assert issue.message.endswith(
            " Simple extraction returns one response per section, so a section "
            "whose answer does not fit in one response loses the tail; use "
            "Advanced (agentic) extraction, which shards the section across "
            "requests, or split the document."
        )
        assert issue.details["advanced_mode_available"] is True

    def test_the_refusal_this_avoids_is_real(self):
        """The ``ValueError`` the recommendation would have walked a reader into."""
        cfg = IDPConfig(
            **{
                "extraction": {
                    "mode": "advanced",
                    "agentic": {"enabled": True},
                    "model": self.MODELS[0],
                }
            }
        )
        svc = ExtractionService(config=cfg)
        svc._reset_context()
        svc._class_schema = SCHEMA
        with pytest.raises(ValueError):
            with patch(
                "idp_common.extraction.service.bedrock.invoke_model",
                return_value=_response(COMPLETE_JSON, "end_turn"),
            ):
                svc._invoke_extraction_model(
                    content=[{"text": "p"}],
                    system_prompt="sys",
                    section_info=_section_info(),
                )


class TestTheOpenAIAdapterReachesTheMatcher:
    """The ``incomplete`` member, driven through the real adapter.

    Without this the member is only ever asserted against itself: the matcher test
    is parametrized over the same tuple the matcher reads, and
    ``TestOneVocabulary`` is a structural check on the literal. Neither would
    notice that nothing in the tree produces the value.
    """

    def test_an_incomplete_responses_payload_translates_to_a_truncation(self):
        payload = {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": COMPLETE_JSON}],
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
        envelope = translate_response(payload, "openai.gpt-5.5", "Extraction")
        assert envelope["response"]["stopReason"] == "incomplete"
        assert is_output_token_truncation(envelope) is True

    def test_a_completed_responses_payload_does_not(self):
        payload = {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": COMPLETE_JSON}],
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
        envelope = translate_response(payload, "openai.gpt-5.5", "Extraction")
        assert is_output_token_truncation(envelope) is False

    def test_the_adapter_maps_status_and_not_the_incomplete_reason(self):
        """Why ``max_output_tokens`` is documented as defensive rather than mapped.

        The payload above carries ``incomplete_details.reason ==
        "max_output_tokens"`` and the translated ``stopReason`` is ``incomplete``,
        so the reason spelling is not what arrives. A document claiming the adapter
        maps it would be wrong, which is what this pins.
        """
        payload = {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        assert (
            translate_response(payload, "openai.gpt-5.5", "Extraction")["response"][
                "stopReason"
            ]
            != "max_output_tokens"
        )


class TestReachability:
    """One setting path per mode, and they are not the same mechanism.

    ``_build_extraction_issues`` runs for both modes and reports whatever
    ``metadata["output_truncated"]`` says, so the *reporting* half is mode-blind.
    The *setting* half has two paths. On the **Simple** path the flag is assigned
    inside ``_invoke_extraction_model``'s Simple branch, from the response's own
    ``stopReason``; the Advanced branch of that function still never touches it,
    and the AST assertion below keeps that true so the two mechanisms cannot be
    conflated. On the **Advanced** path it arrives as a ``_output_truncated``
    marker in the shard's metering, written by the #1363 salvage in
    ``agentic_idp._invoke_agent_for_extraction`` and popped into the same metadata
    key by ``_save_results`` — a real flow, exercised end to end below.

    Before that salvage existed there was no reachable Advanced-mode path at all:
    a truncated agent turn raised out of the event loop and failed the section,
    taking a validated extraction with it. Both doc tiers have to keep saying
    which route each mode takes, which is what the last test here pins.
    """

    SRC = (
        Path(__file__).resolve().parents[3] / "idp_common/extraction/service.py"
    ).read_text(encoding="utf-8")

    def _invoke_fn(self) -> ast.FunctionDef:
        tree = ast.parse(self.SRC)
        fns = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_invoke_extraction_model"
        ]
        assert len(fns) == 1, f"expected one definition, found {len(fns)}"
        return fns[0]

    def test_the_stop_reason_flag_is_only_ever_set_on_the_simple_branch(self):
        """The Advanced path reaches the issue through metering, not through here.

        Keeping ``_invoke_extraction_model``'s Advanced branch out of this
        variable is what stops the two mechanisms being conflated: a
        ``stopReason`` is a property of one Converse response, and an agent loop
        makes many.
        """
        fn = self._invoke_fn()
        branches = [
            n
            for n in fn.body
            if isinstance(n, ast.If)
            and ast.unparse(n.test) == "use_agentic"
            and n.orelse
        ]
        assert len(branches) == 1, (
            "the mode branch this assertion is anchored on is gone; re-derive it"
        )
        simple = branches[0].orelse
        simple_lines = range(simple[0].lineno, (simple[-1].end_lineno or 0) + 1)
        stores = [
            n.lineno
            for n in ast.walk(fn)
            if isinstance(n, ast.Name)
            and n.id == "output_truncated"
            and isinstance(n.ctx, ast.Store)
        ]
        assert stores, "output_truncated is never assigned: this pin is vacuous"
        initialiser = min(stores)
        assert initialiser < branches[0].lineno, (
            "the initialiser must precede the mode branch"
        )
        outside = [ln for ln in stores if ln != initialiser and ln not in simple_lines]
        assert not outside, (
            f"output_truncated is assigned outside the Simple branch at line(s) "
            f"{outside}. The Advanced path is meant to reach this issue through the "
            f"_output_truncated metering marker, not by setting this variable; if "
            f"that changed, both doc tiers' description of the two routes is now "
            f"wrong and must be rewritten."
        )

    def test_the_advanced_path_reaches_the_issue_through_the_metering_marker(self):
        """The real Advanced-mode flow: a salvaged shard's marker becomes the issue.

        ``_output_truncated`` is what ``agentic_idp.structured_output_async`` puts
        in the shard's metering when it salvages a truncation-killed agent run
        (#1363). This drives the whole tail — ``_save_results`` through
        ``_build_extraction_issues`` — so the issue is produced by a flow rather
        than by handing the tail a pre-set flag.
        """
        svc = _svc(agentic=True)
        result = ExtractionResult(
            extracted_fields={"Account Number": "1", "Transactions": _rows(43)},
            metering={
                "Extraction/bedrock/m": {"outputTokens": 1},
                "_output_truncated": True,
            },
            parsing_succeeded=True,
            total_duration=1.0,
            # Deliberately NOT set: the Advanced path never assigns it.
            output_truncated=False,
        )
        _w, _d, section = _save(svc, result)
        assert CODE in _codes(section.processing_issues)
        # ``result.metering`` rather than the dict handed in: ExtractionResult is a
        # Pydantic model and validation copies it.
        assert "_output_truncated" not in result.metering, (
            "the marker must be popped, or it leaks into the metering report as a "
            "key that is not a token counter"
        )

    def test_an_unsalvaged_agentic_section_carries_no_truncation_issue(self):
        svc = _svc(agentic=True)
        _w, _d, section = _save(
            svc,
            ExtractionResult(
                extracted_fields={"Account Number": "1", "Transactions": _rows(43)},
                metering={"Extraction/bedrock/m": {"outputTokens": 1}},
                parsing_succeeded=True,
                total_duration=1.0,
            ),
        )
        assert CODE not in _codes(section.processing_issues)

    @pytest.mark.parametrize(
        "rel",
        [
            "docs/extraction-and-confidence.md",
            "lib/idp_common_pkg/idp_common/extraction/README.md",
        ],
    )
    def test_both_doc_tiers_state_the_reachability_bound(self, rel):
        repo = Path(__file__).resolve().parents[5]
        text = re.sub(r"[*`#\s]+", " ", (repo / rel).read_text(encoding="utf-8"))
        assert "Advanced mode reaches this" in text, (
            f"{rel} no longer tells a reader that Advanced mode reaches this issue, "
            f"and by which route. It used to say there was no Advanced-mode path at "
            f"all, which the #1363 salvage made false."
        )


class TestTheMatcher:
    def test_the_stop_reason_vocabulary_is_not_empty(self):
        """Every parametrisation in this module is derived from this set.

        An empty one would collect as a skip, so the emptiness has to be a
        failure somewhere, and this is where.
        """
        assert len(OUTPUT_TRUNCATION_STOP_REASONS) > 0

    @pytest.mark.parametrize("stop_reason", list(OUTPUT_TRUNCATION_STOP_REASONS))
    def test_every_declared_stop_reason_matches_in_both_shapes(self, stop_reason):
        assert is_output_token_truncation({"stopReason": stop_reason}) is True
        assert (
            is_output_token_truncation({"response": {"stopReason": stop_reason}})
            is True
        )

    @pytest.mark.parametrize(
        "value", ["end_turn", "tool_use", "stop_sequence", "content_filtered", "", None]
    )
    def test_an_ordinary_stop_does_not_match(self, value):
        assert is_output_token_truncation({"stopReason": value}) is False
        assert is_output_token_truncation({"response": {"stopReason": value}}) is False

    @pytest.mark.parametrize("value", [None, "max_tokens", 7, [], object()])
    def test_a_shape_it_cannot_read_is_not_a_truncation(self, value):
        assert is_output_token_truncation(value) is False

    def test_a_wrapper_whose_response_member_is_not_a_dict_falls_back(self):
        """``{"response": None}`` must not be read as "no stop reason"."""
        assert (
            is_output_token_truncation({"response": None, "stopReason": "max_tokens"})
            is True
        )

    @pytest.mark.parametrize("stop_reason", list(OUTPUT_TRUNCATION_STOP_REASONS))
    def test_rules_discovery_answers_through_the_same_matcher(self, stop_reason):
        assert (
            RulesDiscovery._response_was_truncated(
                {"response": {"stopReason": stop_reason}}
            )
            is True
        )
        assert (
            RulesDiscovery._response_was_truncated({"stopReason": "end_turn"}) is False
        )


class TestOneVocabulary:
    """No second copy of the stop-reason set anywhere in the package.

    Three call sites asked this question with three inline copies of the same
    tuple. A fourth copy is how they drift, and a drifted copy is invisible: each
    site stays internally consistent and its own tests keep passing. Source-read
    rather than imported, so the assertion is about what is committed.
    """

    PKG = Path(__file__).resolve().parents[3] / "idp_common"

    def test_the_literal_set_is_declared_exactly_once(self):
        files = sorted(self.PKG.rglob("*.py"))
        assert files, f"no sources found under {self.PKG}"
        hits = []
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Tuple, ast.Set, ast.List)):
                    continue
                values = {
                    el.value
                    for el in node.elts
                    if isinstance(el, ast.Constant) and isinstance(el.value, str)
                }
                if "max_tokens" in values and "incomplete" in values:
                    hits.append(str(path.relative_to(self.PKG)))
        assert hits == ["utils/bedrock_utils.py"], hits


class TestBothDocTiersCarryTheCode:
    """A warning a user can see is documented in both tiers."""

    #: Asserted rather than assumed: a wrong depth here would turn every
    #: assertion below into a ``FileNotFoundError`` about the wrong tree.

    REPO = Path(__file__).resolve().parents[5]

    def test_the_repository_root_resolves(self):
        assert (self.REPO / "CHANGELOG.md").is_file(), self.REPO

    @pytest.mark.parametrize(
        "rel",
        [
            "docs/extraction-and-confidence.md",
            "lib/idp_common_pkg/idp_common/extraction/README.md",
        ],
    )
    def test_the_code_is_described(self, rel):
        text = (self.REPO / rel).read_text(encoding="utf-8")
        assert CODE in text, f"{rel} does not mention {CODE}"

    @pytest.mark.parametrize(
        "rel",
        [
            "docs/extraction-and-confidence.md",
            "lib/idp_common_pkg/idp_common/extraction/README.md",
        ],
    )
    def test_no_tier_claims_the_stop_reason_detects_an_abbreviated_list(self, rel):
        """The over-claim this change must not ship.

        A reader told that extraction now detects truncation will stop looking for
        the row check, and the measured #1361 runs are exactly the ones a stop
        reason cannot see.
        """
        text = (self.REPO / rel).read_text(encoding="utf-8")
        for para in re.split(r"\n\s*\n", text):
            if CODE not in para:
                continue
            assert not re.search(
                r"\b(all|any|every)\b[^.]{0,80}\btruncat", para, re.IGNORECASE
            ), para[:400]
