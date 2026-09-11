#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Local test script for the Chandra OCR Lambda Hook.

This script tests the Chandra OCR integration by:
1. Converting PDF pages to JPEG images
2. Submitting each page to the Datalab convert API
3. Polling for completion
4. Displaying the OCR results

The Datalab API is asynchronous:
- POST /api/v1/convert (multipart form) → returns request_check_url
- GET request_check_url → poll until status is "complete"

Prerequisites:
  pip install pdf2image Pillow

  # pdf2image requires poppler:
  # macOS: brew install poppler
  # Ubuntu: sudo apt-get install poppler-utils
  # Amazon Linux: sudo yum install poppler-utils

Usage:
  # Set your Datalab API key
  export CHANDRA_API_KEY="your-api-key-here"

  # OCR a PDF file
  python test_local.py ../../insurance_package.pdf

  # OCR with specific output format
  export OUTPUT_FORMAT=json
  python test_local.py ../../insurance_package.pdf

  # OCR specific pages only
  python test_local.py ../../insurance_package.pdf --pages 1,2,3

  # Use accurate mode (default) vs fast mode
  export CONVERSION_MODE=fast
  python test_local.py ../../insurance_package.pdf
"""

import argparse
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
CHANDRA_API_KEY = os.environ.get("CHANDRA_API_KEY", "")
CHANDRA_API_URL = os.environ.get("CHANDRA_API_URL", "https://www.datalab.to")
OUTPUT_FORMAT = os.environ.get("OUTPUT_FORMAT", "markdown")
CONVERSION_MODE = os.environ.get("CONVERSION_MODE", "accurate")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "3"))
MAX_POLL_ATTEMPTS = int(os.environ.get("MAX_POLL_ATTEMPTS", "60"))


def pdf_to_images(pdf_path: str, pages: list[int] | None = None) -> list[bytes]:
    """Convert PDF pages to JPEG image bytes."""
    try:
        from pdf2image import convert_from_path
    except ImportError:
        print("ERROR: pdf2image is required. Install with: pip install pdf2image")
        print("       Also install poppler: brew install poppler (macOS)")
        sys.exit(1)

    print(f"Converting PDF to images: {pdf_path}")
    pil_images = convert_from_path(pdf_path, dpi=150)

    if pages:
        pil_images = [pil_images[p - 1] for p in pages if p <= len(pil_images)]

    image_bytes_list = []
    for i, img in enumerate(pil_images):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        image_bytes_list.append(buf.getvalue())
        print(
            f"  Page {i + 1}: {img.width}x{img.height} -> {len(buf.getvalue())} bytes"
        )

    return image_bytes_list


def submit_conversion(image_bytes: bytes) -> str:
    """Submit an image to the Datalab convert API and return the check URL."""
    if not CHANDRA_API_KEY:
        print("ERROR: Set CHANDRA_API_KEY environment variable.")
        print("       Get your API key from https://www.datalab.to")
        sys.exit(1)

    boundary = uuid.uuid4().hex
    body = b""

    # File field
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="file"; filename="page.jpg"\r\n'
    body += b"Content-Type: image/jpeg\r\n\r\n"
    body += image_bytes
    body += b"\r\n"

    # Output format
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="output_format"\r\n\r\n'
    body += f"{OUTPUT_FORMAT}\r\n".encode()

    # Mode
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="mode"\r\n\r\n'
    body += f"{CONVERSION_MODE}\r\n".encode()

    # End
    body += f"--{boundary}--\r\n".encode()

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

    with urllib.request.urlopen(req, timeout=60) as response:  # nosec B310 - local test harness against the configured https endpoint
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("success"):
        print(f"  ERROR: Submission failed: {result.get('error', 'Unknown')}")
        sys.exit(1)

    return result["request_check_url"]


def poll_for_result(check_url: str) -> dict:
    """Poll the Datalab API until conversion is complete."""
    for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
        req = urllib.request.Request(
            check_url,
            headers={
                "X-Api-Key": CHANDRA_API_KEY,
                "User-Agent": "GENAIIDP-chandra-ocr-hook/1.0",
            },
        )

        with urllib.request.urlopen(req, timeout=30) as response:  # nosec B310 - local test harness against the configured https endpoint
            result = json.loads(response.read().decode("utf-8"))

        status = result.get("status", "unknown")
        if status == "complete":
            return result
        elif status == "error":
            print(f"  ERROR: Conversion failed: {result.get('error', 'Unknown')}")
            sys.exit(1)

        print(f"    Poll {attempt}: {status}...", end="\r")
        time.sleep(POLL_INTERVAL)

    print(f"\n  ERROR: Timed out after {MAX_POLL_ATTEMPTS * POLL_INTERVAL}s")
    sys.exit(1)


def ocr_page(image_bytes: bytes, page_num: int) -> tuple[str, float]:
    """OCR a single page image. Returns (text, elapsed_seconds)."""
    print(f"  Submitting to Datalab API ({len(image_bytes)} bytes)...")
    start = time.time()

    check_url = submit_conversion(image_bytes)
    result = poll_for_result(check_url)
    elapsed = time.time() - start

    # Extract content based on output format
    text = (
        result.get(OUTPUT_FORMAT)
        or result.get("markdown")
        or result.get("html")
        or result.get("json")
        or "(no content)"
    )
    if isinstance(text, dict):
        text = json.dumps(text, indent=2)

    print(f"  Completed in {elapsed:.1f}s ({len(text)} chars)   ")
    return text, elapsed


def main():
    parser = argparse.ArgumentParser(
        description="Test Chandra OCR on a PDF document",
        epilog="Set CHANDRA_API_KEY environment variable before running.",
    )
    parser.add_argument("pdf_path", help="Path to PDF file to OCR")
    parser.add_argument(
        "--pages",
        help="Comma-separated page numbers to OCR (default: all)",
        default=None,
    )
    parser.add_argument(
        "--output",
        help="Output file to write results (default: stdout)",
        default=None,
    )
    args = parser.parse_args()

    if not os.path.exists(args.pdf_path):
        print(f"ERROR: File not found: {args.pdf_path}")
        sys.exit(1)

    pages = None
    if args.pages:
        pages = [int(p.strip()) for p in args.pages.split(",")]

    # Convert PDF to images
    image_bytes_list = pdf_to_images(args.pdf_path, pages)
    print(f"\nProcessing {len(image_bytes_list)} page(s) with Datalab Chandra OCR")
    print(f"API: {CHANDRA_API_URL}")
    print(f"Output format: {OUTPUT_FORMAT}")
    print(f"Conversion mode: {CONVERSION_MODE}")
    print("=" * 70)

    all_results = []
    total_start = time.time()

    for i, img_bytes in enumerate(image_bytes_list):
        page_num = pages[i] if pages else i + 1
        print(f"\n--- Page {page_num} ---")

        text, elapsed = ocr_page(img_bytes, page_num)
        print()
        print(text)
        print()

        all_results.append({"page": page_num, "text": text, "elapsed": elapsed})

    total_elapsed = time.time() - total_start
    print("=" * 70)
    print(f"Total: {len(image_bytes_list)} pages in {total_elapsed:.1f}s")

    # Write to output file if requested
    if args.output:
        with open(args.output, "w") as f:
            if args.output.endswith(".json"):
                json.dump(all_results, f, indent=2)
            else:
                for r in all_results:
                    f.write(f"--- Page {r['page']} ---\n")
                    f.write(r["text"])
                    f.write("\n\n")
        print(f"\nResults written to {args.output}")


if __name__ == "__main__":
    main()
