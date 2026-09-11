---
title: "Document Versions"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Document Versions

GenAIIDP retains a **version** of a document each time it is processed. When you
upload a new copy of a document that was already processed — or reprocess an
existing one — the prior results are not lost: each successful processing run is
recorded as an immutable version you can view, compare, download, and (as an
admin) delete.

This directly supports enterprise use cases like reprocessing a mortgage or
lending package after a few days and keeping every prior extraction result
linked to the same logical document.

> **Terminology.** A document *version* is a snapshot of one processing run. A
> Configuration Profile *revision* is a snapshot of a configuration. Both are
> immutable and numbered, and they are deliberately different words — see the
> [terminology table](configuration-profiles.md#terminology-which-word-means-what).

## What is a "version"?

A version is one **successful processing run** of a document. The document's
identity is its S3 object key: re-uploading `loan-12345/package.pdf` (or
reprocessing it) creates version 2 of the same logical document, version 3, and
so on.

Each version records:

- **When** it completed and which **configuration version** processed it
- Its section/page structure and metering (cost) snapshot
- Per-section quality data — the low-confidence field alerts and any processing
  issues detected for that run — so a historical view shows the same
  **Low Confidence Fields** count and section **Status** the document showed
  when it completed
- A **manifest** that pins the exact S3 object version of every output file the
  run produced

## How it works

Output artifacts are written to deterministic keys under `<object-key>/` in the
Output bucket (for example `<object-key>/sections/1/result.json`), so a later
run overwrites them in place. All IDP buckets have **S3 versioning enabled**, so
the bytes of every prior run are retained as noncurrent object versions.

On its own, S3 versioning cannot tell you which object versions belonged to
which run — a run may produce a different set of sections than the last one. To
close that gap, when a workflow completes successfully the solution:

1. Snapshots the current `VersionId` of every output object into a per-run
   **manifest** at `<object-key>/runs/<run-id>/manifest.json`.
2. Writes an immutable **run record** to the tracking table
   (`PK = doc#<object-key>`, `SK = run#<run-id>`).

Because the output bucket is versioned, fetching an object by its pinned
`VersionId` returns that run's exact bytes **even after later runs overwrite the
object or a reprocess deletes it**. The reserved `runs/` prefix is preserved
across reprocessing so version history survives.

```
s3://<output-bucket>/loan-12345/package.pdf/
├── sections/1/result.json          ← current run's output (overwritten each run)
├── pages/1/rawText.json
└── runs/
    ├── 20250701T090000Z-exec-a/manifest.json   ← version 1 (pins prior VersionIds)
    └── 20250707T141530Z-exec-b/manifest.json   ← version 2 (current)
```

## Retention

Document versions are retained for the stack's **`DataRetentionInDays`** period.
Noncurrent S3 object versions (the byte-store for version history) are expired on
the same schedule via bucket lifecycle rules, so version history does not grow
unbounded. Deleting a version (below) reclaims its storage immediately.

> **Always on.** Versioning uses infrastructure the solution already had (S3
> bucket versioning) plus one small tracking-table record per run, so it is
> enabled by default. To disable run-record creation, set the WorkflowTracker
> Lambda's `DOCUMENT_VERSIONING_ENABLED` environment variable to `false`.

## Viewing and managing versions

### Web UI

The document detail page includes a **Version History** panel listing every
retained version newest-first, with the current version badged. From there you
can:

- **View a past version** — choose *View* on any row to render that run's
  snapshot on the page (read-only): its sections, page text/images, extraction
  results, and summary/evaluation reports are all fetched from the run's pinned
  S3 object versions (page images via version-pinned presigned URLs), so you see
  exactly what that run produced even after later runs overwrote the outputs. A
  banner indicates you're viewing a previous version; *Return to current
  version* switches back. Editing is disabled while viewing history.
- **Compare any two versions** — select two rows and choose *Compare selected*
  to see a section-by-section, field-level diff of their extraction results
  (values are read from each run's pinned S3 versions, so the comparison is
  exact).
- **Delete a version** (Admin only) — permanently removes that run's pinned
  output object versions and its record. The current version cannot be deleted.
  Deletion is reference-counted: any output object version also pinned by
  another retained version is kept, so deleting one version never corrupts
  another.

### CLI

```bash
# List versions for a document (newest first)
idp-cli list-versions --stack-name my-stack --document-id loan-12345/package.pdf

# Download the exact output bytes of a specific version
idp-cli download-results --stack-name my-stack \
    --document-id loan-12345/package.pdf \
    --run-id 20250707T141530Z-exec-b \
    --output-dir ./results/
```

### API

The GraphQL/REST API exposes:

| Field | Type | Access |
|-------|------|--------|
| `listDocumentVersions(objectKey)` | query | any authenticated user |
| `getDocumentVersion(objectKey, runId)` | query | any authenticated user |
| `compareDocumentVersions(objectKey, runIdA, runIdB)` | query | any authenticated user |
| `deleteDocumentVersion(objectKey, runId)` | mutation | **Admin only** |

## Caveats

- **HITL edits after completion.** A version reflects the pipeline output at the
  moment the workflow completed. If a reviewer edits extraction results via
  Human-in-the-Loop review *after* completion, those edits modify the current
  output objects; the version snapshot captured at completion is unchanged.
- **Documents processed before this feature.** For documents last processed by a
  release without version history, the prior object *bytes* may still exist as
  noncurrent S3 versions, but there are no run manifests, so those old runs
  cannot be enumerated as versions. Version history begins accruing from the
  first run after upgrade.
- **Quality data on pre-existing versions.** The per-section low-confidence
  alerts and processing issues are written into the run record at completion.
  Versions recorded before the release that added them have no stored quality
  data, so they display a **Low Confidence Fields** count of 0 and an empty
  section **Status**. Versions recorded after upgrading show the real values.
- **Logical grouping is by object key.** Two different S3 keys are two different
  logical documents. To version a packet, upload it under the same key each
  time.

## Related

- [Post-Processing Lambda Hook](post-processing-lambda-hook.md) — for archiving
  results into a customer-owned system on every completion.
- [Configuration Profiles](configuration-profiles.md) — revisions of a
  *configuration profile*, distinct from versions of a *document*.
