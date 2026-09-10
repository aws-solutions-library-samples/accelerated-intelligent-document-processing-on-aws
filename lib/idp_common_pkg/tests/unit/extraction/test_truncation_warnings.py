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
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

from idp_common.config.models import IDPConfig
from idp_common.extraction.service import ExtractionInputTooLarge, ExtractionService
from idp_common.utils.bedrock_utils import is_input_token_overflow

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


def _svc(*, agentic: bool = False, schema: dict | None = None) -> ExtractionService:
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
        assert issue.severity == "warning"
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
    @pytest.mark.parametrize("gap,rows", [(5, [11]), (6, [6])])
    def test_the_gap_boundary_tolerates_empty_lines_inside_a_table(self, gap, rows):
        """Up to 5 intervening lines keep one table; at 6 the second run stands alone,
        and without its own separator it is not a table at all."""
        body = "\n".join(f"| a{i} | b{i} | c{i} |" for i in range(5))
        text = _table(5, cols=3) + "\n" * gap + body
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
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (1500, 3000)).save(buf, format="PNG")
        assert (
            ExtractionService._image_token_estimate(
                {"format": "png", "source": {"bytes": buf.getvalue()}}, 1600
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
