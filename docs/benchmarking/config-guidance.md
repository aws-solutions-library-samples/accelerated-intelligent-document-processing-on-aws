---
title: "Configuration Guidance"
---

> **This is the evergreen "which configuration should I pick?" paper** — a cross-config
> comparison at the current release. For **release-over-release** comparisons (is the
> upgrade safe / cheaper / faster?), see the [Release Benchmark Audit Trail](releases/).

# GenAIIDP Configuration Guidance — Empirical Guidance for Document Extraction at Scale

**Release:** v0.6.5 · **Region:** us-west-2 · **Stack:** `IDPBench065`
**Models:** extraction Claude Sonnet 5 (the shipped default) · confidence Nova Lite ·
summarization disabled (unscored)
**Pricing:** `config_library/pricing.yaml` (sha256 `bc68d49e…`; rates as of 2026-08; intro
pricing may apply)

> Reproducible via the `benchmarks/` harness (run the `run-benchmarks` skill). Every number
> here is produced by `benchmarks/harness/aggregate.py` from live runs; none are recalled
> from memory. Supporting data: the `coresynth` grid (§2), `scaling` (§3), `cost` (§4), and
> two `intconf` runs on Sonnet 5 and Sonnet 4.6 (§2.1). Per
> [`benchmarks/results/RETENTION.md`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/benchmarks/results/RETENTION.md)
> only one complete set is retained per release, so these slices are no longer in the working
> tree; recover them from git with
> `git checkout ec3eb05ae -- benchmarks/results/v0.6.5-config-core/` (likewise
> `-config-scaling`, `-config-cost`, `-intconf-sonnet5`, `-intconf-sonnet46`).

---

## Abstract

We benchmark the GenAI IDP accelerator across a controlled matrix of **configuration
options** (OCR backend, extraction mode, assessment mode, geometry, model, escalation) and
**document types and sizes** (synthetic documents with exact ground truth). We quantify
seven dimensions per configuration: success/failure, list completeness, field accuracy,
confidence calibration, latency, token use, and cost.

Headline results at v0.6.5:

1. **Extraction mode is primarily a cost/robustness decision, not an accuracy one, until
   documents get large.** On list documents up to **800 rows / 17 pages**, simple mode is
   complete (recall 1.000) and **~4× cheaper** than advanced (mean $0.476 vs $1.967 per
   document over 70 runs).
2. **Simple mode has a hard completeness cliff between 800 and 1,200 rows.** Recall is
   1.000 through 800 rows, then collapses: **0.199 @1,200 · 0.088 @1,600 · 0.009 @3,200** —
   and it is *silent* (status `COMPLETED`, valid-looking partial output, no error).
3. **Advanced (agentic) mode holds perfect completeness (recall 1.000) through 3,200 rows /
   66 pages**, at a steeply growing cost premium ($0.17 → $20.93 per document; **~11×
   simple** at 3,200 rows).
4. **🚨 Do not use `assessment: integrated` with simple extraction on list documents.** At
   the shipped default extraction model it returned **1–10 of 100 rows across 4 of 4
   repeats**, and **none of the returned rows matched ground truth**, while reporting
   `COMPLETED` **and scalar accuracy 1.000**. Across the 7-document grid its mean recall is
   **0.294**; the 800-row document came back with the transaction list **absent entirely**.
   This is the single most dangerous configuration in the matrix (§2.1).
5. **Advanced mode can null an entire list** on a long-free-text document (recall 0.000 on
   `longdesc_100` where simple mode got 1.000) — the agent's tool-decline path, unchanged
   since v0.6.0 (§2, finding 3).
6. **Scalar-field accuracy is 1.000 in every one of the 70 core runs**, and there were
   **0 failures**. Every completeness problem above is a *silent list* problem, invisible to
   field accuracy.

> **What changed between releases** is tracked separately in the
> [Release Audit Trail](releases/) — this paper focuses on *choosing a configuration at the
> current release*.

---

## 1. Methodology (summary)

See `benchmarks/matrices/METHODOLOGY.md` for the full protocol. In brief:
- **Synthetic corpus (exact GT):** generated bank statements whose every transaction row
  carries a unique `SEQnnnnn` tag, so completeness and accuracy are measured exactly, and
  size, row width, list count, text length and OCR noise are controlled variables.
- **Config matrix:** 10 curated *core* cells (the OCR × mode × assessment decision space),
  a two-cell scaling series, and a repeated-measures cost suite.
- **Scoring is resolver-free** (reads S3 + DynamoDB metering directly); costs priced from
  `pricing.yaml`; calibration from `explainability_info` confidence leaves.
- **Reference (real, labeled) corpora were not run for this release** — the numbers below
  are all synthetic-with-exact-ground-truth. The v0.6.0 edition of this paper additionally
  reported RealKIE-FCC ≈0.80 and OCR-Benchmark ≈0.87 weighted accuracy; those are **not**
  re-measured here and are omitted rather than carried forward.

### Configuration axes measured
| Axis | Values |
|------|--------|
| OCR | Textract LAYOUT, Textract TABLES, BDA, Bedrock-LLM |
| Extraction mode | simple (1 call) · advanced (agentic sharding + table tool) |
| Assessment | off · separate (Nova Lite pass) · integrated (inline) |
| Geometry | ocr_only (all cells below) |
| Extraction model | Sonnet 5 (all cells below; the shipped default) |
| Confidence model | Nova Lite (all cells below) |
| Reasoning effort | low (all cells below) |

The one-axis sweeps over geometry / escalation / extraction model / confidence model /
reasoning effort (the `full` suite) were **not** run for this release; §2–§4 vary OCR,
extraction mode and assessment only.

**Axes added since this grid was run** (all have suites; see [index.md](index.md) for which
metric each is judged on). Advice for the ones that have been measured is in §7.

| Axis | Config path | Measured? |
|------|-------------|-----------|
| Enforcement | `extraction.coercion.enabled`, `extraction.validation.{enabled,fail_action}` | partially — see §7 |
| Forcing | `extraction.forced_tool.enabled` | yes — §7 |
| Schema restatement | `extraction.agentic.restate_schema_in_system_prompt` | yes — §7 |
| Prompt schema prose (Simple) | `extraction.forced_tool.drop_prose_schema` | yes — §7 |
| Prompt schema prose (Advanced) | `extraction.agentic.prose_schema` | yes — §7 |
| Section splitting | `classification.sectionSplitting` | yes — §7 |
| Boundary prompt | `classification.task_prompt` (frozen variants, control only) | yes, but underpowered here — !769 is the authority, §7 |
| Classification confidence | `classification.confidence.mode` | not yet (cost unmeasured) |
| Classification model | `classification.model` | held at Sonnet 5 for §7 only |
| OCR DPI | `ocr.image.dpi` | as a control only — §7 |

---

## 2. Configuration matrix (10 cells × 7 synthetic list docs, exact GT)

> **Cell count has since grown.** `core_cells` held 10 entries when this grid was run and
> now holds 19 — the v0.7 feature A/B cells (enforcement, forcing, schema restatement,
> section splitting) were added to it, so `coresynth`/`corefast` are no longer the 70/90-run
> grids described here. Control arms live in a separate `control_cells:` block and are
> excluded from those expansions.

Mean over 7 bank-statement (transaction-list) documents spanning 5 → 800 rows and varying
row width, list count and description length (`tiny_form`, `small_narrow`, `med_narrow`,
`large_narrow`, `wide_400`, `manylists_400`, `longdesc_100`). `recall` = distinct
ground-truth rows recovered ÷ total (exact, via SEQ tags). **70 runs, 0 failures.**

| OCR / mode / assessment | recall | cost/doc | cost CV | mean conf | alert % | wall_s | fails |
|-------------------------|-------:|---------:|--------:|----------:|--------:|-------:|------:|
| Textract TABLES / simple / separate | 1.000 | $0.608 | 0.97 | 0.952 | 4.7 | 268 | 0 |
| Textract TABLES / simple / off | 1.000 | $0.457 | 0.78 | n/a | n/a | 108 | 0 |
| **🚨 Textract TABLES / simple / integrated** | **0.294** | $0.247 | 0.53 | 0.947 | 5.7 | 35 | 0 |
| Textract TABLES / advanced / separate | **0.857** | $1.904 | 1.05 | 0.977 | 1.8 | 252 | 0 |
| Textract TABLES / advanced / integrated | 1.000 | $2.436 | 0.96 | 0.944 | 5.7 | 410 | 0 |
| Textract LAYOUT / simple / separate | 1.000 | $0.514 | 1.02 | 0.996 | 0.0 | 266 | 0 |
| Textract LAYOUT / advanced / separate | 1.000 | $1.745 | 0.75 | 0.986 | 0.0 | 317 | 0 |
| BDA / simple / separate | 1.000 | $0.603 | 0.98 | 0.951 | 4.7 | 263 | 0 |
| BDA / advanced / separate | 1.000 | $1.784 | 0.66 | 0.983 | 0.1 | 290 | 0 |
| Bedrock-LLM / simple / separate | **0.860** | $0.430 | 0.82 | 0.951 | 4.7 | 388 | 0 |

`scalar_accuracy` is **1.000 for every cell and every document**, so it is not shown — and
that is itself the most important caveat in this table: **a cell that lost an entire
transaction list still scores perfect field accuracy.** Cost CV is high across the board
(0.53–1.05) because the 7 documents differ ~160× in row count; §4 measures cost variance
properly, with repeats on a single document.

**Findings**

1. **Simple mode is complete and cheapest at these sizes.** Every simple cell except
   `integrated` and Bedrock-LLM OCR is at recall 1.000 through 800 rows, for $0.43–0.61 per
   document, versus $1.74–2.44 for advanced. Simple is the right default up to the cliff in
   §3.
2. **🚨 Integrated confidence + simple extraction loses lists catastrophically (recall
   0.294).** Per document: on the 800-row document the transaction list is **absent from the
   response entirely**; 400-row documents return 5–10 rows of 400; `longdesc_100` returns 10
   rows of 100 and **none of them match** (§2.1 separates the truncation from the value
   corruption). Only the ≤100-row narrow documents survive. It is also the *cheapest* and
   *fastest* cell ($0.247, 35 s) — because it is doing far less work — which is exactly how
   it can look attractive. Corroborated with repeats in §2.1. Advanced + integrated recovers
   to 1.000 because sharding keeps each call small.
3. **⚠️ Advanced mode nulled an entire list** on `longdesc_100` (recall 0.000, 0/100 rows,
   $0.143 and 33 s — it gave up early), while simple mode returned all 100. That single
   document is the whole 0.857 for `tt-adv-sep`; the other six are 1.000. Same tool-decline
   failure path reported at v0.6.0. **Fixed since**: #668 shipped a retry when the agent
   declines the recommended table tool, and the post-fix `advverify` runs no longer
   reproduce the null (see `docs/benchmarking/releases/`). BDA + advanced did **not**
   reproduce it here either (1.000 across all 7).
4. **LAYOUT-only is the cheapest complete option** (simple $0.514, advanced $1.745) and has
   the best-behaved confidence (mean 0.996, **0%** alert rate). Textract TABLES adds cost
   with no completeness benefit at this scale; enable TABLES for very large multi-page
   tables where it aids recovery, not by default.
5. **BDA is a fully viable OCR alternative** (simple $0.603 / advanced $1.784, both
   complete). **Bedrock-LLM is the cheapest ($0.430) but loses rows** on the larger
   documents — recall 0.403 on `wide_400` and 0.830 on `large_narrow` — and is slow
   (1,123 s on the 800-row document). Pick it for small documents and cost, not for
   completeness.
6. **Separate assessment is the safe confidence default.** It is dense (≈6,650 confidence
   leaves per cell across the grid), well-behaved, and costs ~$0.15/doc over `off`. Note
   `tt-simple-int` produced only **392** confidence leaves — another visible symptom of the
   missing rows.
7. **Advanced mode reports higher confidence and raises far fewer alerts** (`tt-adv-sep`
   0.977 / 1.8%, `bda-adv-sep` 0.983 / 0.1%) than the simple cells (≈0.95 / 4.7%), at
   equal (perfect) scalar accuracy on this corpus.

---

## 2.1 The `integrated` + simple hazard, with repeats

Because the failure is a *partial or empty list* rather than an error, a single run cannot
establish it. The `intconf` suite runs the integrated cell **and** a `separate` control on
the same document (`longdesc_100`, 100 rows) 4× each:

| extraction model | cell | recall per repeat | rows **returned** | rows **matching ground truth** | scalar accuracy |
|---|---|---|---|---|---|
| **Sonnet 5** (shipped default) | simple / **integrated** | 0.000 ×4 | **10, 4, 1, 5** of 100 | **0 of 100 ×4** | **1.000** |
| **Sonnet 5** | simple / separate | 1.000 ×4 | 100 of 100 ×4 | 100 ×4 | 1.000 |
| Sonnet 4.6 | simple / **integrated** | 0.100, 0.100, **1.000, 1.000** | 10, 10, 100, 100 | 10, 10, 100, 100 | 0.500 |
| Sonnet 4.6 | simple / separate | 1.000 ×4 | 100 of 100 ×4 | 100 ×4 | 0.500 |

The **returned** and **matching** columns are separated deliberately, because two distinct
failures stack up at the default model and an earlier draft of this paper conflated them
into a single "0 rows":

- **Truncation.** Only 1–10 of 100 rows come back, varying run to run. On the 800-row
  document (§2) the transaction list is **absent from the response entirely**. Root cause:
  the TopK envelope asks for several guesses *per cell*, so a list that fits comfortably in
  a plain extraction exceeds what the model will emit in one response — and it stops
  emitting rows rather than erroring.
- **Value corruption.** *None* of the rows that do come back match ground truth, because the
  prompt asked for each guess "as short as possible": the model put the document's actual
  text in `G2` and a shortened version in `G1`, and `G1` is what becomes the value. So
  recall reads 0.000 even for the handful of rows returned.
- **At Sonnet 4.6 the cell is bimodal (2 of 4 truncate to exactly 10 rows).** So any
  single-sample measurement of this cell — including the release audit trail's n=1 grid, see
  [releases/v0.6.5.md §3.1](releases/v0.6.5.md) — can land on either outcome and appear to
  show a fix.
- **Nothing in the run reveals any of it.** Status is `COMPLETED` and scalar accuracy is
  1.000, because the scalar fields are extracted correctly either way.

The `separate` control was complete in 8 of 8 runs across both models, at 1.5–2× the
integrated cell's cost. **Use `separate`.**

> **Fixes since this measurement.** The value-corruption cause ("as short as possible") is
> removed and list cells now request a single guess instead of four, which cuts list output
> ~4× and pushes the truncation point out. A separate defect found in the same
> investigation — group/object fields keeping their raw `{G1,P1,…}` candidate dict as the
> extracted value, with no confidence at all — is also fixed. The single-response limit is
> fundamental to this mode, so **the recommendation to use `separate` on list-bearing
> schemas stands**, and these numbers describe the configuration as measured, before those
> fixes.

---

## 3. Scaling: where extraction hits limits (synthetic, exact GT)

![Completeness and cost vs document size](../../images/benchmark-scaling.png)

Simple vs advanced, Textract TABLES + separate confidence, one transaction list of N rows
(~48 rows/page). All 14 runs returned `COMPLETED`.

| rows | pages | SIMPLE recall | simple $ | simple wall | ADVANCED recall | adv $ | adv wall |
|-----:|------:|--------------:|---------:|------------:|----------------:|------:|---------:|
| 25 | 1 | 1.000 | $0.055 | 52s | 1.000 | $0.166 | 48s |
| 100 | 3 | 1.000 | $0.169 | 73s | 1.000 | $0.438 | 177s |
| 400 | 9 | 1.000 | $0.614 | 290s | 1.000 | $1.777 | 389s |
| 800 | 17 | **1.000** | $1.782 | 602s | 1.000 | $3.335 | 407s |
| 1200 | 25 | **0.199** | $1.038 | 312s | 1.000 | $4.974 | 416s |
| 1600 | 33 | **0.088** | $1.169 | 389s | 1.000 | $9.026 | 584s |
| 3200 | 66 | **0.009** | $1.917 | 481s | 1.000 | $20.929 | 1120s |

### Simple mode: a silent completeness cliff between 800 and 1,200 rows

Recall is 1.000 through 800 rows (~17 pages), then collapses — 0.199 @1,200, 0.088 @1,600,
0.009 @3,200. The failure is **silent**: the run reports success and returns a
valid-looking but truncated list. Two properties make it worse than a simple token cap:

- **The recovered prefix *shrinks* as the document grows** — 239 rows recovered at 1,200,
  but only 141 at 1,600 and 30 at 3,200. The call abandons the list *earlier* on bigger
  input, so this is **not fixable by raising `max_tokens`**.
- **Cost goes down, not up, past the cliff** ($1.78 at 800 rows → $1.04 at 1,200). A
  truncated run is *cheaper*, so cost monitoring will not flag it either.

### Advanced mode: completeness holds; cost and wall-clock are the limits

Advanced (agentic sharding) holds **recall 1.000 through 3,200 rows / 66 pages** — sharding
keeps each call small, so neither the truncation nor an input-context limit is hit. The
practical limits are **cost** (up to ~$21/doc at 3,200 rows, ~11× simple) and **wall-clock**
(~19 min at 3,200 rows). Cost grows super-linearly in rows above ~800.

---

## 4. Cost: level AND variance (n=5 repeats, same 400-row doc)

Agentic-advanced cost is **high-variance run-to-run** (the agent's turn count is
non-deterministic), so a single sample cannot resolve a cost difference between configs. The
suite measures cost with repeats and reports mean ± stdev + coefficient of variation (CV); a
cost difference is only trustworthy when it exceeds the sampling spread.

Same document (`med_narrow`, 400 rows / 9 pages), 5 repeats per cell, 25 runs. All returned
`COMPLETED` with **recall 1.000 and scalar accuracy 1.000** — so these are like-for-like cost
comparisons of configurations that all did the job.

| config (OCR / mode / assessment) | cost mean ± stdev | CV | min–max | note |
|----------------------------------|-------------------|---:|---------|------|
| Textract TABLES / **simple** / separate | **$0.618 ± $0.006** | **0.9%** | $0.608–0.621 | deterministic |
| Textract LAYOUT / advanced / separate | $1.776 ± $0.121 | 6.8% | $1.677–1.944 | stable |
| BDA / advanced / separate | $1.835 ± $0.432 | 23.6% | $1.211–2.250 | moderate |
| Textract TABLES / advanced / integrated | $2.785 ± $1.131 | 40.6% | $1.879–4.665 | **high variance** |
| Textract TABLES / advanced / separate | $2.193 ± $1.504 | **68.6%** | $1.159–**4.723** | **high variance** |

**Findings**

- **Simple mode is ~2.9–4.5× cheaper than advanced *and* essentially deterministic** (cost CV
  0.9% vs 6.8–68.6%). For budgeting, simple is both cheaper and predictable; agentic cost must
  be planned as a *range*, not a point.
- **The worst case matters more than the mean.** `tt-adv-sep` ranged **$1.16 → $4.72 on the
  same document, same config** — a 4.1× spread. A capacity or cost model built on one sample
  of an agentic cell will be wrong.
- **LAYOUT / advanced is the cheapest and by far the most stable advanced option**
  ($1.776, CV 6.8%) — clean LAYOUT OCR lets the agent finish in fewer turns than TABLES or
  BDA, which is visible in the variance as well as the level.
- **Why advanced costs more even with the deterministic table tool:** advanced is a multi-turn
  agent loop, and each turn re-sends the growing conversation as *input* tokens. The residual
  premium is the irreducible multi-turn overhead, and its variance is inherent to the loop
  (the turn count is not deterministic).

> Methodology note: run these cells with `--suite cost` (or `--repeats ≥5`); the harness
> flags any cell with cost CV > 0.25 as unreliable-at-current-n, and
> `aggregate.py --compare` only reports a cost regression when the mean shift exceeds the
> combined sampling spread — so agentic noise never masquerades as a regression. That
> That treatment now covers **accuracy and completeness too**, not only cost: `--compare`
> reasons about failure *rates* and mean-vs-spread rather than a single draw
> ([index.md](index.md)). It was cost-only for this edition, which is how a
> non-deterministic completeness swing got reported as an improvement in
> [releases/v0.6.5.md §4](releases/v0.6.5.md).

---

## 5. Recommendations (customer guidance)

| Situation | Recommended configuration |
|-----------|---------------------------|
| Typical documents ≤ ~800 rows / ≤ ~17 pages | **simple mode** (complete, ~4× cheaper) |
| Table-free / forms corpora | **LAYOUT-only OCR** (cheapest complete option; best-behaved confidence) |
| Large multi-page tables (> ~800 rows) | **advanced mode** (guaranteed completeness through 3,200 rows; budget cost as a *range*) |
| Very large docs (> ~3,000 rows / 60 pages) | advanced **and split the document** if feasible (~$21 and ~19 min per document at 3,200 rows) |
| Documents with long free-text cells | **simple + separate**, not advanced — advanced nulled the whole list on `longdesc_100` (§2, finding 3) |
| Confidence needed | **`separate`** assessment. **Never `integrated` with simple extraction** (§2.1) |
| Cheapest OCR, small documents | Bedrock-LLM (**not** for >100-row lists — loses rows, §2 finding 5) |
| Packets holding several documents of the **same** class | `x-aws-idp-multi-instance` on that class (**migrate baselines**), or `x-aws-idp-instance-array` if it already lists them (free) — §7 |
| New corpus, shape unknown | run `extraction.multi_instance_detection` **once** as a diagnostic, act on what it names, then turn it off — §7 |

**Safety notes — all three failure modes are silent and all three are invisible to field
accuracy:**

1. Simple mode truncates large lists while reporting success, and a truncated run is
   *cheaper* than a complete one, so neither status nor cost will alert you. If large tables
   are possible, use advanced mode, or add a schema `minItems` constraint, or reconcile row
   counts downstream.
2. **`integrated` confidence with simple extraction returned 1–10 of 100 rows in 4 of 4
   repeats at the shipped default model, with none of them matching ground truth — and
   `scalar_accuracy` 1.000 with status `COMPLETED` throughout.**
3. **A section holding several documents of the same class returns only the first, and
   per-field accuracy cannot see it** — the fields that came back are scored, and they can
   all be right. A section returning 1 of 3 pay statements scores **1.000**. Nothing in
   §2–§4 of this paper would detect it; only a record count against ground truth, or the
   §7 detection probe, will.

---

## 6. Product improvement backlog (surfaced by this study)

1. **🚨 Integrated confidence + simple extraction returns empty/partial lists (P0).** At the
   default extraction model this is a total loss of list data with no error and perfect
   scalar accuracy. Refuse the `integrated` + simple combination for list-bearing schemas
   (route to a separate confidence pass or sharded advanced), or fail the section loudly.
2. **Silent truncation needs detection, not just documentation (P0).** Both failure modes
   above return `COMPLETED`. Compare extracted row count against schema `minItems` (or an
   OCR-derived row estimate) and surface a completeness warning/metric. Note the recovered
   prefix *shrinks* with document size and cost *falls*, so no existing signal catches it.
3. **⚠️ Advanced list-null on long-free-text tables.** When the agent declines the table
   tool it can return the whole list as `null` (recall 0.000 on `longdesc_100`, and it
   finished in 33 s for $0.14 — visibly less work than a real extraction). Fall back to
   direct row extraction rather than nulling, and treat "list null but OCR shows a table" as
   an error.
4. ~~**Variance-aware comparison for accuracy/recall, not just cost**~~ — **done.**
   `--compare` now uses failure rates and mean-vs-spread for accuracy and completeness, not
   just cost.
5. ~~**`kv_form` doc-class benchmark**~~ — **done.** `kv_form` is a corpus document with its
   own generated class schema and typed ground truth (`--class kv_form`).
6. **Reference-corpus cells in the standard release run.** The `core` suite includes two
   20-document reference corpora, which makes it 470 document runs; that is why this edition
   uses the synthetic-only `coresynth` grid and reports no real-world accuracy number. A
   cheaper sampled variant would keep real-world accuracy in every release.

---

## 7. v0.7 configuration options (measured 2026-09-03)

Measured on stack `IDP1` (us-west-2), develop at v0.6.7.dev5 plus PR #744. These are
**separate measurements from §2–§4** above, which remain v0.6.5 numbers on a different
stack and are not restated here. Costs are estimates from `config_library/pricing.yaml`,
rates as of 2026-09-02.

### `extraction.agentic.restate_schema_in_system_prompt` — safe to turn off

Advanced extraction sends the class schema three times per request; the system-prompt
restatement is a byte-identical duplicate of the tool schema (2,600 of 6,680 schema tokens
on the lending `Payslip` class). The gate was **completeness**, because restating a schema
in prose plausibly aids adherence.

| arm | n | failures | completeness recall | cell accuracy |
|---|---|---|---|---|
| `restate-on` (default) | 6 | 0 | **1.0** (sd 0.0) | 1.0 |
| `restate-off` | 6 | 0 | **1.0** (sd 0.0) | 1.0 |

**Guidance: turning it off costs no completeness — and buys nothing measurable either.**
Observed cost was 12% lower with it off, but at cost CV 0.25–0.43 that is **not resolvable
at n=6**. Per-document token counts cannot measure it either (83k/54k/115k *within* one
arm), because agentic turn count is non-deterministic; the per-*request* saving from static
analysis is the only defensible figure.

⚠️ **Correction.** This entry previously told you to "treat the benefit as context-window
headroom, not dollars". That was wrong, and it is worth stating plainly because #710 and the
knob's own documentation made the same claim. Shard planning budgets against **OCR page text
only** (`sharding.plan_shards`), and the budget is `max_input × (1 - context_buffer)` minus
an output reserve and an image reserve (`sizing.compute_sizing_plan`) — **prompt overhead is
never subtracted**. It is absorbed by the blanket `context_buffer` (default 0.30), so the
reclaimed tokens come off a reserve that is already ~60,000 tokens wide on a 200K-window
model and were already unused; `max_pages_per_shard` (default 5) closes shards on page count
regardless. There is therefore **no shard-count mechanism** behind this knob today, which
explains why no arm of any measurement here found a benefit. Making the budget subtract
measured prompt overhead is
[#775](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/775);
until that lands, treat both #710 knobs as neutral instruments rather than optimisations.

### The schema prose in the task prompt — no completeness or accuracy cost, either path

The other half of #710: the copy of the class schema substituted into the task prompt at
`{ATTRIBUTE_NAMES_AND_DESCRIPTIONS}`, which duplicates a tool schema the request already
carries. Measured on stack `IDPBench066` at commit `cefa28201`, extraction model
Sonnet 4.6, 5 cells × 3 docs (`valuenoise_100`, `longdesc_100`, `small_narrow`) × 3
repeats = 45 runs, **0 failures**.

The two paths get different knobs because they are not equally safe to de-duplicate.
On **Simple** the toolSpec is built from the class schema directly and is lossless (all
37 descriptions on lending `Payslip` survive, including the class's own), so the prose is
pure duplication. On **Advanced** the toolSpec comes from a generated Pydantic model,
which drops the root class description on every class measured and every nested-group
description — so `names` removes information the model has nowhere else, and `minimal`
restates exactly that and nothing else. All three Advanced arms hold
`restate_schema_in_system_prompt: off`, so they differ by ONE copy of the schema rather
than two; that also means their costs are not comparable to `restate-on` above.

| arm | n | failures | completeness recall | cell accuracy | cost | cost CV | rendering applied |
|---|---|---|---|---|---|---|---|
| Simple `prose-keep` (default) | 9 | 0 | **1.0** (sd 0.0) | **1.0** (sd 0.0) | $0.165 | 0.08 | forced-tool honored 1.0 |
| Simple `prose-drop` | 9 | 0 | **1.0** (sd 0.0) | **1.0** (sd 0.0) | $0.158 | 0.06 | 15 sections `names`, 0 kept |
| Advanced `prose-adv-full` (default) | 9 | 0 | **1.0** (sd 0.0) | **1.0** (sd 0.0) | $0.591 | 0.51 | — |
| Advanced `prose-adv-minimal` | 9 | 0 | **1.0** (sd 0.0) | **1.0** (sd 0.0) | $0.595 | 0.63 | 13 sections `minimal` |
| Advanced `prose-adv-names` | 9 | 0 | **1.0** (sd 0.0) | **1.0** (sd 0.0) | $0.614 | 0.53 | 14 sections `names` |

**Guidance: dropping the prose costs no completeness and no accuracy on this corpus, on
either path.** Both defaults stay as they are because a single 100-row corpus is not
grounds for changing what every deployment sends; the knobs exist so you can measure the
same question on your own documents.

The **rendering applied** column is what makes that readable, and it is the same
instrument as `forced_tool_honored_rate`: `prose_schema_modes` counts the sections that
actually rendered each mode, and `prose_schema_kept` counts those where a requested drop
was **not** applied because no toolSpec was on the wire. Both are 0-kept here, so every
arm genuinely ran. Without them an arm that never applied would report a confident
"no effect" — and that is not hypothetical: `drop_prose_schema` is deliberately inert
with `forced_tool.enabled: false`, which is the shipped default.

Two honest limits:

- **`scalar_accuracy` deviates, in the DEFAULT arm's favour of nobody.** `prose-keep`
  scored 0.833 against 1.0 for all four other arms — 3 of its 9 runs missed one of **two**
  scalar fields. That denominator is the known artifact the `midetectlong` suite exists
  for: one field wrong moves the cell mean by 0.5 on that document. It is in the direction
  that would make dropping look *better*, and `cell_accuracy` (typed per-row truth, the
  strong measure) is 1.0 with sd 0.0 in every arm — so this is not evidence that dropping
  helps, and it is reported here rather than dropped from the table.
- **The cost figures do not support a claim, and the Advanced ones especially not.** The
  Simple pair moves −4.8% at CV 0.06–0.08, which is the tightest cost pair in this
  section, but at n=9 that gap is still inside sampling error (t≈1.4). The Advanced arms
  sit at CV 0.51–0.63 and their ordering (`full` < `minimal` < `names`) is the *opposite*
  of the token ordering, which is what a non-deterministic agent turn count looks like.
  **The defensible figure is the per-request static saving**, from the token counts below;
  treat measured dollars as corroboration at best. The `kv_form` arm (a different class,
  run separately) was not run.

**Follow-up (`prosecost`, sequential): the cost and latency gaps were queueing artifacts.**
The grid above ran at `--max-inflight 6`, so arms interleaved on a shared stack. Re-running
the Simple pair at `--max-inflight 1` with `repeats: 5` — which satisfies
`config_matrix.yaml`'s own `repeats >= 5` rule for a cost claim — removes that confound, and
both apparent gaps collapse:

| arm | n | fail | recall | cell acc | wall_s | wall CV | cost | cost CV |
|---|---|---|---|---|---|---|---|---|
| `prose-keep` | 5 | 0 | 1.0 | 1.0 | 137.0 | 0.12 | $0.1588 | 0.021 |
| `prose-drop` | 5 | 0 | 1.0 | 1.0 | 127.6 | 0.05 | $0.1576 | 0.011 |

Wall-clock **−6.9% (t=1.17)** and cost **−0.8% (t=0.72)** — neither resolvable at n=5+5.
Compare the interleaved grid, which showed −32% wall-clock and −4.8% cost: those were
scheduling noise, not the feature. Note the CVs here are 3–8× tighter than in any other
measurement in this section (cost CV 0.011–0.021 against 0.25–0.63 elsewhere), so this is a
**strong** null rather than an underpowered one — a real effect of the size the first grid
suggested would have been unmissable.

**Conclusion: neither knob has a measurable cost, latency or completeness effect.** With
`plan_shards` ignoring prompt overhead (above), that is the expected result rather than a
surprising one, and it is why both ship defaulting to current behaviour.

Per request on lending `Payslip` at ~4 chars/token:

| copy | tokens |
|---|---|
| prose `full` (default) | 1,485 |
| prose `minimal` | 372 |
| prose `names` | 176 |
| toolSpec, Simple path | 1,173 |
| toolSpec, Advanced path | 1,612 |

All copies sit inside the prompt-cache prefix, so the dollar effect is ~a tenth of the
token count by construction. And it does **not** reduce shard count: `plan_shards` budgets
against OCR page text only, and `compute_sizing_plan` never subtracts prompt overhead — it is
absorbed by the blanket `context_buffer`, so the reclaimed tokens come off a reserve that was
already unused ([#775](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/775)). That is the mechanism explaining
why no arm above showed a benefit, and until #775 lands these knobs are neutral instruments
rather than optimisations. Changing the setting changes the cached prefix, so the first
request per class after a change is one cache miss.

⚠️ **Do not read this as "safe on any corpus".** The gate #710 names is adherence on
list-heavy documents, and `longdesc_100` is in this grid for exactly that reason — but it
is one synthetic 100-row document per doc type. A corpus whose classes carry heavy
per-field prose, or whose model follows a prose schema more closely than a tool schema,
could behave differently. Run the `proseschema` suite on your own documents before
turning either knob on in production.

### `extraction.forced_tool.enabled` — leave off; measured, buys nothing here

Declares the class schema as a required Converse tool instead of describing it in prose.
Measured on a quiesced stack at Sonnet 5, 3 repeats:

| arm | n | failures | completeness recall | cell accuracy | honored rate | cost |
|---|---|---|---|---|---|---|
| `force-off` | 6 | 0 | 1.0 every run | 1.0 | — | $0.259 |
| `force-on` | 6 | 0 | **1.0 every run** | 1.0 | **1.0** | $0.226 |

On `kv_form` both arms score `typed_accuracy` 1.0 with 0 failures.

**Forcing is honored on every run and makes no measurable difference to accuracy or
completeness** — the null hypothesis the feature was built to test. Cost moves in
opposite directions on the two classes (−13% and +20%) at n=3–6, so there is nothing to
claim there either.

The **honored rate** is what makes that readable: without it, "forcing had no effect" and
"forcing quietly fell back to the prompt" are indistinguishable in the output.

**Guidance: leave it off.** It is not broken and not harmful, it simply buys nothing on
this corpus. What it buys in principle is unchanged — a malformed-JSON parse failure
becomes structurally impossible for the fields the schema declares — so if your corpus
produces parse failures it may be worth measuring there.

> Two earlier attempts at this A/B measured defective code and are void: Bedrock rejects
> a schema whose `$id` is not a URI-reference (IDP sets it to the class name), and a
> model that nests its answer under a `fields` key had the entire extraction dropped as
> an off-schema field — recall 0.0 while reporting COMPLETED. Both fixed in PR #744; this
> run is the live confirmation, the same cells going recall **0.167 → 1.0**. Details:
> `benchmarks/results/v0.6.7/forcing/FINDINGS.md`.

⚠️ **Not a verdict on tool use generally.** Advanced (agentic) extraction has always used
tool-based structured output and scored completeness recall 1.0 across all 12 runs of the
restatement A/B above.

### `classification.sectionSplitting` — `disabled` is not a workaround for packets

Boundary detection judged on `sections_correct` (1.0/0.0 per run, so the mean over 5
repeats is the pass rate), classification model Sonnet 5.

| cell | one 3-page statement | same, paginated | two statements in one file |
|---|---|---|---|
| `llm_determined` (default) | **1.00** | **1.00** | **1.00** |
| `disabled` | 1.00 | 1.00 | **0.00** |

`disabled` is correct by construction on a single document and **wrong by construction on a
packet** — it emits one all-pages section where two are correct, losing the split silently
(completeness recall stays 1.0, so nothing else reports it). Do not recommend it to avoid
over-splitting.

### `x-aws-idp-multi-instance` and `extraction.multi_instance_detection` — the same-class packet

The failure `sectionSplitting: disabled` exposes above has a second, quieter form, and it
is the one no metric in §2–§4 can see. When one section holds several records of the
**same** class, classification has no type change to split on; the class schema describes
one document; the model answers with one object; records 2..N are simply absent. Section
`SUCCESS`, document `COMPLETED`, `ProcessingIssueCount: 0`.

**Why every accuracy number in this paper is blind to it.** Per-field accuracy scores the
fields that came back. A section that returns 1 of 3 pay statements can score **1.000 on
every field it returned**. So a corpus with this shape can look perfect at the top of a
report while a third of its data never left the page.

Two settings, and they answer different questions:

| setting | scope | what it does | cost |
|---|---|---|---|
| `extraction.multi_instance_detection.enabled` | global, per config profile | asks the model, in the same inference, how many documents of the class the pages hold; warns when that exceeds the records extracted | input **+1.8 %**; **−1.3 accuracy points** on a corpus with nothing to find |
| `x-aws-idp-multi-instance: true` | per class | makes the class's effective schema a **list** of that class, so all records are extracted | ⚠️ changes output shape — **evaluation baselines must be migrated** |
| `x-aws-idp-instance-array: <prop>` | per class | names an array the class **already** has as its instance axis | none — read-only, no schema or output change |

**Does the transform actually recover the records? Yes.** `twodocs_2x20` — two complete
statements in one forced section, globally unique `SEQ` tags so completeness is exact,
`repeats: 3`:

| cell | wrapper | rows extracted | recall | scalar accuracy |
|---|---|---|---|---|
| `mi-silent` | off | 40 | 1.00 | 1.00 |
| `mi-detected` | off | **20** | **0.50** | 1.00 |
| `mi-wrapped` | **on** | 40 | **1.00** | **1.00** |

⚠️ **`mi-silent`'s recall 1.00 is the trap, not the control.** It reached 40 rows by
merging *two accounts' transactions into one statement's list* — higher recall,
semantically wrong data, no warning. A completeness metric therefore **prefers the arm
that is quietly wrong**, which is worth sitting with before trusting recall alone on any
packet corpus.

**Detection's cost, measured on two real labeled corpora** (Test Studio, 80 paired runs,
identical documents per arm, only the toggle differing):

| corpus | accuracy off → on | sign test | input tokens |
|---|---|---|---|
| `OmniAI-OCR-Benchmark` (40 docs) | 0.9380 → 0.9461 | p = **1.000** (no effect) | +1.82 % |
| `RealKIE-FCC-Verified` (40 docs) | 0.7678 → **0.7552** | worse on 14 of 40, better on 1, p = **0.001** | −0.80 % |

Detection counted correctly on every multi-record document it saw: on the bank-check
images, 18 flagged of 18 multi-check sheets, 0 false alarms on the 22 single-check sheets,
and the **exact** count right 18 of 18 (2 to 8 checks).

⚠️ **But a warning is not evidence of data loss, and this same run is the cautionary
example.** Counting the extracted rows on those 18 documents afterwards: **0 checks
missing**, in both arms. `BANK_CHECK`'s schema is a single `checks` array, so the class
already modelled several checks per sheet — nothing was collapsing. The warning fired
because `instance_extracted_count` is **1** for a class that declares no instance axis,
which is true whether the records are absent *or* present inside a declared array. The
predicate cannot tell those apart. The finding was real and worth acting on — the preset
now sets `x-aws-idp-instance-array: checks` — but it was a **configuration** finding, not
a data-loss one. An earlier draft of the feature study claimed the latter and was wrong.

**Guidance:**

1. **Run detection once as a diagnostic on any new corpus**, then turn it off. That is how
   the `BANK_CHECK` missing instance axis was found, and it costs one run.
2. **When it fires, look at the extracted data before concluding anything was lost.** If
   the records are inside an existing array, set `x-aws-idp-instance-array` — free. If they
   are genuinely absent, set `x-aws-idp-multi-instance: true` **and migrate the baselines**
   (`scripts/migrate_multi_instance_baselines.py`), or the class scores ~0 with no error
   anywhere.
3. **Leave detection on permanently only** where a section can hold several documents of
   one class **and** the class schema describes only one. That conjunction is what loses
   records; either half alone does not. On a single-record corpus it is ~1.3 accuracy
   points for nothing.
4. `sectionSplitting` is **not** an alternative here. `disabled` makes it worse (above),
   and `llm_determined` cannot split what has no type change to split on — which is the
   whole premise of the failure.

Suites: `multiinstance`, `midetect`, `midetectlong`, `migate`
(`benchmarks/matrices/config_matrix.yaml`). Full study, including two documented wrong
conclusions and how each was caught: [`feature-multi-instance.md`](feature-multi-instance.md).

### The `<boundary-detection-rules>` prompt block (#653) — keep it

**Validated by GitLab !769**, which measured the same rules on **DocSplit-Poly-Seq**:
500 packets, 7,330 pages, 2,027 sections, 5,000 packet-runs, five models, 0 failures.
Split accuracy on multi-section packets improves on four of five models and regresses
on none — Qwen3-VL +0.117, Opus 5 +0.040, Nova 2 Lite +0.030, Sonnet 5 +0.013 (all
p<0.05), gpt-5.6-sol +0.004 (ns). Under-split rate is 0.000 in all ten cells, so the
anti-over-merge clause holds at scale, and page-level *class* accuracy moves at most
0.015 — the change touches boundaries only. On #653's reported 2-page form Sonnet 5
goes 6/24 → 10/10; on a 4-page packet of two copies of one form, 1/10 → 5/5.

⚠️ **Still incomplete**: an unpaginated multi-page document is split roughly 40% of the
time even with the fix, because the rules lean on pagination markers — corpora whose
scans lack them benefit least. Raising `classification.contextPagesCount` is not the
answer (0/5 on the 4-page two-copies packet, by merging all four pages). The block sits
inside the prompt-cache prefix, so it is not re-billed per page.

⚠️ **A customized `classification.task_prompt` wins over the default**, so a stored
custom prompt does not receive this fix — re-apply it or reset to the default. The
presets that pin their own prompt are synced, with a guard test
(`scripts/tests/test_classification_prompt_copies_in_sync.py`).

> **A local factorial here found nothing, and that was a measurement failure, not a
> result.** 90 runs over prompt × `classification.confidence.mode` × `ocr.image.dpi`
> scored 1.00 in all six arms — but on **Sonnet 5**, whose true effect !769 puts at
> +0.013, across three clean synthetic documents at n=5. That test has no power at
> that effect size. It also led me to "retract" a 0% → 60% figure that had been
> measured on **Nova 2 Lite**, which was a cross-model comparison rather than a
> retraction; !769 independently measures that case at 0/5 → 3/5. Details and the
> corrected write-up:
> `benchmarks/results/v0.6.7/boundary-factorial/FINDINGS.md`. Measure boundary work on
> DocSplit-Poly-Seq, not on this corpus.

### `classification.confidence.mode` — measured; worth it depends on the classifier

Defaults to `topk` as of v0.7, spending output tokens on **every page**. Measured under
#673 on **DocSplit-Poly-Seq** (20 documents, 298 pages per model, `topk` + an `off`
control, stack `IDPBench066`):

| model | mode | cost/page | output tok/page | class accuracy | calibration separation |
|---|---|---|---|---|---|
| Nova 2 Lite (default) | `topk` | $0.000901 | 198.5 | 0.846 | **0.044** |
| Nova 2 Lite | `off` | $0.000767 | 154.1 | 0.832 | — |
| Claude Haiku 4.5 | `topk` | $0.005728 | 352.4 | 0.852 | **0.207** |
| Claude Haiku 4.5 | `off` | $0.004985 | 267.9 | 0.859 | — |

**Cost is ~+17% of the classification step** on the default classifier, which is a small
share of a typical bill because classification is cheap next to extraction — but it
scales with **page count**, not section count.

**The number that decides whether to pay it is the separation, and it is
model-dependent.** On Nova 2 Lite it is **0.044**: mean confidence 0.947 when the page
is classified correctly against 0.903 when it is wrong, with the median 0.95 in *both*
cases — a score that can barely rank right from wrong, and one you cannot usefully
threshold. On Haiku 4.5 the same setting yields **0.207**, which is actionable.

**Guidance:** if you route classification through Nova 2 Lite and intend to *act* on the
score — threshold it, queue pages for review — measure the separation on your own corpus
first, because the default classifier's is near zero. If you only want the score for
after-the-fact triage, or you classify with a stronger model, it is cheap enough to
leave on. `mode: off` returns the previous behaviour. Full analysis:
[classification.md](../classification.md) § Classification Confidence.

---

## Appendix A — Data & reproduction

- Per-(cell,doc) scores — `summary.{json,csv}` for the `coresynth` (§2), `scaling` (§3),
  `cost` (§4) and two `intconf` (§2.1) runs. Pruned per
  [`RETENTION.md`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/benchmarks/results/RETENTION.md)
  (one complete set per release); restore all five from git with:
  ```bash
  git checkout ec3eb05ae -- benchmarks/results/v0.6.5-config-core/ \
      benchmarks/results/v0.6.5-config-scaling/ benchmarks/results/v0.6.5-config-cost/ \
      benchmarks/results/v0.6.5-intconf-sonnet5/ benchmarks/results/v0.6.5-intconf-sonnet46/
  ```
- Figures: `images/benchmark-scaling.png`
- Corpus manifest + generators: `benchmarks/corpus/` (regenerable; PDFs/configs gitignored)
- Matrices + methodology: `benchmarks/matrices/`
- Measured spend for this edition: **$75.08** (§2, 70 runs) + **$47.39** (§3, 14 runs) +
  **$46.03** (§4, 25 runs) + **$3.37** (§2.1, 16 runs) = **$171.87** over 125 document runs,
  priced from `pricing.yaml`.

```bash
source .venv/bin/activate && export PYTHONPATH=$PWD/lib/idp_common_pkg
python3 benchmarks/harness/gen_corpus.py

# §2 cross-config grid, §3 scaling, §4 cost variance — at the PRODUCT DEFAULT model
# (the committed default_cell holds extraction_model at the cross-version A/B control,
#  so a single-release study overrides it explicitly)
for s in coresynth scaling cost; do
  python3 benchmarks/harness/make_configs.py --suite $s --class bank_statement --set extraction_model=sonnet5
  AWS_PROFILE=default python3 benchmarks/harness/run_matrix.py --stack <STACK> --suite $s --native-upload --max-inflight 5
  AWS_PROFILE=default python3 benchmarks/harness/aggregate.py --run benchmarks/results/run-<stamp> --out benchmarks/results/v0.6.5/$s
done

# §2.1 the integrated-confidence hazard, with repeats + same-doc control
python3 benchmarks/harness/make_configs.py --suite intconf --class bank_statement --set extraction_model=sonnet5
AWS_PROFILE=default python3 benchmarks/harness/run_matrix.py --stack <STACK> --suite intconf --native-upload

# §7 the #710 prose-schema A/B (both paths). `prose_schema_modes` in the summary is
# what confirms each arm applied; an arm reporting only `full` never ran.
python3 benchmarks/harness/make_configs.py --suite proseschema --class bank_statement
AWS_PROFILE=default python3 benchmarks/harness/run_matrix.py --stack <STACK> --suite proseschema --max-inflight 6
AWS_PROFILE=default python3 benchmarks/harness/aggregate.py --run benchmarks/results/run-<stamp> --out benchmarks/results/<rel>/proseschema
# ...and the cost/latency follow-up SEQUENTIALLY. --max-inflight 1 is the point: at 6 the
# arms interleave on a shared stack and the queueing shows up as a 32% wall-clock "effect".
python3 benchmarks/harness/make_configs.py --suite prosecost --class bank_statement
AWS_PROFILE=default python3 benchmarks/harness/run_matrix.py --stack <STACK> --suite prosecost --max-inflight 1
AWS_PROFILE=default python3 benchmarks/harness/aggregate.py --run benchmarks/results/run-<stamp> --out benchmarks/results/<rel>/prosecost

# §7 multi-instance: the transform on a same-class packet, and the detection A/B
for s in multiinstance midetect migate; do
  python3 benchmarks/harness/make_configs.py --suite $s --class bank_statement
  AWS_PROFILE=default python3 benchmarks/harness/run_matrix.py --stack <STACK> --suite $s --max-inflight 6
  AWS_PROFILE=default python3 benchmarks/harness/aggregate.py --run benchmarks/results/run-<stamp> --out benchmarks/results/<rel>/$s
done

# §7 detection on REAL labeled corpora — via Test Studio, because run_matrix.py silently
# skips reference corpora (GitHub #766). Two profiles per corpus differing ONLY in
# extraction.multi_instance_detection.enabled; numberOfFiles takes the same first N.
python3 benchmarks/harness/detection_ab_teststudio.py --stack <STACK> launch --n 40 \
    --pair ocr-benchmark:mid-off-ocr:mid-on-ocr \
    --pair realkie-fcc-verified:mid-off-rk:mid-on-rk
python3 benchmarks/harness/detection_ab_teststudio.py --stack <STACK> analyse
```

⚠️ **A detection warning count is not a data-loss count**, and §7 records how that error
was made here. To turn flags into a loss figure you must count the extracted records
against ground truth — see
`benchmarks/results/v0.6.7/detection-real-corpora/extracted_vs_ground_truth.txt` for the
query that settled it.

**Honesty / limits.** Costs are estimates from `pricing.yaml` (rates as of 2026-08; intro
pricing may apply). §2 is one run per (cell, doc) — reliable for the *exact* completeness and
accuracy measures, not for per-cell cost, which is what §4 is for. No reference (real,
labeled) corpus and no one-axis sweeps (geometry, escalation, models, reasoning effort) were
run for this release; those sections are omitted rather than carried forward from v0.6.0.

---
> See the [Benchmarking Guide](./index.md) for how this suite is designed and run,
> the [Release Audit Trail](releases/) for release-over-release comparisons, and the
> [Extraction Scaling Guide](../extraction-scaling-guide.md) for size-based mode selection.
