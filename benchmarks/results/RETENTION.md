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
| `baseline.json` | The PREV release's `corefast` grid, which `aggregate.py --compare` defaults to. See below: it is **not** a byte-identical copy of the committed `v<PREV>/corefast/summary.json`, and must not be assumed to be one. |

### `baseline.json` is a separate artifact, not a copy

It is promoted by copying a `corefast/summary.json`, but the file it is copied from is
not necessarily the one committed under `v<PREV>/corefast/`, and it diverges afterwards.
Today's `baseline.json` is a v0.6.8 `corefast` grid scored on stack `IDPUpg067to068`,
while `v0.6.8/corefast/summary.json` is the same suite over the same three documents
scored on `IDPRel068` — 171 rows and 19 cells on both sides, matching on every
`(cell, doc, repeat)` key, and different data. It has also been `--augment`ed with the
#935 calibration statistic, which the release directory's copy has not been, so it is
now ~300 KB larger as well.

Two consequences. **Verify the promotion rather than assuming it** — compare `meta.stack`
and `meta.scored_at`, which identify the grid, instead of comparing file sizes or
checksums against the release directory. And **a metric backfilled into one is not in the
other**: `--augment` both, or `compare_cells` will report the metric as uncomparable on
one side.

## What is not kept

Suite slices run to answer a one-off question — cross-config grids (`config-*`),
repeated-measures hazard checks (`intconf`, `advverify`), and post-fix re-runs
(`fixed2-*`) — are **not** retained once their finding is written into the prose and
tables of a `docs/benchmarking/` page. The published page is the durable record.

This is a deliberate trade: those pages cite their supporting data, and the data is no
longer at the cited path. The committed **bytes** are not lost — recover any pruned set
with:

```bash
git show <SHA>:benchmarks/results/<dir>/summary.json
git checkout <SHA> -- benchmarks/results/<dir>/      # restore the whole set
```

The commit holding the full pre-pruning set is recorded in each affected doc page and
in the pruning commit message. Cite a commit, not a path, when referencing pruned data.

⚠️ **Git history archives what was committed; it does not archive what those files were
derived from.** A metric added after a grid was scored can only be backfilled by
re-reading the run's output from S3, and that window closes on its own:

- Three v0.6.x release stacks are gone outright, so nothing in their grids can gain a
  new metric.
- `IDPUpg068to069` still has its bucket and every object in it, and every object is
  **unreadable** — the stack's KMS key is pending deletion, so `GetObject` answers
  `KMS.KMSInvalidStateException`. An artifact's readable lifetime is the shorter of its
  bucket's and its key's, and neither is under this repository's control.

So the archive is only as complete as what each summary carried at commit time. The
practical rule: **when a new per-run metric lands, backfill it into every grid whose
stack is still readable, in the same change** — not only the grids the page being written
happens to report. A grid left unbackfilled while it was recoverable cannot be recovered
later, and the pruning policy above then removes the option entirely.

## Adding a release

`make benchmark-release VERSION=x.y.z PREV=a.b.c` writes the new set. Then:

1. Confirm the new data is at `results/v<VERSION>/corefast/`.
2. Promote: `cp results/v<VERSION>/corefast/summary.json results/baseline.json`.
3. Commit the new release dir + `baseline.json` + the audit-trail page and index row.
4. Do **not** add a sibling directory for a re-run or a variant. Either replace the
   set in place (if the first attempt was invalid) or write the finding into the doc
   page and let the data go.
