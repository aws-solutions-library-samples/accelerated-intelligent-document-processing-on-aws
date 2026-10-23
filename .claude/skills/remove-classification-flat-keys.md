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

**A revision body is immutable.** It is converted in memory on every read
(`ConfigurationManager._read_revision_body`, and again through
`IDPConfig(**body)` on the pinned path — §2 names both) and never rewritten, so a
pre-0.9 body stays pre-0.9 for as long as it is retained.

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
from idp_common.config.configuration_manager import ConfigurationManager
from idp_common.config.constants import CONFIG_TYPE_CONFIG
from idp_common.config.migrations._version import is_at_or_after

manager = ConfigurationManager()      # needs CONFIGURATION_TABLE_NAME and CONFIGURATION_BUCKET

# ⚠️ Both halves walk the SAME profile list, and it must be this one. A DynamoDB
# scan returns at most 1 MB per call, so a hand-rolled `table.scan()` without a
# LastEvaluatedKey loop reports a clean census on a stack whose later profiles it
# never looked at. `list_config_versions()` paginates (see its own comment), and
# it also excludes the `ConfigRevIndex#<profile>` index rows and the
# `Config#__active` pointer — neither of which carries config_format_version, so
# a raw scan both truncates sooner and prints findings for records that have no
# stamp to read.
for profile in manager.list_config_versions():
    name = profile["versionName"]

    # The head. Read the item and decompress it: get_raw_configuration()
    # relocates keys and restamps the version, so it reports the CURRENT format
    # for a head stored at an older one.
    key = {"Configuration": f"{CONFIG_TYPE_CONFIG}#{name}"}
    head = ConfigurationManager._decompress_item(manager.table.get_item(Key=key).get("Item") or {})
    if not is_at_or_after(head.get("config_format_version"), "0.9"):
        print("HEAD", name, head.get("config_format_version"))

    # Its revision bodies.
    for entry in manager.list_revisions(name):
        # The STORED body, deliberately. `get_revision()` cannot see the stamp
        # this audit is looking for: it converts on the way out, and for an
        # expired body it answers with the profile HEAD. `get_body()` is the raw
        # reader.
        body = manager.revisions.get_body(name, entry["revision"])
        if body is None:
            continue              # expired or pruned: nothing stored to convert
        if not is_at_or_after(body.get("config_format_version"), "0.9"):
            print("REVISION", name, entry["revision"], body.get("config_format_version"))
```

Check the method names against `configuration_manager.py` and `revisions.py`
before running it. ⚠️ **Each half has a reader that gives a confidently clean
answer, and neither is the obvious one.**

- For a **revision**, do not substitute `get_revision()` — which is what the
  revision-history panel and `config-download --config-revision` use, **because
  it answers an expired body with the profile head.** When a revision's stored
  object is gone and that revision is the profile's published and latest *and*
  provably still what the head holds, `_read_revision_body` falls back to
  `_published_body_from_head`. So a pruned pre-0.9 revision reports whatever its
  head carries now — and a head is the object most likely to have been converted
  already. `revisions.get_body()` returns `None` there, which is the answer this
  census needs, because nothing is stored to convert.

  `get_revision()` **also converts** what it returns, because
  `_read_revision_body` runs the chain on the way out (§2 names that site), so it
  reports the current format for a stored body of any age. That is a second and
  independent reason to use the raw reader here — but note the head fallback above
  is on its own a complete argument, and it holds on every release, so this check
  does not depend on which one you are reading.
- For a **head**, do not substitute `get_raw_configuration()`. "Raw" there means
  "no Pydantic defaults", not "the stored bytes": it runs the migration chain to
  relocate keys and restamps `config_format_version`, so it reports the current
  format for a head stored at an older one. Read the item and decompress it, as
  above.

Two further things about the head half. `config_format_version` lives **inside**
the gzipped `_compressed_config` blob rather than as a top-level attribute, so it
is neither projected by `list_config_versions` nor queryable and every record has
to be fetched and decompressed. And `_is_rollback_to_older_format` in
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
  told them, in its migration notes, to re-save or delete the profiles and
  revisions they still hold, re-saving being what stores a profile converted. If
  that notice has not shipped, stop here and ship it first.

## 2. Make an old body refuse, never degrade

Replace the conversion with a refusal rather than deleting it outright. A body
holding any `FLAT_KEYS` key must raise a `ValueError` that names the profile, the
revision and the remedy (restore it under the last release that reads it, then
save it again). Silently ignoring the keys is the failure this skill exists to
avoid. Keep `FLAT_KEYS` itself — the refusal needs it.

**Two sites convert a revision body, and the first is where the refusal has to be
raised:**

- **`ConfigurationManager._read_revision_body` → `_migrated_body`** converts every
  revision on the way out of the manager, whichever of its two sources it came
  from, and so covers *every* consumer: the pinned read, `restore_revision`,
  `get_revision` behind `idp-cli config-download --config-revision`, the Revision
  History and Compare views, and a test run's configuration capture. It calls
  `migrate_config()` directly. Two things to deal with here. ⚠️ **It catches a
  conversion failure and returns the body as stored**, so a refusal raised inside
  `migrate_config()` would be swallowed at this site and the flat body served
  anyway — the exact silent degradation this section exists to prevent. Remove
  that `except` branch as part of the change. And `migrate_config()` is handed
  neither the profile nor the revision number, so the message above cannot be
  built inside the migration at all: raise it here, where both are in scope, or
  thread them through.
- **`_load_revision_config` → `IDPConfig(**body)`** converts again through the
  model's before-validator. By the time it runs the body has already been through
  the site above, so this one is a no-op on that path and the chain's idempotence
  is what makes it harmless. It still matters for a body reaching `IDPConfig` from
  anywhere else.

## 3. What to remove or change

| Where | What |
|---|---|
| `config/migrations/v08_to_v09.py` | the conversion body; keep `FLAT_KEYS`, `TARGET_VERSION` and the refusal |
| `config/migrations/v07_to_v08.py` | only reachable through flat keys; remove with it, keeping the chain's version stamping |
| `config/configuration_manager.py` — `_read_revision_body` / `_migrated_body` | where a revision is converted for **every** consumer, and the only one holding the profile and revision number the §2 refusal must name. Raise there, and remove the swallow that returns an unconvertible body as stored |
| `config/configuration_manager.py` — `_read_revision_body` / `_migrated_body` | where a revision is converted for **every** consumer, and the only site holding the profile and revision number §2's refusal must name. Raise there, and remove the `except` that returns an unconvertible body as stored |
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
