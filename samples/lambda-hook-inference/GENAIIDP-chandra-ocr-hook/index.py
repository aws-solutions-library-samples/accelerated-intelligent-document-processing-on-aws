# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
GENAIIDP-chandra-ocr-hook: Lambda Hook that calls the Datalab Chandra OCR API.

This Lambda function receives a Converse API-compatible payload from the
GenAI IDP Accelerator's LambdaHook feature and forwards it to the Datalab
hosted API (https://www.datalab.to) for high-quality OCR using Chandra OCR 2.

Chandra OCR 2 is a state-of-the-art VLM-based OCR model that converts
images into structured HTML, Markdown, or JSON while preserving layout.
It supports 90+ languages, math, tables, forms, handwriting, and more.

The Datalab API is asynchronous:
1. POST /api/v1/convert with the image file → returns a request_check_url
2. Poll GET request_check_url until status is "complete"
3. Extract the markdown/json/html result

The function:
1. Downloads page images from S3 (sent as S3 references by the accelerator)
2. Submits each image to the Datalab convert API
3. Polls for completion
4. Maps the response back to Converse API format for the IDP pipeline

Environment variables:
  CHANDRA_API_KEY     - (Required) Datalab API key for authentication
  CHANDRA_API_URL     - Datalab API base URL (default: https://www.datalab.to)
  OUTPUT_FORMAT       - Output format: markdown, json, or html (default: markdown)
  CONVERSION_MODE     - Conversion quality: fast, balanced, or accurate (default: accurate)
  POLL_INTERVAL       - Seconds between polling attempts (default: 3)
  MAX_POLL_ATTEMPTS   - Maximum number of polling attempts (default: 60)
  LOG_LEVEL           - Logging level (default: INFO)
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Initialize AWS clients
s3_client = boto3.client("s3")

# Configuration from environment variables
CHANDRA_API_KEY = os.environ.get("CHANDRA_API_KEY", "")
CHANDRA_API_URL = os.environ.get("CHANDRA_API_URL", "https://www.datalab.to")
OUTPUT_FORMAT = os.environ.get("OUTPUT_FORMAT", "markdown")
CONVERSION_MODE = os.environ.get("CONVERSION_MODE", "accurate")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "3"))
MAX_POLL_ATTEMPTS = int(os.environ.get("MAX_POLL_ATTEMPTS", "60"))

# Image format to MIME type mapping
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


def build_multipart_body(
    image_bytes: bytes, image_format: str, output_format: str, mode: str
) -> tuple[bytes, str]:
    """
    Build a multipart/form-data request body for the Datalab convert API.

    Args:
        image_bytes: Raw image bytes
        image_format: Image format (e.g. 'jpeg', 'png')
        output_format: Desired output format ('markdown', 'json', 'html')
        mode: Conversion mode ('fast', 'balanced', 'accurate')

    Returns:
        Tuple of (body_bytes, boundary_string)
    """
    boundary = uuid.uuid4().hex
    mime_type = IMAGE_MIME_TYPES.get(image_format.lower(), "image/jpeg")
    ext = "jpg" if image_format.lower() in ("jpeg", "jpg") else image_format.lower()

    body = b""

    # File field
    body += f"--{boundary}\r\n".encode()
    body += (
        f'Content-Disposition: form-data; name="file"; filename="page.{ext}"\r\n'
    ).encode()
    body += f"Content-Type: {mime_type}\r\n\r\n".encode()
    body += image_bytes
    body += b"\r\n"

    # Output format field
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="output_format"\r\n\r\n'
    body += f"{output_format}\r\n".encode()

    # Mode field
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="mode"\r\n\r\n'
    body += f"{mode}\r\n".encode()

    # End boundary
    body += f"--{boundary}--\r\n".encode()

    return body, boundary


def submit_conversion(image_bytes: bytes, image_format: str) -> str:
    """
    Submit an image to the Datalab convert API.

    Args:
        image_bytes: Raw image bytes
        image_format: Image format string

    Returns:
        The request_check_url for polling

    Raises:
        ValueError: If API key is not configured
    """
    if not CHANDRA_API_KEY:
        raise ValueError(
            "CHANDRA_API_KEY environment variable is required. "
            "Get your API key from https://www.datalab.to"
        )

    body, boundary = build_multipart_body(
        image_bytes, image_format, OUTPUT_FORMAT, CONVERSION_MODE
    )

    convert_url = f"{CHANDRA_API_URL.rstrip('/')}/api/v1/convert"
    req = urllib.request.Request(
        convert_url,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "X-Api-Key": CHANDRA_API_KEY,
            "User-Agent": "GENAIIDP-chandra-ocr-hook/1.0",
        },
        method="POST",
    )

    logger.info(f"Submitting image to {convert_url} ({len(image_bytes)} bytes)")

    with urllib.request.urlopen(req, timeout=60) as response:  # nosec B310 - CHANDRA_API_URL env var (https default), not request input
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("success"):
        error_msg = result.get("error", "Unknown error")
        raise RuntimeError(f"Datalab API submission failed: {error_msg}")

    check_url = result["request_check_url"]
    request_id = result.get("request_id", "unknown")
    logger.info(f"Conversion submitted. Request ID: {request_id}")
    return check_url


def poll_for_result(check_url: str) -> dict:
    """
    Poll the Datalab API until the conversion is complete.

    Args:
        check_url: The request_check_url returned from submit_conversion

    Returns:
        The complete API response with 'markdown', 'json', or 'html' field

    Raises:
        TimeoutError: If polling exceeds MAX_POLL_ATTEMPTS
        RuntimeError: If the conversion fails
    """
    for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
        req = urllib.request.Request(
            check_url,
            headers={
                "X-Api-Key": CHANDRA_API_KEY,
                "User-Agent": "GENAIIDP-chandra-ocr-hook/1.0",
            },
        )

        with urllib.request.urlopen(req, timeout=30) as response:  # nosec B310 - CHANDRA_API_URL env var (https default), not request input
            result = json.loads(response.read().decode("utf-8"))

        status = result.get("status", "unknown")
        logger.info(f"Poll attempt {attempt}/{MAX_POLL_ATTEMPTS}: status={status}")

        if status == "complete":
            return result
        elif status == "error":
            error_msg = result.get("error", "Unknown conversion error")
            raise RuntimeError(f"Datalab conversion failed: {error_msg}")

        time.sleep(POLL_INTERVAL)

    raise TimeoutError(
        f"Conversion did not complete after {MAX_POLL_ATTEMPTS} "
        f"polling attempts ({MAX_POLL_ATTEMPTS * POLL_INTERVAL}s)"
    )


def extract_content_from_messages(messages: list) -> tuple[str, list[dict]]:
    """
    Extract text and images from Converse API messages.

    Args:
        messages: List of Converse API message objects

    Returns:
        Tuple of (combined_text, list_of_image_dicts)
    """
    texts = []
    images = []

    for message in messages:
        for item in message.get("content", []):
            if "text" in item:
                texts.append(item["text"])
            elif "image" in item:
                source = item["image"].get("source", {})
                img_format = item["image"].get("format", "jpeg")
                if "s3Location" in source:
                    s3_uri = source["s3Location"]["uri"]
                    try:
                        img_bytes = download_image_from_s3(s3_uri)
                        images.append({"bytes": img_bytes, "format": img_format})
                        logger.info(
                            f"Downloaded image from S3: {s3_uri} "
                            f"({len(img_bytes)} bytes)"
                        )
                    except Exception as e:
                        logger.error(f"Failed to download image from {s3_uri}: {e}")
                elif "bytes" in source:
                    images.append({"bytes": source["bytes"], "format": img_format})

    return "\n".join(texts), images


def convert_image(image_bytes: bytes, image_format: str) -> str:
    """
    Convert a single image using the Datalab API (submit + poll).

    Args:
        image_bytes: Raw image bytes
        image_format: Image format string

    Returns:
        OCR result text (markdown, json, or html)
    """
    check_url = submit_conversion(image_bytes, image_format)
    result = poll_for_result(check_url)

    # Extract content based on output format
    content = (
        result.get(OUTPUT_FORMAT)
        or result.get("markdown")
        or result.get("html")
        or result.get("json")
        or ""
    )

    if isinstance(content, dict):
        content = json.dumps(content, indent=2)

    return content


def lambda_handler(event, context):
    """
    Lambda handler that proxies LambdaHook payloads to the Datalab Chandra OCR API.

    Expected event format (Converse API-compatible):
    {
        "modelId": "LambdaHook",
        "messages": [{"role": "user", "content": [...]}],
        "system": [{"text": "..."}],
        "inferenceConfig": {"temperature": 0.0, ...},
        "context": "OCR"
    }

    Returns Converse API-compatible response:
    {
        "output": {"message": {"role": "assistant", "content": [{"text": "..."}]}},
        "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}
    }
    """
    idp_context = event.get("context", "unknown")
    logger.info(f"Received LambdaHook request. Context: {idp_context}")

    # Extract user content (text + images)
    messages = event.get("messages", [])
    user_text, images = extract_content_from_messages(messages)

    if not images:
        logger.warning("No images found in the payload. Chandra OCR requires images.")
        return {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [{"text": ""}],
                }
            },
            "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
        }

    logger.info(
        f"Processing {len(images)} image(s) with Datalab Chandra OCR "
        f"(format: {OUTPUT_FORMAT}, mode: {CONVERSION_MODE})"
    )

    # Process each image — for OCR, typically one image per Lambda invocation
    all_results = []
    for i, img in enumerate(images):
        logger.info(f"Converting image {i + 1}/{len(images)}...")
        result_text = convert_image(img["bytes"], img["format"])
        all_results.append(result_text)

    # Combine results if multiple images
    combined_text = "\n\n".join(all_results)

    logger.info(
        f"Chandra OCR complete. Output length: {len(combined_text)} chars "
        f"from {len(images)} image(s)"
    )

    # Return Converse API-compatible response
    # Note: The Datalab API does not return token counts
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": combined_text}],
            }
        },
        "usage": {
            "inputTokens": 0,
            "outputTokens": 0,
            "totalTokens": 0,
        },
    }
