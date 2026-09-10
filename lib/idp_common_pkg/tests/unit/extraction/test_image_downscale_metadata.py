# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#778: an oversize page image is fitted when the section's images are loaded,
the reduction is recorded for the section's metadata, and the record is reset
with the per-section context (not the per-invocation one, which runs AFTER the
images are loaded and would wipe it before _save_results reads it)."""

import io
import random
from unittest.mock import patch

import pytest
from PIL import Image

from idp_common.extraction.service import ExtractionService
from idp_common.image import BEDROCK_IMAGE_MAX_ENCODED_BYTES, base64_encoded_size
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


def test_reset_context_clears_the_record_for_the_next_section(service, document):
    with patch("idp_common.image.prepare_image", return_value=_oversize_png()):
        service._load_document_images(document, ["1"])
    assert service._pending_image_fit_metadata
    service._reset_context()
    assert service._pending_image_fit_metadata is None
