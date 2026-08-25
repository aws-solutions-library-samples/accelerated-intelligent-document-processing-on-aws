---
title: "Release Benchmark Audit Trail"
---

# Release Benchmark Audit Trail

One entry per release, comparing each `develop` prerelease against the **previous
publicly published release** on the `corefast` grid (10 config cells × 3 ≤100-row docs,
extraction model held at a shared control both versions can run). Each entry is written
once and never overwritten — this table is the durable history.

Generate a new entry with **`make benchmark-release VERSION=<new> PREV=<published>`**
(see the [Benchmarking Guide](../index.md#maintaining-the-release-audit-trail--one-command-per-release)).

| Release | vs (published) | Accuracy | Cost | Notable | Report |
|---------|----------------|----------|------|---------|--------|
| **v0.6.5** | v0.6.4 | flat (0.900 = 0.900, all 30 pairs equal) | +1.4% across the 24 runs in the 8 stable cells; +8.5% total, all of it in 2 non-deterministic cells | **0 regressions.** Measured recall 0.931→0.987 but a 4× repeat run shows the integrated-confidence cell is **bimodal (2/4 truncate)** — the hazard is NOT fixed and the "improvement" is noise; alert rate 10.7%→6.4%; confidence pass now attaches page images (per-page cost note) | [v0.6.5.md](./v0.6.5.md) |
| **v0.6.0** | v0.5.16 | flat (0.917 = 0.917) | **−32.5%** total (advanced −44–55%) | cacheRead −95%; one integrated-confidence completeness regression on long lists; v0.6 fixes v0.5.16 advanced-assessment timeouts | [v0.6.0.md](./v0.6.0.md) |

<!-- APPEND NEW ROWS ABOVE THIS LINE (newest first). Columns:
     Release | vs (published) | Accuracy | Cost | Notable one-liner | link -->

## What each entry contains

- **TL;DR** — safe-to-upgrade verdict + the headline accuracy/cost/latency deltas.
- **Methodology notes** — the control model, disabled steps, doc set, and any
  cross-version config-compatibility handling (so the A/B is apples-to-apples).
- **Per-cell table** — cost/recall/latency per config cell, both versions.
- **Regressions & improvements** — anything past the `aggregate.py --compare` thresholds,
  with root cause verified from S3 output + metering.
- **Reproduce** — the exact commands, plus honesty caveats (n, pricing estimate date).

Raw scored data for each side lives (unpublished) under
`benchmarks/results/<release>/summary.{json,csv}` + `meta.json`.
