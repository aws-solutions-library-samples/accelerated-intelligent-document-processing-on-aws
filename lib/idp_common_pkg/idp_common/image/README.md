Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Image Module

The Image module provides utilities for image processing, resizing, and format conversion used across the IDP pipeline.

## Overview

This module handles image preparation for multimodal LLM prompts and OCR processing. It supports loading images from S3 URIs or raw bytes, resizing while preserving aspect ratios, and formatting images for the Amazon Bedrock API.

## Public Functions

| Function | Description |
|----------|-------------|
| `resize_image(image_data, target_width, target_height, allow_upscale)` | Resize image bytes while preserving aspect ratio |
| `prepare_image(image_source, target_width, target_height, allow_upscale)` | Load image from S3 URI or bytes, then resize |
| `apply_adaptive_binarization(image_data)` | Apply adaptive binarization for OCR preprocessing |
| `prepare_bedrock_image_attachment(image_data)` | Format image bytes as a Bedrock API content block — fits the image to Bedrock's per-image limit first |
| `fit_image_to_bedrock_limit(image_data, max_encoded_bytes, max_dimension, log_fit)` | Downscale an image until its **base64-encoded** size and dimensions fit Bedrock's limits; returns `(bytes, ImageFit | None)` |
| `fit_images_in_request(messages)` | Apply Bedrock's **many-image** dimension cap across a whole Converse request, in place; returns the number of images downscaled |
| `max_dimension_for_image_count(image_count)` | The per-image dimension cap that applies to a request carrying `image_count` image blocks (8,000 px, or 2,000 px above 20) |
| `bedrock_image_format(image_data)` | The Bedrock `format` string for these bytes (`jpeg`/`png`/`gif`/`webp`); raises `ValueError` otherwise |
| `base64_encoded_size(raw_size)` | Bytes a payload occupies once base64-encoded — the number Bedrock compares against its limit |

## Usage

### Resize an Image

```python
from idp_common.image import resize_image

# Resize image bytes to target dimensions (preserves aspect ratio)
resized_bytes = resize_image(
    image_data=original_bytes,
    target_width=1200,
    target_height=1600,
    allow_upscale=False  # Only downscale
)
```

### Load and Prepare from S3

```python
from idp_common.image import prepare_image

# Load from S3 URI and resize
image_bytes = prepare_image(
    image_source="s3://bucket/pages/1/image.jpg",
    target_width=1200,
    target_height=1600
)
```

### Prepare for Bedrock API

```python
from idp_common.image import prepare_bedrock_image_attachment

# Format image for Bedrock multimodal prompt
attachment = prepare_bedrock_image_attachment(image_bytes)
# Returns: {"image": {"format": "jpeg", "source": {"bytes": ...}}}
```

### Bedrock's per-image limit is enforced post-base64 (#778)

Bedrock rejects a single image over **5 MiB** and measures the **base64-encoded**
payload, so the raw budget is **3.75 MiB** (`BEDROCK_IMAGE_MAX_RAW_BYTES` =
3,932,160). A 4 MB PNG is 5.3 MB encoded and fails the whole request with
`ValidationException: image exceeds 5 MB maximum`. Converse also rejects images over
**8,000 px** on a side (`BEDROCK_IMAGE_MAX_DIMENSION`), for every model it routes.

`prepare_bedrock_image_attachment` — the one function every Bedrock image
attachment in the pipeline passes through — fits the image first, so no caller can
emit an oversize image. Callers that want the reduction recorded call the fit
themselves and persist the returned `ImageFit`; extraction does this per page in
`_load_document_images` and writes `metadata.image_downscale` on the section.

```python
from idp_common.image import fit_image_to_bedrock_limit

fitted, fit = fit_image_to_bedrock_limit(page_bytes)
if fit is not None:          # None = already within budget, bytes returned unchanged
    audit.append(fit.to_dict())   # original/final bytes, encoded bytes, size, format, passes, reason
```

The fit shrinks proportionally (LANCZOS, always from the original pixels at the
running scale so passes do not compound loss; bilevel and palette images are converted
first because Pillow would otherwise fall back to NEAREST) by the square root of the
byte ratio, aiming a little under the limit; lossless formats get two passes at their
own format before falling back to JPEG (quality 90, alpha flattened onto white). It raises `ValueError`
naming the sizes if the image still does not fit after nine passes, so the failure is
attributable rather than Bedrock's generic error. Bytes PIL cannot read pass through
unchanged; `prepare_bedrock_image_attachment` still raises its existing
"Unsupported image format" for those.

### A request with MORE THAN 20 images caps every image at 2,000 px (#994)

The 8,000 px cap above is the single-image cap. A **second, stricter** cap applies
to the whole request once it carries more than 20 image blocks
(`BEDROCK_MANY_IMAGE_COUNT_THRESHOLD`): every image in it must then be within
**2,000 px** per side (`BEDROCK_MANY_IMAGE_MAX_DIMENSION`), or Bedrock rejects the
request with `image exceed max allowed size for many-image requests: 2000 pixels`.
On Bedrock, `document` blocks count toward the 20 alongside `image` blocks, and so
do images returned inside a `toolResult` (the agentic `view_image` tool).

Measured on 29 stored pages against `us.anthropic.claude-sonnet-5` in `us-west-2`:
2,001 px rejected, 2,000 px accepted at 117,035 input tokens — and the same pages
at 2,150 px accepted in a 5-image request. So the limit binds on the request's
image **count**, which a per-image guard structurally cannot see: every page is
individually legal and the request still fails. That is why
`fit_image_to_bedrock_limit` alone was not enough.

`fit_images_in_request(messages)` is where the cap is enforced, called from
`BedrockClient.invoke_model` immediately before the `converse` call — the only
point in the library that sees a complete request. It counts the blocks, decides
the cap with `max_dimension_for_image_count`, downscales what is over it, and
refreshes each block's declared `format` (a lossless image can come back as JPEG).
It is deliberately best-effort: an image it cannot fit is sent unchanged with a
warning rather than failing a request Bedrock might accept, and it logs one
aggregate line per request rather than one per image (`log_fit=False`).

Extraction additionally applies the cap at load time in `_load_document_images`,
where the reduction lands in `metadata.image_downscale`, and where it also covers
the agentic (Strands) path that builds its own requests and never passes through
`BedrockClient.invoke_model`. Because Strands re-sends attached pages on every
turn and `view_image` adds a second copy of a page, that path counts each page
twice when deciding whether the threshold is crossed.

The 2,000 px figure is Claude's, and it is the **floor** across families — Nova
documents a 25 MB total-payload budget and no stricter per-image cap — so applying
it uniformly cannot cause a rejection that would not otherwise happen. It costs
roughly 15% of the image tokens, because Converse downscales to about this size
before tokenizing in any case.

### Adaptive Binarization for OCR

```python
from idp_common.image import apply_adaptive_binarization

# Improve OCR accuracy with binarization
enhanced_bytes = apply_adaptive_binarization(image_bytes)
```

## Configuration

Image dimensions are configurable per service (OCR, classification, extraction, assessment). Empty strings preserve original resolution:

```yaml
classification:
  image:
    target_width: ""     # Preserve original (recommended for accuracy)
    target_height: ""

extraction:
  image:
    target_width: "1200"  # Resize for performance
    target_height: "1600"
```

## Related Documentation

- [OCR Image Sizing Guide](../../../../docs/ocr-image-sizing-guide.md) — Detailed image sizing guidance
