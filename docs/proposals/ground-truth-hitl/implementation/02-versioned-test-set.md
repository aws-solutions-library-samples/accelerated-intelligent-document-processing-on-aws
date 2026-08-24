# Implementation: Versioned Test-Set Object

Second slice. Makes a test set a first-class, versioned object. Additive and
backward-compatible: existing sets have no version items and read as unversioned.

## Problem

A test set is a single mutable record. There is no way to freeze a known-good
state, no history, and a test run cannot record which ground-truth it scored
against — so a later comparison silently mixes config changes with label
changes. Reproducible evaluation needs immutable, referenceable versions.

## Solution

Give each test set a mutable working **draft** (the existing `SK='metadata'`
item) plus zero or more immutable, numbered **versions**. One version can be the
**active reference** — the version scoring runs compare against.

- **Publish** freezes the current state into a new version and (by default) sets
  it as the active reference.
- **Test runs pin** the active reference at run time, symmetric to how they
  already pin the config version.

## Design

DynamoDB, single-table (mirrors the existing per-document version pattern):

- `PK='testset#<id>'`, `SK='metadata'` — mutable pointer. New optional fields:
  `latestVersion`, `publishedVersion`, `activeReference` (all absent ⇒ never
  published).
- `PK='testset#<id>'`, `SK='version#<zero-padded N>'`, `ItemType='testset_version'`
  — immutable snapshot: `versionNumber`, `label`, `notes`, `source`, `fileCount`,
  `configVersion`, `createdAt`, `createdBy`.

API (`test_set_resolver`):
- `publishTestSetVersion(input)` → writes the version item, advances the pointer,
  optionally sets `activeReference`. Returns the version.
- `getTestSetVersions(testSetId)` → lists versions ascending.
- `getTestSets` now also returns `latestVersion` + `activeReference`.

Dispatcher: both new fields are **aliased** to the existing `getTestSets`
field-function-map entry (same resolver Lambda, which branches on the original
`fieldName`). This keeps the field-function-map SSM parameter under its 8 KB
Advanced-tier ceiling — no new map entries.

Run pinning (`test_runner`): captures the test set's `activeReference` onto the
`testrun#` item as `TestSetVersion`; `test_results_resolver` surfaces it as
`TestRun.testSetVersion`.

UI (`TestSets.tsx`): a **Publish version** action (one COMPLETED set) and a
**Version** column showing the active reference (noting when the latest published
version is ahead).

## Non-goals

- No content pinning of S3 bytes yet (a version records metadata, not a manifest
  of object VersionIds). That is the next slice; see open question below.
- No diff/compare-versions UI, no membership-edit-creates-draft flow yet.

## Alignment check

- Minimizes change: additive fields + two new fields sharing an existing
  resolver via aliasing; no control-flow changes to existing paths.
- Backward-compatible: unversioned sets read as draft; runs pin None.
- Reuses proven patterns: config-version pinning (`test_runner`) and per-document
  versioning (data model shape).

## Verify

- Publish v1 on a set with no versions → v1 written, active reference = 1.
- Second publish → v2; `setAsActiveReference=false` leaves the reference at v1.
- `getTestSetVersions` returns versions ascending.
- A run against a published set records `TestSetVersion`; an unpublished set
  records none.
- Schema is valid (codegen parses it); ARN-partition check passes; touched unit
  suites green.

## Open question

Version content pinning: record a manifest of S3 object VersionIds per version
(reusing the `document_versions_resolver` pattern) vs copy-on-publish into a
`versions/<n>/` prefix — and how either coexists with the `TestSetBucket`
`DataRetentionInDays` noncurrent-version expiry.
