# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for ExtractionService._build_shard_payloads (input sharding).

These exercise the service-level sharding/prompt-rendering path without Bedrock
or Strands (pure prompt construction), so they run as plain unit tests.
"""

import pytest

from idp_common.extraction.service import ExtractionService, SectionInfo

pytestmark = pytest.mark.unit

PROMPT = "Extract from:\n{DOCUMENT_TEXT}\nEnd."


def _service(max_batches: int = 4, budget: int = 5000) -> ExtractionService:
    cfg = {
        "extraction": {
            "task_prompt": PROMPT,
            "agentic": {
                "enabled": True,
                "max_concurrent_batches": max_batches,
                "shard_token_budget": budget,
            },
        },
        "classes": [
            {"$id": "Doc", "type": "object", "properties": {"x": {"type": "string"}}}
        ],
    }
    svc = ExtractionService(region="us-west-2", config=cfg)
    svc._class_label = "Doc"
    svc._class_schema = cfg["classes"][0]
    svc._attribute_descriptions = "x: a field"
    return svc


def _set_pages(svc: ExtractionService, page_texts: list[str]) -> None:
    svc._page_texts = page_texts
    svc._document_text = "\n".join(page_texts)
    svc._page_images = []


def _shard_text(payload) -> str:
    return "".join(c.get("text", "") for c in payload["content"])


def _png_bytes() -> bytes:
    """A 1x1 PNG — the image-attach path decodes page images with PIL."""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (1, 1)).save(buf, format="PNG")
    return buf.getvalue()


class TestBuildShardPayloads:
    def test_dense_pages_split_and_cover_all(self):
        svc = _service(max_batches=4, budget=5000)
        _set_pages(svc, [f"PAGE{i} " + ("w" * 12000) for i in range(6)])
        payloads = svc._build_shard_payloads(
            prompt_template=PROMPT, send_images=False, max_shards=4
        )
        assert len(payloads) == 4  # cap binds
        # contiguous full coverage
        prev = 0
        for p in payloads:
            assert p["page_start"] == prev
            prev = p["page_end"]
        assert prev == 6

    def test_header_context_only_on_later_shards(self):
        svc = _service(max_batches=4, budget=5000)
        _set_pages(svc, [f"PAGE{i} " + ("w" * 12000) for i in range(6)])
        payloads = svc._build_shard_payloads(
            prompt_template=PROMPT, send_images=False, max_shards=4
        )
        assert "DOCUMENT HEADER" not in _shard_text(payloads[0])
        assert all("DOCUMENT HEADER" in _shard_text(p) for p in payloads[1:])

    def test_restores_instance_state(self):
        svc = _service()
        pages = [f"PAGE{i} " + ("w" * 12000) for i in range(6)]
        _set_pages(svc, pages)
        svc._build_shard_payloads(
            prompt_template=PROMPT, send_images=False, max_shards=4
        )
        # _document_text / _page_images must be restored to the full section.
        assert svc._document_text == "\n".join(pages)
        assert svc._page_images == []

    def test_small_doc_returns_no_shards(self):
        # Light content that fits one budget -> single shard -> caller uses single pass.
        svc = _service(max_batches=4, budget=40000)
        _set_pages(svc, ["short page a", "short page b", "short page c"])
        payloads = svc._build_shard_payloads(
            prompt_template=PROMPT, send_images=False, max_shards=4
        )
        assert payloads == []

    def test_single_page_returns_no_shards(self):
        svc = _service()
        _set_pages(svc, ["only one page " + "w" * 100000])
        payloads = svc._build_shard_payloads(
            prompt_template=PROMPT, send_images=False, max_shards=4
        )
        assert payloads == []

    def test_page_markers_present_in_shard_text(self):
        svc = _service(max_batches=4, budget=5000)
        _set_pages(svc, [f"content{i} " + ("w" * 12000) for i in range(6)])
        payloads = svc._build_shard_payloads(
            prompt_template=PROMPT, send_images=False, max_shards=4
        )
        # Each shard's text uses 1-based PAGE markers for its own pages.
        first = _shard_text(payloads[0])
        assert "--- PAGE 1 ---" in first


class TestTableHeaderContext:
    """Header context prepended to later shards must include the table column
    header but DROP page-1 data rows (else the parser re-emits them -> dup)."""

    def test_truncates_at_markdown_separator(self):
        txt = (
            "# Statement\n\nAccount: X\n\n"
            "| RowID | Symbol |\n|-------|--------|\n"
            "| 1 | A |\n| 2 | B |\n| 3 | C |"
        )
        out = ExtractionService._table_header_context(txt)
        assert "| RowID | Symbol |" in out  # column header kept
        assert "|-------|--------|" in out  # separator kept
        assert "| 1 | A |" not in out  # data rows dropped
        assert "| 2 | B |" not in out

    def test_non_table_falls_back_to_line_cap(self):
        txt = "\n".join(f"line{i}" for i in range(50))
        out = ExtractionService._table_header_context(txt, max_lines=10)
        assert out.count("\n") == 9  # 10 lines
        assert "line0" in out and "line49" not in out

    def test_empty(self):
        assert ExtractionService._table_header_context("") == ""


PROMPT_WITH_IMAGE = "Extract from:\n{DOCUMENT_TEXT}\n{DOCUMENT_IMAGE}\nEnd."


def _table_page(page_index: int) -> str:
    """A page holding a parseable Markdown table with 40 data rows.

    Two such pages clear the pre-flight gate (>= 50 estimated table rows) and are
    dense enough to split into more than one shard.
    """
    rows = "\n".join(
        f"| {page_index}-{i} | Row {i} | {i * 10}.00 | {'w' * 200} |" for i in range(40)
    )
    return (
        "| RowID | Description | Amount | Notes |\n"
        "|-------|-------------|--------|-------|\n" + rows
    )


def _table_doc_service(lazy_images: bool = True) -> ExtractionService:
    """A service whose section is a 6-page Markdown-table document.

    Shared by the two shard-plan test classes below: this is the section that
    both makes `_build_agentic_shard_plan` shard (more than one payload) and
    makes its pre-flight table parse succeed, which is what the lazy_images
    decision and the pre-parsed guidance block both hang off.
    """
    cfg = {
        "extraction": {
            "task_prompt": PROMPT_WITH_IMAGE,
            "agentic": {
                "enabled": True,
                "max_concurrent_batches": 4,
                # A per-shard token budget, not a credential — Bandit's B105
                # matches on the "token" in the key name.
                "shard_token_budget": 5000,  # nosec B105
                "table_parsing": {
                    "enabled": True,
                    "lazy_images": lazy_images,
                },
            },
        },
        "classes": [
            {
                "$id": "Doc",
                "type": "object",
                "properties": {"x": {"type": "string"}},
            }
        ],
    }
    svc = ExtractionService(region="us-west-2", config=cfg)
    svc._class_label = "Doc"
    svc._class_schema = cfg["classes"][0]
    svc._attribute_descriptions = "x: a field"
    pages = [_table_page(p) for p in range(6)]
    svc._page_texts = pages
    svc._document_text = "\n".join(pages)
    # Real PNG bytes: the attach path decodes each page image with PIL.
    svc._page_images = [_png_bytes() for _ in range(6)]
    return svc


def _shard_plan(svc: ExtractionService) -> tuple[list, str | None]:
    """Build the shard plan for the 6-page table section; return payloads + CI."""
    section_info = SectionInfo(
        class_label="Doc",
        sorted_page_ids=[str(p + 1) for p in range(6)],
        page_indices=list(range(6)),
        output_bucket="b",
        output_key="k",
        output_uri="s3://b/k",
        start_page=1,
        end_page=6,
    )
    _, _, payloads, custom_instruction = svc._build_agentic_shard_plan(section_info)
    assert len(payloads) > 1, "test needs a genuinely sharded section"
    return payloads, custom_instruction


class TestShardPlanLazyImages:
    """The SHARDED agentic path must make the same lazy_images decision the
    single-pass path makes.

    It did not: `_build_agentic_shard_plan` never ran the pre-flight table parse
    and never consulted `table_parsing.lazy_images`, so every shard carried its
    page images on every agent turn. Sharding is the default for multi-page table
    documents, which is exactly where the shipped `lazy_images: true` default was
    supposed to save the tokens — so the optimization was dead where it mattered
    most, and no test noticed because the config-level tests only cover the knob's
    value (see TestLazyImagesConfig in test_tool_writeback.py).
    """

    @staticmethod
    def _plan(lazy_images: bool):
        return _shard_plan(_table_doc_service(lazy_images))[0]

    @staticmethod
    def _image_blocks(payload) -> int:
        return sum(1 for c in payload["content"] if "image" in c)

    def test_preflight_parse_suppresses_shard_images(self):
        payloads = self._plan(lazy_images=True)
        assert all(self._image_blocks(p) == 0 for p in payloads)

    def test_lazy_images_off_still_attaches_shard_images(self):
        payloads = self._plan(lazy_images=False)
        assert all(self._image_blocks(p) > 0 for p in payloads)

    def test_assessment_images_survive_suppression(self):
        # lazy_images governs the EXTRACTION prompt only. The in-shard assessment
        # pass reuses the page bytes regardless, so suppressing them for the agent
        # must not blind assessment.
        payloads = self._plan(lazy_images=True)
        assert all(p["assess_page_images"] for p in payloads)


class TestPreflightTableGuidanceBlock:
    """`_append_preflight_table_guidance` is the ONE home of the PRE-PARSED TABLE
    DATA block, shared by the single-pass and sharded agentic paths (#900).

    The literal below is the block exactly as the single-pass path emitted it
    before the refactor. Asserting equality against it is what makes the sharded
    fix attributable: if the shared helper had also reworded the single-pass
    instruction, an A/B against the study's numbers would be measuring two
    changes at once.
    """

    EXPECTED = (
        "\n\n**PRE-PARSED TABLE DATA AVAILABLE**:\n"
        "Found 2 table(s) with 30 total rows.\n"
        "Table columns: ['Date', 'Amount']\n\n"
        "PAGE MARKERS: The document text contains '--- PAGE N ---' "
        "markers between pages. When you are assigned a page range, "
        "extract ONLY text between markers for your pages before "
        "calling parse_table.\n\n"
        "EFFICIENT EXTRACTION WORKFLOW:\n"
        "1. Extract scalar fields from your pages' text\n"
        "2. Call parse_table with your pages' text\n"
        "3. Call map_table_to_schema with column_mapping + static_fields\n"
        "   (merged rows are auto-split — no manual handling needed)\n"
        "4. Call finalize_table_extraction with table_array_field + "
        "scalar_fields\n\n"
        "finalize reads mapped rows from state — no JSON generation needed."
    )

    PREFLIGHT = {
        "status": "success",
        "table_count": 2,
        "tables": [{"row_count": 18}, {"row_count": 12}],
        "columns": ["Date", "Amount"],
    }

    def test_block_is_byte_identical_to_the_pre_refactor_text(self):
        out = ExtractionService._append_preflight_table_guidance(None, self.PREFLIGHT)
        assert out == self.EXPECTED

    def test_appends_to_an_existing_instruction(self):
        # The single-pass path did `custom_instruction += block`; the shared
        # helper must keep that, leading blank line included.
        base = "**IMPORTANT - USE TABLE PARSING TOOL**"
        out = ExtractionService._append_preflight_table_guidance(base, self.PREFLIGHT)
        assert out == base + self.EXPECTED

    def test_reports_table_count_row_total_and_columns(self):
        out = ExtractionService._append_preflight_table_guidance(None, self.PREFLIGHT)
        assert out is not None
        assert "Found 2 table(s) with 30 total rows." in out
        assert "Table columns: ['Date', 'Amount']" in out

    def test_no_block_when_preflight_did_not_run(self):
        assert ExtractionService._append_preflight_table_guidance(None, None) is None
        assert (
            ExtractionService._append_preflight_table_guidance("base", None) == "base"
        )

    def test_no_block_when_preflight_failed(self):
        failed = {"status": "no_tables_found", "table_count": 0, "tables": []}
        assert ExtractionService._append_preflight_table_guidance(None, failed) is None
        assert (
            ExtractionService._append_preflight_table_guidance("base", failed) == "base"
        )


class TestShardPlanPreflightGuidance:
    """The SHARDED path's shard agents must receive the PRE-PARSED TABLE DATA
    block too (#900).

    They did not. `_build_agentic_shard_plan` built `custom_instruction` from
    `_build_table_parsing_guidance` alone, so shard agents got no row/column
    summary and no "finalize reads the rows from state" workflow — the guidance
    that stops an agent re-emitting every parsed row as output tokens. Sharding
    is the default for any multi-page table document, so the agents that most
    needed the block were the only ones not getting it. Uses the same
    table-document fixture as `TestShardPlanLazyImages` (the section that clears
    the pre-flight gate).
    """

    @staticmethod
    def _instruction() -> str | None:
        return _shard_plan(_table_doc_service())[1]

    def test_shard_instruction_carries_the_preflight_block(self):
        ci = self._instruction()
        assert ci and "**PRE-PARSED TABLE DATA AVAILABLE**" in ci
        # The four-step workflow, i.e. the part that keeps rows out of the output.
        assert "finalize reads mapped rows from state" in ci
        assert "--- PAGE N ---" in ci

    def test_block_reports_the_real_parse_result(self):
        import re

        ci = self._instruction()
        assert ci is not None
        m = re.search(r"Found (\d+) table\(s\) with (\d+) total rows\.", ci)
        assert m, ci
        # Real numbers from the deterministic parser, not placeholders. The gate
        # for pre-flight is >= 50 estimated rows, so the section must clear it.
        assert int(m.group(1)) >= 1
        assert int(m.group(2)) >= 50
        assert "Table columns: ['RowID', 'Description', 'Amount', 'Notes']" in ci

    def test_no_block_when_table_parsing_is_disabled(self):
        # table_parsing.enabled: false -> no pre-flight parse -> nothing to
        # describe. The instruction must fall back to whatever
        # _build_table_parsing_guidance produced on its own.
        svc = _table_doc_service()
        svc.config.extraction.agentic.table_parsing.enabled = False
        _payloads, ci = _shard_plan(svc)
        assert "PRE-PARSED TABLE DATA AVAILABLE" not in (ci or "")

    def test_shard_agent_gets_the_block_and_its_own_page_range(self):
        # The plan returns ONE instruction for ALL shards, so the block's page
        # wording stays generic ("when you are assigned a page range"); the
        # concrete range is appended per shard downstream by
        # agentic_idp._run_shard_agent. Pin that division of labour end to end —
        # the block is only coherent because the assignment follows it.
        import asyncio
        from unittest.mock import AsyncMock, patch

        from pydantic import BaseModel

        from idp_common.extraction import agentic_idp

        class M(BaseModel):
            a: str | None = None

        fake = AsyncMock(return_value=(M(), {}))
        with patch.object(agentic_idp, "structured_output_async", fake):
            asyncio.run(
                agentic_idp._run_shard_agent(
                    shard_index=1,
                    total_shards=3,
                    page_start=2,
                    page_end=5,
                    total_pages=6,
                    model_id="m",
                    data_format=M,
                    shard_prompt="p",
                    config=None,  # type: ignore[arg-type]
                    context="Extraction",
                    max_retries=1,
                    connect_timeout=1.0,
                    read_timeout=1.0,
                    max_tokens=None,
                    checkpoint_callback=None,
                    base_custom_instruction=self._instruction(),
                )
            )
        sent = fake.await_args.kwargs["custom_instruction"]
        assert "**PRE-PARSED TABLE DATA AVAILABLE**" in sent
        assert "covering pages 3-5 of 6" in sent
        # Block first, page assignment after it.
        assert sent.index("PRE-PARSED TABLE DATA") < sent.index("covering pages 3-5")


class TestAnalyzeSchemaMinItems:
    """minItems can arrive as a string after a config round-trip; the schema
    analysis must coerce it instead of raising TypeError on `min_items > 50`."""

    def test_string_min_items_does_not_raise(self):
        svc = _service()
        schema = {
            "properties": {
                "rows": {"type": "array", "minItems": "100", "description": "t"}
            }
        }
        result = svc._analyze_schema_for_table_requirements(schema)
        assert result["tool_usage_recommended"] is True

    def test_string_min_items_below_threshold(self):
        svc = _service()
        schema = {"properties": {"rows": {"type": "array", "minItems": "5"}}}
        result = svc._analyze_schema_for_table_requirements(schema)
        assert result["tool_usage_recommended"] is False

    def test_invalid_min_items_treated_as_zero(self):
        svc = _service()
        schema = {"properties": {"rows": {"type": "array", "minItems": "abc"}}}
        result = svc._analyze_schema_for_table_requirements(schema)
        assert result["tool_usage_recommended"] is False
