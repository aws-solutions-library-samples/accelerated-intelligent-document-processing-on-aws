Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# IDP Extraction Module

This module provides functionality for extracting structured information from document sections using LLMs with support for few-shot example prompting to improve accuracy.

## Overview

The extraction module is designed to process document sections, extract key information based on configured attributes, and return structured results. It supports multimodal extraction using both text and images, and can leverage concrete examples to improve extraction accuracy and consistency.

## Components

- **ExtractionService**: Main service class for performing extractions with few-shot example support
- **Models**: Data classes for extraction results

## Usage

The ExtractionService uses a Document-based approach which simplifies integration with the entire IDP pipeline:

```python
from idp_common import get_config
from idp_common.extraction.service import ExtractionService
from idp_common.models import Document

# Initialize the service with configuration
config = get_config()
extraction_service = ExtractionService(config=config)

# Load your document
document = Document(...)  # Document with sections already classified

# Process a specific section in the document
updated_document = extraction_service.process_document_section(
    document=document,
    section_id="section-123"
)

# Access the extraction results URI from the section
section = next(s for s in updated_document.sections if s.section_id == "section-123")
result_uri = section.extraction_result_uri
print(f"Extraction results stored at: {result_uri}")

# To get the attributes, you would load them from the result URI
# For example:
# extracted_fields = s3.get_json_content(result_uri)
```

### Lambda Function Pattern

For AWS Lambda functions, we recommend using a focused document with only the relevant section:

```python
# Get document and section from event
full_document = Document.from_dict(event.get("document", {}))
section_id = event.get("section", {}).get("section_id", "")

# Find the section - should be present
section = next((s for s in full_document.sections if s.section_id == section_id), None)
if not section:
    raise ValueError(f"Section {section_id} not found in document")

# Filter document to only include this section and its pages
section_document = full_document
section_document.sections = [section]

# Keep only pages needed for this section
needed_pages = {}
for page_id in section.page_ids:
    if page_id in full_document.pages:
        needed_pages[page_id] = full_document.pages[page_id]
section_document.pages = needed_pages

# Process the focused document
extraction_service = ExtractionService(config=CONFIG)
processed_document = extraction_service.process_document_section(
    document=section_document,
    section_id=section_id
)
```

## Configuration

The extraction service uses the following configuration structure:

```json
{
  "extraction": {
    "model": "anthropic.claude-3-sonnet-20240229-v1:0",
    "temperature": 0.0,
    "top_k": 5,
    "system_prompt": "You are an expert at extracting information from documents...",
    "task_prompt": "Extract the following fields from this {DOCUMENT_CLASS} document: {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}\n\n{FEW_SHOT_EXAMPLES}\n\nDocument text:\n{DOCUMENT_TEXT}"
  },
  "classes": [
    {
      "name": "invoice",
      "description": "An invoice document",
      "attributes": [
        {
          "name": "invoice_number",
          "description": "The invoice number or ID"
        },
        {
          "name": "date",
          "description": "The invoice date"
        }
      ]
    }
  ]
}
```

## Few Shot Example Feature

The extraction service supports few-shot learning through example-based prompting. This feature allows you to provide concrete examples of documents with their expected attribute extractions, significantly improving model accuracy, consistency, and reducing hallucination.

### Overview

Few-shot examples work by including reference documents with known expected attribute values in the prompts sent to the AI model. Unlike classification which uses examples from all document classes, extraction uses examples only from the specific class being processed to provide targeted guidance for attribute extraction.

### Key Differences from Classification

- **Example Scope**: Extraction uses examples ONLY from the specific document class being processed (e.g., only "letter" examples when extracting from a "letter" document)
- **Prompt Field**: Uses `x-aws-idp-attributes-prompt` instead of `x-aws-idp-class-prompt` from examples
- **Purpose**: Shows expected attribute extraction format and values rather than distinguishing between document types

### Configuration

Few-shot examples are configured in the document class definitions within your configuration file:

```yaml
classes:
  - name: letter
    description: "A formal written correspondence..."
    attributes:
      - name: sender_name
        description: "The name of the person who wrote the letter..."
      - name: sender_address
        description: "The physical address of the sender..."
      - name: recipient_name
        description: "The name of the person receiving the letter..."
      # ... other attributes
    examples:
      - x-aws-idp-class-prompt: "This is an example of the class 'letter'"
        name: "Letter1"
        x-aws-idp-attributes-prompt: |
          expected attributes are:
              "sender_name": "Will E. Clark",
              "sender_address": "206 Maple Street P.O. Box 1056 Murray Kentucky 42071-1056",
              "recipient_name": "The Honorable Wendell H. Ford",
              "recipient_address": "United States Senate Washington, D. C. 20510",
              "date": "10/31/1995",
              "subject": null,
              "letter_type": "opposition letter",
              "signature": "Will E. Clark",
              "cc": null,
              "reference_number": "TNJB 0008497"
        x-aws-idp-image-path: "config_library/unified/few_shot_example/example-images/letter1.jpg"
      - x-aws-idp-class-prompt: "This is an example of the class 'letter'"
        name: "Letter2"
        x-aws-idp-attributes-prompt: |
          expected attributes are:
              "sender_name": "William H. W. Anderson",
              "sender_address": "P O. BOX 12046 CAMERON VILLAGE STATION RALEIGH N. c 27605",
              "recipient_name": "Mr. Addison Y. Yeaman",
              "recipient_address": "1600 West Hill Street Louisville, Kentucky 40201",
              "date": "10/14/1970",
              "subject": "Invitation to the Twelfth Annual Meeting of the TGIC",
              "letter_type": "Invitation",
              "signature": "Bill",
              "cc": null,
              "reference_number": null
        x-aws-idp-image-path: "config_library/unified/few_shot_example/example-images/letter2.png"
```

### Configuration Parameters

Each few-shot example includes:

- **x-aws-idp-class-prompt**: A description identifying this as an example of the document class (used for classification)
- **x-aws-idp-attributes-prompt**: The expected attribute extraction results showing the exact JSON format and values expected
- **name**: A unique identifier for the example (for reference and debugging)
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

### Task Prompt Integration

All three shipped extraction prompt variants in
`config/system_defaults/base-extraction.yaml` (`task_prompt`,
`task_prompt_extraction_with_confidence`,
`task_prompt_extraction_with_confidence_topk`) already include
`{FEW_SHOT_EXAMPLES}` ahead of `<<CACHEPOINT>>`, so a class that defines
`x-aws-idp-examples` gets them with no prompt edit; classes without examples
render the section empty (no content items added). A config that supplies its
**own** extraction `task_prompt` must include the placeholder itself — exactly
once, since `_build_prompt_content` splits on it and ignores it otherwise.

Example fields are read under either the `x-aws-idp-*` names or the legacy
camelCase names (`x-aws-idp-class-prompt` / `x-aws-idp-attributes-prompt` / `x-aws-idp-image-path`). Unreadable
example images log a warning and the example is sent as text-only rather than
failing the section.

To use few-shot examples, your task prompt must include the `{FEW_SHOT_EXAMPLES}` placeholder:

```yaml
extraction:
  task_prompt: |
    <background>
    You are an expert in business document analysis and information extraction.

    <task>
    Your task is to take the unstructured text provided and convert it into a
    well-organized table format using JSON. Identify the main entities,
    attributes, or categories mentioned in the attributes list below and use
    them as keys in the JSON object.

    Here are the attributes you should extract:
    <attributes>
    {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}
    </attributes>

    <few_shot_examples>
    {FEW_SHOT_EXAMPLES}
    </few_shot_examples>

    </task>
    </background>

    The document type is {DOCUMENT_CLASS}. Here is the document content:
    <document_ocr_data>
    {DOCUMENT_TEXT}
    </document_ocr_data>
```

### Benefits

Using few-shot examples provides several advantages for extraction:

1. **Improved Accuracy**: Models understand the expected extraction format and attribute relationships better
2. **Consistent Formatting**: Examples establish exact JSON structure and value formats expected
3. **Reduced Hallucination**: Examples reduce the likelihood of made-up attribute values
4. **Better Null Handling**: Examples show when attributes should be null vs. empty strings
5. **Domain-Specific Understanding**: Examples help models understand domain-specific terminology and formats

### Best Practices

When creating few-shot examples for extraction:

#### 1. Show Complete Attribute Sets

```yaml
# Good example - shows all attributes with realistic values
x-aws-idp-attributes-prompt: |
  expected attributes are:
      "invoice_number": "INV-2024-001",
      "invoice_date": "01/15/2024",
      "vendor_name": "ACME Corp",
      "customer_name": "Tech Solutions Inc",
      "total_amount": "$1,250.00",
      "due_date": "02/15/2024",
      "po_number": "PO-789456"

# Avoid incomplete examples
x-aws-idp-attributes-prompt: |
  expected attributes are:
      "invoice_number": "INV-2024-001"
      # Missing other important attributes
```

#### 2. Handle Null Values Explicitly

```yaml
x-aws-idp-attributes-prompt: |
  expected attributes are:
      "sender_name": "John Smith",
      "cc": null,  # Explicitly show when fields are not present
      "reference_number": null,
      "subject": "Meeting Request",
      "attachments": null
```

#### 3. Use Realistic and Diverse Examples

- Include examples with different formatting styles
- Show both common cases and edge cases
- Use realistic data that represents your actual documents
- Include examples with varying levels of completeness

#### 4. Maintain Consistent Format

```yaml
# Consistent JSON format across all examples
x-aws-idp-attributes-prompt: |
  expected attributes are:
      "field1": "value1",
      "field2": "value2",
      "field3": null

# Avoid inconsistent formatting
x-aws-idp-attributes-prompt: |
  field1: value1
  field2 = "value2"
  field3: (empty)
```

#### 5. Organize Multiple Images

When using directories or S3 prefixes with multiple images:

```yaml
# Good: Use descriptive, ordered filenames
x-aws-idp-image-path: "examples/letters/"
# Contents: 001_formal_letter.jpg, 002_informal_letter.png, 003_business_letter.jpg

# Good: Group related examples together
x-aws-idp-image-path: "s3://config-bucket/examples/invoices/"
# Contents: invoice_simple.jpg, invoice_complex.png, invoice_international.jpg
```

### Class-Specific Example Filtering

The extraction service automatically filters examples by document class:

```python
# When processing a "letter" document, only letter examples are used
# When processing an "invoice" document, only invoice examples are used

# This ensures extraction examples are relevant and targeted
document = extraction_service.process_document_section(
    document=letter_document,  # Classified as "letter"
    section_id="section-1"
)
# Only letter examples will be included in the prompt
```

### Usage with Extraction Service

The few-shot examples are automatically integrated when using the extraction service:

```python
from idp_common import get_config
from idp_common.extraction.service import ExtractionService
from idp_common.models import Document

# Load configuration with few-shot examples
config = get_config()

# Initialize service - few-shot examples are automatically used
service = ExtractionService(
    region="us-east-1",
    config=config
)

# Examples are automatically included in prompts during extraction
# Only examples matching the document's classification are used
document = service.process_document_section(document, section_id)
```

The service automatically:

1. Loads few-shot examples from the configuration
2. Filters examples to only include those from the document's classified type
3. Includes them in extraction prompts using the `{FEW_SHOT_EXAMPLES}` placeholder
4. Formats examples with both text and images for multimodal understanding

### Example Configuration Structure

Here's a complete example showing how few-shot examples integrate with document class definitions:

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
             "from_address": "Kelahan, Ben",
             "to_address": "TI New York: 'TI Minnesota",
             "cc_address": "Ashley Bratich (MSMAIL)",
             "bcc_address": null,
             "subject": "FW: Morning Team Notes 4/20",
             "date_sent": "04/18/1998",
             "attachments": null,
             "priority": null,
             "thread_id": null,
             "message_id": null
        x-aws-idp-image-path: "config_library/unified/few_shot_example/example-images/email1.jpg"

extraction:
  task_prompt: |
    <background>
    You are an expert in business document analysis and information extraction.

    <task>
    Your task is to take the unstructured text provided and convert it into a
    well-organized table format using JSON.

    Here are the attributes you should extract:
    <attributes>
    {ATTRIBUTE_NAMES_AND_DESCRIPTIONS}
    </attributes>

    <few_shot_examples>
    {FEW_SHOT_EXAMPLES}
    </few_shot_examples>

    </task>
    </background>

    The document type is {DOCUMENT_CLASS}. Here is the document content:
    <document_ocr_data>
    {DOCUMENT_TEXT}
    </document_ocr_data>
```

### Testing Few-Shot Examples

Use the provided test notebook to validate the few-shot functionality:

```python
# Test few-shot extraction examples
import sys
sys.path.append('../lib/idp_common_pkg')

from idp_common.extraction.service import ExtractionService
import yaml

# Load configuration with examples
with open('config_library/unified/few_shot_example/config.yaml', 'r') as f:
    config = yaml.safe_load(f)

# Initialize service
service = ExtractionService(config=config)

# Test building examples for specific class
examples = service._build_few_shot_examples_content('letter')
print(f"Found {len(examples)} example items for 'letter' class")

# Test complete content building
content = service._build_content_with_few_shot_examples(
    task_prompt_template=config['extraction']['task_prompt'],
    document_text="Sample letter text...",
    class_label="letter",
    attribute_descriptions="sender_name\t[The person who wrote the letter]"
)
print(f"Built content with {len(content)} items")
```

### Troubleshooting

Common issues and solutions:

1. **No Examples Loaded**:
   - Verify `{FEW_SHOT_EXAMPLES}` placeholder exists in task_prompt
   - Check that examples are defined for the document class being processed
   - Ensure example image paths are correct

2. **Images Not Found**:
   - Set `ROOT_DIR` environment variable for local development
   - Set `CONFIGURATION_BUCKET` for S3 deployment
   - Verify image files exist at specified paths

3. **Inconsistent Extraction Results**:
   - Review example quality and ensure they're representative
   - Check that `x-aws-idp-attributes-prompt` format matches expected output
   - Ensure examples cover the range of variations in your documents

4. **Poor Performance**:
   - Add more diverse examples for the document class
   - Improve example quality and accuracy
   - Ensure examples demonstrate proper null handling

## Coercion and validation (Simple mode)

Simple extraction used to do a raw `json.loads` and pass whatever came back
downstream, so a wrong *type* or a non-ISO date reached DynamoDB unchallenged.
Advanced (agentic) extraction has had full-schema validation and escalation for
some time; as of v0.7 the simple path gets the same guarantee, in two steps.

**1. Deterministic coercion (always, free).** Before validating, obvious
type/format mismatches are repaired without a model call: `"$1,234.00"` and
`"1.234,00"` into a `number` field, named-month and unambiguous numeric dates
into `format: date`, boolean-ish strings into `boolean`. Every change is recorded
under `metadata.coercion` and anything ambiguous (`01/02/2024`, a 2-digit year,
fractional-to-integer) is **refused** rather than guessed. Nothing is ever
rewritten without a record. See `idp_common.extraction.coercion`.

**2. Full JSON-Schema validation**, then `extraction.validation.fail_action`:

| `fail_action` | Behaviour | Extra inference? |
|---|---|---|
| `warn` (default) | Record the outcome in `metadata.validation` and raise a ProcessingIssue | **No — free** |
| `reject` | Same, plus `parsing_succeeded=False` so downstream/HITL treats the section as failed | **No — free** |
| `escalate` | Re-extract ONLY the failing top-level fields with `escalation_model`, merged back over the fields that already validated | **Yes** |

Validation is **on by default** precisely because the default action is free: it
turns an otherwise-silent schema violation into something visible at no cost.
`escalate` is the opt-in that spends money.

Escalation is deliberately **not** preceded by a same-model retry: re-asking the
model that just produced invalid output, with the same prompt, mostly buys
another invalid answer at full document cost. Only the failing fields are
re-extracted, and only those fields are merged back — an over-eager escalation
response cannot overwrite fields that already validated. A failed escalation
returns the original extraction unchanged; a broken repair must never be worse
than no repair.

## Forced tool use (Simple mode, `extraction.forced_tool`)

Coercion and validation act on a result that already exists. Forced tool use tries
to prevent one failure mode up front: instead of describing the schema in prose
and parsing whatever text comes back, the class schema is sent as a Converse
**tool** and `toolChoice` names it, so the reply arrives as structured tool input.
Simple mode only — the agentic path already sends a tool schema. Off by default.

`idp_common.extraction.forced_tool` owns the whole surface:

| Helper | Purpose |
|---|---|
| `should_force_tool(model_id, enabled, class_schema)` | `(force, skip_reason)`. Refuses routes that cannot carry a `toolConfig` (Lambda hook, GPT-5.x Responses API) and schemas with no properties. |
| `build_extraction_tool_config(class_schema)` | `(tool_config, name_map)` for the single `emit_extracted_fields` tool. |
| `forced_tool_choice()` | `{"tool": {"name": ...}}` — must NAME the tool; `any`/`auto` would not be forcing. |
| `restore_extracted_fields(tool_input, name_map)` | Undoes wire-safe property renaming. |

**Property-name sanitization.** Bedrock enforces `^[a-zA-Z0-9_.-]{1,64}$` on
**top-level** tool property names, which several shipped presets violate
(`"Invoice Number"`). `idp_common.bedrock.tool_schema` rewrites those names for the
request and `restore_extracted_fields` puts the authored names back, so the stored
result is byte-identical to the un-forced path. `BedrockClient` also *rejects* an
invalid `toolConfig` outright rather than letting the API return an opaque
validation error.

**A force is a request, not a guarantee.** A model can accept a `toolConfig` and
still answer in prose (`stopReason: end_turn`). That falls back to text parsing
unless `fallback_to_prompt` is off, in which case the section is a parse failure —
which exists only to measure the honored rate without fallback masking it.

**`metadata.forced_tool`** records `requested`, `honored`, `renamed_properties`,
and `skipped` (with a reason). This is load-bearing for measurement, not just
audit: without it a before/after comparison cannot distinguish "forcing had no
effect" from "forcing never ran", and both look identical in the output.

> **Module-level call sites.** The service reaches the response parsers as
> `bedrock.extract_tool_use_from_response(...)`. Those are bound methods
> re-exported at the bottom of `idp_common/bedrock/__init__.py` — a client method
> missing from that list raises `AttributeError` at runtime, not import time.
> `tests/unit/bedrock/test_module_level_api.py` sweeps the package for
> `bedrock.<name>(...)` calls and asserts each resolves; add the re-export line
> whenever you call a new client method that way.

## Multi-document sections (`instance_count`)

Classification splits sections on document *type*. When a packet concatenates
several records of the **same** type with no separator, there is no type change
to split on, so they land in one section — and extraction, whose class schema
describes one document, returns one record. See
[#565](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/565).

`Section.instance_count` reports how many documents of the class a section turned
out to hold: `0` = undetermined, `1` = normal, `> 1` = the section spans several.
The UI surfaces `> 1` so this is obvious at a glance. It is populated from
whichever of two sources applies.

### Recovered (automatic, no config)

If the model returns a **JSON array** for the single-object schema — which is
what it tends to do when it notices several records — every element is kept.
The first populates `inference_result` (so the output shape is identical to a
normal extraction and no downstream consumer changes), the rest are recorded in
`metadata.recovered_instances`, and the section gets an
`extraction_multi_instance_detected` **warning** because only the first is scored
and reviewed.

This replaced the previous behaviour, which stored the array under a `raw_array`
key that nothing read, failed the section, and reported a misleading
`extraction_sparse`. Note what it does *not* fix: if the model returns a single
object for a two-document section, only the first record exists in the response
and there is nothing to recover.

### Declared (`x-aws-idp-instance-array`)

A class whose schema is already modelled as a **packet of records** can name its
own instance axis:

```yaml
classes:
  - $id: patient_packet
    type: object
    x-aws-idp-instance-array: records   # each element is one document
    properties:
      records:
        type: array
        items:
          type: object
          properties:
            patient_name: { type: string }
            patient_dob:  { type: string, format: date }
```

`instance_count` becomes `len(inference_result["records"])`. Nothing else
changes — **no schema transform, no output-shape change, no downstream impact** —
because the pipeline is only being told which existing array means "one document
per element". This exists so configs that already solved multi-record packets by
hand keep working unchanged while still getting the count and the UI badge.

A declared count above 1 is *correct*, not a problem, so it raises **no** warning
— unlike the recovered case. The declaration is validated at config-validate
time (the property must exist and be an array of objects), because a typo would
otherwise fail silently by simply never producing a count.

### Suspected (`extraction.multi_instance_detection`, #753)

The common branch, and the one that used to be completely silent: the model
returns a **single object** for a several-document section, so records 2..N are
absent from the response and there is nothing to recover. `instance_count` landed
on `1` by construction, `ProcessingIssueCount` on `0`, and the document on
`COMPLETED`.

Detection asks the model, **in the same inference**, how many separate documents
of the class the pages contain. `extraction/instance_probe.py` adds one auxiliary
integer property (`IDPDocumentInstanceCount`) to a **copy** of the class schema —
`self._wire_class_schema`, used for the prompt text and, when forced tool use is
on, for the `toolSpec`. `self._class_schema` is untouched, so the off-schema
filter, the JSON-Schema validator, the generated Pydantic model and every
downstream stage still see exactly the declared fields.

`_read_instance_probe` pops the value **before** the off-schema filter, and pops
it unconditionally — even when detection was not requested for the section — so an
echoed field can never reach `inference_result`, be scored by assessment, become a
reporting column, or turn up in an evaluation diff. It tolerates `3`, `"3"`,
`3.0`, and the 1S-TopK candidate shape `{"G1": 3, "P1": 0.9}`.

`_resolve_instance_reporting` compares the answer with the number of records the
result actually holds. When the answer is larger:
`instance_source = "suspected"`, `instance_count` becomes the model's answer,
`metadata.instance_extracted_count` records what was really extracted (so the
audit trail cannot be misread as "3 records were extracted"), and
`_build_extraction_issues` raises `extraction_multi_instance_suspected`
(**warning**) naming both numbers. A `MultiInstanceSectionsSuspected` metric goes
to CloudWatch.

Not limited to unflagged classes: a class in Designate or Synthesize mode whose
array came back with fewer records than the model reports is under-extracting,
which is the same data loss.

Config: `extraction.multi_instance_detection.enabled`, default **false**.

Measured on two real labeled corpora via Test Studio, 80 paired runs (identical
documents per pair, only the toggle differing) — see
`docs/benchmarking/feature-multi-instance.md`:

* **The counting works.** On 40 bank-check images from the OmniAI OCR benchmark,
  scored against their committed baselines: 18 true positives, **0 false
  positives**, 0 false negatives, 22 correct silences. Precision and recall
  **1.000**, and the reported count was **exactly** right on all 18 (2 to 8 checks
  per image).
* **⚠ A warning is not by itself evidence of loss, and the same benchmark proves
  it.** Counting the extracted rows on those 18 documents afterwards: **0 checks
  missing**, in both arms. `BANK_CHECK`'s schema is a single `checks` array, so the
  class already models several checks per sheet. The warning fired because
  `instance_count` is **1** for a class that declares no instance axis — which is
  true whether the records are absent *or* sitting inside a declared array. The
  predicate `probe_count > instance_extracted_count` cannot distinguish the two.
  Correct and useful (the fix is `x-aws-idp-instance-array: checks`), but it is a
  configuration finding, not a data-loss finding. Say so wherever this warning is
  surfaced.
* **Tokens are negligible:** input +1.8%, output −0.5%.
* **On a corpus with nothing to find it is pure cost.** RealKIE-FCC-Verified:
  0.7678 → 0.7552 weighted score, worse on 14 of 40 documents and better on 1,
  sign test **p = 0.001**. The loss is diffuse (four attributes, one or two
  documents each), not a single failure mode.

Hence off by default, and turn it on per configuration profile where a section can
hold several documents of one class **and the class schema describes only one** —
that conjunction is what loses records. It is also worth running once as a pure
diagnostic on any corpus, then turning off: that is how the `BANK_CHECK` missing
instance axis above was found.

The question itself is `extraction.multi_instance_detection.question`, sent as the
auxiliary property's description and supporting `{DOCUMENT_CLASS}`. The shipped text
lives in `config/system_defaults/base-extraction.yaml` (so it is editable in
`Config#default` like every other prompt) *and* as `DEFAULT_INSTANCE_QUESTION` in
`instance_probe.py` (so an `IDPConfig` built without merging system defaults still
has it). A test asserts the two are byte-identical, and another pins the two
load-bearing clauses, because dropping either quietly degrades detection rather
than failing.

Simple extraction only (prompt and forced-tool paths) — Advanced (agentic)
extraction validates through a generated Pydantic model and shards by field, so an
auxiliary property would be dropped on some paths and duplicated on others.

## Synthesize mode (`x-aws-idp-multi-instance`, #715)

Detection makes the loss visible; this fixes it. A class flagged
`x-aws-idp-multi-instance: true` has its **effective** schema replaced by

```json
{"type": "object", "title": "<Class> Instances",
 "properties": {"instances": {"type": "array", "minItems": 1,
                              "items": { …the original class schema… }}},
 "required": ["instances"]}
```

so the author keeps writing the class as ONE record and the pipeline asks for a
list of them.

### It is a schema TRANSFORM, not an output envelope

This is the decision that makes the feature affordable. Because the wrapper is
declared *in* the class schema, `inference_result` stays a valid instance of its
own declared schema, and the prompt, the generated Pydantic model, the JSON-Schema
validator, `_filter_extracted_to_schema`, assessment's `attr_type == "list"`
branch, `resolve_array_item_thresholds` and Stickler's Hungarian row matching all
work **unchanged** — none of them special-case anything, they all just read "the
class schema".

Treating the wrapper as an out-of-band key instead breaks every one of them:
`_filter_extracted_to_schema` deletes `instances` and emits `{}`; assessment
collapses the whole section to one `{"confidence": 0.5}` leaf;
`_schema_field_mismatch_reason` blacklists the section so the escalation ladder
skips it permanently; evaluation builds a Stickler model with zero declared fields
and silently scores 0.0.

### The helper

`idp_common/schema/multi_instance.py` — pure (no boto3, no Strands, no service
imports), idempotent, never mutates its input:

```python
is_multi_instance(class_schema) -> bool     # tolerates "true" from a round-trip
is_wrapped(class_schema) -> bool
wrap_class_schema(class_schema) -> dict     # no-op (same object) when unflagged
unwrap_instances(inference_result) -> list[dict] | None
wrap_instances(records) -> dict
```

Assessment, evaluation, reporting and the analytics agent read the class schema
**from config**, not from the extraction output, so each derives the wrapper
itself. Applied at the single point each stage loads a schema:
`ExtractionService._get_class_schema`, `AssessmentService._get_class_schema`,
`SticklerConfigMapper.build_all_stickler_configs`. **Do not** persist the wrapped
schema as the source of truth — config is, and the helper is the one derivation.

### Two things that are easy to get wrong

1. **The wrapper also emits `x-aws-idp-instance-array: instances`.** The wrapper
   alone extracts every record but leaves `Section.instance_count` at 1, so the UI
   badge, the `extraction_multi_instance_detected` warning and the
   `MultiInstanceSections` / `MultiInstanceRecordsRecovered` metrics all still
   report a single instance. Everything the visibility work shipped goes dark on
   exactly the schemas this creates unless both are emitted.
2. **The inner `items` gets a DISTINCT title** (`"<Class> Record"`).
   `_find_model_in_module` selects the generated Pydantic model by title/label
   priority, so items keeping the class title select the **inner** model and
   silently validate ONE instance instead of the list. Backed by a structural
   guard in `pydantic_generator._ensure_model_covers_schema`: the selected model
   must declare the schema's top-level properties; a better-covering model is
   preferred with a warning, and only a total absence is fatal.

`$defs` are hoisted to the wrapper, because `{"$ref": "#/$defs/X"}` resolves from
the document root — moving them down with the properties would dangle every ref.
Class-level metadata (identity, classification hints, per-class model/prompt
overrides, exclusion flags, few-shot examples) stays on the wrapper; only
shape-describing keywords move into `items`. The class-level
`x-aws-idp-evaluation-match-threshold` becomes the `instances` array's row-match
threshold, which is what makes record alignment order-insensitive.

### Rescuing a response that ignored the wrapper

`_adapt_to_instances_wrapper` re-expresses two shapes a model can still return,
both of which would otherwise be deleted entirely by the off-schema filter (whose
only allowed top-level key is now `instances`) and emitted as `{}`:

- a **bare top-level array** of records → adopted as the instance list;
- a **single flat record** (the pre-flag shape) → adopted as one instance, but
  only when its keys actually overlap the record's properties, so an
  `error`/`raw_output` sentinel stays a parse failure.

Forgiving on purpose: the alternative to accepting a recoverable shape is throwing
the section's data away.

### Config-validate rules

Enforced by the `classes` field validator in `config/models.py`:

- **reject** both `multi-instance` and `instance-array` on one class — they answer
  opposite questions, so setting both is a contradiction, not a stronger request;
- **reject** `multi-instance: true` on a class that already declares a top-level
  `instances` property (the wrapper would shadow it; rejected rather than renamed,
  because a silent rename changes the extraction contract under the user);
- **warn only** when the class's top level is nothing but one array-of-object.
  Having an internal array is **not** evidence of already being a list wrapper —
  an invoice with `line_items[]` is a single-instance document and
  `multi-instance: true` on it correctly gives `instances[i].line_items[j]`.

An already-wrapped schema legitimately carries both keys, so `is_wrapped` short-
circuits these rules; the transform is applied at runtime and never persisted, but
rejecting a schema this code produced itself would be a nasty trap.

### Evaluation baselines must be migrated

`evaluation/baseline_migration.py` (pure) plus
`scripts/migrate_multi_instance_baselines.py` (the S3 walker: dry-run by default,
idempotent, `--direction unwrap` for rollback, `--backup-suffix`). A wrapped
prediction against a flat baseline scores every field as missing-on-one-side, so
the class reads ~0 accuracy with no error — the one way this feature can break a
working deployment.

Rollback refuses to flatten a baseline holding more than one record, because that
would discard ground truth the user authored.

## Error Handling

The ExtractionService has built-in error handling:

1. If a section ID is not found in the document, an exception is raised
2. If extraction fails for any reason, the error is captured in `document.errors`
3. All errors are logged for debugging
4. Few-shot example loading errors are handled gracefully with fallback to standard prompts

### Schema-compliance filtering (Simple mode)

Advanced (agentic) extraction validates its output through a generated Pydantic
model, which structurally **ignores any field the class schema does not define**.
Simple (traditional) extraction has no such step — it does a raw `json.loads` and
keeps whatever the model emits — so a model that hallucinates or bleeds attributes
across classes (e.g. a `resume` section coming back with `publications`, or a
`scientific_publication` with `invoice_number`) would carry those off-schema fields
into `inference_result`. Downstream that breaks the confidence assessment: the
enhancer collapses a **list emitted for an attribute the schema does not declare as
an array**, leaving those rows permanently unscored and failing the section
(`assessment_schema_mismatch`).

To close that gap at zero extra model cost, `_filter_extracted_to_schema` runs on
the simple path right after parsing and **drops top-level keys not present in the
class schema's `properties`**, mirroring the agentic Pydantic behavior. It is
conservative and fail-open:

- No-op when the schema has no `properties` (can't tell what's off-schema).
- Only drops **unknown** top-level keys; defined fields pass through untouched
  (value/type correction remains assessment's job, not this filter's).
- Dropped names are logged and surfaced as an **`extraction_off_schema_fields`**
  (info) `ProcessingIssue` naming the fields, so a systematic prompt/schema
  mismatch is visible rather than silent.

> **Note on `additionalProperties`.** Enabling the agentic `validation` gate does
> *not* catch off-schema extras on its own: JSON-Schema validation allows unknown
> properties unless the class schema sets `additionalProperties: false`. Agentic
> mode is safe here because its Pydantic model ignores extras regardless; this
> filter gives Simple mode the same guarantee.

## Performance Optimization

For optimal performance, especially in serverless environments:

1. Only include the section being processed and its required pages
2. Set clear expectations about document structure and fail fast on violations
3. Use the Document model to track metering data
4. Consider the trade-off between few-shot example accuracy improvements and increased token costs

### Extraction Results Storage

The extraction service stores extraction results in S3 and only includes the S3 URI in the document:

1. Extracted attributes are written to S3 as JSON files
2. Only the S3 URI (`extraction_result_uri`) is included in the document
3. This approach prevents the document from growing too large when extraction results contain many attributes
4. To access the actual attributes, load them from the S3 URI when needed

## Multimodal Extraction

The service supports both text and image inputs:

1. Text content is read from each page's `parsed_text_uri`
2. Images are retrieved from each page's `image_uri`
3. Both are combined in a multimodal prompt to the LLM
4. Few-shot examples include both text prompts and document images for better understanding

## Thread Safety

The extraction service is designed to be thread-safe, supporting concurrent processing of multiple sections in parallel workloads.

## 1S-TopK: single-stage extraction + confidence (Simple mode)

When `extraction.mode: simple` and `extraction.confidence.mode: integrated`, the
service produces the extracted values **and** their per-field confidence in a
**single LLM call** — there is no separate Assessment pass, halving the number of
LLM invocations for the extraction workflow.

**How it works.** The selected task prompt
(`extraction.task_prompt_extraction_with_confidence_topk`, chosen automatically by
`prompt_assembly.select_extraction_task_prompt`) instructs the model to return,
per field, its **top-K guesses with probabilities** rather than a single value:

```json
{
  "Agency": { "G1": "ACME Media", "P1": 0.93, "G2": "ACME", "P2": 0.05, "G3": "...", "P3": 0.02 },
  "LineItems": [
    { "LineItemRate": { "G1": "800.15", "P1": 0.88 }, "LineItemDays": { "G1": "-TWTF--", "P1": 0.71 } }
  ]
}
```

`idp_common.extraction.topk_resolver.resolve_candidates` then splits this into:

- **`inference_result`** — the top guess `G1` per field (numbers coerced per schema).
- **raw confidence leaves** — `{"confidence": P1}` per field, stashed in
  `metering["_integrated_field_assessment"]`. `_save_results` runs these through the
  **shared** `enrich_assessment_with_thresholds` (attaches
  `confidence_threshold` from each field's `x-aws-idp-confidence-threshold` or the
  default, and builds threshold alerts) and grounds them into
  `explainability_info` — the identical output contract to separate mode, so
  evaluation, reporting, the UI, and HITL are all unchanged.
- **`metadata.topk_candidates`** — the full candidate set (all K guesses) per field,
  preserved for auditability, plus `metadata.assessment_method: "1s_topk"`.

Because `explainability_info` is already present in the saved `result.json`, the
downstream Assessment Lambda **auto-skips**.

**Shapes the resolver handles.** Three, plus a pass-through:

| Shape | Example |
|---|---|
| Scalar candidate | `{"Agency": {"G1": …, "P1": …}}` |
| Array, direct | `{"LineItems": [{"Rate": {"G1": …, "P1": …}}]}` |
| Array, wrapped | `{"LineItems": {"G1": [ … ], "P1": …}}` |
| **Group (object)** | `{"Address": {"City": {"G1": …, "P1": …}}}` |
| Pass-through | a field the model returned as a plain value — kept verbatim, no confidence leaf |

Groups recurse, so a group inside a group, and a list inside a group, both
resolve; `$ref`s are dereferenced against `$defs` at every level so numeric
coercion applies to nested sub-attributes too.

> **Group handling was previously missing**, and the failure was silent: a group
> fell through to pass-through, so the raw candidate dict became the *extracted
> value* (`Address.City` came out as `{"G1": "Anytown", "P1": 0.95, …}` instead of
> `"Anytown"`) and the group produced **no confidence leaves at all**, making its
> fields invisible to threshold alerts and HITL. Observed on every group field of
> every document processed in integrated mode.

⚠️ **Cost/completeness caveat on long lists.** The TopK envelope costs several
guesses per cell, so a list that fits comfortably in a plain extraction can exceed
what the model will emit in one response — and it stops emitting rows rather than
erroring. Measured on a 100-row list: `integrated` returned **10 of 100 rows**
where `separate` returned 100 of 100 in every repeat. List cells are therefore
asked for a **single** guess (`G1/P1` only, not four), which cuts list output
roughly 4×, and the prompt no longer instructs the model to make guesses *"as
short as possible"* — that wording was also causing it to return **shortened
values**, with the document's actual text demoted to `G2`. The single-response
limit is fundamental to this mode, so **prefer `separate` on list-bearing
schemas**.

**Why top-K.** Asking the model to enumerate and rank alternatives (instead of a
single value + a single confidence number) forces it to distribute probability
mass, yielding **better-calibrated, less-overconfident** scores. See Tian et al.,
*"Just Ask for Calibration"* (EMNLP 2023).

**Contract.** Output keys follow `G<N>` (guess) / `P<N>` (probability). K is
flexible — `G1`/`P1` alone is valid; the default prompt requests K=4. Only `G1`/`P1`
are consumed; `G2..GK`/`P2..PK` are preserved verbatim in `topk_candidates`. If the
model returns a flat response with no `G1`/`P1` candidates, the values pass through
unchanged and the standalone Assessment step runs as the fallback (no regression).
Under `geometry.mode: llm`/`llm_grounded` the bbox block is appended to the TopK
prompt, so a candidate may also carry `bbox`/`page`; the resolver carries those onto
the confidence leaf for the shared grounding path (under pure `llm` — no OCR
grounding — this is the only box source).

Advanced (agentic) integrated mode can opt into the same top-K elicitation via the
hidden experimental knob `extraction.agentic.integrated_confidence_strategy: topk`
(see below).

Reference config:
[`config_library/unified/realkie-fcc-verified/config-1s-topk-with-ocr-image.yaml`](../../../../config_library/unified/realkie-fcc-verified/config-1s-topk-with-ocr-image.yaml);
end-to-end demo: [`notebooks/misc/e2e-example-with-1s-topk.ipynb`](../../../../notebooks/misc/e2e-example-with-1s-topk.ipynb).

## Agentic Extraction with Table Parsing Tool

The extraction service supports an optional **agentic extraction mode** powered by the Strands agent framework with tool-based structured output. When enabled, the extraction agent gains intelligent tools including a deterministic table parser for robust tabular data extraction.

### Schema token budget: the schema is sent three times (#710)

Advanced extraction transmits the class schema **three times per request**. Measured
on `config_library/unified/lending-package-sample` -> `Payslip` at ~4 chars/token:

| # | Where | Tokens |
|---|---|---|
| 1 | prose `{ATTRIBUTE_NAMES_AND_DESCRIPTIONS}` substituted into the task prompt | ~1,485 |
| 2 | `"Expected Schema: ..."` appended to the **system** prompt | ~2,600 |
| 3 | the extraction tool's `inputSchema`, which Strands derives from the same model | ~2,595 |
| | **total ~6,680, of which copy 2 is 38%** | |

Copies 2 and 3 are **the same JSON string** — not merely equivalent; a test asserts
the substring match. So copy 2 carries no information copy 3 does not, and
`extraction.agentic.restate_schema_in_system_prompt: false` removes it. The agent
can still fetch the schema on demand via `get_extraction_schema_reminder`, which is
unaffected.

**It defaults to `true`, and that is deliberate.** Restating a schema in prose often
improves how closely a model follows it, so the duplication may be doing real work.
Treat this as an A/B knob: the benchmark `restatement` suite runs both arms on one
stack with identical code, and the gate is **completeness**, not token count — a
token saving that loses list rows is a loss.

**What the payoff actually is — smaller than it looks in both directions:**

- **Smaller in dollars.** All three copies sit inside the prompt-cache prefix, so on
  a repeated-class workload they are cache reads at roughly a tenth of input price.
- **It does NOT reduce shard count.** An earlier version of this section claimed the
  reclaimed tokens free "the same context budget that `context_buffer` /
  `shard_token_budget` manage", so removing them could keep a document out of an
  extra shard. **That is not how the code works.** `plan_shards` budgets against
  **OCR page text only**, and `compute_sizing_plan` derives that budget as
  `max_input × (1 - context_buffer)` minus an output reserve and an image reserve —
  prompt overhead (this restatement, the prose schema, the toolSpec, few-shot
  examples) is subtracted nowhere. It is absorbed by the blanket `context_buffer`
  (default 0.30), so the reclaimed tokens come off a safety reserve that is already
  ~60,000 tokens on a 200K-window model and were already unused. `max_pages_per_shard`
  (default 5) closes shards on page count regardless.

  So with the current design this is a per-request token reduction with **no**
  shard-count, cost or latency mechanism behind it. Making the shard budget subtract
  measured prompt overhead — which would make this knob pay for itself — is
  [#775](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/775).

A fourth copy is stored in agent state for the reminder tool, but it is only
transmitted if that tool is invoked, so it is not a per-request cost.

### Enabling Agentic Extraction

Configure agentic extraction in your configuration file:

```yaml
extraction:
  model: "us.anthropic.claude-sonnet-4-6"  # Anthropic Claude recommended for agentic
  agentic:
    enabled: true
    max_concurrent_batches: 1  # Parallel processing (2-10 for very large docs)
    table_parsing:
      enabled: true  # Enable deterministic table parser tool
      min_confidence_threshold: 95.0  # OCR confidence target (Textract only)
      min_parse_success_rate: 0.90  # Parse quality threshold
      use_confidence_data: true  # Cross-reference with OCR confidence
      max_empty_line_gap: 3  # Tolerate up to N empty lines in tables
      auto_merge_adjacent_tables: true  # Merge table fragments
    validation:                # see "Schema-Constraint Validation" below
      enabled: false
```

> **Every `agentic.*` sub-option only takes effect when `agentic.enabled: true`**
> — `table_parsing`, `validation`, and `max_concurrent_batches` are all ignored
> for non-agentic extraction. In the **Configuration UI** these options are
> progressively disclosed: they appear only after you enable Agentic Extraction,
> the table-parsing thresholds appear only after you enable the parse_table tool,
> and the escalation model appears only when validation `fail_action` is
> `escalate` — so you only see the knobs that currently matter.

### Table Parsing Tool

When `table_parsing.enabled: true`, the extraction agent gains a `parse_table` tool that:

1. **Deterministically parses Markdown tables** from OCR text without LLM inference
2. **Handles OCR artifacts robustly**:
   - Tolerates empty lines (page breaks) within tables via configurable lookahead
   - Recovers from missing pipe characters in corrupted rows
   - Automatically merges table fragments with identical columns
3. **Provides quality metrics and warnings**:
   - `parse_success_rate`: Ratio of cleanly parsed rows
   - `avg_confidence`: OCR confidence scores (when available from Textract)
   - `low_confidence_cells`: Specific cells needing LLM verification
   - **⚠️ Warnings**: Alert agent to table fragmentation or quality issues
   - **ℹ️ Info**: Confirm successful gap recovery
4. **Enables hybrid extraction workflow**:
   - Agent uses `parse_table` for well-structured tabular data
   - Falls back to LLM extraction for complex layouts or poor quality
   - Cross-validates low-confidence cells using multimodal reasoning

### How It Works

```
1. Agent analyzes document and identifies Markdown tables
2. Agent calls parse_table(table_text, expected_columns)
3. Tool returns:
   - Structured rows as list of dicts
   - Quality metrics (parse_success_rate, avg_confidence)
   - Warnings about potential incompleteness
4. Agent reviews quality:
   - If good (parse_rate >= 0.90, confidence >= 95):
     * Calls map_table_to_schema with column mapping and transforms
     * Calls finalize_table_extraction with scalar fields
   - If poor or warnings present:
     * Falls back to LLM extraction for affected sections
     * Verifies low-confidence cells using document images
5. Agent validates completeness:
   - Cross-checks row counts against document visuals
   - Extracts from ALL table fragments if multiple found
   - Verifies schema constraints (e.g., min_length) are met
```

### Three-Tool Table Extraction Workflow

The table extraction uses a three-tool pipeline that keeps large data out of the LLM context, minimizing token usage:

```
parse_table ──► map_table_to_schema ──► finalize_table_extraction
   │                    │                        │
   ▼                    ▼                        ▼
 Agent State:       Agent State:             Agent State:
 last_parse_        mapped_table_            current_extraction
 table_result       rows                     (validated Pydantic)
```

**Step 1: `parse_table`** — Deterministic Markdown parser. Finds all tables, recovers from OCR artifacts, returns structured rows with quality metrics. Result stored in agent state as `last_parse_table_result`.

**Step 2: `map_table_to_schema`** — Bulk transformation of parsed rows using a column mapping. The agent provides a small mapping dict; the tool transforms all rows instantly. Supports:
- **`column_mapping`**: Maps table columns to schema fields (case-insensitive, fuzzy substring matching)
- **`static_fields`**: Constant values added to every row (e.g., `{"account_number": "1234"}`)
- **`value_transforms`**: Per-field transforms applied during mapping:
  - `strip_currency`: Removes `$` and `,` (e.g., `"$1,234.56"` → `"1234.56"`)
  - `strip_whitespace`: Removes all internal whitespace
  - `lowercase` / `uppercase`: Case conversion
- **Merged-row auto-splitting**: Detects OCR page-boundary artifacts where two rows are concatenated on one line (e.g., `"$57.90 $55.11"` in multiple columns) and splits them back into separate rows

Mapped rows accumulate in agent state as `mapped_table_rows` (supports multiple calls for chunked processing).

**Step 3: `finalize_table_extraction`** — Combines mapped table rows (from state) with scalar fields the agent provides. Validates the complete extraction against the Pydantic schema model. The agent never generates large JSON — only a small `scalar_fields` dict.
- **`table_array_field`**: Schema field name for the table array (e.g., `"transactions"`)
- **`scalar_fields`**: Non-table fields (e.g., `{"statement_period": "Jan 2025"}`)

### Page Markers and Batch Extraction

When processing multi-page documents, the service inserts page boundary markers between page texts:

```
--- PAGE 1 ---
Account Number: 12345
Statement Period: January 2025

| Date | Description | Amount |
|---|---|---|
| 01/15 | Deposit | 3500.00 |
--- PAGE 2 ---
| 01/16 | ATM | -200.00 |
| 01/20 | Transfer | -1500.00 |
```

**Page marker format**: `--- PAGE {N} ---` (1-indexed)

The table parser transparently skips page markers inside tables — they do not break table continuity or appear in parsed rows.

**Sharded concurrent extraction** (`max_concurrent_batches > 1`): the section's
pages are split into **token-budgeted page ranges**, and each shard's prompt
contains **only that shard's OCR text and images** — not the whole document. The
shards run concurrently (up to `max_concurrent_batches` at a time) and their
results are merged. This serves two purposes:

> **Why "shard" and not "chunk"?** A *shard* here is a **non-overlapping page
> range fanned out to a concurrent extraction agent** — the term is chosen for
> its distributed-systems connotation of *partition-for-parallelism/scale*,
> which is exactly this feature's purpose. We deliberately avoid "chunk" because
> it already denotes several *other*, unrelated concepts elsewhere in the
> codebase: RAG-style overlapping text windows (`max_chunk_size`,
> `chunk_overlap_percentage`), sequential assessment list sub-batches
> (`assessment/batching.py`), rule-validation `chunking_occurred`, and streaming
> byte reads. Reusing "chunk" for agentic-extraction shards would collide with
> all of those; "shard" keeps this concept unambiguous.

1. **Bounds the context window.** Because each agent sees only its pages, a long
   or dense section that would overflow a single agent's context (the failure
   mode behind `ContextWindowOverflowException`) is split until each shard fits.
2. **Reduces wall-clock time** via parallelism.

Key behaviors:
- **Bounded by tokens AND pages.** Pages are grouped so each shard's estimated
  input stays under `shard_token_budget` (default **8,000**; `≈ chars/4`) **and**
  holds at most `max_pages_per_shard` pages (default **5**, `0` disables the page
  ceiling). A shard closes when *either* bound is hit. The page ceiling
  guarantees a large document shards even when its OCR text is unusually compact
  and would otherwise fit one token budget — so sharding engages **by default**
  with no per-config tuning. `max_concurrent_batches` is an **upper bound on
  parallelism and shard count** — a very large section is split into as many
  shards as needed to fit (capped at `max_concurrent_batches`), not exactly N
  equal pieces.
  > **Why the low default budget?** A high budget (the old 40,000 default) let
  > even a ~25-page dense table fit one shard, so sharding silently did *not*
  > engage and a single agent had to emit the whole giant table in one Bedrock
  > call → read timeout. 8,000 + a 5-page ceiling reliably shard large docs so
  > each agent's work stays bounded. Raise `shard_token_budget` for
  > large-context (`:1m`) models if you want fewer, larger shards.
- **Page-aligned splits keep table rows intact.** A table spans pages but each
  row lives on one page, so splits fall between rows; list fields are
  concatenated in page order on merge (no row loss/duplication).
- **Header context propagation.** The section's first-page text is prepended to
  every later shard (clearly marked "for context only") so column headers and
  page-1 scalar context survive the split.
- **Scalar merge.** Each shard extracts what it can see; scalars take the
  **first non-null** value across shards. If two shards disagree on a scalar, the
  first is kept and the conflict is recorded in `metadata.shard_scalar_conflicts`.

> If a single shard's input *still* exceeds the model context window, extraction
> raises a clear, actionable error (enable table parsing / lower
> `shard_token_budget` / use a larger-context `:1m` model) rather than the
> opaque Strands "insufficient messages for summarization" message.

```yaml
# Enable sharded concurrent extraction (up to 4 shards in parallel)
extraction:
  agentic:
    enabled: true
    max_concurrent_batches: 4
    shard_token_budget: 8000    # default; lower if shards still overflow, raise for 1M-context models
    max_pages_per_shard: 5      # default; page ceiling so large docs always shard (0 = disable)
    table_parsing:
      enabled: true
```

**Document-size guidance.** The defaults above are tuned to work without
hand-tuning on large documents (validated at scale on 100- and 200-page
single- and multi-table PDFs). For very large documents (100+ pages) prefer the
Step Functions Distributed Map runtime (`runtime: step_functions`) so each shard
runs in its own Lambda and the section is not bound by the single-Lambda 15-min
ceiling; the in-process runtime still shards correctly but a 200-page section may
approach that ceiling.

### ExtractionRuntime: pluggable orchestration over shared primitives

Sharding is factored into runtime-agnostic primitives in
`idp_common.extraction.runtime` so the **same** shard/merge logic runs whether
you call the library from a notebook, a CLI, a single Lambda, or a production
Step Functions Distributed Map — one implementation, no behaviour divergence.

**Primitives (the single source of truth):**

- `extract_one_shard(...)` — runs ONE shard's agent (via an injected
  `shard_runner`; `agentic_idp.default_shard_runner` in production) and is
  **idempotent**: if a `ShardPersistence` backend already holds a complete
  result for the shard's page range, it is loaded and returned instead of
  re-inferring. This is the asyncio task body AND the SFN Map iteration body.
- `merge_shard_results(...)` / `merge_shard_dicts(...)` — concatenate list fields
  in page order and take first-non-null scalars (recording conflicts).
- `ShardPersistence` protocol + `S3ShardPersistence`, keyed at
  `checkpoints/{execution_arn}/{section_id}/shards/shard_{start}_{end}.json`.

**Two backends behind the `ExtractionRuntime` interface:**

- **`InProcessRuntime`** (default) — plans shards, runs them via `asyncio.gather`
  + a semaphore, then merges. This is what a notebook / CLI / single Lambda uses;
  **sharding works fully here with no Step Functions dependency.**
  `ExtractionService.process_document_section()` routes its concurrent path
  through this runtime, so standalone usage is unchanged.
- **`StepFunctionsRuntime`** (production) — a nested SFN **Distributed Map** where
  each iteration is a thin shard Lambda calling `extract_one_shard` (one fresh
  15-minute Lambda per shard) and a following merge state calls
  `merge_shard_results`. Because each shard persists its result idempotently to
  S3, SFN's **native per-iteration retry re-runs only the failed/incomplete
  shards** — completed shards load from S3, with no custom near-timeout/reentry
  code and no 15-minute ceiling for the section as a whole.

Select the backend with `extraction.agentic.runtime` (`in_process` default, or
`step_functions`); the `EXTRACTION_RUNTIME` env var and an explicit `override`
argument also work. The in-process section Lambda additionally wires
`S3ShardPersistence`, so even on the in-process path an SFN `ExtractionStep`
retry of a timed-out section resumes only the incomplete shards.

**Standalone usage (plain Python, no SFN):**

```python
from idp_common.extraction import ExtractionService
service = ExtractionService(config=config)            # config.extraction.agentic.max_concurrent_batches > 1
doc = service.process_document_section(document, section_id)   # shards + merges in-process
```

See `notebooks/misc/standalone_sharded_extraction_demo.py` for a runnable
demonstration (offline with a fake agent; live with real Bedrock).

### Confidence Assessment (in-shard confidence & bounding boxes)

> **Config v0.6:** confidence scoring is an **output of extraction** — its settings
> live under `extraction.confidence` (was the top-level `assessment` block) and
> geometry under `extraction.geometry` (was `assessment.geometry_mode`). HITL moved
> to the top-level `hitl` block. Old configs are migrated on read.

Per-field confidence/bbox **assessment can run inside extraction** instead of as a
separate downstream step, controlled by `extraction.confidence.mode`:

```yaml
extraction:
  confidence:
    enabled: true                     # master on/off — false disables confidence entirely
    mode: separate                    # off | "separate" (default) | "integrated"
    model: us.anthropic.claude-haiku-4-5-20251001-v1:0
    list_batch_size: 25               # rows scored per inference (agentic in-shard)
  geometry:
    mode: ocr_only                    # ocr_only (default) | llm_grounded | llm | off
  agentic:
    enabled: true
    max_concurrent_batches: 4         # sharded; confidence scoring runs per-shard
hitl:
  enabled: false                      # route low-confidence fields to human review
  confidence_threshold: 0.8
```

- **`separate`** (default, **no behavior change on upgrade**): extraction and
  assessment are distinct inferences.
  - *Agentic*: after each shard extracts, a **second assessment inference runs
    inside that same shard** over the shard's pages/values — so assessment
    inherits the same per-shard scaling as extraction (a 200-page section never
    assesses in one oversized call). Per-shard assessments are collated on merge
    (per-field, **page-ordered for list items**, first-shard-wins for scalars),
    grounded once in real OCR geometry over the whole section, and emitted as
    `explainability_info` — byte-for-byte the same output the standalone
    Assessment step produces.
  - *Non-agentic*: unchanged — the pipeline flows to the standalone Assessment
    step exactly as before.
- **`integrated`**: confidence is produced **within the extraction inference
  itself** — the document is already in context, so there is no separate
  assessment request and no re-sent document. The inline result rides the **same**
  collation → post-merge reconcile → OCR grounding → `explainability_info` path as
  `separate`, so the output contract is identical; only the source of the
  confidence differs. The `extraction.confidence` model/prompt settings are unused,
  and the standalone Assessment step is bypassed (auto-skips once
  `explainability_info` is present).
  - *Agentic*: the agent emits confidence via a tool call (see the strategy knob
    below).
  - *Non-agentic (simple)*: the single extraction inference is prompted to return
    values **and** a parallel confidence structure as
    `{"extraction": {...}, "confidence": {...}}`; the service splits that envelope
    (values → `inference_result`, confidence → the same in-extraction marker),
    enriches it with per-field `confidence_threshold` + alerts, reconciles, grounds,
    and emits `explainability_info`. If the model returns a flat response with no
    confidence envelope, the path **falls back to the standalone Assessment step**
    (no data loss). Best for **smaller documents** where one inference comfortably
    holds the whole doc; for large docs prefer agentic (sharded) or `separate`.
  - **Missing-row robustness (both paths):** because integrated confidence rides on
    the extraction call, a model can under-score a large table. After reconcile,
    any list rows left unscored are **retried in focused, bounded re-assessment
    calls** (only the missing rows) and spliced back — so large-list confidence
    coverage reaches 100% rather than leaving null placeholders. Best for
    cost/latency once you've confirmed your model produces well-calibrated inline
    confidence; otherwise prefer `separate` (a dedicated assessment inference).

  <a id="integrated-confidence-strategy"></a>
  **Hidden setting — `extraction.agentic.integrated_confidence_strategy` (experimental).**
  For the *agentic* path, integrated confidence can be produced two ways. This knob
  is **not surfaced in the config UI** — it exists so operators can A/B the
  cost/latency-vs-calibration trade-off before we pick a default. Set it via
  `idp-cli config-upload` on a throwaway config version; both values produce
  identical `explainability_info` downstream (only the inference mechanics differ):

  | value | how confidence is produced | inferences per turn/shard (happy path) |
  |---|---|---|
  | `two_step` *(default)* | the agent extracts via the extraction tool, then calls `provide_field_assessment` in a **follow-up inference** within the same turn — a dedicated reflection pass over the finalized values | 3 (extract → assess → close) |
  | `single_shot` | the agent emits values **and** per-field confidence in **one combined tool call** (`extraction_with_confidence_tool`), saving the follow-up inference | 2 (extract+confidence → close) |

  Why it is not one inference either way: agentic extraction is delivered via a
  *tool call*, and the tool-use protocol ends an inference at each tool call, so a
  final "close the turn" inference is always required after the last tool result.
  `single_shot` removes the *middle* (assessment) inference, not the close. It
  reuses the same prompt cache as `two_step` (the document/system/tools prefix is
  written once and read by the closing inference). Trade-off: `two_step` gives the
  model a dedicated look at the finalized extraction before scoring (often
  better-calibrated confidence); `single_shot` is cheaper/faster but scores inline
  as it extracts. For large/multi-step (patched) extractions where rows are added
  after the combined call, the agent may still call `provide_field_assessment` once
  at the end to (re)assess every row (rows never assessed are padded with neutral
  `confidence: null`, same as today). Non-agentic (simple) integrated is always a
  true single inference and is unaffected by this knob.

**Standalone Assessment step bypass.** When in-shard (or integrated) assessment
has already written `explainability_info` to the section result, the downstream
Assessment Lambda detects it and **skips its own inference** (a cheap
pass-through) — so no duplicate assessment cost and no state-machine change. When
`extraction.confidence.enabled: false`, confidence scoring is skipped everywhere. The document status
remains `EXTRACTING` while in-shard assessment runs (it *is* part of the extraction
step).

**List alignment guarantee.** Downstream consumers index
`explainability_info[0][field][i]` against `inference_result[field][i]`, but an
assessment LLM often returns a *different* number of row assessments than the
table has rows (e.g. 44 assessments for 120 extracted rows). In-shard assessment
**reconciles each list field's assessment to exactly match the extracted row
count** (truncating extras, padding shortfalls with a neutral `confidence: null`
"not individually assessed" entry) *before* the per-shard merge — so confidence is
never misattributed to the wrong row and the drift never compounds across shards.

**Reuse, not duplication.** In-shard assessment calls the **same**
`AssessmentService.assess_results(...)` core the standalone step uses (extracted
into a pure, S3-free method) and the same `ocr_grounding.ground_assessment_geometry`
— so confidence scoring, thresholds, alerts, and geometry grounding are identical;
only the *where it runs* differs. The output contract (`explainability_info[0]` +
`section.confidence_threshold_alerts`) is unchanged, so HITL, the UI confidence
display, evaluation, and reporting all consume it as before.

### Configuration Options

#### `max_empty_line_gap` (integer, 0-10, default: 3)
Maximum consecutive empty lines to tolerate within a table before treating as table boundary. Higher values increase tolerance for OCR page breaks but may merge unrelated tables.

**Tuning guidance**:
- **High-quality OCR** (Textract LAYOUT): Use 2
- **Standard quality**: Use 3 (default)
- **Low-quality or complex documents**: Use 5-7
- **Multiple similar tables close together**: Use 1-2

#### `auto_merge_adjacent_tables` (boolean, default: true)
Automatically merge consecutive tables with identical column structure. Recovers from table splits caused by OCR artifacts like page breaks.

**When to disable**:
- Documents contain multiple distinct tables with same columns
- Need to preserve table boundaries for semantic reasons

#### `min_confidence_threshold` (float, 0-100, default: 95.0)
Minimum average OCR confidence (Textract scale) for agent to prefer table parsing over LLM extraction. Only applies when using Textract OCR backend.

#### `min_parse_success_rate` (float, 0-1, default: 0.90)
Minimum parse success rate for agent to trust parsed results. Below this threshold, agent should fall back to LLM extraction.

### Schema-Constraint Validation and Model Escalation

Agentic extraction always validates the agent's output against the **Pydantic
model** generated from the class JSON Schema (`field_constraints=True`), so
`enum`, `pattern`, numeric bounds and `minItems`/`maxItems` violations are fed
back to the agent for self-correction during extraction.

The `extraction.validation` block (moved up from `extraction.agentic.validation`
in v0.7, since Simple extraction runs this path too; stored configs migrate
automatically) adds **full JSON-Schema validation** of the final result — most importantly the `format` keyword
(`date`, `date-time`, `email`, `uri`, `uuid`, ...), which the generated Pydantic
model does **not** enforce — and an optional **bounded model escalation** when
validation still fails.

```yaml
extraction:
  agentic:
    enabled: true
    validation:
      enabled: false          # Off by default (no behavior change on upgrade)
      check_formats: true     # Enforce JSON-Schema 'format' keywords
      fail_action: escalate   # warn | escalate | reject
      escalation_model: "us.anthropic.claude-opus-4-8"  # stronger tier; "" = retry same model
```

How it works:

1. After extraction, the merged result is validated against the full class
   schema. All violations are collected at once (not one-at-a-time) with
   human-readable field paths.
2. `fail_action` controls the response when validation fails:
   - **`warn`** — record a `validation` block in the result metadata and proceed.
   - **`escalate`** — re-extract **only the failing top-level fields** with a
     stronger model (`escalation_model`), then merge the corrected fields back
     into the result. Scoping to the failing fields keeps the schema, prompt and
     output small — far cheaper and faster than re-running the whole section —
     and the fields that already validated are preserved untouched. The merged
     result is kept only if it is valid or has strictly fewer violations; then
     warn if it still fails. (When the failures can't be expressed as a field
     subset — e.g. they're root-level only — it falls back to a whole-section
     re-extraction.)
   - **`reject`** — mark `parsing_succeeded=false` so downstream/HITL can act.
3. The outcome is recorded under `metadata.validation` (see *Audit metadata*
   below).

**Audit metadata.** Each section's extraction result records, under `metadata`:
- `extraction_model` and `extraction_model_overridden` — the model that actually
  ran the section and whether it came from a per-class override.
- `metadata.validation` — `valid`, `error_count`, `failed_fields`, `errors`
  (path + validator + message), `check_formats`, `fail_action`,
  `initial_error_count` / `initial_failed_fields` (before any escalation), and —
  when escalation ran — `escalated`, `escalation_model`, `escalation_scope`
  (`field-subset` | `full-section`), `escalation_fields`, and
  `resolved_by_escalation`.
- `metadata.population_check` — completeness heuristic (advisory). Reports
  `fields_defined`, `fields_populated`, `population_ratio`, `below_threshold`,
  and `empty_fields` (dotted paths of unpopulated leaves). A warning is logged
  when the ratio falls below `validation.min_population_ratio` (default `0.5`).
  This catches *silent* extraction loss — e.g. nested fields returning null, or
  a table that extracted zero rows — that schema validation alone cannot, since
  sparse-but-valid output is still schema-valid. It never fails extraction (a
  genuinely sparse document scores low too); set `min_population_ratio: 0` to
  silence the warning.

**Escalation model precedence:** per-class `x-aws-idp-extraction-escalation-model`
schema extension → global `validation.escalation_model` → the extraction model
itself (escalation becomes a plain second attempt).

**Configuration UI.** The global `validation` block (enabled / check_formats /
fail_action / escalation_model) is editable under **Extraction → Agentic
Extraction → Schema Validation & Escalation** in the Configuration editor. The
per-class `x-aws-idp-extraction-escalation-model` override is editable as
"Escalation Model Override" in the **Document Schema** editor, next to the
per-class extraction-model override.

**An empty list with OCR evidence to the contrary is rejected.** `null = absent`
(below) is the right convention for a scalar, but for a *list* it is also exactly
what a total row loss looks like — and it breaks no schema constraint unless the
config sets `minItems`, which most do not. So the in-loop validator adds one
evidence-based check on the **single-agent path**: when the OCR pre-flight
(`_analyze_ocr_for_tables`) finds a substantial table — its own `>30` pipe-table-row
threshold, the same signal that drives the tool guidance — and **every** declared
top-level array field comes back `null`, `[]`, or absent, the result is rejected and
the agent gets a correction round naming the field and the row count
(`validation.find_empty_declared_lists` / `build_empty_list_feedback`).

Deliberately narrow:
- **All-empty only.** A populated sibling list means the detected tables plausibly
  belong to that one, and an empty sibling may be genuinely absent.
- **Single-agent only.** Per-shard this inference is unsound — a shard legitimately
  contains none of the whole document's rows. Sharded output is validated once
  after merge, as it already was for `minItems`.
- **Not a schema violation.** It does not enter `ValidationReport`, so escalation
  behaviour is unchanged. `metadata.completeness_check` reports it separately as
  `unexplained_empty_lists` (with a `complete` flag), leaving
  `schema_constraints_met` meaning exactly what it says.
- **Its effect is one more agent turn**, never a failure. After the last attempt
  the loop keeps the best-effort result, so the worst case is one wasted turn on a
  document that genuinely has no rows inside a detected table.

⚠️ **Not gated on `validation.enabled`** — unlike the schema checks above. That flag
defaults to `false`, and the config that produced this bug has it `false`, so the
first version of this check (which *was* gated on it) was dead on exactly the
configurations that needed it — caught by live verification, not by the tests. A
guard against **silent data loss** cannot itself be off by default. The two checks
are independently enabled: schema validation stays opt-in; the empty-list check
runs whenever the OCR evidence is present. `_build_schema_validator` returns `None`
only when *neither* applies.

The failure this closes: an agent declined the deterministic table parser because
one column was OCR-corrupted (`tool_usage_decision.agent_stated_reason`: *"the
Amount column was OCR-corrupted with jumbled text instead of numbers, so
parse_table/map_table_to_schema couldn't cleanly map usable numeric values"*), then
returned a 100-row `Transactions` list as `null`. Schema-valid, scalar accuracy
1.000, status COMPLETED. `SYSTEM_PROMPT` and `TABLE_PARSING_PROMPT_ADDENDUM` now
state the rule outright — declining the tool obliges direct extraction, and one
unreadable column means that *cell* is null, not the row and not the list.

**Null = absent.** Extraction follows the convention "return `null` if a field is
not found", and the generated Pydantic model makes every non-required property
`Optional[...] = None`. Validation therefore treats a `null` property as
**absent**: an optional field left null passes, while a *required* field left
null surfaces as a `required` violation (not a confusing type error). Enum /
pattern / format / numeric / `minItems` checks on present values are unaffected.

> **`format: date` caveat.** JSON-Schema `format: date` means ISO-8601
> (`YYYY-MM-DD`). The default extraction prompt asks the model for `MM/DD/YYYY`,
> which is **not** a valid `date` format and will fail format validation. If
> your schema uses `format: date` for non-ISO dates, either set
> `check_formats: false` or use a `pattern` instead of `format`.

### Benefits

- **Faster extraction**: Deterministic parsing is faster than LLM inference for well-structured tables
- **Higher accuracy**: Eliminates LLM hallucination for tabular data
- **Better completeness**: Intelligent recovery from OCR artifacts prevents data loss
- **Cost reduction**: Reduces token usage for large tables
- **Hybrid flexibility**: Agent intelligently chooses between parsing and LLM based on quality

### OCR Backend Compatibility

The table parsing tool works with any OCR backend producing Markdown tables:

| OCR Backend | Markdown Support | Confidence Data | Notes |
|-------------|-----------------|-----------------|-------|
| **Textract** (TABLES) | ✅ Yes | ✅ Yes | Best for structured tables |
| **Textract** (LAYOUT) | ✅ Yes | ✅ Yes | Handles complex layouts |
| **Bedrock OCR** | ✅ Yes | ❌ No | Tool uses parse_success_rate only |
| **Chandra OCR** | ✅ Yes (markdown mode) | ❌ No | High-quality Markdown output |

When OCR confidence data is unavailable, the tool relies on `parse_success_rate` and column consistency for quality assessment.

### Example: Bank Statement with 1000+ Transactions

```yaml
# config.yaml
extraction:
  model: "us.anthropic.claude-sonnet-4-20250514-v1:0"
  agentic:
    enabled: true
    table_parsing:
      enabled: true
      max_empty_line_gap: 5  # Handle multi-page statements
      auto_merge_adjacent_tables: true

classes:
  - $id: BankStatement
    type: object
    properties:
      account_number:
        type: string
      statement_period:
        type: string
      transactions:
        type: array
        minItems: 1000  # Completeness validation
        items:
          type: object
          properties:
            date: {type: string}
            description: {type: string}
            amount: {type: number}
            balance: {type: number}
```

**Extraction flow**:
1. Agent identifies transaction table in OCR text
2. Calls `parse_table(table_text)` → Returns 1020 rows with quality metrics
3. Tool warnings: "ℹ️ Successfully recovered 3 gaps in table data"
4. Calls `map_table_to_schema(column_mapping={"Date": "date", "Description": "description", ...}, value_transforms={"amount": "strip_currency"})` → All 1020 rows transformed instantly
5. Calls `finalize_table_extraction(table_array_field="transactions", scalar_fields={"account_number": "12345678", "statement_period": "January 2024"})` → Validated against Pydantic model
6. Completeness check logs: "Extraction exceeds minimum constraint: 'transactions' has 1020 items (minimum: 1000)"

### Troubleshooting

**Problem**: Agent extracts only 900 records instead of 1020

**Root causes & solutions**:
1. **Table split by empty lines** → Increase `max_empty_line_gap` to 5
2. **Multiple table fragments** → Ensure `auto_merge_adjacent_tables: true`
3. **Agent stopped early** → Check extraction logs for timeout/retry issues
4. **Schema constraint too strict** → Reduce `minItems` or make it optional

**Problem**: Parse quality is low (< 0.90)

**Solutions**:
1. **Improve OCR quality** → Use Textract LAYOUT or Chandra OCR
2. **Complex table structure** → Use LLM extraction instead of parse_table
3. **Merged cells or nested headers** → Preprocess table or use LLM

**Problem**: Unrelated tables being merged

**Solutions**:
1. Reduce `max_empty_line_gap` to 1-2
2. Set `auto_merge_adjacent_tables: false`
3. Add more context to distinguish tables semantically

### Observability

Extraction results include table parsing metadata when the tool is used:

```json
{
  "metadata": {
    "extraction_method": "agentic",
    "table_parsing_tool_used": true,
    "table_parsing_stats": {
      "tables_parsed": 1,
      "rows_parsed": 1020,
      "parse_success_rate": 0.98,
      "avg_confidence": 96.5,
      "confidence_available": true,
      "invocation_count": 1
    }
  }
}
```

When a section is sharded across concurrent agents, each shard contributes its own
`table_parsing_stats` and they are merged with quality-aware semantics (not summed):
counts (`tables_parsed`, `rows_parsed`, `rows_mapped`, `invocation_count`) add up,
while `parse_success_rate` and `avg_confidence` are combined as **row-weighted
averages** so the reported values stay a real 0-1 rate / 0-100 confidence regardless
of shard count.

Use these metrics to:
- Identify documents where table parsing is working well
- Detect quality issues requiring configuration tuning
- Measure cost savings vs LLM-only extraction

## Future Enhancements

- ✅ Few-shot example support for improved accuracy and consistency
- ✅ Class-specific example filtering for targeted extraction guidance
- ✅ Multimodal example support with document images
- ✅ Enhanced x-aws-idp-image-path support for multiple images from directories and S3 prefixes
- ✅ Agentic extraction with tool-based structured output
- ✅ Deterministic table parsing tool for robust tabular data extraction
- 🔲 Dynamic few-shot example selection based on document similarity
- 🔲 Confidence scoring for extracted attributes
- 🔲 Support for additional extraction backends (custom models)
- 🔲 Automatic example quality assessment and recommendations
- 🔲 Table structure detection for complex layouts (merged cells, nested headers)
