# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Filled-in form values must reach the page image, however the PDF stores them.

The earlier tests for #240 replaced pypdfium2 with mocks and asserted that
``flatten()`` was called. That held while the rendered image lost every value
stored in a FreeText annotation (#1780), because whether a value survives is a
property of the pixels, which a mock never produces. These tests render real
PDFs, one per storage shape in ``pdf_form_fixtures``, and count the blue pixels
the value is drawn in.

The first group pins that each fixture discriminates: the two broken rendering
sequences are run directly and must lose the value they are known to lose. A
fixture that rendered correctly under every sequence would let the later tests
pass against any implementation.
"""

import io
from typing import Dict
from unittest.mock import MagicMock, patch

import pypdfium2 as pdfium
import pytest
from PIL import Image

from idp_common.image.pdf_pages import load_page_for_render
from idp_common.models import Document, Status
from idp_common.ocr import service as service_module
from idp_common.ocr.service import OcrService

from . import pdf_form_fixtures as fixtures

pytestmark = pytest.mark.unit

MIN_VALUE_PIXELS = fixtures.MIN_VALUE_PIXELS

SHAPES_WITH_VALUE = [name for name, (_, has) in fixtures.SHAPES.items() if has]


def _render(page) -> Image.Image:
    return page.render(scale=2).to_pil()


def _flatten_and_render_stale_handle(pdf_bytes: bytes) -> Image.Image:
    """The v0.5.3 sequence: flatten, then render the page handle already held."""
    pdf = pdfium.PdfDocument(pdf_bytes)
    pdf.init_forms()
    page = pdf[0]
    if page.formenv is not None:
        page.flatten()
    return _render(page)


def _render_without_form_engine(pdf_bytes: bytes) -> Image.Image:
    """The pre-0.5.3 sequence: no ``init_forms()`` and no flattening."""
    return _render(pdfium.PdfDocument(pdf_bytes)[0])


def _render_with_helper(pdf_bytes: bytes) -> Image.Image:
    pdf = pdfium.PdfDocument(pdf_bytes)
    pdf.init_forms()
    return _render(load_page_for_render(pdf, 0))


class TestFixturesDiscriminate:
    def test_a_stale_handle_after_flatten_loses_freetext_values(self):
        image = _flatten_and_render_stale_handle(fixtures.freetext_with_acroform())
        assert fixtures.blue_pixels_in_value_rect(image) == 0

    def test_no_form_engine_loses_values_held_only_in_the_field(self):
        image = _render_without_form_engine(fixtures.widget_needs_appearance())
        assert fixtures.blue_pixels_in_value_rect(image) == 0

    def test_the_freetext_fixture_has_a_form_environment(self):
        """Without one it would never take the flatten branch it exists to test."""
        pdf = pdfium.PdfDocument(fixtures.freetext_with_acroform())
        pdf.init_forms()
        assert pdf[0].formenv is not None


class TestLoadPageForRender:
    @pytest.mark.parametrize("shape", SHAPES_WITH_VALUE)
    def test_the_filled_in_value_is_rendered(self, shape):
        builder, _ = fixtures.SHAPES[shape]
        image = _render_with_helper(builder())
        assert fixtures.blue_pixels_in_value_rect(image) >= MIN_VALUE_PIXELS

    @pytest.mark.parametrize("shape", list(fixtures.SHAPES))
    def test_static_page_content_is_still_rendered(self, shape):
        builder, _ = fixtures.SHAPES[shape]
        image = _render_with_helper(builder())
        assert fixtures.dark_pixels(image) > 0

    def test_a_page_without_a_form_environment_is_not_flattened(self):
        pdf = MagicMock()
        page = MagicMock()
        page.formenv = None
        pdf.__getitem__.return_value = page

        assert load_page_for_render(pdf, 0) is page
        page.flatten.assert_not_called()
        page.close.assert_not_called()

    def test_a_flattened_page_is_reloaded_before_it_is_returned(self):
        pdf = MagicMock()
        stale, fresh = MagicMock(), MagicMock()
        pdf.__getitem__.side_effect = [stale, fresh]

        assert load_page_for_render(pdf, 3) is fresh
        stale.flatten.assert_called_once()
        stale.close.assert_called_once()
        assert [c.args for c in pdf.__getitem__.call_args_list] == [(3,), (3,)]


class TestOcrServiceRendersFormValues:
    """The OCR service's real render path, with only S3 and OCR replaced."""

    def _rendered_pages(self, pdf_bytes: bytes) -> Dict[int, Image.Image]:
        with patch(
            "boto3.client",
            side_effect=lambda name, **kw: MagicMock(name=f"{name}_client"),
        ):
            service = OcrService(backend="none")
        body = MagicMock()
        body.read.return_value = pdf_bytes
        service.s3_client.get_object.return_value = {"Body": body}

        captured: Dict[int, Image.Image] = {}

        def capture(page_index, image_bytes, *_args):
            captured[page_index] = Image.open(io.BytesIO(image_bytes))
            uri = f"s3://out/doc.pdf/pages/{page_index + 1}"
            return (
                {
                    "image_uri": f"{uri}/image.jpg",
                    "raw_text_uri": f"{uri}/rawText.json",
                    "parsed_text_uri": f"{uri}/result.json",
                    "text_confidence_uri": f"{uri}/textConfidence.json",
                },
                {},
            )

        document = Document(
            id="d1",
            input_bucket="in",
            input_key="doc.pdf",
            output_bucket="out",
            status=Status.OCR,
        )
        with (
            patch.object(service_module, "s3", MagicMock()),
            patch.object(service, "_process_page_with_image", side_effect=capture),
        ):
            result = service.process_document(document)
        assert not result.errors
        return captured

    @pytest.mark.parametrize("shape", list(fixtures.SHAPES))
    def test_the_page_image_keeps_the_filled_in_value(self, shape):
        # One document per shape: pypdfium2's import_pages does not carry the
        # AcroForm dictionary across, so merging the shapes into one document
        # would silently remove the form environment the widget shapes need.
        builder, has_value = fixtures.SHAPES[shape]
        pages = self._rendered_pages(builder())

        assert list(pages) == [0]
        blue = fixtures.blue_pixels_in_value_rect(pages[0])
        if has_value:
            assert blue >= MIN_VALUE_PIXELS, f"{shape}: value missing from image"
        else:
            assert blue == 0, f"{shape}: unexpected value pixels"
        assert fixtures.dark_pixels(pages[0]) > 0, f"{shape}: page is blank"
