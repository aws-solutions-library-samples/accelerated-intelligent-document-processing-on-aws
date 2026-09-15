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

    PROMPT_WITH_IMAGE = "Extract from:\n{DOCUMENT_TEXT}\n{DOCUMENT_IMAGE}\nEnd."

    def _service(self, lazy_images: bool) -> ExtractionService:
        cfg = {
            "extraction": {
                "task_prompt": self.PROMPT_WITH_IMAGE,
                "agentic": {
                    "enabled": True,
                    "max_concurrent_batches": 4,
                    "shard_token_budget": 5000,
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
        return svc

    @staticmethod
    def _table_page(page_index: int) -> str:
        """A page holding a parseable Markdown table with 40 data rows.

        Two such pages clear the pre-flight gate (>= 50 estimated table rows)
        and are dense enough to split into more than one shard.
        """
        rows = "\n".join(
            f"| {page_index}-{i} | Row {i} | {i * 10}.00 | {'w' * 200} |"
            for i in range(40)
        )
        return (
            "| RowID | Description | Amount | Notes |\n"
            "|-------|-------------|--------|-------|\n" + rows
        )

    def _plan(self, lazy_images: bool):
        svc = self._service(lazy_images)
        pages = [self._table_page(p) for p in range(6)]
        svc._page_texts = pages
        svc._document_text = "\n".join(pages)
        # Real PNG bytes: the attach path decodes each page image with PIL.
        svc._page_images = [_png_bytes() for _ in range(6)]
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
        _, _, payloads, _ = svc._build_agentic_shard_plan(section_info)
        assert len(payloads) > 1, "test needs a genuinely sharded section"
        return payloads

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
