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
model/feature-compatibility guards). Two guards, with deliberately different
scopes — both hard errors at config time rather than an obscure mid-processing
failure:

| Guard | Rejects | Why |
|---|---|---|
| `_validate_agentic_openai` | OpenAI GPT-5.x with `extraction.agentic.enabled` | Served via the `bedrock-mantle` Responses API, incompatible with the Converse-based Strands loop |
| `_validate_discovery_openai` | OpenAI GPT-5.x **and xAI Grok** as a discovery model | Discovery ingests whole PDFs as Converse `document` blocks; both models take text + image only, so the document would be silently dropped |

Grok is therefore rejected for **discovery** but not for agentic extraction. The
authoritative per-model answer is
`idp_common.bedrock.client.document_blocks_unsupported_reason()` — call it rather
than duplicating the model list.

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
| `revisions.py` | `ConfigRevisionStore` — immutable numbered snapshots of a Configuration Profile's configuration. See [Configuration Profiles and revisions](#configuration-profiles-and-revisions). |
| `constants.py` | Configuration constants, including the reserved profile names and the active-profile pointer key. |
| `class_names.py` | Canonical rules for document class ids — `is_valid_class_name()` / `sanitize_class_name()`. See [Class ids](#class-ids). |
| `class_settings.py` | `carry_forward_authored_settings()` — preserve a class's hand-authored class-level `x-aws-idp-*` keys when a generator (Discovery, BDA blueprint optimization) regenerates that class. See [Regenerating a class](#regenerating-a-class). |
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
`description` overrides the definition's) and follows `$ref` chains. An
unresolvable `$ref` — remote, dangling, or cyclic — leaves the node returned
as-is, so callers degrade to the un-dereferenced reading rather than raising. A
non-dict node yields `{}` instead, so callers can `.get()` the result
unconditionally.

```python
from idp_common.config.schema_utils import deref_schema

prop = deref_schema(class_schema["properties"]["Signatures"], class_schema)
prop["type"]  # "object", not None
```

Callers: the confidence prompt's attribute-description formatter and the
confidence enhancer's attribute-type read (`assessment/service.py`), the
classification attribute-name walk (`classification/service.py`), and the
assessment escalation-skip reason plus the integrated/BDA threshold enrichment
(`assessment/batching.py`). The Web UI carries a deliberate port, `derefSchema`
in `configuration-layout/PromptPreview.tsx`, so the prompt preview shows the
same attribute list the backend builds — keep the two in step.

Dereference for the **type/description** read specifically; do not hoist it over
a property wholesale. `_assess_core` reads a property's own
`x-aws-idp-confidence-threshold` right beside its `type`, and honoring one
declared on the `$defs` definition rather than the property is a change to
threshold *inheritance* — the carve-out below, not a bug to fix in passing.

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

## Regenerating a class

Three write paths regenerate an existing document class from a model's output —
Discovery (`discovery/classes_discovery.py::_merge_and_save_class`), BDA
blueprint optimization (`bda/blueprint_optimizer.py::_apply_optimized_schema`)
and schema bootstrap (`synthesis/bootstrap.py::merge_class_into_version`).
All three used to assign the generated dict over the existing class, which erased
every class-level `x-aws-idp-*` key an author had set. The write reported
success, the class looked right, and the loss only appeared in the *next*
document processed — as a different extraction model, a missing escalation, a
re-included class or dropped records.

```python
from idp_common.config.class_settings import carry_forward_authored_settings

carried = carry_forward_authored_settings(existing_class, new_class, synthesized)
# new_class is mutated in place; `carried` lists the keys taken from existing_class
```

- **The rule is "preserve anything the generator did not emit"**, not a list of
  keys to keep — a deny-list silently stops covering extension keys added later.
  It has exactly two carve-outs, both for keys that describe the `properties` map
  the generator just replaced rather than the class itself:
  `_PROPERTY_COUPLED_KEYS` (`required`, `$defs`, `dependentRequired`,
  `propertyNames`) are never carried — a stale `required` is validated against
  every extracted object, so it reports a missing property on every document
  forever — and `x-aws-idp-instance-array` is carried only while the property it
  names survives, because `IDPConfig.validate_instance_array` **raises** otherwise
  and the save path constructs `IDPConfig`, so a dangling pointer aborts the whole
  write instead of losing one setting. Both drops are logged.
- **`synthesized`** names keys the caller derived itself rather than receiving
  from the model, so they lose to an authored value. Discovery passes
  `{"description"}` when `_normalize_class_id()` filled a description in from a
  class id it had to rename.
- **Falsy authored values are settings**, not absences:
  `x-aws-idp-exclude-from-processing: false` and a `0` threshold are carried.
- **Scope is class-level.** Keys inside `properties` (per-attribute
  `x-aws-idp-evaluation-method` / `-evaluation-threshold`) are replaced along with
  the property, because a regenerated attribute can legitimately change type and
  carrying a stale evaluation method onto it can be worse than dropping it.
- **Carried values are deep-copied**, so a carried list/dict is not shared with
  the existing class dict — `_apply_optimized_schema` hands its result back to a
  caller that may still hold that dict.
- A setting the generator *does* replace is logged as a `WARNING` naming the key,
  including `description`. `$id` / `x-aws-idp-document-type` are excluded: those
  are rewritten by the caller's id normalization, which logs its own rename.

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

## Configuration Profiles and revisions

A **Configuration Profile** is the named entity users manage (`default`,
`Production`, `lending`) — the RBAC object, the document-visibility partition, and
the activation target. A **revision** is an immutable numbered snapshot of one
profile's configuration, cut by `save_configuration()` on every save. The user-facing
guide is [docs/configuration-profiles.md](../../../../docs/configuration-profiles.md).

The invariant: **revisions are content, profiles are access-control objects.** Scope
(`allowedConfigVersions`) is checked at the profile; nothing checks a revision.

| Item | Key | Holds |
|---|---|---|
| Profile head | `Config#<profile>` | The working configuration (gzip Binary), plus `LatestRevision` / `PublishedRevision` |
| Revision index | `ConfigRevIndex#<profile>` | One small entry per retained revision (number, timestamps, author, label, notes, size, class fingerprint, pinned) |
| Revision body | `s3://<ConfigurationBucket>/config_revisions/<profile>/<nnnnnn>.json.gz` | The full configuration that revision recorded |
| Active pointer | `Config#__active` | The active profile name (`__active` is a reserved profile name) |

Four decisions worth knowing before changing this code:

- **Bodies are in S3, not DynamoDB.** `ConfigurationTable` is HASH-only, so listing
  profiles requires a `Scan`, and DynamoDB bills a scan on **full item size
  regardless of `ProjectionExpression`**. Storing revision bodies in the table would
  make the profile list — which the UI loads constantly — more expensive with every
  save.
- **Metadata is one index item per profile.** Listing history is a single `get_item`,
  not a scan. Appends use DynamoDB's native `list_append` (which cannot lose a
  concurrent append); the rare read-modify-write paths (label, delete, prune) are
  guarded by an `IndexSeq` counter with one retry.
- **`ConfigRevIndex#` deliberately does not match `begins_with(Configuration,
  "Config#")`.** That filter lists profiles and feeds the scope-filtered dropdowns; a
  revision leaking into it would look like a profile with no configuration.
  `list_config_versions()` additionally skips reserved names.
- **History is best-effort.** If the revision cannot be recorded, the save still
  succeeds (logged at WARNING). Losing a history entry is recoverable; refusing a
  save is an outage. `ConfigRevisionStore.enabled` is False when no
  `CONFIGURATION_BUCKET` is configured, so older deployments and unit tests keep
  working unchanged.

`_record_revisions()` skips the cut entirely when the saved configuration equals
what was already stored. Every deployment re-saves `default` and each managed
profile, so without that check a few no-op upgrades would evict a user's real
history from the retention window.

Retention keeps the last `CONFIG_REVISION_CAP` (default 20) revisions per profile,
plus the published revision and anything labeled or pinned by a test run. A
count-based cap cannot be expressed as an S3 lifecycle rule, which is why pruning
runs in `ConfigRevisionStore.prune()` on write.

`restore_revision()` is forward-only: it saves the chosen revision as a *new*
revision rather than rewinding the counter, so history is never rewritten.

### Reading a pinned revision

`get_config(version=…, revision=…)` (→ `ConfigurationManager.get_merged_configuration`)
loads a specific revision's stored body instead of the profile head. Every pipeline
Lambda passes `document.config_revision`, which the queue processor pins at queue
time, so a save made mid-flight cannot change the configuration under an in-flight
document.

Two deliberate choices:

- **A missing pinned revision raises.** It does *not* fall back to the head: a run
  that silently used the wrong configuration looks successful, and its numbers then
  enter a comparison.
- **No "published revision" branch on the unpinned path.** The head always holds
  the published revision's content, and reading the head is one `get_item` against
  an S3 GET, so an unpinned read stays on the head.

`resolve_published_revision(profile)` returns the revision a new document should be
pinned to, or None when the profile has no history (an older deployment, or one
untouched since the upgrade) — in which case consumers fall back to the head, which
is the pre-revision behavior.

`confidence_fingerprint()` hashes only the configuration that determines what a
confidence number *means* (extraction model/sampling, assessment). It is recorded on
every revision so confidence curves can eventually be branched per semantics rather
than per profile; nothing keys off it yet.

Both fingerprints normalize numerics (`_canonical_numbers`) before hashing, because
a configuration arrives here by two routes that disagree about numeric type: from a
save it is JSON (`float`), read back from DynamoDB it is `Decimal`, and `json.dumps`
falls back to `default=str` for `Decimal`. Without normalization `temperature: 0.0`
hashed three different ways — as `0.0`, as `"0.0"`, and as `"0"` when DynamoDB
returned an unscaled zero — giving one configuration several fingerprints, which is
precisely what a fingerprint exists to rule out. `bool` is special-cased because it
is an `int` subclass and `enabled: true` must not collapse into `enabled: 1`.

Fingerprints recorded by revisions cut before this normalization landed may differ
from the value the same configuration hashes to now. That is harmless while nothing
keys off them, but anything that starts comparing stored fingerprints must treat a
mismatch on a pre-normalization revision as "unknown" rather than "changed".

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
