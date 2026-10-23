# Removing the pre-0.9 flat classification keys

Use this when a release is ready to stop reading the **flat** classification keys
that config format 0.9 replaced (`classification.model`, `.task_prompt`,
`.granularity`, `.labeling`, `.invalidClassFallback`, … — the full list is
`FLAT_KEYS` in `lib/idp_common_pkg/idp_common/config/migrations/v08_to_v09.py`).
Since 0.9 every one of them is converted on read; this skill removes that
conversion. Do not start it because the code looks redundant: the conversion is
what lets an old **pinned revision** load.

## 1. First check: no retained revision predates 0.9

A profile head is converted and written back the first time 0.9 code resolves it,
so heads stop carrying flat keys on their own. A **revision body is immutable**: it
is converted in memory on every read and never rewritten. Removing the conversion
makes every pre-0.9 revision either fail validation or — worse — load with its
classification settings silently replaced by defaults, so a test run or reprocess
pinned to it would run under a configuration nobody chose.

Two sites convert a revision body, and the first is where the refusal §2 asks for
will have to be raised:

- **`ConfigurationManager._read_revision_body` → `_migrated_body`** converts every
  revision on the way out of the manager, whichever of its two sources it came
  from, and so covers *every* consumer: the pinned read, `restore_revision`,
  `get_revision` behind `idp-cli config-download --config-revision`, the Revision
  History and Compare views, and a test run's configuration capture. It calls
  `migrate_config()` directly. ⚠️ **It also swallows a conversion failure** and
  returns the body as stored, which a refusal must not be left behind — and
  `migrate_config()` is handed neither the profile nor the revision number, so the
  message §2 requires cannot be built inside the migration. Raise it here, where
  both are in scope, or thread them through.
- **`_load_revision_config` → `IDPConfig(**body)`** converts again through the
  model's before-validator. By the time it runs the body has already been through
  the site above, so this one is a no-op on that path and the chain's idempotence
  is what makes it harmless. It still matters for a body reaching `IDPConfig` from
  anywhere else.

So, before anything else, establish that no deployment still retains a pre-0.9
revision:

- Revisions expire under the Configuration bucket's `DataRetentionInDays`
  lifecycle rule. A revision cut before the 0.9 upgrade is gone only once that
  many days have passed **since the upgrade** on that stack, and only if
  retention is finite on that stack.
- For a stack you can reach, list the bodies and their stamps (read-only):

  ```python
  from idp_common.config.configuration_manager import ConfigurationManager
  from idp_common.config.migrations._version import is_at_or_after

  manager = ConfigurationManager()          # needs CONFIGURATION_TABLE_NAME and CONFIGURATION_BUCKET
  for profile in manager.list_config_versions():
      name = profile["versionName"]
      for entry in manager.list_revisions(name):
          # The STORED body, deliberately: `get_revision()` converts on read, so
          # it answers 0.9 for every revision and cannot see the stamp this audit
          # is looking for. `get_body()` is the raw reader.
          body = manager.revisions.get_body(name, entry["revision"])
          if body is None:
              continue                      # expired or pruned: nothing stored to convert
          if not is_at_or_after(body.get("config_format_version"), "0.9"):
              print(name, entry["revision"], body.get("config_format_version"))
  ```

  Check the method names against `configuration_manager.py` and `revisions.py`
  before running it. ⚠️ Do **not** substitute `get_revision()` here, which is what
  the revision-history panel and `config-download --config-revision` use: it
  converts the body to the current format on the way out, so the condition above is
  never true and the audit reports a clean stack whatever it holds.
- Deployments you cannot reach are the binding constraint. This repository ships
  to customers, so the release that removes the conversion must come after one
  that told them, in its migration notes, to re-save or delete pre-0.9 revisions
  they still need. If that notice has not shipped, stop here and ship it first.

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
| `config/configuration_manager.py` — `_read_revision_body` / `_migrated_body` | where a revision is converted for **every** consumer, and the only one holding the profile and revision number the §2 refusal must name. Raise there, and remove the swallow that returns an unconvertible body as stored |
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
