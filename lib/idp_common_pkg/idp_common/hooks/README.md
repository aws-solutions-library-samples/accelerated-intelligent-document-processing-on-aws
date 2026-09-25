# Pipeline Hook Helpers

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

Helpers for authoring **pipeline-hook Lambdas** — the functions the unified
workflow invokes at its extension points, via the host's pipeline-hooks
dispatcher (`patterns/unified/src/pipeline_hooks_function`).

A hook may simply observe the pipeline, or hand a **modified `Document`** back
for the next step to consume. This module makes that round-trip safe in two
calls.

## Installation

`idp_common` is first-party and is **not published to PyPI**, while the name
`idp-common` on public PyPI is registered by an unrelated party. Install from a
path, run from the repository root:

```bash
pip install -e "lib/idp_common_pkg[core]"
```

No extra dependencies beyond core — the helpers only touch `Document` and S3.
Inside a Lambda source directory the same thing is expressed as a relative path in
`requirements.txt` (`../../lib/idp_common_pkg[core]`). See
[Installing First-Party Packages Safely](../../../../docs/dependency-confusion.md).

## Quick start

```python
from idp_common.hooks import load_hook_document, updated_document_result

def lambda_handler(event, context):
    document = load_hook_document(event)     # resolves compressed refs for you

    for section in document.sections:
        if section.classification == "Unknown":
            section.classification = classify_with_my_rules(section)

    return updated_document_result(document, rulesApplied=True)
```

Set the `WORKING_BUCKET` env var on the hook Lambda (from the host's
`<MainStackName>-WorkingBucketName` export) so compressed documents resolve.

## API

### `load_hook_document(event, working_bucket=None) -> Document`

Resolves the hook event's `document` payload to a full `Document`, handling both
shapes the dispatcher may send:

| Payload shape | Handling |
|---|---|
| Compressed reference (`{compressed: true, s3_uri, ...}`) — the common case | Fetched from the working bucket |
| Inline document dict | Parsed directly |

`working_bucket` defaults to the `WORKING_BUCKET` env var. Raises `ValueError`
if the event has no usable document, or if a compressed reference arrives with
no bucket to resolve it from — failing loudly beats returning a half-empty
document the hook would then hand back as an "update".

### `updated_document_result(document, working_bucket=None, size_threshold_kb=200, **extra) -> dict`

Builds the hook response that hands `document` to the next workflow step, under
the `updatedDocument` key the dispatcher reads. Documents under
`size_threshold_kb` are returned inline (the dispatcher spills them to S3
itself); larger ones are compressed here, keeping the hook's synchronous
response well under Lambda's 6 MB limit at any document size.

`**extra` is merged into the response — your own result fields, and/or
`halt=True` at the `preprocessing` point (the only point where `halt` is
actionable):

```python
return updated_document_result(document, halt=True, redactedKey=key)
```

### `UPDATED_DOCUMENT_KEY`

The response key the dispatcher reads a modified document from
(`"updatedDocument"`). Deliberately not `"document"`, so a read-only hook that
echoes its input cannot accidentally start mutating the pipeline.

## Why use these helpers

- **The event rarely contains the document itself.** It's usually a small
  compressed reference; `load_hook_document` hides the difference.
- **Building a `Document` from scratch loses data.** `metering`, `errors`,
  `hitl_metadata`, and `processing_issues` are silently dropped. Load → mutate →
  return preserves them.
- **The dispatcher only honors one key.** Returning any other shape leaves the
  pipeline's document untouched.

## Guardrails

The dispatcher **refuses** an update that violates any of these, keeping the
pre-hook document and recording the reason in
`$.HookResults.<point>.Payload.results[].documentUpdateRejected`. It never fails
the workflow over a bad update:

| Rule | Why |
|---|---|
| `id` / `input_key` / `input_bucket` / `output_bucket` are immutable | The tracking-table row and output S3 prefixes are keyed off them |
| `sections` in a compressed reference must be a list of section-id strings | The workflow's `ProcessSections` Map iterates it directly |
| `config_version` is preserved | It resolves hooks for the rest of the pipeline |
| A compressed ref's `s3_uri` must be under `compressed_documents/` in the working bucket | `Document.decompress()` discards the URI's bucket and reads the key against the consumer's own working bucket, so an unconstrained URI is a key-injection vector |
| Inline documents capped at 5 MB | Bounded by Lambda's 6 MB response limit |

The dispatcher also **back-fills** fields the state machine reads by JSONPath
that are *not* `Document` model fields, and so are dropped by the
load → mutate → return round-trip. Left dropped, each fails the execution
outright rather than degrading. Set one explicitly to override:

- `use_bda`, `bda_project_arn` — injected by the queue processor from the
  resolved config; drive the BDA/pipeline routing Choice.
- `num_pages`, `status`, `sections` — compressed-wrapper metadata read by
  `BDA_CheckExistingData` and `ProcessSections`' `ItemsPath`.
  `updated_document_result()` always emits these, so this only matters if you
  hand-roll a reference.

## Write idempotent mutations

The workflow **retries** a hook dispatch on transient Lambda faults, which
re-invokes your hook. A mutation that *sets* a value is safe; one that *appends*
can apply twice:

```python
# Unsafe under retry — may yield "W2-REVIEWED-REVIEWED"
section.classification += "-REVIEWED"

# Safe — idempotent
if not section.classification.endswith("-REVIEWED"):
    section.classification += "-REVIEWED"
```

## Hook-point scope

Where a mutation reaches depends on the point:

| Point | Scope | Propagates to |
|---|---|---|
| `preprocessing` | Whole document, pre-OCR | Everything downstream |
| `postOcr` | Whole document + page results | Classification onward |
| `postClassification` | Whole document | The section Map fan-out and everything after |
| `postExtraction` | **A single section** (runs inside the Map) | Assessment; only `sections[0]` + `metering` merge into the final document |
| `postRuleValidation` | Whole document | Summarization, evaluation, final output |
| `postSummarization` | Whole document | Evaluation and the final workflow output |
| `postprocessing` | Whole document, fully processed | Nothing downstream — but it IS the workflow output, so it reaches the tracking row, reporting, and the UI |

`postprocessing` runs last (after evaluation) and, like `preprocessing`, is a
standalone single-hook point. Two caveats there: `halt` is not actionable (the
dispatcher reports `haltIgnored`), and a mutation cannot set a terminal
`status` — a successful execution is forced to `COMPLETED`. It also fires while
a HITL review is pending, so branch on the document's `hitl_status` /
`hitl_triggered` fields (absent means no HITL) and keep the hook idempotent.

## See also

- [Feature Platform → Pipeline hooks](../../../../docs/feature-platform.md#pipeline-hooks) — full contract
- [Feature Platform developer guide](../../../../docs/feature-platform-developer-guide.md) — registering a hook
- [Document model](../README.md) — what you can mutate
