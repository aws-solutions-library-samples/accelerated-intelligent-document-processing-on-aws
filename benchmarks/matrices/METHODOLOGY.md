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

### B. Reference (real, labeled)
Existing stack test sets (`realkie-fcc-verified`, `ocr-benchmark`, `samples-tables`)
with curated evaluation baselines. Real-world messiness the synthetic set can't emulate.

> ⚠️ **`run_matrix.py` cannot launch reference corpora yet ([#766](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/766)).**
> It launches one local PDF per run, and a reference doc is a test *set* on the
> stack with no PDF under `corpus/docs/`. It used to drop them silently, so a
> suite naming `core_docs` measured **7 of its 9 documents** and reported like a
> full sweep — losing exactly the two corpora with real documents and
> human-verified labels. The launcher now names them, excludes them from the plan
> count, and records `docs_named` / `docs_run` / `docs_unlaunchable` in
> `runmap.json`; **check `docs_unlaunchable` before quoting a suite's result as
> covering its whole document list.** The scorer for them
> (`analyze.score_reference()`) is fully implemented and reachable — only the
> launcher is missing. Until it exists, run reference corpora through Test Studio
> (`harness/detection_ab_teststudio.py` invokes the same TestRunner Lambda the
> Test Studio UI does) and report them as a separate arm.

## 2. Test-set + config registration
- Each synthetic doc is uploaded to `s3://<stack>-testsetbucket-*/bench-<id>/input/` and
  registered as a test set (a `testset#bench-<id>` metadata row with `filePattern`).
- `make_configs.py` expands `config_matrix.yaml` into full v0.6 configs (one per
  cell × doc-class), validates each with `merge_config_with_defaults(..., validate=True)`,
  strips `managed`, and uploads as `Config#bench-<cell>-<class>` via `idp-cli config-upload`.
- NEVER mutate `Config#default`. Always run against a named `--config-profile`.
- PYTHONPATH is pinned to the repo's `idp_common` to avoid a stale sibling checkout
  silently stripping v0.6 fields on upload.

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
| **confidence calibration** | From `explainability_info` leaves: mean confidence; %below-threshold (alert rate); and, where a match flag exists, separation = mean(conf\|correct) − mean(conf\|incorrect). Over-confidence on wrong values is a calibration regression even if accuracy holds. |
| **latency** | Wall-clock from doc WorkflowStartTime→CompletionTime; also per-phase where available. |
| **token use** | Per-phase, per-model, per-unit (input/output/cacheRead/cacheWrite/requests) from the doc `Metering` map. |
| **cost** | Metering priced with `config_library/pricing.yaml` (longest-suffix key match), broken out by phase (OCR/Extraction/Assessment/Summarization/Lambda). |

Scoring is **resolver-free** (reads S3 + DDB directly) so it works on any stack version.

## 5. Aggregation + comparison — aggregate.py
- Rolls per-run scores into `results/<release>/<suite>/summary.{json,csv}`: one row per
  (cell, doc) with all seven dimensions, plus per-cell and per-doc marginals.
- Cross-release comparison: diff a release's `summary.json` against `baseline.json`
  on matched (cell, doc) keys; flag deltas beyond thresholds (accuracy −>2%, cost
  +>15%, any new failure, calibration separation drop) as **regressions**.
- Emits the paper's tables + figures (matplotlib) into `paper/figures/`.

## 6. Reproducibility & honesty rules
- Record the exact commit, stack, model IDs, pricing.yaml hash, and date in each
  results dir (`meta.json`).
- Advanced/large runs are survivorship-sensitive: report failures explicitly and
  NEVER average accuracy over only the docs that completed without saying so.
- Any cell that is capped/sampled/skipped for cost is logged in `meta.json`, not
  silently dropped.
- Costs are ESTIMATES from pricing.yaml (intro pricing may apply); state the rate date.

## 7. Cost/time budgeting
The `full` suite is large. `run_matrix.py --estimate` prints projected doc-count,
Bedrock cost (from prior per-cell cost priors in `results/baseline.json`), and
wall-clock BEFORE launching, so a release owner opts in knowingly. `smoke` is the
per-PR gate (~2 cells × 2 tiny docs); `core` the standard release run; `full` +
`scaling` the deep study for the paper.
