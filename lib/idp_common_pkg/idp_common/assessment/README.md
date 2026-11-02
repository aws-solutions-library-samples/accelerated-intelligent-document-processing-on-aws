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
    list_batch_size: 25                 # CEILING on rows per batch; the size used is
                                        # derived from the confidence model's output
                                        # cap, row width and geometry mode
    # NOTE: no max_tokens knob — each confidence call requests an OUTPUT BUDGET:
    # what a correct answer over its fields needs (one leaf per scalar and per list
    # cell, ~40 tokens each, x3 with LLM bounding boxes, plus 1,500 overhead),
    # floored at 2,000 and capped at the model's maximum from
    # config_library/model_config_limits.yaml (`bedrock.sizing.confidence_output_budget`).
    # A response that overruns it is handled by the truncation-aware splitting below.
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

1. Finds the single largest list field whose length exceeds the effective batch
   size (derived from the confidence model's output cap, the row's width in
   confidence leaves, the geometry mode and the fields every batch repeats; never larger than the `extraction.confidence.list_batch_size`
   ceiling, default 25; and never larger than a **per-family loop ceiling** where one
   was measured — 12 rows for Amazon Nova Lite/Micro, see
   `bedrock.sizing._MODEL_LIST_BATCH_CEILINGS`).
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

> **Why the Nova Lite ceiling is 12, not a token count.** Live and in 4/4 offline
> replays of the same inputs, Nova Lite at temperature 0 asked to score a 25-row,
> 3-column batch emitted the same `{"Date": {"confidence": 1.0}, ...}` object 189
> times until it hit its 10,000-token cap — ~60 s and 10,000 output tokens per
> document before the splitter recovered the rows at 12. The token math allows 41
> rows for that shape; 13 rows looped 1/5, 8 rows 0/8. Greedy decoding on a long
> run of near-identical objects is the trigger, not input size (it still looped with
> no images, with no OCR text, and 2/3 with no text-confidence block). Two guards now apply: the
> family ceiling above, and — on those two models only — a per-call output budget
> (a loop is now cut off at the budget, ~2,000–4,600 tokens, instead of the cap, and
> recovered the same way). Other confidence models keep requesting their maximum.

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
  `escalation_model`, `escalation_rounds`, and `rows_recovered_by_escalation`,
  plus `oversized_row_fields` / `oversized_row_model` /
  `oversized_row_output_cap` / `oversized_row_chars` / `oversized_row_class` when
  the give-up guard below fired.
- An **`⚠ Assessment Batch Splitting`** block in the agentic extraction
  **processing report**.

### Self-healing: token-aware sizing + model escalation

Adaptive splitting recovers a truncated batch by *shrinking* it against the
**same** model. When the model's output cap is the real bottleneck, shrinking
alone can bottom out at a single row and still recover nothing (the failure that
left 34/68 transaction rows with `confidence: null`). Two additions make advanced
mode complete correctly on the first try:

1. **Token-aware first-pass sizing** (`compute_token_aware_batch_size`). Before
   the first call the batch size is derived from four inputs: the confidence
   model's output cap (`bedrock.model_utils.get_model_max_output_tokens`), the
   **confidence-leaf count** of the list's widest row, whether `geometry.mode`
   is `llm`/`llm_grounded` (a per-cell bounding box roughly triples per-row
   output), and the estimated output of **everything else the call carries**.
   The estimator itself lives in `bedrock.sizing`
   (`confidence_rows_per_call`) so this module and `compute_sizing_plan` cannot
   drift apart. Recorded as `derived_batch_size`.

   A row's width is counted at every depth (`bedrock.sizing.confidence_leaf_count`,
   the same count `confidence_output_budget` uses): the model scores a nested group
   field by field and a nested list item by item, so a line item holding six scalars
   and a seven-field group is thirteen wide, not six. And every batch repeats the
   section's scalars, groups and every other list unsliced, so their output is
   reserved first: the rows must fit both in the safety fraction of the cap and in
   what the cap leaves after that fixed part and the per-call overhead. When the
   fixed part is small the fraction is the tighter bound and nothing changes; a
   section heavy in scalars, or carrying a second list, gets a smaller batch instead
   of one planned to overrun the cap.

   There is no correct fixed value. On a 10,000-token-cap model without a loop
   ceiling (Nova Pro) with bounding boxes the batch that fits is 41 rows for a
   1-column list, 13 for 3 columns and 5 for 8 (Nova Lite: 12, 12 and 5, because
   its measured loop ceiling binds first); on a 128K-output model the reliability cap of 50 bounds the derivation
   instead of the token math. `list_batch_size` is therefore a **ceiling** on the
   derived size (default 25), never a target — and an *explicit* ceiling is honoured
   in full, so a deliberate pin above 50 is not clamped.

   Two behaviours changed after v0.6.7, both in the safe direction. The column
   count is measured across a sample of rows rather than off `rows[0]`, because a
   first row missing a key made a wide list look narrow and inflated the batch.
   And an unknown model, or a row shape whose width cannot be measured, now sizes
   from a conservative fallback cap instead of returning the configured value —
   silently trusting a permissive configured value on a small-cap model is how a
   25-row batch reached Nova Lite in the first place.

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
       escalation_model: "us.anthropic.claude-sonnet-5:1m"
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

**Oversized-row guard (give up instead of retrying, [#894](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/894)).**
Halving a truncated batch converges only while a *smaller* batch can fit. If the
model still returns `stopReason=max_tokens` with a **single row** in the call,
that one row's confidence output exceeds the model's cap and **no batch size can
work**. The ladder now stops there: it records the terminal condition in
`split_stats["oversized_row_fields"]` (with the model, its output cap, the
offending row's approximate serialized size and the class), **skips the same-model
retry rung entirely**, allows at most **one** escalation round (a bigger output
cap is the only remedy that can legitimately succeed), and emits
`assessment_row_too_large` (**error**).

Before this guard there was no terminal condition for that case: the ladder kept
re-running the impossible call through the retry rounds and the bisection tree, and
the section ended up reporting the generic `assessment_incomplete`. Measured on a
shape that batches (8 rows at batch 4) the guard halves the primary calls, 28 → 14;
32 rows go 112 → 56, and with escalation 42 → 28 total. On the single-outer-row shape
#894 reports, `len(rows) <= batch_size` means `assess_results_batched` takes its
`if not list_fields:` branch and calls the model directly, so
`_assess_slice_adaptive` is not reached on the first pass and the pre-rung skip
cannot fire; the condition is detected inside the first retry round instead. The call
count there is unchanged (2 before, 2 after) and the gain is purely diagnostic.

The observed trigger is a class marked `x-aws-idp-multi-instance: true`: the wrapper
makes the *instance list* the outer list field, so one "row" is a whole instance
(record) carrying its own long inner list (a 100-row `Transactions` table). The
sizer counts such a row by every leaf it emits, inner cells included, so it is sized
as the 201-leaf row it is rather than as a one-column one. That makes the first pass
batch it correctly; it cannot make a single instance whose own output exceeds the
cap fit, which is the case this guard reports.

⚠️ **The 900s timeout reported on #894 is not explained by this loop.** The observed
failure was an Assessment Lambda hitting its 900s wall three times
(`Sandbox.Timedout` x3) after a 45s extraction, with the document stuck in
`ASSESSING`. This guard is not known to fix that, and it should not be cited as its
resolution: `_retry_missing_rows` already broke on no progress (both the retry and
escalation rungs), and the wall-clock deadline guard plus its threading from
`context.get_remaining_time_in_millis()` in the Assessment Lambda were already
present in the release where those timeouts were observed.

The one ladder path that the wall-clock guard did **not** cover — the most concrete
lead on that timeout — has since been closed (#958): the same-model retry **round**
loop had no round-level deadline check, only the escalation loop and further
bisection did, so a sequence of rounds that each recovered *something* while never
bisecting was bounded only by `max_retries`, at a real model call per round. All
three rungs now consult the deadline before starting work. Whether that was the
cause of the observed 900s timeouts is still **not established** — no reproduction
exists.

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
| A single row still truncated the model — no batch size can fit it (#894) | `assessment_row_too_large` | **error** |
| Rows still unscored after the full ladder | `assessment_incomplete` | **error** |
| Wall-clock guard stopped recovery, but every row ended up scored anyway | `assessment_deadline_reached` | **warning** |
| Wall-clock guard stopped recovery with rows still unscored | `assessment_incomplete` (message names the time budget) | **error** |
| Healed, but needed shrinking/escalation | `assessment_recovered_with_retries` | **info** |

`assessment_schema_mismatch` takes precedence over `assessment_incomplete`: when
both fire, the schema mismatch is the true root cause and is emitted alone.
`assessment_row_too_large` ranks next for the same reason — the generic "rows could
not be scored" message sends an operator to shrink `list_batch_size`, which is the
one remedy that provably cannot work when the batch was already a single row.

A **completeness gate** (`audit_explainability`) runs after the ladder on both
the standalone and in-shard paths: it confirms every extracted value has a real
(non-null), in-range confidence and — when `geometry.mode != "off"` — a bounding
box, emitting `assessment_confidence_out_of_range` / `assessment_geometry_incomplete`
for anything structurally wrong.

The geometry half asks a **different question of a list row than of a scalar
field**, and the asymmetry is deliberate:

| Shape | Reports when |
|---|---|
| List row | **any** scored row holding a populated value carries no box |
| Scalar / group field | **no populated field** in the section carries a box |

Both halves test the **leaf's own** value, so a scored but empty cell counts for
neither: an absent value has no place on the page for a box to sit, and a missing
box is the correct outcome rather than a finding. ⚠️ That narrows the list half as
well — a section whose only box-less scored leaves sit in empty cells reported
before and is silent now. Under `ocr_only`, neither half reports when no page carried
OCR geometry: that mode derives every box from value-matching, so a box-less leaf
means no match was attempted rather than that one failed. Both callers — the
assessment service and the extraction service — pass that fact through.

⚠️ The suppression is scoped to `ocr_only` because only there is the premise true.
Under `llm_grounded` the model emits the boxes and OCR only corrects them, so a
box-less leaf on a backend carrying no geometry is a genuine model omission — and
it still reports, with `root_cause` naming the model rather than the match.

The scalar side was counted in neither half before
([#1520](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1520)) —
the counter lived inside the `isinstance(data_val, list)` branch — so the rung
could not fire for a section whose fields are all scalars, which is most of them.
Measured over 571 real sections: **no** list leaf was scored-but-box-less, while
495 scalar ones were, across 222 sections. The rung was counting the case that does
not arise and was blind to the one that does.

⚠️ **The scalar threshold is "nothing was located", not "something is missing",
and loosening it is not the harmless improvement it looks like.** Reporting per
leaf fires on 40% of real sections, and the largest single contributor is
`Language` — values like "English" — followed by `LetterType`, `MemoType` and
`FormType`. Those values are the model's characterization of the document rather
than text on the page, so no box can exist for them and their absence is the
correct outcome rather than a defect. An `info` note that is usually about nothing
teaches an operator to ignore the surface. "No populated field in this section
carries a box" is a different statement and worth making: the highlight feature
produced nothing there at all, which a reviewer opening the section sees and cannot
otherwise explain. It fires on 2% of the same corpus, and the shape mirrors the
`scored_fields <= 0` condition one rung down so the two read alike.

### Omitted fields, as distinct from unscored rows

The recovery and reporting above are built around list **rows**, and a populated
**non-list** field the response omitted outright was invisible to all of it
([#1474](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1474)).
Three separate mechanisms each stopped short of it, which is why a section could
record confidence for 3 of its 6 populated fields and still report unqualified
success with a tick beside Confidence:

- `reconcile_assessment_to_data` padded list fields only, so the field got no
  placeholder — and since geometry grounding attaches a box per confidence leaf,
  no leaf meant no bounding box either.
- Both recovery call sites gate the ladder on there being a list field to retry
  rows *of* (`if all_list_fields:` in `assess_results_batched`; an early return
  whose own comment said "scalar-only classes always land here" in the integrated
  path), so this shape reached **no** rung at all.
- The gate's scalar branch range-checked the leaves it *found*, and an omitted
  field yields none. The coverage rung below it could not reach the section
  either: its denominator counts list rows and it is gated on that being
  non-zero, so a form with no list attribute has a zero denominator.

The cause measured was not truncation — the response was valid JSON carrying the
first three keys, and the same model on the same class answered about all ten
fields for the next section of the same packet. So `_retry_missing_scalars` is one
same-model rung asking about only the still-missing fields, bounded by
`max_retries` and stopping on a round that recovers nothing; it does **not**
escalate, because `escalation_rounds` is a bound shared with the row ladder and
the evidence is that the same model can answer. Whatever survives it is reported
as `assessment_fields_unscored` (**warning**; **error** when no populated field in
the section is scored) and counted in `split_stats` as `scalar_retry_calls`,
`fields_recovered_by_retry` and `unrecoverable_fields`.

Two design points worth not re-deciding by accident:

- **The findings stay out of `gaps`.** That dict is keyed field → row *index* and
  its lengths are the numerator `coverage_from_gaps` divides by a list-row
  denominator, so a scalar entry there makes `unscored_rows` exceed
  `expected_rows` and drives `scored_rows` negative. The coverage denominator is
  therefore **unchanged**, and `conf_coverage` still means what the benchmark
  grids measured; whether it should also count scalar leaves is a separate
  decision with a published-metric consequence.
- **Scalar padding belongs to the final assembly only.** `reconcile_assessment_to_data`
  takes `pad_missing_scalars`, and the intermediate reconciles reach it through
  `_reconcile_rows_only` with padding off. Those callers hand their non-big fields
  on as a slice's `scalars`, and the batched path takes the first slice whose
  `scalars` is non-empty — so padding per chunk would let a first chunk that
  omitted a scalar shadow a later chunk's real score with a null placeholder,
  losing data the pipeline already had.

It also reports **partial coverage**
([#901](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/901)
item 3). The gate already computed which rows carry no confidence, but every
caller discarded that, so a section could return a fraction of its rows scored and
still report unqualified success (one run scored 1,146 leaves where 5-page shards
scored 4,807 — ~24% coverage, no issue raised; the *reason* coverage fell rather
than failing was never established, and this issue makes no claim about it). When
unscored rows reach **5%** of the extracted list rows **or 10 unscored rows in
absolute terms**, whichever comes first, the gate emits
`assessment_coverage_incomplete` — **warning** up to 25% unscored, **error** at or
above 25% **and** at least **10 unscored rows**, and **error** at any list length
when *no* row was scored — carrying `expected_rows`, `scored_rows`,
`unscored_rows` and a per-field breakdown of both the unscored rows and the
expected ones.

Five deliberate choices in those thresholds:

- **5%, not 0%.** Reconciliation pads one assessment entry per extracted row, so a
  run whose model scored every row lands at 0%. Small residual shortfalls are also
  already named precisely by `assessment_incomplete`, so a 1-2 row gap on an 800-row
  table is that issue's business rather than a second document-level alarm. What the
  fraction means on a *short* list is arithmetic and worth stating outright: a single
  unscored row is a shortfall of `1/N`, so **one unscored row fires the warning for
  any section totalling 20 list rows or fewer**. Whether a healthy run ever produces
  that single unscored row is measured, not reasoned, and the answer is that it does
  not — see *Coverage: measured* below.
- **An absolute ceiling of 10 unscored rows on the warning rung.** The 5% above is a
  share, and the second half of its justification — "a 1-2 row gap is
  `assessment_incomplete`'s business" — is not: it names an absolute quantity, which a
  ratio then scales with the document. Expressed only as a fraction the rung passes 39
  unscored rows in silence at 800 rows and 159 at 3,200. So the shortfall is read both
  ways and whichever rung speaks first wins. The ceiling is the error rung's absolute
  floor read in the other direction rather than a second tuned number, and what it
  costs is measured — see *The shape of the warning fraction, and the ceiling under
  it* below.
- **An absolute floor of 10 unscored rows on the error rung.** A fraction alone makes
  short lists fire hardest: one unscored row in a four-row list is 25%, and an error
  renders the section red ("Incomplete") in the Sections panel. Short list attributes
  are ordinary (a two-entry `ENDORSEMENTS` array in `lending-package-sample`). Below
  the floor the shortfall is still reported, as a warning. The floor bounds this
  class for the error rung only; the warning rung still fires on a short list.
- **The floor does not apply when nothing was scored.** `scored_rows == 0` is not a
  short list with a gap in it — the section has no confidence surface anywhere, and
  the message the gate emits at that point says confidence-based review "does not
  apply to any of it". Over the committed grids, every run that crossed 25% with
  fewer than 10 unscored rows — the class the floor exists to hold at warning — had
  `scored_rows == 0`, and the partial case the floor was written for occurred zero
  times, so the floor as shipped only ever downgraded a total absence of coverage.
  Zero scored rows is therefore an error at any list length, and the floor keeps
  governing the partial shortfalls it was argued for. No constant was added: this is
  an exact boundary the gate already computes.
- **Suppressed when the ladder already reported an error.** `build_assessment_issues`
  and this gate run on the same section, and the ladder's error rungs describe the
  same unscored rows *with a cause attached*. Emitting both doubles
  `ProcessingIssueCount` with two counts that can legitimately disagree
  (`unrecoverable_rows` tracks only the largest list field; this gate counts every
  list field), and it would break `assessment_schema_mismatch`'s "emitted alone"
  contract by appending a coverage note under it. Callers pass the ladder's issues in
  as `audit_explainability(..., ladder_issues=...)`; a `warning`/`info` ladder issue
  does not suppress it. The absolute ceiling widens that last overlap, and it is
  accepted rather than overlooked: a shortfall of 10 rows or more on a long list now
  emits a coverage warning where the fraction alone was silent, so a section can
  carry both a `warning`-severity `assessment_incomplete` and a coverage warning
  about the same rows. The double-count argument above is about an **error** the
  ladder has already diagnosed; two warnings naming the same rows cost a reader
  nothing they cannot reconcile. How often the pair actually occurs is not answerable
  from the grids measured below — 6 runs cross the ceiling without crossing 5%, and
  `processing_issues` is null on every one of those grids, so whether any of the 6
  also carried a ladder warning is unrecorded.

Issues are attached to each `Section`
(`section.processing_issues`), rolled up to `Document.processing_issue_count`,
and rendered in the extraction processing report.

#### Coverage: measured

`confidence_coverage(assessment, extraction_results)` returns the figure —
`scored_rows` over `expected_rows`, plus the per-field breakdown — for one section.
It calls `audit_explainability` rather than re-deriving "is this row scored?", so a
measurement and the guard that ships are one computation; `audit_explainability`
puts the very same dict in the issue's `details`. Use it whenever you want the
number unconditionally, because the issue only carries it once one of the rungs has
fired, which is the reason nothing recorded coverage on healthy runs before it
existed.

`expected_rows == 0` (a section with no list attribute) means coverage is
**undefined**, not perfect: `scored_fraction` is `None` so an aggregate drops it. A
corpus mean that scored those as 1.0 would be reporting the share of list-free
documents. The gate declines to fire there for the same reason — there is nothing to
score, which is an extraction finding rather than a coverage one.

⚠️ **An undefined denominator means there was no list to walk, and that is not the
same as nothing having gone wrong.** The walk counts the rows of list-valued
attributes, so it reports no denominator precisely when the attribute holds no
list — which happens both on a document that legitimately has no rows and on one
where the model returned something else entirely in a field the schema declares as
an array. The coverage rung cannot tell those apart and is right to stay quiet in
both: the finding belongs to extraction, and
`extraction_list_field_not_a_list` is what reports it
([#1505](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1505)).

The worked case is worth knowing because the figure beside it misleads. Four
`coresynth__extraction-model-sonnet55` runs on `med_narrow.pdf` carry an undefined
denominator while `rows_extracted` reads **2 of 400** — which looks like rows the
walk could not see, and is not: `Transactions` came back as a prose refusal naming
`SEQ00000` and `SEQ00399`, and the benchmark scorer's identifier scan counted those
two quoted ids as rows. Zero rows were extracted, so `expected_rows == 0` is the
correct answer and the row count is the wrong one. ⚠️ **Do not read a non-zero
`rows_extracted` beside an undefined denominator as a coverage gap** — check what
the field actually holds first.
`benchmarks/tests/test_coverage_threshold_evidence.py::test_an_undefined_coverage_run_extracted_nothing`
carves those four out by grid **and** count, so a fifth instance or the same shape on
another model fails there, and
`test_row_identity_collapse.py::test_a_prose_refusal_inflates_the_row_count`
executes the scan to show where the 2 comes from.

The benchmark harness records it per document
(`benchmarks/harness/analyze.py::score_confidence_coverage` → `conf_rows_expected`,
`conf_rows_scored`, `conf_rows_unscored`, `conf_coverage`,
`conf_unscored_by_field`, `conf_rows_expected_by_field`), and `aggregate.cell_stats`
rolls up the **distribution** per cell — min, max, stdev and CV, not only the mean,
because a mean of 0.99 is equally consistent with every document at 0.99 and with
one document at 0.

##### The observed distribution

Computed over every committed `benchmarks/results/*/*/summary.json` row that
carries the metric — **10,240 coverage-bearing document-runs across 150 grids,
v0.6.8 → v0.6.12**, on Claude, Nova and GPT extraction and confidence models — and
re-derived on every test run by
`benchmarks/tests/test_coverage_threshold_evidence.py`, which classifies each
recorded `(expected, unscored)` pair by calling `audit_explainability` itself rather
than re-stating the rungs:

Shares are over the **10,033 runs where coverage is defined**; the other 207 record an
undefined denominator and are counted in neither column (see the warning above for what
that does and does not mean).

| Shortfall | Runs | Share |
|---|---|---|
| exactly 0 unscored rows — no issue | 9,769 | 97.37% |
| above 0 but below both warning rungs — no issue | 200 | 1.99% |
| the gate fires | 64 | 0.64% |
| …of those, at **warning** severity | 27 | 0.27% |
| ……reported by the 10-row ceiling alone, under 5% | 6 | 0.06% |
| …of those, at **error** severity | 37 | 0.37% |
| ……by the fraction **and** the 10-row floor | 9 | 0.09% |
| ……by having no scored row at all | 28 | 0.28% |

⚠️ **The residual the warning rung leaves is now bounded in rows, not proportional.**
Before the ceiling, the largest shortfall this rung said nothing about was **115
unscored rows of 3,200** (3.6%) on `scale_3200.pdf`, against 24 of 800 (3.0%) when it
was last measured — the share barely moved while the row count grew 4.8×, because the
document grew 4×, which is what a purely multiplicative rung does. With the 10-row
ceiling the largest silent shortfall over this whole corpus is **6 unscored rows of
400** (1.5%), and it cannot exceed 9 whatever the list's length
([#1495](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1495)).

The two error paths are disjoint on this corpus, and together they account for
every run at or above 25%. The median, p90 and p95 are all exactly 0; p99 is
0.0164 and the maximum is 1.0.
So the premise the rungs were argued from — a healthy run lands at 0% because
reconciliation pads one assessment entry per extracted row — holds as an observed
property, not only a code-path reading.

**On short lists, which is the regime #997 asked about, the warning rung has not
been observed firing on a partial gap at all.** 3,511 runs extracted
20 rows or fewer, the range where one unscored row crosses 5%. The number of those
showing a *partial* shortfall — some rows scored, at least one not — is **zero**.
The 28 that do fire there have `scored_rows == 0`: no confidence anywhere in the
section, each one `realkie`'s `LineItems` with the document's scalar block scored
and the list not. Those are the strongest true positives the gate can report, and
they are why `scored_rows == 0` is now an error rather than being held at warning by
the absolute floor.

Two artifact checks worth repeating before reading any of the above as data. The 21
total-shortfall runs each carry six confidence leaves and a mean confidence near
0.97, so the assessment *existed* and simply scored no list row — the figure is not
a missing-artifact artifact. And none of the out-of-range `mean_confidence` rows
identified in #1395 and #1411 is among the coverage outliers: six of them fall in
coverage-bearing grids and all six sit at exactly 0 shortfall.

##### What the denominator counts, and what it does not

`expected_rows` counts rows of **top-level list-valued attributes** of the section's
`inference_result`, and a row counts as scored exactly when
`_row_confidence_missing` says so — which requires **every** confidence leaf in the
row, including leaves inside a nested group or an inner list, to be non-`None`. That
is stricter than a per-cell ratio, not laxer: a column that is `None` on every row
makes every row unscored and reads as 0% coverage, where a cell-counting metric
would score the same section at `(cells − 1) / cells`. A list nested *inside* a
group attribute is not a top-level list and so contributes to neither the numerator
nor the denominator.

##### The shape of the warning fraction, and the ceiling under it

The 5% rung is multiplicative and one half of its justification is not: "a 1-2 row
gap on an 800-row table is `assessment_incomplete`'s business" names an **absolute**
quantity, which a ratio then scales with the list. A rung expressed only as a
fraction therefore leaves a silent band proportional to the section — 0 rows at
`N ≤ 20`, 19 at `N = 400`, 39 at `N = 800`, 159 at `N = 3,200`, 199 at
`N = 4,000` — and that band was observed growing exactly that way: the largest silent
shortfall in the committed grids was **24 unscored rows of 800** (3.0%) until a
3,200-row document was benchmarked, at which point it was **115 of 3,200** (3.6%).
The count grew 4.8× on a 4× larger document at a near-identical share, so 115 is the
same state of the world as 24 rather than a worse one
([#1495](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1495)).

The additive backstop the sentence relies on is real but narrower than it reads:
`split_stats["unrecoverable_rows"]` is computed for the **largest** list field only,
so a residual confined to a smaller list attribute reaches neither
`assessment_incomplete` nor — being divided by the section-wide total — the 5% rung.
`expected_rows_by_field` exists so a recorded run can be read for that case.

So the warning rung reads the shortfall in absolute rows as well, at a ceiling of
**10 unscored rows**, and the band is now bounded at 9 rows whatever the document's
size. The number is **derived, not tuned**:
`_COVERAGE_SHORTFALL_ERROR_MIN_UNSCORED_ROWS` already names "more unscored rows than
a reviewer can be assumed to spot-check by hand", which the error rung reads in one
direction (below it, a large proportion of a tiny list must not render a section red)
and the warning rung needs in the other (at or above it, that much unreviewable data
is worth saying whatever the list length). This is the move
[#1361](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1361)
made for the sibling check in `extraction/service.py`, where `_OCR_ROW_GAP_MIN` is
defined as `_OCR_ROW_ESTIMATE_MIN` for the same reason.

What the derived value costs is measured over the same artifacts, at 10,033 runs with
coverage defined once the v0.6.12 grids are included (PR #1487, where the 3,200-row
document lives). 206 runs sit in the silent band, distributed by unscored rows as 1
(62 runs), 2 (18), 3 (94), 4 (17), 5 (7), 6 (2), 12 (4), 24 (1), 115 (1) — **empty
between 7 and 11**, so the data cannot distinguish any ceiling in `[7, 12]` and the
derived 10 falls inside that band. The curve of what each candidate would newly
report:

| Ceiling | Newly reported runs | Share of defined runs | Warning-rung firing rate after | Largest silent shortfall after |
|---:|---:|---:|---:|---:|
| 3 | 126 | 1.2559% | 1.4652% | 2 |
| 5 | 15 | 0.1495% | 0.3588% | 4 |
| **10** (shipped) | **6** | **0.0598%** | **0.2691%** | **6** |
| 30 | 1 | 0.0100% | 0.2193% | 24 |
| none | 0 | 0 | 0.2093% | 115 |

The mass is at 1–4 unscored rows, which is the band the fraction deliberately leaves
to `assessment_incomplete`, so a ceiling of 5 would buy a 4-row band at 2.5× the new
firing and one of 3 a 2-row band at 21× it, where 10 closes the part that grows with
the document for 6 runs. 30 — the sibling check's own constant, `_OCR_ROW_GAP_MIN`,
which leaves it a silent band of 29 rows — would leave the 24-of-800 shortfall
silent; that one absorbs an additive OCR imprecision (a column heading reprinted per
page) and this one absorbs nothing comparable, because reconciliation pads one
assessment entry per extracted row and healthy runs land at exactly 0.

Severity at the ceiling is always `warning`, structurally: a run crossing the ceiling
without crossing 5% is below the 25% error fraction by arithmetic, and
`scored_rows == 0` implies a shortfall of 1.0, so neither error path is reachable
from the ceiling alone. `extraction.row_shortfall_action` does not govern this code
either, so no deployment can turn it into a failure.

One limit on every firing share above. `processing_issues` postdates every grid
measured here (null on all rows), so the recorded shortfall is the symptom without
the product's verdict beside it, and the shares are an **upper bound** on how often
the gate actually spoke — `_ladder_reported_error` suppresses it whenever the ladder
got there first. The `processing_issues`/`processing_issue_codes` columns added in
#1378 answer this directly on the next grid, at no extra cost.

##### The other limits on the figures above

- **A document-level ratio can dilute a per-section one.** The harness sums over a
  document's sections; the gate runs per section. 9,992 of the 10,033 runs are
  single-section (or record no section count), where the two coincide, and a 0
  aggregate implies 0 in every section — so the 97.37% figure is exact rather than
  averaged. For the 264 nonzero runs the recorded ratio is a *lower* bound on the
  worst section's.
- **No multi-record corpus has been run.** The measured row shapes are flat (three
  or eight scalar cells from
  `benchmarks/corpus/generators/bank_statement.py::_row`, plus `realkie`'s
  `LineItems`). The nested-row shape that the any-leaf-`None` rule is hardest on —
  and that a multi-instance section (#715) makes universal — is covered by
  `test_a_healthy_nested_row_is_scored_not_unscored` rather than by a corpus. That
  unit test is what retired the sharpest form of the original concern:
  `_row_confidence_missing` recurses, so the known-awkward pay-statement shape
  scores clean, and the 100%-unscored figure quoted for it belongs to the one-level
  implementation that rule replaced.
- **Pre-v0.6.8 grids carry no row denominator**, which is why the corpus starts
  there: `conf_rows_expected` was added with the instrument, and the earlier
  artifacts retain only `n_conf_leaves`.

### Lambda wall-clock budget & resume safety

The primary confidence pass and the escalation ladder behind it both spend model
calls inside the 900s Extraction/Assessment Lambdas. To avoid a hard timeout, both
handlers thread the Lambda's `context.get_remaining_time_in_millis()` down as an
absolute `deadline_epoch`. Every path that can spend a model call consults it
against the remaining time minus a 90s safety reserve, pricing one call at
`_ESTIMATED_MODEL_CALL_SECONDS` (60s):

| Where | Granularity | Price of one call | Added |
|---|---|---|---|
| Every batch of the primary pass's sequential loop (`assess_results_batched`) | per batch | flat 60s | #1488 |
| The cache-warming batch that precedes a concurrent fan-out | per pass | flat 60s | #1488 |
| Each wave of the concurrent fan-out (up to `max_concurrent_batches` batches) | **per wave** | flat 60s | #1488 |
| Further bisection of a truncated slice (`_assess_slice_adaptive`) | per split | `max(measured, 60s)` | 1.5 |
| Every recovery call, retry **and** escalation (`_splice_missing_rows`) | **per chunk** | `max(measured, 60s)` | #958 |
| Before an escalation *round* (`_retry_missing_rows` rung 2) | per round | flat 60s | 1.5 |

**60s is a floor, not a ceiling.** Which rows have more than the floor to work
with is a property of where the check sits.
The two `max(measured, 60s)` rows have a just-completed
call of their own to measure — the call that truncated, and the previous recovery
chunk — and they floor it because the next call is the one that matters and these
guards exist for the slow shapes. The four flat rows have no measurement in hand:
the primary pass's first batch has made no call yet, and the fan-out's waves and
the escalation round are priced before they start. On those paths under-pricing a
slow model is self-correcting rather than unbounded, because each one re-reads the
clock before its next dispatch, so a model that ran long is charged in full on the
next decision.

When a check fails the pass or the ladder stops, keeps everything already scored or
recovered, and sets `deadline_reached` — converting a would-be timeout into a soft,
flagged, complete document. On the primary pass the batches it does not run are
always a **trailing suffix**, because rows accumulate positionally and dropping a
middle batch would slide later rows' confidence and bounding boxes onto earlier
rows.

The granularity column is the honest unit: the check bounds a **dispatch unit**,
not a call. Every path reaches the model through `_assess_slice_adaptive`, whose
first call is unconditional once the unit has been cleared, and a truncating model
bisects from there — each further split is checked, but a cleared split makes two
calls. So a chunk the model keeps truncating can finish roughly (bisection depth ×
one call) past the point it was cleared for. That residual is deliberate; pricing a
prospective bisection tree would reintroduce the count cap described next.

**Why none of these checks prices more than one call at a time.** A check that
covers a whole round, or a whole fan-out, has to price it before making any of its
calls, and the only safe price is a worst case — chunks × 60s. That is not a bound
on time, it is a cap on chunk COUNT: with 800s remaining it refuses any round past
11 chunks however fast the model actually is, which for a 200-row list at a
token-aware batch size of 4 discarded recovery that measurably took under a second
and turned a clean section into an error-severity `assessment_incomplete`. Pricing
one call at a time bounds the wall clock exactly and keeps every row the budget did
cover. The concurrent fan-out is held to the same rule, which is why it dispatches
in waves and re-reads the clock before each: a wave runs concurrently so it costs
about one call, and calls that return faster than the estimate leave the budget
intact, so the next wave is allowed on the evidence instead of refused on the
guess. The price of the wave boundary is the straggler — a wave is only as fast as
its slowest call, where one continuous `pool.map` refills a free worker the moment
it frees up — which buys the observation point that makes the budget
self-correcting. That price is paid only where there is a budget to observe: a
caller with no `deadline_epoch` (a local run, a notebook) keeps the single
continuous map, because for it the guard is inert and there is nothing to re-read.
The rule also treats the rungs alike: before #958 the cheap
same-model rung could be refused while the slower, dearer escalation rung
proceeded, because the two sized their chunks differently.

**What an operator sees.** With rows still unscored the section reports
`assessment_incomplete` (**error**) whose message names the time budget and its
remedy — more Lambda time, or less work per section. It does **not** report
`assessment_deadline_reached`: that rung sits below `assessment_incomplete` in the
severity ladder and the guard only fires while rows are missing, so the warning is
reachable only when a later rung went on to score every row anyway.

Per row, the text an operator reads beside the empty confidence in the UI names
the same cause: a row whose batch was never dispatched carries
`_UNASSESSED_DEADLINE_REASON` ("the Lambda invocation time budget was reached
before this row's confidence batch was dispatched"), where a row the model was
asked about and answered short carries `_UNASSESSED_SHORT_RESPONSE_REASON`
("assessment returned fewer items than were extracted"). The boundary is exact
rather than inferred — everything past the last dispatched chunk is the
never-dispatched suffix — and the distinction matters because the two have
different remedies: the first is the time budget, the second is the model's
output budget.

`batch_count` in `metadata.assessment_batch_split_stats` counts the batches that
were **dispatched**, not the batches that were planned, which is how it stays
consistent with `deadline_reached`; the processing report renders it as
"Confidence list batches: N" and the UI as "ran in N batch(es)". `0` is the
honest value for a pass whose cache-warming batch was already refused. Note that
`assessment_parsing_succeeded` is about whether the model's JSON could be *read*
and is `True` on such a pass — it is not evidence that any call was made.

As defense in depth, the Step Functions
`ExtractionStep`/`AssessmentStep`/`ShardExtractionStep` retry sets include
`States.Timeout` / `Lambda.Unknown`, so a genuine timeout is retried and resumes
via the per-shard S3 persistence and the Assessment step's "skip if
`explainability_info` already present" short-circuit.

### What to alarm on when a section ends up with no confidence at all

The rungs above degrade *within* a successful pass. A section can also come out
of this module with **no confidence scores whatsoever**, and there are two ways
that happens. Both live in `assessment/degradation.py`, and both leave the same
trace, by construction rather than by convention:

| Cause | Entry point | Issue code |
|---|---|---|
| The pass ran and failed **deterministically** — the confidence model rejecting the input outright, most often `ValidationException: Input is too long for requested model.` (#901) | `degrade_section_to_no_confidence`, called from the Assessment Lambda's non-transient branch | `assessment_failed_confidence_unavailable` |
| The pass **never ran**: the section had no `extraction_result_uri`, no `page_ids`, an extraction result whose `inference_result` was empty, or none of its pages present in the document (#1006) | `skip_section_no_confidence`, called from `process_document_section`'s early returns | `assessment_skipped_confidence_unavailable` |

Neither fails the document. For the first that is #901's deliberate trade — the
extraction already succeeded and was already paid for, so losing the advisory
half must not discard it. For the second there is nothing to fail *over*: the
confidence model was never called.

That has an observability consequence worth knowing when reading this module's
behaviour operationally. Because such a document **completes**, a systemic
confidence gap moves none of the failure alarms — no failed executions, no
DLQ messages — and `ProcessingIssueCount` is a DynamoDB attribute rather than a
metric, so nothing aggregates it. Both paths therefore publish
`AssessmentConfidenceUnavailable` (value 1 per section) into the parent stack's
metric namespace, and the parent template alarms on ten or more in fifteen
minutes — deliberately on volume, because one such section is an expected outcome
and a steady stream is not (issue #996). **The metric does not distinguish the two
causes**, because the alarm's question is whether sections are coming back without
confidence; the issue `code` and `root_cause` on the section are what separate
them for whoever opens the document.

Two details that are load-bearing rather than tidy. The metric put is wrapped in
its own `try`: these paths exist to avoid failing a document whose extraction
succeeded, so a lost telemetry point is the cheaper failure. And appending to
`document.errors` is **not** a substitute for the issue —
`processresults_function` reads a section document's `errors` only inside its
`Status.FAILED` branch, so on a completing document nothing reads it, which is
exactly how the skip paths stayed silent before #1006.

Three returns from `process_document_section` deliberately record nothing, and
getting that set right is what keeps the alarm's volume threshold meaningful:

- a section whose class is **excluded** — extraction never ran, so no confidence
  is missing;
- a section whose extraction result is flagged `skipped_due_to_empty_attributes`
  *and* whose `metadata.empty_schema_reason` is one of the two deliberate causes
  below. `ExtractionService` skips the model for an empty effective schema
  (`_handle_empty_schema`) and still sets `extraction_result_uri`, so the stub
  arrives here with an empty `inference_result` and is indistinguishable from a
  real gap without the flag;
- a configuration with `extraction.confidence.enabled: false`.

That second bullet covers three situations, which extraction distinguishes at the
producer (`idp_common.empty_schema`) because only one of them is a fault:

| `empty_schema_reason` | What it is | Reported here? |
|---|---|---|
| `class_has_no_attributes` | The class is in configuration and declares no attributes — an authoring choice. Nothing was expected from the section | No |
| `class_unclassified` | Classification determined no class, so the label is the `unclassified` sentinel: a blank page, a page whose classification failed, or a deployment with no document types configured. Routine — one cover sheet in an otherwise normal document lands here, and a dozen such documents in fifteen minutes would clear the default alarm threshold on their own | No. The **classification** stage records it, with the severity only it can judge, and a `root_cause` written from here would send the operator to an Extraction step where nothing is wrong |
| `class_not_configured` | The section carries a **named** class the configuration in force does not contain: renamed or deleted while documents were in flight, or an old document reprocessed under a newer configuration | **Yes**, with a `root_cause` naming the configuration rather than the confidence model. Extraction records the extraction-stage half (`extraction_class_not_configured`) at the same time. Bounded by operator configuration changes rather than by document content, so it cannot reach the threshold on throughput |

A stub written before `empty_schema_reason` existed carries no reason at all and
is read as `class_has_no_attributes`, so stored results keep reading back exactly
as they did — and reassessing a backlog of old documents cannot manufacture a
fleet of new alarm points.

An empty `inference_result` **without** the flag is still reported: the class had
a schema, the model returned nothing, and those values now have no confidence.

A `section_id` that is not in the document, or a document with no sections,
**raises** instead: there is no section on which to record anything, so a quiet
return would leave the caller with no signal at all.

**Pages the document does not contain.** The page loop skips a `page_id` absent
from `document.pages`. When *every* page of the section is absent there is no page
text and no page image, and the confidence pass used to run anyway and return a
score per field derived from nothing — a fabricated number, indistinguishable in
the UI, in HITL routing and in the reporting lake from one the model read off the
page. The pass is now skipped for that case, through
`skip_section_no_confidence` like the others. When only *some* pages are absent it
still runs — partial evidence is not no evidence — and records a warning-severity
`assessment_pages_missing` issue naming the absent pages. That one publishes **no**
metric: the section does have confidence scores, so the metric's question is
answered no.

See [Monitoring](../../../../docs/monitoring.md#confidence-assessment-degraded).

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
  **number found only inside a longer number** → multi-line span (boxes unioned) →
  same date/number/phone by type → line fragment covering at least half the value →
  token-overlap fuzzy (≥ 0.6) → character similarity (≥ 0.8) → **fragment covering less
  than half the value**, ranked last.

  That last tier is the whole of
  [#1519](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1519).
  A fragment used to rank as an ordinary line-fragment match with no size
  requirement, which put boxes on a single digit for a form id, on a single letter
  for an author name, and on a different author entirely. It also *suppressed* the
  right answer: any candidate short-circuits the fuzzy and character passes, so
  where OCR had misread one character inside a sentence, a 29%-coverage column
  header won and the line resembling the whole value — 0.9706 similarity — was never
  computed.

  **It is demoted rather than dropped, and that is measured.** Dropping sub-half
  fragments removes 14 wrong boxes and 48 right ones: most of them point at a value
  that genuinely is on the page, spread over several lines (a three-line address, a
  specification assembled from consecutive rows). No absolute-size rule does better
  than break-even, because short real content is indistinguishable from short junk —
  the property that separates them is spatial. Ranking the fragment below the
  whole-string tiers instead loses **no** box and moves 10 onto a better line, over
  50 measured sections.

  ⚠️ The cost is real and is paid elsewhere: the character-similarity pass now runs
  whenever no whole-string candidate exists, and it is an O(n·m) dynamic program per
  line. `_levenshtein_can_reach` is an **exact** O(1) ceiling in front of it
  (`similarity ≤ min(len)/max(len)`, computed over the same stripping the scorer
  applies), without which grounding went from 4.6s to 25.4s over those sections, 98%
  of it in that one function. Keep the guard if you touch the tiers.

  The **number inside a longer number** tier (`_TIER_SUBSTRING_EMBEDDED`) is
  [#1565](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1565).
  A number value is searched in several surface forms (`75.0` also as `75` and
  `75.00`), and a short form is contained by coincidence in any longer number — `75`
  in an invoice id `08337510`, `6.0` in `126.00`. As an ordinary value-in-line hit it
  tied with the line that prints the value, so reading order or proximity to the LLM
  box chose between them. A hit now counts as a whole token only when neither
  neighbour continues a digit run: an ASCII digit, or a `.` `,` or date `/`
  followed by a digit, joins the run; a space, `$`, `%`, a unit, a sign, a hyphen, a
  colon, a ratio's slash or closing punctuation does not. Three refinements keep
  correct boxes that rule alone would demote:

  - A comma followed by one or two digits is a **decimal comma**, so `1250` in
    `1250,00` is whole. No surface form renders a decimal comma, so for a European
    amount the integer part is the only form that matches at all.
  - An occurrence inside a longer number that *is* the value with more zeros
    (`075.00`, `75.000`, an item number `0075`; compared unsigned) is whole.
  - Only a **date's slash** joins: two slashes (`12/17/2024`), or one beside a
    four-digit year (`12/2024`). `120/80`, `75/100`, `10/325` and `3/12` print
    numbers that are values in their own right, so their slash is a boundary.

  **It is demoted by exactly one place, not rejected.** A digit run holding the value
  is sometimes the right line — the model dropped the cents (`272243` for
  `272243.34`) or misread one digit of a long number — and there the box is how a
  reviewer finds the error. Placed directly below value-in-line and above span, the
  tier changes one comparison only: a whole-token line beats an embedded one. With
  nothing better the embedded line still supplies the box, and the same passes are
  skipped as for any value-in-line hit. A date's slash is in the joining set because
  a date component is not a free-standing number; without it, `17` for `$17.49` found
  a "whole" `17` inside `12/17/2024`. A colon (times) was measured in the joining set
  as well and did worse, so it is a boundary.

  ⚠️ **The rule reads one line at a time, so it has a known cost.** Where the right
  line holds the value embedded and another line prints the same digits whole, the
  whole line now wins even when the LLM box points at the right one. That is the
  intended outcome for a coincidence, and the wrong one for three shapes the rule
  cannot tell apart from it: a list written without spaces (`20` in `10,20,30` reads
  as part of one number), a value that is itself a date component (a month `12` in
  `12/2024`), and a value the model truncated (`14` for `14.8` beside `14 days`).
  The date-slash test also accepts a residual: a ratio written in a date's shape
  (`1/1000`, `FY 2024/25`) is read as a date, so in a section that also prints a
  date with a slash suffix it can lose its box to that line.

  **Measured on lines derived from the PDF text layer of the repository's sample
  documents, not on Textract output.** Over the text-layer PDFs in `samples/` (97
  pages, cut into sections of up to 3 pages), every printed number of two or more
  digits outside a date was grounded as a value, as a string and as a float, with
  `schema_hint={"type": "number"}`, plus two perturbed sets — decimals dropped, and a
  long number missing a digit — that model values no line prints. With no
  disambiguator 1,608 boxes moved onto a line printing the value and 4 moved off
  one; with a noisy LLM box (proximity), 70 moved on and 31 off. Every loss was a
  perturbed value. That zero for printed values is weak evidence on its own: the
  measurement judges a box by whether its line prints the value as a whole token,
  so it cannot tell two lines printing the same number apart, and its value set has
  no space-free lists, date components, ratios, European decimal commas or
  zero-padded amounts. Those shapes are pinned by the tests in
  `test_ocr_grounding.py` instead. The harness was ad hoc and is not kept in the
  repository.

  `geometry_source` changes only where a line holds the value's own form embedded
  and another of its forms whole (`1250` against `Ref 12500 total 1,250`): the box is
  the same, and the tag now names the form that matched whole, `"ocr-normalized"`.
  Over the same sample documents (all but the 17-page fund factsheet, left out of
  that pass for time) none of 11,466 unchanged boxes changed tag.
- **Spatial disambiguation** of repeated values: when a value matches multiple lines, the
  candidate nearest the LLM-estimated box wins; with no usable reference box the field keeps
  its LLM box (so identical amounts across table rows don't collapse onto one line).
- **Coordinates stay 0–1**: `pageData` geometry is already normalized, so grounded boxes skip
  the 0–1000 → 0–1 rescale that LLM boxes go through. No mixed scales in `explainability_info`.
- **Additive output**: a matched field gets `geometry_source`
  (`"ocr"`/`"ocr-paragraph"`/`"ocr-normalized"`/`"ocr-fuzzy"`/`"ocr-weak"`/`"llm"`) and, when available,
  `ocr_confidence` (0–1). `"ocr-weak"` marks a box whose matched line covers less than
  half the value, which is how to count such boxes in a stored artifact without
  re-deriving the tiers. The LLM `confidence`/`confidence_reason` are never modified,
  so HITL and confidence alerts are unaffected.
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

### Absent values raise no alert; missing required fields are counted

`assessment/presence.py` holds the two rules that read the **extracted values**,
which the alert builders above never see
([#1527](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1527)).

- **`exclude_absent_value_alerts(alerts, assessment, extraction_results)`** drops
  every alert raised on a confidence leaf whose own extracted value is absent
  (`has_extracted_value`: `_is_populated_value` from #1474, applied recursively, so
  an object or list holding only null, blank or empty members is absent too). The
  leaf's score and reason stay in `explainability_info`. The walk names leaves
  exactly as `enrich_assessment_with_thresholds` names alerts, and an alert beneath
  an absent leaf (as `_check_confidence_alerts` reports for a leaf's sub-fields) is
  dropped with it. A populated string the model scored as sub-keyed fragments keeps
  its fragment alerts, since there is no per-fragment value to test.
- **`collect_missing_required_fields(extraction_results, class_schema, assessment)`**
  lists the fields the class schema's JSON Schema `required` arrays require and the
  extraction left absent: per present object, per existing list row, one entry for
  an absent required object rather than one per child, nothing for a schema with no
  `required`, and the multi-instance wrapper's own `required: ["instances"]`
  ignored. Entries have the alert shape (`attribute_name`, `confidence`,
  `confidence_threshold`), with the score read from the assessment leaf at the same
  path when there is one. The list is evaluated per section against that section's
  class schema; the document's `missing_required_field_count` is the sum over its
  sections.

They are applied where each mode stores its alerts, so every producer upstream is
covered by one call: `AssessmentService.process_document_section` (`separate`),
`ExtractionService._attach_explainability` (`integrated`, and `separate` run
inside agentic shards), and
`create_confidence_threshold_alerts` in the BDA result processor, which skips a
key-value entry whose `value` is absent. `ExtractionService._save_results` builds
`missing_required_fields` for every confidence mode, including none, and the
`separate` Assessment step rebuilds it with scores. Because `ConfidenceAlertCount`,
`review_queues.routing.legacy_section_triggers` and the `low_confidence` review
reasons all read `confidence_threshold_alerts`, none of them sees an absent value.
Nothing routes on `missing_required_fields`.

One path judges list rows it cannot place. On the agentic sharded path, when the
integrated retry did not rebuild the alert surface, each alert's row index counts
from its shard's first row (upstream #813), so matching it against the merged data
would test the wrong row and could drop a real alert on a populated row.
`_attach_explainability` therefore passes `row_indexes_are_global=False` there, and
alerts carrying a row index are kept unjudged.

The tracking item stores the list as `MissingRequiredFields` beside
`ConfidenceThresholdAlerts`, bounded the same way (at most 50 per section and 300
per document, with `MissingRequiredFieldsTotal` recorded when a section's list was
truncated), and the document count as `MissingRequiredFieldCount`. The document list's
`TypeDateIndexV2` projects that count, so the list shows it for every document
processed since the count was introduced; an older row, or one served from
`TypeDateIndex` while `TypeDateIndexV2` backfills after an upgrade, shows a dash.

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
- Output tokens are not configurable — the confidence pass requests the model
  maximum (so long list assessments aren't truncated), except on Nova Lite/Micro,
  where each call requests a row-sized output budget (see *Truncation-aware
  adaptive batch splitting*)
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
```
