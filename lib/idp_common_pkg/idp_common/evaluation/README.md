Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Evaluation Service

The Evaluation Service component provides functionality to evaluate document extraction results by comparing extracted attributes against expected values.

## Backend Integration

The evaluation service uses **[Stickler](https://github.com/awslabs/stickler)** as its backend evaluation engine (installed from PyPI; pinned in the top-level `pyproject.toml` — see `stickler_version.STICKLER_VERSION` for the resolved version). Stickler is an AWS open-source library that provides sophisticated comparison algorithms and flexible configuration options.

### Package layout — single import boundary

All code that touches `stickler.*` lives under `idp_common/evaluation/stickler_backend/`:

- `mapper.py` — IDP → Stickler schema-extension translation (`SticklerConfigMapper`).
- `model_factory.py` — build a `StructuredModel` subclass from an IDP config (`get_stickler_model`), including the two upstream-tagged shims (`make_model_fields_nullable`, `clean_null_descriptions`).
- `comparators.py` — `LLMComparator` and `register_idp_comparators()` (public-API registration).
- `results.py` — Stickler `compare_with` dict → IDP `SectionEvaluationResult` (no re-scoring; encodes R3).
- `doc_split.py` — thin adapters over `stickler.doc_split` for `load_sections_for_doc_split` and `compute_graded_packet_metrics` (R14).

Outside that boundary:

- `baseline_migration.py` — pure helpers for migrating stored evaluation
  baselines to/from the multi-instance shape (GitHub #715). The operational S3
  walker is `scripts/migrate_multi_instance_baselines.py` (dry-run by default,
  idempotent, `--direction unwrap` to roll back).

## Multi-instance classes (#715) — baselines must match the prediction's shape

A class flagged `x-aws-idp-multi-instance` extracts
`{"instances": [ …record… ]}`. Evaluation compares against a stored baseline **of
the same shape**, so a wrapped prediction against a flat baseline scores every
field as missing-on-one-side: the class reads ~0 accuracy and nothing explains
why. This is the single biggest risk the feature introduces.

Three things handle it:

1. `stickler_backend/mapper.py` `build_all_stickler_configs` wraps each class
   before translating it. Built from the flat schema, the prediction's only key is
   `instances`, zero declared fields match, and the section **silently scores
   0.0**.
2. ⚠️ **Report granularity degrades, and the obvious fix is wrong.** For a wrapped
   class every row's `expected_key` is `instances[i].Field`, so
   `contract.py` `row_root_attribute` groups them all under the single attribute
   `instances`: the per-attribute report is one giant attribute rather than one per
   field. Stepping past the synthesized root here was tried and **reverted** — it
   made the helper return `CheckNumber`, which matches no attribute at all, because
   the attribute list is built from the class SCHEMA and a wrapped class has exactly
   one property. All 24 of Stickler's `field_comparisons` rows were then dropped
   from `field_comparison_details` (measured live), emptying the report's per-field
   drilldown and the UI's mismatch highlighting, which joins on `expected_key`.
   Section metrics stayed correct, so accuracy still read 1.000 with an empty
   drilldown — invisible in the numbers. Recovering per-field granularity means
   changing how the ATTRIBUTE LIST is constructed for a flagged class, not how rows
   are keyed.
3. `service.py` `_warn_on_multi_instance_shape_mismatch` logs a warning naming the
   migration command when the two shapes disagree, in **either** direction
   (rollback fails just as silently). Advisory only; it never changes a score.

## Confidence-curve keys (`curve_store.py`)

⚠️ **Key-shape change.** `_flatten_confidences` / `_flatten_values` used to key the
list index off list *length* (`prefix if len(node) == 1 else prefix[i]`), so any
single-element list lost its index: a one-row table keyed `Transactions.date`
while a two-row table keyed `Transactions[0].date`. They now key off *depth* — only
the outer `explainability_info` wrapper is un-indexed — so a list is always
`Field[i].Sub`.

Consequence to be honest about: a list whose length *varied* never joined and now
does. But a list that was **always** single-element joined fine before, and its
stored history is now orphaned — there is no migration and no read-side fallback,
so those curve points will not join with new ones.

Everything else — `service.py` (orchestration), `models.py` (dataclasses),
`stickler_mapper.py` / `llm_comparator.py` (thin re-export shims for
backward compatibility) — is backend-agnostic. A future Stickler upgrade is
a one-package review, not a cross-cutting hunt.

The cross-Lambda `results.json` contract (S3 key template, `compare_with`
flag set, result-version stamp) is formalized in `contract.py` — bump
`STICKLER_RESULT_VERSION` when the raw Stickler blob shape changes.

The IDP evaluation service provides an abstraction layer through `SticklerConfigMapper` that:

- Translates IDP evaluation extensions (`x-aws-idp-evaluation-*`) to Stickler format
- Maintains backend-agnostic configuration in IDP
- Enables seamless integration with Stickler's advanced evaluation capabilities

### Confidence Calibration Metrics

Bulk evaluation surfaces the standard calibration metrics:

- **ECE (Expected Calibration Error)**: Measures how well confidence scores match actual accuracy (0.0 = perfect calibration)
- **Brier Score**: Mean squared error between confidence and outcome (lower is better; 0.0 = perfect, 0.25 = random)
- **AUROC**: How well confidence discriminates correct from incorrect predictions (1.0 = perfect discrimination)
- **Per-Field Metrics**: Field-level calibration analysis to identify poorly calibrated fields
- **Coverage Tracking**: Ratio of fields with confidence data to total fields

These are computed in the aggregation Lambda via Stickler's `BulkStructuredModelEvaluator` (see `patterns/unified/src/test_execution_aggregation_function/index.py`).

### Confidence→accuracy curve and the review-effort estimator

`confidence_curve.py` and `curve_store.py` implement the engine behind Test
Studio's "how many documents must I review?" estimate. They answer it from a
measured `P(correct | confidence)` curve rather than a fixed heuristic.

**Import-safety note.** Both modules depend only on the standard library and
boto3 — deliberately, because the Lambdas that use them (the test-set resolver,
the HITL review function) run on the shared base layer, which excludes the
`[evaluation]` extra and its ~50MB of Stickler dependencies. For the same reason
`evaluation/__init__.py` resolves `EvaluationService`, `SticklerConfigMapper` and
`LLMComparator` **lazily** via a module-level `__getattr__`; importing them
eagerly made every module in the package un-importable without the extra.
Accessing a Stickler-backed name without it installed raises an error naming the
extra rather than an opaque `ModuleNotFoundError` at cold start.

**The curve** is a reliability table: per-confidence-bin counts of
`(observations, correct)`. Chosen over a fitted model (e.g. isotonic regression)
because it is explainable, ~230 bytes serialized, and additive — folding in new
observations is two counter increments per bin, which lets `CurveStore` use
DynamoDB `ADD` and stay correct when several reviewers finish at once. The bin
edges match Stickler's `ECEMetric`, so its output folds in without rebinning.

**Three observation sources**, in increasing fidelity:

| Source | Where it is recorded | Covers |
|---|---|---|
| Prior | `CurveStore.get_global_prior()` | Cross-set fallback for a cold start |
| Human review | `complete_section_review` → `record_curve_observations` | Low-confidence range (review is worst-first) |
| Scoring run | aggregation Lambda → `add_ece_bins` | The full range, including high confidence |

A field a reviewer *changed* is an incorrect observation; one they *left alone* is
correct. `observations_from_baseline_review` derives those pairs by diffing the
drafted label against the saved one — which is why the review Lambda must read
the previous baseline **before** overwriting it.

Curves are keyed by `(test set, config version)` since confidence semantics shift
across models and prompts, with fallback to the set aggregate and then the global
prior.

**Safety.** `estimate_for_target` never returns a bare number. It reports an
`EstimateConfidence` state (`prior` / `partially-measured` / `measured` /
`unreliable`), a `CalibrationHealth` block, and a docs-to-review *range* that
widens when the curve is prior-driven. Two failure modes are detected explicitly
because either would let the estimator certify an inaccurate set:

- **Overconfident** (wrong *and* confident) — errors hide in the high-confidence
  zone worst-first review never visits. Detected from ECE plus a high-confidence
  accuracy gap; mitigated by `audit_sample_size`, a random sample of that zone.
- **Degenerate** (confidence barely varies) — no signal to rank by. **ECE does not
  catch this**: a single populated bin is trivially well-calibrated, so bin
  coverage is tracked as an independent signal.

Either sets `estimateConfidence="unreliable"` and `recommendReviewAll=True`.

**Quality tiers.** `quality_tier()` derives a `QualityTier` (`gold` / `silver` /
`bronze` / `unrated`) from the estimated accuracy *and* how it was obtained: 99%
computed from a cross-set prior says nothing about these labels, so only a curve
measured on this set can earn `gold`. An unreliable curve is `unrated` rather than
graded, since no accuracy claim is defensible when confidence cannot rank
correctness.

The tier is deliberately a derived value, not a settable field — it must be
earnable and losable rather than assertable. Note that the UI leads with the
estimated accuracy and treats the tier name as shorthand: "gold data" carries a
specific connotation for customers, and a badge alone reads as a certification.

The effort model is a flat heuristic (fields × per-field seconds + sections ×
per-section seconds), measured from the test set where possible. It does not model
field complexity or annotator speed; per-annotator timings from claim→complete
durations would be the next refinement.

## Features

- Compares document extraction results with expected (ground truth) results
- Supports multiple evaluation methods:
  - Exact match - Character-for-character comparison after normalizing whitespace and punctuation
  - Numeric exact match - Value-based comparison after normalizing numeric formats
  - Fuzzy string matching - Similarity-based matching with configurable thresholds
  - Hungarian algorithm - Optimal matching for lists of values
  - Semantic similarity - Meaning-based comparison using Bedrock Titan embeddings
  - LLM-based semantic evaluation - Advanced meaning comparison with explanation using Bedrock models
- Smart attribute discovery and evaluation:
  - Automatically discovers attributes in the extraction results not defined in the configuration
  - Handles attributes found only in expected data, only in actual data, or in both
  - Applies default comparison method (LLM) for unconfigured attributes with clear indication
- **Assessment Confidence Integration**:
  - Automatically extracts and displays confidence scores from assessment results
  - Shows confidence (extraction confidence)
  - Integrates with explainability_info from the assessment feature
  - Provides insights into data quality for both baseline and extraction results
- Calculates key metrics including:
  - Precision, Recall, and F1 score
  - Accuracy and Error rates
  - False alarm rate and False discovery rate
- Generates rich, visual evaluation reports with:
  - Color-coded status indicators
  - Performance ratings
  - Progress bar visualizations
  - Detailed attribute comparisons
  - Confidence score columns for quality analysis
- Supports both JSON and Markdown report formats
- Fully integrated with the Document model architecture
- **Document Split Classification Metrics**:
  - Evaluates document splitting and classification accuracy
  - Calculates page-level classification accuracy
  - Measures split accuracy (with and without page order consideration)
  - Provides detailed per-page and per-section analysis
  - Generates comprehensive markdown reports with visual indicators

## Usage

```python
from idp_common.models import Document, Status
from idp_common import ocr, classification, extraction, evaluation

# Get configuration (with evaluation methods specified for all attribute types)
config = {
    "evaluation": {
        "llm_method": {
            "model": "anthropic.claude-3-sonnet-20240229-v1:0",
            "temperature": 0.0,
            "top_k": 5,
            "system_prompt": "You are an evaluator that helps determine if the predicted and expected values match...",
            "task_prompt": "I need to evaluate attribute extraction for a document of class: {DOCUMENT_CLASS}..."
        }
    },
    "classes": [
        {
            "name": "Bank Statement",
            "attributes": [
                # Simple Attributes
                {
                    "name": "Account Number",
                    "description": "Primary account identifier",
                    "attributeType": "simple",  # or omit for default
                    "evaluation_method": "EXACT"
                },
                {
                    "name": "Statement Period",
                    "description": "Statement period (e.g., January 2024)",
                    "attributeType": "simple",
                    "evaluation_method": "FUZZY",
                    "evaluation_threshold": 0.8
                },
                
                # Group Attributes - nested object structures
                {
                    "name": "Account Holder Address",
                    "description": "Complete address information for the account holder",
                    "attributeType": "group",
                    "groupAttributes": [
                        {
                            "name": "Street Number",
                            "description": "House or building number",
                            "evaluation_method": "FUZZY",
                            "evaluation_threshold": 0.9
                        },
                        {
                            "name": "Street Name",
                            "description": "Name of the street",
                            "evaluation_method": "FUZZY",
                            "evaluation_threshold": 0.8
                        },
                        {
                            "name": "City",
                            "description": "City name",
                            "evaluation_method": "FUZZY",
                            "evaluation_threshold": 0.9
                        },
                        {
                            "name": "State",
                            "description": "State abbreviation",
                            "evaluation_method": "EXACT"
                        },
                        {
                            "name": "ZIP Code",
                            "description": "Postal code",
                            "evaluation_method": "EXACT"
                        }
                    ]
                },
                
                # List Attributes - arrays of items with consistent structure
                {
                    "name": "Transactions",
                    "description": "List of all transactions in the statement period",
                    "attributeType": "list",
                    "listItemTemplate": {
                        "itemDescription": "Individual transaction record",
                        "itemAttributes": [
                            {
                                "name": "Date",
                                "description": "Transaction date",
                                "evaluation_method": "FUZZY",
                                "evaluation_threshold": 0.9
                            },
                            {
                                "name": "Description",
                                "description": "Transaction description or merchant name",
                                "evaluation_method": "SEMANTIC",
                                "evaluation_threshold": 0.7
                            },
                            {
                                "name": "Amount",
                                "description": "Transaction amount",
                                "evaluation_method": "NUMERIC_EXACT"
                            }
                        ]
                    }
                }
            ]
        }
    ]
}

# Create evaluation service
evaluation_service = evaluation.EvaluationService(config=config)

# Evaluate documents (stores results in S3 by default)
result_document = evaluation_service.evaluate_document(
    actual_document=processed_document,
    expected_document=expected_document
)

# Access evaluation report URI
evaluation_report_uri = result_document.evaluation_report_uri

# You can also access the evaluation result directly
evaluation_result = result_document.evaluation_result
overall_metrics = evaluation_result.overall_metrics
section_results = evaluation_result.section_results

# Or skip storage if needed (for quick memory-only evaluations)
memory_only_document = evaluation_service.evaluate_document(
    actual_document=processed_document,
    expected_document=expected_document,
    store_results=False
)
```

## Evaluation Methods

The service supports multiple evaluation methods that can be configured for each attribute:

- `EXACT`: Exact string match (after normalizing whitespace and punctuation)
- `NUMERIC_EXACT`: Exact match for numeric values (after normalizing currency symbols)
- `FUZZY`: Fuzzy string matching with configurable evaluation_threshold
- `DATE`: Format-insensitive date comparison (Stickler v0.5.0+ `DateComparator`). Parses both values into dates before comparing, so `01/05/2024`, `2024-01-05`, and `January 5, 2024` all match. Also handles date ranges (e.g. `2024-01-01 to 2024-01-31`). Optional per-field tuning via `x-aws-idp-evaluation-method-config` (e.g. `dayfirst`, `tolerance`, `range_mode`).
- `HUNGARIAN`: Optimal matching for lists of values using the Hungarian algorithm with configurable comparator types:
  - `EXACT`: Default comparator for exact string matching (after normalization)
  - `FUZZY`: Fuzzy string matching with configurable threshold
  - `NUMERIC`: Numeric comparison after normalizing currency symbols and formats
- `SEMANTIC`: Efficient semantic similarity comparison using Bedrock Titan embeddings (amazon.titan-embed-text-v1)
- `LLM`: LLM-based evaluation using Bedrock models (Claude or Titan) for semantically comparable values with detailed explanations. **Not supported on fields inside a structured list — see below.**

#### ⚠️ `LLM` is not usable inside a structured list

An `LLM` method on a field **inside a list's items** is downgraded to that field's
deterministic type default (string → Levenshtein, number → Numeric, boolean →
Exact), with a warning naming the field.

Structured lists are matched with the Hungarian algorithm, which builds a full
`N_ground_truth × N_predicted` similarity matrix and invokes each item field's
comparator **once per cell**, then scores the matched pairs — measured at
`N² + 2N` comparator calls. One Bedrock round trip per cell means a 54-row
invoice needs ~3,000 sequential calls (~45 minutes), so the 900 s evaluation
Lambda can never finish it at any retry count. This was observed wedging an
entire stack: every affected document burned 9 × 900 s attempts before failing,
and the leaked workflow-concurrency slots stopped the pipeline accepting new
documents.

A matching cost function wants a cheap, deterministic similarity anyway, so the
downgrade is also the right shape. To override on a small, bounded list:

```yaml
LineItems:
  type: array
  items:
    type: object
    properties:
      Description:
        type: string
        x-aws-idp-evaluation-method: LLM
        x-aws-idp-evaluation-allow-llm-in-list: true   # accepts the O(N²) cost
```

Note also that an evaluation method on the **array itself** has never had any
effect (lists score through their item fields, and row matching is Hungarian).
That is now logged as a warning rather than silently discarded.

#### Field context in the `LLM` prompt

The `LLM` prompt interpolates `{DOCUMENT_CLASS}`, `{ATTRIBUTE_NAME}` and
`{ATTRIBUTE_DESCRIPTION}`. Stickler's comparator protocol is
`compare(value1, value2)` and carries no field context, so `SticklerConfigMapper`
supplies each LLM-method field's class, name and description through the same
per-field `x-aws-stickler-comparator-config` channel the model config uses, and
`LLMComparator` forwards them.

> Before this was wired up, every judge call went out as
> `for a document of class: . For the attribute named "" described as "":` — the
> model was asked to decide whether two bare strings meant the same thing with no
> idea what field it was grading. Scores produced by the `LLM` method before this
> fix are context-free and not comparable with scores produced after it.

Identical values (after case and whitespace normalization) short-circuit to a
match with **no** Bedrock call, and repeated `(expected, actual)` pairs are
memoized per comparator instance.

#### DATE method configuration

The `DATE` method accepts an optional `x-aws-idp-evaluation-method-config` object that is passed through to Stickler's `DateComparator`:

```yaml
InvoiceDate:
  type: string
  format: date
  description: Invoice issue date
  x-aws-idp-evaluation-method: DATE
  x-aws-idp-evaluation-method-config:
    dayfirst: false        # false = month-first (US); true = day-first (EU); omit to try both
    range_mode: graded     # graded (default) | strict | contains | reject
```

Notes:
- `DATE` is for calendar dates only. Time-only values (e.g. `19:02`) score `0.0` — keep those on `EXACT`.
- Fields that merely mention "date" in free text (e.g. a narrative statement) should stay on `LLM`/`SEMANTIC`, not `DATE`.

### Semantic vs LLM Evaluation

The service offers two approaches for semantic evaluation:

- **SEMANTIC Method**: Uses embedding-based comparison with Bedrock Titan embeddings
  - Faster and more cost-effective than LLM-based evaluation
  - Provides similarity scores without explanations
  - Great for high-volume comparisons where speed is important
  - Configurable threshold for matching sensitivity
  
- **LLM Method**: Uses Bedrock Claude or other LLM models
  - Provides detailed reasoning for why values match or don't match
  - Better at handling implicit/explicit information differences
  - More nuanced understanding of semantic equivalence
  - Ideal for cases where understanding the rationale is important
  - Used as the default method for attributes discovered in the data but not in the configuration

## Output

The evaluation produces:

1. **JSON Results**: Detailed evaluation results with metrics
2. **Markdown Report**: Human-readable report with tables and summaries

## Metrics

The evaluation calculates the following metrics. As of v0.6.7, counts come
directly from
Stickler's row-level `field_comparisons` — one count per drilldown row the
UI displays — with item-level rejected/missing/extra rows weighted by their
leaf count so a truncated 5-item list and a partially-wrong 5-item list
contribute the same leaf-normalized units.

- **Precision**: `TP / (TP + FP)` where `FP = FA + FD`
- **Recall**: `TP / (TP + FN)`
- **F1 Score**: `2·TP / (2·TP + FP + FN)`
- **Accuracy**: `(TP + TN) / (TP + FP + FN + TN)`
- **False Alarm Rate (FAR)**: `FA / (FA + TN)`
  - Rate of *hallucinated* fields (predicted values where none was expected)
    among true-negatives. Stickler splits `FP` into `fa` (false alarm) and
    `fd` (false discovery); FAR measures the hallucination side.
- **False Discovery Rate (FDR)**: `FD / (FD + TP)`
  - Rate of *wrong-value* fields among positive predictions. The other side
    of the `fa`/`fd` split — measures incorrect extractions.

The `fa`/`fd` distinction matters because they represent different failure
modes and warrant different remediations — FAR isolates hallucinations, FDR
isolates wrong extractions. These metrics are calculated at attribute
level (per field), section level (per document class), and document level.

**Historical data note:** runs recorded on v0.6.3–v0.6.6 predate this
counting semantics and may show inflated (leaves-inside-kept-items masked)
or deflated (item-level rows counted as one unit each) section metrics on
list-heavy configs. See [issue #625](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/625);
re-run those evaluations after upgrading for accurate comparison.

### Failure and exclusion flags in section metrics

A section's `metrics` dict can also carry non-numeric state that distinguishes
*not scored* from *scored zero*. Consumers of `results.json` should branch on
these before reading the numbers:

| Key | Meaning |
|-----|---------|
| `evaluation_skipped: True` | Nothing to score (class has no extractable fields, or is excluded from processing). `weighted_overall_score` is `None` and the section is dropped from the document weighted mean and confusion-matrix rollup. |
| `evaluation_failed: True` | Evaluation was attempted and failed. Metrics are zeroed and **do** count against document-level aggregates. |
| `failure_type: str` | Set alongside `evaluation_failed`; names the cause — `missing_schema_configuration`, `empty_nested_object`, `extraction_parsing_failed`, `baseline_data_validation_error`, `schema_configuration_error`, `unexpected_error`. |
| `skipped_field_count: int` | Some fields were dropped from scoring after per-field validation errors; the rest were scored normally. |

`DocumentEvaluationResult.to_markdown` keys the failure block's "How to fix"
guidance on `failure_type`, and renders none when it is absent (results written
before the field existed) rather than guessing — advice for the wrong cause is
worse than no advice. When adding a new failure branch, set `failure_type` and
add a matching case in `_failure_remediation`.

## Visual Reporting

The evaluation module produces richly formatted Markdown reports with:

1. **Summary Dashboard**:
   - Overall match rate with visual progress bar
   - Color-coded indicators for key metrics (🟢 Excellent, 🟡 Good, 🟠 Fair, 🔴 Poor)
   - Fraction of matched attributes (e.g., 8/10 attributes matched)

2. **Performance Tables**:
   - Metrics tables with value ratings
   - First-column status indicators (✅/❌) for immediate identification of matches
   - Detailed attribution of evaluation methods used for each field, including:
     - Method types (EXACT, FUZZY, HUNGARIAN, etc.)
     - Thresholds for fuzzy and semantic matching methods
     - Comparator types for the Hungarian method
     - Combined display for HUNGARIAN with FUZZY comparator showing both comparator type and threshold

3. **Method Explanations**:
   - Clear documentation of evaluation methods
   - Descriptions of scoring mechanisms
   - Guidance on interpreting results
   - Indications for attributes that were discovered in the data but not in the configuration

Examples of method display in reports:
- `EXACT` - Simple exact matching
- `FUZZY (threshold: 0.8)` - Fuzzy matching with threshold
- `HUNGARIAN (comparator: EXACT)` - Hungarian algorithm with exact matching
- `HUNGARIAN (comparator: FUZZY, threshold: 0.7)` - Hungarian with fuzzy matching and threshold
- `HUNGARIAN (comparator: NUMERIC)` - Hungarian with numeric comparison

The reports are designed to provide both at-a-glance performance assessment and detailed diagnostic information.

## Auto-Discovery of Attributes

The EvaluationService can automatically discover and evaluate attributes that exist in the data but are not defined in the configuration:

```python
# Sample extracted data may have more attributes than configured
actual_results = {
    "invoice_number": "INV-12345",          # In configuration
    "amount_due": 1250.00,                  # In configuration
    "issue_date": "2023-01-15",             # Not in configuration
    "due_date": "2023-02-15"                # Not in configuration
}

expected_results = {
    "invoice_number": "INV-12345",          # In configuration
    "amount_due": "$1,250.00",              # In configuration 
    "issue_date": "01/15/2023",             # Not in configuration
    "reference_number": "REF-98765"         # Not in configuration, missing in actual
}

# The service will:
# 1. Evaluate invoice_number and amount_due using methods in configuration
# 2. Discover issue_date (in both) and evaluate using LLM (default method)
# 3. Discover due_date (only in actual) and evaluate as not matched
# 4. Discover reference_number (only in expected) and evaluate as not matched
# 5. Add "[Default method - attribute not specified in the configuration]" to reason for discovered attributes
```

This capability is particularly useful for:
- Exploratory evaluation when the complete schema is not yet defined
- Handling variations in extraction outputs that may contain additional information
- Identifying potential new attributes to add to the configuration
- Ensuring all extracted data is evaluated, even without explicit configuration

## Assessment Confidence Integration

The evaluation service automatically integrates with the assessment feature to display confidence scores alongside evaluation results. When extraction results include `explainability_info` (generated by the assessment feature), the confidence scores are automatically extracted and displayed in both JSON and Markdown reports.

### Confidence Score Types

- **Confidence**: Confidence score for the extraction results being evaluated

### Enhanced Report Format

#### JSON Output with Confidence
```json
{
  "attributes": [
    {
      "name": "invoice_number",
      "expected": "INV-2024-001",
      "actual": "INV-2024-001",
      "matched": true,
      "score": 1.0,
      "confidence": 0.92,
      "evaluation_method": "EXACT"
    }
  ]
}
```

#### Markdown Table with Confidence
```
| Status | Attribute | Expected | Actual | Confidence | Score | Method | Reason |
| :----: | --------- | -------- | ------ | :---------------: | ----- | ------ | ------ |
| ✅ | invoice_number | INV-2024-001 | INV-2024-001 | 0.92 | 1.00 | EXACT | Exact match |
| ❌ | vendor_name | ABC Corp | XYZ Inc | 0.75 | 0.00 | EXACT | Values do not match |
```

### Quality Analysis Benefits

Confidence scores provide additional insights for evaluation analysis:

1. **Extraction Quality Assessment**: Low confidence highlights extraction results needing review
2. **Confidence-Accuracy Correlation**: Compare confidence levels with evaluation accuracy to identify patterns
3. **Quality Prioritization**: Focus improvement efforts on low-confidence, low-accuracy results

### Backward Compatibility

The confidence integration is fully backward compatible:
- Reports without assessment data show "N/A" for confidence columns
- Evaluation logic remains unchanged when confidence data is absent
- Existing evaluation workflows continue to work without modification

## Nested Structure Support

The evaluation service fully supports nested document structures including group attributes and list attributes. The service automatically processes these complex structures by flattening them into individual evaluable fields while preserving the configured evaluation methods.

### Attribute Types and Processing

#### Simple Attributes
Basic single-value extractions that are evaluated directly:

```python
# Configuration
{
    "name": "Account Number",
    "attributeType": "simple",
    "evaluation_method": "EXACT"
}

# Flattened attribute name: "Account Number"
# Evaluation: Direct comparison using EXACT method
```

#### Group Attributes  
Nested object structures where each sub-attribute is evaluated individually:

```python
# Configuration
{
    "name": "Account Holder Address",
    "attributeType": "group",
    "groupAttributes": [
        {
            "name": "Street Number",
            "evaluation_method": "FUZZY",
            "evaluation_threshold": 0.9
        },
        {
            "name": "City",
            "evaluation_method": "FUZZY", 
            "evaluation_threshold": 0.9
        }
    ]
}

# Flattened attribute names:
# - "Account Holder Address.Street Number" (FUZZY evaluation)
# - "Account Holder Address.City" (FUZZY evaluation)
```

#### List Attributes
Arrays of items where each item's attributes are evaluated individually:

```python
# Configuration
{
    "name": "Transactions",
    "attributeType": "list",
    "listItemTemplate": {
        "itemAttributes": [
            {
                "name": "Date",
                "evaluation_method": "FUZZY",
                "evaluation_threshold": 0.9
            },
            {
                "name": "Amount",
                "evaluation_method": "NUMERIC_EXACT"
            }
        ]
    }
}

# Flattened attribute names for each transaction:
# - "Transactions[0].Date" (FUZZY evaluation)
# - "Transactions[0].Amount" (NUMERIC_EXACT evaluation)
# - "Transactions[1].Date" (FUZZY evaluation)
# - "Transactions[1].Amount" (NUMERIC_EXACT evaluation)
# - And so on for each transaction...
```

### Data Flattening Process

The evaluation service automatically flattens nested extraction results for comparison:

#### Input Data (Nested)
```json
{
  "Account Number": "1234567890",
  "Account Holder Address": {
    "Street Number": "123",
    "Street Name": "Main St",
    "City": "Seattle",
    "State": "WA"
  },
  "Transactions": [
    {
      "Date": "01/15/2024",
      "Description": "Coffee Shop",
      "Amount": "-4.50"
    },
    {
      "Date": "01/16/2024", 
      "Description": "ATM Withdrawal",
      "Amount": "-20.00"
    }
  ]
}
```

#### Flattened Data (For Evaluation)
```json
{
  "Account Number": "1234567890",
  "Account Holder Address.Street Number": "123",
  "Account Holder Address.Street Name": "Main St", 
  "Account Holder Address.City": "Seattle",
  "Account Holder Address.State": "WA",
  "Transactions[0].Date": "01/15/2024",
  "Transactions[0].Description": "Coffee Shop",
  "Transactions[0].Amount": "-4.50",
  "Transactions[1].Date": "01/16/2024",
  "Transactions[1].Description": "ATM Withdrawal", 
  "Transactions[1].Amount": "-20.00"
}
```

### Evaluation Results for Nested Structures

The evaluation service provides detailed results for all flattened attributes:

#### Sample Evaluation Output
```json
{
  "attributes": [
    {
      "name": "Account Number",
      "expected": "1234567890",
      "actual": "1234567890", 
      "matched": true,
      "score": 1.0,
      "confidence": 0.95,
      "evaluation_method": "EXACT"
    },
    {
      "name": "Account Holder Address.City",
      "expected": "Seattle",
      "actual": "Seattle",
      "matched": true,
      "score": 1.0,
      "confidence": 0.88,
      "evaluation_method": "FUZZY",
      "evaluation_threshold": 0.9
    },
    {
      "name": "Transactions[0].Amount",
      "expected": "-4.50",
      "actual": "-4.50",
      "matched": true,
      "score": 1.0,
      "confidence": 0.92,
      "evaluation_method": "NUMERIC_EXACT"
    },
    {
      "name": "Transactions[1].Description", 
      "expected": "ATM Withdrawal",
      "actual": "ATM Cash",
      "matched": true,
      "score": 0.85,
      "confidence": 0.87,
      "evaluation_method": "SEMANTIC",
      "evaluation_threshold": 0.7
    }
  ]
}
```

#### Markdown Report for Nested Structures
```markdown
| Status | Attribute | Expected | Actual | Confidence | Score | Method | Reason |
| :----: | --------- | -------- | ------ | :--------: | ----- | ------ | ------ |
| ✅ | Account Number | 1234567890 | 1234567890 | 0.95 | 1.00 | EXACT | Exact match |
| ✅ | Account Holder Address.Street Number | 123 | 123 | 0.95 | 1.00 | FUZZY (threshold: 0.9) | Exact match |
| ✅ | Account Holder Address.City | Seattle | Seattle | 0.88 | 1.00 | FUZZY (threshold: 0.9) | Exact match |
| ❌ | Account Holder Address.State | WA | Washington | 0.82 | 0.00 | EXACT | Values do not match exactly |
| ✅ | Transactions[0].Date | 01/15/2024 | 01/15/2024 | 0.94 | 1.00 | FUZZY (threshold: 0.9) | Exact match |
| ✅ | Transactions[0].Amount | -4.50 | -4.50 | 0.92 | 1.00 | NUMERIC_EXACT | Exact numeric match |
| ✅ | Transactions[1].Description | ATM Withdrawal | ATM Cash | 0.87 | 0.85 | SEMANTIC (threshold: 0.7) | Semantically similar |
```

### Benefits of Nested Structure Support

1. **Granular Analysis**: Individual evaluation of each nested field provides precise insights
2. **Flexible Configuration**: Different evaluation methods can be applied to different parts of nested structures
3. **Comprehensive Coverage**: All attributes in complex documents are evaluated, regardless of nesting level
4. **Pattern Recognition**: Identify consistent issues with specific nested attributes (e.g., address parsing problems)
5. **Scalable Processing**: Handles documents with varying numbers of list items efficiently
6. **Detailed Reporting**: Clear attribution of evaluation results to specific nested fields

### Use Cases for Nested Evaluation

- **Bank Statements**: Evaluate account details (group) and individual transactions (list)
- **Invoices**: Evaluate vendor information (group) and line items (list)
- **Medical Records**: Evaluate patient information (group) and procedures/medications (lists)
- **Legal Documents**: Evaluate parties (group) and clauses/terms (lists)
- **Financial Reports**: Evaluate company info (group) and financial line items (lists)

The nested structure support enables comprehensive evaluation of complex documents while maintaining the flexibility to apply appropriate evaluation methods to each type of data within the document.

## Document Split Classification Metrics

The evaluation service provides specialized metrics for evaluating document splitting and classification accuracy. This feature is particularly useful for assessing how well the system:
- Classifies individual pages
- Groups pages into document sections
- Maintains correct page order within sections

### Overview

`DocSplitClassificationMetrics` evaluates three types of accuracy:

1. **Page Level Accuracy**: Classification accuracy for individual pages
2. **Split Accuracy (Without Order)**: Correct page grouping regardless of order
3. **Split Accuracy (With Order)**: Correct page grouping with exact order

### Usage

Doc-split metrics are computed by `EvaluationService.evaluate_document` and
surfaced through `DocumentEvaluationResult.doc_split_metrics` (see
`models.DocSplitMetrics`); the markdown rendering is part of the
service-owned `DocumentEvaluationResult.to_markdown` output. If you need the
raw calculator (e.g. for a custom driver), it lives upstream at
`stickler.doc_split.doc_split_classification_metrics.DocSplitClassificationMetrics`
and accepts plain section dicts of the form
`{"section_id": ..., "document_class": {...}, "split_document": {"page_indices": [...]}}`.

### Metrics Explained

#### 1. Page Level Accuracy

Evaluates classification accuracy for **individual pages** by comparing the `document_class` assigned to each page index.

**Calculation:**
- For each page index in ground truth or predicted data
- Check if the predicted document_class matches the ground truth document_class
- Calculate: `correct_pages / total_pages`

**Use Case:** Determine if the classification model correctly identifies document types at the page level.

**Example:**
```python
page_level = {
    "accuracy": 0.95,
    "total_pages": 20,
    "correct_pages": 19,
    "page_details": [
        {
            "page_index": 0,
            "ground_truth_class": "Invoice",
            "predicted_class": "Invoice",
            "correct": True,
            "predicted_confidence": 0.91
        },
        {
            "page_index": 5,
            "ground_truth_class": "W2",
            "predicted_class": "Receipt",
            "correct": False,
            "predicted_confidence": 0.48
        }
    ]
}
```

`predicted_confidence` is the classifier's own confidence in the class it
predicted (`None` when the page was not scored — the default; see
[classification confidence](../classification/README.md#classification-confidence-confidence-classification_reason)).
Paired with `correct` on the same row it is the **calibration** measurement: if
confident-and-wrong pages score as high as confident-and-right ones, the
confidence carries no information and must not drive escalation. The benchmark
harness computes exactly that separation from these rows.

#### 2. Split Accuracy (Without Order)

Evaluates whether the system correctly groups pages into sections with the right document class, **regardless of page order**.

**Calculation:**
- For each ground truth section
- Find a predicted section with:
  - Same set of page indices (as a set, order doesn't matter)
  - Same document_class
- Calculate: `matched_sections / total_ground_truth_sections`

**Use Case:** Assess if the system correctly identifies which pages belong together, even if the order is different.

**Example:**
```python
split_no_order = {
    "accuracy": 0.90,
    "total_sections": 10,
    "correct_sections": 9,
    "section_details": [
        {
            "section_id": "section_1",
            "ground_truth_class": "Invoice",
            "ground_truth_pages": [0, 1, 2],
            "matched": True,
            "predicted_class": "Invoice",
            "predicted_pages": [2, 0, 1]  # Different order, but same pages
        }
    ]
}
```

#### 3. Split Accuracy (With Order)

Evaluates whether the system correctly groups pages into sections with the right document class, **including exact page order**.

**Calculation:**
- For each ground truth section
- Find a predicted section with:
  - Exact same page indices list (same order)
  - Same document_class
- Calculate: `matched_sections / total_ground_truth_sections`

**Use Case:** Assess if the system maintains the correct page sequence within document sections.

**Example:**
```python
split_with_order = {
    "accuracy": 0.85,
    "total_sections": 10,
    "correct_sections": 8,  # Lower than split_no_order due to order requirement
    "section_details": [
        {
            "section_id": "section_1",
            "ground_truth_class": "Invoice",
            "ground_truth_pages": [0, 1, 2],
            "matched": True,
            "order_matched": True,
            "predicted_class": "Invoice",
            "predicted_pages": [0, 1, 2]  # Exact match including order
        },
        {
            "section_id": "section_2",
            "ground_truth_class": "W2",
            "ground_truth_pages": [3, 4],
            "matched": False,
            "order_matched": False,
            "predicted_class": "W2",
            "predicted_pages": [4, 3]  # Wrong order
        }
    ]
}
```

### Visual Reporting

Doc-split visualization is emitted as part of `DocumentEvaluationResult.to_markdown`
(in `models.py`) — a single service-owned renderer that interleaves split
metrics with extraction metrics and excluded-section annotations. The
upstream calculator's `generate_markdown_report` is not called by IDP.

### Integration with Evaluation Service

Document split classification metrics are automatically calculated during document evaluation when both ground truth and predicted sections are available:

```python
# Automatic integration during evaluation
result_document = evaluation_service.evaluate_document(
    actual_document=processed_document,
    expected_document=expected_document
)

# Split classification results are included in evaluation output
if result_document.evaluation_result:
    split_metrics = result_document.evaluation_result.doc_split_metrics
    # Access page-level, split accuracy, and detailed analysis
```

### Use Cases

1. **Model Validation**: Assess classification model performance at page and document levels
2. **System Tuning**: Compare different splitting algorithms or thresholds
3. **Quality Assurance**: Identify systematic issues in document segmentation
4. **A/B Testing**: Compare performance of different classification approaches
5. **Continuous Monitoring**: Track classification accuracy over time

### Best Practices

1. **Prepare Ground Truth**: Ensure ground truth sections have accurate:
   - `document_class` assignments
   - `page_indices` lists
   - Consistent section identifiers

2. **Interpret Metrics Together**:
   - High page-level accuracy but low split accuracy → Correct classification but poor grouping
   - Low page-level accuracy → Review classification model
   - High split accuracy without order but low with order → Page sequencing issues

3. **Use Detailed Analysis**: Review `page_details` and `section_details` to identify specific problem areas

4. **Monitor Over Time**: Track metrics across multiple evaluation runs to detect regression

### Error Handling

The calculator gracefully handles missing or malformed data:
- Missing `extraction_result_uri`: Section skipped with warning
- Invalid page indices: Empty list with warning
- Missing document_class: Recorded as "Unknown"
- Errors logged in `metrics["errors"]` array

### Example: Complete Workflow

The standard workflow is via `EvaluationService.evaluate_document`, which
calculates doc-split metrics automatically and surfaces them on the returned
`DocumentEvaluationResult`. For direct use of the upstream calculator, see
[Usage](#usage) above.
