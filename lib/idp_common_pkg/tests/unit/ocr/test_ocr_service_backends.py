# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the per-page and per-file processing paths of `OcrService` — the
methods that turn one page into the five S3 artifacts (`image.*`, `rawText.json`,
`textConfidence.json`, `result.json`, `pageData.json`) that every later stage of
the pipeline reads, and the file-type and document-level plumbing that decides
which of those paths runs.

`idp_common/ocr/service.py` is the widest single place in this library where a
wrong answer is silent. Nothing downstream re-derives OCR: classification reads
`result.json`, assessment reads `textConfidence.json`, the UI overlays
`pageData.json` geometry, and the reporting lake bills from the metering key. A
page that produced *something* looks identical to a page that produced the right
thing, so the tests below are written around the shapes a defect actually takes
here rather than around the return value being non-empty.

Four failure modes drove most of the cases.

**An artifact URI that names a key nobody wrote.** Each page processor builds its
result dict from local `*_key` variables and returns `s3://bucket/key` strings.
If a branch writes one key and reports another, the OCR step succeeds and
extraction fails later with a 404 on an object that was never created. Every page
test therefore records the exact set of keys written and cross-checks all five
returned URIs against it, rather than asserting the dict has five entries.

**Confidence invented where there is none.** The `none` backend and plain-LLM
Bedrock OCR have no confidence signal at all; a missing score defaulting to `0.0`
would tell the assessment model every line was maximally unreliable, and
defaulting to `100.0` would tell it the opposite. Both directions are asserted
explicitly, and the LambdaHook case — where `textractBlocks` *does* carry real
scores — is asserted to produce numbers rather than the placeholder, because the
two are one `if` apart.

**Image handling that destroys the page before OCR sees it.** Bedrock cannot read
TIFF, BDA's modality routing keys off the file extension, and 16-bit grayscale
TIFF converted naively clips every value above 255 to white — a blank page that
OCRs to nothing with no error anywhere. The conversion tests therefore build real
images with PIL and assert on the decoded result (the extension actually stored,
the content type actually stored, and that a wide-dynamic-range source still has
dark *and* light pixels afterwards), not on a mock having been called.

**Numbers derived from the wrong source.** A metering key that names
`detect_document_text` while `analyze_document` was called under-bills by roughly
an order of magnitude, and a page count hardcoded to 1 under-bills a multi-page
response. Resize targets are asserted against values deliberately different from
`DEFAULT_TARGET_WIDTH`/`DEFAULT_TARGET_HEIGHT`, and DPI against a value
deliberately different from `DEFAULT_DPI`, so a test cannot pass by the code
ignoring configuration and falling back to the constant it happens to match.

Two things are patched at the point of use rather than at the source module. The
service binds `s3`, `image`, `bedrock` and `pdfium` as module-level names via
`from idp_common import ...`, and `idp_common/__init__.py` caches lazily-imported
submodules in its own dict instead of reading `sys.modules`, so the object the
service holds is not always the object `patch("idp_common.s3.write_content")`
reaches. Patching `idp_common.ocr.service.s3` replaces the name in the namespace
that is actually consulted and is immune to that.

No test here makes an AWS call or renders a real PDF. `pypdfium2` page objects are
stubbed because the rendering itself is PDFium's job, not this module's; what this
module decides is the *scale* it renders at, and that is asserted directly against
`page.render`.
"""

import io
import json
import time as time_module
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image as PILImage

import idp_common.bda as _bda_package
from idp_common import image as _image_module
from idp_common.models import Document, Status
from idp_common.ocr import service as service_module
from idp_common.ocr.document_converter import UnsupportedLegacyFormatError
from idp_common.ocr.service import (
    DEFAULT_DPI,
    DEFAULT_TARGET_HEIGHT,
    DEFAULT_TARGET_WIDTH,
    OcrService,
)

_real_resize_image = _image_module.resize_image


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class S3Recorder:
    """Stands in for the `s3` module the service writes through.

    Keeps the full content of every write so tests can assert on what landed in
    a specific key instead of on a call count, and so a returned artifact URI can
    be checked against the keys that were genuinely written.
    """

    def __init__(self) -> None:
        self.writes: List[Tuple[str, str, Any, Optional[str]]] = []

    def write_content(
        self,
        content: Any,
        bucket: str,
        key: str,
        content_type: Optional[str] = None,
    ) -> None:
        self.writes.append((bucket, key, content, content_type))

    # -- query helpers ------------------------------------------------------

    @property
    def keys(self) -> List[str]:
        return [key for _, key, _, _ in self.writes]

    def content_for(self, key: str) -> Any:
        matches = [c for _, k, c, _ in self.writes if k == key]
        assert matches, f"nothing written to {key}; wrote {self.keys}"
        return matches[-1]

    def content_type_for(self, key: str) -> Optional[str]:
        matches = [ct for _, k, _, ct in self.writes if k == key]
        assert matches, f"nothing written to {key}; wrote {self.keys}"
        return matches[-1]

    def content_ending(self, suffix: str) -> Any:
        matches = [c for _, k, c, _ in self.writes if k.endswith(suffix)]
        assert matches, f"nothing written ending {suffix}; wrote {self.keys}"
        return matches[-1]


def make_service(**kwargs: Any) -> OcrService:
    """Build an `OcrService` with every boto3 client replaced by a mock."""
    with patch(
        "boto3.client",
        side_effect=lambda name, **kw: MagicMock(name=f"{name}_client"),
    ):
        return OcrService(**kwargs)


def encode_image(
    fmt: str,
    size: Tuple[int, int] = (120, 90),
    mode: str = "RGB",
    pixels: Any = None,
) -> bytes:
    """Produce genuine encoded image bytes so PIL decoding is exercised."""
    img = PILImage.new(mode, size)
    if pixels is not None:
        img.putdata(pixels)
    elif mode == "RGB":
        # A non-uniform image so a JPEG re-encode is detectable.
        img.putdata(
            [
                (x * 2 % 256, y * 2 % 256, 128)
                for y in range(size[1])
                for x in range(size[0])
            ]
        )
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


PNG_BYTES = encode_image("PNG")
JPEG_BYTES = encode_image("JPEG")


def greyscale_extremes(payload: bytes) -> Tuple[int, int]:
    """The darkest and lightest greyscale value present in encoded image bytes.

    Used to show that a wide-dynamic-range source survived the 8-bit conversion
    with contrast intact, rather than being clipped to a single flat value.
    """
    extrema = PILImage.open(io.BytesIO(payload)).convert("L").getextrema()
    assert isinstance(extrema, tuple) and len(extrema) == 2, extrema
    darkest, lightest = extrema
    return int(darkest), int(lightest)


def textract_response(pages: int = 1, lines: Optional[List[str]] = None) -> Dict:
    """A minimal Textract-shaped response with LINE blocks."""
    lines = ["Sample line one", "Sample line two"] if lines is None else lines
    blocks: List[Dict[str, Any]] = [{"BlockType": "PAGE", "Id": "p1"}]
    for index, text in enumerate(lines, start=1):
        blocks.append(
            {
                "BlockType": "LINE",
                "Id": f"line-{index}",
                "Text": text,
                "Confidence": 90.0 + index,
                "TextType": "PRINTED",
            }
        )
    return {"DocumentMetadata": {"Pages": pages}, "Blocks": blocks}


BEDROCK_CONFIG = {
    "model_id": "us.anthropic.claude-sonnet-4-20250514-v1:0",
    "system_prompt": "You are an OCR assistant.",
    "task_prompt": "Transcribe this page.",
}


def bedrock_config_dict(**overrides: Any) -> Dict[str, Any]:
    """An `IDPConfig`-shaped dict selecting the Bedrock OCR backend."""
    ocr: Dict[str, Any] = {"backend": "bedrock", **BEDROCK_CONFIG}
    ocr.update(overrides)
    return {"ocr": ocr}


def bedrock_reply(text: str = "page text", textract_blocks: Any = None) -> Dict:
    payload: Dict[str, Any] = {"output": {"message": {"content": [{"text": text}]}}}
    if textract_blocks is not None:
        payload["textractBlocks"] = textract_blocks
    return {"response": payload, "metering": {"OCR/bedrock/model": {"inputTokens": 7}}}


def mock_pdf_page(
    width_pt: float = 612.0,
    height_pt: float = 792.0,
    rendered: bytes = JPEG_BYTES,
) -> MagicMock:
    """A stubbed pypdfium2 page that renders to real JPEG bytes."""
    page = MagicMock()
    page.get_width.return_value = width_pt
    page.get_height.return_value = height_pt
    page.formenv = None
    pil = MagicMock()
    pil.size = PILImage.open(io.BytesIO(rendered)).size
    pil.save.side_effect = lambda buf, **kw: buf.write(rendered)
    page.render.return_value.to_pil.return_value = pil
    return page


def assert_uris_were_written(result: Dict[str, str], recorder: S3Recorder) -> None:
    """Every URI the page processor reports must name a key it actually wrote.

    Reporting an unwritten key is invisible here and surfaces as a 404 in
    classification or extraction, on a document that OCR marked successful.
    """
    written = {f"s3://{bucket}/{key}" for bucket, key, _, _ in recorder.writes}
    for field in (
        "image_uri",
        "raw_text_uri",
        "parsed_text_uri",
        "text_confidence_uri",
        "ocr_page_data_uri",
    ):
        assert field in result, f"{field} missing from page result"
        assert result[field] in written, (
            f"{field}={result[field]} was never written; wrote {sorted(written)}"
        )


@contextmanager
def patched_bda_ocr(
    blocks: Optional[Dict[str, Any]] = None, markdown: str = ""
) -> Iterator[MagicMock]:
    """Stub the `bda_ocr` helpers `_run_bda_ocr` imports at call time.

    Patched as an attribute of the `idp_common.bda` package rather than through
    `sys.modules`: `from idp_common.bda import bda_ocr` resolves the name by
    `getattr` on the already-imported package first, so a `sys.modules` entry is
    never consulted and the real converter would run.
    """
    mock = MagicMock()
    mock.bda_standard_output_to_textract_blocks.return_value = (
        {"Blocks": []} if blocks is None else blocks
    )
    mock.extract_markdown.return_value = markdown
    with patch.object(_bda_package, "bda_ocr", mock):
        yield mock


@pytest.fixture
def s3_writes():
    """Replace the `s3` module the service under test uses with a recorder."""
    recorder = S3Recorder()
    with patch.object(service_module, "s3", recorder):
        yield recorder


@pytest.fixture
def real_resize():
    """Patch the `image` module but keep `resize_image` genuine.

    `_process_image_file_direct` re-opens the resize output with PIL, so a bare
    `MagicMock` return value would raise there rather than exercising the branch.
    """
    mock_image = MagicMock()
    mock_image.resize_image.side_effect = _real_resize_image
    mock_image.prepare_bedrock_image_attachment.side_effect = lambda b: {
        "image": {"format": "jpeg", "source": {"bytes": b}}
    }
    with patch.object(service_module, "image", mock_image):
        yield mock_image


# ---------------------------------------------------------------------------
# _process_image_file_direct
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProcessImageFileDirect:
    """The single-page path taken when the uploaded file is itself an image."""

    def test_absent_content_raises_instead_of_storing_an_empty_page(self, s3_writes):
        """No bytes must fail loudly, not produce a page with no image.

        A silently-empty page reaches classification and extraction as a valid
        page with no content, which surfaces much later as inexplicably empty
        results rather than as an OCR failure.
        """
        service = make_service(backend="none")
        with pytest.raises(ValueError, match="original_file_content"):
            service._process_image_file_direct("out", "doc", None)
        assert s3_writes.writes == [], (
            "nothing should be persisted when the content is missing"
        )

    def test_png_keeps_its_extension_and_content_type(self, s3_writes, real_resize):
        """A Bedrock-readable format must be stored as-is, not re-encoded.

        The stored extension is what BDA's modality routing and the UI viewer key
        off, so silently rewriting a PNG to `image.jpg` is not cosmetic.
        """
        service = make_service(backend="none")
        result, _ = service._process_image_file_direct("out", "doc", PNG_BYTES)

        assert result["image_uri"] == "s3://out/doc/pages/1/image.png"
        assert s3_writes.content_type_for("doc/pages/1/image.png") == "image/png"
        assert s3_writes.content_for("doc/pages/1/image.png") == PNG_BYTES
        real_resize.resize_image.assert_not_called()

    def test_tiff_is_transcoded_to_jpeg_for_bedrock_compatibility(
        self, s3_writes, real_resize
    ):
        """TIFF is not in Bedrock's accepted set, so it must not reach it.

        Asserted on the decoded bytes rather than the key alone: a `.jpg` key
        holding TIFF bytes is the same API rejection with a more confusing error.
        """
        service = make_service(backend="none")
        result, _ = service._process_image_file_direct(
            "out", "doc", encode_image("TIFF")
        )

        assert result["image_uri"].endswith("/image.jpg")
        assert s3_writes.content_type_for("doc/pages/1/image.jpg") == "image/jpeg"
        stored = s3_writes.content_for("doc/pages/1/image.jpg")
        assert PILImage.open(io.BytesIO(stored)).format == "JPEG"

    def test_sixteen_bit_grayscale_is_rescaled_rather_than_clipped(
        self, s3_writes, real_resize
    ):
        """A 16-bit source must keep its contrast through the 8-bit conversion.

        `PILImage.convert("L")` on mode `I;16` clips every value above 255 to
        white, so a scan whose ink sits at 12000 and paper at 60000 becomes a
        uniformly white page that OCRs to nothing, with no error raised. The
        assertion is that dark *and* light pixels survive.
        """
        width, height = 16, 16
        half = width * height // 2
        img = PILImage.new("I;16", (width, height))
        img.putdata([4000] * half + [60000] * half)
        buf = io.BytesIO()
        img.save(buf, format="TIFF")

        service = make_service(backend="none")
        service._process_image_file_direct("out", "doc", buf.getvalue())

        darkest, lightest = greyscale_extremes(
            s3_writes.content_for("doc/pages/1/image.jpg")
        )
        assert darkest < 64, f"dark end of the 16-bit range was lost (min={darkest})"
        assert lightest > 192, (
            f"light end of the 16-bit range was lost (max={lightest})"
        )

    def test_float_mode_is_normalised_to_the_eight_bit_range(
        self, s3_writes, real_resize
    ):
        """Mode `F` values are not 0-255 and need the same rescale as `I;16`."""
        width, height = 8, 8
        half = width * height // 2
        img = PILImage.new("F", (width, height))
        img.putdata([0.0] * half + [1.0] * half)
        buf = io.BytesIO()
        img.save(buf, format="TIFF")

        service = make_service(backend="none")
        service._process_image_file_direct("out", "doc", buf.getvalue())

        darkest, lightest = greyscale_extremes(
            s3_writes.content_for("doc/pages/1/image.jpg")
        )
        assert darkest < 64, f"float range was clipped at the dark end: {darkest}"
        assert lightest > 192, f"float range was clipped at the light end: {lightest}"

    def test_cmyk_is_converted_before_jpeg_encoding(self, s3_writes, real_resize):
        """CMYK is neither RGB nor L, so it takes the generic convert branch."""
        service = make_service(backend="none")
        service._process_image_file_direct(
            "out", "doc", encode_image("TIFF", mode="CMYK")
        )

        decoded = PILImage.open(
            io.BytesIO(s3_writes.content_for("doc/pages/1/image.jpg"))
        )
        assert decoded.mode == "RGB", f"stored JPEG mode was {decoded.mode}"

    def test_an_image_inside_the_ceiling_is_stored_untouched(
        self, s3_writes, real_resize
    ):
        """The default ceiling is an OOM guard, not a downscale-everything rule.

        Re-encoding a page that already fits costs quality for nothing, and issue
        #729 is about resolution loss being invisible to the caller.
        """
        service = make_service()  # default ceiling 2600x3600
        assert service.resize_config == {
            "target_width": DEFAULT_TARGET_WIDTH,
            "target_height": DEFAULT_TARGET_HEIGHT,
        }
        service.backend = "none"
        service._process_image_file_direct("out", "doc", PNG_BYTES)

        real_resize.resize_image.assert_not_called()
        assert s3_writes.content_for("doc/pages/1/image.png") == PNG_BYTES

    def test_an_oversized_image_is_resized_to_the_configured_ceiling(
        self, s3_writes, real_resize
    ):
        """The targets handed to `resize_image` must come from configuration.

        Both values are deliberately unequal to `DEFAULT_TARGET_WIDTH` and
        `DEFAULT_TARGET_HEIGHT`, so this cannot pass if the configured ceiling is
        dropped and the constant used instead.
        """
        service = make_service(
            config={
                "ocr": {
                    "backend": "none",
                    "image": {"target_width": 64, "target_height": 48},
                }
            }
        )
        assert 64 != DEFAULT_TARGET_WIDTH and 48 != DEFAULT_TARGET_HEIGHT

        service._process_image_file_direct("out", "doc", PNG_BYTES)

        real_resize.resize_image.assert_called_once()
        _, target_width, target_height = real_resize.resize_image.call_args.args
        assert (target_width, target_height) == (64, 48)

    def test_a_partial_ceiling_still_resizes_to_derive_the_other_dimension(
        self, s3_writes, real_resize
    ):
        """One configured dimension must not be ignored just because it is alone.

        With only a width, the height has to be computed from the aspect ratio,
        so the "already fits" shortcut does not apply and the resize must run.
        The source here is *narrower* than the configured width, which is exactly
        the case a fits-check would wrongly skip.
        """
        service = make_service(
            config={"ocr": {"backend": "none", "image": {"target_width": 400}}}
        )
        assert service.resize_config == {"target_width": 400, "target_height": None}

        service._process_image_file_direct("out", "doc", PNG_BYTES)

        real_resize.resize_image.assert_called_once()
        assert real_resize.resize_image.call_args.args[1:] == (400, None)

    def test_none_backend_reports_no_confidence_rather_than_a_score(
        self, s3_writes, real_resize
    ):
        """With no OCR there is no confidence, and inventing one misleads assessment.

        `0.0` would tell the assessment model every line was unreliable and
        `100.0` the opposite; the artifact has to say the data is absent.
        """
        service = make_service(backend="none")
        result, metering = service._process_image_file_direct("out", "doc", PNG_BYTES)

        assert metering == {}, "image-only processing must not meter an OCR call"
        assert s3_writes.content_for("doc/pages/1/rawText.json")["Blocks"] == []
        confidence = s3_writes.content_for("doc/pages/1/textConfidence.json")["text"]
        assert "No OCR performed" in confidence
        assert "0.0" not in confidence and "100.0" not in confidence
        assert s3_writes.content_for("doc/pages/1/result.json") == {"text": ""}
        assert s3_writes.content_for("doc/pages/1/pageData.json")["provider"] == "none"
        assert_uris_were_written(result, s3_writes)

    def test_textract_metering_names_the_api_and_features_actually_used(
        self, s3_writes, real_resize
    ):
        """The metering key drives billing, so it must track the call, not a default.

        `analyze_document` with TABLES costs roughly an order of magnitude more
        than `detect_document_text`; a key that says the cheaper one under-bills
        every page silently. The page count comes from the response, not a literal.
        """
        service = make_service(
            config={"ocr": {"backend": "textract", "features": [{"name": "TABLES"}]}}
        )
        service.textract_client.analyze_document.return_value = textract_response(
            pages=3
        )

        _, metering = service._process_image_file_direct("out", "doc", PNG_BYTES)

        service.textract_client.detect_document_text.assert_not_called()
        service.textract_client.analyze_document.assert_called_once()
        assert metering == {"OCR/textract/analyze_document-Tables": {"pages": 3}}, (
            metering
        )

    def test_textract_without_features_uses_the_cheap_detect_api(
        self, s3_writes, real_resize
    ):
        """The other direction: no features must not silently escalate to analyze."""
        service = make_service(backend="textract")
        service.textract_client.detect_document_text.return_value = textract_response()

        result, metering = service._process_image_file_direct("out", "doc", PNG_BYTES)

        service.textract_client.analyze_document.assert_not_called()
        assert metering == {"OCR/textract/detect_document_text": {"pages": 1}}
        assert (
            "Sample line one"
            in s3_writes.content_for("doc/pages/1/result.json")["text"]
        )
        assert_uris_were_written(result, s3_writes)

    def test_bedrock_backend_persists_the_llm_text_and_a_confidence_placeholder(
        self, s3_writes, real_resize
    ):
        """Plain LLM OCR has no per-line confidence and must say so."""
        service = make_service(config=bedrock_config_dict())
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = bedrock_reply("hello page")
        mock_bedrock.extract_text_from_response.return_value = "hello page"

        with patch.object(service_module, "bedrock", mock_bedrock):
            result, metering = service._process_image_file_direct(
                "out", "doc", PNG_BYTES
            )

        assert metering == {"OCR/bedrock/model": {"inputTokens": 7}}
        assert s3_writes.content_for("doc/pages/1/result.json") == {
            "text": "hello page"
        }
        confidence = s3_writes.content_for("doc/pages/1/textConfidence.json")["text"]
        assert "No confidence data available" in confidence
        page_data = s3_writes.content_for("doc/pages/1/pageData.json")
        assert page_data["provider"] == "bedrock-llm"
        assert page_data["confidenceAvailable"] is False
        assert_uris_were_written(result, s3_writes)

    def test_bedrock_max_tokens_is_left_unset_so_the_client_resolves_the_model_max(
        self, s3_writes, real_resize
    ):
        """`max_tokens=None` is load-bearing, not an omission.

        Bedrock's own default-when-omitted truncates long pages mid-transcription,
        which reads downstream as a page that simply ended early.
        """
        service = make_service(config=bedrock_config_dict())
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = bedrock_reply()
        mock_bedrock.extract_text_from_response.return_value = "t"

        with patch.object(service_module, "bedrock", mock_bedrock):
            service._process_image_file_direct("out", "doc", PNG_BYTES)

        kwargs = mock_bedrock.invoke_model.call_args.kwargs
        assert "max_tokens" in kwargs
        assert kwargs["max_tokens"] is None
        assert kwargs["temperature"] == 0.0
        assert kwargs["context"] == "OCR"

    def test_bedrock_hook_blocks_become_the_raw_artifact_with_real_confidence(
        self, s3_writes, real_resize
    ):
        """A LambdaHook returning Textract blocks must not get the placeholder.

        This is the difference between the assessment prompt seeing per-line
        scores and seeing "N/A" for a page that had them all along.
        """
        blocks = {
            "Blocks": [
                {
                    "BlockType": "LINE",
                    "Id": "l1",
                    "Text": "Total 42.00",
                    "Confidence": 87.65,
                }
            ]
        }
        service = make_service(config=bedrock_config_dict())
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = bedrock_reply(
            "Total 42.00", textract_blocks=blocks
        )
        mock_bedrock.extract_text_from_response.return_value = "Total 42.00"

        with patch.object(service_module, "bedrock", mock_bedrock):
            service._process_image_file_direct("out", "doc", PNG_BYTES)

        assert s3_writes.content_for("doc/pages/1/rawText.json") == blocks
        confidence = s3_writes.content_for("doc/pages/1/textConfidence.json")["text"]
        assert "| Total 42.00 | 87.7 |" in confidence
        assert "N/A" not in confidence
        page_data = s3_writes.content_for("doc/pages/1/pageData.json")
        assert page_data["provider"] == "bedrock-lambdahook"
        assert page_data["confidenceAvailable"] is True

    def test_bedrock_preprocessing_binarises_the_ocr_input_only(
        self, s3_writes, real_resize
    ):
        """The stored page image must stay legible even when OCR gets a binarised one.

        The UI shows the stored image to a human reviewer; replacing it with the
        1-bit OCR input makes every review screen unreadable.
        """
        service = make_service(
            config=bedrock_config_dict(image={"preprocessing": True})
        )
        assert service.preprocessing_config == {"enabled": True}

        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = bedrock_reply()
        mock_bedrock.extract_text_from_response.return_value = "t"

        with (
            patch.object(service_module, "bedrock", mock_bedrock),
            patch.object(
                _image_module,
                "apply_adaptive_binarization",
                return_value=b"BINARISED",
            ) as mock_binarize,
        ):
            service._process_image_file_direct("out", "doc", PNG_BYTES)

        mock_binarize.assert_called_once()
        assert s3_writes.content_for("doc/pages/1/image.png") == PNG_BYTES
        attachment = real_resize.prepare_bedrock_image_attachment.call_args.args[0]
        assert attachment == b"BINARISED"

    def test_bda_transcodes_an_incompatible_format_and_points_bda_at_it(
        self, s3_writes, real_resize
    ):
        """BDA reads by S3 URI and routes on the extension, so a GIF must be re-keyed.

        Handing BDA a `.gif` falls back to content-based classification, which
        under load misclassifies pages as IMAGE and yields empty OCR — a page
        with no text and no error.
        """
        service = make_service(
            config={"ocr": {"backend": "bda", "bda_project_arn": "arn:proj"}}
        )
        with patch.object(
            service,
            "_run_bda_ocr",
            return_value=({"Blocks": []}, {"text": "t"}, "md", {"k": 1}),
        ) as mock_run:
            service._process_image_file_direct(
                "out", "doc", encode_image("GIF", mode="P")
            )

        assert "doc/pages/1/image.gif" in s3_writes.keys
        assert "doc/pages/1/image_bda.jpg" in s3_writes.keys
        assert mock_run.call_args.args[0] == "s3://out/doc/pages/1/image_bda.jpg"
        transcoded = s3_writes.content_for("doc/pages/1/image_bda.jpg")
        assert PILImage.open(io.BytesIO(transcoded)).format == "JPEG"

    def test_bda_reuses_the_stored_image_when_it_is_already_compatible(
        self, s3_writes, real_resize
    ):
        """A JPEG needs no second copy; writing one doubles storage for nothing."""
        service = make_service(
            config={"ocr": {"backend": "bda", "bda_project_arn": "arn:proj"}}
        )
        with patch.object(
            service,
            "_run_bda_ocr",
            return_value=({"Blocks": []}, {"text": "t"}, "md", {"k": 1}),
        ) as mock_run:
            service._process_image_file_direct("out", "doc", JPEG_BYTES)

        assert "doc/pages/1/image_bda.jpg" not in s3_writes.keys
        assert mock_run.call_args.args[0] == "s3://out/doc/pages/1/image.jpg"

    def test_bda_forwards_the_stored_image_dimensions(self, s3_writes, real_resize):
        """Rectification corners are normalised against BDA's crop, not the page.

        Without the original size the converter cannot rescale them back, and
        every bounding box the UI draws lands in the wrong place.
        """
        service = make_service(
            config={"ocr": {"backend": "bda", "bda_project_arn": "arn:proj"}}
        )
        expected_size = PILImage.open(io.BytesIO(JPEG_BYTES)).size
        with patch.object(
            service,
            "_run_bda_ocr",
            return_value=({"Blocks": []}, {"text": "t"}, "md", {"k": 1}),
        ) as mock_run:
            service._process_image_file_direct("out", "doc", JPEG_BYTES)

        assert mock_run.call_args.kwargs["original_image_size"] == expected_size


# ---------------------------------------------------------------------------
# _process_page_with_image
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProcessPageWithImage:
    """The parallel-worker path: one pre-rendered page image in, five artifacts out."""

    def test_page_ids_are_one_based(self, s3_writes):
        """Zero-based index in, one-based page id out.

        An off-by-one here silently mis-keys every artifact, so page N's text is
        attributed to page N+1 throughout extraction.
        """
        service = make_service(backend="none")
        result, _ = service._process_page_with_image(0, JPEG_BYTES, "out", "doc")
        assert result["image_uri"] == "s3://out/doc/pages/1/image.jpg"

        result_second, _ = service._process_page_with_image(4, JPEG_BYTES, "out", "doc")
        assert result_second["image_uri"] == "s3://out/doc/pages/5/image.jpg"

    def test_none_backend_writes_every_artifact_and_meters_nothing(self, s3_writes):
        service = make_service(backend="none")
        result, metering = service._process_page_with_image(0, JPEG_BYTES, "out", "doc")

        assert metering == {}
        assert s3_writes.content_for("doc/pages/1/rawText.json")["Blocks"] == []
        assert s3_writes.content_for("doc/pages/1/result.json") == {"text": ""}
        assert_uris_were_written(result, s3_writes)

    def test_textract_page_count_comes_from_the_response(self, s3_writes):
        """A literal 1 would under-bill any response reporting more than one page."""
        service = make_service(backend="textract")
        service.textract_client.detect_document_text.return_value = textract_response(
            pages=4
        )

        _, metering = service._process_page_with_image(0, JPEG_BYTES, "out", "doc")

        assert metering == {"OCR/textract/detect_document_text": {"pages": 4}}

    def test_textract_features_select_analyze_document_and_its_metering_key(
        self, s3_writes
    ):
        service = make_service(
            config={
                "ocr": {
                    "backend": "textract",
                    "features": [{"name": "FORMS"}, {"name": "TABLES"}],
                }
            }
        )
        service.textract_client.analyze_document.return_value = textract_response()

        result, metering = service._process_page_with_image(0, JPEG_BYTES, "out", "doc")

        service.textract_client.detect_document_text.assert_not_called()
        assert metering == {"OCR/textract/analyze_document-Tables+Forms": {"pages": 1}}
        assert_uris_were_written(result, s3_writes)

    def test_preprocessing_binarises_the_ocr_bytes_but_not_the_stored_image(
        self, s3_writes
    ):
        """Both directions in one test, because one `=` decides them.

        If the binarised bytes were stored, human review breaks; if the original
        bytes were sent to Textract, the preprocessing setting does nothing while
        appearing to be honoured.
        """
        service = make_service(
            config={"ocr": {"backend": "textract", "image": {"preprocessing": True}}}
        )
        service.textract_client.detect_document_text.return_value = textract_response()

        with patch.object(
            _image_module,
            "apply_adaptive_binarization",
            return_value=b"BINARISED",
        ):
            service._process_page_with_image(0, JPEG_BYTES, "out", "doc")

        assert s3_writes.content_for("doc/pages/1/image.jpg") == JPEG_BYTES
        sent = service.textract_client.detect_document_text.call_args.kwargs[
            "Document"
        ]["Bytes"]
        assert sent == b"BINARISED"

    def test_preprocessing_off_sends_the_original_bytes(self, s3_writes):
        """The complementary case: unconfigured preprocessing must not run."""
        service = make_service(backend="textract")
        assert service.preprocessing_config is None
        service.textract_client.detect_document_text.return_value = textract_response()

        with patch.object(
            _image_module,
            "apply_adaptive_binarization",
            return_value=b"BINARISED",
        ) as mock_binarize:
            service._process_page_with_image(0, JPEG_BYTES, "out", "doc")

        mock_binarize.assert_not_called()
        sent = service.textract_client.detect_document_text.call_args.kwargs[
            "Document"
        ]["Bytes"]
        assert sent == JPEG_BYTES

    def test_bedrock_page_persists_text_and_metering(self, s3_writes):
        service = make_service(config=bedrock_config_dict())
        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = bedrock_reply("worker text")
        mock_bedrock.extract_text_from_response.return_value = "worker text"

        with (
            patch.object(service_module, "bedrock", mock_bedrock),
            patch.object(service_module, "image", MagicMock()),
        ):
            result, metering = service._process_page_with_image(
                0, JPEG_BYTES, "out", "doc"
            )

        assert metering == {"OCR/bedrock/model": {"inputTokens": 7}}
        assert s3_writes.content_for("doc/pages/1/result.json") == {
            "text": "worker text"
        }
        assert_uris_were_written(result, s3_writes)

    def test_bda_page_uses_the_uploaded_image_and_its_real_dimensions(self, s3_writes):
        """The size forwarded must be measured from the bytes, not assumed."""
        service = make_service(
            config={"ocr": {"backend": "bda", "bda_project_arn": "arn:proj"}}
        )
        expected_size = PILImage.open(io.BytesIO(JPEG_BYTES)).size
        with patch.object(
            service,
            "_run_bda_ocr",
            return_value=(
                {"Blocks": []},
                {"text": "conf"},
                "markdown text",
                {"OCR/bda/documents-standard": {"pages": 1}},
            ),
        ) as mock_run:
            result, metering = service._process_page_with_image(
                2, JPEG_BYTES, "out", "doc"
            )

        assert mock_run.call_args.args[0] == "s3://out/doc/pages/3/image.jpg"
        assert mock_run.call_args.kwargs["original_image_size"] == expected_size
        assert metering == {"OCR/bda/documents-standard": {"pages": 1}}
        assert s3_writes.content_for("doc/pages/3/result.json") == {
            "text": "markdown text"
        }
        assert_uris_were_written(result, s3_writes)


# ---------------------------------------------------------------------------
# _process_converted_page
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProcessConvertedPage:
    """The path for text/CSV/Excel/Word pages, which arrive already transcribed."""

    def test_blank_lines_do_not_become_ocr_lines(self, s3_writes):
        """Empty blocks would be scored by assessment as unreadable content."""
        service = make_service(backend="none")
        service._process_converted_page(
            0, JPEG_BYTES, "alpha\n\n   \nbeta\n", "out", "doc"
        )

        blocks = s3_writes.content_for("doc/pages/1/rawText.json")["Blocks"]
        assert [b["Text"] for b in blocks] == ["alpha", "beta"]

    def test_the_parsed_text_keeps_the_original_layout(self, s3_writes):
        """Blank lines are dropped from the blocks but must survive in the text.

        The blocks feed confidence scoring; `result.json` feeds extraction, where
        paragraph breaks carry meaning.
        """
        service = make_service(backend="none")
        page_text = "alpha\n\nbeta"
        service._process_converted_page(0, JPEG_BYTES, page_text, "out", "doc")

        assert s3_writes.content_for("doc/pages/1/result.json") == {"text": page_text}

    def test_pipes_are_escaped_so_the_confidence_table_stays_parseable(self, s3_writes):
        """A raw `|` from a CSV would split into phantom markdown columns."""
        service = make_service(backend="none")
        service._process_converted_page(0, JPEG_BYTES, "a|b|c", "out", "doc")

        confidence = s3_writes.content_for("doc/pages/1/textConfidence.json")["text"]
        assert "| a\\|b\\|c | 99.0 |" in confidence

    def test_metering_names_conversion_rather_than_an_ocr_api(self, s3_writes):
        """No OCR API was called, so billing must not attribute one."""
        result, metering = service_converted_metering(s3_writes)
        assert metering == {"OCR/converted/document_conversion": {"pages": 1}}
        assert_uris_were_written(result, s3_writes)

    def test_page_data_is_tagged_as_converted_with_placeholder_confidence(
        self, s3_writes
    ):
        """`provider` is how the UI knows the 99.0 scores are synthetic."""
        service = make_service(backend="none")
        service._process_converted_page(0, JPEG_BYTES, "alpha", "out", "doc")

        page_data = s3_writes.content_for("doc/pages/1/pageData.json")
        assert page_data["provider"] == "converted"
        assert page_data["geometryAvailable"] is False
        assert [line["confidence"] for line in page_data["lines"]] == [99.0]


def service_converted_metering(recorder: S3Recorder):
    service = make_service(backend="none")
    return service._process_converted_page(0, JPEG_BYTES, "alpha", "out", "doc")


# ---------------------------------------------------------------------------
# _detect_file_type
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDetectFileType:
    """File-type detection, which selects the whole downstream processing path."""

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("a.txt", "txt"),
            ("a.csv", "csv"),
            ("a.xlsx", "xlsx"),
            ("a.xls", "xlsx"),
            ("a.docx", "docx"),
            ("a.doc", "docx"),
            ("a.pdf", "pdf"),
            ("a.jpg", "jpg"),
            ("a.jpeg", "jpeg"),
            ("a.png", "png"),
            ("a.tif", "tif"),
            ("a.webp", "webp"),
            ("A.PDF", "pdf"),
        ],
    )
    def test_extension_decides_when_present(self, filename, expected):
        """Legacy extensions must map onto their modern handler, not fall through.

        `.doc` reaching the PDF branch is how an unreadable legacy file became a
        blank page rather than a clear error.
        """
        service = make_service(backend="none")
        assert service._detect_file_type(filename, b"irrelevant") == expected

    def test_pdf_is_recognised_from_its_magic_bytes(self):
        service = make_service(backend="none")
        assert service._detect_file_type("upload", b"%PDF-1.7\nstuff") == "pdf"

    def test_zip_containers_are_told_apart_by_their_internal_paths(self):
        """Excel and Word are both ZIPs; the wrong one produces an empty document."""
        service = make_service(backend="none")
        assert service._detect_file_type("x", b"PK\x03\x04" + b"xl/workbook") == "xlsx"
        assert (
            service._detect_file_type("x", b"PK\x03\x04" + b"word/document") == "docx"
        )

    def test_the_container_marker_is_only_looked_for_near_the_start(self):
        """The sniff window is the first 1000 bytes, and that boundary is real.

        Asserted because a reader of the code cannot tell whether `content[:1000]`
        is a deliberate bound or an accident. The filler is deliberately
        undecodable so the final text fallback cannot answer instead, which
        isolates the window: an extensionless `.xlsx` whose central directory sits
        past 1000 bytes is handed to the PDF renderer.
        """
        service = make_service(backend="none")
        near_marker = b"PK\x03\x04" + (b"\xff" * 100) + b"xl/workbook"
        far_marker = b"PK\x03\x04" + (b"\xff" * 2000) + b"xl/workbook"
        assert service._detect_file_type("x", near_marker) == "xlsx"
        assert service._detect_file_type("x", far_marker) == "pdf"

    def test_decodable_bytes_with_no_extension_are_treated_as_text(self):
        service = make_service(backend="none")
        assert service._detect_file_type("notes", "héllo".encode()) == "txt"

    def test_undecodable_bytes_fall_back_to_pdf(self):
        service = make_service(backend="none")
        assert service._detect_file_type("blob", b"\xff\xfe\x00\x80\x81") == "pdf"


# ---------------------------------------------------------------------------
# _process_non_pdf_document
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProcessNonPdfDocument:
    """Dispatch into `DocumentConverter`, plus how conversion failures are reported."""

    def test_unreadable_legacy_format_propagates_instead_of_becoming_a_blank_page(
        self,
    ):
        """Issue #829: a blank page lets the document "succeed" with no content.

        Asserting the empty page is *not* produced matters as much as the raise:
        the generic handler two lines below does exactly that, and swapping the
        two `except` clauses would keep the exception type flowing nowhere.
        """
        service = make_service(backend="none")
        service.document_converter = MagicMock()
        service.document_converter.convert_word_to_pages.side_effect = (
            UnsupportedLegacyFormatError("legacy .doc")
        )

        with pytest.raises(UnsupportedLegacyFormatError):
            service._process_non_pdf_document("docx", b"\xd0\xcf\x11\xe0")

        service.document_converter._create_empty_page.assert_not_called()

    def test_other_conversion_errors_degrade_to_one_error_page(self):
        """A parse failure is recoverable-ish and still names the format."""
        service = make_service(backend="none")
        service.document_converter = MagicMock()
        service.document_converter.convert_excel_to_pages.side_effect = RuntimeError(
            "corrupt sheet"
        )
        service.document_converter._create_empty_page.return_value = b"blank"

        pages = service._process_non_pdf_document("xlsx", b"PK")

        assert pages == [(b"blank", "Error processing xlsx document")]

    def test_text_is_decoded_as_utf8_before_conversion(self):
        service = make_service(backend="none")
        service.document_converter = MagicMock()
        service.document_converter.convert_text_to_pages.return_value = [
            (b"img", "héllo")
        ]

        pages = service._process_non_pdf_document("txt", "héllo".encode())

        service.document_converter.convert_text_to_pages.assert_called_once_with(
            "héllo"
        )
        assert pages == [(b"img", "héllo")]

    def test_csv_goes_to_the_csv_converter_not_the_plain_text_one(self):
        """CSV needs table layout; routing it to the text converter loses columns."""
        service = make_service(backend="none")
        service.document_converter = MagicMock()
        service.document_converter.convert_csv_to_pages.return_value = [(b"i", "a,b")]

        service._process_non_pdf_document("csv", b"a,b")

        service.document_converter.convert_csv_to_pages.assert_called_once_with("a,b")
        service.document_converter.convert_text_to_pages.assert_not_called()

    def test_word_conversion_is_given_the_ocr_callback_for_embedded_images(self):
        """Without the callback, images inside a .docx contribute no text at all."""
        service = make_service(backend="none")
        service.document_converter = MagicMock()
        service.document_converter.convert_word_to_pages.return_value = []

        service._process_non_pdf_document("docx", b"PK")

        callback = service.document_converter.convert_word_to_pages.call_args.kwargs[
            "ocr_image_callback"
        ]
        assert callback == service._ocr_image_bytes

    def test_an_unknown_type_falls_back_to_text_when_it_decodes(self):
        service = make_service(backend="none")
        service.document_converter = MagicMock()
        service.document_converter.convert_text_to_pages.return_value = [(b"i", "hi")]

        pages = service._process_non_pdf_document("rtf", b"hi")

        assert pages == [(b"i", "hi")]

    def test_an_unknown_undecodable_type_yields_a_single_error_page(self):
        service = make_service(backend="none")
        service.document_converter = MagicMock()
        service.document_converter.convert_text_to_pages.side_effect = (
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")
        )
        service.document_converter._create_empty_page.return_value = b"blank"

        pages = service._process_non_pdf_document("bin", b"\xff\xfe")

        assert pages == [(b"blank", "Error: Unable to process file")]


# ---------------------------------------------------------------------------
# BDA ARN resolution
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBdaArnResolution:
    """Project and profile ARN resolution for the BDA OCR backend."""

    def test_project_arn_comes_from_config_in_preference_to_the_environment(
        self, monkeypatch
    ):
        """An explicit config value must win, or a stack-wide env var overrides it."""
        monkeypatch.setenv("BDA_OCR_PROJECT_ARN", "arn:from-env")
        service = make_service(
            config={"ocr": {"backend": "bda", "bda_project_arn": "arn:from-config"}}
        )
        assert service.bda_project_arn == "arn:from-config"

    def test_project_arn_falls_back_to_the_environment_variable(self, monkeypatch):
        """The stack delivers the provisioned project this way."""
        monkeypatch.setenv("BDA_OCR_PROJECT_ARN", "arn:from-env")
        service = make_service(config={"ocr": {"backend": "bda"}})
        assert service.bda_project_arn == "arn:from-env"

    def test_no_project_arn_anywhere_leaves_it_unset(self, monkeypatch):
        """Resolution is deferred, so construction must not raise here."""
        monkeypatch.delenv("BDA_OCR_PROJECT_ARN", raising=False)
        service = make_service(config={"ocr": {"backend": "bda"}})
        assert service.bda_project_arn is None

    def test_missing_project_arn_raises_an_actionable_error_on_first_use(
        self, monkeypatch
    ):
        """The message has to name both ways of supplying it and the alternative.

        This fires in regions without BDA and in GovCloud/China, where the stack
        deliberately does not create the project, so "no project ARN" is a
        configuration answer an operator can act on, not an internal error.
        """
        monkeypatch.delenv("BDA_OCR_PROJECT_ARN", raising=False)
        service = make_service(config={"ocr": {"backend": "bda"}})

        with pytest.raises(ValueError) as excinfo:
            service._ensure_bda_arns()

        message = str(excinfo.value)
        assert "ocr.bda_project_arn" in message
        assert "BDA_OCR_PROJECT_ARN" in message
        assert "Textract" in message

    def test_the_profile_partition_is_read_from_the_caller_identity(self):
        """A hardcoded `aws` partition produces an unusable ARN in GovCloud.

        The partition is deliberately not `aws` here, so the assertion fails if
        the caller ARN is ignored.
        """
        service = make_service(
            config={"ocr": {"backend": "bda", "bda_project_arn": "arn:aws-us-gov:x"}},
            region="us-gov-west-1",
        )
        sts = MagicMock()
        sts.get_caller_identity.return_value = {
            "Account": "123456789012",
            "Arn": "arn:aws-us-gov:iam::123456789012:role/OcrRole",
        }

        with patch("boto3.client", return_value=sts):
            service._ensure_bda_arns()

        profile_arn = service._bda_profile_arn
        assert profile_arn is not None, "the profile ARN was never resolved"
        assert profile_arn.startswith("arn:aws-us-gov:bedrock:")
        assert "123456789012" in profile_arn

    def test_the_profile_arn_is_resolved_once_across_repeated_calls(self):
        """Every parallel page worker calls this; an STS call per page throttles."""
        service = make_service(
            config={"ocr": {"backend": "bda", "bda_project_arn": "arn:proj"}}
        )
        sts = MagicMock()
        sts.get_caller_identity.return_value = {
            "Account": "1",
            "Arn": "arn:aws:iam::1:role/R",
        }

        with patch("boto3.client", return_value=sts) as mock_client:
            service._ensure_bda_arns()
            first = service._bda_profile_arn
            service._ensure_bda_arns()
            service._ensure_bda_arns()

        assert mock_client.call_count == 1
        assert service._bda_profile_arn == first


# ---------------------------------------------------------------------------
# _run_bda_ocr / _write_bda_page_artifacts
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRunBdaOcr:
    """The synchronous BDA invocation and the artifacts derived from its output."""

    @pytest.fixture
    def bda_service(self):
        service = make_service(
            config={"ocr": {"backend": "bda", "bda_project_arn": "arn:proj"}}
        )
        service._bda_profile_arn = "arn:aws:bedrock:us-east-1:1:profile/x"
        return service

    def test_the_image_is_passed_by_s3_uri_and_never_inline(self, bda_service):
        """Inline bytes carry no extension, so BDA guesses the modality.

        Under concurrency that guess lands on IMAGE for some pages and the page
        OCRs to nothing, with a successful API response.
        """
        bda_service.bda_runtime_client.invoke_data_automation.return_value = {
            "outputSegments": [{"standardOutput": {}}]
        }
        with patch.object(service_module, "s3", S3Recorder()):
            bda_service._run_bda_ocr("s3://b/doc/pages/1/image.jpg")

        kwargs = bda_service.bda_runtime_client.invoke_data_automation.call_args.kwargs
        assert kwargs["inputConfiguration"] == {"s3Uri": "s3://b/doc/pages/1/image.jpg"}
        assert "bytes" not in json.dumps(kwargs, default=str)
        assert kwargs["dataAutomationConfiguration"]["stage"] == "LIVE"
        assert (
            kwargs["dataAutomationConfiguration"]["dataAutomationProjectArn"]
            == "arn:proj"
        )

    def test_a_json_string_standard_output_is_parsed(self, bda_service):
        """BDA returns the payload as a JSON string in some responses.

        Left as a string it reaches the converter as an unsubscriptable object and
        the page fails, so the parse is the difference between text and an error.
        """
        bda_service.bda_runtime_client.invoke_data_automation.return_value = {
            "outputSegments": [{"standardOutput": json.dumps({"pages": [{"id": 0}]})}]
        }
        with patched_bda_ocr(markdown="text") as mock_bda_ocr:
            bda_service._run_bda_ocr("s3://b/i.jpg")

        forwarded = mock_bda_ocr.bda_standard_output_to_textract_blocks.call_args.args[
            0
        ]
        assert forwarded == {"pages": [{"id": 0}]}

    def test_an_empty_segment_list_yields_an_empty_standard_output(self, bda_service):
        """No segments must not raise IndexError on a page that simply had no text."""
        bda_service.bda_runtime_client.invoke_data_automation.return_value = {}
        with patched_bda_ocr() as mock_bda_ocr:
            _, _, text, _ = bda_service._run_bda_ocr("s3://b/i.jpg")

        assert (
            mock_bda_ocr.bda_standard_output_to_textract_blocks.call_args.args[0] == {}
        )
        assert text == ""

    def test_the_metering_key_matches_the_bda_pricing_entry(self, bda_service):
        """`OCR/bda/documents-standard` is what `pricing.yaml` prices at $0.01/page.

        A renamed key does not fail anything; it silently costs nothing in the
        reporting lake.
        """
        bda_service.bda_runtime_client.invoke_data_automation.return_value = {
            "outputSegments": [{"standardOutput": {}}]
        }
        with patched_bda_ocr():
            *_, metering = bda_service._run_bda_ocr("s3://b/i.jpg")

        assert metering == {"OCR/bda/documents-standard": {"pages": 1}}

    def test_the_original_image_size_reaches_the_converter(self, bda_service):
        bda_service.bda_runtime_client.invoke_data_automation.return_value = {
            "outputSegments": [{"standardOutput": {}}]
        }
        with patched_bda_ocr() as mock_bda_ocr:
            bda_service._run_bda_ocr("s3://b/i.jpg", original_image_size=(800, 1000))

        kwargs = mock_bda_ocr.bda_standard_output_to_textract_blocks.call_args.kwargs
        assert kwargs["original_image_size"] == (800, 1000)

    def test_confidence_is_derived_from_the_converted_blocks(self, bda_service):
        """BDA supplies real per-line confidence, so it must not be placeholdered."""
        bda_service.bda_runtime_client.invoke_data_automation.return_value = {
            "outputSegments": [{"standardOutput": {}}]
        }
        blocks = {
            "Blocks": [
                {
                    "BlockType": "LINE",
                    "Id": "l1",
                    "Text": "Invoice 7",
                    "Confidence": 91.44,
                }
            ]
        }
        with patched_bda_ocr(blocks=blocks, markdown="Invoice 7"):
            _, confidence, _, _ = bda_service._run_bda_ocr("s3://b/i.jpg")

        assert "| Invoice 7 | 91.4 |" in confidence["text"]

    def test_write_bda_page_artifacts_persists_all_three_and_reports_their_keys(
        self, bda_service, s3_writes
    ):
        """The returned keys are used verbatim to build the page URIs."""
        with patch.object(
            bda_service,
            "_run_bda_ocr",
            return_value=(
                {"Blocks": [{"BlockType": "LINE", "Text": "x"}]},
                {"text": "| x | 90.0 |"},
                "x",
                {"OCR/bda/documents-standard": {"pages": 1}},
            ),
        ):
            (
                blocks,
                text,
                raw_key,
                confidence_key,
                parsed_key,
                metering,
            ) = bda_service._write_bda_page_artifacts("s3://b/i.jpg", "out", "doc", 4)

        assert raw_key == "doc/pages/4/rawText.json"
        assert confidence_key == "doc/pages/4/textConfidence.json"
        assert parsed_key == "doc/pages/4/result.json"
        assert set(s3_writes.keys) == {raw_key, confidence_key, parsed_key}
        assert s3_writes.content_for(parsed_key) == {"text": "x"}
        assert s3_writes.content_for(raw_key) == blocks
        assert text == "x"
        assert metering == {"OCR/bda/documents-standard": {"pages": 1}}


# ---------------------------------------------------------------------------
# _image_size_from_bytes
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestImageSizeFromBytes:
    """Sizing used only to rescale BDA geometry, so it must degrade, not raise."""

    @pytest.mark.parametrize("payload", [None, b""])
    def test_absent_bytes_are_reported_as_unknown(self, payload):
        assert OcrService._image_size_from_bytes(payload) is None

    def test_undecodable_bytes_are_reported_as_unknown_rather_than_raising(self):
        """A page must still OCR when only its geometry rescaling is unavailable."""
        assert OcrService._image_size_from_bytes(b"not an image at all") is None

    def test_a_real_image_reports_its_own_dimensions(self):
        """Asserted against a non-square size so width/height cannot be swapped."""
        payload = encode_image("PNG", size=(133, 71))
        assert OcrService._image_size_from_bytes(payload) == (133, 71)


# ---------------------------------------------------------------------------
# _extract_page_image
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExtractPageImage:
    """The render scale chosen for a PDF page, which sets the OCR resolution."""

    def test_dpi_defaults_high_enough_for_small_glyphs(self, s3_writes):
        """Issue #729: below ~200 dpi Textract drops faint glyphs with no signal.

        `DEFAULT_DPI` is referenced rather than repeated so the test tracks the
        constant, and the page is chosen small enough that the ceiling does not
        bind and reduce the scale.
        """
        service = make_service(backend="none")
        assert service.dpi is None
        page = mock_pdf_page(width_pt=200.0, height_pt=200.0)

        service._extract_page_image(page, True, 1)

        assert page.render.call_args.kwargs["scale"] == pytest.approx(DEFAULT_DPI / 72)

    def test_a_configured_dpi_is_honoured(self, s3_writes):
        """150 is deliberately not `DEFAULT_DPI`, so a fallback would fail here."""
        service = make_service(
            config={"ocr": {"backend": "none", "image": {"dpi": 150}}}
        )
        assert service.dpi == 150 and service.dpi != DEFAULT_DPI
        page = mock_pdf_page(width_pt=200.0, height_pt=200.0)

        service._extract_page_image(page, True, 1)

        assert page.render.call_args.kwargs["scale"] == pytest.approx(150 / 72)

    def test_an_oversized_page_is_rendered_at_a_reduced_scale(self, s3_writes):
        """The ceiling is enforced at render time, not by resizing afterwards.

        Rendering full size and shrinking later is what caused the OOM this guard
        exists for, so the reduction has to appear in the `scale` argument.

        The DPI is deliberately 150 rather than `DEFAULT_DPI`. This calculation
        reads `self.dpi or DEFAULT_DPI` twice — once to work out the page's full
        pixel size and once to build the render matrix — and at 300 dpi either
        read could fall back to the constant and still produce the same number,
        so the test would not distinguish the configured value from the default.
        """
        service = make_service(
            config={
                "ocr": {
                    "backend": "none",
                    "image": {"dpi": 150, "target_width": 600, "target_height": 600},
                }
            }
        )
        assert service.dpi != DEFAULT_DPI
        page = mock_pdf_page(width_pt=612.0, height_pt=792.0)

        service._extract_page_image(page, True, 1)

        # 612x792pt at 150dpi is 1275x1650px; the binding ratio is 600/1650.
        expected = (150 / 72) * min(600 / 1275, 600 / 1650)
        scale = page.render.call_args.kwargs["scale"]
        assert scale == pytest.approx(expected)
        assert scale < 150 / 72

    def test_a_page_already_inside_the_ceiling_is_not_downscaled(self, s3_writes):
        """Never upscale, and never shrink a page that already fits."""
        service = make_service(
            config={
                "ocr": {
                    "backend": "none",
                    "image": {"dpi": 72, "target_width": 5000, "target_height": 5000},
                }
            }
        )
        page = mock_pdf_page(width_pt=612.0, height_pt=792.0)

        service._extract_page_image(page, True, 1)

        assert page.render.call_args.kwargs["scale"] == pytest.approx(1.0)

    def test_a_partial_ceiling_renders_at_plain_dpi(self, s3_writes):
        """With only one target dimension no scale factor can be computed."""
        service = make_service(
            config={
                "ocr": {
                    "backend": "none",
                    "image": {"dpi": 200, "target_width": 100},
                }
            }
        )
        page = mock_pdf_page(width_pt=612.0, height_pt=792.0)

        service._extract_page_image(page, True, 1)

        assert page.render.call_args.kwargs["scale"] == pytest.approx(200 / 72)

    def test_a_non_pdf_page_is_rendered_at_its_own_resolution(self, s3_writes):
        """DPI scaling is meaningless for a raster source already in pixels."""
        service = make_service(backend="none")
        page = mock_pdf_page(width_pt=100.0, height_pt=100.0)

        service._extract_page_image(page, False, 1)

        assert page.render.call_args == ((), {})

    def test_the_result_is_jpeg_encoded_bytes(self, s3_writes):
        service = make_service(backend="none")
        page = mock_pdf_page(width_pt=200.0, height_pt=200.0)

        payload = service._extract_page_image(page, True, 1)

        assert PILImage.open(io.BytesIO(payload)).format == "JPEG"


# ---------------------------------------------------------------------------
# process_document
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProcessDocument:
    """Document-level orchestration: page fan-out, retry skipping, ordering, errors."""

    @pytest.fixture
    def document(self):
        return Document(
            id="d1",
            input_bucket="in",
            input_key="doc.pdf",
            output_bucket="out",
            status=Status.OCR,
        )

    def _stub_s3_get(self, service: OcrService, payload: bytes) -> None:
        body = MagicMock()
        body.read.return_value = payload
        service.s3_client.get_object.return_value = {"Body": body}

    def test_a_converted_document_yields_one_page_per_converted_page(
        self, document, s3_writes
    ):
        """Text/CSV/Office documents bypass rendering entirely."""
        document.input_key = "notes.txt"
        service = make_service(backend="none")
        self._stub_s3_get(service, b"line one\nline two")
        service.document_converter = MagicMock()
        service.document_converter.convert_text_to_pages.return_value = [
            (JPEG_BYTES, "line one"),
            (JPEG_BYTES, "line two"),
        ]

        result = service.process_document(document)

        assert result.num_pages == 2
        assert sorted(result.pages) == ["1", "2"]
        assert result.metering == {"OCR/converted/document_conversion": {"pages": 2}}
        assert result.status != Status.FAILED
        assert result.errors == []

    def test_an_s3_failure_fails_the_document_and_records_why(
        self, document, s3_writes
    ):
        """Nothing downstream can recover, so the status must not stay OCR."""
        service = make_service(backend="none")
        service.s3_client.get_object.side_effect = RuntimeError("AccessDenied")

        result = service.process_document(document)

        assert result.status == Status.FAILED
        assert len(result.errors) == 1
        assert "AccessDenied" in result.errors[0]
        assert result.pages == {}

    def test_a_single_failing_page_fails_the_document_and_names_that_page(
        self, document, s3_writes
    ):
        """The error must carry the one-based page number an operator will look up.

        Reporting page 1 for the second page sends the investigation to the wrong
        image, and reporting success for a document missing a page is worse.
        """
        service = make_service(backend="none")
        self._stub_s3_get(service, b"%PDF-1.4")

        pdf_doc = MagicMock()
        pdf_doc.__len__ = MagicMock(return_value=2)
        pdf_doc.__getitem__ = MagicMock(side_effect=lambda i: mock_pdf_page())

        def fake_page(index, img, bucket, prefix):
            if index == 1:
                raise RuntimeError("ProvisionedThroughputExceeded")
            return (
                {
                    "image_uri": "s3://out/p1/image.jpg",
                    "raw_text_uri": "s3://out/p1/rawText.json",
                    "parsed_text_uri": "s3://out/p1/result.json",
                    "text_confidence_uri": "s3://out/p1/textConfidence.json",
                },
                {},
            )

        with (
            patch.object(service_module, "pdfium") as mock_pdfium,
            patch.object(service, "_process_page_with_image", side_effect=fake_page),
        ):
            mock_pdfium.PdfDocument.return_value = pdf_doc
            result = service.process_document(document)

        assert result.status == Status.FAILED
        assert len(result.errors) == 1
        assert "page 2" in result.errors[0]
        assert "ProvisionedThroughputExceeded" in result.errors[0]
        assert sorted(result.pages) == ["1"], "the good page should still be kept"

    def test_completed_pages_are_skipped_when_ocr_is_retried(self, document, s3_writes):
        """Re-OCRing a finished page pays Textract twice for the same result."""
        from idp_common.models import Page

        document.pages["1"] = Page(
            page_id="1",
            image_uri="s3://out/doc/pages/1/image.jpg",
            raw_text_uri="s3://out/doc/pages/1/rawText.json",
            parsed_text_uri="s3://out/doc/pages/1/result.json",
            text_confidence_uri="s3://out/doc/pages/1/textConfidence.json",
        )
        service = make_service(backend="none")
        self._stub_s3_get(service, b"%PDF-1.4")

        pdf_doc = MagicMock()
        pdf_doc.__len__ = MagicMock(return_value=2)
        pdf_doc.__getitem__ = MagicMock(side_effect=lambda i: mock_pdf_page())

        with (
            patch.object(service_module, "pdfium") as mock_pdfium,
            patch.object(
                service,
                "_process_page_with_image",
                return_value=(
                    {
                        "image_uri": "s3://out/x/image.jpg",
                        "raw_text_uri": "s3://out/x/rawText.json",
                        "parsed_text_uri": "s3://out/x/result.json",
                        "text_confidence_uri": "s3://out/x/textConfidence.json",
                    },
                    {},
                ),
            ) as mock_page,
        ):
            mock_pdfium.PdfDocument.return_value = pdf_doc
            result = service.process_document(document)

        processed_indices = [c.args[0] for c in mock_page.call_args_list]
        assert processed_indices == [1], "only the unfinished page should be processed"
        assert result.num_pages == 2

    @pytest.mark.parametrize(
        "missing", ["image_uri", "raw_text_uri", "parsed_text_uri"]
    )
    def test_a_partially_written_page_is_reprocessed(
        self, document, s3_writes, missing
    ):
        """The skip must require *every* artifact it names, not merely one of them.

        A page whose image was uploaded before the process died has no text, and
        skipping it leaves a permanently empty page that never retries. Each URI
        is dropped in turn rather than all at once, because with all three absent
        any single surviving check still refuses the skip and the test would pass
        while two of the three conditions did nothing.
        """
        from idp_common.models import Page

        uris = {
            "image_uri": "s3://out/doc/pages/1/image.jpg",
            "raw_text_uri": "s3://out/doc/pages/1/rawText.json",
            "parsed_text_uri": "s3://out/doc/pages/1/result.json",
        }
        uris[missing] = None
        document.pages["1"] = Page(
            page_id="1",
            text_confidence_uri="s3://out/doc/pages/1/textConfidence.json",
            **uris,
        )
        service = make_service(backend="none")
        self._stub_s3_get(service, b"%PDF-1.4")

        pdf_doc = MagicMock()
        pdf_doc.__len__ = MagicMock(return_value=1)
        pdf_doc.__getitem__ = MagicMock(side_effect=lambda i: mock_pdf_page())

        with (
            patch.object(service_module, "pdfium") as mock_pdfium,
            patch.object(
                service,
                "_process_page_with_image",
                return_value=(
                    {
                        "image_uri": "s3://out/x/image.jpg",
                        "raw_text_uri": "s3://out/x/rawText.json",
                        "parsed_text_uri": "s3://out/x/result.json",
                        "text_confidence_uri": "s3://out/x/textConfidence.json",
                    },
                    {},
                ),
            ) as mock_page,
        ):
            mock_pdfium.PdfDocument.return_value = pdf_doc
            service.process_document(document)

        assert [c.args[0] for c in mock_page.call_args_list] == [0]

    def test_pages_are_ordered_numerically_not_lexicographically(
        self, document, s3_writes
    ):
        """Eleven pages is the smallest count that tells the two orders apart.

        Sections are assembled by iterating this dict, so lexicographic order
        ("1", "10", "11", "2", ...) silently reorders the document's content.
        """
        service = make_service(backend="none")
        self._stub_s3_get(service, b"%PDF-1.4")

        pdf_doc = MagicMock()
        pdf_doc.__len__ = MagicMock(return_value=11)
        pdf_doc.__getitem__ = MagicMock(side_effect=lambda i: mock_pdf_page())

        with (
            patch.object(service_module, "pdfium") as mock_pdfium,
            patch.object(
                service,
                "_process_page_with_image",
                side_effect=lambda index, *a: (
                    {
                        "image_uri": f"s3://out/{index}/image.jpg",
                        "raw_text_uri": f"s3://out/{index}/rawText.json",
                        "parsed_text_uri": f"s3://out/{index}/result.json",
                        "text_confidence_uri": f"s3://out/{index}/tc.json",
                    },
                    {},
                ),
            ),
        ):
            mock_pdfium.PdfDocument.return_value = pdf_doc
            result = service.process_document(document)

        assert list(result.pages) == [str(n) for n in range(1, 12)]

    def test_fillable_form_fields_are_flattened_before_rendering(
        self, document, s3_writes
    ):
        """Without `flatten()` a government form renders with every field blank.

        Only pages that actually have a form environment should be flattened, so
        both directions are covered in one document.
        """
        service = make_service(backend="none")
        self._stub_s3_get(service, b"%PDF-1.4")

        with_form = mock_pdf_page()
        with_form.formenv = MagicMock()
        without_form = mock_pdf_page()
        without_form.formenv = None

        pdf_doc = MagicMock()
        pdf_doc.__len__ = MagicMock(return_value=2)
        pdf_doc.__getitem__ = MagicMock(
            side_effect=lambda i: [with_form, without_form][i]
        )

        with (
            patch.object(service_module, "pdfium") as mock_pdfium,
            patch.object(
                service,
                "_process_page_with_image",
                return_value=(
                    {
                        "image_uri": "s3://out/x/i.jpg",
                        "raw_text_uri": "s3://out/x/r.json",
                        "parsed_text_uri": "s3://out/x/p.json",
                        "text_confidence_uri": "s3://out/x/t.json",
                    },
                    {},
                ),
            ),
        ):
            mock_pdfium.PdfDocument.return_value = pdf_doc
            service.process_document(document)

        pdf_doc.init_forms.assert_called_once()
        with_form.flatten.assert_called_once()
        without_form.flatten.assert_not_called()

    def test_an_image_upload_becomes_a_single_page_document(self, document, s3_writes):
        """Images take the direct path with no PDF rendering at all."""
        document.input_key = "scan.png"
        service = make_service(backend="none")
        self._stub_s3_get(service, PNG_BYTES)

        with patch.object(service_module, "pdfium") as mock_pdfium:
            result = service.process_document(document)

        mock_pdfium.PdfDocument.assert_not_called()
        assert result.num_pages == 1
        assert list(result.pages) == ["1"]
        assert result.pages["1"].image_uri == "s3://out/scan.png/pages/1/image.png"

    def test_an_unreadable_legacy_office_file_fails_the_whole_document(
        self, document, s3_writes
    ):
        """The exception has to reach `process_document`'s handler, not a page's.

        A per-page rescue would leave the document FAILED but with a blank page
        recorded, which is the outcome #829 set out to remove.
        """
        document.input_key = "old.doc"
        service = make_service(backend="none")
        self._stub_s3_get(service, b"\xd0\xcf\x11\xe0")
        service.document_converter = MagicMock()
        service.document_converter.convert_word_to_pages.side_effect = (
            UnsupportedLegacyFormatError("cannot read legacy .doc")
        )

        result = service.process_document(document)

        assert result.status == Status.FAILED
        assert result.pages == {}
        assert "cannot read legacy .doc" in result.errors[0]


# ---------------------------------------------------------------------------
# _parse_textract_response fallbacks
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseTextractResponseFallbacks:
    """Markdown linearization is fragile, and its fallbacks must keep the text."""

    def _service_with_parse_failure(self, error: Exception) -> OcrService:
        service = make_service(backend="textract")
        parsed = MagicMock()
        parsed.to_markdown.side_effect = error
        parsed.text = "plain text fallback"
        self._parsed = parsed
        return service

    def _run(self, service: OcrService, response: Dict) -> Dict:
        mock_parser = MagicMock()
        mock_parser.parse.return_value = self._parsed
        with patch.dict(
            "sys.modules",
            {"textractor.parsers": MagicMock(response_parser=mock_parser)},
        ):
            return service._parse_textract_response(response, 3)

    def test_a_signature_linearizer_failure_still_returns_the_page_text(self, caplog):
        """Losing the page because markdown failed would lose the whole document."""
        service = self._service_with_parse_failure(
            RuntimeError("reading_order failed on Signature object")
        )
        with caplog.at_level("WARNING"):
            result = self._run(service, textract_response())

        assert result["text"] == "plain text fallback"
        assert "SIGNATURES feature" in caplog.text

    def test_a_forms_linearizer_failure_is_attributed_to_forms(self, caplog):
        """The two diagnostics are one `elif` apart and name different features."""
        service = self._service_with_parse_failure(
            RuntimeError("reading_order failed on KeyValue object")
        )
        with caplog.at_level("WARNING"):
            result = self._run(service, textract_response())

        assert result["text"] == "plain text fallback"
        assert "FORMS feature" in caplog.text
        assert "SIGNATURES feature" not in caplog.text

    def test_an_unrelated_markdown_failure_is_not_misattributed(self, caplog):
        """A generic failure must not be reported as a features problem."""
        service = self._service_with_parse_failure(RuntimeError("boom"))
        with caplog.at_level("WARNING"):
            result = self._run(service, textract_response())

        assert result["text"] == "plain text fallback"
        assert "SIGNATURES feature" not in caplog.text
        assert "FORMS feature" not in caplog.text

    def test_a_total_parser_failure_falls_back_to_the_line_blocks(self):
        """Textractor refusing the response must not discard text Textract found."""
        service = make_service(backend="textract")
        mock_parser = MagicMock()
        mock_parser.parse.side_effect = RuntimeError("unparseable")

        with patch.dict(
            "sys.modules",
            {"textractor.parsers": MagicMock(response_parser=mock_parser)},
        ):
            result = service._parse_textract_response(
                textract_response(lines=["alpha", "beta"]), 1
            )

        assert result["text"] == "alpha\nbeta"

    def test_a_page_with_no_recoverable_text_says_so_explicitly(self):
        """An empty string would read downstream as a genuinely blank page."""
        service = make_service(backend="textract")
        mock_parser = MagicMock()
        mock_parser.parse.side_effect = RuntimeError("unparseable")

        with patch.dict(
            "sys.modules",
            {"textractor.parsers": MagicMock(response_parser=mock_parser)},
        ):
            result = service._parse_textract_response(
                {"DocumentMetadata": {"Pages": 1}, "Blocks": []}, 7
            )

        assert result["text"] != ""
        assert "Error extracting text" in result["text"]
        assert "page 7" in result["text"]


# ---------------------------------------------------------------------------
# _ocr_image_bytes (embedded DOCX images)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOcrImageBytesBdaBackend:
    """Embedded-image OCR has no BDA branch, and that matters to report."""

    def test_the_bda_backend_silently_uses_textract_for_embedded_images(self):
        """Documented here because the `else` covers BDA as well as Textract.

        With `backend="bda"` there is no `textract_client` attribute at all, so
        the call raises and the helper returns its failure placeholder instead of
        the image's text. Embedded images in a .docx therefore contribute nothing
        under the BDA backend.
        """
        service = make_service(
            config={"ocr": {"backend": "bda", "bda_project_arn": "arn:proj"}}
        )
        assert not hasattr(service, "textract_client")

        assert service._ocr_image_bytes(JPEG_BYTES) == "[Image - OCR failed]"

    def test_the_none_backend_marks_the_image_without_calling_anything(self):
        service = make_service(backend="none")
        assert service._ocr_image_bytes(JPEG_BYTES) == "[Image]"


# ---------------------------------------------------------------------------
# Memory monitoring
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMemoryMonitoring:
    """The background sampler that runs for the lifetime of the page fan-out."""

    def test_setting_the_event_stops_the_monitor_thread(self):
        """A thread that ignores the shutdown event keeps logging after the run.

        It is a daemon thread, so the leak is invisible until a Lambda container
        accumulates one per invocation.
        """
        import threading

        service = make_service(backend="none")
        before = threading.active_count()
        shutdown = service._start_memory_monitoring()
        shutdown.set()

        for thread in threading.enumerate():
            if thread.name.startswith("Thread-") and thread.daemon:
                thread.join(timeout=2.0)

        assert shutdown.is_set()
        assert threading.active_count() <= before + 1


# ---------------------------------------------------------------------------
# Remaining branches: dispatch, artifact shapes, and the memory sampler
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProcessSinglePageDispatch:
    """`_process_single_page` chooses the backend path for one page."""

    def test_page_zero_of_a_non_pdf_is_handed_to_the_direct_image_path(self):
        """An image must not be routed through PDF rendering.

        `pdf_document` is `None` for an image upload, so a mis-route is an
        immediate `TypeError` rather than a wrong answer — but only if the
        original bytes are forwarded, which is what is asserted.
        """
        service = make_service(backend="none")
        with patch.object(
            service, "_process_image_file_direct", return_value=({}, {})
        ) as direct:
            service._process_single_page(0, None, False, "out", "doc", PNG_BYTES)

        direct.assert_called_once_with("out", "doc", PNG_BYTES)

    def test_the_bda_backend_renders_the_page_then_reuses_the_image_path(self):
        """BDA has no PDF entry point of its own; it renders first, like a worker."""
        service = make_service(
            config={"ocr": {"backend": "bda", "bda_project_arn": "arn:proj"}}
        )
        pdf_doc = MagicMock()
        pdf_doc.__getitem__ = MagicMock(return_value=mock_pdf_page())

        with (
            patch.object(
                service, "_extract_page_image", return_value=b"RENDERED"
            ) as extract,
            patch.object(
                service, "_process_page_with_image", return_value=({}, {})
            ) as with_image,
        ):
            service._process_single_page(2, pdf_doc, True, "out", "doc")

        assert extract.call_args.args[1:] == (True, 3)
        assert with_image.call_args.args == (2, b"RENDERED", "out", "doc")

    def test_textract_features_reach_analyze_document_on_the_pdf_page_path(
        self, s3_writes
    ):
        """The per-page Textract path repeats the API choice and can drift from it."""
        service = make_service(
            config={"ocr": {"backend": "textract", "features": [{"name": "LAYOUT"}]}}
        )
        service.textract_client.analyze_document.return_value = textract_response()
        pdf_doc = MagicMock()
        pdf_doc.__getitem__ = MagicMock(return_value=mock_pdf_page())

        _, metering = service._process_single_page(0, pdf_doc, True, "out", "doc")

        service.textract_client.detect_document_text.assert_not_called()
        assert service.textract_client.analyze_document.call_args.kwargs[
            "FeatureTypes"
        ] == ["LAYOUT"]
        assert metering == {"OCR/textract/analyze_document-Layout": {"pages": 1}}

    def test_bedrock_page_preprocessing_binarises_only_the_model_input(self, s3_writes):
        """Same split as the worker path, in the code that renders its own page."""
        service = make_service(
            config=bedrock_config_dict(image={"preprocessing": True})
        )
        pdf_doc = MagicMock()
        pdf_doc.__getitem__ = MagicMock(return_value=mock_pdf_page())

        mock_bedrock = MagicMock()
        mock_bedrock.invoke_model.return_value = bedrock_reply()
        mock_bedrock.extract_text_from_response.return_value = "t"
        mock_image = MagicMock()

        with (
            patch.object(service_module, "bedrock", mock_bedrock),
            patch.object(service_module, "image", mock_image),
            patch.object(
                _image_module,
                "apply_adaptive_binarization",
                return_value=b"BINARISED",
            ) as binarize,
        ):
            service._process_single_page(0, pdf_doc, True, "out", "doc")

        binarize.assert_called_once()
        assert s3_writes.content_for("doc/pages/1/image.jpg") == JPEG_BYTES
        assert (
            mock_image.prepare_bedrock_image_attachment.call_args.args[0]
            == b"BINARISED"
        )

    def test_textract_image_page_preprocessing_binarises_only_the_ocr_input(
        self, s3_writes, real_resize
    ):
        """The direct-image Textract branch has its own copy of the same split."""
        service = make_service(
            config={"ocr": {"backend": "textract", "image": {"preprocessing": True}}}
        )
        service.textract_client.detect_document_text.return_value = textract_response()

        with patch.object(
            _image_module, "apply_adaptive_binarization", return_value=b"BINARISED"
        ):
            service._process_image_file_direct("out", "doc", PNG_BYTES)

        assert s3_writes.content_for("doc/pages/1/image.png") == PNG_BYTES
        sent = service.textract_client.detect_document_text.call_args.kwargs[
            "Document"
        ]["Bytes"]
        assert sent == b"BINARISED"


@pytest.mark.unit
class TestResizeChangingTheStoredFormat:
    """A resize that re-encodes must carry the new format into key and content type."""

    def test_the_stored_extension_and_content_type_follow_the_resized_format(
        self, s3_writes
    ):
        """`resize_image` may not preserve the input format.

        A `.png` key holding JPEG bytes served as `image/png` is the kind of
        mismatch that works in a browser and breaks BDA's extension-based
        modality routing and any consumer that trusts the extension.
        """
        service = make_service(
            config={
                "ocr": {
                    "backend": "none",
                    "image": {"target_width": 40, "target_height": 30},
                }
            }
        )
        mock_image = MagicMock()
        mock_image.resize_image.return_value = encode_image("JPEG", size=(40, 30))

        with patch.object(service_module, "image", mock_image):
            result = service._process_image_file_direct("out", "doc", PNG_BYTES)[0]

        assert result["image_uri"] == "s3://out/doc/pages/1/image.jpg"
        assert s3_writes.content_type_for("doc/pages/1/image.jpg") == "image/jpeg"

    def test_a_resize_that_preserves_the_format_keeps_the_original_content_type(
        self, s3_writes
    ):
        """The complementary direction, so the branch above cannot be unconditional."""
        service = make_service(
            config={
                "ocr": {
                    "backend": "none",
                    "image": {"target_width": 40, "target_height": 30},
                }
            }
        )
        mock_image = MagicMock()
        mock_image.resize_image.return_value = encode_image("PNG", size=(40, 30))

        with patch.object(service_module, "image", mock_image):
            result = service._process_image_file_direct("out", "doc", PNG_BYTES)[0]

        assert result["image_uri"] == "s3://out/doc/pages/1/image.png"
        assert s3_writes.content_type_for("doc/pages/1/image.png") == "image/png"


@pytest.mark.unit
class TestExtractPageImageRasterScaling:
    """The non-PDF branches of the render-scale calculation."""

    def test_a_raster_page_is_scaled_without_a_dpi_factor(self):
        """Pixels are already pixels; multiplying by dpi/72 would upscale 4x.

        The configured dpi is deliberately non-default so a leaked dpi factor
        changes the expected value rather than coinciding with it.
        """
        service = make_service(
            config={
                "ocr": {
                    "backend": "none",
                    "image": {"dpi": 288, "target_width": 600, "target_height": 600},
                }
            }
        )
        page = mock_pdf_page(width_pt=4000.0, height_pt=2000.0)

        service._extract_page_image(page, False, 1)

        assert page.render.call_args.kwargs["scale"] == pytest.approx(600 / 4000)

    def test_a_raster_page_with_a_partial_ceiling_renders_unscaled(self):
        """No scale factor is computable from one dimension, so render as-is."""
        service = make_service(
            config={"ocr": {"backend": "none", "image": {"target_height": 500}}}
        )
        page = mock_pdf_page(width_pt=4000.0, height_pt=2000.0)

        service._extract_page_image(page, False, 1)

        assert page.render.call_args == ((), {})


@pytest.mark.unit
class TestConfidenceTableDetails:
    """Details of the markdown confidence artifact the assessment prompt consumes."""

    def test_handwriting_is_flagged_in_the_confidence_table(self):
        """Hand-filled values are the least reliable text on a form.

        Without the marker the assessment model sees a 95%-confidence line and no
        indication that it was handwritten, which is the signal that should lower
        its trust in the extracted value.
        """
        service = make_service(backend="none")
        data = service._generate_text_confidence_data(
            {
                "Blocks": [
                    {
                        "BlockType": "LINE",
                        "Text": "J. Smith",
                        "Confidence": 95.04,
                        "TextType": "HANDWRITING",
                    },
                    {
                        "BlockType": "LINE",
                        "Text": "Printed Name",
                        "Confidence": 99.1,
                        "TextType": "PRINTED",
                    },
                ]
            }
        )

        assert "| J. Smith (HANDWRITING) | 95.0 |" in data["text"]
        assert "| Printed Name | 99.1 |" in data["text"]
        assert "Printed Name (HANDWRITING)" not in data["text"]

    def test_a_signature_without_a_confidence_is_reported_as_unknown(self):
        """A missing detection score must not be rendered as a number.

        `0` would read as "almost certainly not a signature" and `100` as
        certainty; neither is what an absent field means.
        """
        summary = OcrService._format_signature_summary(
            [
                {
                    "id": "s1",
                    "confidence": None,
                    "geometry": {
                        "boundingBox": {
                            "left": 0.1,
                            "top": 0.8,
                            "width": 0.2,
                            "height": 0.05,
                        }
                    },
                }
            ]
        )

        assert "confidence=unknown" in summary
        assert "confidence=0" not in summary
        assert "confidence=100" not in summary

    def test_a_signature_without_geometry_reports_an_unknown_position(self):
        """Position is what attributes a mark to a field, so absence must be said."""
        summary = OcrService._format_signature_summary(
            [{"id": "s1", "confidence": 90.0, "geometry": None}]
        )
        assert "page position: unknown" in summary


@pytest.mark.unit
class TestGeometryAndWordResolution:
    """Geometry extraction and LINE-to-WORD resolution in `_build_page_data`."""

    def test_a_block_whose_geometry_is_not_a_mapping_yields_no_geometry(self):
        """Malformed geometry must degrade to none, not raise or fabricate zeros.

        A box of all zeros would draw a highlight in the page corner for every
        line, which looks like a rendering bug rather than missing data.
        """
        assert OcrService._extract_geometry({"Geometry": "not-a-dict"}) is None
        assert OcrService._extract_geometry({"Geometry": {"BoundingBox": []}}) is None
        assert OcrService._extract_geometry({}) is None

    def test_non_child_relationships_do_not_become_words(self):
        """Textract emits VALUE and MERGED_CELL relationships on the same blocks.

        Treating them as children would attach a form value as a word of the line
        that references it, duplicating text in the word-level geometry the UI
        draws.
        """
        service = make_service(backend="none")
        page_data = service._build_page_data(
            {
                "Blocks": [
                    {
                        "BlockType": "LINE",
                        "Id": "l1",
                        "Text": "Name",
                        "Confidence": 99.0,
                        "Relationships": [{"Type": "VALUE", "Ids": ["w1"]}],
                    },
                    {
                        "BlockType": "WORD",
                        "Id": "w1",
                        "Text": "Name",
                        "Confidence": 99.0,
                    },
                ]
            },
            "Name",
            "textract",
        )

        assert page_data["lines"][0]["words"] is None
        assert page_data["wordsAvailable"] is False

    def test_child_ids_that_are_not_words_are_ignored(self):
        """A CHILD id may point at a SELECTION_ELEMENT or at nothing at all.

        A dangling id resolving to `None` would otherwise append a word with no
        text, which reaches the geometry grounder as an empty match target.
        """
        service = make_service(backend="none")
        page_data = service._build_page_data(
            {
                "Blocks": [
                    {
                        "BlockType": "LINE",
                        "Id": "l1",
                        "Text": "Agreed",
                        "Confidence": 99.0,
                        "Relationships": [
                            {"Type": "CHILD", "Ids": ["sel1", "missing", "w1"]}
                        ],
                    },
                    {"BlockType": "SELECTION_ELEMENT", "Id": "sel1"},
                    {
                        "BlockType": "WORD",
                        "Id": "w1",
                        "Text": "Agreed",
                        "Confidence": 98.0,
                    },
                ]
            },
            "Agreed",
            "textract",
        )

        words = page_data["lines"][0]["words"]
        assert [w["text"] for w in words] == ["Agreed"]


@pytest.mark.unit
class TestConfigObjectPassedDirectly:
    """`config` may arrive as an already-validated `IDPConfig`, not a dict."""

    def test_an_idpconfig_instance_is_used_without_revalidation(self):
        """Callers that already hold a parsed config must not have it rebuilt.

        Re-running `IDPConfig(**config)` on a model instance would raise, so this
        branch is the difference between working and a hard failure at construction.
        """
        from idp_common.config.models import IDPConfig

        parsed = IDPConfig(
            **{
                "ocr": {
                    "backend": "none",
                    "max_workers": 7,
                    "image": {"dpi": 111, "target_width": 321, "target_height": 654},
                }
            }
        )
        service = make_service(config=parsed)

        assert service.config is parsed
        assert service.backend == "none"
        assert service.max_workers == 7
        assert service.dpi == 111
        assert service.resize_config == {
            "target_width": 321,
            "target_height": 654,
        }


@pytest.mark.unit
class TestAnalyzeDocumentDebugLogging:
    """`_analyze_document`'s block-type tally, which only runs under DEBUG."""

    def test_the_block_type_tally_is_only_computed_when_debug_is_enabled(self, caplog):
        """Counting blocks on every page at INFO would be pure overhead.

        Asserted in both directions because the guard is what keeps a per-page
        dictionary build out of the hot path.
        """
        service = make_service(
            config={"ocr": {"backend": "textract", "features": [{"name": "TABLES"}]}}
        )
        service.textract_client.analyze_document.return_value = textract_response()

        with caplog.at_level("DEBUG", logger=service_module.logger.name):
            service._analyze_document(b"bytes", 2)
        assert "block types" in caplog.text
        assert "'LINE': 2" in caplog.text

        caplog.clear()
        with caplog.at_level("INFO", logger=service_module.logger.name):
            service._analyze_document(b"bytes", 2)
        assert "block types" not in caplog.text


@pytest.mark.unit
class TestConvertedDocumentPageFailures:
    """A converted page that fails must not be silently dropped."""

    def test_a_failing_converted_page_fails_the_document_and_names_the_page(
        self, s3_writes
    ):
        """Every other page still lands, but the document must not report success.

        Silently dropping page 2 of a three-page spreadsheet gives extraction a
        document that is internally consistent and missing a third of its data.
        """
        document = Document(
            id="d1",
            input_bucket="in",
            input_key="book.xlsx",
            output_bucket="out",
            status=Status.OCR,
        )
        service = make_service(backend="none")
        body = MagicMock()
        body.read.return_value = b"PK\x03\x04xl/"
        service.s3_client.get_object.return_value = {"Body": body}
        service.document_converter = MagicMock()
        service.document_converter.convert_excel_to_pages.return_value = [
            (JPEG_BYTES, "sheet one"),
            (JPEG_BYTES, "sheet two"),
            (JPEG_BYTES, "sheet three"),
        ]

        original = service._process_converted_page

        def flaky(index, *args, **kwargs):
            if index == 1:
                raise RuntimeError("SlowDown")
            return original(index, *args, **kwargs)

        with patch.object(service, "_process_converted_page", side_effect=flaky):
            result = service.process_document(document)

        assert result.num_pages == 3
        assert sorted(result.pages) == ["1", "3"]
        assert result.status == Status.FAILED
        assert len(result.errors) == 1
        assert "page 2" in result.errors[0]
        assert "SlowDown" in result.errors[0]


@pytest.mark.unit
class TestMemorySamplerBody:
    """What the background sampler actually reports while the fan-out runs."""

    def test_normal_usage_is_logged_without_a_warning(self, caplog):
        """The sampler is the only in-flight signal before a Lambda OOM kill."""
        service = make_service(backend="none")
        fake_process = MagicMock()
        fake_process.memory_info.return_value = MagicMock(rss=200 * 1024 * 1024)

        import psutil

        with (
            caplog.at_level("INFO", logger=service_module.logger.name),
            patch.object(psutil, "Process", return_value=fake_process),
        ):
            shutdown = service._start_memory_monitoring()
            for _ in range(200):
                if "Memory usage" in caplog.text:
                    break
                time_module.sleep(0.01)
            shutdown.set()

        assert "Memory usage: 200.0 MB" in caplog.text
        assert "HIGH memory usage" not in caplog.text

    def test_high_usage_is_escalated_to_a_warning(self, caplog):
        """3500 MB is the threshold; below it there is no warning at all."""
        service = make_service(backend="none")
        fake_process = MagicMock()
        fake_process.memory_info.return_value = MagicMock(rss=3600 * 1024 * 1024)

        import psutil

        with (
            caplog.at_level("INFO", logger=service_module.logger.name),
            patch.object(psutil, "Process", return_value=fake_process),
        ):
            shutdown = service._start_memory_monitoring()
            for _ in range(200):
                if "HIGH memory usage" in caplog.text:
                    break
                time_module.sleep(0.01)
            shutdown.set()

        assert "HIGH memory usage detected: 3600.0 MB" in caplog.text

    def test_a_missing_psutil_stops_the_sampler_instead_of_spinning(self, caplog):
        """`psutil` is not a hard dependency, and the loop must not retry forever.

        The `break` is what stops a 5-second-interval loop becoming a tight one:
        `continue` here would spin a CPU for the whole OCR run.
        """
        service = make_service(backend="none")
        with (
            caplog.at_level("DEBUG", logger=service_module.logger.name),
            patch.dict("sys.modules", {"psutil": None}),
        ):
            shutdown = service._start_memory_monitoring()
            for _ in range(200):
                if "psutil not available" in caplog.text:
                    break
                time_module.sleep(0.01)

        assert "psutil not available" in caplog.text
        assert not shutdown.is_set(), (
            "the sampler should exit on its own without the event being set"
        )

    def test_an_unexpected_sampling_error_does_not_kill_the_sampler(self, caplog):
        """Only `ImportError` is a permanent condition; anything else may be transient.

        `psutil.Process` raises `NoSuchProcess` and `AccessDenied` in containers.
        Breaking out on those would silence the OOM warning for the rest of the
        run, so the distinction from the `ImportError` path above is deliberate:
        the thread must still be alive afterwards.
        """
        service = make_service(backend="none")

        import psutil

        with (
            caplog.at_level("DEBUG", logger=service_module.logger.name),
            patch.object(
                psutil, "Process", side_effect=RuntimeError("no such process")
            ),
        ):
            shutdown = service._start_memory_monitoring()
            for _ in range(200):
                if "Error monitoring memory" in caplog.text:
                    break
                time_module.sleep(0.01)
            still_running = any(
                thread.is_alive()
                and thread.daemon
                and thread.name.startswith("Thread-")
                for thread in __import__("threading").enumerate()
            )
            shutdown.set()

        assert "no such process" in caplog.text
        assert "psutil not available" not in caplog.text
        assert still_running, "the sampler should wait and retry, not exit"
