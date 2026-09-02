---
title: "OCR Image Sizing Best Practices Guide"
---

# OCR Image Sizing Best Practices Guide

## Overview

The OCR service renders each page to an image, sends that image to the OCR
backend (Textract, Bedrock or BDA), and stores it for the LLM stages
(classification, extraction, assessment) and the UI to reuse.

Resolution is therefore an **accuracy** setting first and a resource setting
second. This guide explains the defaults and when to change them.

## Defaults

| Setting | Default | Role |
|---------|---------|------|
| `ocr.image.dpi` | `300` | Render resolution. The setting that actually controls fidelity. |
| `ocr.image.target_width` | `2600` | Out-of-memory ceiling. Does not bind for A4/Letter at 300 DPI. |
| `ocr.image.target_height` | `3600` | Out-of-memory ceiling. Does not bind for A4/Letter at 300 DPI. |

At these defaults an A4 page renders to 2482×3510 and a US Letter page to
2550×3300 — both at full 300 DPI, with the ceiling never applied. The ceiling
exists to bound memory on unusually large pages (e.g. Legal, plans), not to
reduce cost.

```yaml
ocr:
  image:
    # Nothing specified → 300 DPI, capped at 2600x3600
```

## The two knobs interact — read this before lowering either

`target_width`/`target_height` **cannot raise** resolution. The page is rendered
at `dpi` first, and images are never upscaled. Raising the ceiling without also
raising `dpi` is a no-op.

```yaml
ocr:
  image:
    dpi: 150            # A4 renders to 1240x1754 ...
    target_width: 2600  # ... so this ceiling never applies. Still 1240x1754.
```

This is the single most common misconfiguration. If you want more resolution,
raise `dpi`.

## Do not lower resolution to save tokens

Lowering resolution saves far less than it appears to, because Bedrock
downscales images to its own long-edge ceiling before tokenizing. Token spend
therefore saturates: beyond roughly 1568px on the long edge, extra pixels are
free.

Measured end to end on a 4-page scanned document (Sonnet 4.5, all three LLM
stages):

| Stored page image | Total input tokens |
|---|---|
| 897×1269 | 22,598 |
| 2000×2829 | 22,914 (**+1.4%**) |

Meanwhile OCR accuracy improves consistently with resolution. Same document,
Textract per-page word confidence:

| Page | 897×1269 | 2482×3510 |
|---|---|---|
| 1 | 98.36% (11 words <90%) | 98.92% (5 words <90%) |
| 2 | 93.46% (41 words <90%) | 95.11% (39 words <90%) |
| 3 | 95.86% (39 words <90%) | 96.83% (31 words <90%) |

Below roughly 200 DPI, Textract stops returning small, faint or skewed
characters **at all** — no low-confidence block, no signal to the caller, the
text is simply missing from the response. Page numbers, box numbers and
hand-filled values are the usual casualties. This is what caused
[issue #729](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/729),
where a `Page 2` indicator on a photographed form was silently dropped at 150
DPI and read correctly at 98% confidence at 300 DPI.

If you need to cut LLM cost, reduce the image sent to the *LLM* stages via
`classification.image` / `extraction.image` /
`extraction.confidence.image` — those downscale the stored page image for the
prompt only, leaving OCR at full fidelity.

## When to change the defaults

| Situation | Change |
|---|---|
| Clean, high-contrast digital PDFs, very high volume | Lower `dpi` to 200. Do not go below. |
| Photographed / scanned / folded documents, faint form fills | Keep 300 DPI. This is what the default is for. |
| Very large pages (Legal, plans) hitting memory limits | Lower `target_width`/`target_height`; leave `dpi` alone. |
| OOM errors under high concurrency | Lower `ocr.max_workers` before lowering `dpi`. |

### Non-standard page sizes

The ceiling binds on pages larger than Letter/A4. Legal (612×1008pt) renders to
2550×4200 at 300 DPI, which the 3600px height ceiling scales down to 2185×3600 —
about 257 effective DPI. Still well above the ~200 DPI floor.

## Resource impact

Raising the default render resolution costs memory and render time, not tokens.
At 300 DPI an A4 page is ~26 MB in memory while being processed. With the
default `ocr.max_workers: 20` that is ~520 MB of concurrent image data against
the OCR function's 4096 MB, so there is headroom — but if you raise
`max_workers` substantially, watch the function's memory metric.

Textract's synchronous `Bytes` limits are 5 MB and 10,000×10,000 pixels. A
300 DPI A4 page encodes to roughly 1.0–1.3 MB as JPEG, well inside both.

## How the settings are resolved

- Defaults apply when both `target_width` and `target_height` are unspecified,
  empty strings, or `None`.
- Invalid values fall back to the defaults with a warning.
- A partial configuration (only width **or** only height) disables the ceiling
  entirely, preserving legacy behavior.
- Resizing always preserves aspect ratio and never upscales.

## Logging

```
INFO OCR Service initialized - DPI: 300, Image sizing: 2600x3600
INFO No image sizing configured, applying default ceiling: 2600x3600 (out-of-memory guard; does not bind for A4/Letter at 300 dpi)
INFO Page 1 already fits target size, extracted at: 2482x3510
INFO Using configured image sizing: 1200x1600
WARNING Invalid resize configuration values: width=abc, height=xyz. Falling back to defaults: 2600x3600
```

The `Extracted page N at target size` / `already fits target size` lines report
the dimensions actually sent to OCR — the quickest way to confirm what
resolution a document was really processed at.

## Troubleshooting

| Symptom | Check |
|---|---|
| A value visible in the document is missing from OCR text entirely | Resolution. Confirm the `extracted at:` log line, then raise `dpi`. |
| Raised the ceiling but nothing changed | You also need to raise `dpi` — the ceiling cannot upscale. |
| Tables misaligned, handwriting errors | Keep 300 DPI; consider enabling `ocr.image.preprocessing` for uneven lighting. |
| Memory errors | Lower `max_workers`, or lower the ceiling for oversized pages. |
| High LLM cost | Downscale at `classification.image` / `extraction.image`, not at `ocr.image`. |
