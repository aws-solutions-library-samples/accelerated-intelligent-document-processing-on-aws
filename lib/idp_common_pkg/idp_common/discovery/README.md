Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Discovery Module

The Discovery module provides automatic document class and schema discovery using LLMs. It analyzes sample documents to generate JSON Schema definitions for new document types, enabling rapid configuration of new document processing workflows.

## Overview

The discovery service uses Amazon Bedrock LLMs to:
- Analyze document content (text + images) to identify document types
- Generate JSON Schema definitions with attribute extraction fields
- Auto-detect document section boundaries in multi-page PDFs
- Optionally compare results against ground truth for validation

## Components

- **`ClassesDiscovery`**: Main service class for document class discovery

## Usage

### Basic Discovery

```python
from idp_common.discovery.classes_discovery import ClassesDiscovery

# Initialize with S3 document location
discovery = ClassesDiscovery(
    input_bucket="my-bucket",
    input_prefix="documents/sample.pdf",
    region="us-east-1"
)

# Discover document classes and generate schema
result = discovery.discovery_classes_with_document(
    input_bucket="my-bucket",
    input_prefix="documents/sample.pdf",
    save_to_config=False  # Don't save to DynamoDB
)

schema = result["schema"]  # JSON Schema dict
```

### Discovery with Local File Bytes

```python
# Use local file bytes (skip S3 read)
with open("sample.pdf", "rb") as f:
    file_bytes = f.read()

result = discovery.discovery_classes_with_document(
    input_bucket="local",
    input_prefix="sample.pdf",
    file_bytes=file_bytes,
    save_to_config=False
)
```

### Discovery with Page Range

```python
# Only analyze specific pages
result = discovery.discovery_classes_with_document(
    input_bucket="my-bucket",
    input_prefix="documents/packet.pdf",
    page_range="3-5",  # Only pages 3-5
    save_to_config=False
)
```

### Discovery with Ground Truth Comparison

```python
result = discovery.discovery_classes_with_document(
    input_bucket="my-bucket",
    input_prefix="documents/sample.pdf",
    ground_truth_attributes={"invoice_number": "INV-001", "date": "2024-01-15"},
    save_to_config=False
)
```

### Auto-Detect Document Sections

For multi-document packets, auto-detect section boundaries:

```python
sections = discovery.auto_detect_sections(
    input_bucket="my-bucket",
    input_prefix="documents/packet.pdf"
)
# Returns: [{"start": 1, "end": 3, "type": "W2 Form"}, {"start": 4, "end": 6, "type": "Invoice"}]
```

### Class Name Hint

Provide a hint for the expected document class:

```python
result = discovery.discovery_classes_with_document(
    input_bucket="my-bucket",
    input_prefix="documents/w2.pdf",
    class_name_hint="W2 Tax Form",
    save_to_config=False
)
```

## Key Methods

| Method | Description |
|--------|-------------|
| `discovery_classes_with_document()` | Main discovery method — analyzes a document and generates JSON Schema |
| `auto_detect_sections()` | Detects document type boundaries in multi-page PDFs |
| `parse_page_range()` | Static method to parse page range strings (e.g., `"3-5"`) |
| `extract_pdf_pages()` | Static method to extract a subset of pages from a PDF |

## Saving Discovered Schema (Augment vs. Replace)

When `save_to_config=True`, discovered classes are persisted to a target
configuration version via `ClassesDiscovery._merge_and_save_class()`. This
method is **always additive**: it reads the version's existing `classes`, keys
them by `$id` (falling back to `x-aws-idp-document-type`), and inserts each newly
discovered class — overwriting only a class with the same identifier. It never
deletes classes the user curated.

### Re-discovering an existing class preserves its authored settings

Overwriting a class with the same identifier is a **merge**, not an assignment.
`_merge_and_save_class()` calls
`idp_common.config.class_settings.carry_forward_authored_settings()`, which copies
onto the discovered class every class-level key the discovery response did not
itself contain — the discovery LLM only ever emits `$id`,
`x-aws-idp-document-type` and `properties`, so this is what keeps class-level
model pins, prompt overrides, confidence thresholds, classification regexes,
page-type routing, few-shot `x-aws-idp-examples` and
`x-aws-idp-multi-instance` / `-instance-array` alive across a re-run.

- The rule is *preserve anything the generator did not emit*, deliberately not a
  list of keys to keep: a deny-list stops covering extension keys added after it
  was written. Two carve-outs, both for keys that describe the `properties` map
  the generator just replaced: `required` / `$defs` / `dependentRequired` /
  `propertyNames` are never carried, and `x-aws-idp-instance-array` is carried
  only while the property it names still exists (keeping a dangling one fails
  `IDPConfig.validate_instance_array`, aborting the whole save).
- Keys the caller synthesized rather than received from the model are passed in
  `synthesized` and lose to an authored value. `_normalize_class_id()` returns
  that set — it derives `description` from an id it had to rename, which must not
  overwrite a description the author wrote.
- The stale-id path carries settings across a rename before deleting the old
  entry, so normalizing `Task cards` → `Task-cards` does not double as a reset.
- Scope is class-level. Keys inside `properties` (per-attribute
  `x-aws-idp-evaluation-method` / `-evaluation-threshold`) are replaced with the
  property, because a re-derived attribute can change type and a stale evaluation
  method on it can be worse than none.
- A setting discovery *does* replace is logged as a `WARNING` naming the key, so
  the change is visible at write time rather than in the next inference. That
  includes `description`, which discovery's prompt asks the model for and which
  feeds the classification prompt's class table. The class id is excluded: it is
  rewritten by `_normalize_class_id`, which logs its own rename.

`bda/blueprint_optimizer.py::_apply_optimized_schema` and
`synthesis/bootstrap.py::merge_class_into_version` use the same helper, for the
same reason: neither the BDA→IDP transform nor the bootstrap schema author emits
`x-aws-idp-*` authoring keys, so saving their output straight over an existing
class erased them. **Not** covered: `bda_blueprint_service`'s `bda_to_idp` sync
with `sync_mode: replace`, which replaces surviving classes from the same
transform output by design — there the BDA project is the declared source of
truth.

The UI's **Save mode** selector controls whether the target version's schema is
cleared *before* discovery runs:

| Save mode | Behavior |
|-----------|----------|
| `augment` (default) | Existing classes are kept; discovered classes are merged in (dedup by `$id`). |
| `replace` | The target version's schema list is cleared **once, up front** in the discovery upload resolver, then discovery merges the new classes into the now-empty list. |

Replace is implemented in the resolver
(`nested/api-resolvers/src/lambda/discovery_upload_resolver/index.py`,
`_clear_version_schema()`), **not** inside `_merge_and_save_class()`. Clearing
once before enqueuing jobs is what makes Replace correct for multi-section and
multi-document discovery, where a single submission produces *N* jobs that each
merge one class into the same version — clearing per-job would leave only the
last class. Class discovery clears `classes`; Policy Discovery clears
`policy_classes`. All other config sections are preserved.

## Configuration

Discovery uses configuration from DynamoDB (loaded via `get_config()`). Key settings:

```yaml
discovery:
  model: us.amazon.nova-pro-v1:0
  temperature: 0.0
  system_prompt: "..."
  task_prompt: "..."
```

## Integration with IDP SDK

The discovery module is also accessible through the IDP SDK:

```python
from idp_sdk import IDPClient

client = IDPClient(stack_name="my-stack")
result = client.discovery.run(
    file="sample.pdf",
    class_name_hint="Invoice"
)
```

## Related Documentation

- [Discovery Documentation](../../../../docs/discovery.md) — Full discovery workflow guide
- [Configuration Guide](../../../../docs/configuration.md) — Configuration management
