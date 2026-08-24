# Implementation: Test Set Source (provenance)

First, smallest slice of the Test Sets & Ground-Truth Datasets work. Additive and
backward-compatible: no behavior changes, no migrations.

## Problem

A test set gives no signal about where its documents came from. Uploaded document sets and
synthetically generated sets look identical in the list, so users can't tell which sets are
synthetic vs real, and the data model has nowhere to record it. This blocks the larger goal of
treating a test set as a first-class object with clear provenance.

## Solution

Add an optional **`source`** attribute to the test-set record and surface it end to end:

- **`uploaded`** — created from user-provided documents (pattern copy or zip upload).
- **`synthetic`** — created by the synthetic document generator.
- **`mixed`** — reserved for future sets that combine sources (not written yet).

`source` is optional everywhere. Existing records without it read as unknown and render as a dash,
so nothing breaks and no backfill is required.

## Design

One value, written once at creation, read straight through:

1. **Data model** — write `source` on the test-set DynamoDB item at creation.
   - `test_set_resolver`: uploaded paths (`add_test_set`, `add_test_set_from_upload`) write
     `'source': 'uploaded'`.
   - `synthesis/packet_io.upload_packet_to_test_set`: writes `'source': 'synthetic'`.
2. **API** — add `source: String` to the `TestSet` GraphQL type; `get_test_sets` returns
   `item.get('source')` in both the tracked-item path and the S3 auto-discovery path (auto-discovered
   folders are `uploaded`).
3. **UI** — add `source` to the `TestSet` type and the `GetTestSets` query, and render a **Source**
   column in the Test Sets table.

## Non-goals

- No new mutations, endpoints, or migrations.
- No versioning, review, or membership-editing changes.
- `mixed` is defined but not produced until membership-merge exists.

## Alignment check

- Minimizes code changes: one new optional field, no control-flow changes.
- Backward-compatible: absent `source` renders as a dash.
- Matches the requirements' label-source model (`Synthetic` / `Uploaded` on the set) and the
  Test Sets list mockup's Source column.

## Verify

- `get_test_sets` returns `source` for new and existing (null) records without error.
- New uploaded set → `uploaded`; new synthetic set → `synthetic`.
- UI shows the Source column; existing sets show a dash.
- Unit tests for the resolver mapping and the synthesis registration pass.
