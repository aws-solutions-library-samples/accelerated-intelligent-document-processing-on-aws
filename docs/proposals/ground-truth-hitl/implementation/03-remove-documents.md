# Implementation: Remove Documents from a Test Set

Membership editing (remove). Additive; complements the existing add-documents paths.

## Problem

Documents can be added to a test set but never removed. Bad samples found during
review are stuck in the set, and there is no way to curate membership down.

## Solution

`removeDocumentsFromTestSet(testSetId, fileNames)` deletes, for each named file,
the `{id}/input/<file>` object and the whole `{id}/baseline/<file>/` folder, then
recounts inputs and updates `fileCount`. Editing membership targets the mutable
working draft; a later publish cuts the next immutable version.

## Design

- Resolver (`test_set_resolver.remove_documents_from_test_set`): batch-deletes the
  input + baseline keys (paginated list, 1000-key delete batches), reuses
  `_validate_test_set_files` to recount, updates the metadata pointer's
  `fileCount` + `lastAddResult`.
- GraphQL: `removeDocumentsFromTestSet(testSetId, fileNames): TestSet`
  (Admin/Author).
- Dispatcher: aliased to the existing `getTestSets` map entry (same resolver;
  keeps the field-function-map SSM parameter under its ceiling).

## Non-goals

- No UI document-picker yet (TestSets has no per-set document list today) — the
  backend capability + generated op ship first; the picker is a follow-up.
- No merge (separate slice).

## Verify

- Removing a file deletes its input object and baseline folder, then updates
  fileCount to the recounted value.
- Missing test set raises. Touched unit suite green; ruff baseline unchanged.
