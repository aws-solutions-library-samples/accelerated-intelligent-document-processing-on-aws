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

### A key no field matches is reported, at every depth

⚠️ **Of the models reachable from `IDPConfig`, three take `extra="allow"`, none
takes `extra="forbid"`, and every other one takes Pydantic's default
`extra="ignore"`.** So a key no field matches is **dropped during validation**, and
the setting the author believes they changed simply is not set — which is
indistinguishable from a working configuration, because the shipped default is in
force and the run completes.

That scope is exactly `IDPConfig`'s tree, and **`extra="forbid"` on a record root
buys nothing below it.** This module holds three other root models. `PricingConfig`
and `ModelConfigLimitsConfig` forbid extras at depth 0, so a stray key *there*
raises — but `PricingEntry`, `PricingUnit` and `ModelLimitEntry`, the element types
of their one list field each, take the permissive default, so a mistyped key inside
a row was dropped in silence
([#1211](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1211)).
`SchemaConfig` takes `extra="allow"` and its fields name no model, so it drops
nothing and has nothing to report.

⚠️ **`ModelConfigLimitsConfig` is not reachable from `IDPConfig` at any depth** —
there is no `model_limits` field on it — which is why the walk written for #1134
never saw a limit row. `PricingEntry` *is* reachable, via `IDPConfig.pricing`, so a
misspelled key in a pricing row was already reported when a whole configuration
document was validated and not when the `DefaultPricing`/`CustomPricing` record was
validated on its own. The walk takes any root model, so what was missing in both
cases was the call.

Each record root now makes that call from its own `mode="before"` validator, through
the shared `log_ignored_config_keys`, which is also what `IDPConfig`'s validator uses
— one wording, one `deprecated`/`unknown` split, one bound on the line, and the
message names the root it came from. **Attach a new root's report there and not to a
save path:** these roots are built from a dict in four modules — the configuration
resolver behind the Pricing and Model Limits panels, `update_configuration` at deploy
time, `ConfigurationManager` (both `save_configuration` and the per-record `save_*`
helpers), and, for pricing, the merge that re-validates a dumped record — and the
resolver, the operator-facing one, does not go through `save_configuration`.

`include_top_level` stays off for all of them, and on the two that forbid extras that
is not a matter of taste: Pydantic raises for a depth-0 key, so a line saying it was
ignored would be false.

`tests/unit/config/test_record_root_unknown_keys.py` derives the root set from the
annotation on `ConfigurationManager.save_configuration` — the enumeration production
code already keeps — and the models under each root from the annotations, so a fifth
record type is covered by those tests without being named in them. It also checks
`config_library/pricing.yaml` and `config_library/model_config_limits.yaml` against
their own roots. `scripts/tests/test_preset_keys_are_read.py` deliberately excludes
those two files from its scan, correctly, because they are not `IDPConfig` documents;
the effect was that no gate read them against any model.

`IDPConfig.log_deprecated_fields` reports those keys. It walks the whole model
tree, so a key at any depth is named with its **dotted path**:

```
IDPConfig: Ignoring unknown nested fields (not defined in model, so the shipped
default stays in force): extraction.validation.enabld (did you mean
extraction.validation.enabled?), ocr.dpi (did you mean ocr.image.dpi?)
```

`validate_config()` puts the same findings in `result["warnings"]`, which is where
`idp-cli config-validate` shows them — the moment a typo is cheap to fix — and in
`result["ignored_keys"]` as `{path, kind, suggestion}` for a caller that needs to act
rather than print. `idp-cli` and `idp_sdk` both consume those, so **this is the only
reporter at any depth**. Each used to compute its own top-level extras as
`set(config) - set(IDPConfig.model_fields)`, which said two keys the loader honours
would be ignored — `description`, which `update_configuration` pops and stores, and
`rule_classes`, which is renamed to `policy_classes` — and, once the library began
reporting too, said everything else twice.

`config-validate --strict` keeps its contract of failing on a **top-level** extra
only. Extending it downwards would fail configurations that pass today, in the one
flag built for a pipeline; the nested finding is reported either way.

**It reports; it does not reject.** `extra` is unchanged on every model, so a
stored configuration that loads today still loads. Rejecting would refuse
configurations that work, and would need a migration story for every key a later
version removes.

The walk is `models.collect_ignored_config_keys(data, model)`, and it is public
because the gates that ask the same question of shipped files call it rather than
reimplementing the resolution, so none of them can drift from what a load actually
drops: `scripts/tests/test_preset_keys_are_read.py` over `config_library/`,
`tests/unit/config/test_unknown_nested_keys.py` over the merged defaults, and
`tests/unit/config/test_record_root_unknown_keys.py` over the pricing and model-limit
records.

Three things to know before using it:

- **Migrate first.** A legacy key is *relocated* on load, not dropped —
  `extraction.agentic.validation` becomes `extraction.validation` — so against a
  pre-migration dict the walk reports a key that works. The model validator runs
  after `migrations.migrate_config` for that reason; `validate_config` migrates a
  copy before asking.
- **Two things are deliberately not unknown**, and both are read off the models
  rather than listed. A field whose annotation names no model is a free-form
  document whose keys are the author's (`classes`, `policy_classes`, a hook's
  `args`), so the walk does not enter it. A model with `extra="allow"` *keeps* an
  undeclared key, so nothing is dropped and there is nothing to report.
- ⚠️ **One thing is a gap rather than a decision:** a field whose annotation names
  *more than one* model — a discriminated union — is not entered, because nothing
  in the annotation says which member a value is, and keys in there **are** dropped.
  No field in the tree is shaped that way today, and
  `test_no_field_in_the_tree_holds_a_model_the_walk_declines_to_enter` fails when one
  appears, because otherwise the guarantee narrows silently: the models the walk
  reaches would stop including that subtree and every derived parametrisation would
  shrink with it.
- **One path is suppressed**, `discovery.output_format`, a dead knob this repository
  ships in its own system defaults. The reasoning, the ratchets and why it is not in
  `scripts/tests/gate_exemptions.json` are written at
  `SUPPRESSED_IGNORED_KEY_PATHS`.
- **The mis-nested case is the sharp one.** `dpi` is a real field of
  `ImageConfig`; written as `ocr.dpi` it is dropped, and `ImageConfig`'s
  validator never runs — so `ocr.dpi: "abc"` is accepted in silence while
  `ocr.image.dpi: "abc"` raises. When you probe this config tree, assert the
  value **arrived** (`cfg.ocr.image.dpi == expected`), never that construction
  succeeded.
- **A suggestion is offered only when it is the only answer.** First the wrong-depth
  question, read outwards from where the key was written — the written prefix, then
  its parent, stopping at the first level with any candidate — which answers both
  directions: `ocr.dpi` → `ocr.image.dpi` and `ocr.image.backend` → `ocr.backend`.
  Within that level the shallowest candidate wins if it is alone there, and a tie
  declines: `enabled` is declared at eight places one level under `extraction`, so
  `extraction.enabled` gets no hint, and `hitl.model` reaches the root to find eleven
  and declines rather than answering with `classification.model`. Failing that, a
  close name among the **siblings** (`enabld` → `enabled`). A wrong path is worse
  than none: it sends the author to edit something correct. A list step is spelled
  `ocr.postHook[].arn` — notation, since the dotted form is not a path — and a
  mapping subtree gets findings but no suggestions, because a suggestion there would
  have to invent a key name.

## Files

| File | Purpose |
|------|---------|
| `models.py` | Typed `IDPConfig` Pydantic models (per-service config: OCR, classification, extraction, assessment, summarization, evaluation, chat, discovery, …). The source of truth for config field defaults and validation. |
| `merge_utils.py` | Merge user config with system defaults, diff/strip helpers, and `validate_config()` with its enhanced validators. |
| `configuration_manager.py` | `ConfigurationManager` — CRUD against the DynamoDB Configuration Table (Default + Custom records), compression, versioning. Takes an optional `region`; see [Region for the underlying clients](#region-for-the-underlying-clients). |
| `migration.py` | Migration of legacy configuration formats to the current JSON-Schema-based format. |
| `revisions.py` | `ConfigRevisionStore` — immutable numbered snapshots of a Configuration Profile's configuration. See [Configuration Profiles and revisions](#configuration-profiles-and-revisions). |
| `retired_models.py` | `RETIRED_MODELS` / `is_retired()` — the single registry of Bedrock models past their end-of-life date. See [Retired models](#retired-models). |
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
every revision, and Test Studio's confidence curves are keyed by it (#698): the test
runner recomputes it from the configuration body it captures for a run and stamps
it on the run item, so the curve key never depends on the value stored in the
revision index.

Both fingerprints normalize numerics (`_canonical_numbers`) before hashing, because
a configuration arrives here by two routes that disagree about numeric type: from a
save it is JSON (`float`), read back from DynamoDB it is `Decimal`, and `json.dumps`
falls back to `default=str` for `Decimal`. Without normalization `temperature: 0.0`
hashed three different ways — as `0.0`, as `"0.0"`, and as `"0"` when DynamoDB
returned an unscaled zero — giving one configuration several fingerprints, which is
precisely what a fingerprint exists to rule out. `bool` is special-cased because it
is an `int` subclass and `enabled: true` must not collapse into `enabled: 1`.

Fingerprints recorded by revisions cut before this normalization landed may differ
from the value the same configuration hashes to now. The curve keys are unaffected
(they are recomputed from the captured body, never read from the index), but
anything that compares *stored* index fingerprints must treat a mismatch on a
pre-normalization revision as "unknown" rather than "changed".

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

## Region for the underlying clients

`ConfigurationManager(table_name=…, region=…)`,
`ConfigurationReader(table_name=…, region=…)` and the `get_config(…, region=…)`
convenience wrapper take an optional `region`, which is passed to the DynamoDB
resource they build and, through `ConfigRevisionStore`, to the S3 client used for
revision history.

`region=None` means "let boto3 resolve it" — `AWS_REGION`, then
`AWS_DEFAULT_REGION`, then the profile, then IMDS. That is the right value inside
a Lambda, where the runtime always sets `AWS_REGION`, and it is why these classes
worked for years without the parameter.

**An out-of-region caller must pass it.** A DynamoDB table name is not
region-qualified, so a caller that resolved `ConfigurationTable`'s physical id
from CloudFormation in one region and then builds a manager without that region
reads and writes *the same name* in whatever region the ambient credentials
resolve to. On a multi-region account that is a successful write to a different
stack's configuration table, and the caller is told it succeeded. Every
`idp-cli config-*` command, `idp-cli bootstrap`, `idp-cli discover`,
`idp-cli config-sync-bda` and `scripts/migrate_multi_instance_baselines.py` are
out-of-region callers in this sense. Two service classes build their own clients
and take a `region` for the same reason — `BdaBlueprintService`, which writes
BDA-derived document classes, and both discovery classes, which write the
discovered schema and rules.

`scripts/tests/test_config_region_threading.py` enforces this across the whole
tree: it parses every tracked `.py` and requires each construction of these
classes to pass a `region` unless it lives in a Lambda-deployed directory, where
the runtime always sets `AWS_REGION`. That exemption is decided by **directory**
rather than by a list, so a new handler is covered automatically; the two
library-internal exceptions are named there with a premise the file asserts.

The precedence, stated once: an explicit `--region` (or `region=`) wins;
otherwise boto3's own chain applies. No hardcoded region is substituted at any
point in this layer.

## Retired models

`retired_models.py` holds every Bedrock model past its AWS **end-of-life** date —
inaccessible in every region, every call returning
`ResourceNotFoundException: This model version has reached the end of its life`.
That is distinct from `LEGACY`, where existing users can still invoke the model and
it correctly stays selectable; only the first class is listed.

It lives in shipped code because three consumers need the same answer:
`validate_config` (so `idp-cli config-validate` and `config-upload --validate`
reject a configuration that pins a dead model before a document fails two stages
in), `scripts/tests/test_model_surface_consistency.py`, and the #708 gate
`scripts/sdlc/tests/test_retired_models_not_offered.py`.

**Why one registry and not two.** There were two, and they encoded contradictory
policies. The #708 gate required a retired model to be *absent* from
`pricing.yaml`, because `validate_config` derived its valid-model set from that
file and the absence is what made validation fail. But a pricing entry is read
retrospectively — a cost report over documents processed while the model was still
selectable resolves its rate by model id, so deleting the row re-prices historical
runs at zero. Both goals are legitimate and neither can be met by the presence or
absence of a pricing row. So validation now rejects a model because it is *known to
be retired*, the pricing row stays, and the fragile coupling to an unrelated file
is gone.

`is_retired()` matches any region or geo variant: end of life is a property of the
**foundation model**, so when `amazon.nova-premier-v1:0` was withdrawn the `us.`,
`eu.` and `global.` profiles routing to it died with it, and the bare form is the
one GovCloud uses.

`was_offered` decides one thing only — whether a `pricing.yaml` row must be
retained. A model this solution never made selectable cannot appear in anyone's
cost report, so it needs no rate, and inventing one would breach the "never invent
model facts" rule in `.claude/skills/add-model.md`.

## Adding or changing a model

Model defaults and inference fields live in `models.py`, and model/feature
compatibility is enforced in `merge_utils.py`. Adding a selectable Bedrock model
touches many other files too (template enums, pricing, UI, the bedrock client,
docs) — follow the checklist in
[.claude/skills/documentation.md](../../../../.claude/skills/documentation.md).

Removing one that has reached **end of life** is the reverse walk of that
checklist, with one exception: the model keeps its `pricing.yaml` entry, its
`model_config_limits.yaml` pattern and its quota-code entries, because all three
are consulted for whatever model a *deployed* stack's stored configuration names,
which is a superset of what is newly selectable. What must go is every surface a
customer can newly choose from — the template enums, the UI dropdown, the config
presets and any `default=` in `models.py`.
`scripts/tests/test_model_surface_consistency.py` enforces both halves.
