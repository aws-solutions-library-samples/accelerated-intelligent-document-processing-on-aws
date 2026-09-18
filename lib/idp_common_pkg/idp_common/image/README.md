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
| `count_request_image_blocks(messages)` | How many blocks in a Converse request count against the many-image threshold (`image` **and** `document`, at any `toolResult` depth) |
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

The sweep **mutates a copy**. `invoke_model` is frequently handed the caller's own
`content` list (when no `<<CACHEPOINT>>` tag is present, `processed_content is
content`), so fitting in place would permanently downscale whatever the caller
holds — a cached few-shot example image, or a page-image list a later pass reuses
where the tighter cap does not apply. `_fit_request_images` therefore counts first
(`count_request_image_blocks`) and copies the request spine only when the cap
actually binds; `deepcopy` treats `bytes` as atomic, so the image payloads are
shared rather than duplicated. `fit_images_in_request` itself is documented as
in-place — the copy is the caller's responsibility.

Extraction additionally applies the cap at load time in `_load_document_images`,
where the reduction lands in `metadata.image_downscale`, and where it also covers
the agentic (Strands) path that builds its own requests and never passes through
`BedrockClient.invoke_model`.

**The thresholds differ by mode, because what counts is the number of images ONE
REQUEST carries, not the section's page count.**

| Configuration | Images per request | Clamps at |
|---|---|---|
| Simple (`mode: simple`) | the whole section | **21+ pages** |
| Advanced, **shipped defaults** (`max_concurrent_batches: 10`, `max_pages_per_shard: 5`) | the largest shard `plan_shards` returns, capped by `max_images_per_agent`, doubled | **~101+ pages** |
| Advanced, `max_concurrent_batches: 5` / `2` | same | ~51+ / ~21+ pages |
| Advanced, sharding off (`max_concurrent_batches: 1`) | `min(pages, max_images_per_agent)` — 20 by default — doubled | **11+ pages** |
| Advanced, `max_images_per_agent` ≤ 10 | at most 10, doubled | never |

The Advanced figure is **not computed** — `ExtractionService._agentic_images_per_request`
calls `plan_shards` on the section's real per-page OCR text and takes the largest
shard. Two closed forms were tried and both were wrong: `min(pages, max_pages_per_shard)`
(the page cap is not a ceiling) and `ceil(pages / max_concurrent_batches)` (the
rebalance is token-balanced, not page-balanced). When honouring the page cap would
need more shards than `max_concurrent_batches` allows, `_rebalance_to_cap` discards
those ranges and repacks into exactly that many **token-balanced** groups, so one
dense page can take a shard to itself and crowd the sparse pages into another. Note
`max_concurrent_batches` caps the shard *count*, so raising it lowers the images per
request and raises the threshold. `max_images_per_agent` is the only hard ceiling on
the attached count.

The doubling is pessimistic on purpose: clamping costs some resolution, a rejected
request costs the whole section.

Two limits of this uniform clamp, stated plainly:

- **It is applied to every model family, not just Claude.** The 2,000 px figure is
  measured on Claude; whether Nova, Grok or Astra enforce a many-image cap is not
  established either way. 2,000 px is legal everywhere, so clamping cannot cause a
  rejection that would not otherwise happen, whereas not clamping risks a hard
  failure on a family that does enforce it. The cost is some resolution on a
  >20-image non-Claude request.
- **On high-resolution-tier models it is a real reduction, not a free one.** The
  tier target for Claude 4.7+/Opus 5/Sonnet 5 is a ~2,576 px long edge (a
  4,784-patch cap), so 2,000 px sits about 20% below the resolution the model would
  otherwise have received, and costs roughly 15% of the image tokens — 15% fewer
  28 px patches is 15% less visual information. On standard-tier models (Sonnet 4.6,
  Haiku 4.5, the 3.x family) the tier target is ~1,568 px, so there the clamp
  genuinely costs nothing. **No extraction-accuracy A/B has been run either way**;
  if you process dense small print on a high-res-tier model and care more about
  accuracy than about the request succeeding unattended, set
  `image.target_width`/`target_height` so pages arrive under 2,000 px and keep
  requests at 20 images or fewer.

Two residual gaps this does not close:

- **Tool-result growth mid-loop.** A small agentic request starts at 5 attached
  images; an agent that calls `view_image` 16 times in one conversation reaches 21
  blocks, at which point the attached images are retroactively over the cap. The
  doubling covers one extra copy per page, not an arbitrary number.
- **Resumed agentic runs.** Sharding is skipped entirely when the run carries an
  `existing_data_model` or a `checkpoint_buffer`, so the whole section goes to one
  agent even with `max_concurrent_batches > 1`. The load-time estimate cannot see
  that, so a resumed run can carry more images than it predicted.
  `max_images_per_agent` still caps the attached count (at its default of 20; `0`
  means unlimited and removes that backstop), and the failure is a named
  `ExtractionImageRejected` rather than a wrong result.
- **No total-payload guard.** The sweep bounds each image (5 MiB base64) and each
  side (2,000 px), but nothing bounds the request's total bytes. #994 measured 29
  pages at 27.7 MB rejected on payload alone, with the `Input is too long for
  requested model` wording — so a long enough request crosses a limit again at any
  per-image resolution. At roughly 0.35 MB per clamped page that is on the order of
  60 pages in one request.

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
