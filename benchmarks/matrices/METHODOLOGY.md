# Benchmark Methodology

This defines how the suite builds test sets, executes the config × doc matrix, and
scores results so numbers are comparable across configs and across releases.

## 1. Corpus construction

### A. Synthetic (exact ground truth)
`corpus/generators/*` produce PDFs with a **known** field set and, for list fields,
one row per record tagged with a unique `SEQnnnnn` marker embedded in a cell. The
generator writes `<id>.pdf` + `<id>.truth.json` (`{fields, seq_ids, per_list, rows,
cols, lists, ...}`). This makes completeness/accuracy measurable EXACTLY and lets us
vary size (rows/pages), list count, row width (token density), text length, and a
controllable OCR-noise level. Generators are deterministic given their params (no RNG
that would break reproducibility) so a regenerated corpus is byte-comparable.

> ⚠️ **A generated document can also be wrong, and the ground truth will not say so.**
> `longdesc_100` rendered its long descriptions as plain strings in a reportlab table
> cell, which does not wrap — so the text ran past the column edge and **overprinted the
> Amount column**. Textract read the collision: the OCR of that document contained *zero*
> amount-shaped lines, and one row came back as `"...recurring monthly charge00ference
> invoice 0"`, the amount `0.00` stamped over the word `reference`. Every Amount on the
> document was physically unreadable, so it tested nothing — while the truth file
> confidently asserted the values that had been drawn over.
>
> Fixed by wrapping long description cells in a `Paragraph`. **Results for
> `desc_len: long` documents are not comparable across that fix** (the page count changes,
> 3 → 4). The generic lesson: when a metric is unexpectedly *uniform* across every
> configuration — as `cell_accuracy` was here, exactly 0.500 for all 19 cells — suspect the
> document before the product.

### B. Reference (real, labeled)
Existing stack test sets (`realkie-fcc-verified`, `ocr-benchmark`, `samples-tables`)
with curated evaluation baselines. Real-world messiness the synthetic set can't emulate.

> **Reference corpora are launched as test sets** ([#766](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/766)).
> A reference doc is a test *set* on the stack, not a PDF under `corpus/docs/`, so
> `run_matrix.py` submits it through the TestRunner with the cell's config version
> and the corpus's `n` documents, polls until every document has finished, and
> `aggregate.py` scores each document with `analyze.score_reference()` (the
> stack's own evaluation; there is no local truth). Two prerequisites, or the
> corpus is reported *unlaunchable* with the exact command to fix it and recorded
> as `docs_unlaunchable`:
>
> 1. The suite's cells must be built onto the corpus's **own** base config —
>    `make_configs.py --suite <suite> --class realkie` and `--class ocr_bench`
>    (same `--set` overrides as the synthetic class). `doc_matrix.yaml`'s
>    `class` field names that class.
> 2. The test set must exist on the stack; a runner rejection is printed and the
>    run recorded as `NOT_LAUNCHED`.
>
> Reference corpora ride along with the suite regardless of `--class`; on a
> second per-class pass of the same suite (e.g. `--class kv_form` for `kv_form`)
> pass `--no-reference` so 20-document corpora are not paid for twice.
>
> **What this does to a cell's headline number.** `cell_stats` averages over
> rows, and a reference run contributes one row per document (`sub_doc`). In
> `core` a cell therefore averages 7 synthetic rows and 40 real-document rows, so
> most of the cell mean now comes from the two real corpora and `n_runs` counts
> documents, not the 9 names in the plan. Release comparisons pair rows on
> `(cell, doc, sub_doc)`, so a corpus is compared document by document. Compare
> `core` results only with `core` results produced after this change; `coresynth`
> is the synthetic-only view.
>
> `runmap.json` (and the committed `meta.json`) record `docs_named` / `docs_run`
> / `docs_reference` / `docs_unlaunchable` / `docs_skipped_reference` /
> `docs_other_class`. Those fields are *absent* on a runmap produced before they
> existed (or by another launcher): absent means **unknown**, not "nothing was
> skipped". `docs_other_class` is not a shortfall: a suite may legitimately name
> documents of several classes (`enforcement` and `forcing` name `kv_form` beside
> bank-statement docs), and those are run under their own `--class` in a
> separate invocation.

## Setup failures do not silently become results

A cell may only run if its configuration reached the stack. If
`idp-cli config-upload` fails for a version, the stack still holds *some*
configuration under that name — `Config#bench-*` names are deterministic and
reused across grids, so a previous grid's version of the same name is often still
there — and launching the cell anyway produces a full set of plausible numbers
attributed to a configuration that never landed. That is worse than a crash,
because the numbers get written down.

`run_matrix.py` therefore:

- **skips the affected cells** rather than aborting the grid. A sibling cell whose
  configuration did land is unaffected and still worth running, so an overnight
  40-cell suite does not lose 39 good arms to one bad upload. If *every* version
  fails, nothing is launched and the run exits immediately.
- records `config_upload_failed_versions` and `cells_skipped_config_upload` in
  `runmap.json`, which `aggregate.py` carries into `summary.json`'s `meta`. The
  runmap is gitignored, so the committed summary is the only durable record —
  a non-empty `cells_skipped_config_upload` means that summary **does not cover
  the whole suite**. `aggregate.py` also prints the warning at scoring time,
  because whoever scores is the person about to copy the numbers somewhere and
  did not necessarily watch the launch.
- **exits non-zero** after draining, so an unattended invocation or a wrapper
  script does not read a partial grid as a complete one.

Upload success is read from `idp-cli config-upload`'s **exit code**, not by
looking for a success message in its output: that message is rendered by `rich`,
which hard-wraps at the terminal width, so a narrow or non-tty terminal splits it
mid-string. While a FAIL was only a misleading console line that was cosmetic;
now that a FAIL skips a paid-for arm and fails the grid, it is not.

A grid whose every launch was **rejected** by the TestRunner — a stale `--stack`,
or a missing TestRunner Lambda — also exits non-zero. It used to print `done.` and
exit 0 after measuring nothing.

Two other setup steps fail closed for the same reason. A test-set registration
whose `aws s3 cp` fails is fatal before anything is launched — the metadata row
asserts `status: READY` and `fileCount: 1`, so writing it for a document that is
not in the bucket produces a test set the runner accepts and never completes. And
a synthetic document with no `<id>.pdf.truth.json` beside its PDF is recorded in
`docs_missing_truth`: the run is still valid, but it is scored by the stack's own
evaluation rather than exact local ground truth, which is a different scorer and
not comparable.

## Read failures do not silently become measurements either

The scoring half has the same property as the launch half above, and for the same
reason: **the harness may continue past a failure, but it may not record the failure
as a value.** An artifact that cannot tell the two apart is not checkable afterwards,
and these artifacts are published and long-lived.

The concrete shape this takes is that every read distinguishes **three** states, not
two. "Nothing there", "could not read it" and "read it, and the answer is zero" are all
real and all different, and zero cannot be the sentinel for either of the first two
because it is a legitimate answer for most of these metrics — no cost for a cached call,
no corrections, no missing rows. `lib.Reading` carries the state, and it is built so the
distinction cannot be dropped by accident: truth-testing one raises, `.value` raises
unless the read happened, and `.value_or(default)` substitutes for an absence but
refuses for a failure. `lib.SectionRead` does the same for a document's section objects,
separating "this document has no sections" from "some of its sections would not read".

What the callers do with that:

- **`analyze.score_doc` refuses to price a metering row it did not read.** `cost` is
  null and `cost_unread` names the state and the reason. An empty metering map still
  prices to $0.00, because that is a real reading of a run that metered nothing — the
  distinction is between that and a DynamoDB failure, which used to produce the same
  `$0.00`. A null drops out of every mean; a zero would drag each of them down and make
  the configuration look cheaper than it is.
- **A document whose section objects did not all read contributes no metric at all.**
  Not a set of nulls: `calibration_curve: null` already means "measured, and there was
  nothing to join". The row carries `sections_unreadable` and `sections_unread` and
  nothing else, so it falls out of every average instead of biasing one.
- **`aggregate.cell_stats` counts the exclusions** (`n_cost_unread`,
  `n_sections_unread`), so a mean taken over a thinned sample says that it was thinned.
  `compare_cells` prints the same under `MEASURED OVER FEWER RUNS THAN IT LOOKS`.
- **`aggregate.calibration_study` has an `unreadable` bucket** next to `no_confidence`
  and `no_joinable_cell`, so an undecryptable grid is visibly different from an
  unassessed one.
- **`aggregate.augment_summary` asks every row** rather than probing the first empty
  prefix, and leaves an unreadable row un-augmented.

The failure that motivated all of this cost nothing only by luck. A release stack's KMS
key entered pending deletion, so every object in its output bucket was present, listable
and undecryptable; `GetObject` answered `KMS.KMSInvalidStateException`, the reader turned
that into `None`, and the grid read as one that had recorded no confidence at all — which
would have been written into a committed artifact and published as a fact about that
release.

## 2. Test-set + config registration
- Each synthetic doc is uploaded to `s3://<stack>-testsetbucket-*/bench-<id>/input/` and
  registered as a test set (a `testset#bench-<id>` metadata row with `filePattern`).
- `make_configs.py` expands `config_matrix.yaml` into full v0.6 configs (one per
  cell × doc-class), validates each with `merge_config_with_defaults(..., validate=True)`,
  strips `managed`, and uploads as `Config#bench-<cell>-<class>` via `idp-cli config-upload`.
- NEVER mutate `Config#default`. Always run against a named `--config-profile`.
- PYTHONPATH is pinned to the repo's `idp_common` to avoid a stale sibling checkout
  silently stripping v0.6 fields on upload. ⚠️ `PYTHONPATH` puts the source tree on the
  path but **not** its dependencies, so `pip install -e "lib/idp_common_pkg[core]"` is a
  prerequisite for scoring as well as for upload: `analyze.py` imports
  `idp_common.assessment.batching` for the coverage figure and
  `idp_common.evaluation` for the calibration figures, and a missing dependency there
  surfaces part-way through a grid rather than at the start. Neither import needs the
  `[evaluation]` extra — `confidence_curve` and `curve_store` are standard-library-only
  by design, and the unbinned AUROC and Brier score are derived from the stored value
  tally and squared-error sum rather than by calling Stickler. `matplotlib` is needed
  only for figures and is skipped with a message when absent.

## 3. Execution
- `run_matrix.py` launches each (config-cell × doc) via the stack TestRunner
  (`idp-cli run-inference --test-set bench-<id> --config-profile bench-<cell>-<class>`),
  records `runId`s to `results/<run>/runmap.json`, and polls per-doc rows in the
  TrackingTable (`ObjectStatus`/`EvaluationStatus`) until COMPLETED/FAILED.
- Concurrency is capped and large docs are launched last to limit Bedrock throttling.
- `repeats` (config_matrix suites) > 1 enables measuring run-to-run variance
  (important: advanced mode is non-deterministic on OCR-corrupted tables).

## 4. Scoring (per run) — analyze.py
Every run is scored on SEVEN dimensions:

| Dimension | Definition |
|-----------|------------|
| **success/fail** | ObjectStatus COMPLETED vs FAILED; failure phase + Bedrock error class captured (e.g. `ValidationException: Input too long`). |
| **completeness** | Synthetic: distinct `SEQ` recovered ÷ GT count (recall); truncation point = longest contiguous prefix; dup/gap counts. Reference: parse-failure rate. |
| **accuracy** | Synthetic: field-exact match rate on scalar fields + per-row cell match on list fields (keyed by SEQ). Reference: stack `evaluation/results.json` `weighted_overall_score`. |
| **confidence calibration** | Two instruments, and the second answers the question the first cannot. Distributional: mean confidence, %below-threshold (alert rate), and — where a match flag exists — separation = mean(conf\|correct) − mean(conf\|incorrect). Per-cell, on the synthetic corpus only: each confidence leaf is joined to the cell it scores through `flatten_confidences`' field path and the row's `SEQ` tag, giving true `(confidence, correct)` pairs from which `idp_common.evaluation.ConfidenceCurve` computes **ECE** (calibration) and **AUROC** (discrimination), alongside an unbinned AUROC and a Brier score computed locally from the stored value tally and squared-error sum (asserted equal to Stickler's `AUROCMetric` / `BrierScoreMetric` in `benchmarks/tests/`, but not computed by them — that is what keeps the `[evaluation]` extra off the scoring path). Over-confidence on wrong values is a calibration regression even if accuracy holds — and a score that is well calibrated can still rank at chance, which only AUROC sees. |
| **confidence coverage** | `scored_rows / expected_rows` per document and per field, computed by the same `audit_explainability` rule the `assessment_coverage_incomplete` guard fires on. Distinct from the row above: it counts rows with NO confidence, where the row above describes the scores that exist. Undefined (`None`), not 1.0, for a document with no list attribute. |
| **latency** | Wall-clock from doc WorkflowStartTime→CompletionTime; also per-phase where available. |
| **token use** | Per-phase, per-model, per-unit (input/output/cacheRead/cacheWrite/requests) from the doc `Metering` map. |
| **cost** | Metering priced with `config_library/pricing.yaml` (longest-suffix key match), broken out by phase (OCR/Extraction/Assessment/Summarization/Lambda). |

Scoring is **resolver-free** (reads S3 + DDB directly) so it works on any stack version.

### An instrument only exists where the truth file declares it

`accuracy` above promises "per-row cell match on list fields (keyed by SEQ)", and that
promise is only kept for a document whose truth file carries `rows_typed`. It used to be
emitted **only** for the value-noise variants, so every other synthetic document —
including all seven in `core_synth`, the grid the config-guidance paper reports — had no
per-cell instrument at all, and `cell_accuracy` came back `None`.

That is not a theoretical gap. On `longdesc_100`, simple extraction returned all 100 rows
with `Amount: null`; `completeness_recall` counts SEQ tags in the *Description* and
`scalar_accuracy` only looks at document-level fields, so the run scored **recall 1.000 /
accuracy 1.000 with an entire column empty**. Two studies drew conclusions across that
blind spot before it was closed.

`rows_typed` is now emitted for every generated bank statement. Two rules follow:

- **A `None` metric is "not measured", never "fine".** Before reporting "no effect",
  check that the instrument that would have shown the effect was populated — the
  aggregate prints `cells_compared` alongside `cell_accuracy` for exactly this.
- **Scoring is retroactive.** It re-reads S3 and DynamoDB, so adding truth or a metric
  lets an already-completed run be re-scored with no new spend. Prefer that to a re-run.

## 5. Aggregation + comparison — aggregate.py
- Rolls per-run scores into `results/<release>/<suite>/summary.{json,csv}`: one row per
  (cell, doc) with all seven dimensions, plus per-cell and per-doc marginals.
- Cross-release comparison: diff a release's `summary.json` against `baseline.json`
  on matched (cell, doc) keys; flag deltas beyond thresholds (accuracy −>2%, cost
  +>15%, any new failure, calibration separation drop) as **regressions**.
- Per-cell calibration is compared on the **pooled** curve rather than on a mean of
  per-document figures, because a 5-row form and a 400-row statement must not carry
  equal weight and a single document's AUROC is usually undefined outright. Flagged:
  pooled `ece_mean_conf` +0.01, pooled **unbinned** AUROC −0.05, or — at any size of
  step — a cell crossing `ECE_UNRELIABLE_THRESHOLD` or `AUROC_UNRELIABLE_THRESHOLD`,
  past which the shipped review-effort estimator stops recommending a review subset at
  all. For BOTH metrics the magnitude and crossing checks deliberately read **different**
  estimators, and in the same direction: magnitude reads the one that can move, crossing
  reads the one the product thresholds. The gate's own estimators barely move here —
  midpoint `ece` spans 0.0013 against 0.0239 for `ece_mean_conf`, and binned `auroc` is
  *exactly* 0.5000 on 29 of the 33 committed cells where it is defined at all, against an
  unbinned drift of up to 0.0461 on the same data. ⚠️ Neither magnitude threshold has a
  stability-of-`n` guard: at cell granularity the largest observed worsening, +0.0088,
  came from a cell whose sample collapsed 4,410 → 410, which clears
  `MIN_OBSERVATIONS_FOR_MEASURED` (30) and is gated on as if it were the larger sample.
- `--calibration` pools the same figures per **configuration arm** across a whole
  grid, prints each arm's own run / document / excluded-run counts (the arms are not
  equally powered), and prints the count of WRONG cells beside the AUROC columns,
  because ranking power is estimated over wrong × correct pairs — an arm with 70,000
  cells and 55 errors is a 55-observation measurement of discrimination.
- The `calibration_curve` stored per row is an **exact sufficient statistic** for every
  pooled figure, so `--calibration` reads the committed artifact and needs no AWS;
  `--calibration-from-s3` forces the re-read that verifies it, and `--augment`
  backfills it into a grid scored before it existed. This is what keeps a published
  measurement checkable after its stack is deleted.
- Emits the paper's tables + figures (matplotlib) into `paper/figures/`, including a
  per-arm reliability diagram plotted against each bin's mean confidence.

## 6. Reproducibility & honesty rules
- Record the exact commit, stack, model IDs, pricing.yaml hash, and date in each
  results dir (`meta.json`).
- Advanced/large runs are survivorship-sensitive: report failures explicitly and
  NEVER average accuracy over only the docs that completed without saying so.
- Any cell that is capped/sampled/skipped for cost is logged in `meta.json`, not
  silently dropped.
- A figure that could not be READ is null and carries a reason, never zero. Before
  quoting a cost or an accuracy, check the row's `cost_unread` / `sections_unread` and
  the cell's `n_cost_unread` / `n_sections_unread` — see "Read failures do not silently
  become measurements either" above.
- Costs are ESTIMATES from pricing.yaml (intro pricing may apply); state the rate date.

## 7. Cost/time budgeting
The `full` suite is large. `run_matrix.py --estimate` prints projected doc-count,
Bedrock cost (from prior per-cell cost priors in `results/baseline.json`), and
wall-clock BEFORE launching, so a release owner opts in knowingly. `smoke` is the
per-PR gate (~2 cells × 2 tiny docs); `core` the standard release run; `full` +
`scaling` the deep study for the paper.
