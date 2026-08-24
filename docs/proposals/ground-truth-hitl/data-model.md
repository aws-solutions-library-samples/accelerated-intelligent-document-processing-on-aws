# Test Sets — Data Model & Versioning (ERD)

**What this is:** how a test set, its immutable versions, its documents, and the
test runs that score against it relate on disk. It explains the versioning model
the UI surfaces (Version history panel + run pinning).

Everything lives in the existing **single DynamoDB tracking table** (partition
key `PK`, sort key `SK`) plus label/document bytes in the **Test Set S3 bucket**.
There is no new table.

---

## Entity-relationship diagram

```mermaid
erDiagram
    TEST_SET ||--o{ TEST_SET_VERSION : "freezes into"
    TEST_SET ||--o{ TEST_SET_DOC : "contains (draft membership)"
    TEST_SET ||--o| TEST_SET_VERSION : "activeReference points to"
    TEST_SET_VERSION ||--o{ TEST_RUN : "pinned by"
    TEST_SET ||--o{ TEST_RUN : "scored by"
    TEST_SET_DOC ||--|| S3_LABELS : "ground truth in"

    TEST_SET {
        string PK "testset#{id}"
        string SK "metadata (mutable pointer)"
        string ItemType "testset"
        string name
        string source "uploaded | synthetic | mixed"
        int    fileCount
        int    latestVersion "highest version cut"
        int    publishedVersion "last published"
        int    activeReference "version runs pin (nullable)"
        string boundConfigVersion
    }

    TEST_SET_VERSION {
        string PK "testset#{id}"
        string SK "version#{n:06d} (immutable)"
        string ItemType "testset_version"
        int    versionNumber
        string label
        string notes
        int    fileCount "snapshot at publish"
        string configVersion "config frozen against"
        string createdBy "publisher email"
        string createdAt
    }

    TEST_SET_DOC {
        string PK "doc#{objectKey}"
        string SK "none"
        string TestSetId "back-reference"
        string labelSource "synthetic | draft-machine | reviewed-human | unlabeled"
        float  minConfidence
        bool   HITLTriggered
        string HITLStatus "PendingReview when queued"
    }

    TEST_RUN {
        string PK "testrun#{id}"
        string SK "metadata"
        string ItemType "testrun"
        string TestSetId
        int    TestSetVersion "pinned activeReference"
        string ConfigVersion "pinned config"
    }

    S3_LABELS {
        string bucket "TestSetBucket"
        string inputKey "{testSetId}/input/{file}"
        string baselineKey "{testSetId}/baseline/{file}/sections/{sectionId}/result.json"
        string sourceMarker "{testSetId}/.source (synthetic)"
    }
```

---

## The versioning model in one paragraph

A test set is a **mutable draft** (`SK=metadata`) that you edit freely — add/remove
docs, generate labels, annotate. **Publishing** snapshots the current state into an
**immutable version** item (`SK=version#000001`, `version#000002`, …) that is never
rewritten. The draft's `metadata` row carries three pointers into that version
series:

| Pointer | Meaning |
|---|---|
| `latestVersion` | highest version number ever cut (monotonic) |
| `publishedVersion` | the most recently published version |
| `activeReference` | the version new test runs pin by default (the "trusted" one) |

This is deliberately **DVC-like**: the draft is your working tree, each publish is
an immutable commit, and `activeReference` is the tag evaluation follows.

## Why runs pin a version

When a test run starts, it copies `activeReference` into its own
`TestSetVersion` field — symmetric to how it already pins `ConfigVersion`. Two
runs are only apples-to-apples when **both** match. That's what stops a label
correction (new test-set version) from silently masquerading as a config/model
regression, and it's why the Executions view flags mismatched comparisons.

## Where labels actually live

The DynamoDB items are the **index**; the ground-truth bytes are S3 objects under
the test set's prefix:

- `input/` — the source documents.
- `baseline/{file}/sections/{sectionId}/result.json` — the reviewed labels. A
  correction in the Visual Document Editor writes here (in addition to the doc's
  own output), which is what turns a review into reusable ground truth.
- `.source` marker — present (body `synthetic`) for generated sets, so provenance
  survives even if the DynamoDB `source` field is lost.

> **Open question (unchanged):** versions currently share one S3 label prefix —
> the `version#` items snapshot *counts and metadata*, not the bytes. True
> byte-level immutability across versions needs a manifest, copy-on-publish, or a
> lifecycle change. See `requirements-v3.md` open questions.

## Backend status

The version items, the three pointers, `publishTestSetVersion`,
`getTestSetVersions`, and run-pinning are **built and verified live** on
`sr-testing`. The UI surfaces are: the Version column (live) and the new Version
history panel + publish-time accuracy estimate (prototype, this round).
