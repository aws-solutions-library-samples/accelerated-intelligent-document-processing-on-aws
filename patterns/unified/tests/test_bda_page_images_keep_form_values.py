# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""BDA mode's page images must keep filled-in form values (#240, #1780).

``create_pdf_page_images`` in the BDA results processor renders the page images
the Visual Editor shows in BDA mode, separately from the OCR service. It is run
here with real pypdfium2 over one synthetic PDF per storage shape, with only S3
replaced, and the uploaded JPEG is checked for the blue value pixels. The
fixtures and the pixel counters are the ones the library's OCR rendering tests
use, loaded from that tree so both renderers are held to the same shapes.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image

PATTERN_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PATTERN_ROOT.parents[1]
HANDLER_PATH = PATTERN_ROOT / "src" / "bda_processresults_function" / "index.py"
FIXTURES_PATH = (
    REPO_ROOT
    / "lib"
    / "idp_common_pkg"
    / "tests"
    / "unit"
    / "ocr"
    / "pdf_form_fixtures.py"
)

MIN_VALUE_PIXELS = 100


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fixtures = _load("pdf_form_fixtures_for_bda", FIXTURES_PATH)


@pytest.fixture
def handler_module(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    name = "bda_processresults_page_images_under_test"
    try:
        yield _load(name, HANDLER_PATH)
    finally:
        sys.modules.pop(name, None)


def _uploaded_images(handler_module, monkeypatch, pdf_bytes: bytes):
    s3 = MagicMock()
    body = MagicMock()
    body.read.return_value = pdf_bytes
    s3.get_object.return_value = {"Body": body}
    uploads = {}
    s3.upload_fileobj.side_effect = lambda buf, _bucket, key, **_kw: uploads.update(
        {key: Image.open(io.BytesIO(buf.read()))}
    )
    monkeypatch.setattr(handler_module, "s3_client", s3)

    count = handler_module.create_pdf_page_images("bda-bucket", "out", "doc.pdf")
    assert count == len(uploads)
    return uploads


@pytest.mark.parametrize("shape", list(fixtures.SHAPES))
def test_the_page_image_keeps_the_filled_in_value(handler_module, monkeypatch, shape):
    builder, has_value = fixtures.SHAPES[shape]
    uploads = _uploaded_images(handler_module, monkeypatch, builder())

    assert list(uploads) == ["doc.pdf/pages/1/image.jpg"]
    image = uploads["doc.pdf/pages/1/image.jpg"]
    blue = fixtures.blue_pixels_in_value_rect(image)
    if has_value:
        assert blue >= MIN_VALUE_PIXELS, f"{shape}: value missing from image"
    else:
        assert blue == 0, f"{shape}: unexpected value pixels"
    assert fixtures.dark_pixels(image) > 0, f"{shape}: page is blank"
