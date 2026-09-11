#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
End-to-end test for the DEPLOYED Cohere Parse Lambda hook.

This script exercises the real Lambda exactly as the IDP OCR service would:
1. Uploads a page image to the IDP working bucket under temp/lambdahook/
   (the only prefix the hook is granted s3:GetObject on).
2. Invokes the deployed Lambda with a Converse API-compatible OCR event that
   references the image by S3 URI (the same payload shape the accelerator's
   bedrock LambdaHook path sends).
3. Validates the Converse-format response AND the structured "textractBlocks"
   payload (PAGE/LINE blocks, 0-1 geometry, and the ABSENCE of Confidence —
   Cohere Parse returns no confidence scores) plus "usage.pages" metering.
4. Cleans up the uploaded test image.

No secrets are read or written here — the Cohere API key lives only in the
deployed Lambda's environment. AWS credentials come from your environment /
profile (set AWS_PROFILE=default for the active deployment account).

Usage:
  AWS_PROFILE=default python test_deployed.py \
      --bucket <idp-working-bucket> \
      --image ../../old_cal_license.png \
      [--function GENAIIDP-cohere-parse-hook] [--region us-west-2]
"""

import argparse
import json
import os
import sys
import uuid

import boto3


def detect_format(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else "jpeg"
    return "jpeg" if ext == "jpg" else ext


def main() -> int:
    parser = argparse.ArgumentParser(description="Test the deployed Cohere Parse hook")
    parser.add_argument("--bucket", required=True, help="IDP working bucket name")
    parser.add_argument(
        "--image", required=True, help="Path to a page image (png/jpeg) to OCR"
    )
    parser.add_argument(
        "--function", default="GENAIIDP-cohere-parse-hook", help="Lambda function name"
    )
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument(
        "--keep", action="store_true", help="Do not delete the uploaded test image"
    )
    args = parser.parse_args()

    if not os.path.exists(args.image):
        print(f"ERROR: image not found: {args.image}")
        return 1

    img_format = detect_format(args.image)
    with open(args.image, "rb") as f:
        img_bytes = f.read()

    s3 = boto3.client("s3", region_name=args.region)
    lambda_client = boto3.client("lambda", region_name=args.region)

    key = f"temp/lambdahook/{uuid.uuid4().hex}.{img_format}"
    print(f"Uploading test image -> s3://{args.bucket}/{key} ({len(img_bytes)} bytes)")
    s3.put_object(Bucket=args.bucket, Key=key, Body=img_bytes)

    event = {
        "modelId": "LambdaHook",
        "context": "OCR",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"text": "Extract all text from this document image."},
                    {
                        "image": {
                            "format": img_format,
                            "source": {
                                "s3Location": {"uri": f"s3://{args.bucket}/{key}"}
                            },
                        }
                    },
                ],
            }
        ],
        "system": [{"text": "You are an OCR engine."}],
        "inferenceConfig": {"temperature": 0.0, "maxTokens": 4096},
    }

    try:
        print(f"Invoking Lambda: {args.function} ...")
        resp = lambda_client.invoke(
            FunctionName=args.function,
            InvocationType="RequestResponse",
            Payload=json.dumps(event).encode("utf-8"),
        )
        payload = json.loads(resp["Payload"].read().decode("utf-8"))

        if resp.get("FunctionError"):
            print(f"LAMBDA ERROR ({resp['FunctionError']}):")
            print(json.dumps(payload, indent=2))
            return 1

        # --- Validate Converse response ---
        text = payload["output"]["message"]["content"][0]["text"]
        assert text.strip(), "Expected non-empty OCR text"
        print(f"\nOCR text length: {len(text)} chars")
        print("First 300 chars:\n" + text[:300])

        # --- Validate structured textractBlocks ---
        tb = payload.get("textractBlocks")
        assert tb and tb.get("Blocks"), "Expected non-empty textractBlocks"
        counts: dict[str, int] = {}
        for b in tb["Blocks"]:
            counts[b["BlockType"]] = counts.get(b["BlockType"], 0) + 1
        print(f"\nBlock counts: {counts}")
        assert counts.get("LINE", 0) > 0, "Expected at least one LINE block"

        # Cohere Parse returns no confidence scores — no block may claim one, or
        # the assessment prompt would report a fabricated number.
        assert all("Confidence" not in b for b in tb["Blocks"]), (
            "Cohere Parse provides no confidence scores, but a block carries one"
        )
        print("Confidence: absent on all blocks (expected)")

        # Geometry, when present, must be normalized 0-1. Text-only pages
        # legitimately have none (Parse boxes tables and figures only).
        lines = [b for b in tb["Blocks"] if b["BlockType"] == "LINE"]
        with_geometry = [b for b in lines if "Geometry" in b]
        for b in with_geometry:
            bbox = b["Geometry"]["BoundingBox"]
            for k in ("Left", "Top", "Width", "Height"):
                assert 0.0 <= bbox[k] <= 1.0, f"Geometry {k} not normalized: {bbox[k]}"
        print(
            f"LINE blocks with table/figure geometry: {len(with_geometry)}/{len(lines)}"
        )
        if with_geometry:
            sample = with_geometry[0]
            print(
                f"Sample: {sample['Text'][:50]!r} "
                f"bbox={sample['Geometry']['BoundingBox']}"
            )
        else:
            print("(no tables or figures detected on this page)")

        # --- Validate metering ---
        usage = payload.get("usage", {})
        print(f"\nUsage (metering): {usage}")
        assert usage.get("pages", 0) >= 1, "Expected usage.pages >= 1 for metering"

        print("\n✅ DEPLOYED LAMBDA TEST: PASS")
        return 0
    finally:
        if not args.keep:
            s3.delete_object(Bucket=args.bucket, Key=key)
            print(f"Cleaned up s3://{args.bucket}/{key}")


if __name__ == "__main__":
    sys.exit(main())
