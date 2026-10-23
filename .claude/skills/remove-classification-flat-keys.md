# Removing the pre-0.9 flat classification keys

Use this when a release is ready to stop reading the **flat** classification keys
that config format 0.9 replaced (`classification.model`, `.task_prompt`,
`.granularity`, `.labeling`, `.invalidClassFallback`, … — the full list is
`FLAT_KEYS` in `lib/idp_common_pkg/idp_common/config/migrations/v08_to_v09.py`).
Since 0.9 every one of them is converted on read; this skill removes that
conversion. Do not start it because the code looks redundant: the conversion is
what lets an old **pinned revision** load.

## 1. First check: census what is still stored at the old shape

Two stored things can hold flat keys. ⚠️ **Both have to be checked, and the
heads are the half that is easy to get wrong.**

**A revision body is immutable.** It is converted in memory on every load
(`ConfigurationManager._load_revision_config` → `IDPConfig(**body)`) and never
rewritten, so a pre-0.9 body stays pre-0.9 for as long as it is retained.

**A profile head is not guaranteed to have converged either.** The only thing
that moves a head onto the current shape by itself is the write-back in
`ConfigurationManager.get_merged_configuration`, and it is conditional three
ways. The configuration editor reads through `get_raw_configuration`, which
relocates keys in memory and writes nothing back. The write is skipped when the
resolved configuration already matches what is stored. And
`_backfill_may_be_persisted` **refuses** it for a profile whose published
revision was cut before revisions recorded a `storedHash`, because rewriting the
head would destroy the `UpdatedAt <= createdAt` comparison that is that
revision's only integrity proof — it also fails closed when the revision index
cannot be read. A profile in that state is converted on every read and keeps the
old shape in storage indefinitely. What lifts it is a **save** (an editor save,
Save As Version, `idp-cli config-upload`, or a stack deployment for `default`
and the managed presets), because a save records a hash and stores the converted
body; a profile nobody saves again never converges. Nothing sweeps user profiles
at upgrade time, so on a stack that has been through several releases expect to
find heads stored at more than one configuration format, all of them resolving
correctly on read.

Removing the conversion makes each of those — head or body — either fail
validation or, worse, load with its classification settings silently replaced by
defaults, so a run against it would use a configuration nobody chose.

So, before anything else, census both. For a stack you can reach (read-only):

```python
import boto3
from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.migrations._version import is_at_or_after

manager = ConfigurationManager()      # needs CONFIGURATION_TABLE_NAME and CONFIGURATION_BUCKET
table = boto3.resource("dynamodb").Table(TABLE_NAME)

# Heads. Read the item and decompress it: get_raw_configuration() relocates
# keys and restamps the version, so it reports the CURRENT format for a head
# stored at an older one.
for row in table.scan(ProjectionExpression="Configuration").get("Items", []):
    key = str(row.get("Configuration", ""))
    if not key.startswith("Config"):
        continue
    item = table.get_item(Key={"Configuration": key}).get("Item")
    stored = ConfigurationManager._decompress_item(item or {})
    if not is_at_or_after(stored.get("config_format_version"), "0.9"):
        print("HEAD", key, stored.get("config_format_version"))

# Revision bodies.
for profile in manager.list_config_versions():
    name = profile["versionName"]
    for entry in manager.list_revisions(name):
        body = manager.get_revision(name, entry["revision"]) or {}
        if not is_at_or_after(body.get("config_format_version"), "0.9"):
            print("REVISION", name, entry["revision"], body.get("config_format_version"))
```

Check the method names against `configuration_manager.py` before running it. Two
things about the head half: `config_format_version` lives **inside** the gzipped
`_compressed_config` blob rather than as a top-level attribute, so it is neither
projected by `list_config_versions` nor queryable and every record has to be
fetched and decompressed; and `_is_rollback_to_older_format` in
`src/lambda/update_configuration/index.py` is the in-tree precedent for exactly
that walk, so read it rather than reinventing the decompression.

Then the two bounds on what a green census means:

- Revisions expire under the Configuration bucket's `DataRetentionInDays`
  lifecycle rule. A revision cut before the 0.9 upgrade is gone only once that
  many days have passed **since the upgrade** on that stack, and only if
  retention is finite on that stack. A head never expires, so retention does not
  help with that half at all.
- ⚠️ **The fleet-wide premise cannot be established from inside this
  repository, and this check does not try to.** Nothing in the tree enumerates
  stored configuration formats across deployments — there is no API for it and
  no schema field exposing the stamp — so a census proves a property of the one
  stack you ran it on and says nothing about any other. This repository ships to
  customers, so the release that removes the conversion must come after one that
  told them, in its migration notes, to re-save the profiles and revisions they
  still need, re-saving being what stores a profile converted. If that notice
  has not shipped, stop here and ship it first.

## 2. Make an old body refuse, never degrade

Replace the conversion with a refusal rather than deleting it outright. A body
holding any `FLAT_KEYS` key must raise a `ValueError` that names the profile, the
revision and the remedy (restore it under the last release that reads it, then
save it again). Silently ignoring the keys is the failure this skill exists to
avoid. Keep `FLAT_KEYS` itself — the refusal needs it.

## 3. What to remove or change

| Where | What |
|---|---|
| `config/migrations/v08_to_v09.py` | the conversion body; keep `FLAT_KEYS`, `TARGET_VERSION` and the refusal |
| `config/migrations/v07_to_v08.py` | only reachable through flat keys; remove with it, keeping the chain's version stamping |
| `config/models.py` — `ClassificationConfig.migrate_flat_keys` | the before-validator that converts flat kwargs; make it refuse |
| `config/merge_utils.py` — `_nested_classification` | the defensive conversion in the save-time validators |
| `patterns/unified/src/mlflow_logger_function/index.py` — `_classification_view` | the flat-dict fallback branch |
| `src/lambda/update_configuration/index.py` | nothing — it writes the nested path already; confirm |
| `lib/idp_common_pkg/tests/unit/config/fixtures/base-classification-0.6.12.yaml`, `base-classification-0.8.yaml` | keep, and repoint their tests at the refusal |
| tests that **build** flat classification dicts as a shorthand | `grep -rn '"classification": {' lib/idp_common_pkg/tests` and look for flat keys; the `_service` helpers in `test_two_stage.py`, `test_packet_granularity.py`, `test_modality.py` and `test_classification_service.py` are the large ones |
| `scripts/tests/test_classification_prompt_copies_in_sync.py`, `test_nuveen_boundary_precedence.py` | their `_page_prompt` helpers read either shape; drop the flat branch |
| `docs/migration-v0612-to-v0613.md` | keep; add a line saying which release stopped reading the old layout |

## 4. Verify

- `cd lib/idp_common_pkg && make test-unit`, then `make test` from the root.
- `git grep -nE "FLAT_KEYS|migrate_v08_to_v09|migrate_flat_keys"` — every hit is
  the refusal, its tests, or this skill.
- A test that loads `base-classification-0.6.12.yaml` through
  `IDPConfig(**…)` and through `_load_revision_config` asserts the refusal and its
  message.
- `CHANGELOG.md` `[Unreleased]`: a **Changed** entry, flagged ⚠️, saying that a
  revision saved before config format 0.9 no longer loads and what to do instead.
