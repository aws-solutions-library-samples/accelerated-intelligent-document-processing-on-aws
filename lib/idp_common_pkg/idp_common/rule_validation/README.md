Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Rule Validation Service

The Rule Validation Service validates extracted document information against predefined business rules using a three-step approach: regex-based policy classification, LLM-based fact extraction, and LLM-based compliance decisioning.

## How a failure is recorded

On a **deterministic** failure this service does not raise. It records the reason in
`document.errors` and sets `Status.FAILED`, and the two rule-validation Lambdas
check that status and raise. (A transient failure does raise, from the service — see
the next section.) `document.errors` is persisted nowhere, so the
handlers are what make the reason visible: both call
[`idp_common.document_failure`](../README.md#-recording-a-document-level-failure)
to attach an error-severity `ProcessingIssue` to the affected section(s) before
re-raising. `rule_validation_failed` means this section's validation did not
complete; `rule_validation_not_consolidated` means the orchestrator's consolidation
failed, so no section has a verdict. Read that section before changing either
handler's `except` block — in particular, the original exception must propagate
unchanged and a transient failure must record nothing.

## Transient failures are retried; deterministic ones are not

This service and its orchestrator have many `except` blocks that deliberately do not
raise — they return a fallback so one bad rule does not discard the others, or a
document marked failed so the handler can record a diagnosis. Every one of them first
asks `idp_common.utils.transient_errors` whether the failure is transient, and
re-raises it under `TransientError` if it is. That is the one class name
`PolicyClassificationStep`, `RuleValidationStep` and `RuleValidationOrchestration`
list in `Retry.ErrorEquals`, so a throttle, read timeout, dropped connection or
not-ready model is retried — eight attempts at 2.5× backoff from ten seconds — rather
than becoming a permanent outcome (#1101).

**The classification lives in one place on purpose.** A `Retry.ErrorEquals` entry can
only match an exception's class name; transience is a property of the error code and
the cause chain. Listing the transient *codes* in each state would be a second copy of
the predicate that drifts, so the library decides and reports the answer under one
name. Do not add a transient code to a rule-validation state's retry list, and do not
add `TransientError` to a state whose handler cannot raise it —
`patterns/unified/tests/test_workflow_transient_retry.py` checks both directions.

⚠️ **Use `reraise_if_transient` in a block that swallows, not `raise_if_transient`.**
The latter returns silently when the exception already *is* a `TransientError`,
because it expects a bare `raise` to follow it. These blocks nest — a transient
re-raised for one rule travels up through `asyncio.gather` into
`validate_document_async`'s `except`, which returns — so using `raise_if_transient`
there leaves each site correct in isolation and swallows the inner classification
anyway. `test_a_transient_from_one_rule_is_not_swallowed_by_the_document_level_handler`
is the test for that composition.

The sites that classify, and what each returns for a deterministic failure:

| Site | Deterministic result |
|---|---|
| `_process_rule_question` | the per-rule `Information Not Found` verdict, reason in `reasoning` |
| `process_one_section`'s extraction-results load | empty results, so rules see no extracted data |
| `validate_document_async` | `Status.FAILED` plus an `errors` entry, returned not raised |
| `orchestrator._process_single_z3_rule` | the per-rule `Information Not Found` verdict |
| `orchestrator._summarize_responses` and its per-rule gather | the unsummarised responses; a failed rule is dropped |
| `orchestrator.load_section_results` | an empty mapping, read by the caller as "nothing to consolidate" |
| `orchestrator.consolidate_and_save_all` | an empty `RuleValidationResult`, **returned normally** |
| the policy-classification handler's page read and stale-result cleanup | the page is skipped / the cleanup is skipped |

⚠️ Note what the last two rows in the orchestrator mean for anyone adding a failure
path: `consolidate_and_save_all` returning normally is why the orchestration handler's
`except` almost never runs. A deterministic consolidation failure does not reach it,
so a diagnosis that must be recorded belongs inside the orchestrator, not in the
handler's `except`.

`Information Not Found` is a **verdict**, not an error channel — it is one of the
configured `recommendation_options` and downstream features act on it — so it must
never be returned because a service call failed transiently.

## Overview

The rule validation service uses a three-step approach:
1. **Policy Classification (regex)**: Determines which policy types apply to the document via filename / page-content regex patterns. No LLM call.
2. **Fact Extraction (LLM)**: Extracts relevant facts from document sections for matched policy types.
3. **Orchestrator (LLM)**: Consolidates facts and makes final compliance decisions.

## Features

- **Policy Classification**:
  - Deterministic regex matching against `x-aws-idp-document-name-regex` and `x-aws-idp-document-page-content-regex`
  - Filters rule validation to only relevant policy types
  - Reduces processing time and costs by skipping irrelevant policies
- **Three-Step Validation Workflow**:
  - Policy classification to identify applicable policy types
  - Fact extraction from document sections with intelligent chunking
  - Orchestrator consolidates facts across sections for final decisions
  - Separation of evidence gathering from compliance determination
- **Asynchronous Processing**: Handles multiple rule types and rules concurrently using asyncio
- **Rate Limiting**: Built-in semaphore-based rate limiting for API calls to prevent throttling
- **Intelligent Text Chunking**: 
  - Page-aware chunking that preserves page boundaries
  - Configurable overlap (default 10%) for context preservation. `0` repeats nothing;
    values above 50 are bounded to 50 by the character chunker
  - Automatic fallback to character-based chunking
  - Chunking always occurs for fact extraction, orchestrator always runs
- **Customizable Recommendations**: 
  - User-defined recommendation options (e.g., Pass/Fail/Info Not Found)
  - Dynamic statistics generation based on actual recommendations
  - Prompt integration with custom options
- **Comprehensive Tracking**: 
  - Token usage and cost tracking
  - Detailed timing metrics
  - Supporting page references for each rule evaluation
- **Robust Error Handling**: Graceful degradation with fallback responses and detailed error logging
- **Pydantic Validation**: Strong data validation for inputs and outputs
- **JSON Response Parsing**: Intelligent parsing of LLM responses including markdown code block handling
- **Orchestrated Consolidation**:
  - Aggregates fact extraction results across all document sections
  - Generates consolidated compliance decisions per rule
  - Creates comprehensive summaries with recommendation counts
  - Produces both JSON and Markdown output formats

## Architecture

### Service Components

1. **PolicyClassificationService** (`policy_classification.py`):
   - Deterministic regex-based classifier — no LLM call
   - Matches document ID and page-content text against policy-class regex patterns
   - Returns matched policy types and page IDs

2. **RuleValidationService** (`service.py`):
   - Section-level fact extraction for matched policy types
   - Intelligent page-aware chunking
   - Concurrent rule processing with rate limiting
   - Extracts facts with citations and relevance
   - Returns `FactExtractionResponse` with extracted_facts and extraction_summary

3. **RuleValidationOrchestratorService** (`orchestrator.py`):
   - Loads fact extraction results from S3
   - Consolidates facts across sections using LLM
   - Makes final compliance decisions per rule
   - Generates overall statistics with dynamic recommendation counts
   - Creates JSON and Markdown summary outputs
   - Always runs (even for single sections) to make compliance decisions

### Data Flow

```
Document
    ↓
Policy Classification (regex)
    ↓ (identifies applicable policy types)
Document Sections (filtered by matched policies)
    ↓
Fact Extraction (LLM)
    ↓ (stores extracted facts in S3)
    ↓ (multiple chunks per section if needed)
Orchestrator Consolidation (LLM)
    ↓ (consolidates facts → compliance decision)
Final Compliance Decision
    ↓
Output (JSON + Markdown)
```

### Three-Step Approach Details

**Step 1: Policy Classification (regex)**
- Input: Document + Policy class definitions with regex patterns
- Output: `PolicyClassificationResult`
  - `matched_policy_types`: List of applicable policy types
  - `matched_page_ids`: Page IDs where page-content regex matched
- Deterministic regex matching — no LLM call
- Subsequent steps only process matched policy types

**Step 2: Fact Extraction**
- Input: Document text + Rules (filtered by matched policy types)
- Output: `FactExtractionResponse`
  - `extracted_facts`: List of facts with citations
  - `extraction_summary`: Summary of findings
- LLM focuses on finding relevant evidence
- No compliance decision made at this stage

**Step 3: Orchestrator**
- Input: All extracted facts from all chunks/sections
- Output: `LLMResponse`
  - `recommendation`: Pass/Fail/Information Not Found
  - `reasoning`: Compliance determination explanation
  - `supporting_pages`: Aggregated page references
- LLM makes final compliance decision
- Consolidates evidence from multiple sources
- Always runs (even for single section documents)

## Models

### Core Data Models

#### FactExtractionResponse
Response model from fact extraction step:
- **policy_type**: Type of policy class being evaluated
- **rule**: The specific rule description
- **extracted_facts**: List of facts with citation and relevance
- **extraction_summary**: Summary of extracted evidence

#### LLMResponse
Validated response model from orchestrator with automatic data cleaning:
- **policy_type**: Type of policy class being evaluated (e.g., "global_periods", "same_day_service_rules")
- **rule**: The specific rule description being validated
- **supporting_pages**: List of page IDs that support the recommendation
- **recommendation**: Validated recommendation (customizable, default: Pass/Fail/Info Not Found)
- **reasoning**: Cleaned explanation text with specific page citations

Features:
- Automatic validation of recommendation against configured options
- Reasoning text cleaned of special characters
- Supporting pages validated as list format
- Whitespace automatically stripped
- `policy_type` and `rule` added by code (not from LLM response)

#### Section Response Structure
```python
{
    "section_id": "section_1",
    "chunking_occurred": True,  # True if section was chunked
    "chunks_created": 2,  # Number of chunks (0 = no chunking, 2+ = chunked)
    "responses": {
        "policy_type_1": [
            {
                "policy_type": "global_periods",
                "rule": "Rule description",
                "supporting_pages": ["1", "3"],
                "recommendation": "Pass",
                "reasoning": "Detailed reasoning with page citations"
            }
        ]
    }
}
```

#### Consolidated Summary Structure
```python
{
    "document_id": "doc_123",
    "overall_status": "COMPLETE",
    "total_policy_types": 4,
    "rule_summary": {
        "policy_type_1": {
            "status": "COMPLETE",
            "total_rules": 5,
            "Pass": 4,
            "Fail": 1
        }
    },
    "overall_statistics": {
        "total_rules": 20,
        "recommendation_counts": {
            "Pass": 15,
            "Fail": 3,
            "Info Not Found": 2
        }
    },
    "supporting_pages": ["1", "2", "3", "4"],
    "rule_details": {
        "policy_type_1": {
            "total_rules": 5,
            "recommendation_counts": {"Pass": 4, "Fail": 1},
            "rules": [...]
        }
    }
}
```

## Configuration

### Rule Validation Configuration

```yaml
rule_validation:
  model: us.anthropic.claude-sonnet-4-5-20250929-v1:0
  temperature: "0.0"
  top_k: "20"
  top_p: "0.0"
  max_tokens: "4096"
  semaphore: 5  # Concurrent API calls
  
  # Customizable recommendation options
  recommendation_options: |-
    Pass: The requirement criteria are fully met.
    Fail: The requirement is partially met or requires additional information.
    Info Not Found: No relevant data exists in the user history.
  
  system_prompt: |
    You are a specialized evaluator for medical coding compliance...
  
  task_prompt: |
    <<CACHEPOINT>>
    
    <options>
    {recommendation_options}
    </options>
    
    <document-text>
    {DOCUMENT_TEXT}
    </document-text>
    
    <policy-type>{policy_type}</policy-type>
    <rule>{rule}</rule>
```

### Rule Validation Orchestrator Configuration

```yaml
rule_validation_orchestrator:
  model: us.anthropic.claude-sonnet-4-5-20250929-v1:0
  temperature: "0.0"
  top_k: "20"
  top_p: "0.0"
  max_tokens: "4096"
  
  system_prompt: |
    You are a specialized evaluator that consolidates rule validation results...
  
  task_prompt: |
    <initial_response>
    {initial_responses}
    </initial_response>
    
    <<CACHEPOINT>>
    
    <options>
    {recommendation_options}
    </options>
    
    <criteria>
    <policy_type>{policy_type}</policy_type>
    <rule>{rule}</rule>
    </criteria>
```

### Policy Classes Configuration

```yaml
policy_classes:
  - $schema: https://json-schema.org/draft/2020-12/schema
    x-aws-idp-policy-type: global_periods
    type: object
    rule_properties:
      minor_surgery_000_010:
        type: string
        description: >-
          If a procedure has a global period of 000 or 010 days...
      major_surgery_090:
        type: string
        description: >-
          If a procedure has a global period of 090 days...
    $id: global_periods
```

## Usage

### Basic Usage with Document

```python
import yaml
from idp_common.models import Document
from idp_common.rule_validation import RuleValidationService

# Load configuration from YAML
with open('config.yaml', 'r') as f:
    config = yaml.safe_load(f)

# Initialize service with region and config
service = RuleValidationService(
    region="us-east-1",
    config=config
)

# Process document (processes all sections)
document = Document(...)  # Document with classified sections
result = service.validate_document(document)

# Results stored in S3 at:
# s3://{output_bucket}/{document.id}/rule_validation/sections/section_{id}_responses.json
```

### Orchestrated Consolidation

```python
from idp_common.rule_validation import RuleValidationOrchestratorService

# Initialize orchestrator
orchestrator = RuleValidationOrchestratorService(config=config)

# Consolidate all section results
updated_document = orchestrator.consolidate_and_save(
    document=document,
    config=config,
    multiple_sections=True
)

# Access consolidated results
print(f"Consolidated summary: {updated_document.rule_validation_result.output_uri}")
print(f"Sections processed: {updated_document.rule_validation_result.metadata['sections_processed']}")

# Results saved to s3://{output_bucket}/{document.id}/rule_validation/consolidated/:
# - consolidated_summary.json (with dynamic recommendation_counts)
# - consolidated_summary.md (Markdown report)
# - Aggregated supporting page IDs
```

### Customizing Recommendation Options

```python
# In configuration
config = {
    "rule_validation": {
        "recommendation_options": """
Approved: All requirements are satisfied.
Rejected: Requirements are not met.
Pending: Additional information needed.
        """
    }
}

# Statistics will automatically use these custom options:
# {
#   "recommendation_counts": {
#     "Approved": 10,
#     "Rejected": 2,
#     "Pending": 3
#   }
# }
```

## Workflow Details

### 1. Section-Level Evaluation (Async)

For each document section:
1. Extract text from section pages with page markers
2. Check if chunking is needed based on token limits
3. If chunking required:
   - Split by page boundaries
   - Add overlap from previous chunk
   - Preserve page markers
4. **Evaluate all rules concurrently** using asyncio:
   - Each rule type processed in parallel
   - Each rule within a type processed in parallel
   - Semaphore controls concurrent API calls
5. Store section responses in S3

### 2. Rule Type Consolidation (Async)

For each rule type:
1. Load all section responses from S3
2. Group responses by rule
3. **For each rule with multiple responses (processed concurrently)**:
   - Send to LLM for consolidation
   - LLM analyzes all evidence
   - Generates single consolidated recommendation
   - Aggregates supporting pages
4. Save consolidated responses per rule type

### 3. Orchestrated Summarization

1. Load all consolidated rule type responses
2. Generate statistics:
   - Count total rules
   - Dynamically count each recommendation type
   - Calculate per-rule-type breakdowns
3. Collect all supporting pages
4. Create JSON summary
5. Generate Markdown report
6. Save both formats to S3

## Page Number Handling

Page numbers in `supporting_pages` are actually **page IDs** from the document structure:

```python
# In service.py - page markers are added during text preparation
all_text += f"<page-number>{page_id}</page-number>\n{page_text}\n\n"

# LLM sees these markers and references them in responses
{
    "supporting_pages": ["1", "3", "4"],  # These are page IDs
    "reasoning": "Evidence found on pages 1, 3, and 4..."
}

# Orchestrator aggregates all page IDs
"supporting_pages": ["1", "2", "3", "4", "5"]  # Sorted unique page IDs
```

### The consolidated aggregate is always a list of strings

The document-level `supporting_pages` in `consolidated_summary.json` holds `str`
elements, deduplicated by that string and ordered by codepoint within two groups:
numeric references first by numeric value, then everything else by its own text. That
is the same element type `LLMResponse.supporting_pages` declares and coerces to.

Its one consumer in this repository is the sample health-insurance review extension,
whose claim-detail API serves it as `supportingPages` against a TypeScript interface
declaring `string[]`. The Athena `supporting_pages` column is **not** fed from this
list — it belongs to the per-rule `rule_details` rows, which are left as the responses
delivered them.

This matters because the two engines produce different shapes. The solver path builds
its pages from `str(citation).split(",")` and always yields `str`; the model path
passes a JSON array through unchanged and can yield `int`. A document routing some
rules to each mixes both in one aggregate, and unnormalised that listed page `1` twice
— once as `1` and once as `"1"`.

The **per-rule** lists under `rule_details[<policy_type>]["rules"][*]`
["supporting_pages"] are *not* normalised: they stay exactly as the response
delivered them, and are what the Markdown table formats. So a value dropped from the
aggregate — a `list`, a `dict`, a boolean, anything that is not text or a number — is
still recorded there, with a warning naming it in the log.

Two page shapes are worth knowing about because `int()` refuses them while
`str.isdigit()` accepts them: the digit characters that are not decimal digits
(`'²'`, `'₂'`, `'②'`), and a decimal string longer than
`sys.get_int_max_str_digits()`. Both are kept as page references and ordered as text.
`str.isdecimal()` — not `isdigit()` — is the predicate that matches what `int()`
accepts. Note that a reference is not length-bounded: a model that returns a
5,000-digit page reference puts a 5,000-character string in the artifact, which is
better than losing the report to it but is not a validated page id.

The solver path's **per-rule** page list (`_process_z3_cross_section_rule`) sorts with
the same `int(x) if x.isdigit() else 0` key this aggregate used to. It cannot raise
there, because it builds its elements with `str(citation).split(",")` so every one is
a `str`, but non-numeric citations still share one key and so still have no defined
order among themselves.

### A consolidation that fails keeps its statistics

`_generate_consolidated_summary` does not raise: the caller writes whatever it
returns to S3 as the document's compliance report. On an unexpected failure it
returns the summary built **so far** — the policy types already counted, their
rules, the document-level counts and the pages collected — with `overall_status`
`"ERROR"` and the reason in `error`, and `_format_summary_as_markdown` renders that
reason as a banner above the statistics. A report carrying zero rules and no error is
indistinguishable from a document on which nothing was evaluated, which is why the
partial statistics are kept rather than discarded.

Those figures are a floor on what was evaluated, and they are internally consistent:
a rule is counted once its fields have been read, so `total_rules` never exceeds the
rules the report details and `pass_percentage` is computed over the rules counted. A
policy type whose responses failed part way through keeps the rules already read; one
whose response list could not be read at all gets no entry, since there is nothing
partial to report for it.

Because nothing re-raises there, this path records **no** `ProcessingIssue`:
`rule_validation_not_consolidated` is attached by the orchestration Lambda's handler,
and only when the handler itself raises (see "How a failure is recorded" above). The
surviving statistics, the `error` field, the rendered banner and a logged traceback
are what make an instance of this visible.

## Intelligent Chunking

### Page-Aware Chunking

```python
# Splits text by page markers
page_pattern = r"<page-number>(\d+)</page-number>\s*"
pages = [(page_id, content), ...]

# Builds chunks respecting page boundaries
chunk_text = "<page-number>1</page-number>\nPage 1 content\n\n<page-number>2</page-number>\nPage 2 content"

# Adds overlap from previous chunk
if previous_chunk_pages:
    # Include last page or partial content from previous chunk
    overlap_text = build_overlap(previous_chunk_pages)
    chunk_text = overlap_text + current_chunk_text
```

### Fallback Chunking

Taken only when the page parser finds **no** pages at all — not merely when a
document carries no markers, which is parsed as a single page numbered `0`. The way
in is `<page-number>` markers with no content between any of them, which a section of
whitespace-only OCR text produces: the split pattern's trailing `\s*` consumes the
whitespace, so every page strips to empty and is dropped while those characters still
count toward the length that decides whether chunking is needed at all.

- Falls back to character-based chunking
- Uses configurable overlap percentage, bounded at half a chunk so that the number
  of chunks — and so of model calls — stays within twice the no-overlap count. A
  request above that is reduced and logged
- ⚠️ Slices at raw character offsets. It does **not** preserve word boundaries, so a
  boundary can fall inside a word or a number; the overlap is what keeps a value
  split that way readable in the following chunk

## Error Handling

### Graceful Degradation

```python
# If LLM response parsing fails
response_dict = {
    "policy_type": policy_type,
    "rule": rule,
    "supporting_pages": [],
    "recommendation": "Error",
    "reasoning": f"Failed to parse response: {response_text}"
}
```

### Validation Errors

- Invalid recommendations are caught by Pydantic validation
- Custom recommendations must match configured options
- Missing required fields trigger validation errors

### Readings the Z3 engine refuses to evaluate

A parameter value reaches the Z3 solver by one of three routes — path-based
extraction (`z3/data_extractor.py`), LLM extraction (`z3/rule_translator.py`,
which is both the default for a rule with no `path_mappings` and the fallback
when path extraction fails), and the orchestrator's production fact-extraction
call, which hands parsed JSON straight to `Z3Validator.validate`. All three end at
`Z3Validator._bind_values`.

For the numeric types all three go through `z3/type_coercion.py`, so what counts
as a valid reading has one definition rather than one per route:

| Declared | Accepted | Refused |
|---|---|---|
| `Int` | any reading that denotes a whole number, whatever its Python type or spelling — `42`, `42.0`, `Decimal("42.0")`, `Fraction(42)`, `"42"`, `"42.0"`, `"1.5e3"`. Arbitrary precision: an exact 401-digit integer binds | a fractional reading (`30.9`, `Decimal("30.9")`, `Fraction(309,10)`, `"30.9"`), a reading merely *close* to whole (`29.999999999999996`), a `bool`, a non-numeral, infinity, NaN |
| `Real` | any numeral, whole or fractional, that has a `float` to be recorded as. **Bound to the solver exactly**, as a rational — `Decimal("0.1000000000000000000001")` is not equal to `0.1` | a `bool`, a non-numeral, infinity, NaN, a magnitude larger than any `float` |

An `Int` reading is refused rather than truncated because `int()` rounds toward
zero, moving the reading by up to a whole unit. That is enough to flip the verdict
of any rule with an integer threshold, and to flip it in either direction
depending on the comparator, so no rounding rule is sound: `days_late <= 30` read
as 30.9 would report a Pass if truncated to 30, and `days_late >= 31` would report
a Pass if rounded to 31.

**Exact is not the same as recorded.** There are two entry points, and the
difference matters when reading a result:

- `exact_numeric_reading` returns an `int` or a `Fraction` and is what
  `Z3Validator._bind_values` binds and what `_parse_smt_atom` parses the
  constraint's own decimal literals with. Rounding either operand to a double
  first decides an equality constraint below the 17th significant digit, and a
  `Decimal` from DynamoDB carries up to 38. Both operands go through it, because
  making one exact and leaving the other collapsed is wrong in the other
  direction.
- `coerce_numeric_reading` returns an `int` or a `float` and is what goes into
  `extracted_values` and `model`, which are serialised to JSON. A `Real` there is
  the nearest double to the reading, so a magnitude below the smallest double is
  recorded as `0.0` while the verdict is still decided on the reading. The two
  accept and refuse identically — the second is written in terms of the first —
  and differ only in representation.

A refusal raises `ValidationError`. What that means for the rule depends on where
it happens:

- **At binding**, it is terminal for that rule, and every caller renders it as
  **Information Not Found**: the orchestrator's `_run_z3_validation` and
  `Z3RuleEngine.validate_rule` put the reason in the rule's `reasoning`, and
  `ValidationSystem.validate_batch` records an error `ValidationResult` and carries
  on with the remaining rules.
- **At LLM extraction**, `RuleTranslator._parse_extraction_output` raises
  `TranslationError` instead, and `Z3RuleEngine._extract_values` treats that as one
  failed attempt: it retries extraction against the document text. So a refused
  reading there costs a second Bedrock call, and the rule still gets a verdict if
  the retry answers a whole number. The verdict is derived from whatever reading
  the solver actually saw, and that reading is the one reported.

`Bool` and `String` keep their per-route conversions. They agree on everything
except an integer read for a `Bool`: path extraction truthies it, so **every**
non-zero integer becomes `True` — lossless for `1`, a guess for `37` — while
binding refuses a non-bool, non-string reading outright. No verdict is derived
from the guess, because the strict route refuses, which is why this is pinned
rather than changed; `tests/unit/rule_validation/test_z3_type_coercion.py` records
its full width so closing it stays a deliberate change.

One rough edge worth knowing: the `reasoning` a refused rule carries is the
exception's full log formatting — component, operation, rule id, message, context
dict and timestamp. That is the shape every Z3 error has had in that field; it is
useful in logs and noisy in a compliance report.

## Performance Considerations

### Rate Limiting

```python
# Semaphore controls concurrent API calls
semaphore: 5  # Max 5 concurrent requests

# In code
async with self.semaphore:
    response = await self._invoke_model_async(...)
```

### Token Optimization

- Prompt caching with `<<CACHEPOINT>>` marker
- Static content before marker is cached
- Dynamic content (document text, options) after marker
- Reduces token costs for repeated evaluations

### Metrics Tracking

```python
# Automatic token tracking
self.token_metrics = utils.merge_metering_data(
    self.token_metrics, 
    metering or {}
)

# Includes:
# - inputTokens, outputTokens, totalTokens
# - requests count
# - Per-context breakdown
```

## Output Formats

### JSON Summary

```json
{
  "document_id": "doc_123",
  "overall_status": "COMPLETE",
  "total_policy_types": 4,
  "rule_summary": {...},
  "overall_statistics": {
    "total_rules": 20,
    "recommendation_counts": {
      "Pass": 15,
      "Fail": 3,
      "Info Not Found": 2
    }
  },
  "supporting_pages": ["1", "2", "3"],
  "rule_details": {...},
  "generated_at": "2026-01-22T19:00:00"
}
```

### Markdown Report

```markdown
# Rule Validation Summary

**Document ID**: doc_123
**Status**: COMPLETE
**Total Rule Types**: 4
**Total Rules**: 20

## Overall Statistics

**Recommendation Counts:**
- **Pass:** 15
- **Fail:** 3
- **Info Not Found:** 2

**Supporting Pages:** 1, 2, 3

## Rule Type: global_periods

**Total Rules:** 5

**Pass:** 4
**Fail:** 1

### Rules

| Rule | Recommendation | Reasoning | Supporting Pages |
|------|----------------|-----------|------------------|
| Minor surgery rule | Pass | Evidence found... | 1, 3 |
...
```

## Integration with IDP Pipeline

The rule validation service integrates with the IDP pipeline through:

1. **Document Model**: Uses Document object with classified sections
2. **S3 Storage**: Stores intermediate and final results in S3
3. **Step Functions**: Orchestrated through AWS Step Functions workflow
4. **Lambda Functions**:
   - `rule-validation-function`: Executes section-level evaluation
   - `rule-validation-orchestration-function`: Executes orchestration
5. **Status Tracking**: Updates document status through pipeline

## Testing with Notebooks

Use Jupyter notebooks to test and validate rule validation functionality. See `notebooks/misc/e2e-holistic-packet-classification-rule-validation.ipynb` for a complete end-to-end example.

### Setup

```python
# Install IDP common package
%pip install -e "../../lib/idp_common_pkg[dev, all]"

import yaml
from idp_common.models import Document, Status
from idp_common.rule_validation import RuleValidationService
from idp_common import s3

# Load configuration
config_path = "../../config_library/unified/rule-validation/config.yaml"
with open(config_path, 'r') as f:
    config = yaml.safe_load(f)

print(f"Model: {config['rule_validation']['model']}")
```

### Process Document Sections

```python
# Initialize service
rule_validation_service = RuleValidationService(
    region=region,
    config=config
)

# Process each section individually
section_results = []

for section in document.sections:
    print(f"Processing section {section.section_id} ({section.classification})")
    
    # Create document with only this section
    section_document = Document(
        id=document.id,
        input_key=document.input_key,
        input_bucket=document.input_bucket,
        output_bucket=document.output_bucket,
        pages=document.pages,
        sections=[section],  # Only this section
        status=document.status,
        metering=document.metering.copy() if document.metering else {}
    )
    
    # Create fresh service instance (avoids asyncio semaphore issues in notebooks)
    section_service = RuleValidationService(region=region, config=config)
    
    # Process the section
    section_result = section_service.validate_document(section_document)
    section_results.append(section_result)
    
    print(f"Completed section {section.section_id}")
```

### View Section Results

```python
# Check section results
for i, section_result in enumerate(section_results):
    section_id = document.sections[i].section_id
    if hasattr(section_result, 'rule_validation_result'):
        rv_result = section_result.rule_validation_result
        section_uri = rv_result.metadata.get('section_output_uri')
        print(f"Section {section_id}: {section_uri}")
        
        # Load and view section responses
        section_data = s3.get_json_content(section_uri)
        print(f"  Rules evaluated: {len(section_data.get('responses', {}))}")
```

### Test Orchestrator (Consolidation)

```python
from idp_common.rule_validation import RuleValidationOrchestratorService

# Initialize orchestrator
orchestrator = RuleValidationOrchestratorService(config=config)

# Consolidate all section results
updated_document = orchestrator.consolidate_and_save(
    document=document,
    config=config,
    multiple_sections=True
)

print("Consolidation complete")
print(f"Summary URI: {updated_document.rule_validation_result.output_uri}")

# View consolidated summary
summary_uri = updated_document.rule_validation_result.output_uri
summary = s3.get_json_content(summary_uri)

print("\nOverall Statistics:")
print(json.dumps(summary['overall_statistics'], indent=2))

print("\nRule Summary:")
for policy_type, stats in summary['rule_summary'].items():
    print(f"\n{policy_type}:")
    print(f"  Total rules: {stats['total_rules']}")
    for rec, count in stats.items():
        if rec not in ['status', 'total_rules']:
            print(f"  {rec}: {count}")
```

### Test Custom Recommendations

```python
# Modify config for custom recommendations
config['rule_validation']['recommendation_options'] = """
Compliant: Fully meets regulatory requirements.
Non-Compliant: Does not meet requirements.
Requires Review: Manual review needed.
"""

# Reinitialize service with custom config
custom_service = RuleValidationService(region=region, config=config)

# Process with custom recommendations
result = custom_service.validate_document(section_document)

# Statistics will use custom options:
# {"Compliant": 10, "Non-Compliant": 2, "Requires Review": 3}
```

**Important Notes**:
- Create a fresh `RuleValidationService` instance for each section in notebooks to avoid asyncio semaphore issues
- Section results are stored in S3 at `{document_id}/rule_validation/sections/section_{id}_responses.json`
- Consolidated results are at `{document_id}/rule_validation/consolidated/`
- The orchestrator's `consolidate_and_save()` method handles async operations internally - no need to use `asyncio.run()`

For complete workflow examples, see `notebooks/misc/e2e-holistic-packet-classification-rule-validation.ipynb`.

## Best Practices

### Configuration

- Place `{recommendation_options}` AFTER `<<CACHEPOINT>>` for proper caching
- Use temperature 0 for deterministic rule evaluation
- Adjust semaphore based on API rate limits
- Define clear, distinct recommendation options

### Rule Design

- Write specific, measurable rule descriptions
- Include clear success criteria
- Reference specific document sections or fields
- Provide examples in rule descriptions

### Performance

- Use page-level classification to create focused sections
- Limit section size to avoid excessive chunking
- Monitor token usage and adjust max_tokens if needed
- Use appropriate models (faster models for simple rules)

### Troubleshooting

- Check section response files in S3 for intermediate results
- Review consolidated responses for rule-level details
- Examine Markdown report for human-readable summary
- Monitor CloudWatch logs for detailed execution traces

## Version History

- **v2.0**: Added rule validation orchestrator with dynamic recommendations
- **v1.5**: Implemented page-aware chunking
- **v1.0**: Initial release with section-level evaluation

## Contributors

- GenAI IDP Accelerator Team
