---
title: "Migration Prompt: One Configuration Profile per Feature (not per release)"
---

<!--
Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0
-->

# Migration Prompt — One Configuration Profile per Feature

This file is a **ready-to-paste prompt** for running an AI coding agent (Claude
Code, etc.) inside a **separate feature/extension repository** (e.g. the private
marketplace-feature repo) so its features stop minting a new host **Configuration
Profile** for every release, and clean up after themselves on uninstall.

## Background (why this is needed)

The host renamed its named-configuration entity to **Configuration Profile** and
gave each profile a **revision history** (one immutable revision per save). A
profile is *not* a version: it is

- an access-control object — an admin must add it to every scoped user's
  `allowedConfigVersions`,
- a document-visibility partition,
- a confidence-curve bucket in Test Studio,
- a permanent row in the Configuration Profiles table.

The host's own `applyFeatureConfigPreset` used to write
`Config#<featureId>-v<version>` — a new profile per release. That was fixed in the
host (issue 697): it now writes `Config#<featureId>` and records each release as a
**revision** of that one profile, and `removeFeatureConfigPreset` deletes the
profile plus any legacy `<featureId>-v*` profiles and their revision history.

**Features that write the host's ConfigurationTable directly did not get that
fix.** On a real dev stack, one such pack left twelve orphaned profiles behind:

```
Config#claims-pack-v0.2.0 … v0.5.3   ← 12 profiles, one uninstalled pack
Config#claims-pack-authored-20260807T203120355518Z
```

They survived uninstall because the Delete handler deletes only
`Config#<featureId>-v<CURRENT_VERSION>` — every *earlier* release's profile is
orphaned by design. They are also written with `Managed: True`, and the host's Web
UI refuses to delete a managed profile (normally the owning stack would recreate
it), so an admin cannot clear them from the UI either.

## The prompt

Paste everything below into an agent running in the feature repository.

---

You are working in a repository of GenAI IDP **extensions/features** that install
into a GenAIIDP host stack. The host has changed how a feature's configuration is
stored. Update this repo to match, and fix an uninstall-cleanup bug.

### 1. Find every place a feature writes the host's ConfigurationTable

Search for direct writes to the host's configuration table and for the
per-release naming convention:

```bash
grep -rn 'Config#' --include='*.py' --include='*.yaml' --include='*.ts' --include='*.tsx' . | grep -v node_modules
grep -rn -- '-v{_FEATURE_VERSION}\|-v${\|_config_preset_version\|config_version_name' --include='*.py' --include='*.tsx' . | grep -v node_modules
grep -rn 'CONFIGURATION_TABLE\|"Managed"' --include='*.py' --include='*.yaml' . | grep -v node_modules
```

Expect hits in each feature's `ui-deployer/handler.py` and its `template.yaml`,
and possibly in a `feature-ui` that displays or preselects the profile name.

### 2. One profile per feature

For every feature that applies a config preset:

- The profile name becomes the **feature id alone** — `claims-pack`, not
  `claims-pack-v0.6.0`. Delete the version suffix from the name builder (e.g.
  `_config_preset_version()` / `config_version_name()`), and from any UI that
  reconstructs it client-side.
- Keep the feature version in the **description** and in the revision note, not
  in the name. That is where lineage belongs now.

### 3. Prefer the host's resolver over a direct `put_item`

If the feature writes the row itself, switch to invoking the host's
`applyFeatureConfigPreset` resolver (the ARN is exported as
`<MainStackName>-ApplyFeatureConfigPresetFunctionArn`; the payload shape is
`{info:{fieldName}, arguments:{input:{featureId, version, config, description}}}`).
The host resolver now:

- merges the preset over the host's `Config#default` and stores a **full**
  configuration, so the recorded revision has the same shape a later admin edit
  produces;
- **cuts a revision** per install/upgrade, with no-op suppression — an upgrade
  that does not change the preset records nothing;
- preserves `IsActive` and `CreatedAt` across re-applies;
- returns `configRevision` so the installer can log which revision it produced.

Only keep a direct write if the feature genuinely needs something the resolver
cannot express (e.g. inlining pipeline-hook ARNs into the config body, or
activating its own profile). If you keep it:

- write `Config#<featureId>` (no suffix);
- do **not** set `Managed: True` unless you want the Web UI to refuse deletion —
  and if you do set it, your Delete handler is then the *only* thing that can
  clean up, so it must work (§4);
- if you write the row raw and unflagged, the host merges it over defaults on
  first read; if you write a merged full config, set `_config_format: "full"`.

### 4. Uninstall must delete **all** of the feature's profiles

This is the actual bug. Today's Delete handler deletes only the profile for the
version being uninstalled, so every earlier release's profile is left behind
forever.

On `Delete`, remove:

- `Config#<featureId>` (the new, unsuffixed profile), **and**
- every legacy `Config#<featureId>-v*` row, **and**
- any other profile the feature created (e.g. `Config#<featureId>-authored-*`).

Prefer calling the host's `removeFeatureConfigPreset`. One Lambda serves both
fields, so use the SAME export as apply —
`<MainStackName>-ApplyFeatureConfigPresetFunctionArn` — with
`{info:{fieldName:"removeFeatureConfigPreset"}, arguments:{featureId}}`. It
already sweeps all of the above **and** deletes each profile's revision history
(the DynamoDB revision index plus the revision bodies in S3 under
`config_revisions/<profile>/`). A raw `delete_item` leaves those orphaned, and a
later profile of the same name then appears to inherit the deleted one's history.

If the feature's profile is **active**, do not simply skip it: a feature's config
carries that feature's pipeline hooks inline, so leaving it active after uninstall
points every subsequent document at deleted hook Lambdas. Activate
`Config#default` first, then delete. (The host's resolver does exactly this. The
one case it refuses is an active profile with no `default` to fall back to — no
active configuration fails *all* processing, which is worse.)

### 5. Follow the rename through everything that consumes the name

- Any `feature-ui` that shows or preselects the profile (e.g. "select this
  configuration in Test Studio") must show the unsuffixed name.
- Any template environment variable carrying the name (e.g. `CONFIG_VERSION_NAME`)
  becomes the feature id.
- Any recorded association — a test set that stores `configVersion`, an analytics
  row, a saved job — keeps whatever it recorded historically. Do not rewrite
  stored data; just stop writing new suffixed names.
- Grep for hardcoded `<featureId>-v` strings in tests and fixtures.

### 6. Tests

Add or update tests that assert:

- installing twice at different versions produces exactly **one** profile row;
- uninstall removes the unsuffixed profile **and** pre-existing legacy
  `<featureId>-v*` rows;
- an active feature profile is handed back to `default` before deletion;
- an unrelated feature's profiles and `Config#default` are untouched.

A unit test with a mocked table cannot see the failure that matters here, so
finish with a **live install → upgrade → uninstall** against a dev host stack and
check the ConfigurationTable before and after:

```bash
aws dynamodb scan --table-name <MainStack>-ConfigurationTable-XXXX \
  --filter-expression 'begins_with(Configuration, :p)' \
  --expression-attribute-values '{":p":{"S":"Config#<featureId>"}}' \
  --projection-expression 'Configuration,Managed,IsActive'
```

Expect exactly one row after install and upgrade, and none after uninstall.

### 7. Report back

Summarize: which features wrote profiles directly, which now use the host
resolver, what the Delete handler sweeps, and the before/after profile counts from
the live test.

---

## Cleaning up profiles already orphaned

Existing `<featureId>-v*` rows on a running stack are **not** removed by upgrading
the host — deleting stored configuration is not an upgrade's business. Two ways to
clear them:

1. Reinstall and uninstall the feature once, after the feature repo picks up §4.
   The sweep removes the legacy rows.
2. Delete them directly. The Web UI refuses managed profiles; the CLI does not:

   ```bash
   idp-cli config-list --stack-name <stack>          # find the orphans
   idp-cli config-delete --stack-name <stack> --config-profile claims-pack-v0.2.0
   ```

   You are warned when the target is stack-managed. Use `--force` in scripts. This
   also deletes the profile's revision history, so nothing is left in S3.
