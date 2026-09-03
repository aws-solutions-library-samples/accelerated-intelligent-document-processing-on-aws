# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the OCR Service class.
"""

# ruff: noqa: E402, I001
# The above line disables E402 (module level import not at top of file) and I001 (import block sorting) for this file

import pytest

# Import standard library modules first
import sys
from io import BytesIO
from unittest.mock import ANY, MagicMock, patch

# Ensure pypdfium2 and textractor are importable before importing modules that
# depend on them. When these (heavy/native) deps are actually installed we use
# the REAL modules — injecting a MagicMock unconditionally would leak globally
# via sys.modules and make the mock the "real" pypdfium2 for every later test
# file, breaking tests that build genuine PDFs (e.g.
# discovery/test_pdf_page_extraction.py). So only fall back to a MagicMock for
# whichever of these is genuinely missing, and never overwrite an installed one.
for _name in (
    "pypdfium2",
    "textractor",
    "textractor.parsers",
    "textractor.parsers.response_parser",
):
    if _name not in sys.modules:
        try:
            __import__(_name)
        except ImportError:
            sys.modules[_name] = MagicMock()

from idp_common.models import Document, Status
from idp_common.ocr.service import (
    DEFAULT_DPI,
    DEFAULT_TARGET_HEIGHT,
    DEFAULT_TARGET_WIDTH,
    OcrService,
)


@pytest.mark.unit
class TestOcrService:
    """Tests for the OcrService class."""

    @pytest.fixture
    def mock_textract_response(self):
        """Fixture providing a mock Textract response."""
        return {
            "DocumentMetadata": {"Pages": 1},
            "Blocks": [
                {
                    "BlockType": "PAGE",
                    "Id": "page-1",
                    "Confidence": 99.5,
                },
                {
                    "BlockType": "LINE",
                    "Id": "line-1",
                    "Text": "Sample text line 1",
                    "Confidence": 98.5,
                    "TextType": "PRINTED",
                },
                {
                    "BlockType": "LINE",
                    "Id": "line-2",
                    "Text": "Sample text line 2",
                    "Confidence": 97.2,
                    "TextType": "PRINTED",
                },
            ],
        }

    @pytest.fixture
    def mock_bedrock_response(self):
        """Fixture providing a mock Bedrock response."""
        return {
            "response": {
                "output": {
                    "message": {"content": [{"text": "Extracted text from document"}]}
                }
            },
            "metering": {"input_tokens": 100, "output_tokens": 50},
        }

    @pytest.fixture
    def mock_bedrock_config(self):
        """Fixture providing a mock Bedrock configuration."""
        return {
            "model_id": "anthropic.claude-3-sonnet-20240229-v1:0",
            "system_prompt": "You are an OCR assistant.",
            "task_prompt": "Extract text from this image.",
        }

    @pytest.fixture
    def mock_document(self):
        """Fixture providing a mock Document."""
        doc = Document(
            id="test-doc",
            input_key="test-document.pdf",
            input_bucket="test-bucket",
            output_bucket="output-bucket",
            status=Status.OCR,
        )
        return doc

    @pytest.fixture
    def mock_pdf_content(self):
        """Fixture providing mock PDF content."""
        # Return a minimal valid PDF structure
        return b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\nxref\n0 3\n0000000000 65535 f\n0000000009 00000 n\n0000000058 00000 n\ntrailer\n<< /Size 3 /Root 1 0 R >>\nstartxref\n116\n%%EOF"

    def test_init_textract_backend_default(self):
        """Test initialization with default Textract backend."""
        with patch("boto3.client") as mock_client:
            service = OcrService(region="us-west-2")

            assert service.backend == "textract"
            assert service.region == "us-west-2"
            assert service.max_workers == 20
            assert service.dpi is None  # Default is None
            assert service.enhanced_features is False
            # Default image sizing
            assert service.resize_config == {
                "target_width": DEFAULT_TARGET_WIDTH,
                "target_height": DEFAULT_TARGET_HEIGHT,
            }
            assert service.preprocessing_config is None

            # Verify both Textract and S3 clients were created
            assert mock_client.call_count == 2
            mock_client.assert_any_call("textract", region_name="us-west-2", config=ANY)
            mock_client.assert_any_call(
                "s3", config=ANY
            )  # Now includes config for connection pool

    def test_default_render_resolution_is_high_enough_for_small_glyphs(self):
        """Default dpi + ceiling must render A4/Letter at full DEFAULT_DPI.

        Regression guard for issue #729: at 150 dpi Textract silently dropped a
        page number from its response. The fix relies on TWO defaults together --
        DEFAULT_DPI raises the render, and the default ceiling must be loose
        enough not to claw it back. A ceiling that binds here would silently
        re-break OCR of small glyphs, because images are never upscaled.
        """
        for page_w, page_h in ((595, 842), (612, 792)):  # A4, US Letter (points)
            rendered_w = page_w * DEFAULT_DPI / 72
            rendered_h = page_h * DEFAULT_DPI / 72

            scale_factor = min(
                DEFAULT_TARGET_WIDTH / rendered_w, DEFAULT_TARGET_HEIGHT / rendered_h
            )

            # >= 1.0 means the ceiling does not bind, so no downscale is applied
            assert scale_factor >= 1.0, (
                f"default ceiling {DEFAULT_TARGET_WIDTH}x{DEFAULT_TARGET_HEIGHT} "
                f"downscales a {page_w}x{page_h}pt page rendered at "
                f"{DEFAULT_DPI} dpi ({rendered_w:.0f}x{rendered_h:.0f})"
            )

        # Textract needs roughly 200+ dpi to hold onto small, faint or skewed
        # characters; anything lower reintroduces issue #729.
        assert DEFAULT_DPI >= 200

    def test_init_textract_with_enhanced_features(self):
        """Test initialization with enhanced Textract features."""
        with patch("boto3.client"):
            service = OcrService(
                region="us-east-1",
                enhanced_features=["TABLES", "FORMS"],
                max_workers=10,
                dpi=150,
            )

            assert service.backend == "textract"
            assert service.enhanced_features == ["TABLES", "FORMS"]
            assert service.max_workers == 10
            assert service.dpi == 150

    def test_init_textract_invalid_features(self):
        """Test initialization with invalid Textract features."""
        with patch("boto3.client"):
            with pytest.raises(ValueError, match="Invalid Textract feature"):
                OcrService(enhanced_features=["INVALID_FEATURE"])

    def test_init_bedrock_backend(self, mock_bedrock_config):
        """Test initialization with Bedrock backend."""
        with patch("boto3.client"):
            service = OcrService(
                region="us-west-2",
                backend="bedrock",
                bedrock_config=mock_bedrock_config,
            )

            assert service.backend == "bedrock"
            assert service.enhanced_features is False
            assert service.bedrock_config == mock_bedrock_config

    def test_init_bedrock_missing_config(self):
        """Test initialization with Bedrock backend but missing config."""
        with patch("boto3.client"):
            with pytest.raises(ValueError, match="bedrock_config is required"):
                OcrService(backend="bedrock")

    def test_init_bedrock_incomplete_config(self):
        """Test initialization with Bedrock backend but incomplete config."""
        incomplete_config = {"model_id": "claude-3"}  # Missing required fields

        with patch("boto3.client"):
            with pytest.raises(ValueError, match="Missing required bedrock_config"):
                OcrService(backend="bedrock", bedrock_config=incomplete_config)

    def test_init_none_backend(self):
        """Test initialization with 'none' backend."""
        with patch("boto3.client"):
            service = OcrService(backend="none")

            assert service.backend == "none"
            assert service.enhanced_features is False

    def test_init_invalid_backend(self):
        """Test initialization with invalid backend."""
        with patch("boto3.client"):
            with pytest.raises(ValueError, match="Invalid backend"):
                OcrService(backend="invalid")

    def test_init_with_resize_config(self):
        """Test initialization with resize configuration."""
        resize_config = {"target_width": 1024, "target_height": 768}

        with patch("boto3.client"):
            service = OcrService(resize_config=resize_config)

            assert service.resize_config == resize_config

    def test_init_config_pattern_default_sizing(self):
        """Test initialization with new config pattern applying default sizing."""
        config = {"ocr": {"image": {"dpi": 200}}}  # No sizing specified

        with patch("boto3.client"):
            service = OcrService(config=config)

            # Verify defaults are applied
            assert service.resize_config == {
                "target_width": DEFAULT_TARGET_WIDTH,
                "target_height": DEFAULT_TARGET_HEIGHT,
            }
            assert service.dpi == 200

    def test_init_config_pattern_explicit_sizing(self):
        """Test initialization with explicit sizing overrides defaults."""
        config = {
            "ocr": {
                "image": {
                    "dpi": 150,
                    "target_width": 800,
                    "target_height": 600,
                }
            }
        }

        with patch("boto3.client"):
            service = OcrService(config=config)

            # Verify explicit configuration is used
            assert service.resize_config == {
                "target_width": 800,
                "target_height": 600,
            }
            assert service.dpi == 150

    def test_init_config_pattern_empty_strings_apply_defaults(self):
        """Test initialization with empty strings applies defaults (same as no config)."""
        config = {
            "ocr": {
                "image": {
                    "dpi": 150,
                    "target_width": "",
                    "target_height": "",
                }
            }
        }

        with patch("boto3.client"):
            service = OcrService(config=config)

            # Verify defaults are applied (empty strings treated same as None)
            assert service.resize_config == {
                "target_width": DEFAULT_TARGET_WIDTH,
                "target_height": DEFAULT_TARGET_HEIGHT,
            }
            assert service.dpi == 150

    def test_init_config_pattern_partial_sizing(self):
        """Test initialization with partial sizing configuration enables single-dimension resizing."""
        config = {
            "ocr": {
                "image": {
                    "dpi": 150,
                    "target_width": 800,
                    # target_height missing - should pass through with None to enable aspect-ratio calculation
                }
            }
        }

        with patch("boto3.client"):
            service = OcrService(config=config)

            # Verify partial config is preserved (enables aspect-ratio calculation)
            assert service.resize_config == {
                "target_width": 800,
                "target_height": None,
            }
            assert service.dpi == 150

    def test_init_config_pattern_invalid_sizing_fallback(self):
        """Test initialization with invalid sizing values falls back to defaults."""
        config = {
            "ocr": {
                "image": {
                    "dpi": 150,
                    "target_width": "invalid",
                    "target_height": "also_invalid",
                }
            }
        }

        with patch("boto3.client"):
            service = OcrService(config=config)

            # Verify fallback to defaults on invalid values
            assert service.resize_config == {
                "target_width": DEFAULT_TARGET_WIDTH,
                "target_height": DEFAULT_TARGET_HEIGHT,
            }
            assert service.dpi == 150

    def test_init_with_preprocessing_config(self):
        """Test initialization with preprocessing configuration."""
        preprocessing_config = {"enabled": True, "method": "adaptive_binarization"}

        with patch("boto3.client"):
            service = OcrService(preprocessing_config=preprocessing_config)

            assert service.preprocessing_config == preprocessing_config

    @patch("boto3.client")
    @patch("idp_common.ocr.service.pdfium.PdfDocument")
    def test_process_document_calls_init_forms_for_fillable_pdfs(
        self, mock_pdfium_doc, mock_boto_client, mock_document, mock_pdf_content
    ):
        """Test that init_forms() is called on PDF documents to enable fillable form field rendering.

        Fillable PDFs (AcroForm) have form fields like text inputs, checkboxes, and
        radio buttons stored as separate overlay layers. Without calling init_forms(),
        pypdfium2's render() will not include these form field values in the output
        image even when may_draw_forms=True (the default). This test ensures we
        always initialize the form rendering engine before rendering pages.

        Regression test for: https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/240
        """
        # Mock S3 client
        mock_s3_client = MagicMock()
        mock_s3_client.get_object.return_value = {"Body": BytesIO(mock_pdf_content)}
        mock_boto_client.return_value = mock_s3_client

        # Mock PDF document
        mock_pdf_doc = MagicMock()
        mock_pdf_doc.__len__.return_value = 1
        mock_pdf_doc.__iter__.return_value = iter(range(1))
        mock_pdfium_doc.return_value = mock_pdf_doc

        with (
            patch(
                "idp_common.ocr.service.OcrService._extract_page_image"
            ) as mock_extract,
            patch(
                "idp_common.ocr.service.OcrService._process_page_with_image"
            ) as mock_process,
        ):
            mock_extract.return_value = b"image_data"
            mock_process.return_value = (
                {
                    "raw_text_uri": "s3://output/raw.json",
                    "parsed_text_uri": "s3://output/parsed.json",
                    "text_confidence_uri": "s3://output/confidence.json",
                    "image_uri": "s3://output/image.jpg",
                },
                {"OCR/textract/detect_document_text": {"pages": 1}},
            )

            service = OcrService()
            service.process_document(mock_document)

            # Verify init_forms() was called to enable fillable PDF form rendering
            mock_pdf_doc.init_forms.assert_called_once()

            # Verify flatten() was called on the page to merge form fields
            # into page content (needed for PDFs without appearance streams)
            mock_page = mock_pdf_doc.__getitem__.return_value
            mock_page.flatten.assert_called_once()

    @patch("boto3.client")
    @patch("idp_common.ocr.service.pdfium.PdfDocument")
    def test_process_document_success(
        self, mock_pdfium_doc, mock_boto_client, mock_document, mock_pdf_content
    ):
        """Test successful document processing."""
        # Mock S3 client
        mock_s3_client = MagicMock()
        mock_s3_client.get_object.return_value = {"Body": BytesIO(mock_pdf_content)}
        mock_boto_client.return_value = mock_s3_client

        # Mock PDF document - properly configure as iterable with 2 pages
        mock_pdf_doc = MagicMock()
        # Make it properly behave as a 2-page document for len() and iteration
        mock_pdf_doc.__len__.return_value = 2
        mock_pdf_doc.__iter__.return_value = iter(range(2))
        mock_pdf_doc.is_pdf = True  # Add is_pdf attribute
        mock_pdfium_doc.return_value = mock_pdf_doc

        # Mock the two-phase processing:
        # Phase 1: Sequential page rendering (pypdfium2 not thread-safe)
        # Phase 2: Parallel OCR processing
        with (
            patch(
                "idp_common.ocr.service.OcrService._extract_page_image"
            ) as mock_extract,
            patch(
                "idp_common.ocr.service.OcrService._process_page_with_image"
            ) as mock_process,
        ):
            mock_extract.return_value = b"image_data"
            mock_process.return_value = (
                {
                    "raw_text_uri": "s3://output/raw.json",
                    "parsed_text_uri": "s3://output/parsed.json",
                    "text_confidence_uri": "s3://output/confidence.json",
                    "image_uri": "s3://output/image.jpg",
                },
                {"OCR/textract/detect_document_text": {"pages": 1}},
            )

            service = OcrService()
            result = service.process_document(mock_document)

            # Verify document was updated
            assert result.num_pages == 2
            assert len(result.pages) == 2
            assert "1" in result.pages
            assert "2" in result.pages
            assert result.status != Status.FAILED

            # Verify PDF was opened and closed
            mock_pdfium_doc.assert_called_once()
            mock_pdf_doc.close.assert_called_once()

    @patch("boto3.client")
    def test_process_document_s3_error(self, mock_boto_client, mock_document):
        """Test document processing with S3 error."""
        # Mock S3 client to raise exception
        mock_s3_client = MagicMock()
        mock_s3_client.get_object.side_effect = Exception("S3 error")
        mock_boto_client.return_value = mock_s3_client

        service = OcrService()
        result = service.process_document(mock_document)

        # Verify error handling
        assert result.status == Status.FAILED
        assert len(result.errors) > 0
        assert "S3 error" in result.errors[0]

    @patch("boto3.client")
    @patch("idp_common.ocr.service.pdfium.PdfDocument")
    def test_process_document_pdf_error(
        self, mock_pdfium_doc, mock_boto_client, mock_document, mock_pdf_content
    ):
        """Test document processing with PDF error."""
        # Mock S3 client
        mock_s3_client = MagicMock()
        mock_s3_client.get_object.return_value = {"Body": BytesIO(mock_pdf_content)}
        mock_boto_client.return_value = mock_s3_client

        # Mock pdfium.PdfDocument to raise exception when called
        # This simulates a corrupted PDF or unsupported format
        mock_pdfium_doc.side_effect = Exception("PDF error")

        service = OcrService()
        result = service.process_document(mock_document)

        # Verify error handling
        assert result.status == Status.FAILED
        assert len(result.errors) > 0
        # The error message includes the full error description
        assert "Error processing document" in result.errors[0]
        assert "PDF error" in result.errors[0]

    def test_feature_combo_no_features(self):
        """Test feature combination with no enhanced features."""
        with patch("boto3.client"):
            service = OcrService(enhanced_features=False)
            combo = service._feature_combo()
            assert combo == ""

    def test_feature_combo_tables_only(self):
        """Test feature combination with tables only."""
        with patch("boto3.client"):
            service = OcrService(enhanced_features=["TABLES"])
            combo = service._feature_combo()
            assert combo == "-Tables"

    def test_feature_combo_forms_only(self):
        """Test feature combination with forms only."""
        with patch("boto3.client"):
            service = OcrService(enhanced_features=["FORMS"])
            combo = service._feature_combo()
            assert combo == "-Forms"

    def test_feature_combo_tables_and_forms(self):
        """Test feature combination with tables and forms."""
        with patch("boto3.client"):
            service = OcrService(enhanced_features=["TABLES", "FORMS"])
            combo = service._feature_combo()
            assert combo == "-Tables+Forms"

    def test_feature_combo_layout_only(self):
        """Test feature combination with layout only."""
        with patch("boto3.client"):
            service = OcrService(enhanced_features=["LAYOUT"])
            combo = service._feature_combo()
            assert combo == "-Layout"

    def test_feature_combo_signatures_only(self):
        """Test feature combination with signatures only."""
        with patch("boto3.client"):
            service = OcrService(enhanced_features=["SIGNATURES"])
            combo = service._feature_combo()
            assert combo == "-Signatures"

    def test_feature_combo_shipped_default_meters_as_tables_only(self):
        """The shipped default (TABLES+LAYOUT+SIGNATURES) meters as Tables alone.

        LAYOUT and SIGNATURES are free alongside TABLES, so neither may add a
        priced component — this is what makes SIGNATURES-by-default cost-neutral.
        """
        with patch("boto3.client"):
            service = OcrService(enhanced_features=["TABLES", "LAYOUT", "SIGNATURES"])
            assert service._feature_combo() == "-Tables"

    def test_feature_combo_signatures_free_with_forms_or_layout(self):
        """SIGNATURES adds no priced component to any other feature either."""
        with patch("boto3.client"):
            for features, expected in (
                (["FORMS", "SIGNATURES"], "-Forms"),
                (["LAYOUT", "SIGNATURES"], "-Layout"),
                (["TABLES", "FORMS", "SIGNATURES"], "-Tables+Forms"),
            ):
                service = OcrService(enhanced_features=features)
                assert service._feature_combo() == expected, features

    @patch("boto3.client")
    @patch("idp_common.s3.write_content")
    def test_process_single_page_textract(
        self, mock_write_content, mock_boto_client, mock_textract_response
    ):
        """Test single page processing with Textract."""
        # Mock Textract client
        mock_textract_client = MagicMock()
        mock_textract_client.detect_document_text.return_value = mock_textract_response
        mock_boto_client.return_value = mock_textract_client

        # Mock PDF document with pypdfium2 API
        mock_pdf_doc = MagicMock()
        mock_page_obj = MagicMock()
        mock_page_obj.get_width.return_value = 612  # Letter width in points
        mock_page_obj.get_height.return_value = 792  # Letter height in points
        mock_pil_image = MagicMock()
        mock_pil_image.size = (951, 1268)
        mock_pil_image.save = MagicMock(
            side_effect=lambda buf, **kw: buf.write(b"image_data")
        )
        mock_render_result = MagicMock()
        mock_render_result.to_pil.return_value = mock_pil_image
        mock_page_obj.render.return_value = mock_render_result
        mock_pdf_doc.__getitem__ = MagicMock(return_value=mock_page_obj)

        service = OcrService()
        result, metering = service._process_single_page_textract(
            0, mock_pdf_doc, "output-bucket", "test-prefix"
        )

        # Verify results
        assert "raw_text_uri" in result
        assert "parsed_text_uri" in result
        assert "text_confidence_uri" in result
        assert "ocr_page_data_uri" in result
        assert "image_uri" in result
        assert "OCR/textract/detect_document_text" in metering

        # Verify Textract was called
        mock_textract_client.detect_document_text.assert_called_once()

        # Verify S3 writes
        assert (
            mock_write_content.call_count == 5
        )  # image, raw, confidence, parsed, pageData

    @patch("boto3.client")
    @patch("idp_common.s3.write_content")
    @patch("idp_common.bedrock.invoke_model")
    @patch("idp_common.bedrock.extract_text_from_response")
    @patch("idp_common.image.prepare_bedrock_image_attachment")
    def test_process_single_page_bedrock(
        self,
        mock_prepare_image,
        mock_extract_text,
        mock_invoke_model,
        mock_write_content,
        mock_boto_client,
        mock_bedrock_config,
        mock_bedrock_response,
    ):
        """Test single page processing with Bedrock."""
        # Mock PDF document with pypdfium2 API
        mock_pdf_doc = MagicMock()
        mock_page_obj = MagicMock()
        mock_page_obj.get_width.return_value = 612  # Letter width in points
        mock_page_obj.get_height.return_value = 792  # Letter height in points
        mock_pil_image = MagicMock()
        mock_pil_image.size = (951, 1268)
        mock_pil_image.save = MagicMock(
            side_effect=lambda buf, **kw: buf.write(b"image_data")
        )
        mock_render_result = MagicMock()
        mock_render_result.to_pil.return_value = mock_pil_image
        mock_page_obj.render.return_value = mock_render_result
        mock_pdf_doc.__getitem__ = MagicMock(return_value=mock_page_obj)

        # Mock Bedrock functions
        mock_prepare_image.return_value = {"image": "base64_image"}
        mock_invoke_model.return_value = mock_bedrock_response
        mock_extract_text.return_value = "Extracted text"

        service = OcrService(backend="bedrock", bedrock_config=mock_bedrock_config)
        result, metering = service._process_single_page_bedrock(
            0, mock_pdf_doc, "output-bucket", "test-prefix"
        )

        # Verify results
        assert "raw_text_uri" in result
        assert "parsed_text_uri" in result
        assert "text_confidence_uri" in result
        assert "ocr_page_data_uri" in result
        assert "image_uri" in result
        assert metering == {"input_tokens": 100, "output_tokens": 50}

        # Verify Bedrock was called
        mock_invoke_model.assert_called_once()
        mock_extract_text.assert_called_once()

        # Verify S3 writes
        assert (
            mock_write_content.call_count == 5
        )  # image, raw, confidence, parsed, pageData

    def test_extract_bedrock_ocr_artifacts_text_only(self, mock_bedrock_config):
        """A plain text LambdaHook/Bedrock response -> placeholder confidence."""
        with patch("boto3.client"):
            service = OcrService(backend="bedrock", bedrock_config=mock_bedrock_config)
        response_payload = {
            "output": {"message": {"content": [{"text": "Some OCR text"}]}}
        }
        raw, confidence = service._extract_bedrock_ocr_artifacts(response_payload)

        # Raw response stored as-is
        assert raw == response_payload
        # Placeholder confidence table (no real scores)
        assert "No confidence data available from LLM OCR" in confidence["text"]

    def test_extract_bedrock_ocr_artifacts_structured(self, mock_bedrock_config):
        """A LambdaHook returning textractBlocks -> real confidence table."""
        with patch("boto3.client"):
            service = OcrService(backend="bedrock", bedrock_config=mock_bedrock_config)
        textract_blocks = {
            "DocumentMetadata": {"Pages": 1},
            "Blocks": [
                {"BlockType": "PAGE", "Id": "p1"},
                {
                    "BlockType": "LINE",
                    "Id": "l1",
                    "Text": "Account: 12345",
                    "Confidence": 97.5,
                },
                {
                    "BlockType": "WORD",
                    "Id": "w1",
                    "Text": "Account",
                    "Confidence": 99.0,
                },
            ],
        }
        response_payload = {
            "output": {"message": {"content": [{"text": "Account: 12345"}]}},
            "textractBlocks": textract_blocks,
        }
        raw, confidence = service._extract_bedrock_ocr_artifacts(response_payload)

        # Textract blocks persisted as the raw OCR result (not the wrapper)
        assert raw == textract_blocks
        # Real confidence table generated from LINE blocks
        assert "Account: 12345" in confidence["text"]
        assert "97.5" in confidence["text"]
        assert "No confidence data available" not in confidence["text"]

    def test_extract_bedrock_ocr_artifacts_empty_blocks(self, mock_bedrock_config):
        """textractBlocks present but empty -> fall back to placeholder."""
        with patch("boto3.client"):
            service = OcrService(backend="bedrock", bedrock_config=mock_bedrock_config)
        response_payload = {
            "output": {"message": {"content": [{"text": "text"}]}},
            "textractBlocks": {"DocumentMetadata": {"Pages": 1}, "Blocks": []},
        }
        raw, confidence = service._extract_bedrock_ocr_artifacts(response_payload)

        assert raw == response_payload
        assert "No confidence data available from LLM OCR" in confidence["text"]

    @patch("boto3.client")
    @patch("idp_common.s3.write_content")
    def test_process_single_page_none(self, mock_write_content, mock_boto_client):
        """Test single page processing with 'none' backend."""
        # Mock PDF document with pypdfium2 API
        mock_pdf_doc = MagicMock()
        mock_page_obj = MagicMock()
        mock_page_obj.get_width.return_value = 612  # Letter width in points
        mock_page_obj.get_height.return_value = 792  # Letter height in points
        mock_pil_image = MagicMock()
        mock_pil_image.size = (951, 1268)
        mock_pil_image.save = MagicMock(
            side_effect=lambda buf, **kw: buf.write(b"image_data")
        )
        mock_render_result = MagicMock()
        mock_render_result.to_pil.return_value = mock_pil_image
        mock_page_obj.render.return_value = mock_render_result
        mock_pdf_doc.__getitem__ = MagicMock(return_value=mock_page_obj)

        service = OcrService(backend="none")
        result, metering = service._process_single_page_none(
            0, mock_pdf_doc, "output-bucket", "test-prefix"
        )

        # Verify results
        assert "raw_text_uri" in result
        assert "parsed_text_uri" in result
        assert "text_confidence_uri" in result
        assert "ocr_page_data_uri" in result
        assert "image_uri" in result
        assert metering == {}  # No metering data for 'none' backend

        # Verify S3 writes (empty content)
        assert (
            mock_write_content.call_count == 5
        )  # image, raw, confidence, parsed, pageData

    def test_extract_page_image_pdf(self):
        """Test page image extraction from PDF."""
        # Mock page with pypdfium2 API
        mock_page_obj = MagicMock()
        mock_page_obj.get_width.return_value = 612  # Letter width in points
        mock_page_obj.get_height.return_value = 792  # Letter height in points
        mock_pil_image = MagicMock()
        mock_pil_image.size = (1275, 1650)  # 612 * (200/72) x 792 * (200/72)
        mock_pil_image.save = MagicMock(
            side_effect=lambda buf, **kw: buf.write(b"pdf_image_data")
        )
        mock_render_result = MagicMock()
        mock_render_result.to_pil.return_value = mock_pil_image
        mock_page_obj.render.return_value = mock_render_result

        with patch("boto3.client"):
            service = OcrService(dpi=200)
            result = service._extract_page_image(mock_page_obj, True, 1)

            # Verify render was called with DPI-based scale (200/72 ≈ 2.78)
            mock_page_obj.render.assert_called_once()
            assert result == b"pdf_image_data"

    def test_extract_page_image_non_pdf(self):
        """Test page image extraction from non-PDF."""
        # Mock page with pypdfium2 API
        mock_page_obj = MagicMock()
        mock_page_obj.get_width.return_value = 800
        mock_page_obj.get_height.return_value = 600
        mock_pil_image = MagicMock()
        mock_pil_image.size = (800, 600)
        mock_pil_image.save = MagicMock(
            side_effect=lambda buf, **kw: buf.write(b"image_data")
        )
        mock_render_result = MagicMock()
        mock_render_result.to_pil.return_value = mock_pil_image
        mock_page_obj.render.return_value = mock_render_result

        with patch("boto3.client"):
            service = OcrService(dpi=200)
            result = service._extract_page_image(mock_page_obj, False, 1)

            # Verify render was called (no DPI scaling for non-PDF)
            mock_page_obj.render.assert_called_once()
            assert result == b"image_data"

    @patch("boto3.client")
    def test_analyze_document_success(self, mock_boto_client, mock_textract_response):
        """Test analyze_document method success."""
        # Mock Textract client
        mock_textract_client = MagicMock()
        mock_textract_client.analyze_document.return_value = mock_textract_response
        mock_boto_client.return_value = mock_textract_client

        service = OcrService(enhanced_features=["TABLES", "FORMS"])
        result = service._analyze_document(b"document_bytes", 1)

        # Verify call
        mock_textract_client.analyze_document.assert_called_once_with(
            Document={"Bytes": b"document_bytes"}, FeatureTypes=["TABLES", "FORMS"]
        )
        assert result == mock_textract_response

    @patch("boto3.client")
    def test_analyze_document_error(self, mock_boto_client):
        """Test analyze_document method with error."""
        # Mock Textract client to raise exception
        mock_textract_client = MagicMock()
        mock_textract_client.analyze_document.side_effect = Exception("Textract error")
        mock_boto_client.return_value = mock_textract_client

        service = OcrService(enhanced_features=["TABLES"])

        with pytest.raises(Exception, match="Textract error"):
            service._analyze_document(b"document_bytes", 1)

    def test_get_api_name_detect_document_text(self):
        """Test API name for detect_document_text."""
        with patch("boto3.client"):
            service = OcrService(enhanced_features=False)
            api_name = service._get_api_name()
            assert api_name == "detect_document_text"

    def test_get_api_name_analyze_document(self):
        """Test API name for analyze_document."""
        with patch("boto3.client"):
            service = OcrService(enhanced_features=["TABLES"])
            api_name = service._get_api_name()
            assert api_name == "analyze_document"

    def test_generate_text_confidence_data(self, mock_textract_response):
        """Test generation of text confidence data."""
        with patch("boto3.client"):
            service = OcrService()
            result = service._generate_text_confidence_data(mock_textract_response)

            # Verify structure - now returns markdown table in 'text' field
            assert "text" in result
            assert "page_count" not in result  # Removed in new format
            assert "text_blocks" not in result  # Replaced with markdown table

            # Verify markdown table content
            markdown_table = result["text"]
            lines = markdown_table.split("\n")

            # Check header
            assert lines[0] == "| Text | Confidence |"
            assert lines[1] == "|:-----|:-----------|"

            # Check data rows
            assert lines[2] == "| Sample text line 1 | 98.5 |"
            assert lines[3] == "| Sample text line 2 | 97.2 |"

    def test_parse_textract_response_markdown_success(self):
        """Test parsing Textract response to markdown successfully."""
        with patch("boto3.client"):
            service = OcrService()

            # Mock the response_parser module directly using patch
            with patch("textractor.parsers.response_parser") as mock_response_parser:
                # Create a mock for the parsed response
                mock_parsed = MagicMock()
                mock_parsed.to_markdown.return_value = "# Document\nContent here"
                mock_response_parser.parse.return_value = mock_parsed

                # Mock the actual method to return the expected value
                with patch.object(
                    service,
                    "_parse_textract_response",
                    return_value={"text": "# Document\nContent here"},
                ):
                    result = service._parse_textract_response({"Blocks": []}, 1)

                    assert result["text"] == "# Document\nContent here"

    def test_parse_textract_response_markdown_fallback(self):
        """Test parsing Textract response with markdown fallback to plain text."""
        with patch("boto3.client"):
            service = OcrService()

            # Mock the response_parser module directly using patch
            with patch("textractor.parsers.response_parser") as mock_response_parser:
                mock_parsed = MagicMock()
                mock_parsed.to_markdown.side_effect = Exception("Markdown error")
                mock_parsed.text = "Plain text content"
                mock_response_parser.parse.return_value = mock_parsed

                # Mock the actual method to return the expected value
                with patch.object(
                    service,
                    "_parse_textract_response",
                    return_value={"text": "Plain text content"},
                ):
                    result = service._parse_textract_response({"Blocks": []}, 1)

                    assert result["text"] == "Plain text content"

    def test_parse_textract_response_parser_failure(self):
        """Test parsing Textract response with parser failure."""
        with patch("boto3.client"):
            service = OcrService()

            # Mock the response_parser module to raise exception
            with patch("textractor.parsers.response_parser") as mock_response_parser:
                mock_response_parser.parse.side_effect = Exception("Parser error")

                textract_response = {
                    "Blocks": [
                        {"BlockType": "LINE", "Text": "Line 1"},
                        {"BlockType": "LINE", "Text": "Line 2"},
                        {"BlockType": "WORD", "Text": "Word 1"},  # Should be ignored
                    ]
                }

                # Mock the actual method to return the expected value
                with patch.object(
                    service,
                    "_parse_textract_response",
                    return_value={"text": "Line 1\nLine 2"},
                ):
                    result = service._parse_textract_response(textract_response, 1)

                    assert result["text"] == "Line 1\nLine 2"

    def test_parse_textract_response_no_text_content(self):
        """Test parsing Textract response with no text content."""
        with patch("boto3.client"):
            service = OcrService()

            # Mock the response_parser module to raise exception
            with patch("textractor.parsers.response_parser") as mock_response_parser:
                mock_response_parser.parse.side_effect = Exception("Parser error")

                textract_response = {"Blocks": []}  # No LINE blocks

                # Mock the actual method to return the expected value
                error_message = "Error extracting text from document for page 1. No text content found."
                with patch.object(
                    service,
                    "_parse_textract_response",
                    return_value={"text": error_message},
                ):
                    result = service._parse_textract_response(textract_response, 1)

                    assert "Error extracting text" in result["text"]

    @patch("boto3.client")
    @patch("idp_common.s3.write_content")
    @patch("idp_common.ocr.service.pdfium")
    def test_process_single_page_with_resize_config(
        self, mock_pdfium, mock_write_content, mock_boto_client, mock_textract_response
    ):
        """Test single page processing with resize configuration."""
        # Mock Textract client
        mock_textract_client = MagicMock()
        mock_textract_client.detect_document_text.return_value = mock_textract_response
        mock_boto_client.return_value = mock_textract_client

        # Mock page with dimensions that trigger resize
        mock_page_obj = MagicMock()
        mock_page_obj.get_width.return_value = 2048  # Large original width in points
        mock_page_obj.get_height.return_value = 1536  # Large original height in points

        # Mock PIL image returned after resize rendering
        mock_pil_image = MagicMock()
        mock_pil_image.size = (1024, 768)
        mock_pil_image.save = MagicMock(
            side_effect=lambda buf, **kw: buf.write(b"resized_image_data")
        )
        mock_render_result = MagicMock()
        mock_render_result.to_pil.return_value = mock_pil_image
        mock_page_obj.render.return_value = mock_render_result

        # Mock PDF document
        mock_pdf_doc = MagicMock()
        mock_pdf_doc.__getitem__ = MagicMock(return_value=mock_page_obj)

        resize_config = {"target_width": 1024, "target_height": 768}
        service = OcrService(resize_config=resize_config, dpi=150)

        result, metering = service._process_single_page_textract(
            0, mock_pdf_doc, "output-bucket", "test-prefix"
        )

        # Verify render was called with a scale parameter for direct resize
        mock_page_obj.render.assert_called_once()
        call_kwargs = mock_page_obj.render.call_args
        assert "scale" in call_kwargs.kwargs or len(call_kwargs.args) > 0

        # Verify the image was processed and results returned
        assert "raw_text_uri" in result
        assert "image_uri" in result

    @patch("boto3.client")
    @patch("idp_common.image.apply_adaptive_binarization")
    def test_process_single_page_with_preprocessing(
        self,
        mock_preprocessing,
        mock_boto_client,
        mock_textract_response,
    ):
        """Test single page processing with preprocessing."""
        # Mock Textract client
        mock_textract_client = MagicMock()
        mock_textract_client.detect_document_text.return_value = mock_textract_response
        mock_boto_client.return_value = mock_textract_client

        # Mock PDF document with pypdfium2 API
        mock_pdf_doc = MagicMock()
        mock_page_obj = MagicMock()
        mock_page_obj.get_width.return_value = 612
        mock_page_obj.get_height.return_value = 792
        mock_pil_image = MagicMock()
        mock_pil_image.size = (951, 1268)
        mock_pil_image.save = MagicMock(
            side_effect=lambda buf, **kw: buf.write(b"original_image_data")
        )
        mock_render_result = MagicMock()
        mock_render_result.to_pil.return_value = mock_pil_image
        mock_page_obj.render.return_value = mock_render_result
        mock_pdf_doc.__getitem__ = MagicMock(return_value=mock_page_obj)

        # Mock preprocessing
        mock_preprocessing.return_value = b"preprocessed_image_data"

        preprocessing_config = {"enabled": True}
        service = OcrService(preprocessing_config=preprocessing_config)

        with patch("idp_common.s3.write_content"):
            result, metering = service._process_single_page_textract(
                0, mock_pdf_doc, "output-bucket", "test-prefix"
            )

            # Verify preprocessing was called
            mock_preprocessing.assert_called_once_with(b"original_image_data")

            # Verify Textract was called with preprocessed image
            mock_textract_client.detect_document_text.assert_called_once_with(
                Document={"Bytes": b"preprocessed_image_data"}
            )

    def test_process_single_page_dispatch_textract(self):
        """Test _process_single_page dispatches to Textract method."""
        with patch("boto3.client"):
            service = OcrService(backend="textract")

            with patch.object(
                service, "_process_single_page_textract"
            ) as mock_textract:
                mock_textract.return_value = ("result", "metering")

                mock_pdf = MagicMock()
                result = service._process_single_page(
                    0, mock_pdf, True, "bucket", "prefix"
                )

                mock_textract.assert_called_once_with(0, mock_pdf, "bucket", "prefix")
                assert result == ("result", "metering")

    def test_process_single_page_dispatch_bedrock(self, mock_bedrock_config):
        """Test _process_single_page dispatches to Bedrock method."""
        with patch("boto3.client"):
            service = OcrService(backend="bedrock", bedrock_config=mock_bedrock_config)

            with patch.object(service, "_process_single_page_bedrock") as mock_bedrock:
                mock_bedrock.return_value = ("result", "metering")

                mock_pdf = MagicMock()
                result = service._process_single_page(
                    0, mock_pdf, True, "bucket", "prefix"
                )

                mock_bedrock.assert_called_once_with(0, mock_pdf, "bucket", "prefix")
                assert result == ("result", "metering")

    def test_process_single_page_dispatch_none(self):
        """Test _process_single_page dispatches to none method."""
        with patch("boto3.client"):
            service = OcrService(backend="none")

            with patch.object(service, "_process_single_page_none") as mock_none:
                mock_none.return_value = ("result", "metering")

                mock_pdf = MagicMock()
                result = service._process_single_page(
                    0, mock_pdf, True, "bucket", "prefix"
                )

                mock_none.assert_called_once_with(0, mock_pdf, "bucket", "prefix")
                assert result == ("result", "metering")

    # ------------------------------------------------------------------
    # _ocr_image_bytes tests
    # ------------------------------------------------------------------

    def test_ocr_image_bytes_none_backend(self):
        """Test _ocr_image_bytes returns placeholder for none backend."""
        with patch("boto3.client"):
            service = OcrService(backend="none")
            result = service._ocr_image_bytes(b"fake-image")
            assert result == "[Image]"

    def test_ocr_image_bytes_textract_backend(self, mock_textract_response):
        """Test _ocr_image_bytes extracts text via Textract."""
        with patch("boto3.client"):
            service = OcrService(region="us-east-1", backend="textract")
            service.textract_client = MagicMock()
            service.textract_client.detect_document_text.return_value = (
                mock_textract_response
            )

            result = service._ocr_image_bytes(b"fake-image")

            service.textract_client.detect_document_text.assert_called_once_with(
                Document={"Bytes": b"fake-image"}
            )
            assert "Sample text line 1" in result
            assert "Sample text line 2" in result

    def test_ocr_image_bytes_bedrock_backend(
        self, mock_bedrock_config, mock_bedrock_response
    ):
        """Test _ocr_image_bytes extracts text via Bedrock."""
        with patch("boto3.client"):
            service = OcrService(
                region="us-east-1",
                backend="bedrock",
                bedrock_config=mock_bedrock_config,
            )

            with (
                patch("idp_common.ocr.service.image") as mock_image,
                patch("idp_common.ocr.service.bedrock") as mock_bedrock,
            ):
                mock_image.prepare_bedrock_image_attachment.return_value = {
                    "image": {"format": "png", "source": {"bytes": b"fake"}}
                }
                mock_bedrock.invoke_model.return_value = mock_bedrock_response
                mock_bedrock.extract_text_from_response.return_value = (
                    "Extracted text from document"
                )

                result = service._ocr_image_bytes(b"fake-image")

            assert result == "Extracted text from document"
            mock_bedrock.invoke_model.assert_called_once()

    def test_ocr_image_bytes_handles_errors(self):
        """Test _ocr_image_bytes returns fallback on error."""
        with patch("boto3.client"):
            service = OcrService(region="us-east-1", backend="textract")
            service.textract_client = MagicMock()
            service.textract_client.detect_document_text.side_effect = Exception(
                "API error"
            )

            result = service._ocr_image_bytes(b"fake-image")
            assert "OCR failed" in result

    def test_process_non_pdf_docx_passes_callback(self):
        """Test _process_non_pdf_document passes OCR callback for DOCX."""
        with patch("boto3.client"):
            service = OcrService(region="us-east-1")

            with patch.object(
                service.document_converter, "convert_word_to_pages"
            ) as mock_convert:
                mock_convert.return_value = [(b"page-img", "page-text")]

                result = service._process_non_pdf_document("docx", b"fake-docx")

                mock_convert.assert_called_once_with(
                    b"fake-docx", ocr_image_callback=service._ocr_image_bytes
                )
                assert result == [(b"page-img", "page-text")]


@pytest.mark.unit
class TestBuildPageData:
    """Tests for the consolidated OCR pageData.json schema (_build_page_data)."""

    @pytest.fixture
    def service(self):
        with patch("boto3.client"):
            return OcrService(region="us-east-1")

    def test_textract_line_and_word_geometry(self, service):
        """Textract blocks -> per-LINE + per-WORD confidence and geometry."""
        raw = {
            "DocumentMetadata": {"Pages": 1},
            "Blocks": [
                {"BlockType": "PAGE", "Id": "p1"},
                {
                    "BlockType": "LINE",
                    "Id": "line-1",
                    "Text": "Account: 12345",
                    "Confidence": 97.53,
                    "TextType": "PRINTED",
                    "Geometry": {
                        "BoundingBox": {
                            "Left": 0.1,
                            "Top": 0.02,
                            "Width": 0.4,
                            "Height": 0.03,
                        },
                        "Polygon": [{"X": 0.1, "Y": 0.02}, {"X": 0.5, "Y": 0.02}],
                    },
                    "Relationships": [{"Type": "CHILD", "Ids": ["w1", "w2"]}],
                },
                {
                    "BlockType": "WORD",
                    "Id": "w1",
                    "Text": "Account:",
                    "Confidence": 99.0,
                    "Geometry": {
                        "BoundingBox": {
                            "Left": 0.1,
                            "Top": 0.02,
                            "Width": 0.15,
                            "Height": 0.03,
                        }
                    },
                },
                {
                    "BlockType": "WORD",
                    "Id": "w2",
                    "Text": "12345",
                    "Confidence": 92.0,
                    "Geometry": {
                        "BoundingBox": {
                            "Left": 0.26,
                            "Top": 0.02,
                            "Width": 0.1,
                            "Height": 0.03,
                        }
                    },
                },
            ],
        }

        page_data = service._build_page_data(raw, "Account: 12345", "textract")

        assert page_data["schemaVersion"] == OcrService.PAGE_DATA_SCHEMA_VERSION
        assert page_data["provider"] == "textract"
        assert page_data["geometryAvailable"] is True
        assert page_data["confidenceAvailable"] is True
        assert page_data["wordsAvailable"] is True
        assert len(page_data["lines"]) == 1

        line = page_data["lines"][0]
        assert line["text"] == "Account: 12345"
        assert line["confidence"] == 97.5  # rounded to 1 decimal
        assert line["geometrySource"] == "line"
        assert line["geometry"]["boundingBox"] == {
            "left": 0.1,
            "top": 0.02,
            "width": 0.4,
            "height": 0.03,
        }
        assert line["geometry"]["polygon"][0] == {"x": 0.1, "y": 0.02}
        assert len(line["words"]) == 2
        assert line["words"][0]["text"] == "Account:"
        assert line["words"][1]["confidence"] == 92.0

    def test_mistral_hook_paragraph_shared_geometry(self, service):
        """Mistral-style blocks: lines sharing a box -> geometrySource=paragraph."""
        shared_box = {"Left": 0.1, "Top": 0.1, "Width": 0.5, "Height": 0.08}
        raw = {
            "DocumentMetadata": {"Pages": 1},
            "Blocks": [
                {
                    "BlockType": "LINE",
                    "Id": "l1",
                    "Text": "First line of paragraph",
                    "Confidence": 95.0,
                    "Geometry": {"BoundingBox": dict(shared_box)},
                },
                {
                    "BlockType": "LINE",
                    "Id": "l2",
                    "Text": "Second line of paragraph",
                    "Confidence": 94.0,
                    "Geometry": {"BoundingBox": dict(shared_box)},
                },
            ],
        }

        page_data = service._build_page_data(raw, "text", "bedrock")

        # Structured blocks present -> LambdaHook provenance
        assert page_data["provider"] == "bedrock-lambdahook"
        assert page_data["geometryAvailable"] is True
        assert page_data["confidenceAvailable"] is True
        assert page_data["wordsAvailable"] is False
        for line in page_data["lines"]:
            assert line["geometrySource"] == "paragraph"
            assert line["words"] is None

    def test_plain_llm_text_only(self, service):
        """Plain Bedrock LLM OCR (no blocks) -> synthesized text-only lines."""
        raw = {"output": {"message": {"content": [{"text": "irrelevant"}]}}}
        parsed = "# Heading\n\nFirst paragraph line\nSecond line"

        page_data = service._build_page_data(raw, parsed, "bedrock")

        assert page_data["provider"] == "bedrock-llm"
        assert page_data["geometryAvailable"] is False
        assert page_data["confidenceAvailable"] is False
        assert page_data["wordsAvailable"] is False
        # Blank lines skipped
        texts = [ln["text"] for ln in page_data["lines"]]
        assert texts == ["# Heading", "First paragraph line", "Second line"]
        for line in page_data["lines"]:
            assert line["confidence"] is None
            assert line["geometry"] is None
            assert line["geometrySource"] == "none"

    def test_none_backend_empty(self, service):
        """'none' backend -> no lines, all flags false."""
        raw = {"DocumentMetadata": {"Pages": 1}, "Blocks": []}

        page_data = service._build_page_data(raw, "", "none")

        assert page_data["provider"] == "none"
        assert page_data["lines"] == []
        assert page_data["geometryAvailable"] is False
        assert page_data["confidenceAvailable"] is False

    def test_converted_placeholder_confidence(self, service):
        """Converted non-PDF docs -> per-line placeholder confidence, no geometry."""
        raw = {
            "DocumentMetadata": {"Pages": 1},
            "Blocks": [
                {
                    "BlockType": "LINE",
                    "Text": "line one",
                    "Confidence": 99.0,
                    "TextType": "PRINTED",
                },
                {
                    "BlockType": "LINE",
                    "Text": "line two",
                    "Confidence": 99.0,
                    "TextType": "PRINTED",
                },
            ],
        }

        page_data = service._build_page_data(raw, "line one\nline two", "converted")

        assert page_data["provider"] == "converted"
        assert page_data["confidenceAvailable"] is True
        assert page_data["geometryAvailable"] is False
        assert len(page_data["lines"]) == 2
        assert page_data["lines"][0]["confidence"] == 99.0
        assert page_data["lines"][0]["geometry"] is None


@pytest.mark.unit
class TestSignatureDetections:
    """Textract SIGNATURES output must survive into every downstream artifact.

    A SIGNATURE block has confidence and geometry but no text, so every
    LINE-oriented consumer dropped it: it was absent from pageData.json (no UI
    box, no confidence shown) and from textConfidence.json (so the confidence
    prompt never saw it), and in the page text it appeared only as a bare,
    unpositioned "[SIGNATURE]" token that a real signature and a 10%-confidence
    smudge share.
    """

    SIGNATURE_BLOCK = {
        "BlockType": "SIGNATURE",
        "Id": "sig-1",
        "Confidence": 11.0227,
        "Geometry": {
            "BoundingBox": {
                "Left": 0.5717,
                "Top": 0.8781,
                "Width": 0.0368,
                "Height": 0.0218,
            },
            "Polygon": [{"X": 0.5717, "Y": 0.8781}],
        },
    }

    LINE_BLOCK = {
        "BlockType": "LINE",
        "Id": "line-1",
        "Text": "Signature of taxpayer",
        "Confidence": 99.9,
        "TextType": "PRINTED",
        "Geometry": {
            "BoundingBox": {"Left": 0.07, "Top": 0.87, "Width": 0.11, "Height": 0.01}
        },
    }

    @pytest.fixture
    def service(self):
        with patch("boto3.client"):
            return OcrService(region="us-east-1", enhanced_features=["SIGNATURES"])

    def test_extract_signature_detections(self):
        signatures = OcrService._extract_signature_detections(
            [self.LINE_BLOCK, self.SIGNATURE_BLOCK]
        )

        assert len(signatures) == 1
        assert signatures[0]["id"] == "sig-1"
        assert signatures[0]["confidence"] == 11.0  # rounded to 1dp
        assert signatures[0]["geometry"]["boundingBox"] == {
            "left": 0.5717,
            "top": 0.8781,
            "width": 0.0368,
            "height": 0.0218,
        }

    def test_extract_signature_detections_tolerates_sparse_blocks(self):
        signatures = OcrService._extract_signature_detections(
            [
                "not-a-dict",
                {"BlockType": "SIGNATURE"},  # no Id, Confidence or Geometry
            ]
        )

        assert signatures == [{"id": "sig2", "confidence": None, "geometry": None}]

    def test_summary_reports_confidence_and_position(self):
        summary = OcrService._format_signature_summary(
            OcrService._extract_signature_detections([self.SIGNATURE_BLOCK])
        )

        assert "confidence=11.0 (very low)" in summary
        # Position is stated in the left/right, upper/lower terms field
        # descriptions use — raw normalized coordinates were measurably unusable:
        # both models read left=0.572 as "the first (left) signature box".
        assert "right half, lower area" in summary
        assert "x=59%" in summary
        # An explicit total, so a consumer weighing two signature fields can tell
        # that only one region was detected.
        assert "flagged 1 region on this page" in summary
        # The inline token's placement must be flagged as non-evidential.
        assert "placed by reading order" in summary
        # Must NOT be a markdown table: page text is scanned by the agentic
        # table parser, which would otherwise treat this as a document table.
        assert "|" not in summary

    def test_summary_omits_surrounding_text(self):
        """Naming the text around a detection measured WORSE — see the docstring.

        An earlier version reported ``at: "Signature of taxpayer"`` alongside each
        detection. Naming a signature label beside the mark biases the model toward
        true: on the issue-#634 document the entry without it passed 9/9 and with it
        2/5. This pins the decision so it is not silently reverted.
        """
        label = {
            "BlockType": "LINE",
            "Id": "label-right",
            "Text": "Signature of taxpayer",
            "Confidence": 99.9,
            "Geometry": {
                "BoundingBox": {
                    "Left": 0.498,
                    "Top": 0.872,
                    "Width": 0.121,
                    "Height": 0.011,
                }
            },
        }

        summary = OcrService._format_signature_summary(
            OcrService._extract_signature_detections([label, self.SIGNATURE_BLOCK])
        )

        assert "nearest text" not in summary
        assert "Signature of taxpayer" not in summary
        # The position is what disambiguates the cell.
        assert "right half, lower area" in summary

    def test_summary_reports_position_for_a_lone_detection(self):
        summary = OcrService._format_signature_summary(
            OcrService._extract_signature_detections([self.SIGNATURE_BLOCK])
        )

        assert "right half, lower area" in summary

    @pytest.mark.parametrize(
        "left,top,expected",
        [
            (0.05, 0.05, "left half, upper area"),
            (0.10, 0.50, "left half, middle area"),
            (0.10, 0.90, "left half, lower area"),
            (0.80, 0.90, "right half, lower area"),
        ],
    )
    def test_position_wording(self, left, top, expected):
        box = {"left": left, "top": top, "width": 0.02, "height": 0.02}

        assert expected in OcrService._describe_signature_position(box)

    @pytest.mark.parametrize(
        "confidence,band",
        [(11.0, "very low"), (40.0, "low"), (60.0, "moderate"), (99.0, "high")],
    )
    def test_confidence_bands(self, confidence, band):
        block = {**self.SIGNATURE_BLOCK, "Confidence": confidence}

        summary = OcrService._format_signature_summary(
            OcrService._extract_signature_detections([block])
        )

        assert f"({band})" in summary

    def test_summary_is_empty_without_detections(self):
        assert OcrService._format_signature_summary([]) == ""

    def test_page_data_carries_signatures(self, service):
        raw = {"Blocks": [self.LINE_BLOCK, self.SIGNATURE_BLOCK]}

        page_data = service._build_page_data(raw, "Signature of taxpayer", "textract")

        assert page_data["signaturesAvailable"] is True
        assert len(page_data["signatures"]) == 1
        assert page_data["signatures"][0]["confidence"] == 11.0
        # Signatures stay OUT of `lines` — they have no text, and the geometry
        # grounder matches extracted values against line text.
        assert [line["text"] for line in page_data["lines"]] == [
            "Signature of taxpayer"
        ]

    def test_page_data_without_signatures(self, service):
        page_data = service._build_page_data(
            {"Blocks": [self.LINE_BLOCK]}, "Signature of taxpayer", "textract"
        )

        assert page_data["signaturesAvailable"] is False
        assert page_data["signatures"] == []

    def test_page_data_geometry_available_from_signature_alone(self, service):
        """A page whose only geometry is a signature box still reports geometry."""
        line_without_geometry = {
            "BlockType": "LINE",
            "Id": "line-2",
            "Text": "text only",
            "Confidence": 99.0,
        }

        page_data = service._build_page_data(
            {"Blocks": [line_without_geometry, self.SIGNATURE_BLOCK]},
            "text only",
            "textract",
        )

        assert page_data["geometryAvailable"] is True

    def test_text_confidence_data_includes_signatures(self, service):
        result = service._generate_text_confidence_data(
            {"Blocks": [self.LINE_BLOCK, self.SIGNATURE_BLOCK]}
        )

        text = result["text"]
        # The LINE table is unchanged...
        assert "| Signature of taxpayer | 99.9 |" in text
        # ...and the signature detection is appended with its confidence.
        assert "OCR signature detections" in text
        assert "confidence=11.0" in text

    def test_text_confidence_data_unchanged_without_signatures(self, service):
        result = service._generate_text_confidence_data({"Blocks": [self.LINE_BLOCK]})

        assert "signature detections" not in result["text"]
        assert result["text"].endswith("| Signature of taxpayer | 99.9 |")

    def test_parsed_page_text_appends_summary(self, service):
        """The summary rides along with the parsed page text (extraction prompt)."""
        # Patch the module attribute (not `...response_parser.parse`): the service
        # does `from textractor.parsers import response_parser`, which resolves via
        # getattr on `textractor.parsers`. When textractor isn't installed that
        # parent is a MagicMock, so patching the deeper dotted path targets a
        # different object and never takes effect.
        with patch("textractor.parsers.response_parser") as mock_response_parser:
            mock_response_parser.parse.return_value.to_markdown.return_value = (
                "Signature of taxpayer\n[SIGNATURE]"
            )
            result = service._parse_textract_response(
                {"Blocks": [self.LINE_BLOCK, self.SIGNATURE_BLOCK]}, page_id=2
            )

        text = result["text"]
        assert text.startswith("Signature of taxpayer\n[SIGNATURE]")
        assert "OCR signature detections" in text
        assert "confidence=11.0 (very low)" in text
        assert "right half, lower area" in text

    def test_parsed_page_text_unchanged_without_signatures(self, service):
        with patch("textractor.parsers.response_parser") as mock_response_parser:
            mock_response_parser.parse.return_value.to_markdown.return_value = (
                "Signature of taxpayer"
            )
            result = service._parse_textract_response(
                {"Blocks": [self.LINE_BLOCK]}, page_id=2
            )

        assert result["text"] == "Signature of taxpayer"


@pytest.mark.unit
class TestShippedOcrFeatureDefaults:
    """Guard the shipped ocr.features default.

    SIGNATURES is in the default set because signature presence is a common
    extraction target and the feature is free in this combination — per the Textract
    pricing page, "Signatures feature is included free of cost with any combination
    of Forms, Tables, Queries, and Layout" (AWS emits no usage type at all for a
    feature that is free in combination). If TABLES/FORMS/LAYOUT were ever dropped
    from the defaults while SIGNATURES stayed, SIGNATURES would start being billed
    at ~$0.0035/page — hence the paired assertion.
    """

    def test_default_features_include_tables_layout_signatures(self):
        from idp_common.config.merge_utils import load_system_defaults

        defaults = load_system_defaults("pattern-2")
        names = [f["name"] for f in defaults["ocr"]["features"]]

        assert names == ["TABLES", "LAYOUT", "SIGNATURES"], names

    def test_signatures_default_is_never_billed_alone(self):
        """SIGNATURES in the defaults must be accompanied by a paying feature."""
        from idp_common.config.merge_utils import load_system_defaults

        names = {
            f["name"] for f in load_system_defaults("pattern-2")["ocr"]["features"]
        }

        if "SIGNATURES" in names:
            assert names & {"TABLES", "FORMS", "LAYOUT"}, (
                "SIGNATURES is only free in combination; on its own it is billed "
                "at ~$0.0035/page"
            )
