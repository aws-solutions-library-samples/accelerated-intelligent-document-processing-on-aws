# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Per-page texts for the confidence pass line up with the page images (#2032).

A decision model with ``input: both`` pairs image *i* with text *i*. Extraction
keeps ``_page_texts`` per section page id ("" for a page the document lacks), while
``_load_document_images`` skips such a page, so passing ``_page_texts`` as is put
every image after a missing page beside the wrong page's text. The texts sent to
the confidence pass are built from the same page ids as the images.
"""

from __future__ import annotations

import io
from typing import Any, Dict
from unittest.mock import patch

import pytest
from PIL import Image

from idp_common.extraction.service import ExtractionService, SectionInfo
from idp_common.models import Document, Page, Section, Status

pytestmark = pytest.mark.unit

_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "Invoice",
    "x-aws-idp-document-type": "Invoice",
    "type": "object",
    "properties": {"Total": {"type": "string", "description": "The total"}},
}


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buf, format="PNG")
    return buf.getvalue()


def _document() -> Document:
    """Pages 1 and 3 only: the section below also lists page 2."""
    document = Document(
        id="d", input_key="d.pdf", output_bucket="out", status=Status.EXTRACTING
    )
    for n in (1, 3):
        document.pages[str(n)] = Page(
            page_id=str(n),
            image_uri=f"s3://in/{n}.png",
            parsed_text_uri=f"s3://in/{n}.txt",
        )
    return document


def _section_info() -> SectionInfo:
    return SectionInfo(
        class_label="Invoice",
        sorted_page_ids=["1", "2", "3"],
        page_indices=[0, 1, 2],
        output_bucket="out",
        output_key="d.pdf/sections/1/result.json",
        output_uri="s3://out/d.pdf/sections/1/result.json",
        start_page=1,
        end_page=3,
    )


def test_a_missing_middle_page_does_not_shift_the_texts_beside_the_images():
    service = ExtractionService(region="us-west-2", config={"classes": [_SCHEMA]})
    document = _document()
    section = Section(
        section_id="1", classification="Invoice", page_ids=["1", "2", "3"]
    )
    texts = {"s3://in/1.txt": "text of page 1", "s3://in/3.txt": "text of page 3"}
    with (
        patch("idp_common.s3.get_text_content", side_effect=texts.__getitem__),
        patch("idp_common.image.prepare_image", return_value=_png()),
    ):
        service._prepare_section_context(document, section, _section_info())

    sent: Dict[str, Any] = {}

    def capture(_service, **kwargs):
        sent.update(kwargs)
        return {"assessment": {}, "alerts": [], "metering": {}}

    with patch.object(service, "_assess_results_batched", side_effect=capture):
        service._assess_single_agent(
            extracted_fields={"Total": "1.00"},
            section_info=_section_info(),
            metering={},
        )

    assert len(sent["page_images"]) == 2
    assert sent["page_texts"] == ["text of page 1", "text of page 3"]


def test_each_shard_sends_the_texts_of_the_pages_whose_images_it_sends():
    config = {
        "classes": [_SCHEMA],
        "extraction": {
            "agentic": {
                "enabled": True,
                "max_concurrent_batches": 4,
                "max_pages_per_shard": 1,
            }
        },
    }
    service = ExtractionService(region="us-west-2", config=config)
    section = Section(
        section_id="1", classification="Invoice", page_ids=["1", "2", "3"]
    )
    texts = {"s3://in/1.txt": "text of page 1", "s3://in/3.txt": "text of page 3"}
    with (
        patch("idp_common.s3.get_text_content", side_effect=texts.__getitem__),
        patch("idp_common.image.prepare_image", return_value=_png()),
    ):
        service._prepare_section_context(_document(), section, _section_info())
    payloads = service._build_shard_payloads(
        prompt_template="Extract from:\n{DOCUMENT_TEXT}\nEnd.",
        send_images=True,
        max_shards=4,
    )

    assert len(payloads) == 3
    # Image i of a shard sits beside text i: page 3's image is never sent with the
    # missing page 2's empty text.
    assert [len(p["assess_page_images"]) for p in payloads] == [1, 1, 0]
    assert [p["assess_page_texts"] for p in payloads] == [
        ["text of page 1"],
        ["text of page 3"],
        [],
    ]
