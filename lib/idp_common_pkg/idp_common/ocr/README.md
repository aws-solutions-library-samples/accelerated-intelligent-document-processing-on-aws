Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# OCR Service for IDP Accelerator

This module provides OCR (Optical Character Recognition) capabilities for processing documents within the IDP Accelerator project.

## Overview

The OCR service is designed to process PDF documents and extract text using multiple backend options. It supports AWS Textract for traditional OCR with confidence scores, Amazon Bedrock for LLM-based text extraction, and image-only processing. The service works directly with the Document model from the common data model.

## OCR Backend Options

The service supports four OCR backends, each with different capabilities and use cases:

### 1. Textract Backend (Default - Recommended for Assessment)
- **Technology**: AWS Textract OCR service
- **Confidence Data**: ✅ Full granular confidence scores per text line (displayed as markdown table)
- **Features**: Basic text detection + enhanced document analysis (tables, forms, signatures, layout)
- **Assessment Quality**: ⭐⭐⭐ Optimal - Real OCR confidence enables accurate assessment
- **Use Cases**: Standard document processing, when assessment is enabled, production workflows
- **Cost**: cheapest for raw text (~$1.50/1K pages); Tables +$15/1K, Forms +$50/1K

### 2. BDA Backend (Bedrock Data Automation standard-output OCR)
- **Technology**: Amazon Bedrock Data Automation "standard output" used as a pure OCR engine (no blueprints/extraction).
- **Confidence Data**: ✅ Word-level confidence + bounding boxes. (BDA's *line* confidence is unreliable, so the service reconstructs each line's confidence as the mean of its words — see `idp_common/bda/bda_ocr.py`.)
- **Features**: Reading-order **markdown with tables and layout** in a single call — no feature flags to compose. Auto-enables the agentic extraction table tool.
- **Assessment Quality**: ⭐⭐⭐ Real word-level confidence/geometry preserved.
- **Cost**: flat **$10/1K pages** regardless of tables/forms/layout.
- **Use Cases**: Table-heavy documents (bank/brokerage statements), when you want table-aware OCR + predictable pricing without composing Textract features. For plain text where you don't need tables, Textract is cheaper.

**How it works**: pages are rendered to images (as with Textract), each page image is
uploaded to S3, then processed one page at a time via the **synchronous**
`InvokeDataAutomation` API. Per-page invocation is required because sync BDA is
capped at ~10 pages per call; it also gives per-page concurrency and retry
isolation. A standard-output-only **SYNC** project named
`<stackname>_OCR_StdOutput` is provisioned per-stack at deploy time by a
CloudFormation custom resource (`Custom::BDAOCRProject`) and its ARN is passed
to the OCR function via the `BDA_OCR_PROJECT_ARN` env var (override via
`ocr.bda_project_arn`). The OCR function never creates the project at runtime;
if no ARN is available, selecting the `bda` backend raises a clear error — use
the Textract backend there. Two cases produce an empty ARN: a region where BDA
is unreachable (the custom resource returns empty rather than failing the
stack), and **any partition other than `aws`**, where the resource is not
created at all (condition `ShouldCreateBDAOCRProject`). GovCloud offers BDA but
rejects this project shape — `ValidationException: Sync project does not support
video/audio/document modality in Standard Output Configuration` — so the
`bda` OCR backend is not available in GovCloud or China regions. The handler
also treats that ValidationException as "unsupported here" and returns an empty
ARN rather than failing the stack, so a commercial region that refuses the
project shape degrades the same way. The project sets `modalityRouting: {jpeg: DOCUMENT,
png: DOCUMENT}` and the page is passed by **`s3Uri`** (extension-bearing) so BDA
reliably treats each page image as a document — inline `bytes` lack an
extension and BDA misclassifies some page images as `IMAGE` (empty OCR) under
concurrent load. BDA standard output is converted to Amazon Textract response
format (PAGE/LINE/WORD blocks), so it flows through the same
`textConfidence.json` / `pageData.json` path as the Textract backend.

> **Geometry / rectification:** BDA internally deskews (perspective-corrects)
> each page — and may crop/rectify to a document *sub-region* (e.g. a driver's
> license occupying part of the page) — then returns bounding boxes and
> `asset_metadata.corners` normalized against that *rectified crop*, while the
> pipeline stores and the UI overlays the *original* page image. Left uncorrected
> this offsets every box (visibly misaligned overlays in the Page/Section Visual
> Editors): a skewed scan tilts them, and a sub-region crop squeezes them into a
> band. `bda_ocr.py` corrects both by, for each page:
> 1. rescaling `corners` from rectified-crop space into original-image 0–1 space
>    using `original_image_size / (rectified_image_width_pixels,
>    rectified_image_height_pixels)` — so the corner quad covers the crop's true
>    place on the page (the converter takes `original_image_size` from the OCR
>    service, which knows the stored `image.jpg` dimensions); then
> 2. bilinearly mapping every box's corners through that quad into original-image
>    space.
>
> This makes BDA geometry agree with Textract's (which is never rectified),
> verified to within ~0.001 of Textract centers on skewed and cropped pages. A
> rectified axis-aligned box becomes a quadrilateral `Polygon` with an
> axis-aligned `BoundingBox` envelope. The mapping is a no-op when corners are
> identity/absent, or when `original_image_size` is unavailable (falls back to
> treating corners as already page-normalized).

### 3. Bedrock Backend (LLM-based OCR, incl. LambdaHook)
- **Technology**: Amazon Bedrock LLMs (Claude, Nova) for text extraction, or a custom `LambdaHook` (`model_id: "LambdaHook"`) that proxies to any inference provider.
- **Confidence Data**:
  - Plain Bedrock LLM OCR: ❌ No confidence data (displays "No confidence data available from LLM OCR").
  - LambdaHook returning **structured OCR**: ✅ Real confidence + geometry — if the hook returns a top-level `textractBlocks` object (Amazon Textract response format with a `Blocks` list), the service persists it as `rawText.json` and generates a real `textConfidence.json` from it.
- **Features**: Advanced text understanding, better handling of challenging/degraded documents; with a LambdaHook, any third-party OCR (e.g. Mistral OCR, Chandra OCR).
- **Assessment Quality**: ❌ for plain LLM OCR; ⭐⭐⭐ when a LambdaHook supplies `textractBlocks` confidence.
- **Use Cases**: Challenging documents where traditional OCR fails; integrating external OCR providers via the LambdaHook feature.

### 4. None Backend (Image-only)
- **Technology**: No OCR processing
- **Confidence Data**: ❌ No confidence data (displays "No OCR performed")
- **Features**: Image extraction and storage only
- **Assessment Quality**: ❌ No text confidence for assessment
- **Use Cases**: Image-only workflows, custom OCR integration

> ⚠️ **CRITICAL for Assessment**: When assessment functionality is enabled, use `backend="textract"` (default) to preserve granular confidence data. Plain `backend="bedrock"` LLM OCR produces empty confidence data that eliminates assessment capability — **unless** the configured LambdaHook returns structured `textractBlocks` (see below), in which case confidence/geometry are preserved.

### Structured OCR from a LambdaHook (`textractBlocks`)

When `backend="bedrock"` and `model_id="LambdaHook"`, the hook receives a Converse-API payload and may return — in addition to the markdown text — a top-level `textractBlocks` object in **Amazon Textract response format**:

```json
{
  "output": {"message": {"content": [{"text": "# Page markdown..."}]}},
  "textractBlocks": {
    "DocumentMetadata": {"Pages": 1},
    "Blocks": [
      {"BlockType": "PAGE", "Id": "..."},
      {"BlockType": "LINE", "Id": "...", "Text": "Account: 12345", "Confidence": 97.5,
       "Geometry": {"BoundingBox": {"Left": 0.1, "Top": 0.02, "Width": 0.4, "Height": 0.03}}},
      {"BlockType": "WORD", "Id": "...", "Text": "12345", "Confidence": 92.0}
    ]
  },
  "usage": {"pages": 1}
}
```

`OcrService._extract_bedrock_ocr_artifacts()` detects a non-empty `textractBlocks` and persists it as the page's `rawText.json`, then builds a real `textConfidence.json` from its LINE blocks (same path as the Textract backend). Geometry uses Textract's normalized 0–1 `BoundingBox`. Hooks returning only text keep the previous placeholder behavior. See `samples/lambda-hook-inference/GENAIIDP-mistral-ocr-hook/` for a reference implementation (Mistral OCR) and [docs/lambda-hook-inference.md](../../../../docs/lambda-hook-inference.md).

## Features

- PDF processing with page-by-page OCR
- Concurrent processing of pages for improved performance
- Support for basic text detection (faster) or enhanced document analysis with granular Textract feature selection
- Direct integration with the Document data model
- Automatic S3 retrieval of input documents
- S3 storage of intermediate and final results
- **Text confidence data generation** for efficient assessment prompts
- Metering data collection for usage tracking
- Comprehensive error handling
- Rich markdown output for tables and forms when using enhanced features

## Usage Example

### New Simplified Pattern (Recommended)

```python
from idp_common import ocr, get_config
from idp_common.models import Document

# Load configuration (typically from DynamoDB)
config = get_config()

# Create or retrieve a Document object with input/output details
document = Document(
    id="doc-123",
    input_bucket="input-bucket",
    input_key="document.pdf",
    output_bucket="output-bucket"
)

# Initialize OCR service with config dictionary
ocr_service = ocr.OcrService(
    region='us-east-1',
    config=config,  # Pass entire config dictionary
    backend='textract'  # Optional: override backend from config
)

# Process document - this will automatically get the PDF from S3
processed_document = ocr_service.process_document(document)

# Use the results
print(f"Processed {processed_document.num_pages} pages")
for page_id, page in processed_document.pages.items():
    print(f"Page {page_id}: Image at {page.image_uri}")
    print(f"Page {page_id}: Text and Markdown at {page.parsed_text_uri}")
    print(f"Page {page_id}: Text confidence data at {page.text_confidence_uri}")
```

### Legacy Pattern (Deprecated)

```python
# The old pattern with individual parameters is still supported but deprecated
ocr_service = ocr.OcrService(
    region='us-east-1',
    max_workers=20,
    enhanced_features=False,  # or ["TABLES", "FORMS"]
    dpi=300,  # the shipped default since #729; 150 loses faint characters
    resize_config={"target_width": 2600, "target_height": 3600},
    backend='textract'
)
```

## Configuration Structure

When using the new pattern, the OCR service expects configuration in the following structure:

```yaml
ocr:
  backend: "textract"  # Options: "textract", "bda", "bedrock", "none"
  max_workers: 20
  features:
    - name: "TABLES"
    - name: "FORMS"
  image:
    dpi: 300  # DPI for PDF page extraction (default: 300)
    # Out-of-memory ceiling, not a cost control (defaults: 2600x3600).
    # Cannot raise resolution -- the page renders at `dpi` first and is
    # never upscaled, so raising this without raising `dpi` is a no-op.
    target_width: 2600
    target_height: 3600
    preprocessing: false  # Enable adaptive binarization
  # For BDA backend only (optional): use a specific standard-output SYNC
  # project instead of the per-stack <stackname>_OCR_StdOutput project the
  # stack provisions (delivered via the BDA_OCR_PROJECT_ARN env var).
  bda_project_arn: null
  # For Bedrock backend only:
  model_id: "anthropic.claude-3-sonnet-20240229-v1:0"
  system_prompt: "You are an OCR system..."
  task_prompt: "Extract all text from this image..."
```

### Memory-Optimized Image Extraction

The OCR service uses advanced memory optimization to prevent OutOfMemory errors when processing large high-resolution documents:

**Direct Size Extraction**: When resize configuration is provided (`target_width` and `target_height`), images are extracted directly at the target dimensions using pypdfium2 matrix transformations. This completely eliminates memory spikes from creating oversized images.

**Example for Large Document:**
- **Original approach**: Extract 7469×9623 (101MB) → Resize to 2482×3510 (26MB) → Memory spike
- **Optimized approach**: Extract directly at 2482×3510 (26MB) → No memory spike

**Preserved Logic**: The optimization maintains all existing resize behavior:
- ✅ Never upscales images (only applies scaling when scale_factor < 1.0)
- ✅ Preserves aspect ratio using `min(width_ratio, height_ratio)`
- ✅ Handles edge cases (no config, images already smaller than targets)
- ✅ Full backward compatibility

### DPI Configuration

The DPI (dots per inch) setting controls the base resolution when extracting images from PDF pages. It is the setting that actually governs OCR fidelity — `target_width`/`target_height` can only shrink what `dpi` produced.

- **Default**: 300 DPI (`DEFAULT_DPI` in `service.py`)
- **Range**: 72-300 DPI
- **Location**: `ocr.image.dpi` in the configuration
- **Behavior**:
  - Only applies to PDF files (image files maintain their original resolution)
  - Combined with the resize ceiling for memory safety
  - Never upscales, so the ceiling cannot recover resolution `dpi` did not produce

**Do not lower DPI to save tokens.** Bedrock downscales images to its own
long-edge ceiling before tokenizing, so LLM token spend saturates — measured end
to end, moving the stored page image from 897×1269 to 2000×2829 changed total
input tokens by 1.4% (22,598 → 22,914). To cut LLM cost, downscale for the
prompt via `classification.image` / `extraction.image` /
`extraction.confidence.image` and leave OCR at full fidelity.

**Below ~200 DPI Textract drops small glyphs silently.** Small, faint or skewed
characters — page numbers, box numbers, hand-filled values — are omitted from
the response entirely, with no low-confidence block and no signal to the caller.
This caused issue #729: a `Page 2` indicator on a photographed form was absent
at 150 DPI and read at 98% confidence at 300 DPI. Measured per-page word
confidence on that document improved on every page going from 897×1269 to
2482×3510 (98.36→98.92, 93.46→95.11, 95.86→96.83).

**Memory Considerations**: at 300 DPI an A4 page is ~26 MB in memory. With the
default `max_workers: 20` that is ~520 MB concurrent against the OCR function's
4096 MB. If you raise `max_workers`, watch the memory metric; prefer lowering
`max_workers` over lowering `dpi`.


## Migration Guide

To migrate from the old pattern to the new pattern:

1. **In Lambda functions:**
   ```python
   # Old pattern
   features = [feature['name'] for feature in ocr_config.get("features", [])]
   service = ocr.OcrService(
       region=region,
       max_workers=MAX_WORKERS,
       enhanced_features=features,
       resize_config=resize_config,
       backend=backend
   )
   
   # New pattern
   config = get_config()
   service = ocr.OcrService(
       region=region,
       config=config,
       backend=config.get("ocr", {}).get("backend", "textract")
   )
   ```

2. **In notebooks:**
   ```python
   # Old pattern
   ocr_service = ocr.OcrService(
       region=region,
       enhanced_features=features
   )
   
   # New pattern
   ocr_service = ocr.OcrService(
       region=region,
       config=CONFIG  # Where CONFIG is your loaded configuration
   )
   ```

The new pattern provides:
- Cleaner, more consistent API across all IDP services
- Easier configuration management
- No need to extract individual parameters
- Future-proof design for adding new features

## Text Confidence Data

The OCR service automatically generates optimized text confidence data for each page, which is specifically designed for LLM assessment prompts. This feature dramatically reduces token usage while preserving all information needed for confidence evaluation.

### Generated Files per Page

For each page, the OCR service creates:

- **`image.jpg`** - Page image in JPEG format
- **`rawText.json`** - Complete Textract response (full metadata, geometric data, relationships)
- **`result.json`** - Parsed markdown text content for human readability
- **`textConfidence.json`** - Condensed text confidence data for assessment prompts
- **`pageData.json`** - **NEW** - Consolidated, backend-agnostic OCR page data
  (text + confidence + geometry) — see [Consolidated OCR Page Data](#consolidated-ocr-page-data-pagedatajson)

### Text Confidence Data Format

The format varies by OCR backend:

**Textract Backend (with confidence data):**
```json
{
  "text": "| Text | Confidence |\n|------|------------|\n| WESTERN DARK FIRED TOBACCO GROWERS' ASSOCIATION | 99.4 |\n| 206 Maple Street | 91.4 |\n| Murray, KY 42071 | 98.7 |"
}
```

The `text` field contains a markdown table with two columns:
- **Text**: The extracted text content (with pipe characters escaped as `\|`)
- **Confidence**: OCR confidence score rounded to 1 decimal point
- Handwriting is indicated with "(HANDWRITING)" suffix in the text column

**Bedrock Backend (no confidence data):**
```json
{
  "text": "| Text | Confidence |\n|------|------------|\n| *No confidence data available from LLM OCR* | N/A |"
}
```

**None Backend (no OCR):**
```json
{
  "text": "| Text | Confidence |\n|------|------------|\n| *No OCR performed* | N/A |"
}
```

### Benefits

- **85-95% token reduction** compared to raw Textract output (markdown table format is more compact than JSON)
- **Preserved assessment data**: Text content, OCR confidence scores (rounded to 1 decimal), text type (PRINTED/HANDWRITING)
- **Removed overhead**: Geometric data, relationships, block IDs, verbose metadata, and unnecessary JSON syntax
- **Improved readability**: Markdown table format is human-readable in both UI and assessment prompts
- **Cost efficiency**: Significantly reduced LLM inference costs for assessment workflows
- **UI compatibility**: Displays beautifully in the Text Confidence View using existing markdown rendering
- **Automated generation**: Created during initial OCR processing, not repeatedly during assessment

### Usage in Assessment Prompts

Assessment services can reference this data using the `{OCR_TEXT_CONFIDENCE}` placeholder in prompt templates:

```python
task_prompt = """
Assess the extraction confidence for this document.

Text Confidence Data:
{OCR_TEXT_CONFIDENCE}

Extraction Results:
{EXTRACTION_RESULTS}
"""
```

## Consolidated OCR Page Data (`pageData.json`)

`textConfidence.json` is intentionally token-reduced (LINE text + confidence, no
geometry) for assessment prompts, and `rawText.json` holds geometry only for
backends that produce Textract-format blocks. To give consumers (the UI page
viewer, and — in a future phase — assessment grounding) a single,
**backend-agnostic** view of text **+ confidence + geometry**, the OCR service
also writes `pageData.json` per page.

The `Page` model carries its URI as `ocr_page_data_uri`; AppSync/DynamoDB expose
it as `OcrPageDataUri`. The artifact is **additive** — existing files and the
`{OCR_TEXT_CONFIDENCE}` assessment prompt are unchanged, so there is **zero
token-budget impact** and documents processed before this change simply have no
`pageData.json` (consumers degrade gracefully).

### Schema

The primary text unit is the **LINE**, with optional **WORD** children.
`confidence` and `geometry` are *independently optional* on every unit, since
backends differ in what they provide. Geometry is normalized **0–1** (Textract
convention), matching what the UI bounding-box renderer consumes.

```jsonc
{
  "schemaVersion": 1,
  "provider": "textract",        // textract | bedrock-lambdahook | bedrock-llm | none | converted
  "page": null,                  // reserved for native page dimensions
  "geometryAvailable": true,     // any unit has geometry
  "confidenceAvailable": true,   // any unit has confidence
  "wordsAvailable": true,        // any line has word children
  "signaturesAvailable": true,   // the page has signature detections
  "signatures": [                // SIGNATURES feature; [] when not requested/found
    {
      "id": "sig-1",
      "confidence": 11.0,        // 0–100 detection confidence, optional
      "geometry": {              // optional; normalized 0–1
        "boundingBox": { "left": 0.57, "top": 0.88, "width": 0.04, "height": 0.02 },
        "polygon": [ { "x": 0.57, "y": 0.88 } ]
      }
    }
  ],
  "lines": [
    {
      "id": "line-1",
      "text": "Account: 12345",
      "confidence": 97.5,                       // 0–100, optional
      "geometry": {                              // optional; normalized 0–1
        "boundingBox": { "left": 0.10, "top": 0.02, "width": 0.40, "height": 0.03 },
        "polygon": [ { "x": 0.10, "y": 0.02 } ]  // optional (Textract only)
      },
      "geometrySource": "line",                  // line | paragraph | none
      "textType": "PRINTED",                     // PRINTED | HANDWRITING | null
      "words": [                                  // optional (Textract)
        { "text": "Account:", "confidence": 99.0, "geometry": { "boundingBox": {} } },
        { "text": "12345",    "confidence": 92.0, "geometry": null }
      ]
    }
  ]
}
```

### Cross-backend matrix

| Backend | text | confidence | geometry | `geometrySource` |
|---|---|---|---|---|
| **Textract** | LINE + WORD | per-LINE & per-WORD | per-LINE & per-WORD (box + polygon) | `line` |
| **Mistral LambdaHook** | LINE + WORD | per-LINE & per-WORD | paragraph-level box shared by sibling lines; WORDs none | `paragraph` |
| **Chandra / plain Bedrock LLM** | lines synthesized from markdown | none | none | `none` |
| **`none`** | none | none | none | — |
| **Converted (non-PDF)** | per-line | per-line `99.0` placeholder | none | `none` |

The producer derives `pageData.json` in-process from the same OCR result already
used for `rawText.json`/`textConfidence.json` (`OcrService._build_page_data`),
so it adds **no extra OCR calls**.

### Signature detections

When the OCR backend is asked for the Textract `SIGNATURES` feature, each
detection is a block with a **confidence and geometry but no text**. They are
kept in their own `signatures` array rather than mixed into `lines`, because the
`ocr_only` geometry grounder matches extracted values against line *text* and a
textless pseudo-line has nothing to match.

The same detections are surfaced in two other places, both of which previously
dropped them silently:

- **`textConfidence.json`** — appended after the LINE table as an `OCR signature
  detections` block, so the confidence prompt can see that a signature region was
  flagged and with what confidence.
- **The parsed page text** (`result.json`, which feeds the extraction prompt and
  the UI markdown view) — same block, appended after the text.

The linearizer already emits an inline `[SIGNATURE]` token per detection, but on
its own that token is not usable evidence: it is placed by reading order (so it
can land beside an unrelated field) and carries no confidence, making a genuine
signature indistinguishable from a 10%-confidence smudge.

The appended block therefore reports, per detection:

- **confidence with a band** — `confidence=11.0 (very low)`; Textract's score is a
  *detection* confidence, so a very low value often means a stray mark;
- **position in words** — `right half, lower area (x=59%, y=89%)`, matching the
  left/right, upper/lower language field descriptions actually use;
- **an explicit total** (`flagged 1 region on this page`), so a consumer weighing two
  signature fields can see that only one region exists;
- **a caveat** that the inline `[SIGNATURE]` token's placement is reading-order
  derived and is *not* evidence of which field the signature belongs to.

Raw coordinates alone were measured to be unusable: on the two-column signature
block of an IRS Form 4549, both the extraction and the confidence model read a
detection at `left=0.572` as "the first (left) signature box". Stating the page half
removes that spatial inference.

**Naming the OCR text around each detection was tried and removed.** Reporting
`at: "Signature of taxpayer"; right: "Date"` reads as more helpful and measured
worse — naming a signature *label* beside the mark biases the model toward `true`.
With a confidence-threshold rule in the field descriptions, the entry above passed
**9/9** on that document while the same entry plus the surrounding text passed
**2/5**. Do not re-add it without measuring.

The block is deliberately **not** formatted as a markdown table, since page text is
scanned by the agentic extraction table parser.

## Lambda Integration Example

```python
import json
import logging
import os
from idp_common import ocr
from idp_common.models import Document

# Initialize settings
region = os.environ['AWS_REGION']
MAX_WORKERS = int(os.environ.get('MAX_WORKERS', 20))

def handler(event, context): 
    # Get document from event
    document = Document.from_dict(event["document"])
    
    # Initialize the OCR service
    service = ocr.OcrService(
        region=region,
        max_workers=MAX_WORKERS,
        enhanced_features=False  # Use basic OCR (or specify features as a list)
    )
    
    # Process the document - the service will read the PDF content directly
    document = service.process_document(document)
    
    # Return the document as a dict - it will be passed to the next function
    return {
        "document": document.to_dict()
    }
```

## Roadmap

### Phase 1: Current Implementation (Basic Integration)
- ✅ Basic OCR service with pypdfium2 for PDF processing
- ✅ Support for Textract's text detection
- ✅ Compatible with existing Pattern workflow
- ✅ Full integration with Document data model
- ✅ Automatic document retrieval from S3
- ✅ Comprehensive error handling

### Phase 2: Enhanced Features
- ✅ Support for table extraction and form recognition
- ✅ Granular control of Textract feature types (TABLES, FORMS, SIGNATURES, LAYOUT)
- ✅ Improved parsing for extracted tables and forms
- ✅ Markdown output format for richer text representation
- 🔲 PDF processing options (resolution, format)
