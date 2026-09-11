# benchmarks/paper/

**The benchmark papers now live in `docs/benchmarking/` — that is the single source
of truth (published to the docs site).** This directory previously held duplicate
Markdown copies (`BENCHMARK_PAPER.md`, `VERSION_COMPARISON_*.md`) that drifted from the
`docs/` versions; they have been retired.

| What | Where |
|------|-------|
| "How the suite works" guide | `docs/benchmarking/index.md` |
| "Which config should I pick?" (evergreen, per release) | `docs/benchmarking/config-guidance.md` |
| Release-vs-release audit trail (one file per release) | `docs/benchmarking/releases/vX.Y.Z.md` + `releases/README.md` index |
| Raw scored data (unpublished) | `benchmarks/results/<release>/<suite>/summary.{json,csv}` + `meta.json` (one set per release — see `benchmarks/results/RETENTION.md`) |
| Published figures | `images/benchmark-*.png` (referenced by the docs) |

`figures/` here is **harness scratch** — `aggregate.py --figures` writes charts here;
copy the ones you cite into `images/benchmark-<release>-<name>.png` (the docs reference
`../images/...` / `../../../images/...`). `make benchmark-release` does this copy for you.
