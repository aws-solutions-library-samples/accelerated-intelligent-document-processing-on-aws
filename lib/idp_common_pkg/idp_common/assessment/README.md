Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Assessment Service for IDP Accelerator

This module provides the **standalone confidence assessment step** for the IDP
Accelerator. As of config **v0.6**, confidence is an **output of extraction**:
its settings live under `extraction.confidence.*` (and geometry under
`extraction.geometry.*`), not a top-level `assessment.*` block. This service
implements the standalone step that runs on the Simple (non-agentic) path when
`extraction.confidence.mode: separate` (the default). On the agentic path, and
for `integrated` mode, confidence is produced inside extraction and this
standalone step auto-skips.

> **Granular assessment is retired.** The former `GranularAssessmentService` /
> `granular_service.py` and the `extraction.confidence.granular` config field
> have been **deleted**. Large lists are handled by the standalone large-list
> batching described below (plus a bounded missing-row retry). Any leftover
> `granular.*` keys still validate but are ignored. See
> `docs/migration-granular-retirement.md`.

> **A list row with a nested group or an inner list is not "unscored."**
> `batching._row_confidence_missing` used to look exactly one level down, so a
> nested group inside a list row was mistaken for a confidence leaf (a group has no
> `confidence` key, so the lookup returned `None`) and an inner list was skipped by
> the `isinstance(v, dict)` filter entirely. Measured live on a 3-record pay
> statement whose rows carry an `Employee` group and an `Earnings` list: every leaf
> came back at 0.99–1.0 with OCR geometry and `truncated_calls: 0`, and the section
> still reported `assessment_incomplete` (**error**) for all 3 rows after burning a
> `claude-sonnet-5:1m` escalation that recovered 0 — a stronger model reproduces
> the identical shape, so the ladder had no way out. The predicate now recurses
> (`_iter_confidence_leaves`). Pre-existing for any list-of-object attribute whose
> rows contain a group or an inner list; multi-instance sections (GitHub #715) make
> every record a row, so it became universal there.
>
> Two consequences worth knowing: a row whose group carries no leaves at all, next
> to at least one scored scalar leaf, now counts as scored (the old predicate
> flagged it); and for a wrapped class the retry unit is the whole record, so one
> unscored leaf anywhere re-runs that entire record.

> **Multi-instance (#715).** `_get_class_schema` returns the **effective** schema,
> so a flagged class's `instances` array is a real top-level array property here:
> the `attr_type == "list"` branch produces `instances[i]` row keys,
> `resolve_array_item_thresholds` resolves per-record sub-field thresholds, and
> `batching._schema_field_mismatch_reason` does not blacklist the section. Without
> the wrap, `instances` is an unknown key and the whole section collapses to one
> `{"confidence": 0.5}` leaf that the escalation ladder then skips permanently.
> Two known granularity losses for a flagged class: `_format_property_descriptions`
> descends one level under an array, so a nested group/list *inside* a record loses
> its sub-field descriptions in the confidence prompt; and
> `assessment_function/assessment_validator.py` compares one top-level attribute
> (`instances`) rather than per-field.

> **Compact reasons (default prompts).** The shipped confidence prompts ask the
> model to emit `confidence_reason` **only for leaves below 0.9 confidence**;
> confident leaves emit just `{"confidence": <score>}`. Because output tokens
> dominate assessment cost, this materially cuts cost with no effect on the
> scores or threshold/alert logic (`_enhance_dict_assessment` spreads whatever
> leaf keys are present). The many `confidence_reason`-on-every-field examples
> below predate this and are illustrative of the *structure*, not the
> reason-frequency. Widen the 0.9 threshold in the confidence `task_prompt` to
> get a reason on every field.

> **Integrated (simple-path) response shapes.** In `integrated` mode the single
> extraction inference returns values **and** confidence. The service prefers a
> `{"extraction": {...}, "confidence": {...}}` envelope, but also lifts a
> `field_assessment` (or `confidence`) **sibling** key emitted next to the
> extracted fields — stripping it from `inference_result` and promoting it to
> `explainability_info` so the standalone step auto-skips (avoids a redundant
> second Bedrock pass). See `ExtractionService._split_inline_confidence`.

## Overview

The Assessment service is designed to assess the confidence and accuracy of extraction results by analyzing them against source documents using LLMs. It supports both text and image content analysis and provides detailed confidence scores and explanations for each extracted attribute, applying configured confidence thresholds (threshold enrichment) to each field.

## Features

- **LLM-powered confidence assessment** using Amazon Bedrock models
- **Multi-modal analysis** with support for both document text and images
- **Automatic bounding box processing** with spatial localization of extracted fields
- **UI-compatible geometry output** for immediate visualization
- **Optimized token usage** with pre-generated text confidence data (80-90% reduction)
- **Structured confidence output** with scores and explanations per attribute
- **Prompt template support** with placeholder substitution
- **Image placeholder positioning** for precise multimodal prompt construction
- **Fallback mechanisms** for robust error handling
- **Metering integration** for usage tracking
- **Direct Document model integration**
- **Automatic large-list batching** for long tables (see *Large-list batching* below)

## Usage Example

```python
from idp_common.assessment.service import AssessmentService
from idp_common.models import Document

# Initialize assessment service with configuration
assessment_service = AssessmentService(
    region="us-east-1",
    config=config_dict
)

# Process a single section
document = assessment_service.process_document_section(document, section_id="1")

# Or assess entire document
document = assessment_service.assess_document(document)

# Access assessment results in the extraction results
section = document.sections[0]
extraction_data = s3.get_json_content(section.extraction_result_uri)
assessment_info = extraction_data.get("explainability_info", {})

# Example assessment output:
# {
#   "vendor_name": {
#     "confidence": 0.95,
#     "confidence_reason": "Vendor name clearly visible in header with high OCR confidence"
#   },
#   "total_amount": {
#     "confidence": 0.87,
#     "confidence_reason": "Amount visible but OCR confidence slightly lower due to formatting"
#   }
# }
```

## Configuration

The assessment service uses configuration-driven prompts and model parameters,
under `extraction.confidence` in v0.6:

```yaml
extraction:
  confidence:
    enabled: true                       # Enable/disable confidence processing
    mode: separate                      # off | separate (default) | integrated
    model: "us.amazon.nova-pro-v1:0"
    temperature: 0
    top_k: 5
    top_p: 0.1
    reasoning_effort: low               # only if a reasoning-capable model is selected
    list_batch_size: 25                 # rows per assessment batch for large lists
    # NOTE: no max_tokens knob — the confidence pass always requests the model's
    # maximum output (resolved from config_library/model_config_limits.yaml) so
    # long list assessments are never truncated.
    system_prompt: "You are an expert document analyst..."
    task_prompt: |
      Assess the confidence of extraction results for this {DOCUMENT_CLASS} document.

      Text Confidence Data:
      {OCR_TEXT_CONFIDENCE}

      Extraction Results:
      {EXTRACTION_RESULTS}

      Attributes Definition:
      {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}

      Document Images:
      {DOCUMENT_IMAGE}

      Respond with confidence assessments in JSON format.
```

### `enabled` Configuration Property

The assessment service supports runtime enable/disable control via the `enabled` property:

- **`enabled: true`** (default): Assessment processing proceeds normally
- **`enabled: false`**: Assessment is skipped entirely with minimal overhead

**Cost Optimization**: When `enabled: false`, no LLM API calls are made, resulting in zero assessment costs.

**Example - Disabling Assessment:**
```yaml
extraction:
  confidence:
    enabled: false  # Disables all confidence processing (equivalent to mode: off)
    # Other properties can remain but will be ignored
    model: us.amazon.nova-lite-v1:0
    temperature: 0.0
```

**Behavior When Disabled:**
- Service immediately returns with logging: "Assessment is disabled via configuration"
- No LLM API calls or S3 operations are performed
- Document processing continues to completion
- Minimal performance impact (early return)

## Large-list batching (`assessment/batching.py`)

A single confidence inference over a large list field (e.g. a 120-row transaction
table) is unreliable: the model under-enumerates or omits the list, leaving most
rows unassessed. The standalone Assessment step handles this itself — it does **not**
depend on granular assessment for large lists.

`process_document_section` runs the assessment through the shared
`idp_common.assessment.batching.assess_results_batched`, which:

1. Finds the single largest list field whose length exceeds
   `extraction.confidence.list_batch_size` (default 25).
2. Slices that list into `list_batch_size` chunks and assesses each chunk
   **sequentially**, passing the SAME scalars/context every time so scalar
   assessments and the document context are preserved (scalars come from the first
   batch). Sequential, not a thread pool, is intentional: the historical granular
   path's 20-way fan-out caused a Bedrock prompt-cacheWrite storm — batching
   sequentially avoids it and is why retiring granular is a net cost win.
3. Concatenates the per-row assessments in order and calls
   `reconcile_assessment_to_data` to force the assessment to index-align with the
   extracted data — truncating over-long lists, padding short/omitted ones with
   per-sub-field placeholders (so every un-assessed row is still groundable), and
   fanning any per-row scalar confidence out to per-column leaves.
4. Runs a **bounded missing-row retry**: any rows the model dropped within a
   batch are re-scored in a follow-up pass (missing rows only), so large-list
   coverage reaches 100% without re-scoring rows that already have confidence.

Both `assess_results_batched` and `reconcile_assessment_to_data` are shared with
the agentic in-shard assessment path (`ExtractionService`), so there is exactly one
implementation of large-list assessment. When no list exceeds the batch size the
helper makes a single (still reconciled) call — identical to the previous behavior.

### Truncation-aware adaptive batch splitting

A configured `list_batch_size` is a *row* count, but the model's real limit is
its **max output tokens**. When per-row output is large — most notably with
`extraction.geometry.mode: llm`, which asks the model to emit a bounding box for
every cell — even a modest batch can exceed a small-cap model's ceiling (e.g.
Amazon Nova Lite caps at 10,000 output tokens). A truncated response
(`stopReason == "max_tokens"`) is unparseable JSON, and previously the service
silently fell back to a default `0.5` for every field / null-confidence
placeholders for every row — with no signal that anything went wrong.

The core now detects truncation (`AssessmentCoreResult.truncated`) and the
batcher recovers automatically: any slice the model truncates is **recursively
halved and re-assessed** until it parses or bottoms out at a single row —
instead of accepting the placeholder. The recursive splitter
(`_assess_slice_adaptive`) runs in the initial batch loop and in **every**
missing-row retry, so it protects all four confidence code paths uniformly:
the standalone Assessment step (`separate`), the agentic single-agent and
sharded in-shard passes, and the simple/agentic `integrated` path's inline-row
retry (`ExtractionService._retry_missing_integrated_rows`). Simple `separate`
extraction — the granular-assessment replacement — goes through the standalone
step and is fully covered.

The activity is surfaced for visibility (only when a run actually had to shrink):

- **`metadata.assessment_batch_split_stats`** on the section result — a dict with
  `truncated_calls`, `splits`, `min_batch_size_used`, `rows_recovered_by_retry`,
  `unrecoverable_rows`, `derived_batch_size`, `configured_batch_size`,
  `escalation_model`, `escalation_rounds`, and `rows_recovered_by_escalation`.
- An **`⚠ Assessment Batch Splitting`** block in the agentic extraction
  **processing report**.

### Self-healing: token-aware sizing + model escalation

Adaptive splitting recovers a truncated batch by *shrinking* it against the
**same** model. When the model's output cap is the real bottleneck, shrinking
alone can bottom out at a single row and still recover nothing (the failure that
left 34/68 transaction rows with `confidence: null`). Two additions make advanced
mode complete correctly on the first try:

1. **Token-aware first-pass sizing** (`compute_token_aware_batch_size`). Before
   the first call, the effective batch size is derived from the confidence
   model's output cap (`bedrock.model_utils.get_model_max_output_tokens`) and an
   estimate of per-row output tokens (`extraction.sharding.estimate_tokens` ×
   a confidence-envelope multiplier × a larger bbox multiplier for
   `geometry.mode` `llm`/`llm_grounded`). The result **only ever shrinks**
   `list_batch_size` (never grows it past your ceiling), so a small-cap model
   (Nova Lite, 10K) starts at ~6–9 rows instead of truncating at 25. Unknown
   models fall back to the configured size. Recorded as `derived_batch_size`.

2. **Model-escalation ladder** (`extraction.confidence.escalation_*`). When rows
   are *still* unscored after token-aware shrink + same-model retries, the
   still-missing rows (only those) are re-assessed on a **stronger confidence
   model** with a bigger output cap — the rung that actually fixes small-cap
   truncation. Cheapest-first and bounded by `max_escalation_rounds`; a round
   that recovers nothing stops early. Configure with:

   ```yaml
   extraction:
     confidence:
       escalation_enabled: true          # ON by default
       escalation_model: "us.anthropic.claude-sonnet-4-20250514-v1:0"
       max_escalation_rounds: 2
   ```

   Per-class override: `x-aws-idp-confidence-escalation-model` (mirrors
   `x-aws-idp-extraction-escalation-model`). `escalation_model: null` skips the
   model rung (ladder stays at shrink + retry). Escalation applies uniformly to
   the standalone `separate` step and the `integrated` in-shard/inline retry.

If rows remain unscored even after escalation, `unrecoverable_rows` is non-zero —
reduce per-row output, e.g. switch `extraction.geometry.mode` from `llm` to
`ocr_only` (the default), which derives boxes from OCR value-matching instead of
the model.

**Schema-mismatch guard.** Some "unscored" rows are not a truncation problem at
all: if extraction returns a **list** for an attribute the class schema does *not*
declare as `type: array` (an off-schema/hallucinated field, or one typed as a
scalar), the confidence enhancer collapses it to a single default `{"confidence":
0.5}` leaf, and reconciliation pads the data rows with null placeholders that no
model can fill — escalating them just burns a slow large-model call to re-collapse
the same list. When a `class_schema` is threaded into `assess_results_batched`
(both the standalone and in-shard paths pass it), the ladder detects this via
`_schema_field_mismatch_reason`, **skips both retry and escalation** for the
offending field, records it in `split_stats["schema_mismatch_fields"]`, and emits
an `assessment_schema_mismatch` **error** naming the field and the real fix:
correct the class schema or the extraction prompt so the attribute is defined (as
an array where it should be). A validly array-typed field is never blocked.

The property is dereferenced (`config/schema_utils.deref_schema`) before its
`type` is read, since a property declared as `{"$ref": "#/$defs/Foo"}` carries
none — so a `$defs` group is reported as `declared as 'object'` rather than
mislabelled `'scalar'` (a type the schema contains nowhere).

An **array** declared as a bare `$ref` (hand-authored configs can do this; the
UI's schema editor only emits objects into `$defs`) used to be a genuine
dead-end for the same reason one level down: `_assess_core` also read `type` off
the raw property, treated the attribute as a scalar, and collapsed the model's
per-row list into one default leaf. That read is dereferenced too, so such a
field is now row-scored normally — and only because of that is it correct for
this guard to stop skipping it. Note the two reads are deliberately split: the
**type** is taken from the dereferenced subschema, while the property's own
`x-aws-idp-confidence-threshold` is still read from the raw property, because
honoring a threshold declared on the `$defs` definition instead of the property
is a change to threshold *inheritance* and belongs with `threshold_resolver`'s
rules.

> This is an **extraction/schema** defect surfaced at assessment time — note that
> traditional (non-agentic) extraction has no schema-validation step, and even the
> agentic `validation` gate won't catch *extra* attributes unless the class schema
> sets `additionalProperties: false` (unknown properties pass JSON-Schema
> validation by default). So an off-schema attribute can reach assessment
> unflagged; this guard is where it becomes visible.

### Structured processing issues + completeness gate

The ladder's `split_stats` are translated into user-surfacing
`ProcessingIssue`s (`idp_common.models.ProcessingIssue`) by
`build_assessment_issues`, so a run that healed (or couldn't heal) is visible
without reading raw metadata. Severity ladder:

| Condition | code | severity |
|-----------|------|----------|
| List extracted for a non-array/off-schema attribute (retry+escalation skipped) | `assessment_schema_mismatch` | **error** |
| Rows still unscored after the full ladder | `assessment_incomplete` | **error** |
| Wall-clock guard cut escalation short | `assessment_deadline_reached` | **warning** |
| Healed, but needed shrinking/escalation | `assessment_recovered_with_retries` | **info** |

`assessment_schema_mismatch` takes precedence over `assessment_incomplete`: when
both fire, the schema mismatch is the true root cause and is emitted alone.

A **completeness gate** (`audit_explainability`) runs after the ladder on both
the standalone and in-shard paths: it confirms every extracted value has a real
(non-null), in-range confidence and — when `geometry.mode != "off"` — a bounding
box, emitting `assessment_confidence_out_of_range` / `assessment_geometry_incomplete`
for anything structurally wrong. Issues are attached to each `Section`
(`section.processing_issues`), rolled up to `Document.processing_issue_count`,
and rendered in the extraction processing report.

### Lambda wall-clock budget & resume safety

The escalation ladder adds sequential model calls inside the 900s
Extraction/Assessment Lambdas. To avoid a hard timeout, both handlers thread the
Lambda's `context.get_remaining_time_in_millis()` down as an absolute
`deadline_epoch`; before starting a **new escalation round** the ladder checks
the estimated round cost fits in the remaining time minus a 90s safety reserve.
If not, it stops, keeps what was recovered, and flags `deadline_reached` (→
`assessment_deadline_reached` warning) — converting a would-be timeout into a
soft, flagged, complete document. As defense in depth, the Step Functions
`ExtractionStep`/`AssessmentStep`/`ShardExtractionStep` retry sets include
`States.Timeout` / `Lambda.Unknown`, so a genuine timeout is retried and resumes
via the per-shard S3 persistence and the Assessment step's "skip if
`explainability_info` already present" short-circuit.

## Prompt Template Placeholders

The assessment service supports the following placeholders in prompt templates:

### Standard Placeholders
- `{DOCUMENT_TEXT}` - Parsed document text (markdown format)
- `{DOCUMENT_CLASS}` - Document classification (e.g., "invoice", "contract")
- `{ATTRIBUTE_NAMES_AND_DESCRIPTIONS}` - Formatted list of attributes to extract,
  including nested group members and list-item columns. Subschemas declared as a
  local `$ref` into the class's `$defs` (what the UI's schema editor emits) are
  dereferenced first, so `$ref`-based groups/lists render their real descriptions.
- `{EXTRACTION_RESULTS}` - JSON of extraction results to assess

### OCR Confidence Data
- `{OCR_TEXT_CONFIDENCE}` - **NEW** - Optimized text confidence data with 80-90% token reduction

### Image Positioning
- `{DOCUMENT_IMAGE}` - Placeholder for precise image positioning in multimodal prompts.
  Presence of this placeholder is the **only** switch for image attachment: the
  section's page images are attached when it is present and omitted when it is not,
  independent of `extraction.geometry.mode`. Visually-evidenced fields (signature /
  checkbox / stamp booleans, handwriting) can only be judged from the image, so the
  page images are sent even when the model is not asked for bounding boxes. Drop the
  placeholder from the prompt for a cheaper text-only confidence pass.

## Text Confidence Data Integration

The assessment service automatically uses pre-generated text confidence data when available, providing significant performance and cost benefits:

### Automatic Data Source Selection
1. **Primary**: Uses pre-generated `textConfidence.json` files from OCR processing
2. **Fallback**: Generates text confidence data on-demand from raw OCR for backward compatibility

### Token Usage Optimization
```python
# Traditional approach (high token usage)
prompt = f"OCR Data: {raw_textract_response}"  # ~50,000 tokens

# Optimized approach (low token usage)  
prompt = f"Text Confidence Data: {text_confidence_data}"  # ~5,000 tokens
```

### Data Format
The text confidence data provides essential information in a minimal format:

```json
{
  "page_count": 2,
  "text_blocks": [
    {
      "text": "INVOICE #12345",
      "confidence": 98.7
    },
    {
      "text": "Date: March 15, 2024",
      "confidence": 95.2
    }
  ]
}
```

## Automatic Bounding Box Processing

The assessment service now includes **automatic spatial localization** capabilities that convert LLM-provided bounding box coordinates to UI-compatible geometry format without any configuration.

### How It Works

1. **Enhanced Prompts**: Prompt templates request both confidence scores and spatial coordinates
2. **Automatic Detection**: Service detects when LLM provides `bbox` and `page` data
3. **Coordinate Conversion**: Converts from 0-1000 normalized scale to 0-1 geometry format
4. **UI Integration**: Outputs geometry format compatible with existing visualization

### Example Assessment with Spatial Data

**LLM Response (with bbox data):**
```json
{
  "InvoiceNumber": {
    "confidence": 0.95,
    "confidence_reason": "Clear text with high OCR confidence",
    "bbox": [100, 200, 300, 250],
    "page": 1
  },
  "VendorAddress": {
    "State": {
      "confidence": 0.99,
      "confidence_reason": "State clearly visible",
      "bbox": [230, 116, 259, 126], 
      "page": 1
    }
  }
}
```

**Automatic Conversion Output:**
```json
{
  "InvoiceNumber": {
    "confidence": 0.95,
    "confidence_reason": "Clear text with high OCR confidence",
    "confidence_threshold": 0.9,
    "geometry": [{
      "boundingBox": {
        "top": 0.2,
        "left": 0.1,
        "width": 0.2,
        "height": 0.05
      },
      "page": 1
    }]
  },
  "VendorAddress": {
    "State": {
      "confidence": 0.99,
      "confidence_reason": "State clearly visible",
      "confidence_threshold": 0.9,
      "geometry": [{
        "boundingBox": {
          "top": 0.116,
          "left": 0.23,
          "width": 0.029,
          "height": 0.01
        },
        "page": 1
      }]
    }
  }
}
```

### Supported Attribute Types

**All attribute types support automatic bounding box processing:**

- ✅ **Simple Attributes**: Direct conversion of bbox → geometry
- ✅ **Group Attributes**: Recursive processing of nested bbox data
- ✅ **List Attributes**: Individual bbox conversion for each list item

### Enhanced Prompt Requirements

To enable spatial localization, include these instructions in your `task_prompt`:

```yaml
extraction:
  geometry:
    mode: llm_grounded
  confidence:
    task_prompt: |
      <spatial-localization-guidelines>
      For each field, provide bounding box coordinates:
      - bbox: [x1, y1, x2, y2] coordinates in normalized 0-1000 scale
      - page: Page number where the field appears (starting from 1)

      Coordinate system:
      - Use normalized scale 0-1000 for both x and y axes
      - x1, y1 = top-left corner of bounding box
      - x2, y2 = bottom-right corner of bounding box
      - Ensure x2 > x1 and y2 > y1
      - Make bounding boxes tight around the actual text content
      </spatial-localization-guidelines>

      For each attribute, provide:
      {
        "attribute_name": {
          "confidence": 0.95,
          "confidence_reason": "Clear explanation",
          "bbox": [100, 200, 300, 250],
          "page": 1
        }
      }
```

### Benefits

- **No Configuration Required**: Works automatically when LLM provides bbox data
- **Backward Compatible**: Existing assessments without bbox continue working
- **UI Ready**: Geometry format works immediately with existing visualizations
- **Consistent**: applies uniformly across the standalone step and the agentic in-shard path

## Grounding Geometry in Real OCR Data

The bounding boxes produced above are **LLM-estimated**. When the OCR backend supplies real
geometry (Textract or the Mistral OCR LambdaHook), a post-LLM enrichment pass grounds each
field's box in the actual OCR coordinates from the consolidated per-page `pageData.json`
artifact (see `idp_common/ocr/README.md`). Implemented in `idp_common.assessment.ocr_grounding`
and used by the standalone assessment step (`service.py`) and the agentic in-shard path alike.

```python
from idp_common.assessment.ocr_grounding import (
    load_page_ocr_data,
    ground_assessment_geometry,
)

# Read pageData.json for the section's pages (keyed by 1-indexed page number).
page_data = load_page_ocr_data(document.pages, sorted_page_ids)

# Replace LLM-estimated boxes with real OCR boxes where the extracted value matches a line.
enhanced_assessment = ground_assessment_geometry(
    enhanced_assessment, extraction_results, page_data
)
```

Key behaviors:

- **Tiered matching** of each extracted value to OCR `lines[]`: exact → value-in-line →
  multi-line span (boxes unioned) → line fragment → token-overlap fuzzy (≥ 0.6).
- **Spatial disambiguation** of repeated values: when a value matches multiple lines, the
  candidate nearest the LLM-estimated box wins; with no usable reference box the field keeps
  its LLM box (so identical amounts across table rows don't collapse onto one line).
- **Coordinates stay 0–1**: `pageData` geometry is already normalized, so grounded boxes skip
  the 0–1000 → 0–1 rescale that LLM boxes go through. No mixed scales in `explainability_info`.
- **Additive output**: a matched field gets `geometry_source` (`"ocr"`/`"ocr-paragraph"`/
  `"llm"`) and, when available, `ocr_confidence` (0–1). The LLM `confidence`/`confidence_reason`
  are never modified, so HITL and confidence alerts are unaffected.
- **Config gate**: `extraction.geometry.mode` (`ocr_only` default | `llm_grounded` | `llm` | `off`). The legacy `assessment.ground_geometry_in_ocr: false` maps to `llm`; old configs are migrated on read.
- **Safe fallback**: absent `pageData.json`, `geometryAvailable: false`, or no value match →
  keep the LLM-estimated box (identical to prior behavior). `pageData.json` is read from S3, so
  the `{OCR_TEXT_CONFIDENCE}` prompt and token budget are unchanged.

### Per-shard grounding (sharded agentic path)

In the sharded agentic path each shard **grounds its own rows against only its own
pages** immediately after its in-shard confidence assessment (in
`ExtractionService._build_assess_runner` → `_ground_shard_assessment`), rather than
deferring one full-section grounding sweep to the merge step. This matters because
grounding is `O(rows × pages)` fuzzy line-matching: a large multi-page table
(e.g. 1,440 rows over 24 pages) grounded once at merge time is single-threaded, has
no wall-clock guard, and could exceed the merge Lambda's 900s ceiling. Grounding
per-shard makes it scale exactly like the confidence assessment does — each shard's
work is bounded to its own ~N rows × ~5 pages and runs concurrently across shards.
Scoping to the shard's pages also improves correctness: a row's value can only appear
on its own pages, so cross-page false matches are avoided.

The merge step then re-runs `ground_assessment_geometry(..., skip_grounded=True)`,
which is a near-instant no-op over leaves that already carry a `geometry_source`
(everything the shards grounded) and only grounds any **residual** leaves — e.g.
reconcile-padded placeholder rows the assessment LLM omitted. The non-sharded
single-agent path still grounds once at the end (`skip_grounded=True` is a no-op
there because no leaf is pre-grounded), so behavior is identical.

### Indexed value→line matching (performance)

`match_value_to_geometry` builds, once per page and caches on the `pageData` dict,
a normalized-line list plus an **exact-text → lines index**, then does an
index-first pass across all pages before any linear scan. Because EXACT is the most
precise tier, a value that equals an OCR line verbatim (the overwhelmingly common
case for table cells) resolves as an O(1) dict hit and skips the substring / span /
token-overlap / Levenshtein passes entirely; a tier-aware early-out likewise skips
the fuzzy ladder whenever a hit that it cannot beat already exists. This took a
1,440-row section from ~64s to ~2s with byte-identical output. When nothing matches
exactly (reformatting, OCR noise) the full fuzzy ladder still runs, so match quality
is unchanged. `_ground_shard_assessment` logs the row count + duration so a
regression can never again be a silent multi-minute hang.

### Images omitted in OCR-geometry modes

In `geometry.mode: ocr_only` (default) and `off`, `assess_results` **drops the page
images from the confidence prompt** — the model is never asked for boxes (geometry
comes from OCR value-matching), so the images only bloat the request (~1.7K input
tokens each; a 5-page shard ≈ 8.7K) and, on a small multimodal model like Nova Lite,
materially raise latency and the odds of a max-output-token truncation on large
tables. In `llm`/`llm_grounded` the images are kept (the model needs them to estimate
boxes).

See the *Geometry / Bounding Boxes* section of `docs/extraction-and-confidence.md`
for the user-facing description.

## Multimodal Assessment

The service supports sophisticated multimodal prompts with precise image positioning:

### Image Placeholder Usage
```python
task_prompt = """
Analyze the extraction results for accuracy.

Extraction Results:
{EXTRACTION_RESULTS}

{DOCUMENT_IMAGE}

Based on the document image above and the OCR confidence data below, 
assess each extracted field:

{OCR_TEXT_CONFIDENCE}
"""
```

### Automatic Image Handling
- Supports both single and multiple document images
- Processes all document pages without image count restrictions
- Graceful fallback when images are unavailable
- Info logging for image count monitoring

## Attribute Types and Assessment Formats

The assessment service supports three distinct attribute types, each requiring a specific assessment response format. The service automatically detects the attribute type from your document class configuration and handles the assessment processing accordingly.

### 1. Simple Attributes

For basic single-value extractions like dates, amounts, or names.

**Configuration Example:**
```yaml
attributes:
  - name: "InvoiceNumber"
    attributeType: "simple"  # or omit for default
    description: "The invoice number from the document"
  - name: "TotalAmount"
    attributeType: "simple"
    description: "The total amount due"
```

**Expected Assessment Response:**
```json
{
  "InvoiceNumber": {
    "confidence": 0.92,
    "confidence_reason": "Invoice number clearly visible in standard location"
  },
  "TotalAmount": {
    "confidence": 0.87,
    "confidence_reason": "Amount visible but OCR confidence slightly lower due to formatting"
  }
}
```

### 2. Group Attributes

For nested object structures with multiple related fields that are logically grouped together.

**Configuration Example:**
```yaml
attributes:
  - name: "VendorDetails"
    attributeType: "group"
    description: "Vendor contact information"
    groupAttributes:
      - name: "VendorName"
        description: "Name of the vendor company"
      - name: "VendorAddress"
        description: "Vendor's business address"
      - name: "VendorPhone"
        description: "Vendor's contact phone number"
```

**Expected Assessment Response:**
```json
{
  "VendorDetails": {
    "VendorName": {
      "confidence": 0.95,
      "confidence_reason": "Company name clearly printed in header"
    },
    "VendorAddress": {
      "confidence": 0.88,
      "confidence_reason": "Address visible with good OCR quality"
    },
    "VendorPhone": {
      "confidence": 0.82,
      "confidence_reason": "Phone number partially blurred but readable"
    }
  }
}
```

### 3. List Attributes

For arrays of items where each item has the same structure, such as line items, transactions, or entries.

**Configuration Example:**
```yaml
attributes:
  - name: "LineItems"
    attributeType: "list"
    description: "Individual line items on the invoice"
    listItemTemplate:
      itemDescription: "A single invoice line item"
      itemAttributes:
        - name: "Description"
          description: "Item description or service name"
        - name: "Quantity"
          description: "Number of items or hours"
        - name: "UnitPrice"
          description: "Price per unit"
        - name: "Total"
          description: "Line item total (quantity × unit price)"
```

**Expected Assessment Response:**
```json
{
  "LineItems": [
    {
      "Description": {
        "confidence": 0.94,
        "confidence_reason": "Service description clearly printed"
      },
      "Quantity": {
        "confidence": 0.91,
        "confidence_reason": "Quantity number easily readable"
      },
      "UnitPrice": {
        "confidence": 0.89,
        "confidence_reason": "Unit price in standard currency format"
      },
      "Total": {
        "confidence": 0.93,
        "confidence_reason": "Total amount calculation clearly visible"
      }
    },
    {
      "Description": {
        "confidence": 0.87,
        "confidence_reason": "Description text slightly compressed but readable"
      },
      "Quantity": {
        "confidence": 0.95,
        "confidence_reason": "Quantity clearly printed in quantity column"
      },
      "UnitPrice": {
        "confidence": 0.88,
        "confidence_reason": "Unit price readable with minor OCR uncertainty"
      },
      "Total": {
        "confidence": 0.92,
        "confidence_reason": "Line total properly formatted and clear"
      }
    }
  ]
}
```

### Service Processing Behavior

The assessment service automatically handles each attribute type differently:

**Simple Attributes:**
- Expects a single confidence assessment object
- Adds confidence threshold to the assessment data
- Creates alerts for low confidence scores

**Group Attributes:**
- Processes each sub-attribute within the group independently
- Applies confidence thresholds to each sub-attribute
- Creates individual alerts for each sub-attribute that falls below threshold

**List Attributes:**
- Processes each array item separately (individual assessment per list item)
- Applies the same confidence thresholds to all items in the list
- Creates alerts using array notation (e.g., "LineItems[0].Description", "LineItems[1].Total")
- **Important**: Does NOT create aggregate assessments - each item must be assessed individually

### Assessment Response Requirements

**Critical Guidelines:**

1. **Structure Matching**: Assessment response must exactly mirror the extraction result structure
2. **List Processing**: For list attributes, assess each array item individually, never as an aggregate
3. **Nested Consistency**: Group attributes require confidence assessments for all sub-attributes
4. **Individual Focus**: Each confidence assessment should evaluate a specific field, not summarize multiple fields

**Common Mistakes to Avoid:**

```json
// ❌ WRONG: Aggregate assessment for list
{
  "LineItems": {
    "confidence": 0.85,
    "confidence_reason": "Overall line items look good"
  }
}

// ✅ CORRECT: Individual item assessments
{
  "LineItems": [
    {
      "Description": {"confidence": 0.94, "confidence_reason": "..."},
      "Quantity": {"confidence": 0.91, "confidence_reason": "..."}
    },
    {
      "Description": {"confidence": 0.87, "confidence_reason": "..."},
      "Quantity": {"confidence": 0.95, "confidence_reason": "..."}
    }
  ]
}
```

## Complete Assessment Output Example

Here's a comprehensive example showing all three attribute types in a single assessment:

```json
{
  "inference_result": {
    "InvoiceNumber": "INV-12345",
    "VendorDetails": {
      "VendorName": "ACME Corporation",
      "VendorAddress": "123 Business St, City, ST 12345",
      "VendorPhone": "(555) 123-4567"
    },
    "LineItems": [
      {
        "Description": "Professional Services",
        "Quantity": "40",
        "UnitPrice": "$125.00",
        "Total": "$5,000.00"
      },
      {
        "Description": "Materials",
        "Quantity": "10",
        "UnitPrice": "$25.00", 
        "Total": "$250.00"
      }
    ]
  },
  "explainability_info": [
    {
      "InvoiceNumber": {
        "confidence": 0.92,
        "confidence_reason": "Invoice number clearly visible in standard header location",
        "confidence_threshold": 0.85
      },
      "VendorDetails": {
        "VendorName": {
          "confidence": 0.95,
          "confidence_reason": "Company name clearly printed in document header with high OCR confidence",
          "confidence_threshold": 0.90
        },
        "VendorAddress": {
          "confidence": 0.88,
          "confidence_reason": "Address visible with good OCR quality, standard formatting",
          "confidence_threshold": 0.80
        },
        "VendorPhone": {
          "confidence": 0.82,
          "confidence_reason": "Phone number readable but slightly compressed in layout",
          "confidence_threshold": 0.75
        }
      },
      "LineItems": [
        {
          "Description": {
            "confidence": 0.94,
            "confidence_reason": "Service description clearly printed in line item table",
            "confidence_threshold": 0.80
          },
          "Quantity": {
            "confidence": 0.91,
            "confidence_reason": "Quantity number clearly visible in quantity column",
            "confidence_threshold": 0.85
          },
          "UnitPrice": {
            "confidence": 0.89,
            "confidence_reason": "Unit price in standard currency format, well aligned",
            "confidence_threshold": 0.85
          },
          "Total": {
            "confidence": 0.93,
            "confidence_reason": "Total amount clearly calculated and displayed",
            "confidence_threshold": 0.85
          }
        },
        {
          "Description": {
            "confidence": 0.87,
            "confidence_reason": "Description text slightly compressed but fully readable",
            "confidence_threshold": 0.80
          },
          "Quantity": {
            "confidence": 0.95,
            "confidence_reason": "Quantity clearly printed with excellent OCR confidence",
            "confidence_threshold": 0.85
          },
          "UnitPrice": {
            "confidence": 0.88,
            "confidence_reason": "Unit price readable with standard formatting",
            "confidence_threshold": 0.85
          },
          "Total": {
            "confidence": 0.92,
            "confidence_reason": "Line total properly formatted and clearly visible",
            "confidence_threshold": 0.85
          }
        }
      ]
    }
  ],
  "metadata": {
    "assessment_time_seconds": 4.23,
    "assessment_parsing_succeeded": true
  }
}
```

## Error Handling and Fallbacks

The assessment service includes comprehensive error handling:

### Parsing Failures
- Automatic fallback to default confidence scores (0.5) when LLM response parsing fails
- Detailed error logging for troubleshooting
- Continued processing of other sections

### Data Source Fallbacks
- Primary: Pre-generated text confidence files
- Secondary: On-demand text confidence generation from raw OCR
- Tertiary: Graceful degradation without OCR confidence data

### Template Validation
- Validates required placeholders in prompt templates
- Fallback to default prompts when template validation fails
- Flexible placeholder enforcement for partial templates

## Integration Example

```python
import json
from idp_common.assessment.service import AssessmentService
from idp_common.models import Document
from idp_common import s3

def lambda_handler(event, context):
    # Initialize service
    assessment_service = AssessmentService(
        region=os.environ['AWS_REGION'],
        config=event.get('config', {})
    )
    
    # Get document from event
    document = Document.from_dict(event['document'])
    
    # Assess all sections in the document
    assessed_document = assessment_service.assess_document(document)
    
    # Return updated document
    return {
        'document': assessed_document.to_dict()
    }
```

## Best Practices

### Prompt Design
- Use `{OCR_TEXT_CONFIDENCE}` instead of raw OCR data for optimal token usage
- Position `{DOCUMENT_IMAGE}` strategically in multimodal prompts
- Include clear instructions for confidence scoring (0.0 to 1.0 scale)

### Configuration
- Set appropriate temperature (0 for deterministic assessment)
- Output tokens are not configurable — the confidence pass always requests the
  model maximum (so long list assessments aren't truncated)
- Use system prompts to establish assessment criteria

### Performance
- Leverage pre-generated text confidence data for best performance
- Monitor assessment timing and token usage through metering data
- Consider image limits for large multi-page documents

## Service Classes

### AssessmentService

Main service class for document assessment:

```python
class AssessmentService:
    def __init__(self, region: str = None, config: Dict[str, Any] = None)
    
    def process_document_section(self, document: Document, section_id: str) -> Document
    def assess_document(self, document: Document) -> Document
    
    # Internal methods for text confidence data and prompt building
    def _get_text_confidence_data(self, page) -> str
    def _build_content_with_or_without_image_placeholder(...) -> List[Dict[str, Any]]
```

### Assessment Models

Data models for structured assessment results:

```python
@dataclass
class AttributeAssessment:
    confidence: float
    confidence_reason: str

@dataclass 
class AssessmentResult:
    attributes: Dict[str, AttributeAssessment]
    metadata: Dict[str, Any]
