# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
GENAIIDP-mistral-ocr-hook: Lambda Hook that calls the hosted Mistral OCR API.

This Lambda function receives a Converse API-compatible payload from the
GenAI IDP Accelerator's LambdaHook feature and forwards each page image to the
hosted Mistral OCR API (https://api.mistral.ai/v1/ocr) for high-quality OCR.

Mistral OCR 4 ("mistral-ocr-latest") is a document-understanding model that
returns markdown-structured text together with paragraph-level bounding boxes
("blocks"), per-word confidence scores, and per-page usage information. This
hook is fully serverless (HTTPS API + API key) — no SageMaker endpoint or
GPU instance is required.

What makes this hook special vs. a plain text OCR hook:

1. It requests structured output from Mistral OCR
   (``confidence_scores_granularity="word"`` and ``include_blocks=true``).
2. It translates Mistral's response into **Amazon Textract response format**
   (a ``Blocks`` list with ``LINE`` and ``WORD`` blocks carrying ``Confidence``
   and ``Geometry.BoundingBox``) and returns it under a top-level
   ``textractBlocks`` key.
3. The IDP OCR service detects ``textractBlocks`` and persists it as the page's
   ``rawText.json`` + ``textConfidence.json``, so the per-word confidence and
   geometry flow downstream into Assessment (``{OCR_TEXT_CONFIDENCE}``) and the
   UI's bounding-box highlighting — exactly like the native Textract backend.
4. It reports per-page metering (``pages``) so cost tracking works.

The function:
1. Downloads page images from S3 (sent as S3 references by the accelerator).
2. Submits each image to the Mistral OCR API as a base64 data URI.
3. Translates the response to Textract-format blocks.
4. Maps everything back to a Converse API-compatible response for the pipeline.

Environment variables:
  MISTRAL_API_KEY     - (Required) Mistral API key (Bearer token).
  MISTRAL_API_URL     - Mistral OCR endpoint (default:
                        https://api.mistral.ai/v1/ocr)
  MISTRAL_OCR_MODEL   - OCR model id (default: mistral-ocr-latest)
  INCLUDE_BLOCKS      - "true"/"false" — request paragraph bounding boxes
                        (default: true)
  CONFIDENCE_GRANULARITY - "word" or "page" (default: word)
  REQUEST_TIMEOUT     - Per-request timeout in seconds (default: 120)
  LOG_LEVEL           - Logging level (default: INFO)
"""

import base64
import json
import logging
import os
import urllib.request

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Initialize AWS clients
s3_client = boto3.client("s3")

# Configuration from environment variables
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "")
MISTRAL_API_URL = os.environ.get("MISTRAL_API_URL", "https://api.mistral.ai/v1/ocr")
MISTRAL_OCR_MODEL = os.environ.get("MISTRAL_OCR_MODEL", "mistral-ocr-latest")
INCLUDE_BLOCKS = os.environ.get("INCLUDE_BLOCKS", "true").lower() == "true"
CONFIDENCE_GRANULARITY = os.environ.get("CONFIDENCE_GRANULARITY", "word")
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "120"))

# Image format to MIME type mapping (for building the data URI)
IMAGE_MIME_TYPES = {
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "tiff": "image/tiff",
    "tif": "image/tiff",
}


def download_image_from_s3(s3_uri: str) -> bytes:
    """Download image bytes from an S3 URI."""
    parts = s3_uri.replace("s3://", "").split("/", 1)
    bucket = parts[0]
    key = parts[1]
    response = s3_client.get_object(Bucket=bucket, Key=key)
    return response["Body"].read()


def extract_images_from_messages(messages: list) -> list[dict]:
    """
    Extract images from Converse API messages.

    Args:
        messages: List of Converse API message objects

    Returns:
        List of dicts with 'bytes' and 'format' keys
    """
    images = []
    for message in messages:
        for item in message.get("content", []):
            if "image" not in item:
                continue
            source = item["image"].get("source", {})
            img_format = item["image"].get("format", "jpeg")
            if "s3Location" in source:
                s3_uri = source["s3Location"]["uri"]
                try:
                    img_bytes = download_image_from_s3(s3_uri)
                    images.append({"bytes": img_bytes, "format": img_format})
                    logger.info(
                        f"Downloaded image from S3: {s3_uri} ({len(img_bytes)} bytes)"
                    )
                except Exception as e:
                    logger.error(f"Failed to download image from {s3_uri}: {e}")
            elif "bytes" in source:
                images.append({"bytes": source["bytes"], "format": img_format})
    return images


def call_mistral_ocr(image_bytes: bytes, image_format: str) -> dict:
    """
    Submit a single image to the hosted Mistral OCR API.

    Args:
        image_bytes: Raw image bytes
        image_format: Image format string (e.g. 'jpeg', 'png')

    Returns:
        The parsed JSON OCR response.

    Raises:
        ValueError: If the API key is not configured.
    """
    if not MISTRAL_API_KEY:
        raise ValueError(
            "MISTRAL_API_KEY environment variable is required. "
            "Get your API key from https://console.mistral.ai"
        )

    mime_type = IMAGE_MIME_TYPES.get(image_format.lower(), "image/jpeg")
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    data_uri = f"data:{mime_type};base64,{b64}"

    payload = {
        "model": MISTRAL_OCR_MODEL,
        "document": {"type": "image_url", "image_url": data_uri},
        "include_blocks": INCLUDE_BLOCKS,
        "confidence_scores_granularity": CONFIDENCE_GRANULARITY,
    }

    req = urllib.request.Request(
        MISTRAL_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {MISTRAL_API_KEY}",
            "User-Agent": "GENAIIDP-mistral-ocr-hook/1.0",
        },
        method="POST",
    )

    logger.info(
        f"Submitting image to Mistral OCR ({len(image_bytes)} bytes, "
        f"model={MISTRAL_OCR_MODEL})"
    )

    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:  # nosec B310 - MISTRAL_API_URL env var (https default), not request input
        return json.loads(response.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Mistral OCR response -> Amazon Textract response format
# ---------------------------------------------------------------------------
#
# Textract geometry uses a normalized 0-1 BoundingBox {Left, Top, Width,
# Height}. Mistral returns paragraph "blocks" with pixel coordinates
# (top_left_x/top_left_y/bottom_right_x/bottom_right_y) plus the page
# "dimensions" (width/height), so we normalize against the page size.
#
# The IDP OCR service's _generate_text_confidence_data() builds the assessment
# text-confidence table from LINE blocks (one table row per LINE: Text +
# Confidence), and the UI reads Geometry for highlighting. To get a meaningful
# confidence on EVERY line, we emit one LINE block per *physical line* of the
# page markdown and compute that line's confidence from the per-word scores
# that fall on it.
#
# Mistral gives confidence two ways: a single page-level average, and a
# per-word array where each entry has a `start_index` into the page markdown
# (verified: every word's text sits at markdown[start_index:...]). It does NOT
# give a per-block/per-line confidence. So we map words to lines by start_index
# and average them per line. A Mistral "block" is often a multi-line paragraph,
# so emitting one LINE per block (with only the page average) would leave most
# rendered rows blank and uniform — hence the per-physical-line approach. Block
# bounding boxes are still used to attach geometry to the lines they contain.
# WORD blocks are also emitted (with real per-word confidence) for completeness.


def _bbox_to_geometry(
    block: dict, page_width: float, page_height: float
) -> dict | None:
    """Convert a Mistral pixel bbox to a Textract normalized Geometry dict."""
    try:
        left_px = float(block["top_left_x"])
        top_px = float(block["top_left_y"])
        right_px = float(block["bottom_right_x"])
        bottom_px = float(block["bottom_right_y"])
    except (KeyError, TypeError, ValueError):
        return None

    if not page_width or not page_height:
        return None

    left = max(0.0, min(1.0, left_px / page_width))
    top = max(0.0, min(1.0, top_px / page_height))
    width = max(0.0, min(1.0, (right_px - left_px) / page_width))
    height = max(0.0, min(1.0, (bottom_px - top_px) / page_height))

    return {
        "BoundingBox": {
            "Width": width,
            "Height": height,
            "Left": left,
            "Top": top,
        },
        "Polygon": [
            {"X": left, "Y": top},
            {"X": left + width, "Y": top},
            {"X": left + width, "Y": top + height},
            {"X": left, "Y": top + height},
        ],
    }


def _normalize_word_entries(confidence_scores: dict) -> list[dict]:
    """
    Normalize Mistral word_confidence_scores into a list of
    {text, confidence(0-100), start_index?, bbox?} dicts.

    The exact shape of word_confidence_scores is not strictly documented, so we
    handle a few plausible shapes defensively:
      - [{"text"|"word": str, "confidence"|"score": float,
          "start_index"?: int, "bbox"?: {...}}]  (the observed hosted-API shape)
      - {"<word>": <confidence>, ...}
    Confidence values in 0-1 are scaled to 0-100 to match Textract.
    """
    raw = confidence_scores.get("word_confidence_scores")
    entries: list[dict] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            text = item.get("text") or item.get("word") or item.get("content") or ""
            conf = item.get("confidence", item.get("score"))
            start_index = item.get("start_index")
            entries.append(
                {
                    "text": str(text),
                    "confidence": conf,
                    "start_index": start_index
                    if isinstance(start_index, int)
                    else None,
                    "bbox": item,
                }
            )
    elif isinstance(raw, dict):
        for text, conf in raw.items():
            entries.append(
                {
                    "text": str(text),
                    "confidence": conf,
                    "start_index": None,
                    "bbox": None,
                }
            )

    # Scale 0-1 confidences to 0-100
    for e in entries:
        c = e["confidence"]
        try:
            c = float(c)
        except (TypeError, ValueError):
            c = None
        if c is not None and 0.0 <= c <= 1.0:
            c = c * 100.0
        e["confidence"] = c
    return entries


def _line_spans(markdown: str) -> list[tuple[int, int, str]]:
    """
    Split page markdown into physical lines, returning (start, end, text) for
    each non-empty line. `start`/`end` are character offsets into `markdown`
    (the same coordinate space as a word's `start_index`), so words can be
    assigned to the line whose span contains their start_index.
    """
    spans: list[tuple[int, int, str]] = []
    offset = 0
    for raw_line in markdown.split("\n"):
        start = offset
        end = offset + len(raw_line)
        if raw_line.strip():
            spans.append((start, end, raw_line))
        offset = end + 1  # +1 for the consumed "\n"
    return spans


def _line_confidence(
    line_start: int, line_end: int, word_entries: list[dict]
) -> float | None:
    """
    Average the confidence of words whose start_index falls within
    [line_start, line_end). Returns None if no per-word data maps to the line.
    """
    confs = [
        e["confidence"]
        for e in word_entries
        if e.get("start_index") is not None
        and e["confidence"] is not None
        and line_start <= e["start_index"] < line_end
    ]
    if not confs:
        return None
    return sum(confs) / len(confs)


def mistral_page_to_textract(page: dict) -> tuple[str, list[dict]]:
    """
    Convert a single Mistral OCR page object into (markdown_text, textract_blocks).

    Returns:
        Tuple of (page markdown text, list of Textract-format Block dicts).
    """
    markdown = page.get("markdown", "") or ""
    dimensions = page.get("dimensions") or {}
    page_width = float(dimensions.get("width") or 0) or None
    page_height = float(dimensions.get("height") or 0) or None

    confidence_scores = page.get("confidence_scores") or {}
    word_entries = _normalize_word_entries(confidence_scores)

    # Page-average confidence (0-100); fallback for lines with no per-word data.
    page_avg = confidence_scores.get("average_page_confidence_score")
    try:
        page_avg = float(page_avg)
        if 0.0 <= page_avg <= 1.0:
            page_avg = page_avg * 100.0
    except (TypeError, ValueError):
        page_avg = None

    blocks: list[dict] = []
    block_id = 0

    def next_id() -> str:
        nonlocal block_id
        block_id += 1
        return f"mistral-{page.get('index', 0)}-{block_id}"

    # PAGE block
    page_block = {"BlockType": "PAGE", "Id": next_id()}
    if page_width and page_height:
        page_block["Geometry"] = {
            "BoundingBox": {"Width": 1.0, "Height": 1.0, "Left": 0.0, "Top": 0.0},
            "Polygon": [
                {"X": 0.0, "Y": 0.0},
                {"X": 1.0, "Y": 0.0},
                {"X": 1.0, "Y": 1.0},
                {"X": 0.0, "Y": 1.0},
            ],
        }
    blocks.append(page_block)

    # Pre-compute geometry per Mistral block so we can attach it to the lines a
    # block covers (a block's content may span several physical lines).
    block_geometries = [
        (
            mblock.get("content") or "",
            _bbox_to_geometry(mblock, page_width, page_height),
        )
        for mblock in page.get("blocks") or []
    ]

    def geometry_for_line(text: str):
        """Best-effort geometry: the first block whose content contains the line."""
        stripped = text.strip()
        if not stripped:
            return None
        for content, geom in block_geometries:
            if geom and stripped in content:
                return geom
        return None

    # LINE blocks: one per physical line of the page markdown, each with a real
    # per-line confidence derived from the per-word scores on that line.
    for line_start, line_end, raw_line in _line_spans(markdown):
        text = raw_line.strip()
        if not text:
            continue
        line = {"BlockType": "LINE", "Id": next_id(), "Text": text}
        conf = _line_confidence(line_start, line_end, word_entries)
        if conf is None:
            conf = page_avg
        if conf is not None:
            line["Confidence"] = round(conf, 1)
        geom = geometry_for_line(text)
        if geom:
            line["Geometry"] = geom
        blocks.append(line)

    # WORD blocks from per-word confidence scores (real per-word confidence)
    for entry in word_entries:
        text = entry["text"].strip()
        if not text:
            continue
        word = {"BlockType": "WORD", "Id": next_id(), "Text": text}
        if entry["confidence"] is not None:
            word["Confidence"] = round(float(entry["confidence"]), 1)
        blocks.append(word)

    return markdown, blocks


def build_textract_response(ocr_response: dict) -> tuple[str, dict, int]:
    """
    Build a Textract-format response from a full Mistral OCR API response.

    Returns:
        Tuple of (combined markdown text, textract-format dict with "Blocks"
        and "DocumentMetadata", pages_processed count).
    """
    pages = ocr_response.get("pages") or []
    all_text: list[str] = []
    all_blocks: list[dict] = []

    for page in pages:
        markdown, blocks = mistral_page_to_textract(page)
        all_text.append(markdown)
        all_blocks.extend(blocks)

    usage_info = ocr_response.get("usage_info") or {}
    pages_processed = int(usage_info.get("pages_processed") or len(pages) or 0)

    textract_response = {
        "DocumentMetadata": {"Pages": pages_processed or len(pages)},
        "Blocks": all_blocks,
        # Preserve the Mistral model id for traceability
        "ModelId": ocr_response.get("model", MISTRAL_OCR_MODEL),
    }
    return "\n\n".join(t for t in all_text if t), textract_response, pages_processed


def lambda_handler(event, context):
    """
    Lambda handler that proxies LambdaHook payloads to the Mistral OCR API.

    Expected event format (Converse API-compatible):
    {
        "modelId": "LambdaHook",
        "messages": [{"role": "user", "content": [...]}],
        "system": [{"text": "..."}],
        "inferenceConfig": {"temperature": 0.0, ...},
        "context": "OCR"
    }

    Returns a Converse API-compatible response, augmented with a top-level
    ``textractBlocks`` object (Amazon Textract response format) so the IDP OCR
    service can persist per-word confidence + bounding-box geometry:
    {
        "output": {"message": {"role": "assistant", "content": [{"text": "..."}]}},
        "textractBlocks": {"DocumentMetadata": {...}, "Blocks": [...]},
        "usage": {"pages": N, "inputTokens": 0, "outputTokens": 0, "totalTokens": 0}
    }
    """
    idp_context = event.get("context", "unknown")
    logger.info(f"Received LambdaHook request. Context: {idp_context}")

    messages = event.get("messages", [])
    images = extract_images_from_messages(messages)

    if not images:
        logger.warning("No images found in the payload. Mistral OCR requires images.")
        return {
            "output": {"message": {"role": "assistant", "content": [{"text": ""}]}},
            "usage": {
                "pages": 0,
                "inputTokens": 0,
                "outputTokens": 0,
                "totalTokens": 0,
            },
        }

    logger.info(
        f"Processing {len(images)} image(s) with Mistral OCR "
        f"(model={MISTRAL_OCR_MODEL}, include_blocks={INCLUDE_BLOCKS}, "
        f"confidence={CONFIDENCE_GRANULARITY})"
    )

    all_text: list[str] = []
    all_blocks: list[dict] = []
    total_pages = 0

    for i, img in enumerate(images):
        logger.info(f"OCR image {i + 1}/{len(images)}...")
        ocr_response = call_mistral_ocr(img["bytes"], img["format"])
        text, textract_response, pages_processed = build_textract_response(ocr_response)
        all_text.append(text)
        all_blocks.extend(textract_response["Blocks"])
        total_pages += pages_processed or 1

    combined_text = "\n\n".join(t for t in all_text if t)

    textract_blocks = {
        "DocumentMetadata": {"Pages": total_pages},
        "Blocks": all_blocks,
        "ModelId": MISTRAL_OCR_MODEL,
    }

    logger.info(
        f"Mistral OCR complete. Output: {len(combined_text)} chars, "
        f"{len(all_blocks)} blocks, {total_pages} page(s)."
    )

    # Return Converse API-compatible response augmented with Textract blocks.
    # "usage.pages" enables per-page cost metering — add a pricing entry keyed
    # on the function name (e.g. "GENAIIDP-mistral-ocr-hook") with unit "pages".
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": combined_text}],
            }
        },
        "textractBlocks": textract_blocks,
        "usage": {
            "pages": total_pages,
            "inputTokens": 0,
            "outputTokens": 0,
            "totalTokens": 0,
        },
    }
