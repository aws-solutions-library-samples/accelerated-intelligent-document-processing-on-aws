Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Document Classification for IDP Accelerator

This module provides document classification capabilities for the IDP Accelerator project, allowing classification of documents based on their text and image content. It supports multiple classification backends including Bedrock LLMs and SageMaker UDOP models.

## Features

- Classification of documents using multiple backend options:
  - Amazon Bedrock LLMs
  - SageMaker UDOP models
- **Optional regex-based classification for enhanced performance**
  - Document name regex matching when all pages should be classified as the same class
  - Page content regex matching for multi-modal page-level classification
- **Valid-class enforcement with re-prompt/retry** (page-level) — rejects
  out-of-vocabulary predictions and re-prompts the model with the allowed classes
- Direct integration with the Document data model
- Support for both text and image content
- Concurrent processing of multiple pages
- Structured data models for results
- Grouping of pages into sections by classification
- Comprehensive error handling and retry mechanisms
- **DynamoDB caching for resilient page-level classification**
- **Sequence segmentation using BIO-like approach for document boundary detection**

## Sequence Segmentation Approach

The multimodal page-level classification method implements a sequence segmentation approach similar to BIO (Begin-Inside-Outside) tagging commonly used in NLP. This enables accurate segmentation of multi-document packets where a single file may contain multiple distinct documents.

### How It Works

Each page receives two pieces of information:
1. **Document Type**: The classification label (e.g., "invoice", "letter", "financial_statement")
2. **Document Boundary**: A boundary indicator that signals document transitions:
   - `"start"`: Indicates the beginning of a new document (similar to "Begin" in BIO)
   - `"continue"`: Indicates continuation of the current document (similar to "Inside" in BIO)

### Benefits

- **Multi-Document Packet Support**: Accurately segments packets containing multiple documents
- **Type-Aware Boundaries**: Detects when a new document of the same type begins
- **Automatic Section Creation**: Pages are grouped into sections based on both type and boundaries
- **Improved Accuracy**: Context-aware classification that considers document flow

### Example Segmentation

Consider a packet with 6 pages containing two invoices and one letter:

```
Page 1: type="invoice", boundary="start"      → Section 1 (Invoice #1)
Page 2: type="invoice", boundary="continue"   → Section 1 (Invoice #1)
Page 3: type="letter", boundary="start"       → Section 2 (Letter)
Page 4: type="letter", boundary="continue"    → Section 2 (Letter)
Page 5: type="invoice", boundary="start"      → Section 3 (Invoice #2)
Page 6: type="invoice", boundary="continue"   → Section 3 (Invoice #2)
```

The system automatically creates three sections, properly separating the two invoices despite them having the same document type.

### Where the `document_boundary` signal lives

The boundary indicator is carried in `PageClassification.classification.metadata["document_boundary"]` and is consumed by `_create_llm_determined_sections` / `_group_consecutive_pages`. It is also copied onto the declared `Page.document_boundary` field, which **is** persisted — it appears in the `document.json` page dict, in the DynamoDB page record as `Boundary`, in the `create_document_run` snapshot, and on the GraphQL `Page` type. So a merge decision can be inspected after the fact rather than re-derived from Lambda logs.

(Note the older `setattr(page, "metadata", ...)` call next to it stashes the whole metadata dict on an attribute that is *not* a dataclass field, so that one does not survive serialization — `Page.document_boundary` is the durable one.)

To keep merges auditable, `_create_llm_determined_sections` logs the complete page → boundary map on one line before building sections:

```
Page document_boundary signals: {'1': 'start', '2': 'continue', '3': '(absent)'}
```

`(absent)` means the model omitted the field, in which case the code defaults it to `"continue"`. That is deliberately reported as distinct from an explicit `"continue"`: an omitted signal merges pages *by accident*, while an explicit `"continue"` is the model's judgement. Distinguishing the two is what makes an unexpected merge diagnosable.

### A class description can override the boundary rules (`PRECEDENCE`)

`<boundary-detection-rules>` in the default `classification.task_prompt` ends with a `PRECEDENCE:` clause that promotes a document type's **own** boundary instructions above the generic rules. `_format_classes_list()` puts every class's `description` into `{CLASS_NAMES_AND_DESCRIPTIONS}`, so that clause makes the description the supported place to state a per-class rule — no code path or config key is involved.

This is the fix for the one shape the generic rules get wrong ([#750](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/750)): a long table that reprints its title and column headers on every page and paginates with a bare number. Rule 3 (opening header block ⇒ `start`) outranks rule 4 (continuation evidence ⇒ `continue`), the running header satisfies rule 3, and a lone `7` matches none of rule 1's pagination patterns — so continuation pages are read as new documents. Measured on `us.amazon.nova-2-lite-v1:0` at `temperature: 0`, a 16-page fund table split into 2–5 sections in 10 of 10 runs; with a `BOUNDARY:` sentence in the class description, 1 section in 10 of 10.

Working example: `scripts/sdlc/config/nuveen.yaml` (the CI Step 8 config), guarded by `scripts/tests/test_nuveen_boundary_precedence.py` — which also asserts the `PRECEDENCE:` clause still exists, since removing it would silently disable every class-level instruction that depends on it. Name `"start"` / `"continue"` explicitly in the description; those are the values `document_boundary` takes.

Two things this is **not** a workaround for, both measured: prompt-level wording changes that generalise the rule (they regress the no-pagination over-split case) and `contextPagesCount: 1` (it regresses back-to-back duplicates). See [docs/classification.md](../../../../docs/classification.md#known-failure-mode-repeating-running-headers) for the numbers.

### Classification confidence (`confidence`, `classification_reason`)

Two OPTIONAL keys in the model's response are parsed alongside `class` and
`document_boundary` ([#673](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/673)).
Neither used to be read by anything, even though `confidence` was documented as
supported and `classification_reason` is asked for by the **default** prompt.

| Key | Lands on | Persisted as |
|-----|----------|--------------|
| `confidence` | `DocumentClassification.confidence` → `Page.confidence` | `ClassConfidence` (DynamoDB), `ClassConfidence` (GraphQL `Page`), `confidence` (document.json) |
| `classification_reason` | `metadata["classification_reason"]` → `Page.classification_reason` | `ClassReason` (DynamoDB + GraphQL), `classification_reason` (document.json) |

`parse_confidence` does the reading: it accepts `0.95`, `"0.95"`, `95` and
`"95%"`, and returns `None` for anything unparseable or out of range rather than
raising — a malformed confidence must not fail a classification that otherwise
worked.

**`None` means NOT SCORED, and is never serialized as a number.** This is the
whole point of the change: `Page.confidence` used to default to a fabricated
`1.0`/`0.0`, and that value flowed to the reporting lake's `section_confidence`
column where it was indistinguishable from a real one. `1.0` now appears only
where something genuinely asserts the class — a document-name regex match
(`_pin_page_class`), a single-class configuration, a page-content regex match, or
a human correction in the Web UI — and `0.0` only on an errored page.

Three paths deliberately produce no score: the SageMaker/UDOP backend (the
endpoint returns none), pages beyond `maxPagesForClassification` (their class is
extrapolated from a sample, not predicted), and a page whose invalid class was
replaced by `invalidClassFallback` (the stored class is no longer the model's, so
neither its score nor its reasoning describes it — both are dropped, including
across a validation retry).

Section confidence is `aggregate_page_confidence`: the **minimum** across the
section's pages, and `None` if any page is unscored. Min because a mean hides the
one page the classifier was unsure about, and because a section cannot be more
certain than its weakest page; `None`-absorbing because the min of a *scored
subset* would present a partial aggregate as a whole-section number.

⚠️ Page-level classification is one inference **per page**, so anything added to
its output format multiplies by page count — unlike extraction's confidence, which
is per section. That is why the default was measured before being turned on:
`topk` costs +17 % of the classification step, which is ~3 % of total document cost
on the default model (so ~0.5 % of the bill) and changes accuracy by nothing
consistent. `mode: off` restores the zero-cost path exactly. See
`docs/benchmarking/classification-confidence.md`, and note the finding that a
*small* classifier's score is a coarse two-level flag while a mid-tier one's is
graded — the mode being on does not make the number equally useful everywhere.

`_apply_page_result` is the single place that copies a result onto the declared
`Page` fields (class, confidence, reason, candidates, boundary), shared by the
cache-hit and fresh-inference branches so a cache hit cannot yield a page with
fewer signals than a miss.

#### Asking for it: `classification.confidence.mode`

`topk` (**default**), `verbalized`, or `off`. `class_confidence.py` owns both halves:

- **Prompt assembly.** `append_class_confidence_block` splices
  `classification.confidence.task_prompt_topk` / `task_prompt_verbalized` into
  the task prompt **before** the first `<<CACHEPOINT>>` / `<document-ocr-data>` /
  `{DOCUMENT_TEXT}` marker, so the static instruction stays inside the
  prompt-cache prefix — the same rule, for the same reason, as
  `extraction/prompt_assembly.py::_append_bbox_block`, and it matters more here
  because classification runs per page. The splice is idempotent (a prompt that
  already carries a `<class-confidence>` block is left alone) and composed only
  for `multimodalPageLevelClassification`; the holistic method logs a warning and
  is left to ask in its own prompt, because its response is a segment list rather
  than one object per page.
- **Resolution.** `resolve_class_and_confidence` prefers an explicit
  `confidence`, else `confidence_from_candidates`, which returns the probability
  of the class being **stored** — not the top of the list. Those differ when the
  model's `class` contradicts its own ranking, and reporting the top probability
  would then describe a class the page was never given; a stored class absent
  from its own candidate list leaves the page unscored rather than inferring a
  number from the leftover mass.

`parse_candidates` drops out-of-vocabulary classes (a class that cannot be stored
cannot be reported), deduplicates on the highest probability, and rescales **only
when the probabilities sum to more than 1.0** — a distribution cannot exceed 1,
whereas a sum below 1 legitimately means "possibly some other class" and
inflating the top candidate to absorb it would manufacture confidence.

`resolve_top_k` clamps the requested count —
`classification.confidence.top_k_candidates` (default **3**) — to
`[2, len(valid_doc_types)]`: asking for more candidates than there are classes
invites invented ones, and a single candidate is a verbalized confidence with
extra syntax — the calibration benefit comes precisely from having to rank
alternatives.

Ranked candidates land on `Page.classification_candidates`, persist as
`ClassCandidates` (a list of `{Class, Probability}` maps), and are exposed as
`Page.ClassCandidates` on the API and in the UI popover.

#### Measuring whether the number means anything

A confidence nobody checks is worse than none. `attach_page_confidence`
(`evaluation/stickler_backend/doc_split.py`) annotates each row of the evaluation
report's `doc_split_metrics.page_details` with `predicted_confidence`, next to the
`correct` flag that is already there. The benchmark harness
(`benchmarks/harness/analyze.py::score_classification`) turns those rows into
`class_calibration_separation` = mean(conf | correct) − mean(conf | wrong), with
`class_accuracy` and `n_class_scored_pages` beside it, and `aggregate.py` treats a
−0.03 move as a regression on the same footing as the field-level separation.
Near-zero separation means the model is equally confident right and wrong — do not
route work on it.

### Section splitting strategies

`classification.sectionSplitting` controls how classified pages become sections:

| Value | Behavior |
|-------|----------|
| `llm_determined` (default) | Group pages using the `document_boundary` signal described above |
| `page` | One section per page — no same-type joining is ever possible |
| `disabled` | One section spanning the whole document (class chosen by majority vote across pages) |

### Single-class configurations

When the configuration defines exactly **one** document class, the *class* decision is predetermined — there is nothing for the model to choose. But section **boundaries** are a separate question, and the backend is only skipped when the configured strategy genuinely needs no model output (GitHub issue [#686](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/686) — previously the short-circuit hard-coded one all-pages section and `sectionSplitting` was silently ignored):

| `sectionSplitting` | Single-class behavior | Backend call? |
|--------------------|-----------------------|---------------|
| `disabled` | One section over all pages | No |
| `page` | One section per page | No |
| `llm_determined`, single-page document | One section | No — one page cannot be split |
| `llm_determined`, multi-page document | Real boundary detection | **Yes** |

`llm_determined` asks for LLM boundary detection, so it performs it. A knob that silently does nothing is a bug, not an optimization — and the old behaviour was actively harmful for the case it mattered most: a packet holding several separate documents of the single class became one section, and extraction then returned only the first record.

**Cost note.** `llm_determined` is the *default*, so a **multi-page** single-class deployment that never set `sectionSplitting` now performs classification inference where it previously performed none. Single-page documents are unaffected (nothing to split, so the call is skipped), which covers the common "one document per file" deployment at zero cost.

If one input file is always exactly one document and you want to keep the zero-cost path on multi-page files, set it explicitly:

```yaml
classification:
  sectionSplitting: disabled   # one file is always one document
```

`page` remains available when you want one section per page without inference.

## Regex-Based Classification for Enhanced Performance

The classification service now supports optional regex-based pattern matching to provide significant performance improvements and deterministic classification for known document patterns. This feature enables instant classification without LLM API calls when regex patterns match.

### Document Name Regex Classification

When you want all pages of a document to be classified the same way, document name regex patterns can instantly classify entire documents based on their filename or ID:

```yaml
classes:
  - name: Payslip
    description: "Employee wage statement showing earnings and deductions"
    document_name_regex: "(?i).*(payslip|paystub|salary|wage).*"
    attributes:
      - name: EmployeeName
        description: "Name of the employee"
        attributeType: simple
```

**How it works:**
- Works with any number of document classes defined in configuration
- When document ID matches the regex pattern, all pages are classified as that class
- Skips the LLM *class* decision entirely — the name is authoritative, and it stays authoritative even when the model does run (see below)
- Provides info-level logging when matches occur

#### …and `sectionSplitting` still applies

A filename tells you **what** the packet holds; it never tells you **where** one record ends and the next begins. So the name match short-circuits the class decision only — section boundaries follow `sectionSplitting`, exactly as for [single-class configurations](#single-class-configurations) (GitHub issue [#705](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/705) — previously this path hard-coded one all-pages section and `sectionSplitting` was silently ignored, the same defect as [#686](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/686) reached through a different trigger):

| `sectionSplitting` | Name-matched behavior | Backend call? |
|--------------------|-----------------------|---------------|
| `disabled` | One section over all pages | No |
| `page` | One section per page | No |
| `llm_determined`, single-page document | One section | No — one page cannot be split |
| `llm_determined`, multi-page document | Real boundary detection, class pinned to the matched class | **Yes** |

In the last row the model is invoked for the per-page `document_boundary` signal (or, under holistic classification, for the segment *ranges*) and its class output is discarded in favour of the regex match — so a packet of five payslips becomes five sections, all classified `Payslip`.

**Cost note.** `llm_determined` is the *default*, so a name-matched **multi-page** document now performs classification inference where it previously performed none: for those documents the regex is no longer a "skip the LLM" shortcut. Single-page matches are unaffected and keep the zero-inference path. To keep multi-page files at zero cost when one file is always one document, set it explicitly:

```yaml
classification:
  sectionSplitting: disabled   # one file is always one document
```

`page` also stays inference-free when you want one section per page.

### Page Content Regex Classification

For multi-modal page-level classification, page content regex patterns can classify individual pages based on text content:

```yaml
classes:
  - name: Invoice
    description: "Business invoice document"
    document_page_content_regex: "(?i)(invoice\\s+number|bill\\s+to|amount\\s+due)"
    attributes:
      - name: InvoiceNumber
        description: "Invoice number"
        attributeType: simple
  - name: Payslip
    description: "Employee wage statement"  
    document_page_content_regex: "(?i)(gross\\s+pay|net\\s+pay|employee\\s+id)"
    attributes:
      - name: EmployeeName
        description: "Employee name"
        attributeType: simple
```

**How it works:**
- Only applies to multi-modal page-level classification method
- Each page's text content is checked against all class regex patterns
- First matching pattern wins and classifies the page instantly
- Falls back to LLM classification when no patterns match
- Provides info-level logging when matches occur

### Configuration Options

Both regex types are optional and can be used together:

```yaml
classes:
  - name: W2-Form
    description: "W2 tax form with wage and tax information"
    # Both regex types can be specified
    document_name_regex: "(?i).*w-?2.*"  # For single-class scenarios
    document_page_content_regex: "(?i)(form\\s+w-?2|wage\\s+and\\s+tax)"  # For page-level
    attributes:
      - name: EmployerEIN
        description: "Employer identification number"
        attributeType: simple
```

### Performance Benefits

**Speed Improvements:**
- Regex matching is nearly instantaneous compared to LLM calls
- Document name regex: ~100-1000x faster (entire document classified instantly)
- Page content regex: ~10-50x faster per matched page

**Cost Savings:**
- Zero token usage for regex-matched classifications
- No Bedrock/SageMaker API calls for matched patterns
- Significant cost reduction for documents with recognizable patterns

**Deterministic Results:**
- Consistent classification results for pattern-matched documents
- Eliminates LLM variability for known document types
- Reliable classification for high-volume processing scenarios

### Best Practices for Regex Patterns

1. **Case-Insensitive Matching**: Use `(?i)` flag for robust matching
   ```regex
   (?i).*(invoice|bill).*  # Matches "Invoice", "INVOICE", "bill", "BILL"
   ```

2. **Flexible Whitespace**: Use `\\s+` for varying whitespace
   ```regex
   (?i)(gross\\s+pay|net\\s+pay)  # Matches "gross pay", "gross  pay", "GROSS PAY"
   ```

3. **Multiple Alternatives**: Use `|` for different possible terms
   ```regex
   (?i).*(payslip|paystub|salary|wage).*  # Matches any of these terms
   ```

4. **Specific Enough**: Balance specificity to avoid false matches
   ```regex
   # Good: Specific to payslips
   (?i)(gross\\s+pay|employee\\s+id|pay\\s+period)
   
   # Too broad: Could match many document types
   (?i)(pay|id|period)
   ```

### Error Handling

The regex system includes comprehensive error handling:

- **Compilation Errors**: Invalid regex patterns are logged and ignored, fallback to LLM
- **Runtime Errors**: Regex matching failures fallback to standard classification
- **Graceful Degradation**: System continues to work normally even with invalid patterns
- **Detailed Logging**: Debug and error logs help with pattern troubleshooting

### Integration Example

```python
from idp_common import classification, get_config
from idp_common.models import Document

# Load configuration with regex patterns
config = get_config()

# Initialize service - regex patterns are automatically used
service = classification.ClassificationService(
    region="us-east-1",
    config=config,
    backend="bedrock"
)

# Classification automatically uses regex when patterns match
document = service.classify_document(document)

# Check if regex was used
for page_id, page in document.pages.items():
    metadata = getattr(page, 'metadata', {})
    if metadata.get('regex_matched', False):
        print(f"Page {page_id} was classified using regex patterns")
    else:
        print(f"Page {page_id} was classified using LLM")
```

### Demonstration Notebook

See `notebooks/examples/step2_classification_with_regex.ipynb` for interactive demonstrations of:
- Document name regex classification
- Page content regex classification  
- Performance comparisons between regex and LLM methods
- Configuration examples and best practices
- Error handling scenarios

## Enforcing a Valid Class Vocabulary (Validation + Retry)

For `multimodalPageLevelClassification`, the service can guarantee the predicted
class is always one of the configured classes. After each LLM call, the
predicted class is validated against `self.valid_doc_types` (built from the
configured `classes`). On an out-of-vocabulary prediction the service
re-prompts the model — re-sending the original request content with an appended
correction message that lists the allowed classes — and retries up to a
configurable limit. Because classification runs at `temperature=0`, this
single-turn re-prompt (rather than an identical re-send) is what lets the model
change its answer.

Implemented in `ClassificationService.classify_page_bedrock` with the helper
`_build_validation_retry_content`. Metering is aggregated across all attempts.

```yaml
classification:
  enforceValidClasses: true       # default: true
  maxValidationRetries: 2         # default: 2
  invalidClassFallback: unclassified  # default: unclassified
```

- `enforceValidClasses` — when `false`, an invalid class is logged and used
  as-is (legacy behavior); the loop runs exactly once.
- `maxValidationRetries` — number of re-prompts after the initial attempt
  (`0` = no retries).
- `invalidClassFallback` — class assigned when all retries are exhausted; the
  resulting `PageClassification.classification.metadata` then carries a
  `validation_error` string. The document is **not** failed.

> Holistic (`textbasedHolisticClassification`) does not use this loop yet; it
> still logs a warning and uses an unknown type as-is.

See `notebooks/misc/classification-valid-class-enforcement.ipynb` for a
deterministic, mock-driven walkthrough of all three scenarios (retry-then-valid,
retries-exhausted-fallback, and enforcement-disabled).

## Usage Example

### Using with Bedrock LLMs (Default)

```python
from idp_common import classification, get_config
from idp_common.models import Document

# Load configuration
config = get_config()

# Initialize classification service with Bedrock backend
service = classification.ClassificationService(
    region="us-east-1",
    config=config,
    backend="bedrock"  # This is the default
)

# Create or get a Document object
document = Document(
    id="doc-123",
    input_bucket="input-bucket",
    input_key="document.pdf",
    output_bucket="output-bucket",
    pages={
        "1": {
            "page_id": "1",
            "parsed_text_uri": "s3://bucket/document/pages/1/result.json",
            "image_uri": "s3://bucket/document/pages/1/image.jpg",
            "raw_text_uri": "s3://bucket/document/pages/1/rawText.json"
        }
    }
)

# Classify the document - updates the Document object directly
document = service.classify_document(document)

# Document now contains classification results
print(f"Document has {len(document.sections)} sections")
for section in document.sections:
    print(f"Section {section.section_id}: {section.classification}")
    print(f"Pages: {section.page_ids}")
```

### Using with SageMaker UDOP Models

```python
from idp_common import classification, get_config
from idp_common.models import Document

# Load configuration and add SageMaker endpoint
config = get_config()
config["sagemaker_endpoint_name"] = "udop-classification-endpoint"

# Initialize classification service with SageMaker backend
service = classification.ClassificationService(
    region="us-east-1",
    config=config,
    backend="sagemaker"
)

# Create or get a Document object
document = Document(
    id="doc-123",
    input_bucket="input-bucket",
    input_key="document.pdf",
    output_bucket="output-bucket",
    pages={
        "1": {
            "page_id": "1",
            "parsed_text_uri": "s3://bucket/document/pages/1/result.json",
            "image_uri": "s3://bucket/document/pages/1/image.jpg",
            "raw_text_uri": "s3://bucket/document/pages/1/rawText.json"
        }
    }
)

# Classify the document using SageMaker
document = service.classify_document(document)

# Access classification results from the Document
print(f"Document status: {document.status}")
for page_id, page in document.pages.items():
    print(f"Page {page_id} classified as: {page.classification}")
```

### Legacy Method (Still Supported)

```python
# Classify a single page
page_result = service.classify_page(
    page_id="1",
    text_uri="s3://bucket/document/pages/1/result.json",
    image_uri="s3://bucket/document/pages/1/image.jpg"
)

# Print the classification result
print(f"Page classified as: {page_result.classification.doc_type}")

# Classify multiple pages concurrently
pages = {
    "1": {"parsedTextUri": "s3://bucket/document/pages/1/result.json", "imageUri": "s3://bucket/document/pages/1/image.jpg"},
    "2": {"parsedTextUri": "s3://bucket/document/pages/2/result.json", "imageUri": "s3://bucket/document/pages/2/image.jpg"}
}

result = service.classify_pages(pages)

# Print the sections
for section in result.sections:
    print(f"Section {section.section_id}: {section.classification.doc_type}")
    for page in section.pages:
        print(f"  - Page {page.page_id}")

# Convert to dictionary for API response
api_response = result.to_dict()
```

## Configuration

The classification service uses the following configuration structure:

```json
{
  "model_id": "anthropic.claude-3-sonnet-20240229-v1:0", // Top-level model_id for Bedrock (optional)
  "sagemaker_endpoint_name": "udop-classification-endpoint", // SageMaker endpoint name (optional)
  "classes": [
    {
      "name": "invoice",
      "description": "An invoice that specifies an amount of money to be paid."
    },
    {
      "name": "financial_statement",
      "description": "Documents that summarize financial performance, such as income statements, balance sheets, or cash flow statements."
    }
  ],
  "classification": {
    "model": "anthropic.claude-3-sonnet-20240229-v1:0", // Specific model for classification (used if top-level model_id not specified)
    "temperature": 0,
    "top_k": 5,
    "sectionSplitting": "llm_determined", // "llm_determined" (default) | "page" | "disabled" - see "Section splitting strategies"
    "system_prompt": "You are a document classification expert...",
    "task_prompt": "Classify the following document into one of these types: {CLASS_NAMES_AND_DESCRIPTIONS}...\n\nDocument text:\n{DOCUMENT_TEXT}"
  }
}
```

## Integration with Lambda Functions

### Using with Bedrock Backend

```python
from idp_common import classification, get_config
from idp_common.models import Document, Status

def handler(event, context):
    # Extract document from event
    document = Document.from_dict(event["OCRResult"]["document"])
       
    # Initialize classification service
    config = get_config()
    service = classification.ClassificationService(config=config) 
    
    # Classify document
    document = service.classify_document(document)
    
    # Return response
    return {
        "document": document.to_dict()
    }
```

### Using with SageMaker Backend

```python
from idp_common import classification, get_config
from idp_common.models import Document, Status
import os

def handler(event, context):
    # Extract document from event
    document = Document.from_dict(event["OCRResult"]["document"])
    
    # Configure SageMaker endpoint
    config = get_config() or {}
    config["sagemaker_endpoint_name"] = os.environ["SAGEMAKER_ENDPOINT_NAME"]
    
    # Initialize classification service with SageMaker backend
    service = classification.ClassificationService(
        config=config,
        backend="sagemaker"
    )
    
    # Classify document using SageMaker
    document = service.classify_document(document)
    
    # Return response
    return {
        "document": document.to_dict()
    }
```

## Data Models

- `DocumentType`: Definition of a document type with name and description
- `DocumentClassification`: Classification result with document type and an optional confidence (`None` = not scored)
- `PageClassification`: Classification result for a single page
- `DocumentSection`: A section of consecutive pages with the same classification
- `ClassificationResult`: Overall result of a classification operation
- `Document`: Core document data model used throughout the IDP pipeline

## DynamoDB Caching for Resilient Classification

The classification service now supports optional DynamoDB caching to improve efficiency and resilience when processing documents with multiple pages. This feature addresses throttling scenarios where some pages succeed while others fail, avoiding the need to reclassify already successful pages on retry.

### How It Works

1. **Cache Check**: Before processing, the service checks for cached classification results for the document
2. **Selective Processing**: Only pages without cached results are classified
3. **Exception-Safe Caching**: Successful page results are cached even when other pages fail
4. **Retry Efficiency**: Subsequent retries only process previously failed pages

### Configuration

#### Via Constructor Parameter
```python
from idp_common import classification, get_config

config = get_config()
service = classification.ClassificationService(
    region="us-east-1",
    config=config,
    backend="bedrock",
    cache_table="classification-cache-table"  # Enable caching
)
```

#### Via Environment Variable
```bash
export CLASSIFICATION_CACHE_TABLE=classification-cache-table
```

```python
# Cache table will be automatically detected from environment
service = classification.ClassificationService(
    region="us-east-1",
    config=config,
    backend="bedrock"
)
```

### DynamoDB Table Schema

The cache uses the following DynamoDB table structure:

- **Primary Key (PK)**: `classcache#{document_id}#{workflow_execution_arn}`
- **Sort Key (SK)**: `none`
- **Attributes**:
  - `page_classifications` (String): JSON-encoded successful page results
  - `cached_at` (String): Unix timestamp of cache creation
  - `document_id` (String): Document identifier
  - `workflow_execution_arn` (String): Workflow execution ARN
  - `ExpiresAfter` (Number): TTL attribute for automatic cleanup (24 hours)

#### Example DynamoDB Item
```json
{
  "PK": "classcache#doc-123#arn:aws:states:us-east-1:123456789012:execution:MyWorkflow:abc-123",
  "SK": "none",
  "page_classifications": "{\"1\":{\"doc_type\":\"invoice\",\"confidence\":null,\"metadata\":{\"metering\":{...}},\"image_uri\":\"s3://...\",\"text_uri\":\"s3://...\",\"raw_text_uri\":\"s3://...\"},\"2\":{...}}",
  "cached_at": "1672531200",
  "document_id": "doc-123",
  "workflow_execution_arn": "arn:aws:states:us-east-1:123456789012:execution:MyWorkflow:abc-123",
  "ExpiresAfter": 1672617600
}
```

### Benefits

- **Cost Reduction**: Avoids redundant API calls to Bedrock/SageMaker for already-classified pages
- **Improved Resilience**: Handles partial failures gracefully during concurrent processing
- **Faster Retries**: Subsequent attempts only process failed pages, not the entire document
- **Automatic Cleanup**: TTL ensures cache entries don't accumulate indefinitely
- **Thread Safety**: Safe for concurrent page processing within the same document

### Example: Resilient Processing Flow

```python
from idp_common import classification, get_config
from idp_common.models import Document

config = get_config()
service = classification.ClassificationService(
    region="us-east-1",
    config=config,
    backend="bedrock",
    cache_table="classification-cache-table"
)

# Create document with 5 pages
document = Document(
    id="doc-123",
    workflow_execution_arn="arn:aws:states:us-east-1:123456789012:execution:MyWorkflow:abc-123",
    pages={
        "1": {...},
        "2": {...},
        "3": {...},
        "4": {...},
        "5": {...}
    }
)

try:
    # First attempt: pages 1,2,4 succeed, pages 3,5 fail due to throttling
    document = service.classify_document(document)
except Exception as e:
    # Pages 1,2,4 are cached automatically before exception is raised
    print(f"Classification failed: {e}")

try:
    # Retry: only pages 3,5 are processed (1,2,4 loaded from cache)
    document = service.classify_document(document)
    print("Document classified successfully on retry")
except Exception as e:
    print(f"Retry failed: {e}")
```

### Cache Lifecycle

1. **Creation**: Cache entries are created when `classify_document()` completes successfully or encounters exceptions
2. **Retrieval**: Cache is checked at the start of each `classify_document()` call
3. **Update**: Cache entries are updated with new successful results from each processing attempt
4. **Expiration**: Entries automatically expire after 24 hours via DynamoDB TTL

### Important Notes

- Caching only applies to the `classify_document()` method, not individual `classify_page()` calls
- Cache entries are scoped to specific document and workflow execution combinations
- Only successful page classifications (without errors in metadata) are cached
- The cache is transparent - existing code continues to work without modifications

## Backend Options

### Bedrock Backend

The Bedrock backend uses Amazon Bedrock LLMs to classify documents:

- Supports multiple model options (Claude, Titan, etc.)
- Works with both text and image content 
- Uses natural language understanding for classification
- Configurable system prompts and parameters

### SageMaker Backend

The SageMaker backend uses custom UDOP (Unified Document Processing) models:

- Uses vision-language models specifically trained for document understanding
- Requires both image and raw text URIs to be available
- Better performance for document-specific classification tasks
- Requires a deployed SageMaker endpoint

## Few Shot Example Feature

The classification service supports few shot learning through example-based prompting. This feature allows you to provide concrete examples of documents with their expected classifications and attribute extractions, significantly improving model accuracy and consistency.

### Overview

Few shot examples work by including reference documents with known classifications and expected attribute values in the prompts sent to the AI model. This helps the model understand the expected format and accuracy requirements for your specific use case.

### Configuration

Few shot examples are configured in the document class definitions within your configuration file:

```yaml
classes:
  - name: letter
    description: "A formal written correspondence..."
    attributes:
      - name: sender_name
        description: "The name of the person who wrote the letter..."
      # ... other attributes
    examples:
      - x-aws-idp-class-prompt: "This is an example of the class 'letter'"
        name: "Letter1"
        x-aws-idp-attributes-prompt: |
          expected attributes are:
              "sender_name": "Will E. Clark",
              "sender_address": "206 Maple Street P.O. Box 1056 Murray Kentucky 42071-1056",
              "recipient_name": "The Honorable Wendell H. Ford",
              # ... other expected attributes
        x-aws-idp-image-path: "config_library/unified/few_shot_example/example-images/letter1.jpg"
      - x-aws-idp-class-prompt: "This is an example of the class 'letter'" 
        name: "Letter2"
        x-aws-idp-attributes-prompt: |
          expected attributes are:
              "sender_name": "William H. W. Anderson",
              # ... other expected attributes
        x-aws-idp-image-path: "config_library/unified/few_shot_example/example-images/letter2.png"
```

### Configuration Parameters

Each few shot example includes:

- **x-aws-idp-class-prompt**: A description identifying this as an example of the document class
- **name**: A unique identifier for the example (for reference and debugging)
- **x-aws-idp-attributes-prompt**: The expected attribute extraction results in a structured format
- **x-aws-idp-image-path**: Path to example document image(s) - supports single files, local directories, or S3 prefixes

#### Image Path Options

The `x-aws-idp-image-path` field now supports multiple formats for maximum flexibility:

**Single Image File (Original functionality)**:
```yaml
x-aws-idp-image-path: "config_library/unified/few_shot_example/example-images/letter1.jpg"
```

**Local Directory with Multiple Images (New)**:
```yaml
x-aws-idp-image-path: "config_library/unified/few_shot_example/example-images/"
```

**S3 Prefix with Multiple Images (New)**:
```yaml
x-aws-idp-image-path: "s3://my-config-bucket/few-shot-examples/letter/"
```

**Direct S3 Image URI**:
```yaml
x-aws-idp-image-path: "s3://my-config-bucket/few-shot-examples/letter/example1.jpg"
```

When pointing to a directory or S3 prefix, the system automatically:
- Discovers all image files with supported extensions (`.jpg`, `.jpeg`, `.png`, `.gif`, `.bmp`, `.tiff`, `.tif`, `.webp`)
- Sorts them alphabetically by filename for consistent ordering
- Includes each image as a separate content item in the few-shot examples
- Gracefully handles individual image loading failures without breaking the entire process

#### Environment Variables for Path Resolution

The system uses these environment variables for resolving relative paths:

- **`CONFIGURATION_BUCKET`**: S3 bucket name for configuration files
  - Used when `x-aws-idp-image-path` doesn't start with `s3://`
  - The path is treated as a key within this bucket

- **`ROOT_DIR`**: Root directory for local file resolution
  - Used when `CONFIGURATION_BUCKET` is not set
  - The path is treated as relative to this directory

### Benefits

Using few shot examples provides several advantages:

1. **Improved Accuracy**: Models perform better when given concrete examples
2. **Consistent Formatting**: Examples help ensure consistent output structure
3. **Domain Adaptation**: Examples help models understand domain-specific terminology
4. **Reduced Hallucination**: Examples reduce the likelihood of made-up data
5. **Better Edge Case Handling**: Examples can demonstrate how to handle unusual cases

### Best Practices

When creating few shot examples:

#### 1. Quality over Quantity
- Use 1-3 high-quality examples per document class
- Ensure examples are representative of real-world documents
- Include diverse examples that cover different variations

#### 2. Clear and Complete Examples
```yaml
# Good example - specific and complete
x-aws-idp-attributes-prompt: |
  expected attributes are:
      "invoice_number": "INV-2024-001",
      "invoice_date": "01/15/2024",
      "vendor_name": "ACME Corp",
      "total_amount": "$1,250.00"

# Avoid incomplete examples
x-aws-idp-attributes-prompt: |
  expected attributes are:
      "invoice_number": "INV-2024-001"
      # Missing other important attributes
```

#### 3. Handle Null Values Appropriately
```yaml
x-aws-idp-attributes-prompt: |
  expected attributes are:
      "sender_name": "John Smith",
      "cc": null,  # Explicitly show when fields are not present
      "reference_number": null
```

#### 4. Use Realistic Examples
- Choose examples that represent typical documents in your use case
- Include examples with both common and edge case scenarios
- Ensure image quality is good and text is clearly readable

### Usage with Classification Service

The few shot examples are automatically integrated when using the classification service:

```python
from idp_common import classification, get_config
from idp_common.models import Document

# Load configuration with few shot examples
config = get_config()

# Initialize service - few shot examples are automatically used
service = classification.ClassificationService(
    region="us-east-1", 
    config=config
)

# Examples are automatically included in prompts during classification
document = service.classify_document(document)
```

The service automatically:
1. Loads few shot examples from the configuration
2. Includes them in classification prompts using the `{FEW_SHOT_EXAMPLES}` placeholder
3. Formats examples appropriately for both classification and extraction tasks

The shipped default `classification.task_prompt` in
`config/system_defaults/base-classification.yaml` **already contains
`{FEW_SHOT_EXAMPLES}`**, positioned after `<document-types>` and before
`<<CACHEPOINT>>` (examples are static per config, so they belong in the cacheable
prefix — which matters here because classification runs per page). Defining
`x-aws-idp-examples` on a class is therefore enough; a config that supplies its own
`task_prompt` must include the placeholder itself, exactly once.

Classification uses each example's `x-aws-idp-class-prompt` (legacy alias:
`classPrompt`) plus `x-aws-idp-image-path`; examples whose class-prompt is empty are
skipped, and with no examples at all the placeholder contributes nothing. Unlike
extraction, classification includes examples from **all** classes, not just one.

### Example Configuration Structure

Here's a complete example showing how few shot examples integrate with document class definitions:

```yaml
classes:
  - name: email
    description: "A digital message with email headers..."
    attributes:
      - name: from_address
        description: "The email address of the sender..."
      - name: to_address  
        description: "The email address of the primary recipient..."
      - name: subject
        description: "The topic of the email..."
      - name: date_sent
        description: "The date and time when the email was sent..."
    examples:
      - x-aws-idp-class-prompt: "This is an example of the class 'email'"
        name: "Email1"
        x-aws-idp-attributes-prompt: |
          expected attributes are: 
             "from_address": "john.doe@company.com",
             "to_address": "jane.smith@client.com", 
             "subject": "FW: Meeting Notes 4/20",
             "date_sent": "04/18/2024"
        x-aws-idp-image-path: "config_library/unified/few_shot_example/example-images/email1.jpg"

classification:
  task_prompt: |
    Classify this document into exactly one of these categories:
    
    {CLASS_NAMES_AND_DESCRIPTIONS}
    
    <few_shot_examples>
    {FEW_SHOT_EXAMPLES}
    </few_shot_examples>
    
    <document_ocr_data>
    {DOCUMENT_TEXT}
    </document_ocr_data>
```

### Optional `{CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS}` placeholder

In addition to the standard `{CLASS_NAMES_AND_DESCRIPTIONS}` placeholder,
classification prompts support an opt-in
`{CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS}` placeholder that expands
to each class's name, description, **and** the schema attribute names
(extraction fields) declared for that class. This gives the classifier
extra disambiguation signal in domains where multiple classes share
similar names but have very different extraction schemas (e.g.
`appraisal_report` vs `inspection_report`).

- **Page-level** (`multimodalPageLevelClassification`) prompts get an
  XML-tagged listing.
- **Holistic** (`textbasedHolisticClassification`) prompts get a
  three-column markdown table (`type | description | attributes`).
- The placeholder is fully **opt-in** — token usage and cost are
  unchanged for users who don't reference it. The library only
  substitutes placeholders that actually appear in the template.
- Per-class attribute counts are soft-capped at
  `ClassificationService.MAX_ATTRIBUTES_PER_CLASS` (default 50) to
  prevent pathologically large schemas from bloating prompts.
- Groups and list-item shapes declared as a local `$ref` into the class's
  `$defs` (what the UI's schema editor emits) are dereferenced via
  `idp_common.config.schema_utils.deref_schema` before their `type` is
  read, so they contribute their child names just like an inline group.
  Unresolvable refs degrade to the property name; a recursive definition is
  walked until it re-enters itself, and the re-entered member becomes a leaf.
- The walk itself stops at `_MAX_WALK_NAMES` (10 x the soft cap above).
  `MAX_ATTRIBUTES_PER_CLASS` truncates the walk's *result*, so it cannot bound
  the walk — and because a `$defs` definition can be re-entered on every
  sibling branch, dereferencing lets a ~2 KB schema expand to hundreds of
  thousands of names. Hitting the ceiling logs a warning.
- `PromptPreview.tsx` in the Web UI carries a deliberate port of this walk
  (`getAttributeNamesForClass` + `derefSchema`) so the prompt preview matches
  what the backend builds. Change both together; the TS tests mirror the
  Python expectations case for case.

Example:

```yaml
classification:
  task_prompt: |
    Classify this page using the schema attribute names as a
    disambiguation signal:

    {CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS}

    Document text:
    {DOCUMENT_TEXT}

    Respond with JSON: {"class": "...", "document_boundary": "start|continue"}
```

See [`docs/classification.md`](../../../../docs/classification.md) for
schema-walking rules, rendered output examples, and recommended use
cases. Tracked in GitHub issue #262.

### Troubleshooting

Common issues and solutions:

1. **Images Not Found**: Ensure image paths are correct and files exist
2. **Inconsistent Results**: Review example quality and ensure they're representative
3. **Poor Performance**: Consider adding more diverse examples or improving example quality
4. **Format Errors**: Ensure x-aws-idp-attributes-prompt follows exact JSON-like format expected by your prompts

## Future Enhancements

- ✅ Support for SageMaker UDOP models
- ✅ Direct integration with Document data model
- ✅ Improved error handling and retry mechanisms
- ✅ Few shot example support for improved accuracy
- 🔲 Better confidence score estimation
- 🔲 More advanced document structure analysis
- 🔲 Support for additional classification backends (custom models)
- 🔲 Multi-model classification for improved accuracy
- 🔲 Dynamic few shot example selection based on document similarity
