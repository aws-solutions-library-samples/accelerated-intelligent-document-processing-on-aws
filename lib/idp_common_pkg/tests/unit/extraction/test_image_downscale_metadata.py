# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#778: an oversize page image is fitted when the section's images are loaded,
the reduction is recorded for the section's metadata, and the record is reset
with the per-section context (not the per-invocation one, which runs AFTER the
images are loaded and would wipe it before _save_results reads it).

#994 extends the same load-time fit with Bedrock's many-image DIMENSION cap:
above 20 image blocks in one request every image is capped at 2,000 px per side.
Applying it here as well as in the Bedrock client keeps the reduction auditable
in section metadata and covers the agentic (Strands) path, which builds its own
requests and so bypasses ``BedrockClient.invoke_model``."""

import io
import random
from unittest.mock import patch

import pytest
from PIL import Image

from idp_common.extraction.service import ExtractionService
from idp_common.image import (
    BEDROCK_IMAGE_MAX_ENCODED_BYTES,
    BEDROCK_MANY_IMAGE_MAX_DIMENSION,
    base64_encoded_size,
)
from idp_common.models import Document, Page, Status

pytestmark = pytest.mark.unit


def _oversize_png() -> bytes:
    rng = random.Random(5)
    w = h = 1180  # ~4.2 MB raw PNG: under 5 MiB, over the 3.75 MiB raw budget
    img = Image.frombytes(
        "RGB", (w, h), bytes(rng.getrandbits(8) for _ in range(w * h * 3))
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _small_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (400, 500), (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def service():
    return ExtractionService(
        region="us-west-2",
        config={
            "classes": [],
            "extraction": {"model": "us.anthropic.claude-sonnet-4-6"},
        },
    )


@pytest.fixture
def document():
    doc = Document(
        id="d",
        input_key="d.pdf",
        input_bucket="in",
        output_bucket="out",
        status=Status.EXTRACTING,
    )
    for pid in ("1", "2"):
        doc.pages[pid] = Page(
            page_id=pid,
            image_uri=f"s3://in/d.pdf/pages/{pid}/image.png",
            parsed_text_uri=f"s3://in/d.pdf/pages/{pid}/parsed.txt",
        )
    return doc


def test_oversize_page_is_fitted_and_recorded_per_page(service, document):
    big, small = _oversize_png(), _small_png()
    assert base64_encoded_size(len(big)) > BEDROCK_IMAGE_MAX_ENCODED_BYTES
    with patch("idp_common.image.prepare_image", side_effect=[big, small]):
        images = service._load_document_images(document, ["1", "2"])

    assert len(images) == 2
    assert base64_encoded_size(len(images[0])) <= BEDROCK_IMAGE_MAX_ENCODED_BYTES
    assert images[1] is small, "an in-budget page passes through byte-identical"

    pending = service._pending_image_fit_metadata
    assert pending is not None and len(pending) == 1
    entry = pending[0]
    assert entry["page_id"] == "1"
    assert entry["original_bytes"] == len(big)
    assert entry["final_encoded_bytes"] <= BEDROCK_IMAGE_MAX_ENCODED_BYTES
    assert entry["passes"] >= 1 and "encoded size" in entry["reason"]


def test_no_entry_when_every_page_fits(service, document):
    with patch("idp_common.image.prepare_image", return_value=_small_png()):
        service._load_document_images(document, ["1", "2"])
    assert service._pending_image_fit_metadata is None


def _wide_png(size=(1585, 2048)) -> bytes:
    """A flat page just over the 2,000 px many-image cap and well under the
    byte budget, so only the dimension rule can trigger a fit."""
    buf = io.BytesIO()
    Image.new("RGB", size, (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _doc_with_pages(count: int) -> Document:
    doc = Document(
        id="d",
        input_key="d.pdf",
        input_bucket="in",
        output_bucket="out",
        status=Status.EXTRACTING,
    )
    for i in range(1, count + 1):
        doc.pages[str(i)] = Page(
            page_id=str(i),
            image_uri=f"s3://in/d.pdf/pages/{i}/image.png",
            parsed_text_uri=f"s3://in/d.pdf/pages/{i}/parsed.txt",
        )
    return doc


def _page_dimensions(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as img:
        return img.size


def test_a_section_over_twenty_pages_is_clamped_to_the_many_image_cap(service):
    """21 pages means 21 image blocks in one request, which drops the per-image
    cap to 2,000 px — each page is individually legal, so only the count reveals
    it (#994)."""
    doc = _doc_with_pages(21)
    page_ids = [str(i) for i in range(1, 22)]
    with patch("idp_common.image.prepare_image", return_value=_wide_png()):
        images = service._load_document_images(doc, page_ids)

    assert len(images) == 21
    assert all(
        max(_page_dimensions(img)) <= BEDROCK_MANY_IMAGE_MAX_DIMENSION for img in images
    )
    pending = service._pending_image_fit_metadata
    assert pending is not None and len(pending) == 21
    assert "px exceeds 2000 px per side" in pending[0]["reason"]


def test_a_twenty_page_section_keeps_full_resolution(service):
    """At exactly 20 the stricter cap does not apply, so nothing is degraded."""
    doc = _doc_with_pages(20)
    page_ids = [str(i) for i in range(1, 21)]
    with patch("idp_common.image.prepare_image", return_value=_wide_png()):
        images = service._load_document_images(doc, page_ids)

    assert all(_page_dimensions(img) == (1585, 2048) for img in images)
    assert service._pending_image_fit_metadata is None


def test_missing_pages_do_not_count_toward_the_threshold(service):
    """``sorted_page_ids`` can name pages the document does not have; those are
    skipped, so they must not push the count over the threshold and clamp the
    pages that ARE sent."""
    doc = _doc_with_pages(5)
    page_ids = [str(i) for i in range(1, 30)]  # 24 of them are absent
    with patch("idp_common.image.prepare_image", return_value=_wide_png()):
        images = service._load_document_images(doc, page_ids)

    assert len(images) == 5
    assert all(_page_dimensions(img) == (1585, 2048) for img in images)


def _agentic_service(**agentic_overrides) -> ExtractionService:
    return ExtractionService(
        region="us-west-2",
        config={
            "classes": [],
            "extraction": {
                "model": "us.anthropic.claude-sonnet-4-6",
                "agentic": {"enabled": True, **agentic_overrides},
            },
        },
    )


def _load(service: ExtractionService, pages: int) -> list[bytes]:
    doc = _doc_with_pages(pages)
    with patch("idp_common.image.prepare_image", return_value=_wide_png()):
        return service._load_document_images(doc, [str(i) for i in range(1, pages + 1)])


def test_agentic_mode_halves_the_effective_page_threshold():
    """Strands re-sends the attached pages each turn and ``view_image`` adds a
    second copy of a page to the same request, so a single agent invocation
    carrying 11 pages can present 22 image blocks. On the default agentic path
    (``max_concurrent_batches`` = 1, so no sharding) the whole section goes to one
    agent, and the threshold therefore lands at 11 pages rather than 21. Clamping
    early costs ~15% of the image tokens; not clamping costs the whole request."""
    images = _load(_agentic_service(), 11)
    assert all(
        max(_page_dimensions(img)) <= BEDROCK_MANY_IMAGE_MAX_DIMENSION for img in images
    )


def test_agentic_mode_counts_what_one_request_carries_not_the_whole_section():
    """The count that matters is per REQUEST. With sharding on, the section's pages
    are split into ``max_pages_per_shard``-page requests, so a 30-page section is
    sent as six 5-image requests — 10 blocks each after doubling, nowhere near the
    threshold. Counting the section instead would downscale all 30 pages for a
    limit no request comes close to."""
    images = _load(
        _agentic_service(max_concurrent_batches=4, max_pages_per_shard=5), 30
    )
    assert len(images) == 30
    assert all(_page_dimensions(img) == (1585, 2048) for img in images)


def test_the_per_agent_image_cap_also_bounds_the_count():
    """``max_images_per_agent`` caps how many pages are attached to one
    invocation, so a section far above the threshold still only ever presents that
    many attached blocks. At a cap of 4 (8 after doubling) nothing is clamped."""
    images = _load(_agentic_service(max_images_per_agent=4), 40)
    assert len(images) == 40
    assert all(_page_dimensions(img) == (1585, 2048) for img in images)


def test_reset_context_clears_the_record_for_the_next_section(service, document):
    with patch("idp_common.image.prepare_image", return_value=_oversize_png()):
        service._load_document_images(document, ["1"])
    assert service._pending_image_fit_metadata
    service._reset_context()
    assert service._pending_image_fit_metadata is None
