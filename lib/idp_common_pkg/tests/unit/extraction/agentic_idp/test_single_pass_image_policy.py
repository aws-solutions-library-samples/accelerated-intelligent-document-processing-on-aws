# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The single-pass agentic path must apply its image policy to what it SENDS.

`{DOCUMENT_IMAGE}` is substituted while the prompt content is built, which
happens before the agentic branch forms any opinion about images. Both image
knobs were applied to the `page_images=` argument instead of to that content, so
on this path `lazy_images` suppressed nothing, `max_images_per_agent` capped
nothing, and with suppression off every page image was sent twice. These tests
run the real branch with Bedrock stubbed and assert on the payload.

Requires the real strands package (skipped in CI / when unavailable, per the
conftest in this directory). Run with: pytest -m agentic
tests/unit/extraction/agentic_idp/
"""

import io
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from idp_common.extraction.service import ExtractionService, SectionInfo

pytestmark = pytest.mark.agentic

TASK_PROMPT = "Extract:\n{DOCUMENT_TEXT}\n{DOCUMENT_IMAGE}\nEnd."

SCHEMA: dict[str, Any] = {
    "type": "object",
    "$id": "Statement",
    "x-aws-idp-document-type": "Statement",
    "properties": {
        "account": {"type": "string"},
        "transactions": {
            "type": "array",
            "minItems": 50,
            "items": {
                "type": "object",
                "properties": {
                    "row_id": {"type": "string"},
                    "description": {"type": "string"},
                    "amount": {"type": "string"},
                },
            },
        },
    },
}


class _Result(BaseModel):
    account: str | None = None


def _png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (1, 1)).save(buf, format="PNG")
    return buf.getvalue()


def _table_page(page_index: int, rows: int = 40) -> str:
    body = "\n".join(
        f"| {page_index}-{i} | Row {i} | {i * 10}.00 |" for i in range(rows)
    )
    return "| RowID | Description | Amount |\n|---|---|---|\n" + body


def _service(*, lazy_images: bool, cap: int) -> ExtractionService:
    config = {
        "extraction": {
            "model": "us.anthropic.claude-sonnet-5",
            "task_prompt": TASK_PROMPT,
            "agentic": {
                "enabled": True,
                # 1 batch keeps the single-pass path (no sharding), which is the
                # path under test.
                "max_concurrent_batches": 1,
                "max_images_per_agent": cap,
                "table_parsing": {"enabled": True, "lazy_images": lazy_images},
            },
        },
        "classes": [SCHEMA],
    }
    svc = ExtractionService(region="us-west-2", config=config)
    svc._class_label = "Statement"
    svc._class_schema = SCHEMA
    svc._attribute_descriptions = "account: the account number"
    return svc


def _invoke(svc: ExtractionService, n_pages: int) -> dict[str, Any]:
    """Run the agentic branch with Bedrock stubbed; return the captured kwargs."""
    pages = [_table_page(p) for p in range(n_pages)]
    svc._page_texts = pages
    svc._document_text = "\n".join(f"--- PAGE {i + 1} ---\n{t}" for i, t in enumerate(pages))
    images = [_png() for _ in range(n_pages)]
    svc._page_images = images
    content = svc._build_prompt_content(TASK_PROMPT, images)
    assert _n_images(content) == n_pages, "precondition: substitution attached images"

    section_info = SectionInfo(
        class_label="Statement",
        sorted_page_ids=[str(p + 1) for p in range(n_pages)],
        page_indices=list(range(n_pages)),
        output_bucket="b",
        output_key="k",
        output_uri="s3://b/k",
        start_page=1,
        end_page=n_pages,
    )
    captured: dict[str, Any] = {}

    def fake_structured_output(**kwargs):
        captured.update(kwargs)
        return _Result(account="X"), {"metering": {}}

    with patch(
        "idp_common.extraction.service.structured_output",
        side_effect=fake_structured_output,
    ):
        svc._invoke_extraction_model(content, "system", section_info)
    assert captured, "structured_output was never called"
    return captured


def _n_images(blocks) -> int:
    n = 0
    for b in blocks:
        if isinstance(b, dict):
            n += 1 if b.get("image") else 0
        else:
            n += 1 if getattr(b, "image", None) else 0
    return n


def _images_sent(captured: dict[str, Any]) -> int:
    """How many images Bedrock would actually receive for this invocation.

    Counted from the composed first turn, not from the prompt alone: the second
    copy of the page images was appended downstream in `_prepare_prompt_content`,
    so a test that looks only at `prompt` cannot see a double attachment.
    """
    from idp_common.extraction.agentic_idp import _prepare_prompt_content

    composed = _prepare_prompt_content(
        prompt=captured["prompt"],
        page_images=captured.get("page_images"),
        existing_data=None,
        model_id="us.anthropic.claude-sonnet-5",
        attach_page_images=captured.get("attach_page_images", True),
    )
    return _n_images(composed)


def test_lazy_images_removes_images_from_the_sent_prompt():
    svc = _service(lazy_images=True, cap=0)
    captured = _invoke(svc, n_pages=3)
    assert _images_sent(captured) == 0
    # The pages still travel, but only so the agent can pull one on demand.
    assert captured["attach_page_images"] is False
    assert len(captured["page_images"]) == 3


def test_cap_applies_to_the_sent_prompt_when_lazy_images_off():
    svc = _service(lazy_images=False, cap=2)
    captured = _invoke(svc, n_pages=5)
    assert _images_sent(captured) == 2


def test_no_double_attachment_when_nothing_is_suppressed():
    svc = _service(lazy_images=False, cap=0)  # 0 = unlimited
    captured = _invoke(svc, n_pages=4)
    # Exactly the four pages the substitution placed — not eight.
    assert _images_sent(captured) == 4
