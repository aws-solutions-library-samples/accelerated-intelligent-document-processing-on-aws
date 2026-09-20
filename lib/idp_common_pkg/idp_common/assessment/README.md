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
                                        # cap, column count and geometry mode
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
   size (derived from the confidence model's output cap, the row's column count and
   the geometry mode; never larger than the `extraction.confidence.list_batch_size`
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
   the first call the batch size is derived from three inputs: the confidence
   model's output cap (`bedrock.model_utils.get_model_max_output_tokens`), the
   **column count** of the list's widest sampled row, and whether `geometry.mode`
   is `llm`/`llm_grounded` (a per-cell bounding box roughly triples per-row
   output). The estimator itself lives in `bedrock.sizing`
   (`confidence_rows_per_call`) so this module and `compute_sizing_plan` cannot
   drift apart. Recorded as `derived_batch_size`.

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
makes the *instance list* the outer list field, so one "row" is a whole document
instance carrying its own long inner list (a 100-row `Transactions` table). The
sizer reports that row as `cols=2 per_row~80` and derives a 12-row batch, which is
wrong by orders of magnitude — **that sizing bug is not fixed** and #894 stays open
for it.

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
exists — so #894 remains open for it, and for the batch sizer.

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

It also reports **partial coverage**
([#901](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/901)
item 3). The gate already computed which rows carry no confidence, but every
caller discarded that, so a section could return a fraction of its rows scored and
still report unqualified success (one run scored 1,146 leaves where 5-page shards
scored 4,807 — ~24% coverage, no issue raised; the *reason* coverage fell rather
than failing was never established, and this issue makes no claim about it). When
unscored rows exceed **5%** of the extracted list rows the gate emits
`assessment_coverage_incomplete` — **warning** up to 25% unscored, **error** at or
above 25% **and** at least **10 unscored rows** — carrying `expected_rows`,
`scored_rows`, `unscored_rows` and a per-field breakdown.

Three deliberate choices in those thresholds:

- **5%, not 0%.** Reconciliation pads one assessment entry per extracted row, so a
  run whose model scored every row lands at 0%. Small residual shortfalls are also
  already named precisely by `assessment_incomplete`, so a 1-2 row gap on an 800-row
  table is that issue's business rather than a second document-level alarm. What the
  fraction means on a *short* list is arithmetic and worth stating outright: a single
  unscored row is a shortfall of `1/N`, so **one unscored row fires the warning for
  any section totalling 20 list rows or fewer**. Whether a healthy run ever produces
  that single unscored row is the part that is measured rather than reasoned — see
  *Coverage: measured and unobserved* below.
- **An absolute floor of 10 unscored rows on the error rung.** A fraction alone makes
  short lists fire hardest: one unscored row in a four-row list is 25%, and an error
  renders the section red ("Incomplete") in the Sections panel. Short list attributes
  are ordinary (a two-entry `ENDORSEMENTS` array in `lending-package-sample`). Below
  the floor the shortfall is still reported, as a warning. The floor bounds this
  class for the error rung only; the warning rung still fires on a short list.
- **Suppressed when the ladder already reported an error.** `build_assessment_issues`
  and this gate run on the same section, and the ladder's error rungs describe the
  same unscored rows *with a cause attached*. Emitting both doubles
  `ProcessingIssueCount` with two counts that can legitimately disagree
  (`unrecoverable_rows` tracks only the largest list field; this gate counts every
  list field), and it would break `assessment_schema_mismatch`'s "emitted alone"
  contract by appending a coverage note under it. Callers pass the ladder's issues in
  as `audit_explainability(..., ladder_issues=...)`; a `warning`/`info` ladder issue
  does not suppress it.

Issues are attached to each `Section`
(`section.processing_issues`), rolled up to `Document.processing_issue_count`,
and rendered in the extraction processing report.

#### Coverage: measured and unobserved

`confidence_coverage(assessment, extraction_results)` returns the figure —
`scored_rows` over `expected_rows`, plus the per-field breakdown — for one section.
It calls `audit_explainability` rather than re-deriving "is this row scored?", so a
measurement and the guard that ships are one computation; `audit_explainability`
puts the very same dict in the issue's `details`. Use it whenever you want the
number unconditionally, because the issue only carries it once the shortfall has
already crossed 5%, which is the reason nothing recorded coverage on healthy runs.

`expected_rows == 0` (a section with no list attribute) means coverage is
**undefined**, not perfect: `scored_fraction` is `None` so an aggregate drops it. A
corpus mean that scored those as 1.0 would be reporting the share of list-free
documents.

The benchmark harness records it per document
(`benchmarks/harness/analyze.py::score_confidence_coverage` → `conf_rows_expected`,
`conf_rows_scored`, `conf_rows_unscored`, `conf_coverage`,
`conf_unscored_by_field`), and `aggregate.cell_stats` rolls up the **distribution**
per cell — min, max, stdev and CV, not only the mean, because a mean of 0.99 is
equally consistent with every document at 0.99 and with one document at 0.

What the existing committed artifacts under `benchmarks/results/` already show, and
what they cannot: they retain `rows_extracted` and `n_conf_leaves` per document-run
but no raw `explainability_info`, so coverage there is a **leaf-count proxy** rather
than this rule. Across 4,344 committed assessment-on synthetic document-runs
spanning v0.5.16 → v0.6.10, roughly 95% show no shortfall at all and about 1% would
cross the 5% rung (0.2% the error rung), by two independent reconstructions that
agree — an upper-envelope method needing no corpus constants, and one using the
generator's known 3-cells-per-row shape. That is consistent with healthy runs
sitting at 0%.

Three limits on that evidence, all of which keep the specific concern open:

- It is a **lower bound** on the guard's shortfall. `_row_confidence_missing` marks a
  whole row unscored if *any* leaf in it is `None`; a leaf count credits the leaves
  that were scored. A row with two of three cells scored is 0 scored rows to the
  guard and 2 leaves to the proxy.
- Only the **synthetic** corpus retains a row denominator. `score_reference` records
  `n_conf_leaves` with no row count, so `realkie` and `ocr_bench` contribute nothing.
- The synthetic corpus rows are **flat** — three or eight scalar cells, generated by
  `benchmarks/corpus/generators/bank_statement.py::_row`. It contains no row carrying
  a nested group or an inner list, which is exactly the shape the any-leaf-`None`
  rule is hardest on and exactly the shape a multi-instance section (#715) makes
  universal.

So the warning rung's false-positive rate on **short, nested, multi-record** lists
remains unobserved. Settling it needs a run over a multi-record corpus — the
`healthcare-multisection-package` or a pay-statement shape — on both a Claude and a
Nova model, since output caps drive the truncation that produces unscored rows; the
instrument above then reports it directly. The thresholds are left where #912 put
them until that run exists, because moving them on a proxy that cannot see the shape
in question would be the same kind of reasoning this note replaces.

### Lambda wall-clock budget & resume safety

The escalation ladder adds sequential model calls inside the 900s
Extraction/Assessment Lambdas. To avoid a hard timeout, both handlers thread the
Lambda's `context.get_remaining_time_in_millis()` down as an absolute
`deadline_epoch`. Every path that can spend a model call consults it, and all of
them price a call at `_ESTIMATED_MODEL_CALL_SECONDS` (60s) or the last measured
duration, whichever is larger, against the remaining time minus a 90s safety
reserve:

| Where | Granularity | Added |
|---|---|---|
| Further bisection of a truncated slice (`_assess_slice_adaptive`) | per split | 1.5 |
| Every recovery call, retry **and** escalation (`_splice_missing_rows`) | **per chunk** | #958 |
| Before an escalation *round* (`_retry_missing_rows` rung 2) | per round | 1.5 |

When a check fails the ladder stops, keeps everything already recovered, and sets
`deadline_reached` — converting a would-be timeout into a soft, flagged, complete
document.

The retry rung was the last to be covered: until #958 it stopped only after
`max_retries` rounds or on a round that recovered nothing, so rounds that each made
partial progress were unbounded in wall-clock terms. Note **why that check is per
chunk and not per round.** A round-level check has to price a whole round before
making any of its calls, and the only safe price is a worst case — chunks × 60s.
That is not a bound on time, it is a cap on chunk COUNT: with 800s remaining it
refuses any round past 11 chunks however fast the model actually is, which for a
200-row list at a token-aware batch size of 4 discarded recovery that measurably
took under a second and turned a clean section into an error-severity
`assessment_incomplete`. Pricing one call at a time bounds the wall clock exactly,
keeps every row the budget did cover, and treats both rungs alike — before #958 the
cheap same-model rung could be refused while the slower, dearer escalation rung
proceeded, because the two sized their chunks differently.

**What an operator sees.** With rows still unscored the section reports
`assessment_incomplete` (**error**) whose message names the time budget and its
remedy — more Lambda time, or less work per section. It does **not** report
`assessment_deadline_reached`: that rung sits below `assessment_incomplete` in the
severity ladder and the guard only fires while rows are missing, so the warning is
reachable only when a later rung went on to score every row anyway. As defense in depth, the Step Functions
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
| The pass **never ran**: the section had no `extraction_result_uri`, no `page_ids`, or an extraction result whose `inference_result` was empty (#1006) | `skip_section_no_confidence`, called from `process_document_section`'s early returns | `assessment_skipped_confidence_unavailable` |

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
- a section whose extraction result is flagged `skipped_due_to_empty_attributes`,
  i.e. a class with **no attributes to extract**. `ExtractionService` skips the
  model for those (`_handle_empty_schema`) and still sets
  `extraction_result_uri`, so the stub arrives here with an empty
  `inference_result` and is indistinguishable from a real gap without the flag.
  It is reached routinely rather than only by a hand-authored attribute-less
  class: classification emits `"unclassified"` for a blank page, for a page whose
  classification errored after retries, and for everything when no document types
  are configured, and no class of that name exists in config, so its effective
  schema is `{}`. One cover sheet in an otherwise normal document lands here, and
  a dozen such documents in fifteen minutes would clear the default threshold on
  their own;
- a configuration with `extraction.confidence.enabled: false`.

An empty `inference_result` **without** that flag is still reported: the class had
a schema, the model returned nothing, and those values now have no confidence.

A `section_id` that is not in the document, or a document with no sections,
**raises** instead: there is no section on which to record anything, so a quiet
return would leave the caller with no signal at all.
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
