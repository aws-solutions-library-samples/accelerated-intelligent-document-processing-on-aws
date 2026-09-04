Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Schema Module

The Schema module provides utilities for dynamically generating Pydantic v2 models from JSON Schema definitions. This enables structured extraction where LLM responses are validated against user-defined schemas.

## Two submodules

| Module | Depends on | Purpose |
|---|---|---|
| `pydantic_generator.py` | `datamodel-code-generator` (extraction extra) | generate a Pydantic v2 model from a JSON Schema at runtime |
| `multi_instance.py` | **nothing** (stdlib + `config.schema_constants`) | the `x-aws-idp-multi-instance` schema transform (GitHub #715) |

> ⚠️ **This package's re-exports are LAZY** (PEP 562 `__getattr__` in
> `__init__.py`), and must stay that way. `pydantic_generator` imports
> `datamodel_code_generator`, which is an extraction-only dependency absent from
> most Lambda layers — so an eager re-export made
> `from idp_common.schema.multi_instance import …` drag the whole code generator in
> with it, and `config/models.py` (imported by *every* Lambda) needs that helper.
> The result was `UpdateDefaultConfig` failing a live stack update with
> `No module named 'datamodel_code_generator'`. Pinned by
> `tests/unit/schema/test_lazy_package_imports.py`; add any new dependency-free
> submodule the same way.

## `multi_instance.py` — Synthesize mode (#715)

A class flagged `x-aws-idp-multi-instance: true` has its **effective** schema
replaced by a List-of-Class wrapper, so a section holding several documents of the
class extracts every one of them. Pure, idempotent, never mutates its input:

```python
is_multi_instance(class_schema) -> bool     # tolerates "true" from a config round-trip
is_wrapped(class_schema) -> bool
wrap_class_schema(class_schema) -> dict     # no-op (same object) when unflagged
unwrap_instances(inference_result) -> list[dict] | None
wrap_instances(records) -> dict
```

Every stage derives the wrapper independently from **config** (the source of
truth), at the single point it loads a class schema. Do not persist the wrapped
schema. Full rationale — why a transform rather than an envelope, where each
class-level key lands, the `$defs` hoist, and the two empirical gotchas — is in
[`../extraction/README.md`](../extraction/README.md#synthesize-mode-x-aws-idp-multi-instance-715).

`_ensure_model_covers_schema` in `pydantic_generator.py` is the guard that keeps
model selection honest for a wrapper: the selected model must declare the schema's
top-level properties, or the inner `items` model is chosen and ONE record is
silently validated where a list was requested. ⚠️ It now **raises** when no
generated model covers them — fail-loud, but a behaviour change: two property
names that sanitize to the same Python identifier go from "silently validated with
a merged field" to a hard section failure.

## Overview

The module uses `datamodel-code-generator` to convert JSON Schema into Pydantic models at runtime. It handles:
- Cleaning custom `x-aws-idp-` extension fields from schemas
- Generating Pydantic v2 `BaseModel` classes from JSON Schema
- Optional JSON Schema validation for advanced constraints (`contains`, `if/then/else`, `dependentSchemas`, etc.)
- Circular reference detection

## Public API

### Functions

| Function | Description |
|----------|-------------|
| `create_pydantic_model_from_json_schema(schema, class_label, ...)` | Main entry point — generates a Pydantic model from a JSON Schema dict |
| `clean_schema_for_generation(schema, fields_to_remove=None)` | Recursively removes custom extension fields from a JSON Schema |

### Exceptions

| Exception | Description |
|-----------|-------------|
| `PydanticModelGenerationError` | Raised when Pydantic model generation fails |
| `CircularReferenceError` | Raised when circular references are detected in the schema |

## Usage

### Generate a Pydantic Model from JSON Schema

```python
from idp_common.schema import create_pydantic_model_from_json_schema

schema = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "Invoice",
    "type": "object",
    "properties": {
        "invoice_number": {"type": "string", "description": "The invoice number"},
        "total_amount": {"type": "number", "description": "Total amount due"},
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "amount": {"type": "number"}
                }
            }
        }
    }
}

# Generate model
Model = create_pydantic_model_from_json_schema(
    schema=schema,
    class_label="Invoice",
    clean_schema=True,
    enable_json_schema_validation=True
)

# Use the model to validate data
result = Model(invoice_number="INV-001", total_amount=1250.00, line_items=[])
```

### Clean Custom Extension Fields

```python
from idp_common.schema import clean_schema_for_generation

schema_with_extensions = {
    "type": "object",
    "x-aws-idp-document-type": "Invoice",
    "x-aws-idp-examples": [...],
    "properties": {
        "name": {"type": "string", "x-aws-idp-custom": "value"}
    }
}

cleaned = clean_schema_for_generation(schema_with_extensions)
# All x-aws-idp-* fields are removed
```

## Integration with Extraction

The schema module is used internally by the extraction service to validate LLM responses against configured JSON Schemas. When `classes` in the configuration use JSON Schema format, the extraction service calls `create_pydantic_model_from_json_schema()` to build validation models.

## Related Documentation

- [JSON Schema Migration](../../../../docs/json-schema-migration.md) — Migrating to JSON Schema format
- [Extraction](../extraction/README.md) — How extraction uses schema validation
