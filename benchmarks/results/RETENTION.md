# Benchmark results — retention policy

**One complete set of results per release.** This directory is a curated index, not a
run log. It had grown to 16 flat sibling directories with ad-hoc labels
(`v0.6.5-fixed2-config-core`, `v0.6.6-advverify-post668`, …) sitting next to real
release directories, with nothing indicating which set was canonical for a release.

## Layout

```
results/
  baseline.json          # the PREV-release summary the regression gate diffs against
  RETENTION.md           # this file
  v<RELEASE>/
    <suite>/             # summary.json, summary.csv, cell_stats.csv, meta.json
```

The release directory is the version the results describe; the suite subdirectory is
the `meta.suite` value the harness recorded (`corefast`, `coresynth`, `scaling`, …).
Never put scored files directly in `v<RELEASE>/` — always under a suite subdirectory,
so a release that later gains a second suite does not need renaming.

`results/run-*/` (raw per-run runmaps) is gitignored and is never committed.

## What is kept

| Keep | Rule |
|------|------|
| `v<RELEASE>/corefast/` | The release-vs-release A/B grid backing `docs/benchmarking/releases/v<RELEASE>.md`. **One per release**, never overwritten. |
| `baseline.json` | Promoted copy of the PREV release's `corefast/summary.json`. Byte-identical to it by construction — `aggregate.py --compare` defaults to this path. |

## What is not kept

Suite slices run to answer a one-off question — cross-config grids (`config-*`),
repeated-measures hazard checks (`intconf`, `advverify`), and post-fix re-runs
(`fixed2-*`) — are **not** retained once their finding is written into the prose and
tables of a `docs/benchmarking/` page. The published page is the durable record.

This is a deliberate trade: those pages cite their supporting data, and the data is no
longer at the cited path. **It is not lost** — these files were committed, so git
history is the archive. Recover any pruned set with:

```bash
git show <SHA>:benchmarks/results/<dir>/summary.json
git checkout <SHA> -- benchmarks/results/<dir>/      # restore the whole set
```

The commit holding the full pre-pruning set is recorded in each affected doc page and
in the pruning commit message. Cite a commit, not a path, when referencing pruned data.

## Adding a release

`make benchmark-release VERSION=x.y.z PREV=a.b.c` writes the new set. Then:

1. Confirm the new data is at `results/v<VERSION>/corefast/`.
2. Promote: `cp results/v<VERSION>/corefast/summary.json results/baseline.json`.
3. Commit the new release dir + `baseline.json` + the audit-trail page and index row.
4. Do **not** add a sibling directory for a re-run or a variant. Either replace the
   set in place (if the first attempt was invalid) or write the finding into the doc
   page and let the data go.
