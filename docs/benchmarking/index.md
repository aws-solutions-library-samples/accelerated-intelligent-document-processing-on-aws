---
title: "Benchmarking Guide"
---

# Benchmarking Guide — How the IDP Benchmark Suite Works

The benchmark suite (`benchmarks/` in the repo) is a repeatable, scientific harness
that runs the accelerator end-to-end across a controlled matrix of **document
types/sizes** and **configuration options**, then quantifies every result on seven
dimensions. It exists to serve two audiences:

- **Users** — an empirical, transparent basis for choosing configuration options
  (the published results are in the [Configuration Guidance](./config-guidance.md) paper).
- **Maintainers** — a regression gate: re-run the same matrix on any change and diff
  against a committed baseline to catch accuracy/cost/robustness regressions.

> To *run* it, see the `run-benchmarks` skill and `benchmarks/matrices/METHODOLOGY.md`.
> This page explains how it is designed and what the numbers mean.

## The three benchmark documents

This section of the docs holds three distinct, non-overlapping documents. Keep them
separate — they answer different questions and are regenerated on different cadences:

| Document | Question it answers | Cadence |
|----------|--------------------|---------|
| **This guide** (`index.md`) | *How does the suite work and what do the numbers mean?* | Evergreen; edit when the harness changes. |
| [Configuration Guidance](./config-guidance.md) | *Which config (OCR / mode / assessment / model) should I pick?* — cross-config at one release | Refreshed per release. |
| [Classification Confidence](./classification-confidence.md) | *When classification reports a confidence, is it worth acting on — and does that depend on the classifier?* | Re-run when the classifier default or the confidence mode changes. |
| [Release Audit Trail](./releases/) | *Is upgrading from the last published release safe / cheaper / faster?* — release-vs-release | **One new entry per release** (never overwritten). |

The release audit trail is the durable history: `docs/benchmarking/releases/vX.Y.Z.md`
compares each `develop` prerelease to the previous **published** release, and the
[index](./releases/) table links them all. Raw data lives (unpublished) under
`benchmarks/results/<release>/<suite>/` — **one complete set per release**, per
[`benchmarks/results/RETENTION.md`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/benchmarks/results/RETENTION.md).

## What it measures (seven dimensions)

| Dimension | Definition |
|-----------|------------|
| **Success / failure** | Did the document complete? Failure phase + Bedrock error class are captured (e.g. input-overflow `ValidationException`). |
| **Completeness** | For list-bearing docs: fraction of ground-truth rows recovered, plus the *truncation point* (longest contiguous prefix) and duplicate/gap counts. |
| **Accuracy** | Exact field/cell match against ground truth (synthetic) or the stack's evaluation `weighted_overall_score` (reference docs). |
| **Confidence calibration** | Mean confidence, %-below-threshold (alert rate), and — where a match flag exists — separation between confidence on correct vs incorrect values. Over-confidence on wrong values is a regression even when accuracy holds. |
| **Latency** | Wall-clock per document (and per phase where available). |
| **Token use** | Per-phase, per-model, per-unit (input / output / cache-read / cache-write) from the document's metering. |
| **Cost** | Metering priced with `config_library/pricing.yaml`, broken out by phase (OCR / Extraction / Assessment / Summarization / Lambda). |

Three of these have more than one instrument, and picking the wrong one has produced
wrong conclusions here before:

| Metric | Reads | Use it for |
|---|---|---|
| `scalar_accuracy` | The text the document **renders** (`"$685.50"`) | Comparability with historical baselines. Do **not** use it to judge value normalization — a correctly-typed `685.5` scores as a miss. |
| `typed_accuracy` | The schema-**typed** target (`685.5`) | Whether values came back correctly typed. Populated only for truth files declaring `fields_typed`. |
| `cell_accuracy` | Per-cell typed match on list rows, matched by `SEQ` tag | Value fidelity *inside* lists. Completeness answers "did the row come back"; this answers "with the right value". |
| `sections_correct` | Section count vs `expected_sections`, as 1.0/0.0 | Boundary detection. Nothing else can see an over-split: a document split into 3 sections still reports `completeness_recall` 1.0 and status `COMPLETED`. The mean over repeats **is** the pass rate. |

**Did the feature under test actually engage?** `analyze.py` also reads the audit block
each section records about itself, because a delta of zero is uninterpretable without
it — no effect, or no opportunity? `forced_tool_honored_rate` (a forced tool the model
answered in prose has measured nothing), `coercions` / `coercion_refusals`, and
`validation_valid_rate` are reported per run and rolled up per cell.

Scoring is **resolver-free** — it reads S3 output and DynamoDB metering directly, so it
works on any stack version (useful for cross-release comparisons).

## The two corpora (ground-truth strategy)

**A. Synthetic, exact ground truth.** Generators (`benchmarks/corpus/generators/`)
produce documents whose field values are known and whose every list row carries a
unique `SEQnnnnn` tag. This makes completeness and accuracy measurable *exactly*, and
lets us treat document size (rows/pages), row width (token density), list count, text
length, and OCR noise as **controlled variables**. Generators are deterministic (no
RNG) so a regenerated corpus is byte-stable and results are reproducible.

**B. Reference, real labeled sets.** Existing curated test sets (RealKIE-FCC,
OCR-Benchmark, bank-statement samples) provide real-world messiness the synthetic set
can't emulate, scored against the stack's evaluation baselines.

## The two matrices

**Configuration matrix** (`benchmarks/matrices/config_matrix.yaml`). Full-factorial
over every knob is combinatorially huge and largely redundant (accuracy is empirically
flat across several axes), so the suite tests:
- a curated set of **core cells** — the decision-relevant combinations of OCR backend ×
  extraction mode × assessment mode; and
- **one-axis sweeps** — vary a single knob (geometry, escalation, extraction model,
  confidence model, reasoning effort) with everything else held at a fixed default, so
  each knob's marginal effect is isolated (scientific control).

Every generated cell is validated against the real config loader
(`merge_config_with_defaults(..., validate=True)`) before it can run.

**Document matrix** (`benchmarks/matrices/doc_matrix.yaml`). Names the synthetic docs to
generate (size series, width/list/noise variants, a non-list key/value form) and the
reference test sets to reference, with each doc's ground-truth pointer and config class.

## How a run executes

1. **Generate the corpus** — `gen_corpus.py` writes PDFs + `<id>.truth.json`.
2. **Expand configs** — `make_configs.py` merges each matrix cell onto a base config per
   document class, validates, and writes full v0.6 config variants.
3. **Run the matrix** — `run_matrix.py` registers each synthetic doc as a test set,
   uploads the config variants as `Config#bench-*` versions (it **never** mutates
   `Config#default`), launches each (cell × doc) via the stack test runner, and polls the
   tracking table to completion. `--estimate` prints projected doc-count/cost/time first.
   ⚠️ It can only launch **synthetic** documents. A suite naming a reference corpus
   (`realkie`, `ocr_bench` — both are in `core_docs`) does not measure it: those are
   test *sets* on the stack, not local PDFs, and there is no launch path for them yet
   ([#766](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/766)).
   The launcher says so and records `docs_unlaunchable` in the runmap and `meta.json`,
   so check that field before reading a suite's result as covering its whole document
   list; run reference corpora through Test Studio meanwhile.
4. **Score & aggregate** — `aggregate.py` scores every run on the seven dimensions, rolls
   them into `results/<release>/<suite>/summary.{json,csv}` with a `meta.json` (commit,
   stack, pricing hash, date), diffs against `results/baseline.json` to flag regressions,
   and emits figures.

## Suites (cost-tiered)

| Suite | Scope | Use |
|-------|-------|-----|
| `smoke` | 2 cells × 2 tiny docs | Per-PR gate (minutes) |
| `corefast` | 10 decision cells × 3 docs (≤100 rows) × **3 repeats** (90 runs/side) | **Release-vs-release A/B** — the grid that completes on *both* the previous published release and the new one (see notes) |
| `coresynth` | 10 decision cells × 7 synthetic docs (**70 runs**) | **Standard single-release run** — the cross-config grid the Configuration Guidance paper reports |
| `core` | `coresynth` + the two 20-document reference corpora (**470 runs**) | Adds real-world labeled accuracy; ~7× the cost of `coresynth`, so opt in deliberately |
| `scaling` | simple vs advanced across the size series | The completeness-cliff study |
| `cost` | cost-decision cells × 1 mid doc, repeats≥5 | Cost-difference detection (variance-aware) |
| `intconf` | integrated + separate confidence × 1 list doc, repeats=4 | Re-verifies the integrated-confidence row-loss hazard; the one finding a single-sample grid cannot settle |
| `advverify` | advanced × integrated + separate × 1 list doc, repeats=4 | Re-verifies the **tool-decline** list-loss hazard (an agent that declines the table tool returning the whole list as `null`). Run with `--set extraction_model=sonnet5` |
| `full` | core + all one-axis sweeps | The deep study for the paper (expensive) |

**Feature A/B suites.** Each pairs two cells that differ on exactly **one** config knob,
on **one** deployed stack with identical code — which attributes a delta to the feature
rather than to anything else that shipped in the same release. All run `repeats: 3` or
more, because each is judged on a *rate* and a single sample cannot resolve one.

| Suite | Question it answers | Judged on |
|-------|---------------------|-----------|
| `enforcement` | Does coercion + validation change what is extracted? | `typed_accuracy`, `cell_accuracy`, `coercions` (a zero-coercion run is not evidence about coercion) |
| `enforcecost` | What does `fail_action: escalate` cost? | `cost` — the only enforcement arm that spends money per failure; kept separate so it is never run by accident |
| `forcing` | Does forcing the schema as a tool improve extraction? | `cell_accuracy`, `completeness_recall`, and `forced_tool_honored_rate` — **not** "did it parse" |
| `restatement` | Does dropping the duplicated schema copy cost completeness? | `completeness_recall`. The gate is completeness, not tokens: a saving that loses list rows is a loss |
| `boundary` | Does boundary detection split correctly in **both** directions? | `sections_correct` on a 3-page single document (must yield 1) *and* a two-document file (must yield 2). A fix that only stops over-splitting regresses the second |
| `boundaryctl` | Control for `boundary`: what did the **pre-fix** prompt score? | `sections_correct`. Runs the frozen pre-#653 prompt from `benchmarks/matrices/prompts/`, so the claim is a measured delta rather than a single post-fix number |
| `splitcost` | What did making `llm_determined` do real work cost? | `cost`, `repeats: 5` |

`kv_form` belongs to a different document class, so suites naming it need a second
invocation with `--class kv_form` (configs are per class; the harness prints which docs
it skipped and why).

> **Picking the extraction model.** The committed `default_cell` holds `extraction_model` at
> a **cross-version control** so the release A/B runs on a model every compared release can
> invoke. A single-release study should instead use the product default —
> `make_configs.py --set extraction_model=sonnet5`. Mixing the two is how a
> model-dependent behaviour can look like a code change.

> **Why `corefast` for release comparisons.** Advanced (agentic) mode + granular
> assessment on ≥400-row list documents can exceed the 900 s assessment-Lambda timeout
> on older releases (e.g. v0.5.16), which then retry for hours before failing. A grid
> that must complete on **both** the old and new release therefore uses the ≤100-row
> `corefast` docs. Larger-doc behavior is covered by the single-release `coresynth`/`scaling`
> suites against the current release only.

> **Why `corefast` runs 3 repeats.** Accuracy is stable at `repeats: 1`, which is why the
> broad grids stay there — but the release gate answers a different question: not "how
> accurate is this config" but "**did the release change anything**". At one sample a
> non-deterministic agentic outcome is indistinguishable from a regression, and that is not
> hypothetical: the v0.6.5 fix verification reported a new FAILURE and a −0.143 accuracy drop
> from a `repeats: 1` grid, and **neither reproduced**. Three samples let `--compare` reason
> about failure *rates* and mean-vs-spread instead of a single draw. The spend is bounded
> because `corefast` is deliberately the cheap ≤100-row subset. Do **not** raise
> `core`/`coresynth`/`full` to match — they include the 400/800-row documents.

## Regression thresholds

`aggregate.py --compare` compares per **(cell, doc)**, aggregating across repeats:

- **Failures** — compared as *rates*. `1/3 → 1/3` is the same behaviour, not a new failure;
  `0/3 → 3/3` is a regression. A partial increase (`1/3 → 2/3`) is reported but tagged
  *confirm before believing*, because both sides fail sometimes.
- **Accuracy / completeness** — mean-vs-mean, and the delta must exceed the run-to-run
  spread observed within that (cell, doc) on either side. Thresholds: accuracy ±0.02,
  calibration-separation −0.03.
- **Cost** — mean-vs-mean, +15%, likewise against the observed spread. Agentic cost varies
  ~4× run-to-run, so a single sample cannot resolve a cost difference at all.
- Anything that clears the threshold but not the spread prints as `~ … INCONCLUSIVE, add
  repeats` and is **excluded from both verdict lists** rather than counted as a finding.
- Failed runs are excluded from quality means, so a failure already reported as a failure
  cannot also manufacture an accuracy regression.

With `repeats: 1` this is identical to the old per-run comparison (mean = the single value,
spread = 0). A second, **variance-aware** pass then re-judges cost and quality at the *cell*
level across documents.

> The n=1 limitation this replaces bit at v0.6.5 — see
> [releases/v0.6.5.md §3.1](./releases/v0.6.5.md) for what it cost.

## Reproducibility & honesty rules

- Each results directory records the exact commit, the deployed stack version (read from the
  stack's CloudFormation `Description`, **not** the local git HEAD — those differ whenever a
  run targets a published template), stack name, model IDs, pricing hash, and date.
- Failures are reported explicitly; accuracy is never averaged over only the docs that
  completed without saying so (advanced/large runs are survivorship-sensitive).
- Any cell capped or skipped for cost is logged in `meta.json`, never silently dropped.
- Costs are **estimates** from `pricing.yaml` (intro pricing may apply); the rate date is stated.

## Maintaining the release audit trail — one command per release

Each release cycle, produce one new audit-trail entry comparing the **previous
published release** to the current **`develop`** prerelease:

```bash
make benchmark-release VERSION=0.6.0 PREV=0.5.16
```

This single target (see the repo `Makefile` and the `run-benchmarks` skill):

1. Deploys / reuses a stack from the **published PREV** template, runs `corefast`
   (native-upload configs, held at a shared control model both versions can run).
2. Upgrades the **same stack** in place to `develop` HEAD (`--from-code --clean-build`)
   and re-runs `corefast` with **byte-identical** configs (only the code version differs).
3. Aggregates both, diffs them, and scaffolds
   `docs/benchmarking/releases/v<VERSION>.md` from the template + appends a row to the
   [audit-trail index](./releases/).
4. Promotes the PREV summary to `benchmarks/results/baseline.json`.

Commit the new `docs/benchmarking/releases/v<VERSION>.md`, its figures under `images/`,
the `benchmarks/results/{vPREV,vVERSION}/corefast/` data dirs, and the updated
`baseline.json` and `releases/README.md`. That is the durable, per-release audit trail.

> **Retention.** `benchmarks/results/` keeps **one complete set per release** and nothing
> else: the `corefast` A/B grid behind each audit-trail entry. One-off suite slices
> (`config-*`, `intconf`, `advverify`, post-fix re-runs) are pruned once their finding is
> written into the prose here — the published page is the durable record, and the data stays
> recoverable from git history. Rules and recovery commands:
> [`RETENTION.md`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/benchmarks/results/RETENTION.md).

> The step-by-step mechanics (and the cross-version config-compatibility handling the
> harness performs) are documented in the `run-benchmarks` skill and
> `benchmarks/matrices/METHODOLOGY.md`.
