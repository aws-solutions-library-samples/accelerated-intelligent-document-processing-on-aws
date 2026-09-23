# Run Benchmarks — GenAIIDP empirical config/scaling suite

Use this skill to run the benchmark suite in `benchmarks/` — an end-to-end,
ground-truth study across document types/sizes × configuration options that
quantifies success/completeness/accuracy/calibration/latency/tokens/cost. Use it
to (a) regenerate the results paper for a release, or (b) gate a code change by
comparing against the committed baseline.

> Read `benchmarks/matrices/METHODOLOGY.md` first — it defines the matrices,
> scoring, and reproducibility/honesty rules. Do NOT restate numbers from memory;
> everything comes from the harness output.

## Environment (two gotchas)
```bash
source /home/ec2-user/projects/idp1/.venv/bin/activate
export PYTHONPATH=/home/ec2-user/projects/idp1/lib/idp_common_pkg   # avoid stale idp2/idp3 checkout
# All AWS + idp-cli calls use AWS_PROFILE=default (deployment acct); confirm:
AWS_PROFILE=default aws sts get-caller-identity
```
Requires `reportlab` + `matplotlib` in the venv (`pip install reportlab matplotlib`).

## Suites (pick by intent)
- `smoke` — 2 cells × 2 tiny docs. Per-PR gate (~minutes, ~$1).
- `core` — 10 decision-relevant cells × ~10 docs. Standard release run.
- `scaling` — simple vs advanced across the row-count series (the cliff study).
- `full` — core + one-axis sweeps of every knob. The deep study for the paper (expensive).

## Workflow
```bash
cd /home/ec2-user/projects/idp1
# 1. build the exact-ground-truth synthetic corpus (deterministic)
python3 benchmarks/harness/gen_corpus.py                 # all synthetic docs
python3 benchmarks/harness/gen_corpus.py --series scaling # just the scaling series

# 2. expand the config matrix into validated v0.6 config variants
python3 benchmarks/harness/make_configs.py --suite <suite> --class bank_statement
#    a suite that names the reference corpora (core_docs -> realkie, ocr_bench) needs
#    their cells too, built onto each corpus's own base config (same --set overrides):
python3 benchmarks/harness/make_configs.py --suite <suite> --class realkie
python3 benchmarks/harness/make_configs.py --suite <suite> --class ocr_bench

# 3. (ALWAYS estimate first — full is expensive) then run against a deployed stack
AWS_PROFILE=default python3 benchmarks/harness/run_matrix.py --stack <STACK> --suite <suite> --estimate
AWS_PROFILE=default python3 benchmarks/harness/run_matrix.py --stack <STACK> --suite <suite> --max-inflight 6

# 4. score + roll up (writes results/<release>/<suite>/summary.{json,csv} + meta.json)
AWS_PROFILE=default python3 benchmarks/harness/aggregate.py --run benchmarks/results/run-<stamp> --out benchmarks/results/<release>/<suite>

# 5. regression-check vs baseline + emit figures
python3 benchmarks/harness/aggregate.py --compare benchmarks/results/<release>/<suite>/summary.json --baseline benchmarks/results/baseline.json
python3 benchmarks/harness/aggregate.py --figures benchmarks/results/<release>/<suite>/summary.json

# 6. update the published papers under docs/benchmarking/ (see "Output docs" below)
```

## Output docs (single source of truth = docs/benchmarking/)

Papers are published from `docs/benchmarking/` (symlinked into the Starlight site by
`docs-site/setup.sh`; sidebar in `docs-site/astro.config.mjs`). Do NOT create parallel
copies under `benchmarks/paper/` — that caused drift and is retired (see
`benchmarks/paper/README.md`).

| File | Purpose | Cadence |
|------|---------|---------|
| `docs/benchmarking/index.md` | how the suite works (design guide) | edit when the harness changes |
| `docs/benchmarking/config-guidance.md` | "which config should I pick?" (cross-config, one release) | refresh per release |
| `docs/benchmarking/releases/vX.Y.Z.md` | **release-vs-release** audit trail (one file per release, never overwritten) | one NEW file per release |
| `docs/benchmarking/releases/README.md` | audit-trail index table | append one row per release |

Figures: `aggregate.py --figures` writes to `benchmarks/paper/figures/` (scratch); copy
the ones you cite into `images/benchmark-<release>-<name>.png`. Docs reference them as
`../../images/...` (from `docs/benchmarking/`) or `../../../images/...` (from `releases/`).

## Release-cycle audit trail — "prev published release vs current develop"

This is the once-per-release deliverable and the reason the harness is version-agnostic.
It creates ONE new `docs/benchmarking/releases/v<NEW>.md` comparing the previous
**published** release to the current **develop** prerelease, on the same stack, with
**byte-identical configs** (only the code version differs). Entry point:
`make benchmark-release VERSION=<new> PREV=<published>` (which just invokes this skill).

Procedure (drive it yourself — several steps need judgment):

1. **Deploy the PREV published release.** Find the public template URL in `README.md`
   (`s3://aws-ml-blog-<region>/artifacts/genai-idp/idp-main.yaml`); confirm its
   Description says `(v<PREV>)`. `idp-cli deploy --stack-name <S> --template-url <url>
   --admin-email <you> --region us-west-2 --wait`.
2. **Generate the `corefast` grid** (`gen_corpus.py`; `make_configs.py --suite corefast`).
   Use `corefast` (≤100-row docs) for the A/B — advanced-mode granular assessment on
   ≥400-row docs times out the 900s Lambda on older releases (retries ~2h then fails).
   `corefast` runs **`repeats: 3`** (90 runs/side): at one sample a non-deterministic
   agentic outcome is indistinguishable from a regression. The v0.6.5 verification
   reported a new FAILURE and a −0.143 accuracy drop from a `repeats: 1` grid and
   **neither reproduced** — do not lower this to save time.
3. **Run on PREV with `--native-upload`** (`run_matrix.py --suite corefast --native-upload`).
   Native-upload is REQUIRED: idp-cli's config-upload force-migrates v0.5→v0.6 and drops
   the top-level `assessment` block older stacks need.
4. **Score** → `benchmarks/results/v<PREV>/corefast/`; **promote to baseline.json**.
5. **Upgrade the SAME stack in place**: `idp-cli deploy --stack-name <S> --from-code .
   --clean-build --region us-west-2 --wait`. Verify `UPDATE_COMPLETE` + Description shows
   `v<NEW>`.
6. **Re-run `corefast` on the upgraded stack** (`--native-upload`, identical config files).
7. **Score** → `benchmarks/results/v<NEW>/corefast/`; **compare** (`aggregate.py --compare` new vs
   PREV) + `--figures`; copy cited charts to `images/benchmark-v<NEW>-*.png`.
8. **Write `docs/benchmarking/releases/v<NEW>.md`** (use the previous release file as the
   template) and **append a row** to `docs/benchmarking/releases/README.md`.

### Cross-version config compatibility the harness handles (do NOT regress these)

Running the v0.6-native suite against an older stack requires these, all already in the
harness — verify they still hold when the schema evolves:
- **Shared control model.** Older clients reject newer models (v0.5.16's bedrock client
  sends deprecated `temperature` → Sonnet 5 `ValidationException`). Hold `extraction.model`
  at a model BOTH versions run (`default_cell.extraction_model`, currently `sonnet46`).
  Capability-only deltas (e.g. "vN unlocks Sonnet 5") are documented, not in the A/B grid.
- **`make_configs.py`** merges each cell with system defaults (so all step prompts are
  populated for old stacks that don't merge custom configs at runtime), re-injects a
  top-level `assessment` block from `compat/v0516-base-assessment.yaml` (old stacks read
  it; v0.6 ignores it via `extra="ignore"`), mirrors `confidence.list_batch_size` into
  `assessment.granular.list_batch_size` (equal Bedrock call counts = fair cost), sanitizes
  non-positive `max_tokens`/`shard_token_budget` (old validators enforce `gt=0`), and
  disables summarization (unscored; its default model hits the temperature bug).
- **`compat/native_upload.py`** writes configs verbatim (bypasses idp-cli migration).
- **`run_matrix.launch()`** invokes the TestRunner Lambda directly (finds it whether it's
  under the pre-migration `APPSYNCSTACK` (v0.5.x) or `APIRESOLVERSTACK` (v0.6)).
- **Validate against the OLD model** before running: `git worktree add -f --detach <wt>
  v<PREV>` then `IDPConfig.model_validate(cfg)` from `<wt>/lib/idp_common_pkg`.
- **Honesty:** report any cell that can't complete on a version (e.g. old-release
  advanced+large-list timeouts) as a finding, not a silent omission.

## Stack setup
- Use a deployed stack you own (e.g. IDPBattery0708). The harness resolves its
  testset/output buckets + tracking/config tables by name prefix.
- It registers `bench-<doc>` test sets and uploads `Config#bench-*` versions.
  **It never mutates `Config#default`.** Clean up afterwards if desired:
  `idp-cli config-delete --config-profile bench-* ` (or leave for the next run).
- BDA and `bedrock_llm` OCR cells require those features enabled on the stack /
  Bedrock model access; cells that can't run are logged, not silently dropped.

## Promoting a baseline
After a release run you trust, copy its summary to the baseline so future runs
compare against it:
```bash
cp benchmarks/results/<release>/corefast/summary.json benchmarks/results/baseline.json
```
Commit `benchmarks/results/<release>/` + the updated `baseline.json` + paper so the
per-release history is maintained in the repo.

**Retention: one complete set per release** — see
`benchmarks/results/RETENTION.md`. Scored files always go in a `<suite>/` subdirectory of
the release dir, never loose in it. Do NOT add a sibling directory for a re-run or a
variant (`v0.6.5-fixed2-…`, `v0.6.6-advverify-post668`): either replace the set in place if
the first attempt was invalid, or write the finding into the `docs/benchmarking/` page and
let the data go (git history is the archive — cite the commit, not a path).

## Confidence calibration (`aggregate.py --calibration`)

It joins each confidence leaf to the cell it scores (by `flatten_confidences` field path
+ the corpus's `SEQ` row tag) and pools true `(confidence, correct)` pairs per
configuration arm through the shipped `ConfidenceCurve`, so the numbers are about the
bars the product acts on:

```bash
# Reads the stored calibration_curve from the committed summaries — NO AWS needed.
PYTHONPATH=<repo>/lib/idp_common_pkg python3 benchmarks/harness/aggregate.py \
  --calibration benchmarks/results/<release>/<suite>/summary.json [more summaries...] \
  --calibration-group assessment,confidence_model,extraction_model \
  --calibration-out /tmp/calibration.json

# Backfill the metric into a grid scored before it existed (needs S3 + truth files).
AWS_PROFILE=default PYTHONPATH=<repo>/lib/idp_common_pkg python3 benchmarks/harness/aggregate.py \
  --augment benchmarks/results/<release>/<suite>/summary.json --corpus <dir with *.truth.json>
```

**The committed `calibration_curve` is an exact sufficient statistic**, so a grid stays
reproducible after its stack is deleted — which has already happened to three v0.6.x
release stacks. `--calibration` prefers it and falls back to S3 only for a row that has
none; `--calibration-from-s3` forces the re-read, which a freshly-scored grid wants and
which verifies the stored statistic. `--augment` adds the metric (and #997's coverage
figure) to an older summary without re-scoring anything priced from DynamoDB metering.

Read the output in this order. **`bins`** first: fewer than 3 populated bins is
`degenerate` — a structural fact, not an estimate, and it means worst-first ordering is
arbitrary regardless of every other column. **`errs`** next: AUROC is estimated over
wrong × correct pairs, so an arm with 70,000 cells and 55 errors is a 55-observation
measurement of discrimination. **`runs` / `docs` / `excl` third** — the arms are not
equally powered (112, 102 and 58 runs in the published grid) and a high `excl` means the
surviving runs are conditioned on success. Only then the statistics. `ECE` is the gate's
midpoint-based estimator and floors at 0.05 on an all-1.00 all-correct curve; `ECEmc` is
the mean-confidence estimator and is the one to quote. `AUROC` is the gate's binned value
(biased low by design); `AUROCu` is the unbinned one and is the one to quote.

⚠️ The `skipped:` line has **four** buckets and the last two are different findings.

*No confidence at all* — the run emitted no confidence leaf. Benign at one per
`confidence.mode: off` cell per document, and **a surplus over that count is also
expected**, from runs whose assessment returned an empty `explainability_info`. The
published v0.6.8 matrix reads 55 against 49 off-cells for exactly that reason, so
"chase it when it exceeds the off-cell count" fires on the page's own output and is the
wrong rule. The exact rule: the surplus should sit in the same arms that also show
`not_success` or `no_joinable_cell` exclusions. A surplus in a clean 112-run arm is
worth chasing.

*Confidence but no joinable cell* — the run completed and produced confidence, and
EXTRACTION returned no `SEQ`-tagged row. Read it as an extraction-completeness figure and
localise it with the per-arm `excl` column. It is neither an S3 failure nor an assessment
failure: no read is attempted for a row whose statistic is stored, and the confidence is
right there. On the published matrix this reads 34, all of it in the two weak-extraction
arms.

⚠️ Group by **extraction model as well as** the grader and the mode. The grader's
calibration depends on the extraction it is grading, so pooling a 50%-wrong arm with a
99.9%-correct one reports neither. `--augment` and `--calibration-from-s3` need the
corpus truth files — pass `--corpus` if `benchmarks/corpus/docs` is not populated, and
note `gen_corpus.py` rewrites the tracked `corpus/manifest.yaml` with absolute paths.

⚠️ **Two ways a `--calibration` run dirties the tree.** The manifest above, and the
reliability diagram: it lands at `benchmarks/paper/figures/reliability-diagram.png`, and
`benchmarks/.gitignore` explicitly **un-ignores** `paper/figures/*.png` (three tracked
PNGs live there), so it appears as untracked rather than being ignored. Revert the
manifest and delete the PNG before committing — and never `rm -rf benchmarks/paper`,
which removes the three tracked figures and the README too.

Measured for v0.6.8/v0.6.9 in
[docs/benchmarking/studies/confidence-calibration.md](../../docs/benchmarking/studies/confidence-calibration.md):
the default Nova Lite grader is well calibrated and **undiscriminating** (one bin,
AUROC 0.417), Sonnet 5 as grader reaches AUROC 0.859 on the same extraction. Do not
re-derive that claim from memory — re-run the command.

## Regression thresholds (in aggregate.py --compare)
accuracy −0.02, cost +15%, any new failure, calibration separation −0.03 → flagged
as regressions. Improvements ≥ +0.02 accuracy are also reported.

`compare_cells` additionally gates the **pooled** per-cell calibration: `ece_mean_conf`
+0.01, **unbinned** AUROC −0.05, or a cell crossing `ECE_UNRELIABLE_THRESHOLD` /
`AUROC_UNRELIABLE_THRESHOLD` at any step size.

⚠️ **For both metrics the magnitude check and the crossing check read different
estimators, and that is not interchangeable.** Magnitude reads the estimator that can
move (`ece_mean_conf`, `auroc_unbinned`); crossing reads the one the shipped product
applies its threshold to (`ece`, binned `auroc`). The gate's own estimators barely move
here: midpoint `ece` compares each bin to its MIDPOINT so confidence moving within a bin
is invisible to it, and it spans 0.0013 across the published grid where `ece_mean_conf`
spans 0.0239; binned `auroc` is *exactly* 0.5000 on 29 of the 33 committed cells where it
is defined at all, and moved 0.0000 on every configuration-matched arm across the two
releases where the unbinned value moved up to 0.0461. A magnitude gate on `ece` is blind
to a real +0.025 worsening and reports a real +0.041 worsening as an *improvement*; a
magnitude gate on binned `auroc` is simply inert on a single-bin curve, which is the shape
the shipped default produces. The crossing arms must nonetheless read the gate's own
values — and note the AUROC crossing arm therefore cannot fire on a cell already at 0.5,
since 0.5 is below the 0.55 bar on both sides.

⚠️ **The +0.01 ECE threshold is justified per ARM and applied per CELL.** Per arm, the
largest drift for an unchanged configuration across the two published releases is 0.0019
over all eight matched arms (0.0008 if you look only at the four `separate` ones, which
overstates the headroom by 2.4×). Per cell, over the 72 matched cell pairs the committed
data supports, 0 of 72 false-positive at 0.01 but median |Δ| is 0.00075 and the largest
worsening is +0.0088. So real headroom is ~1.14× on the worst cell and ~3× excluding
sample collapse, not 14×. ⚠️ **There is no stability-of-`n` guard.**
`MIN_OBSERVATIONS_FOR_MEASURED` is 30, so that +0.0088 — from a cell whose pooled sample
went 4,410 → 410 — clears the floor and is gated on as if it were the larger measurement.
`note()` prints `n 4410->410` beside the finding; the decision to fire ignores it. If you
see a finding whose `n` moved by more than ~2×, treat it as a grid finding first.

⚠️ The gate is **inert whenever either side lacks the metric**, and both directions are
reachable today. `compare_cells` prints a `NOT COMPARED — one side lacks the metric` block
naming the metric, the side and the cells rather than printing nothing, so check for it.
Baseline-side: `--augment` the baseline or re-promote one. **Current-side is the common
case** — 13 of the 96 committed summaries carry a calibration statistic and 83 do not, so
comparing `v<REL>/corefast/summary.json` against `baseline.json` can produce zero findings
and no note purely because the current grid was never backfilled. If its stack or its KMS
key is gone it cannot be, and the gate is permanently inert for those cells.

Two calibration separations are tracked, on the same −0.03 threshold:
`calibration_separation` (extracted FIELDS) and `class_calibration_separation`
(the CLASSIFICATION, from the eval report's per-page `predicted_confidence` vs
`correct`). Both are `mean(conf | right) − mean(conf | wrong)`; both are `None`
when that dimension was not scored, which is the default for classification
(`classification.confidence.mode: off`) — a `None` there means "not measured",
not "perfect". Turn the mode on for any run whose point is to judge whether
classification confidence is worth acting on, and report `class_accuracy` and
`n_class_scored_pages` alongside it so a separation computed from three pages
is not read as a result.

## Honesty
Report failures explicitly; never average accuracy only over docs that completed
without saying so (advanced/large runs are survivorship-sensitive). Costs are
estimates from `config_library/pricing.yaml` (state the rate date). Any capped or
skipped cell must appear in `meta.json`, not vanish.

### A figure that could not be read — or priced — is null, never zero

The harness may continue past something it could not measure; it may not record it as a
value. Every reader returns a three-state `lib.Reading` — present / absent / failed —
and zero is not the sentinel for either empty state, because zero is a real answer for
most of these metrics. Pricing follows the same rule: `lib.price_metering` returns a
`lib.Priced` whose `.total` is unavailable unless every metering entry priced, so an
entry that contributed nothing cannot be read as one that cost nothing. Before quoting a
number from a grid, check the five places this surfaces. All five are null or zero on a
clean run.

| Where | Key | Means |
|---|---|---|
| a row | `cost_unread` | the metering row was not read, so `cost` and `tokens` are null rather than `$0.00` |
| a row | `cost_unpriced` | the metering row read fine and `pricing.yaml` cannot price something in it, so `cost`, `cost_by_phase` and `cost_by_key` are null. The tokens stay — they are what says how much cost is missing |
| a row | `sections_unread` / `sections_unreadable` | some section objects would not read, so the row carries **no** quality metric at all |
| a cell | `n_cost_unread` / `n_cost_unpriced` / `n_sections_unread` | that cell's means are over fewer runs than `n_success` |
| `--calibration` | the `UNREAD` bucket on the `skipped:` line | rows nobody could measure — **not** runs that emitted no confidence |

⚠️ **`cost_unpriced` is the one with no zero to give it away.** A row whose metering
could not be read has every cost figure null. A row carrying an **unpriced model** has
every other phase priced normally, so its total is non-zero and plausible while being
strictly below truth, and no number in the artifact looks wrong. Adding a model arm is
exactly when it happens, so the run introducing the thing being measured is the run at
risk. Check `pricing.yaml` **before** launching a grid with a new model in it; a unit
test covers the `config_matrix.yaml` model axes, but a metering key can also name a
Textract feature or a Lambda hook that no axis mentions.

A unit that a matched pricing entry does **not** list is a different matter and is a
real `$0.00`: `pricing.yaml` omits units that do not apply, and every Bedrock call
meters `totalTokens` and `requests`, which Bedrock does not charge for. That rule is
shared with the product's own cost reporting (`idp_common/reporting/README.md`) and the
two are required to agree, so reporting the unit axis here would both mark every Bedrock
entry in every row unpriceable and put the two implementations out of step.

`--compare` prints the cell-level version under `MEASURED OVER FEWER RUNS THAN IT
LOOKS`. A non-zero count there makes the arm's figures provisional: re-run against a
readable stack before quoting them, and do not publish the arm on the strength of what
happened to decrypt. The case this exists for is a stack whose KMS key has entered
pending deletion — every object present, listable and undecryptable — which is
indistinguishable from an unassessed grid unless the reader says so.
