---
title: "Assessment Feature"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Assessment Feature

> **This guide has moved.** Extraction, confidence, and geometry documentation
> is now consolidated in **[Extraction & Confidence](extraction-and-confidence.md)**.

As of config format 0.6, confidence scoring is an **output of extraction** (configured
under `extraction.confidence.*`, with HITL under the top-level `hitl.*`). What
moved:

- **Confidence assessment** — the three modes (`off` / `separate` / `integrated`), the confidence model, in-shard vs standalone execution, and prompt placeholders.
- **Large-list batching** (`extraction.confidence.list_batch_size`) — the replacement for the now-retired granular assessment; see [Granular Assessment Retirement](migration-granular-retirement.md).
- **Confidence thresholds**, UI color coding, and output format (`explainability_info`).

See **[Extraction & Confidence](extraction-and-confidence.md)**.
