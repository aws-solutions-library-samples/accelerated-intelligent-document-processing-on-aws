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
| **v0.6.7** | v0.6.6 | **+0.167 scalar on 4 of 10 cells** (0.833→1.000 ×3, →0.944 ×1), recall 1.000→1.000 everywhere; each cell individually within spread but 4 cells agree, and the pattern reproduced across two runs | advanced cells **+25–40%**, all three same direction, **each within noise** (CV 0.28–0.39 at n=9); simple cells only +2–8%, which argues against #731 per-page confidence as the cause | **0 regressions, 0 failures in 172 runs.** LLM-OCR recall **0.564→1.000 on 9/9 runs, sd 0** — Likely cause of the accuracy gain is #740's `ocr.image.dpi` 150→300 (Textract drops faint characters below ~200 dpi) — **untested**. First attempt at this grid was **voided** by two CloudFormation updates landing mid-run; the harness now aborts if the stack moves. `baseline.json` deliberately **not** promoted | [v0.6.7.md](./v0.6.7.md) |
| **v0.6.6** | v0.6.5 | +0.033 mean (one cell 0.667→1.000, other 9 identical) | −5.8%/run, **all of it one cell**; the other 9 are +4.0% (within spread) | **0 regressions, 0 failures in 90 runs.** The integrated-confidence row-loss hazard open since v0.6.0 is **fixed** — 9/9 complete at −55% cost. One apparent recall drop root-caused to the **`bedrock_llm` OCR backend corrupting fixed-width identifiers** (not a code change), which also refutes v0.6.5's unconfirmed improvement on that cell. Baseline reused, not re-run — see the deviation note | [v0.6.6.md](./v0.6.6.md) |
| **v0.6.5 fixes** | v0.6.5 (pre-fix) | flat (1.000 = 1.000) | −9% total (inconclusive) | **Post-fix verification, not a release A/B.** recall 0.901→0.947, 0 regressions, 0 failures; integrated-confidence 400-row lists 1%→100% complete (800-row still truncates, now detectable); first attempt discarded for a config/model confound | [v0.6.5-fix-verification.md](./v0.6.5-fix-verification.md) |
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
`benchmarks/results/<release>/<suite>/summary.{json,csv}` + `meta.json` — one release
directory per release, per [`RETENTION.md`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/benchmarks/results/RETENTION.md).
