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
| [Benchmark Studies](./studies/) | *One question each*, asked once during development and not regenerated per release — prompt caching, classification confidence, multi-instance sections, the seven advanced-extraction refinements, schema restatement, greedy decoding on tool use | Added when a one-off question is measured; the [index](./studies/) lists each study's verdict. |
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
| `typed_accuracy` | The schema-**typed** target (`685.5`) | Whether **scalar** values came back correctly typed. Defined only for a document whose truth declares `fields_typed`, which is the `kv_form` document and nothing else — see below. Absent, not zero, on every other row. |
| `cell_accuracy` | Per-cell typed match on list rows, matched by `SEQ` tag | Value fidelity *inside* lists. Completeness answers "did the row come back"; this answers "with the right value". Defined only for a document that has a list attribute — absent, not zero, on a flat key/value form. Read it with `cells_compared`. |
| `sections_correct` | Section count vs `expected_sections`, as 1.0/0.0 | Boundary detection. Nothing else can see an over-split: a document split into 3 sections still reports `completeness_recall` 1.0 and status `COMPLETED`. The mean over repeats **is** the pass rate. |
| `n_gaps` | Truth `SEQ` ids **absent** from the extraction | Completeness. It is an *extraction* metric and says nothing about confidence — a document can have `n_gaps` 0 and every recovered row unscored. |
| `conf_coverage` | Extracted list rows carrying a confidence, over all extracted list rows | Whether the confidence surface covers the data. This is the quantity the `assessment_coverage_incomplete` guard fires on, computed by the same function (`idp_common.assessment.batching.confidence_coverage`) so the measurement and the guard cannot drift apart. |
| `mean_confidence` | The mean of the confidence values that **exist** | The calibration question. It is silent about rows that have no confidence at all — that is `conf_coverage`'s job, and the two move independently. |

**`typed_accuracy` is a scalar-field metric and only one corpus document can carry
it.** `kv_form` is the flat key/value form, and its generator emits the typed scalar
expectations the metric scores against. Every other synthetic document comes from the
`bank_statement` generator, whose two scalar fields are an account number with leading
zeros and a date *range* — both of which a typed schema correctly leaves as strings, so
there is no typed scalar to score. On those rows the pair is **absent rather than
null**, and `typed_truth_unavailable` says so and names `cell_accuracy` as the
per-value instrument that does apply there. Null would have claimed the other thing a
missing value means in these artifacts — measured, and nothing to join — which reads in
a published table as a blank cell instead of as a metric that was never applicable.

So the grid the model table is computed on (`core_synth`, all `bank_statement`) cannot
carry a typed-accuracy column, and the configuration guidance omits it rather than
printing blanks. The suites that do produce it are `enforcement` and `forcing`, both of
which name `kv_form` and must be run with `--class kv_form` for it, because configs are
built per document class.

**`cells_compared` is the column whose blank means one thing, and `cell_accuracy` is
not.** On a document with no list attribute — `kv_form`, the flat key/value form —
`cell_accuracy` and `cells_compared` are both **absent**, with
`cell_truth_unavailable` saying so and naming `scalar_accuracy` and `typed_accuracy`
as the per-value instruments that apply there. On a document that *has* a list and
returned none of its rows, both columns are present: `cell_accuracy` is null, because
0/0 is undefined and a `1.0` there would be the "a null column scores 1.000" blind
spot in its purest form, and `cells_compared` is the literal `0` it compared. In
`summary.json` those two states differ on `cell_accuracy` alone — absent against
present-and-null. **In the CSV they do not**, because `DictWriter` renders an absent
key and a null value as the same empty cell: a blank `cells_compared` means "this
document has no list" and nothing else, while a blank `cell_accuracy` is either that
or a list that joined nothing, which `cells_compared`'s `0` and
`cell_truth_unavailable` tell apart. Both states used to be the same pair of blanks in
both artifacts, so a reader could not tell a metric that never applied from a run that
returned nothing — which is the reading that hides row loss, since a model returning
10 rows of 400 scores 1.000 over the 20 cells it returned. **Never read
`cell_accuracy` without `cells_compared` beside it**; both are in `summary.json` and
in the CSV.

**Which encoding a grid you are reading uses is told by the reason columns, not by the
blank.** A row carrying `typed_truth_unavailable` and `cell_truth_unavailable` was
scored under the encoding above, so an absent metric there means "never applicable"
and nothing else. A row carrying neither column was scored under the earlier one,
where both pairs are present and null on a document they do not apply to, and a blank
is therefore *not* distinguishable from a measurement that came back empty. Every grid
committed under `benchmarks/results/` up to and including `v0.6.11` is of that earlier
vintage — they are not rescored, since scoring is a point-in-time read of a stack's
output and `RETENTION.md` records how short that window is — so check for the columns
before reading one of their blanks.

`conf_coverage` is `None`, not `1.0`, for a document with no list attribute: coverage
is undefined there, and `cell_stats` drops it rather than averaging it in. The cell
roll-up carries min, max, stdev and CV as well as the mean, because a mean of 0.99 is
equally consistent with every document at 0.99 and with one document at 0. The
per-field breakdown (`conf_unscored_by_field`) is in `summary.json` only — the CSV
carries the four scalars.

**Did the feature under test actually engage?** `analyze.py` also reads the audit block
each section records about itself, because a delta of zero is uninterpretable without
it — no effect, or no opportunity? `forced_tool_honored_rate` (a forced tool the model
answered in prose has measured nothing), `coercions` / `coercion_refusals`, and
`validation_valid_rate` are reported per run and rolled up per cell.

**Did the pipeline warn about anything?** Five stages emit structured
`ProcessingIssue`s — extraction, classification and assessment from inside the
services, plus rule validation and postprocessing from the pipeline handlers —
covering 28 codes including `extraction_rows_below_ocr_estimate`,
`assessment_coverage_incomplete` and `extraction_validation_failed`. `analyze.py`
records them per run as `processing_issues` (the count, `0` on a clean
run and never null), `processing_issues_by_severity` / `_by_code` / `_by_stage`
(tallies), and `processing_issue_detail` (every issue verbatim, which is where the
message a reader needs lives). `cell_stats` merges the tallies per cell over **all**
its runs including the failed ones, and `aggregate.py` prints a `⚠` line at scoring
time naming the codes. The CSV carries the count plus the severity and code tallies
rendered flat.

Read these **before** reading a completeness figure. A check that fires into a field
nobody reads is indistinguishable, in the results tree, from a check that does not
fire — so until these existed a run whose extraction reported losing rows was
recorded identically to a clean one, which is how a warned run came to be described
as a silent one. Nothing in the recorder enumerates issue codes: the tallies are
keyed on each issue's own values, so a code added to any service is recorded without
a harness change.

⚠️ **A zero here does not cover everything, and this is the part to know before
trusting one.** The figures come from the section's `result.json`. Three things
record onto the section's DynamoDB record *after* that file is written, and nothing
rewrites it afterwards, so a `processing_issues: 0` is silent about them rather than
evidence they did not fire:

- **The assessment degrade and skip pair** — `assessment_failed_confidence_unavailable`
  and `assessment_skipped_confidence_unavailable`. These are the ones that matter.
  They are **error** severity on a document that **completes**, because not failing
  it is the whole purpose of that path, so a section that came back with *no
  confidence scores at all* reads here as `processing_issues: 0, success: true` with
  no `status` to fall back on. And they are live in what you measure: across
  `results/v0.6.11/`, `resolved.assessment` is `separate` on 4,192 rows against 516
  `integrated` and 257 `off`, and the skip path is the standalone one.
- **`rule_validation` and `postprocessing`** — smaller, because these accompany a
  document the harness already records as failed, so what is lost is the reason
  rather than the event.

The tracking record is not read instead because it would need de-duplicating against
the S3 copy of everything earlier, it is another large attribute fetched per run, and
it is deleted with its stack while the S3 artifacts outlive it. So the boundary is
declared and closed: universe-closure tests over the declared stage constants and
over the codes `assessment/degradation.py` declares, plus a computed check that that
module still performs no S3 write, so a third such stage or code — or the boundary
ceasing to be true — fails a test rather than going quietly unread.

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
   Reference corpora (the two 20-document real labeled sets, plus `bank_real`)
   are launched as **test sets** through the TestRunner with the cell's config
   built onto the corpus's own base config, and every document is scored with
   the stack's evaluation (`analyze.score_reference`). That needs the cells built
   for the corpus's class too — `make_configs.py --suite <suite> --class realkie`
   (and `ocr_bench`) with the same `--set` overrides — and the test set present
   on the stack. A corpus whose index is missing is reported with the exact
   command to build it and recorded in the runmap as `docs_unlaunchable`, so a
   partial grid never reads as complete (#766).
4. **Score & aggregate** — `aggregate.py` scores every run on the seven dimensions, rolls
   them into `results/<release>/<suite>/summary.{json,csv}` with a `meta.json` (commit,
   stack, pricing hash, date), diffs against `results/baseline.json` to flag regressions,
   and emits figures.

## Suites (cost-tiered)

| Suite | Scope | Use |
|-------|-------|-----|
| `smoke` | 2 cells × 2 tiny docs | Per-PR gate (minutes) |
| `corefast` | 19 decision cells × 3 docs (≤100 rows) × **3 repeats** (171 runs/side) | **Release-vs-release A/B** — the grid that completes on *both* the previous published release and the new one (see notes) |
| `coresynth` | 19 decision cells × 7 synthetic docs (**133 runs**) | **Standard single-release run** — the cross-config grid the Configuration Guidance paper reports |
| `simplegrid` | the 9 simple-mode cells of `coresynth` × 7 synthetic docs (**63 runs**) | **Lightweight-model grid** — for a model that cannot run the agentic path (Nova Lite/Pro, [#895](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/895)) the full `coresynth` never finishes; this is what the guide's Nova rows are computed on |
| `core` | `coresynth` + the two 20-document reference corpora | Adds real-world labeled accuracy; several times the cost of `coresynth`, so opt in deliberately |
| `scaling` | simple vs advanced across the size series | The completeness-cliff study |
| `cost` | cost-decision cells × 1 mid doc, repeats≥5 | Cost-difference detection (variance-aware) |
| `intconf` | integrated + separate confidence × 1 list doc, repeats=4 | Re-verifies the integrated-confidence row-loss hazard; the one finding a single-sample grid cannot settle |
| `advverify` | advanced × integrated + separate × 1 list doc, repeats=4 | Re-verifies the **tool-decline** list-loss hazard (an agent that declines the table tool returning the whole list as `null`). Run with `--set extraction_model=sonnet5` |
| `astravalue` | Sonnet 5 vs OpenAI GPT-6 Astra, simple + advanced, 4 docs (3–26 pages) × **5 repeats** (100 runs) | **Does a ~4× more expensive frontier model earn its price for IDP?** See below |
| `astracap` | The same pair plus the 25-page-shard arms on `scale_3200` (66 pages, ~790K tokens), repeats=2 | The **ceiling** arm: a document too large for *both* models in simple mode, so the finding is where the 1.05M window stops helping and sharding takes over. Expensive — opt in deliberately |
| `opus55value` | Claude Opus 5.5 vs Claude Opus 5, simple + advanced, 2 docs (3–9 pages) × **5 repeats** (40 runs) | **Is the newer Opus actually cheaper for the same result?** The comparator is Opus 5 rather than the default, because the claim is a ratio and a ratio needs its denominator on the same documents. See below |
| `full` | core + all one-axis sweeps — including the **extraction-model sweep**, which is what puts a model in the published guide | The deep study for the paper (expensive) |

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

### Which models are actually measured

"Selectable in the product" and "covered by the published guidance" are different
things, and the difference is the **`extraction_model` sweep** — the one-axis sweep
`full` runs (or, as for v0.6.8 and v0.6.9, `coresynth --set extraction_model=<m>` once per model),
which is what the [Configuration Guidance](./config-guidance.md) §5 is computed from. A
model only appears there if it has been swept, and the table records when:

| Model | In the sweep | Measured | Note |
|---|---|---|---|
| Nova Lite | ✅ | 2026-09-12, **v0.6.8** (full `coresynth`, 133 runs, 32 failed) — not re-measured on v0.6.9 | simple mode ≤100 rows only; cannot run the agentic path ([#895](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/895)) — [guide §5](./config-guidance.md#5-which-model-for-which-documents--the-model-axis-measured) |
| Nova Pro | ✅ | 2026-09-12, **v0.6.8** (`simplegrid`, 63 runs; not re-measured on v0.6.9; full grid abandoned after 36 launches — advanced cells stuck in stream-error retries) | simple mode ≤100 rows only |
| Claude Sonnet 4.6 | held as the sweep's control | **2026-09-19, v0.6.9** (`core`, 893 runs incl. both reference corpora) | the fixed baseline every sweep varies against; cheapest of the three measured on v0.6.9 ($0.537/doc) and the only one that failed runs (4 of 133) |
| **Claude Sonnet 5** (default) | ✅ | **2026-09-19, v0.6.9** (133 runs, plus `scaling`, `scalingsimple`, `cost`, `advverify`, `astravalue`, `astracap`) | the shipped default; 0 failures of 133 at $0.676/doc, no confidence leaf below 0.9 anywhere in the grid |
| Sonnet 5 `:1m` | ✅ | 2026-09-12, **v0.6.8** (133 runs) — not re-measured on v0.6.9 | matched Sonnet 5's accuracy at every size that fits and rescued no request on this corpus (its reported +10% / +51% cost predates the #899 pricing fix and overstates the variant — the two are billed at identical rates, the 1M window carrying no premium) |
| **Claude Opus 5** | ✅ | 2026-09-12, **v0.6.8** (133 runs) — not re-measured on v0.6.9 | most complete model in the grid (0.993) at +23% / +41% over Sonnet 5; accuracy identical |
| **Claude Opus 5.5** | ✅ (`extraction_model` sweep + the `opus55value` pair) | **2026-10-11, v0.6.11** (`coresynth`, 133 runs, 13 failed) | **Measured: recall 1.000, cell accuracy 1.000, $0.699/doc, 286 s/doc** — the joint-best accuracy in the grid and cheaper per document than Opus 5 ($0.816), but the slowest arm by 52%. ⚠️ **It cannot complete a large document in simple mode at all**: all 13 of its failures are `large_narrow`, and they are exactly the 13 *simple*-mode cells, while all 6 *advanced* (sharded) cells completed 7/7. Sonnet 5 and Sonnet 5 1M completed all 13 of those same cells in the same window on the same stack, so this is a per-request limit (the 300 s non-streaming `converse` read timeout), not load. **On large documents this model requires advanced mode.** ⚠️ **The `forcing` axis has no measurement for it**, now confirmed from the API rather than the model card: its `force-on` cells completed with `forced_tool_attempted=0`, `forced_tool_honored_rate=None` and the refusal recorded in `forced_tool_skips` — `tool_choice: type "tool" and "any" are not supported for this model`. Every other arm honoured forcing at rate 1.00, so that column is meaningful everywhere else and absent here. `opus55value` has still not run. Its rate card is cheaper than Opus 5 in every category ($4/$20 per 1M vs $5/$25, and a cache read at 0.05x input rather than 0.1x), and its launch claim adds "fewer tokens for the same task" on top — three multipliers that compound, so **no cost figure for it should be quoted until `opus55value` has run**. Two behaviours also differ from Opus 5 in ways a run will show: thinking cannot be disabled (effort is the only spend lever, and its default is `medium` where Opus 5's is `high` — per the model card; a default is not observable from a response, so it is cited rather than measured), and a forced `toolChoice` is rejected, so the `forcing` axis cannot be `on` for it |
| **OpenAI GPT-6 Astra** | ✅ | **2026-09-19, v0.6.9** (133 runs + `astravalue` 100 + `astracap` 12) | the only model at recall 1.000 **and** scalar accuracy 1.000 on all 19 v0.6.9 grid cells (documents ≤400 rows), at 2.6× Sonnet 5's cost and marginally behind on per-row cell accuracy (0.977 vs 0.999); in simple mode returns an empty response on the 17-page document (13 of 13 draws) and rewrites descriptions on the 26-page one — [guide §5.2](./config-guidance.md#52-is-a-premium-model-worth-it-astravalue-astracap-opus55value) |
| `global.openai.gpt-6-astra` | ❌ deliberately; measured in `astravalue` only | 2026-09-12 (20 runs) | same weights ~10% cheaper (measured $0.94 vs $1.10 on the 9-page document); same simple-mode failure shape |
| Claude Haiku 4.5 (classification only) | ✅ `classification_model` axis | 2026-09-12, **v0.6.8** (133 runs) — not re-measured on v0.6.9 | see the guide §5.3 |
| **Claude Haiku 5.5** | ✅ (`extraction_model` **and** `classification_model` sweeps) | **2026-10-11, v0.6.11** (`coresynth` extraction sweep 133 runs / 4 failed, **and** the `classification_model` sweep 133 runs / 5 failed) | **Measured accuracy: cell accuracy 0.999 over ~61,400 compared cells — equal to the shipped default — at recall 0.981 and 168 s/doc.** As a *classification* model it is also at 0.999, indistinguishable from the `nova_2_lite` default, so classification is where it is unambiguously safe to adopt. ⚠️ **Two caveats that the accuracy figure alone hides.** Completeness at size: only **9 of 13** simple-mode cells complete on `large_narrow`, against 13/13 for Sonnet 5 in the same window — it is the second-least reliable arm in simple mode after Opus 5.5. And **cost remains unquotable**: the 5× long-context band below is not a hypothetical here, **105 of its 129 completed runs exceed 100,000 input tokens** (median 543,675, max 10.96M), so the harness's $0.155/doc is a lower bound and 5× of it (~$0.77) would exceed Sonnet 5's $0.587 — the *sign* of the comparison is unresolved, so **no cost ratio for this model is published**. The cheap end of the axis, and the first one that is a Claude: $0.11/$0.55 per 1M in/out, 10x below Haiku 4.5 and 20x below Sonnet 5 on input. The open question is not whether it wins but **how much completeness and accuracy the floor costs**, so quote no accuracy figure for it until the sweep has run. Four things a run will show that cannot be carried over from the Haiku 4.5 row: it accepts `reasoning_effort` (the first Haiku that does — 4.5 rejects it with a 400 — default `medium` per the model card, so an effort-unset cell is not comparable to a 4.5 cell); a forced `toolChoice` **is** accepted, unlike Opus 5.5, so a `forcing` A/B is valid; its prompt-cache minimum is 512 tokens against 4.5's 4,096, so classes that never cached may cache here; and images cost high-resolution tokens (~4,770 for a 2550x3301 page vs ~1,542 on 4.5), so an image-bearing cell is ~3x the input tokens and "10x cheaper per token" is not "10x cheaper per page". ⚠️ **Cost is under-reported by 5x above 100,000 input tokens, and the sizers cross that by default** — Anthropic prices this model in two prompt-length bands (5x on every token type above 100,000 tokens) while `pricing.yaml` holds one flat rate per model, and its agentic shard budget is 578,400 tokens. Same shape as the GPT-6 Astra arm and handled the same way; read the note on its `model_config_limits.yaml` entry before quoting a dollar figure for it. Capping the window to dodge the band was tried and rejected: 100,000 leaves a 2,000-token shard budget against Sonnet 5's 18,400 |
| **Claude Sonnet 5.5** | ❌ not in any sweep | — | selectable but **not measured**, so quote no accuracy, completeness or cost figure for it. Its per-token input and output rates are **identical** to Sonnet 5's in the AWS price list ($2.20/$11.00 per 1M, us./eu.), so "cheaper Sonnet" is wrong as a summary — the one price difference is cache reads at half Sonnet 5's ($0.11 vs $0.22 per 1M), which pays off in proportion to how much of a prompt caches. ⚠️ Three things make a cell for it **not** comparable to a Sonnet 5 cell without a change to the arm: a forced `toolChoice` is **rejected**, so `extraction.forced_tool` silently falls back to the prose schema (a `forcing` A/B is invalid on this model, unlike on Sonnet 5 and Haiku 5.5); its default context window is **1M with no `:1m` id**, so auto-sizing derives a 578,400-token agentic shard budget against Sonnet 5's 18,400, which is the `:1m` arm's sizing rather than the default arm's; and its prompt-cache minimum is **512 tokens against Sonnet 5's 1,024**, so classes that never cached may cache here. Its per-token cost is not under-reported at any prompt length — unlike Haiku 5.5 and Astra, the price list gives it a single band |
| xAI Grok 4.6 | ❌ | — | not yet measured — see `docs/grok-models.md` for its documented capabilities |

The classification-model axis (Nova 2 Lite default · Sonnet 5 · Haiku 5.5) and the
confidence-model axis (Nova Lite default · Nova 2 Lite · Sonnet 5) were **both swept in
full on v0.6.11** — three arms each, 133 runs per arm, 2026-10-11. The guide's §5.3 and
§5.4 report them.

⚠️ **Haiku 4.5 is not on the classification axis**, and that is deliberate rather than an
omission: `config_matrix.yaml` sweeps `haiku55` and states that `haiku45` is not swept, so
that the axis stays "the same width as before plus one". Both remain axis *values*, so a
named cell or an explicit `--set classification_model=haiku45` covers the 4.5-vs-5.5 pair
on demand, and `advsplitcost__classification-model-haiku45` is a committed result to diff
against. **Read the axis in `config_matrix.yaml`, never a prose list of it** — this
paragraph, the release-validation skill's own copy of the same axis, and a finding in the
v0.6.11 prerelease record each drifted from it independently.

Two numbers the `classification_model` sweep does **not** produce, and which the
guidance for that axis needs: per-page **classification accuracy** and
**confidence calibration separation** (`mean(conf | right) − mean(conf | wrong)`,
[#673](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/673)).
Those come from a separate driver, `benchmarks/harness/run_classification_bench.py`,
which runs a real multi-class corpus with per-page ground truth instead of the
synthetic single-class documents `run_matrix.py` launches — so a model added to the
axis above must also be added to that script's own `MODELS` map. Haiku 5.5 is in both.
Run the Haiku pair as one measurement:

```bash
AWS_PROFILE=default python3 benchmarks/harness/run_classification_bench.py \
    --stack <STACK> --testset docsplit --n 20 --models haiku45,haiku55 --mode topk
```

The separation is the number to read first on a model this cheap: high
`class_accuracy` with a confidence that does not separate right answers from wrong
ones is worse for a user than lower accuracy with an honest confidence, because the
escalation path acts on the number.

#### Comparing classification *configurations*, not just models

The same driver crosses the models with named combinations of
`classification.granularity`, `classification.modality` and
`classification.strategy` via `--variants` (see `VARIANTS` in the script for the
list). ⚠️ **No shipped guidance rests on this yet** — it has not been run, and
[#1513](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1513)
tracks doing so; the recommendations in
[Classification](../classification.md#choosing-granularity-modality-and-strategy)
are reasoned from token arithmetic rather than measured.

```bash
# Is packet granularity cheaper, and does it cost accuracy?
AWS_PROFILE=default python3 benchmarks/harness/run_classification_bench.py \
    --stack <STACK> --testset docsplit --n 20 --models nova2lite \
    --variants page-both,packet-both,packet-text --estimate
```

Three things about reading the result:

- **`boundary_accuracy` and `label_accuracy`, not overall section accuracy.**
  `split_accuracy_without_order` requires a section to match on both its page
  range *and* its class, so it cannot show a granularity winning on boundaries
  while losing on labels — and those two call for different fixes. The
  decomposition is exact: `split = boundary × label`.
- **Cost per document *and* per page.** Per-page flatters packet granularity on
  long packets and per-document flatters page granularity on short ones;
  whichever you quote alone will argue for the setting it favours.
- **`classification_by_context`** carries the per-stage call count and cost, so
  a two-stage cell shows whether the money went to `Segmentation` or `Labeling`,
  and packet granularity's N-calls-to-1 claim is read off `calls_per_document`
  rather than assumed.

⚠️ **The matrix is multiplicative and cells drain one at a time** — deliberately,
since a page cell and a packet cell issue very different call volumes for the
same documents, and run concurrently the page cell's per-page burst throttles
the packet cell it is being compared against. `--estimate` prints the document
count before you commit the wall-clock.

The premium cells below live in their own `model_premium_cells` registry, **not** in
`core_cells`, because `core_cells` feeds `core` / `coresynth` / `corefast` / `full`:
putting seven model-pinned cells there inflated the standard grid by 37% and — on any
document over ~11 pages — added cells that are *expected* to fail, which makes a
release regression gate unreadable.

### Is a premium model worth it? (`astravalue` / `astracap`)

A model-price question is a **ratio**, so this pair is built to be able to answer
"no". It compares Claude Sonnet 5 against OpenAI GPT-6 Astra (~4× the input price)
with only `extraction.model` differing.

**Size a fixture by page count, not by text.** This is the finding that shaped the
suite, and it cost five wasted runs to learn. What Simple mode sends is dominated by
**page images**, not OCR text:

| | tokens |
|---|---|
| one 300-DPI Letter page image (2550 × 3300 ÷ 750, Bedrock's own formula) | **~11,220** |
| the OCR text of that same page | ~600 |

So the pipeline costs **~12,000 tokens per page** — about 19× the text — and the
practical Simple-mode ceiling is a *page* count: **~11 pages for Sonnet 5** (200K
window, 140,000 usable at the 0.30 buffer) and **~61 for Astra** (1.05M / 735,000
usable). Measured live on IDP1: an 81-page fixture estimated 964,223 tokens and a
164-page one 1,971,931 — both rejected by *Astra* as well as Sonnet 5. The
discriminating band is therefore roughly **12–61 pages**, and most of it was already
covered by existing fixtures. Four documents separate the two things a frontier model
can actually be paid for:

| Doc | Size | What it isolates |
|---|---|---|
| `small_narrow` | 3 pages, ~35K | **Control.** An ordinary document where the cheaper model is already at ceiling. Astra should *lose* here on cost-per-correct-field; if it doesn't, the suite is broken |
| `med_narrow` | 9 pages, ~108K | **Size that still fits both.** Guards against crediting a capacity win that was never needed |
| `large_narrow` | 17 pages, ~203K | The first cell Sonnet 5 **cannot do in one request** while Astra can — capability, not accuracy |
| `dense_250` | 26 pages, ~304K | **Astra-only *and* maximally difficult** — 8 columns, 4 interleaved lists, long free text, 15% OCR noise, typed value truth |
| `scale_3200` (`astracap`) | 66 pages, ~790K | **Exceeds Astra too.** Both models fail in simple mode; that ceiling is the finding |

Two things to know before reading the output:

- **Sharding is the alternative to buying a bigger window.** On the agentic path the
  5-page-per-shard default is a *timeout* guard, not a context proxy — its field doc
  says a roomy token budget "must NOT collapse a large doc back into one giant shard",
  and `0` is documented as unsuitable for large docs. So neither suite runs an
  unbounded arm (it would measure a Lambda timeout); `astracap` instead adds
  `astra-adv-wide` / `sonnet5-adv-wide`, which raise it to 25 pages — the tuning a
  user could adopt. If sharded Sonnet 5 matches Astra's completeness for less, the
  honest recommendation is "shard, don't buy".
- **`repeats: 5` also measures Astra's implicit prompt caching**, the one mechanism
  that can close a 4× gap: runs 2–5 re-send an identical prefix, so
  `cacheReadInputTokens` should be non-zero from the second run and cost per document
  should fall. Read the per-run series — a mean hides it. Astra needs no
  `<<CACHEPOINT>>` marker (and rejects an explicit one).

**The deciding metric is cost per correctly-extracted field**, not accuracy and not
cost. A premium model earns its price only where the accuracy gap is large enough that
the cheaper model would need human review to close it. If accuracy ties, the answer is
the cheaper model.

### Is the newer Opus cheaper for the same result? (`opus55value`)

The same method, one generation apart: Claude Opus 5.5 against Claude Opus 5, with only
`extraction.model` differing. The comparator is Opus 5 rather than the shipped default
because the claim under test names it — Opus 5.5 is cheaper per token, has cheaper cache
reads, and is said to use fewer tokens for the same task — and a ratio needs its
denominator run on the same documents, on the same stack, in the same window.

**Three multipliers compound, so the rate card cannot predict the answer.** Input and
output are 0.8× Opus 5's ($4/$20 per 1M against $5/$25). A cache read is 0.05× input
rather than the 0.1× every other model in `config_library/pricing.yaml` uses, so cached
reads are 0.4× Opus 5's per-token cost, not 0.8×. And "fewer tokens" is a third factor
the price list says nothing about. The rate card alone predicts about −20%; anything
beyond that is the token claim, and anything short of it is worth explaining before it
is published.

| Doc | Size | What it isolates |
|---|---|---|
| `small_narrow` | 3 pages, ~35K | **Control.** Both models are already at ceiling, so a cost delta here is the rate card and nothing else |
| `med_narrow` | 9 pages, ~108K | 3× the size, still one request in simple mode: where a per-token difference is large enough to read |

Three things to know before reading the output:

- **The documents are a deliberate subset of `astra_docs`.** `large_narrow` (17 pages)
  and `dense_250` (26 pages) are in that set because Astra's window reaches them and
  Sonnet 5's does not — that asymmetry *is* the Astra finding. Opus 5 and Opus 5.5
  share one window and one sizing budget, so both simple-mode arms would be refused on
  both documents: an A/B whose two arms fail identically measures nothing and is still
  billed. The ceiling question belongs to `astracap`, which already owns it.
- ⚠️ **This suite measures the switch, not the model.** Opus 5.5 defaults to `medium`
  reasoning effort where Opus 5 defaults to `high`, and neither cell sets
  `reasoning_effort` — so the two arms differ by model *and* by default effort. That is
  what a user who changes the model and nothing else actually gets, which is the
  decision the guide has to inform, but it is not a model-for-model comparison. To
  separate the two, re-run with `--set reasoning_effort=high` on both arms and read the
  difference between the two runs.
- **`repeats: 5` is what makes a cost claim sayable at all**, for the same reason
  `astravalue` uses it: agentic cost is non-deterministic (turn-count spreads around 4×
  have been observed), so a single draw per cell cannot resolve a cost *difference*.
  It also exercises the cache-read rate that is half of the price story — runs 2–5
  re-send an identical prefix, so read the per-run series rather than the mean.

**Forcing cannot be part of this comparison.** Opus 5.5 rejects a forced `toolChoice`,
so with `extraction.forced_tool.enabled` on it falls back to the prose schema and
records the reason in the section metadata, while Opus 5 forces normally. A `forcing`
A/B built on Opus 5.5 would show a delta of zero for a reason that has nothing to do
with forcing.

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
