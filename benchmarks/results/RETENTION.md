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

### When one suite is run twice in a release: `<suite>__<override-slug>/`

`meta.suite` alone does **not** identify a measurement. One release legitimately runs
the same suite more than once with different `--set` overrides — `cost` at the
cross-version control model for the release A/B *and* at the shipped default for the
config-guidance paper, or `advsplitcost` once per mitigation under test. Two sibling
directories for one suite is the exact confusion this policy exists to prevent, so when
it happens name them with the override slug `make_configs.py` already uses for its
config files:

```
v0.6.7/cost/                              # committed default_cell (no --set)
v0.6.7/cost__extraction-model-sonnet5/    # --set extraction_model=sonnet5
```

The unsuffixed name always means "ran with the committed `default_cell`". Ad-hoc labels
(`cost-paper`, `advsplit-clsmodel`) are what this rule replaces: they read as editorial
rather than as a description of what varied.

`meta.overrides` records the same information inside the file from v0.6.7 onward, so a
directory can be identified even if it is renamed. It is **absent or `None` on every set
committed up to and including v0.6.7** — those runmaps were written before the field
existed, so for them the directory name is the only meta-level record. The measurement is
still recoverable from the data either way: every row carries the fully resolved axis set
in `rows[].resolved`, so `rows[0].resolved.extraction_model` answers "which model was this"
without trusting the directory name.

`[]` (empty list) is meaningfully different from `None`: it means the grid demonstrably ran
with no overrides, i.e. on the committed `default_cell`.

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
