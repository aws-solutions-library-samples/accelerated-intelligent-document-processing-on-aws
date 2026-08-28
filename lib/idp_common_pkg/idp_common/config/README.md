Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Configuration Module

`idp_common.config` manages the IDP configuration: loading it from the DynamoDB
Configuration Table, merging user-provided overrides with system defaults,
validating it against typed Pydantic models, and exposing it to services either
as a plain dict or as a typed `IDPConfig` model.

For the user-facing configuration guide (Web UI editing, custom config paths,
inheritance), see [docs/configuration.md](../../../../docs/configuration.md).

## Public API

```python
from idp_common.config import (
    get_config,            # Load merged config (dict or IDPConfig model)
    ConfigurationReader,   # Read configuration records from DynamoDB
    ConfigurationManager,  # Lower-level CRUD on the Configuration Table
)
from idp_common.config.models import IDPConfig
from idp_common.config.merge_utils import merge_config_with_defaults, validate_config
```

### Loading configuration

```python
from idp_common.config import get_config

# As a plain dict (default)
config = get_config(as_model=False)

# As a typed Pydantic model (validated; attribute access)
idp_config = get_config(as_model=True)
model_id = idp_config.extraction.model
```

### Validating configuration

`validate_config()` powers `idp-cli config-validate`. It merges with system
defaults, runs Pydantic validation, and applies enhanced checks (valid model
IDs, max-token limits, required prompt placeholders, schema-field warnings, and
model/feature-compatibility guards such as rejecting OpenAI Responses models for
agentic extraction or discovery).

```python
from idp_common.config.merge_utils import validate_config

result = validate_config(user_config, pattern="pattern-2")
if not result["valid"]:
    for err in result["errors"]:
        print("ERROR:", err)
```

## Files

| File | Purpose |
|------|---------|
| `models.py` | Typed `IDPConfig` Pydantic models (per-service config: OCR, classification, extraction, assessment, summarization, evaluation, chat, discovery, …). The source of truth for config field defaults and validation. |
| `merge_utils.py` | Merge user config with system defaults, diff/strip helpers, and `validate_config()` with its enhanced validators. |
| `configuration_manager.py` | `ConfigurationManager` — CRUD against the DynamoDB Configuration Table (Default + Custom records), compression, versioning. |
| `migration.py` | Migration of legacy configuration formats to the current JSON-Schema-based format. |
| `constants.py` | Configuration constants. |
| `class_names.py` | Canonical rules for document class ids — `is_valid_class_name()` / `sanitize_class_name()`. See [Class ids](#class-ids). |
| `schema_constants.py` | JSON Schema extension keys (e.g. `x-aws-idp-document-type`, `x-aws-idp-extraction-model`, `x-aws-idp-extraction-system-prompt`, `x-aws-idp-extraction-task-prompt`). |
| `schema_utils.py` | `deref_schema()` — resolve a local `#/$defs/<name>` `$ref` against a class schema. See [Dereferencing `$ref` subschemas](#dereferencing-ref-subschemas). |
| `system_defaults/` | Packaged default configuration YAML used as the merge base. |

## Dereferencing `$ref` subschemas

The Web UI's schema editor emits every group and list-item shape into the
class's `$defs` and references it, so a group property looks like
`{"$ref": "#/$defs/Signatures"}` — carrying **no** `type` and **no**
`description` of its own. Any consumer that reads those keys straight off the
property therefore sees an untyped, undescribed leaf and silently treats a
whole group as a scalar.

`deref_schema(node, root)` is the single shared fix. It returns the referenced
subschema with sibling keys on the referencing node layered on top (a local
`description` overrides the definition's) and follows `$ref` chains. Anything
unresolvable — a remote `$ref`, a dangling name, a cycle, a non-dict node —
is returned as-is so callers degrade to the un-dereferenced reading rather
than raising.

```python
from idp_common.config.schema_utils import deref_schema

prop = deref_schema(class_schema["properties"]["Signatures"], class_schema)
prop["type"]  # "object", not None
```

Callers: the confidence prompt's attribute-description formatter
(`assessment/service.py`), the classification attribute-name walk
(`classification/service.py`), and the assessment escalation-skip reason
(`assessment/batching.py`).

> **Note:** `assessment/threshold_resolver.py` keeps its own `_deref`. Its
> dangling-ref and definition-wins-over-sibling semantics are load-bearing for
> threshold inheritance in `resolve_threshold_for_path()`, so it is
> deliberately not routed through this helper.

Anything that walks a class schema after dereferencing must guard against
**recursive** `$defs` (a definition whose member references the definition):
dereferencing makes those reachable where reading the raw property did not.
`deref_schema` itself is cycle-safe, but a recursive *walk* over the result is
not — track the `$ref` targets already entered on the current branch, as
`_get_attribute_names_for_class()` does.

## Class ids

A document class id (`$id` / `x-aws-idp-document-type`) is composed into
downstream resource names, so it is constrained by its strictest consumer:
Bedrock Data Automation requires a blueprint name matching `[a-zA-Z0-9-_]+`, and
blueprint names are built as `{stack}-{class_id}-{suffix}`. `class_names.py` is
the single definition of that rule, so write paths and name-composing paths
cannot drift:

```python
from idp_common.config.class_names import is_valid_class_name, sanitize_class_name

is_valid_class_name("Bank_Statement")   # True
is_valid_class_name("Task cards")       # False
sanitize_class_name("Task cards")       # "Task-cards"
sanitize_class_name("Bank_Statement")   # "Bank_Statement"  (unchanged)
sanitize_class_name("???")              # ""  -> caller decides
```

Two properties matter when calling it:

- **Valid ids are returned byte-identically**, underscores included. Do not
  substitute `BdaBlueprintService._sanitize_project_name`, which maps `_` to `-`
  — renaming a working class would orphan the BDA blueprint created under the
  old name (lookup misses it, and orphan cleanup then deletes it as unexpected).
- **The empty string means "nothing usable"**, not "use a default". Callers
  raise or skip; inventing a name would silently mislabel the class.

Callers: `discovery/classes_discovery.py` (normalizes a discovered id at its
single write path, matches a stale un-normalized entry for the *same* class so
re-discovery replaces it rather than duplicating it, and sanitizes the
`class_name_hint` before injecting it into the prompt),
`bda/bda_blueprint_service.py` (blueprint create, lookup, and orphan-cleanup
prefixes — all three must agree), `bda/blueprint_optimizer.py`,
`discovery/multi_document_discovery.py` (reports the id that was saved).
The Web UI's `SchemaBuilder.tsx` enforces the same pattern for hand-authored
classes.

## Configuration records

Configuration is stored in DynamoDB with two record types:
- **Default** — built-in pattern configurations (from `config_library/` at deploy time).
- **Custom** — user-provided overrides, merged over the defaults.

The same Default/Custom pattern is used for auxiliary records:
- **`DefaultPricing` / `CustomPricing`** (`PricingConfig`) — service pricing for
  cost estimation; Custom is deep-merged over Default (`get_merged_pricing`).
- **`DefaultModelConfigLimits` / `CustomModelConfigLimits`**
  (`ModelConfigLimitsConfig`) — the ordered, first-match-wins list of per-model
  token limits, seeded from `config_library/model_config_limits.yaml`. Because
  entry **order is semantic**, Custom stores a **full replacement list** rather
  than a delta: `get_merged_model_config_limits()` returns Custom if present,
  else Default. Consumed at runtime by
  `bedrock.model_utils.get_model_max_output_tokens()` (60s cache; falls back to
  the on-disk `config_library/` YAML when no table is configured).

## Rollback-safe DynamoDB serialization

A CloudFormation stack rollback reverts the config custom-resource Lambda to the
**prior release's** code but leaves the current-shape config records in
DynamoDB; the reverted code then re-reads them. If the current shape carries a
value an older Pydantic model rejects, the custom resource fails *on the
rollback path* and wedges the stack in `UPDATE_ROLLBACK_FAILED`. Two known
breaking value classes: `None` on a field an older model coerces with a bare
`int()` (→ `int(None)` `TypeError`), and `0` on a field an older model
constrains with `gt=0` (→ `ValidationError`).

To keep updates rollback-safe, `ConfigurationRecord.to_dynamodb_item`
(`models.py`) calls `_omit_rollback_hostile_defaults`, which **omits any scalar
field whose value equals its declared default AND is `None` or integer `0`**.
Because absent == default for the current model, this is behavior-neutral on
read here, while sparing a reverted older model from values it cannot parse.
Booleans, float `0.0` (e.g. `temperature`), positive defaults, and any non-default
`0` are preserved. As a second layer, the `update_configuration` custom resource
detects a rollback (a stored `config_format_version` newer than the running
code's) and returns SUCCESS rather than FAILED on a parse error, so the rollback
completes instead of wedging — a genuine forward bad-config still fails loudly.

## Adding or changing a model

Model defaults and inference fields live in `models.py`, and model/feature
compatibility is enforced in `merge_utils.py`. Adding a selectable Bedrock model
touches many other files too (template enums, pricing, UI, the bedrock client,
docs) — follow the checklist in
[.claude/skills/documentation.md](../../../../.claude/skills/documentation.md).
