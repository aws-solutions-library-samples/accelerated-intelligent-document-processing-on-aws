---
title: "Granular Assessment Retirement"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Granular Assessment Retirement

This note documents the retirement of **granular assessment** and what it means
for your configuration. The short version: **no customer action is required** —
existing configs continue to validate and run unchanged.

## What changed

**Granular assessment is retired and its service has been deleted.** Granular
assessment was a separate service that fanned per-attribute / per-list-batch
confidence tasks across a thread pool with DynamoDB caching. It was the
historical mechanism for scoring large lists on the non-agentic path, but:

- its `<<CACHEPOINT>>` caching was ineffective (a concurrent cacheWrite storm), and
- it cost roughly **4–5× more** than a consolidated pass for equal accuracy.

In A/B testing on a 120-row bank statement, turning granular **off** yielded
**−78% Bedrock cost** at equal accuracy, with **full per-cell coverage plus
geometry** — whereas granular actually produced **0% geometry**.

Its capability is now fully covered by **standalone large-list batching** (see
below).

## `granular.*` config keys are now a no-op

Any `extraction.confidence.granular.*` (or legacy `assessment.granular.*`) keys
in an existing configuration **continue to validate but are ignored**. The
confidence config model uses `extra="ignore"`, so stale granular keys neither
raise nor take effect.

> **No config edit is required.** You do not need to remove `granular.*` keys.
> They are silently dropped at runtime.

## The single knob for large-list assessment: `list_batch_size`

Large lists (bank statements with hundreds of transactions, line-item tables,
brokerage holdings) are now assessed by **standalone large-list batching**. The
standalone Assessment step slices the largest list field into
`extraction.confidence.list_batch_size` chunks (default **25**), assesses each
chunk sequentially with the shared scalars/context, and reconciles the results so
**every** list cell gets its own confidence and bounding box — automatically,
with no additional configuration.

```yaml
extraction:
  confidence:
    enabled: true
    mode: separate            # off | separate (default) | integrated
    list_batch_size: 25       # rows per assessment batch for large lists
```

- **Lower** `list_batch_size` if a model still struggles to enumerate a full chunk.
  It is a ceiling on a derived size, so raising it has no effect.
- **Raise** it to reduce the number of inference calls.

See [Large-list batching](extraction-and-confidence.md#large-list-batching-list_batch_size)
in the Extraction & Confidence guide.

## Mode guidance for large documents

- **Advanced (agentic) extraction is recommended for large documents and big tables.** It shards both extraction and confidence assessment, and produced the **best-calibrated** confidence in A/B testing. For 100+ page documents prefer `runtime: step_functions` and raise `max_concurrent_batches`.
- **Simple + `separate` confidence remains fully viable for large lists** via `list_batch_size` batching — full per-cell confidence and geometry with no extra configuration. This is the direct replacement for granular assessment.
- **Rule of thumb:** a document that is large only because it contains a long *list* is well served by Simple + separate + batching; a document with a **very large single section** is better served by **Advanced sharding**.

See [Large-Document Guidance](extraction-and-confidence.md#8-large-document-guidance).

## Configuration format is unchanged (stays v0.6)

This retirement does **not** introduce a new config version. Configurations
remain **v0.6**: confidence lives under `extraction.confidence.*`, geometry under
`extraction.geometry.*`, and HITL under the top-level `hitl.*`. Pre-v0.6 configs
are still migrated automatically on read.

## Related Documentation

- [v0.5 → v0.6 Migration](migration-v05-to-v06.md) — the `assessment` block merge into `extraction.confidence` / `extraction.geometry` / `hitl`, with the full old→new setting map
- [Extraction & Confidence](extraction-and-confidence.md) — consolidated extraction, confidence, and geometry guide
- [Configuration Guide](configuration.md) — configuration schema details
- [Human Review](human-review.md) — HITL workflow
