Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# IDP Common Core Data Models

This document describes the core data models for the IDP processing pipeline. For a high-level overview of the entire package, see the [main README](../README.md).

## 📑 Module Structure

The IDP Common library provides these main modules:

- **Models**: Core document representation (this document)
- **[Bedrock](bedrock/README.md)**: Utilities for working with Amazon Bedrock LLMs
- **[Classification](classification/README.md)**: Document classification services
- **[Extraction](extraction/README.md)**: Field extraction services
- **[Evaluation](evaluation/README.md)**: Result evaluation tools
- **[Rule Validation](rule_validation/README.md)**: Business rule validation and compliance checking
- **[OCR](ocr/README.md)**: Text extraction using AWS Textract
- **[Summarization](summarization/README.md)**: Document summarization services
- **[BDA](bda/README.md)**: Bedrock Data Automation integration
- **[Document Service Factory](docs_service_README.md)**: The `create_document_service()` entry point Lambdas use to record document state (always DynamoDB-backed)
- **Document Failure** (`document_failure.py`): recording *why* a document failed before the handler re-raises — see [Recording a document-level failure](#-recording-a-document-level-failure) below
- **[Reporting](reporting/README.md)**: Analytics data storage
- **[Assessment](assessment/README.md)**: Confidence scoring and bounding boxes
- **[Discovery](discovery/README.md)**: Document class and schema discovery
- **[Schema](schema/README.md)**: Dynamic Pydantic model generation from JSON Schema
- **[Agents](agents/README.md)**: Conversational AI agent framework
- **[Model Finetuning](model_finetuning/README.md)**: Nova fine-tuning utilities
- **[DynamoDB](dynamodb/README.md)**: Document tracking and HITL state management
- **[Image](image/README.md)**: Image resizing and format conversion
- **[S3](s3/README.md)**: S3 read/write utilities
- **[Utils](utils/README.md)**: Common utility functions
- **[Metrics](metrics/README.md)**: Performance and token tracking
- **[Config](config/README.md)**: Configuration loading, merging, validation, and typed models
- **[Hooks](hooks/README.md)**: Helpers for authoring pipeline-hook Lambdas (load / mutate / return a Document)
- **[Monitoring](monitoring/README.md)**: Shared monitoring foundation (logs, X-Ray, Step Functions, stack discovery)
- **Step Functions history** (`stepfunctions_history.py`): the one rule for reading an execution history and naming the state that failed — see [Naming the state that failed](#-naming-the-state-that-failed) below

## 🗃️ Key Classes

### Document

The `Document` class is the central data structure for the entire IDP pipeline with automatic compression support for large documents:

```python
@dataclass
class Document:
    """
    Core document type that is passed through the processing pipeline.
    Each processing step enriches this object.
    
    The Document class provides comprehensive support for handling large documents
    in Step Functions workflows through automatic compression and decompression.
    """
    # Core identifiers
    id: Optional[str] = None            # Generated document ID
    input_bucket: Optional[str] = None  # S3 bucket containing the input document
    input_key: Optional[str] = None     # S3 key of the input document
    output_bucket: Optional[str] = None # S3 bucket for processing outputs
    
    # Processing state and timing
    status: Status = Status.QUEUED
    initial_event_time: Optional[str] = None
    queued_time: Optional[str] = None
    start_time: Optional[str] = None
    completion_time: Optional[str] = None
    workflow_execution_arn: Optional[str] = None
    
    # Document content details
    num_pages: int = 0
    pages: Dict[str, Page] = field(default_factory=dict)
    sections: List[Section] = field(default_factory=list)
    summary_report_uri: Optional[str] = None
    
    # Processing metadata
    metering: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    trace_id: Optional[str] = None
    config_version: Optional[str] = None
    evaluation_status: Optional[str] = None
    evaluation_report_uri: Optional[str] = None
    evaluation_results_uri: Optional[str] = None
    rule_validation_result: Optional[RuleValidationResult] = None
    evaluation_result: Any = None
    summarization_result: Any = None
    errors: List[str] = field(default_factory=list)
    
    # HITL metadata
    hitl_metadata: List[HitlMetadata] = field(default_factory=list)
    hitl_status: Optional[str] = None
    hitl_triggered: bool = False
    hitl_sections_pending: List[str] = field(default_factory=list)
    hitl_sections_completed: List[str] = field(default_factory=list)
    
    # Confidence alerts
    confidence_alert_count: int = 0
```

### Page

The `Page` class represents individual pages within a document:

```python
@dataclass
class Page:
    """Represents a single page in a document."""
    page_id: str
    image_uri: Optional[str] = None
    raw_text_uri: Optional[str] = None
    parsed_text_uri: Optional[str] = None
    text_confidence_uri: Optional[str] = None
    classification: Optional[str] = None
    confidence: float = 0.0
    tables: List[Dict[str, Any]] = field(default_factory=list)
    forms: Dict[str, str] = field(default_factory=dict)
```

**Key URIs:**
- `image_uri`: S3 URI to the page image (JPG format)
- `raw_text_uri`: S3 URI to the raw Textract response (full JSON with all metadata)
- `parsed_text_uri`: S3 URI to the parsed text content (markdown format)
- `text_confidence_uri`: S3 URI to condensed text confidence data (optimized for assessment prompts)

### Section

The `Section` class represents a logical section of the document (typically with a consistent document class):

```python
@dataclass
class Section:
    """Represents a section of pages with the same classification."""
    section_id: str
    classification: str
    confidence: float = 1.0
    page_ids: List[str] = field(default_factory=list)
    extraction_result_uri: Optional[str] = None
    attributes: Optional[Dict[str, Any]] = None
    confidence_threshold_alerts: List[Dict[str, Any]] = field(default_factory=list)
```

### Status

The document processing status is represented by the `Status` enum:

```python
class Status(Enum):
    """Document processing status."""
    QUEUED = "QUEUED"                                       # Initial state
    RUNNING = "RUNNING"                                     # Workflow started
    PREPROCESSING = "PREPROCESSING"                         # Preprocessing hook running (e.g. PII redaction)
    OCR = "OCR"                                             # OCR processing
    CLASSIFYING = "CLASSIFYING"                             # Document classification
    EXTRACTING = "EXTRACTING"                               # Information extraction
    ASSESSING = "ASSESSING"                                 # Confidence assessment
    POSTPROCESSING = "POSTPROCESSING"                       # Post-processing
    HITL_IN_PROGRESS = "HITL_IN_PROGRESS"                   # Human review in progress
    SUMMARIZING = "SUMMARIZING"                             # Document summarization
    RULE_VALIDATION = "RULE_VALIDATION"                     # Rule validation
    RULE_VALIDATION_ORCHESTRATOR = "RULE_VALIDATION_ORCHESTRATOR"  # Rule consolidation
    EVALUATING = "EVALUATING"                               # Evaluation
    COMPLETED = "COMPLETED"                                 # All processing completed
    FAILED = "FAILED"                                       # Processing failed
    ABORTED = "ABORTED"                                     # User cancelled
    REDACTED_SUPERSEDED = "REDACTED_SUPERSEDED"             # Original superseded by its redacted copy
```

## 📦 Document Compression for Large Documents

The Document class includes automatic compression support to handle large documents that exceed Step Functions payload limits (256KB). This is essential for processing multi-page documents with extensive content.

### Compression Methods

```python
# Automatic compression when document exceeds threshold
compressed_data = document.compress(working_bucket, "processing_step")
# Returns: {"document_id": "...", "s3_uri": "...", "section_ids": [...], "compressed": True}

# Restore full document from compressed data
restored_document = Document.decompress(working_bucket, compressed_data)

# Handle either compressed or regular document data
document = Document.from_compressed_or_dict(data, working_bucket)
```

### Lambda Function Integration Utilities

```python
# Handle input - automatically detects and decompresses if needed
document = Document.load_document(
    event_data=event["document"], 
    working_bucket=working_bucket, 
    logger=logger
)

# Prepare output - automatically compresses if document is large
response_data = document.serialize_document(
    working_bucket=working_bucket, 
    step_name="classification", 
    logger=logger,
    size_threshold_kb=200  # Optional: custom threshold
)
```

### Key Compression Features

- **Automatic Detection**: Utility methods automatically detect compressed vs uncompressed documents
- **Size Threshold**: Configurable compression threshold (default 0KB - always compress)
- **Section Preservation**: Section IDs are preserved in compressed payloads for Step Functions Map operations
- **Transparent Handling**: Lambda functions work seamlessly with both compressed and uncompressed documents
- **S3 Storage**: Compressed documents are stored in `s3://working-bucket/compressed_documents/{document_id}/`

## 🔄 Common Operations

### Document Creation

```python
# Create an empty document
document = Document(
    id="doc-123",
    input_bucket="my-input-bucket",
    input_key="documents/sample.pdf",
    output_bucket="my-output-bucket"
)

# Create from an S3 event
document = Document.from_s3_event(s3_event, output_bucket="my-output-bucket")

# Create from a dictionary
document = Document.from_dict(document_dict)

# Create from a JSON string
document = Document.from_json(document_json_string)

# Create from baseline files in S3
document = Document.from_s3(bucket="baseline-bucket", input_key="documents/sample.pdf")
```

### Document Serialization

```python
# Convert to dictionary
document_dict = document.to_dict()

# Convert to JSON
document_json = document.to_json()
```

## 📄 Working with Sections and Pages

The document model makes it easy to work with sections and pages:

```python
# Get a specific page
page = document.pages["1"]

# Get all pages in a section
section = document.sections[0]
pages = [document.pages[page_id] for page_id in section.page_ids]

# Add a new section
document.sections.append(Section(
    section_id="new-section",
    classification="invoice",
    page_ids=["1", "2", "3"]
))

# Add a new page
document.pages["new-page"] = Page(
    page_id="new-page",
    image_uri="s3://bucket/image.jpg",
    classification="form"
)
```

## 🛠️ Building a Document from Scratch Example

```python
from idp_common.models import Document, Page, Section, Status

# Create an empty document
document = Document(
    id="invoice-123",
    input_bucket="input-bucket",
    input_key="invoices/invoice-123.pdf",
    output_bucket="output-bucket",
    status=Status.RUNNING
)

# Add pages
document.pages["1"] = Page(
    page_id="1",
    image_uri="s3://output-bucket/invoices/invoice-123.pdf/pages/1/image.jpg",
    raw_text_uri="s3://output-bucket/invoices/invoice-123.pdf/pages/1/rawText.json",
    parsed_text_uri="s3://output-bucket/invoices/invoice-123.pdf/pages/1/result.json",
    classification="invoice",
    confidence=0.98
)

document.pages["2"] = Page(
    page_id="2",
    image_uri="s3://output-bucket/invoices/invoice-123.pdf/pages/2/image.jpg",
    raw_text_uri="s3://output-bucket/invoices/invoice-123.pdf/pages/2/rawText.json",
    parsed_text_uri="s3://output-bucket/invoices/invoice-123.pdf/pages/2/result.json",
    classification="invoice",
    confidence=0.97
)

# Update number of pages
document.num_pages = len(document.pages)

# Add a section
document.sections.append(Section(
    section_id="1",
    classification="invoice",
    confidence=0.98,
    page_ids=["1", "2"],
    extraction_result_uri="s3://output-bucket/invoices/invoice-123.pdf/sections/1/result.json"
))
```

## 📊 Loading a Document for Evaluation Example

```python
from idp_common.models import Document

# Load actual document from processing results
actual_document = Document.from_dict(processed_result["document"])

# Load expected document from baseline files
expected_document = Document.from_s3(
    bucket="baseline-bucket",
    input_key=actual_document.input_key
)

# Now both documents can be compared for evaluation
```

## 🔗 Integration with Services

The document model integrates with all IDP services:

```python
from idp_common import ocr, classification, extraction, evaluation, rule_validation
from idp_common.docs_service import create_document_service

# OCR Processing
ocr_service = ocr.OcrService()
document = ocr_service.process_document(document)

# Document Classification
classification_service = classification.ClassificationService(config=config)
document = classification_service.classify_document(document)

# Field Extraction
extraction_service = extraction.ExtractionService(config=config)
document = extraction_service.process_document_section(document, section_id="1")

# Rule Validation
rule_validation_service = rule_validation.RuleValidationService(config=config)
document = rule_validation_service.validate_document(document)

# Rule Validation Orchestration
orchestrator = rule_validation.RuleValidationOrchestratorService(config=config)
document = orchestrator.consolidate_and_save(document, config=config, multiple_sections=True)

# Document Evaluation
evaluation_service = evaluation.EvaluationService(config=config)
document = evaluation_service.evaluate_document(document, expected_document)

# Document Class Discovery (generates JSON Schema for new document types)
from idp_common.discovery.classes_discovery import ClassesDiscovery
discovery = ClassesDiscovery(input_bucket="bucket", input_prefix="doc.pdf", region="us-west-2")
# With local file bytes (no S3 read):
result = discovery.discovery_classes_with_document(
    input_bucket="local", input_prefix="doc.pdf",
    file_bytes=open("doc.pdf", "rb").read(), save_to_config=False
)
schema = result["schema"]  # JSON Schema dict

# Record document state in the TrackingTable (DynamoDB)
document_service = create_document_service()
document = document_service.update_document(document)
```

## 🧯 Recording a document-level failure

`document_failure.py` is what a Lambda handler calls in the `except` block it is
about to re-raise from, so the document's own record carries the explanation. It
exists because a raise is invisible to a reader: none of the pipeline's task states
has a `Catch`, and `workflow_tracker` writes only a bare `Document` of status plus
completion time for a FAILED execution, so the pre-raise write is the only
opportunity there is.

```python
from idp_common.document_failure import (
    RULE_VALIDATION_FAILED_CODE, RULE_VALIDATION_FAILED_MESSAGE,
    RULE_VALIDATION_STAGE, SectionDiagnosis,
    persist_failed_section, persist_failed_document, summarize_errors,
)

try:
    document = service.do_work(document)
except Exception as error:
    persist_failed_section(            # one section, atomic — use inside a Map
        document_service=document_service,
        document=document,
        error=error,
        diagnosis=SectionDiagnosis(
            section_id=section_id,
            stage=RULE_VALIDATION_STAGE,
            code=RULE_VALIDATION_FAILED_CODE,
            message=RULE_VALIDATION_FAILED_MESSAGE,        # fixed template
            root_cause=summarize_errors(document.errors,   # variable text
                                        fallback=f"{type(error).__name__}: {error}"),
        ),
        section_index=section_index,
    )
    raise                              # unchanged: same type, message, traceback
```

Pick the writer by what the handler owns. `persist_failed_section` issues
`SET Sections[i] = :section`, which is what a handler running inside a `Map` needs
— concurrent iterations do not read-modify-write over each other.
`persist_failed_document` issues one whole-document `update_document`, for a handler
that already owns that write and holds every section.

### The rules it holds, and why they are here rather than at each call site

1. **The original exception propagates unchanged.** Every failure inside the persist
   is logged and swallowed, and neither function ever raises. A DynamoDB write that
   fails while trying to make an exception more visible must not surface in its
   place, or the Step Functions cause reports an unavailable table instead of the
   actual failure.
2. **Only an issue with the same `code` is replaced.** A retried document does not
   collect one issue per attempt, and a diagnosis another stage wrote — the thing a
   reader most needs alongside this one — is not deleted.
3. **A transient failure records nothing**, because the state machine is about to
   retry it and a marked section would show red for the length of the ladder (eight
   attempts at 2.5x backoff from ten seconds) and then clear. `failure_is_transient`
   is the same predicate the extraction and assessment handlers use. Of the three
   call sites this is load-bearing at exactly one — the rule-validation orchestrator,
   which re-raises the caught exception unchanged; the other two raise an exception
   they synthesise from a status check, which carries no transient verdict. The
   residual is that an exhausted ladder leaves the sections unmarked.
4. **Variable-length text goes in `root_cause`.** `ProcessingIssue.__post_init__`
   bounds that field (and every string leaf of `details`) because they share one
   DynamoDB item with a 400 KB ceiling. `message` is written to DynamoDB and is
   **not** bounded, so every `*_MESSAGE` constant here is a fixed template with no
   interpolation — a test asserts that.

### Why the diagnosis goes on a section, never on the document

The obvious alternative is to persist `document.errors`, which is what these stages
actually write. It is the wrong answer twice: `_document_to_update_expressions` has
never persisted `errors`, and `errors` is the scattered free-text signal
`ProcessingIssue` was introduced to replace.

A new document-level `ProcessingIssues` attribute is also declined, for the reason
already recorded where classification faced the same choice
(`ClassificationService._record_unclassified_page_issues`): `ProcessingIssues` is
a **Section** field in the API schema, so a document-level issue bumps
`ProcessingIssueCount` — which the document list does read — and then has no text to
show behind the badge. Giving it text means a new DynamoDB attribute, a resolver
shaping it, a schema type and a UI surface.

So each call site names the section or sections the failure belongs to, and travels
the path that is already persisted and already rendered. Where a diagnosis is
genuinely document-scope and no section can be named, it is left in the exception
and the log rather than attributed to a section by guess — `processresults_function`
does exactly that with `document.errors`, and a test pins it.

⚠️ **Which write you pick decides whether the document list's badge moves.**
`ProcessingIssueCount` is written by `update_document` and **not** by
`update_document_section`, so a failure recorded through `persist_failed_section`
shows on the Sections panel but can leave the list badge at its previous value.
Neither list resolver recovers it — the range resolver returns the stored value, and
the counter is absent from the fast GSI's INCLUDE projection, which also returns no
`Sections` to derive from. This is not an oversight to patch at that writer: it does
not read the item, the per-section handler has already narrowed `document.sections`
to its own section (so a local count would be 1 and would clobber a larger correct
one), and ten sections are being written concurrently. Correcting it needs an atomic
increment, which is not idempotent across an eight-attempt retry ladder, and it would
have to cover `extraction_failed` too.

### Adding a new failure code

The codes meaning "the stage raised" are declared in `FAILURE_CODES` here and in
`EXTRACTION_FAILED_CODE` in `extraction/failure.py`. The UI renders those as
**Failed** and every other error-severity code as **Incomplete**, from a hand-written
literal in `src/ui/src/components/common/processing-issues-utils.ts`. That is a
different language, so nothing about adding a code here would make it appear there —
`scripts/tests/test_failure_code_ui_parity.py` fails when the two disagree in either
direction. Add the code to the `ProcessingIssue` docstring's inventory too.

## 🐢 Lazy submodule loading, and where `mock.patch` goes wrong

`idp_common/__init__.py` loads every submodule through `__getattr__`, so `import
idp_common` costs the standard library and nothing else. That is what lets a Lambda
install `idp_common[core]` without dragging in the `[all]` dependency set — nothing
imports Strands, pypdfium2 or the Textract parser until something asks for it.
`tests/unit/test_lazy_submodule_loading.py` measures that in a fresh interpreter, which
is the only place it can be measured: by the time a test suite is running, half the
library is imported.

The loader defers to `importlib`, which means to `sys.modules`. It deliberately keeps no
cache of its own: a second cache beside `sys.modules` can hold a **different object for
the same name**, and `unittest.mock.patch` resolves its target through `sys.modules`, so
a patch applied to one copy is invisible to code holding the other (#1159).

⚠️ **Removing that cache does not make patching safe, and the difference is worth
understanding before writing a test.** The duplication that prompted #1159 is created
outside this package: `coverage` imports each `--cov=<module>` target inside a
`sys_modules_saved()` block and then deletes every `sys.modules` entry that import
added, while the module objects survive as attributes of their parent packages. Any
later import of such a name re-executes the file and yields a second object. Nothing in
`__init__.py` can prevent that.

**So patch at the point of use, and check how the consumer reached the name** — the two
cases need different targets and the wrong one fails silently, as a mock that records
zero calls:

| How the consumer imports it | Patch target |
|---|---|
| Module-level `from idp_common import s3`, then `s3.write_content(...)` | `idp_common.<consumer module>.s3.write_content` — the consumer's own captured object |
| Function-local `from idp_common.image import f` inside the method | `idp_common.image.f` — the name is resolved from `sys.modules` at call time |

## 🧭 Naming the state that failed

`stepfunctions_history.py` answers one question about a Step Functions execution
history — which state the terminal failure is attributable to — and it exists because
two callers answered it separately and both got it wrong the same way.

```python
from idp_common.stepfunctions_history import failing_state, failing_state_is_resolvable

state = failing_state(events)          # events in either direction; None if unknowable
```

**Why "the last state entered before the failure" is the wrong answer.** A `Catch` that
routes to a `Fail` state enters that handler *before* the terminal `ExecutionFailed`
arrives, and `FailStateEntered` is a real `HistoryEventType` that matches a
`StateEntered` suffix like any other transition. So the history reads:

```
TaskStateEntered:  Extraction      <- the state that actually failed
TaskFailed
FailStateEntered:  <handler>
ExecutionFailed
```

Both the chronological spelling ("the last state entered") and the reverse-order one
("the first state entered we see") name the handler, and both read as obviously right.
The rule that works keeps the two sources apart: the **state** comes from the last
task-level failure, the **error text** from the terminal event. Nine of this workflow's
states route a caught failure to a `Fail` state, so this is the ordinary case, not an
edge one.

Two things to know before relying on the answer:

- It returns `None` rather than a placeholder when the window holds no state transition
  older than the failure. Callers render their own "unknown", because a confident wrong
  state name costs more than an admitted gap — it sends a reader to the wrong log group.
  `failing_state_is_resolvable(events, more_pages=...)` is the matching stop condition
  for a caller paging backwards from the failure, and it deliberately refuses to call an
  execution-level failure resolved while pages remain: the handler's own transition
  would otherwise satisfy it.
- Attribution infers causality from **adjacency**, so inside a concurrent `Map` — whose
  iterations share one history and therefore interleave — it can name a sibling
  iteration's state. Walking `previousEventId` is the exact fix and has not been made.
  Such a walk has to start from an **outcome** event: a `TaskStateEntered` precedes its
  own `TaskScheduled`, so walking back from a state transition reaches the *previous*
  state's events.

The module imports nothing outside the standard library, deliberately — the CodeBuild
deployment harness (`scripts/sdlc/codebuild_deployment.py`) is one of its two callers
and cannot afford `strands`, which the other one (the error-analyzer agent tool) pulls
in.

## 📝 Best Practices

1. **Always use the Document.load_document() method** to handle input data in Lambda functions
2. **Always use document.serialize_document()** to prepare output data
3. **Keep the Document model as the central data structure** across all processing steps
4. **Store large data in S3** and reference by URI in the Document model
5. **Use Section objects** to group related pages by document type
