---
title: "Configuration Guidance"
---

> **This is the evergreen "which configuration should I pick?" paper** — a cross-config
> comparison at the current release. For **release-over-release** comparisons (is the
> upgrade safe / cheaper / faster?), see the [Release Benchmark Audit Trail](releases/).

# GenAIIDP Configuration Guidance — Empirical Guidance for Document Extraction at Scale

**Release:** v0.6.12 · **Region:** us-west-2 · **Stack:** `IDPUpg0611to0612` (created from the
published `idp-main_0.6.11.yaml` and upgraded in place to the published `idp-main_0.6.12.yaml`;
the v0.6.12 record in [release-validation](../release-validation/) carries the stack's
provenance and the failure census these runs contributed to)
**Models:** extraction Claude Sonnet 4.6 — **the cross-version control, not the shipped
default** — in §2–§4 and §7; the extraction, classification and confidence models are
*varied* in §5 · classification Nova 2 Lite (the shipped default) · confidence Nova Lite
(the shipped default) · summarization disabled (unscored)
**Pricing:** `config_library/pricing.yaml` (sha256 `44a79aa06c15…`; rates as of 2026-10; intro
pricing may apply)
**Measured:** 2026-10-17 23:37 → 2026-10-18 06:22 UTC, in one session, on one stack —
**3,248 scored rows from 2,488 runs across 29 result sets**, including the first
measurement of the two reference corpora since the v0.6.8 edition and the first of any
kind for Claude Sonnet 5.5.

⚠️ **Three cost figures in §5 are lower bounds, not measurements, and no cost *ratio* is
published for any of the three models.** Where §5 does compare one of them on cost, it
states two dollar figures from the same grid and a **one-sided bound** over them ("at
least X% less") — which is what a lower bound supports and a ratio is not.
`pricing.yaml` holds one flat rate per model and
cannot express a usage-dependent band, and three swept models need one:

- **Claude Haiku 5.5** is billed 5× on every token type above 100,000 input tokens, and
  that threshold is crossed in practice here.
- **GPT-6 Astra** moves to a long-context band above 272,000 input tokens.
- **Claude Sonnet 5.5** is a different problem with the same consequence: its published
  per-token input and output prices are **identical to Sonnet 5's** ($2.20/$11.00 per
  MTok, `us.`/`eu.`), differing only on cache reads ($0.11 vs $0.22) — but the Sonnet 5
  rows in `pricing.yaml` carry Sonnet 4.6's rates by a separately-tracked decision, so a
  report shows Sonnet 5.5 as roughly a third cheaper than Sonnet 5 when the price list
  says they are the same. Compare those two from the AWS price list, never from here.

Every other arm in the sweep has a single flat rate and its cost figure is a true
measurement. See each model's row in
[Benchmarking](index.md#which-models-are-actually-measured).

### What this edition re-measured, and what it did not

Every number below is either re-measured on v0.6.12 or explicitly marked as carried over. Read
this table before citing a figure.

| Section | On v0.6.12? | Data |
|---|---|---|
| §2 configuration matrix (19 cells × 7 synthetic docs) | ✅ re-measured | `core` synthetic slice (133 runs, exact ground truth) |
| §2c real-corpus accuracy | ✅ **re-measured, ONE arm** | `core` reference slice — **756 scored rows over 760 documents** across both corpora, at the **control** model. ⚠️ The *two-model* comparison the previous editions published is **not** refreshed: only the control arm ran, so the Sonnet-5 side would be a cross-release figure. Treat §2c as a cross-configuration result on this release, not a model comparison |
| §2.1 integrated + simple hazard | ✅ re-measured | `intconf` (8 runs) — **closed**, 8/8 complete |
| §3 scaling | ✅ re-measured | `scaling` (14 runs, 25→3,200 rows) |
| §4 cost level and variance | ✅ re-measured | `cost` (control) and `cost__extraction-model-sonnet5`, n=5 each |
| §5.1 extraction-model sweep | ✅ **all 9 models** | `coresynth__extraction-model-*` for `sonnet5`, `sonnet5_1m`, `opus5`, `opus55`, `astra`, `haiku55`, **`sonnet55`** (133 runs each); `simplegrid__extraction-model-*` for `nova_lite` and `nova_pro` (63 each — neither finishes the agentic path, #895), with `simplegrid` as their comparable control |
| §5.2 premium head-to-head | ✅ **all three pairs** | `astravalue` (100 runs), `astracap` (12), and **`opus55value` (40) — run for the first time** |
| §5.3 classification model | ✅ **all 3** | `nova_2_lite` (default), `sonnet5`, `haiku55`, 133 runs each |
| §5.4 confidence model | ✅ **all 3** | `nova_lite` (default), `nova_2_lite`, `sonnet5`, 133 runs each |
| §6 standing hazards | ✅ re-measured | `intconf`, `advverify__extraction-model-sonnet5` — **both closed**, 8/8 each |
| §7 knob A/Bs | ⚠️ **4 of the set** | `enforcement`, `enforcecost`, `forcing` and the restatement arms inside `core` were re-measured. **`splitcost`, `advsplitcost`, `boundaryab`, `multiinstance` and `sizerab` did not run**, so those subsections carry figures from earlier editions and say so in place |

⚠️ **`typed_accuracy` is absent from every table in this edition, and that is a gap rather
than a choice.** The metric is emitted as `null` on all 3,248 scored rows
([#1490](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1490)),
so no per-model typed-accuracy column can be produced. It is the one metric that would
separate a value extracted with the right *type* from one that merely matches as a
string.

Appendix A lists the exact directory behind every section.

> Reproducible via the `benchmarks/` harness (run the `run-benchmarks` skill). Every number
> here is produced by `benchmarks/harness/aggregate.py` from live runs; none are recalled
> from memory. The data for every section is in the working tree under
> `benchmarks/results/v0.6.12/` — see Appendix A for the exact directory per section. The
> one exception is the `restate_schema_in_system_prompt` axis in §7, which was
> re-measured on its own grid at v0.6.10 (`benchmarks/results/v0.6.10/restate710*/`,
> Sonnet 4.6, 25 runs per arm) and is written up in
> [studies/schema-restatement-tokens.md](studies/schema-restatement-tokens.md); its rows
> carry that date. Per
> [`benchmarks/results/RETENTION.md`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/benchmarks/results/RETENTION.md)
> only one complete set is retained per release, so the v0.6.8 slices this edition replaces
> are in git history (`git checkout <sha> -- benchmarks/results/v0.6.8/`).
>
>
> **What changed in the measurement itself since the v0.6.8 edition.** One thing, and it
> matters for accuracy comparisons across editions: `stickler-eval` moved 0.5.0 → 1.0.0 and now
> infers evaluation comparators **per field**, by name as well as type, so an un-annotated
> schema no longer scores every string `FUZZY@0.85`. Accuracy figures in this edition are not
> like-for-like with the v0.6.8 edition's, even where the underlying behaviour is identical.
> Cost is unaffected: the harness prices the raw metering map itself with exact key matching
> from the `pricing.yaml` named above, so the two cost-reporting fixes in this release do not
> reach these numbers.

## Abstract

We benchmark the GenAI IDP accelerator across a controlled matrix of **configuration
options** (OCR backend, extraction mode, assessment mode, the v0.7 feature arms, and — new in
this edition — the extraction, classification and confidence **models**) and **document
types and sizes** (synthetic documents with exact ground truth, plus two real labelled
corpora). We quantify seven dimensions per configuration: success/failure, list
completeness, per-row field accuracy, confidence calibration, latency, token use, and cost.

Headline results at v0.6.9:

1. **The over-splitting that shaped the v0.6.7 edition is gone, and with it most of the
   agentic cost premium's *variance*.** Every one of the 133 grid runs produced exactly one
   section per document (the truth). At the cross-version control model (Sonnet 4.6) the
   same-document cost spread of advanced mode fell from CV 5–47% at v0.6.7 to **3–13%**; at
   the shipped default, Sonnet 5, it is still **25–40%** — so the remaining agentic
   unpredictability is the model's turn count, not the classifier (§4).
2. **Completeness and per-row accuracy remain solved for Textract-backed cells in both
   modes up to 400 rows, and the two v0.6.7 hazards are closed.** `integrated` confidence
   with simple extraction — 0.294 at v0.6.5, 0.936 at v0.6.7 — is **1.000 on all 7 documents
   and 8 of 8 repeats**, at the same price as `separate` ($0.729 vs $0.721/doc), because
   list-bearing classes are now routed to a separate pass automatically (#795). The
   advanced-mode tool-decline list loss is **0 of 8** (§2.1).
3. **🚨 Simple mode's true ceiling is now visible: ~800 rows / 17 pages is a coin flip, and
   it depends on the OCR backend.** With one section per document the single-response
   limit is no longer masked. Textract TABLES + Sonnet 5 completed the 800-row document in
   **8 of 11** draws across three suites at v0.6.8 and **1 of 2** at v0.6.9; the same
   document under BDA OCR, Bedrock-LLM OCR, forced tool use, or Sonnet 5 `:1m` returned
   **43–92 of 800 rows** with status `COMPLETED`. ⚠️ **On v0.6.9, above 800 rows nothing
   refuses.** Where v0.6.8 rejected 1,200+ rows outright, v0.6.9 returns 43–101 of
   1,200–1,600 rows as `COMPLETED`; only 3,200 rows exceeds the input window. From 0.6.10
   `extraction.row_shortfall_action: fail` makes such a run resolve to `FAILED` again, after
   writing the rows it did get — opt-in, for the reason in §3. §3 also has the arithmetic
   isolating why v0.6.8 refused: the size of the request its page images made, not the token
   window, which is why no input-size gate could have fixed this.
4. **Advanced mode holds recall 1.000 and cell accuracy 1.000 through 3,200 rows / 66
   pages at every model tested** — Sonnet 4.6 ($11.39), Sonnet 5 ($24.93), Opus 5 and GPT-6
   Astra ($22.5–24.0) — so above ~400 rows the choice is only about cost and wall-clock (§3, §5).
5. **A premium model does not earn its price here, and the cheap end is not free either.**
   On the four-document premium study, advanced Sonnet 5 is already at ceiling (recall
   1.000, accuracy ≥0.999) and GPT-6 Astra matches it at **1.7–2.6× the cost**. In simple
   mode Astra's larger window makes the request *accepted* where Sonnet 5's is refused — and
   then returns an **empty response** (5 of 5, 17-page document) or rows whose descriptions
   it has rewritten (26-page document: dates 250/250 by position, amounts 209/250, every
   identifier dropped). At the other end, **Nova Lite cannot run the agentic path**: 247
   `invalid sequence as part of ToolUse` stream errors in one grid, each retried as
   transient ([#895](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/895)). §5 gives the per-profile recommendation.
6. **Bedrock-LLM OCR is still the only backend that gets values wrong** — cell accuracy
   0.979–0.988 on the 400-row documents and 0.926 on the 800-row one, against 1.000 for every
   Textract and BDA cell — and it now also loses 9–10% of rows on ordinary 400-row
   documents (§2, finding 4).
7. **Two new product defects, both found by cells this edition adds.** A configuration
   stored compressed that carries a float fails every Test Studio run at submit
   ([#892](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/892),
   fix in PR #893), and a class wrapped with `x-aws-idp-multi-instance` whose instances hold
   a long list can never finish confidence assessment — three 900-second Lambda timeouts
   and a failed document ([#894](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/894)) (§7).

> **What changed between releases** is tracked separately in the
> [Release Audit Trail](releases/) — this paper focuses on *choosing a configuration at the
> current release*.

---

## 1. Methodology (summary)

See `benchmarks/matrices/METHODOLOGY.md` for the full protocol. In brief:
- **Synthetic corpus (exact GT):** generated bank statements whose every transaction row
  carries a unique `SEQnnnnn` tag, so completeness and accuracy are measured exactly, and
  size, row width, list count, text length and OCR noise are controlled variables.
- **Reference corpora (real, labelled):** `RealKIE-FCC-Verified` (20 documents, one class)
  and `OmniAI-OCR-Benchmark` (20 documents, nine classes), run as Test Studio test sets and
  scored by the product's own evaluation against their labels. **Both were run for this
  release** (`core` suite), at the shipped default model and at the control model.
- **Config matrix:** 19 curated *core* cells — the OCR × mode × assessment decision space
  plus the v0.7 feature arms (enforcement, forcing, schema restatement, section splitting) —
  a two-cell scaling series, a repeated-measures cost suite, the two hazard re-verification
  suites (`intconf`, `advverify`), the feature A/B suites for every knob this release
  touched, and the model sweeps. Control arms live in a separate `control_cells:` block and
  are excluded from `core_cells`.
- **Scoring is resolver-free** (reads S3 + DynamoDB metering directly); costs priced from
  `pricing.yaml`; calibration from `explainability_info` confidence leaves.

### Configuration axes measured
| Axis | Values |
|------|--------|
| OCR | Textract LAYOUT, Textract TABLES, BDA, Bedrock-LLM |
| Extraction mode | simple (1 call) · advanced (agentic sharding + table tool) |
| Assessment | off · separate (Nova Lite pass) · integrated (inline) |
| Enforcement | `off` · `warn` (shipped default) · `escalate` |
| Forcing | off (shipped default) · on |
| Schema restatement | on (shipped default) · off |
| Section splitting | `llm_determined` (shipped default) · `disabled` |
| **Extraction model** (§5) | Nova Lite · Nova Pro · **Sonnet 5** (default) · Sonnet 5 `:1m` · Opus 5 · Opus 5.5 (declared, not yet measured) · GPT-6 Astra (`us.` and `global.`) · Sonnet 4.6 (control) |
| **Classification model** (§5) | **Nova 2 Lite** (default) · Sonnet 5 · Haiku 4.5 |
| **Confidence model** (§5) | **Nova Lite** (default) · Nova 2 Lite · Sonnet 5 |
| Confidence batch size | shipped (ceiling 12 since #861) · pinned 8 · pinned 13 |
| Geometry | ocr_only (all cells) |
| Reasoning effort | low (all cells) |

**Axes not varied.** OCR DPI is held at 300 (the shipped default since #740); geometry at
`ocr_only`; multi-instance settings are measured separately in §7.

---

## 2. Configuration matrix (19 cells × 7 synthetic list docs, exact GT)

Mean over 7 bank-statement (transaction-list) documents spanning 5 → 800 rows and varying
row width, list count and description length (`tiny_form`, `small_narrow`, `med_narrow`,
`large_narrow`, `wide_400`, `manylists_400`, `longdesc_100`). `recall` = distinct
ground-truth rows recovered ÷ total (exact, via SEQ tags); `cell acc` = per-row typed value
match, keyed by SEQ tag. Extraction model **Sonnet 5**, the shipped default. **133 runs,
0 failures, and every run produced exactly one section (7 of 7 documents in every cell).**

### 2a. The decision space: OCR × mode × assessment

| OCR / mode / assessment | recall | cell acc | cost/doc | mean conf | alert % | valid rate | wall_s | fails |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Textract TABLES / simple / separate | 1.000 | 1.000 | $0.721 | 0.999 | 0.0 | 1.000 | 243 | 0 |
| Textract TABLES / simple / off | 1.000 | 1.000 | $0.551 | n/a | n/a | 1.000 | 118 | 0 |
| Textract TABLES / simple / integrated | **1.000** | 1.000 | $0.729 | **0.999** | **0.0** | 1.000 | 257 | 0 |
| Textract TABLES / advanced / separate | 1.000 | 1.000 | $1.661 | 0.988 | 0.0 | 1.000 | 223 | 0 |
| Textract TABLES / advanced / integrated | 1.000 | 1.000 | **$1.882** | 0.990 | 0.0 | 1.000 | 315 | 0 |
| Textract LAYOUT / simple / separate | 1.000 | 1.000 | $0.568 | 0.998 | 0.0 | 1.000 | 244 | 0 |
| Textract LAYOUT / advanced / separate | 1.000 | 1.000 | **$1.171** | 0.987 | 0.0 | 1.000 | 203 | 0 |
| **⚠️ BDA / simple / separate** | **0.865** | 1.000 | $0.524 | 0.997 | 0.0 | 1.000 | 186 | 0 |
| BDA / advanced / separate | 1.000 | 1.000 | $1.380 | 0.989 | 0.0 | 1.000 | 211 | 0 |
| **⚠️ Bedrock-LLM / simple / separate** | **0.827** | **0.983** | **$0.403** | 0.997 | 0.0 | 1.000 | 156 | 0 |

Cost CV is 0.6–0.7 for every cell because the 7 documents differ 160× in row count; that
is *between-document* spread, not run-to-run noise. §4 measures cost variance properly,
with repeats on one document.

### 2b. The v0.7 feature arms

All are Textract TABLES + `separate`; `restate-*` are advanced, the rest simple. `sections`
is the total across the 7 documents, where the ground truth is **7** (one per document) —
**every cell gets 7** this edition (v0.6.7: 13–23).

| cell | recall | cell acc | cost/doc | sections (truth 7) | valid rate | val errors |
|---|---:|---:|---:|---:|---:|---:|
| `enforce-off` | 1.000 | 1.000 | $0.710 | 7 | not measured (validation off) | — |
| `enforce-warn` *(shipped default)* | 1.000 | 1.000 | $0.728 | 7 | **1.000** | 0 |
| `enforce-escalate` | 1.000 | 1.000 | $0.716 | 7 | 1.000 | 0 |
| `force-off` *(shipped default)* | 1.000 | 1.000 | $0.698 | 7 | 1.000 | 0 |
| **⚠️ `force-on`** | **0.874** | 1.000 | $0.533 | 7 | 1.000 | 0 |
| `restate-on` *(shipped default)* | 1.000 | 1.000 | $1.523 | 7 | 1.000 | 0 |
| `restate-off` | 1.000 | 1.000 | $1.548 | 7 | 1.000 | 0 |
| `split-llm` *(shipped default)* | 1.000 | 1.000 | $0.717 | **7** | 1.000 | 0 |
| `split-disabled` | 1.000 | 1.000 | $0.714 | 7 | 1.000 | 0 |

**Findings**

1. **Completeness and per-row accuracy are solved for Textract cells in both modes, up to
   and including the 800-row / 17-page document.** Six of the ten decision cells are at
   recall 1.000 *and* cell accuracy 1.000 on all 7 documents. Mode is therefore a **cost**
   decision at these sizes: simple $0.55–0.73, advanced $1.17–1.88, a **2.1–2.6× premium**
   (v0.6.7: 2.6–2.9×; the gap narrowed because advanced mode no longer pays for 2–3 spurious
   sections per document).
2. **Every sub-1.000 recall in the grid is the same document: `large_narrow`, 800 rows on
   17 pages, in simple mode.** BDA OCR returned 43 of 800 rows, Bedrock-LLM OCR 54, forced
   tool use 92 — each `COMPLETED`, each carrying the new `extraction_rows_below_ocr_estimate`
   warning, and each **cheaper** than the complete run ($0.53–0.81 vs $1.97). Textract
   TABLES and LAYOUT returned all 800 in this grid and in the 4 draws of §3's size series —
   but the same TABLES cell returned 43 rows in 3 of 5 repeats inside the §5.2 premium study,
   and Sonnet 5 `:1m` on it returned 43 (§5). Read this as:
   *800 rows in one section is at the single-response limit, and which side of it a run
   lands on depends on the OCR text, the output format and the model variant.* §3 has the
   size series; §5 the per-profile threshold.
3. **`integrated` + simple is no longer a hazard, and no longer more expensive.** Recall
   1.000 on all 7 documents (v0.6.7: 0.936, with `manylists_400` at 0.552), mean confidence
   0.999 (v0.6.7: 0.901), alert rate 0.0% (2.4%), at $0.729 against `separate`'s $0.721 —
   because a Simple-mode section whose class declares a list is now routed to the separate
   confidence pass automatically (#795). §2.1 has the repeated-measures confirmation. The
   recommendation is still `separate` — it is the mechanism that actually runs — but
   choosing `integrated` no longer costs you rows.
4. **⚠️ Bedrock-LLM OCR is the only backend that gets *values* wrong, and it now also loses
   rows on ordinary documents.** Cell accuracy 0.979–0.988 on the three 400-row documents
   and 0.926 on the 800-row one, where every Textract and BDA cell is 1.000; recall
   0.905–0.910 on the 400-row documents (v0.6.7: 1.000 there, 0.620 on a 100-row one). It is
   the cheapest cell ($0.403) and the only one that corrupts identifiers — the fixed-width
   digit insertion root-caused in the [v0.6.6 audit](releases/v0.6.6.md). **Do not use it
   where identifiers or amounts matter.**
5. **LAYOUT-only is the cheapest complete option in both modes** ($0.568 simple / $1.171
   advanced) with confidence indistinguishable from TABLES (0.998 / 0.987). TABLES costs
   27% (simple) to 42% (advanced) more and bought no completeness here — but note finding 2:
   on the 800-row document both Textract flavours completed where BDA did not, so the
   Textract text itself, not the TABLES markdown, is what helps at the limit.
6. **Validation is now clean everywhere.** Every cell with validation on reports
   `valid rate` 1.000 with 0 errors (v0.6.7: 0.54–0.71 in simple mode, with 9–19 spurious
   `'Account Number' is a required property` errors per cell). The v0.6.7 edition predicted
   this: those errors were continuation sections of over-split documents, and there are no
   continuation sections any more.
7. **`force-on` (forced tool use) is schema-valid again, and neutral on 6 of 7 documents.**
   The #783 `$defs`-pointer defect that made every forced section invalid at v0.6.7 is fixed
   (#794): valid rate 1.000, honoured on every call. Its one loss is finding 2's 800-row
   document (92 of 800 rows, one draw). At $0.533 vs $0.698 it looks cheaper, but $0.13 of
   that is the truncated run; on the six complete documents the two arms are within noise.
   §7 has the dedicated A/B.
8. **`enforce-escalate` no longer costs anything on clean documents** ($0.716 vs `warn`
   $0.728; v0.6.7: +35%) because there is nothing to escalate: 0 validation errors in the
   grid means 0 re-extractions. Its price is paid only when validation actually fails, so
   the v0.6.7 "+35%" was the over-splitting bill again, not the arm's own cost.
9. **Schema restatement off is neutral** ($1.548 vs $1.523, n=7, well inside agentic
   spread) — same conclusion as v0.6.7, at one section per document.
10. **`split-disabled` is a no-op on this corpus** (7 sections either way, $0.714 vs $0.717),
    because every document is a single statement. It remains wrong by construction on
    packets (§7) and is not a cost lever any more.

### 2c. Real-corpus accuracy (RealKIE-FCC-Verified, OmniAI-OCR-Benchmark)

Both reference corpora run as Test Studio test sets — 20 documents each, every one of the 19
cells — and are scored by the product's own evaluation against their labels
(`weighted_accuracy` for extracted fields, `class_accuracy` for classification; the
harness's exact-key metrics do not apply to a document whose truth lives on the stack
rather than in `corpus/docs/`, so `cells_compared`, `cell_accuracy`, `scalar_accuracy` and
`completeness_recall` are absent on these rows — absent, not zero. Each row says which
scorer produced it in `scored_against`).

**The four tables below are the v0.6.8 pair** — one grid at the shipped default and one at
the control, on one stack, which is what makes the model comparison in finding 2 a
comparison of models rather than of releases. **1,520 documents, 1,520 completed, 0 parse
failures.** v0.6.0, the previous real-corpus measurement, reported RealKIE-FCC ≈0.80 and
OCR-Benchmark ≈0.87.

v0.6.12 ran both corpora again at the **control only** — **756 scored rows over 760
(cell, corpus, document) triples**, 0 parse failures anywhere — and reproduces the control
picture: RealKIE **0.758–0.812** weighted accuracy across the 19 cells, OCR-Benchmark
**0.965–0.998**, class accuracy **1.000 in every cell of both corpora**. Render the
per-cell tables from the committed artifact with

```bash
python3 benchmarks/harness/aggregate.py \
    --reference-tables benchmarks/results/v0.6.12/core/summary.json
```

#### ⚠️ On a real labelled corpus, simple mode beats advanced — at half the cost

This is the finding the synthetic grid **cannot** produce, and the reason running these
corpora matters more than the row count suggests. §2's 19 cells score 1.000 on recall and
cell accuracy almost everywhere on exact-ground-truth synthetic documents, so the grid
ranks nothing. RealKIE ranks them, and it inverts the intuition that the agentic path is
the more capable one:

| RealKIE cell | weighted accuracy | cost/doc | wall |
|---|---|---|---|
| `split-disabled` | **0.812** | $0.1251 | 44 s |
| `core-tt-simple-sep` | 0.807 | $0.1423 | 38 s |
| `core-tt-simple-off` | 0.805 | $0.1392 | 29 s |
| `core-tl-adv-sep` | 0.791 | $0.2464 | 53 s |
| `core-tt-adv-int` | 0.768 | $0.4870 | 63 s |
| `core-bda-adv-sep` | 0.760 | $0.3142 | 53 s |
| `core-tt-adv-sep` | **0.758** | $0.2757 | 52 s |

**The three best cells are all simple mode and the three worst are all advanced**, and the
gap in cost is about 2× (`core-tt-simple-sep` $0.1423 against `core-tt-adv-sep` $0.2757) in
the same direction. On this corpus advanced extraction is paying more for less. The two
readings this does *not* support: it says nothing about completeness at scale, which is
§3's territory and where advanced mode is unambiguously better; and RealKIE's documents are
not long lists, so this is a statement about labelled real-world forms rather than about
tabular extraction generally.

OCR-Benchmark does not discriminate the same way — every cell lands between 0.965 and
0.998, with `core-llm-simple-sep` the only one below 0.99 (0.965), consistent with the
`bedrock_llm` arm being the noisiest throughout this edition.

⚠️ **This is one arm, so it is not a model comparison.** Only the control ran, so the
v0.6.8 two-model pair below is still the only like-for-like comparison of the shipped
default against the control on these corpora. Comparing a v0.6.12 control figure with a
v0.6.8 default figure would be a cross-release difference wearing a model label.

⚠️ **4 of the 760 triples produced no score** — `SCORE_ERROR` on `ocr_bench` in four
different cells. That is a **scoring** failure, not a processing one: the documents
processed and the scorer did not return a figure for them, which is why those cells read
19 of 20 completed rather than 20.


### RealKIE-FCC-Verified (20 docs, 1 class) — extraction Sonnet 5 (shipped default), v0.6.8
| cell | docs | completed | weighted accuracy | class accuracy | parse failures | cost/doc | wall |
|---|---|---|---|---|---|---|---|
| `core-tt-simple-sep` | 20 | 20 | 0.777 | 1.000 | 0 | $0.1934 | 43 s |
| `core-tt-simple-int` | 20 | 20 | 0.765 | 1.000 | 0 | $0.1937 | 39 s |
| `core-tt-simple-off` | 20 | 20 | 0.759 | 1.000 | 0 | $0.1913 | 29 s |
| `core-tt-adv-sep` | 20 | 20 | 0.776 | 1.000 | 0 | $0.4110 | 49 s |
| `core-tt-adv-int` | 20 | 20 | 0.789 | 1.000 | 0 | $0.5810 | 61 s |
| `core-tl-simple-sep` | 20 | 20 | 0.771 | 1.000 | 0 | $0.1447 | 38 s |
| `core-tl-adv-sep` | 20 | 20 | 0.797 | 1.000 | 0 | $0.3655 | 59 s |
| `core-bda-simple-sep` | 20 | 20 | 0.754 | 1.000 | 0 | $0.1935 | 39 s |
| `core-bda-adv-sep` | 20 | 20 | 0.782 | 1.000 | 0 | $0.4268 | 57 s |
| `core-llm-simple-sep` | 20 | 20 | 0.731 | 1.000 | 0 | $0.1619 | 52 s |
| `enforce-off` | 20 | 20 | 0.759 | 1.000 | 0 | $0.1985 | 41 s |
| `enforce-warn` | 20 | 20 | 0.764 | 1.000 | 0 | $0.1944 | 39 s |
| `enforce-escalate` | 20 | 20 | 0.771 | 1.000 | 0 | $0.2518 | 40 s |
| `force-off` | 20 | 20 | 0.764 | 1.000 | 0 | $0.1930 | 43 s |
| `force-on` | 20 | 20 | 0.803 | 1.000 | 0 | $0.1980 | 37 s |
| `restate-on` | 20 | 20 | 0.786 | 1.000 | 0 | $0.4041 | 52 s |
| `restate-off` | 20 | 20 | 0.785 | 1.000 | 0 | $0.3757 | 48 s |
| `split-llm` | 20 | 20 | 0.764 | 1.000 | 0 | $0.1943 | 39 s |
| `split-disabled` | 20 | 20 | 0.771 | 1.000 | 0 | $0.1737 | 43 s |

### OmniAI-OCR-Benchmark (20 docs, 9 classes) — extraction Sonnet 5 (shipped default), v0.6.8
| cell | docs | completed | weighted accuracy | class accuracy | parse failures | cost/doc | wall |
|---|---|---|---|---|---|---|---|
| `core-tt-simple-sep` | 20 | 20 | 0.997 | 1.000 | 0 | $0.0411 | 26 s |
| `core-tt-simple-int` | 20 | 20 | 0.997 | 1.000 | 0 | $0.0410 | 25 s |
| `core-tt-simple-off` | 20 | 20 | 0.997 | 1.000 | 0 | $0.0403 | 20 s |
| `core-tt-adv-sep` | 20 | 20 | 0.998 | 1.000 | 0 | $0.0914 | 30 s |
| `core-tt-adv-int` | 20 | 20 | 0.998 | 1.000 | 0 | $0.1365 | 38 s |
| `core-tl-simple-sep` | 20 | 20 | 0.998 | 1.000 | 0 | $0.0300 | 25 s |
| `core-tl-adv-sep` | 20 | 20 | 0.998 | 1.000 | 0 | $0.0488 | 30 s |
| `core-bda-simple-sep` | 20 | 20 | 0.997 | 1.000 | 0 | $0.0299 | 26 s |
| `core-bda-adv-sep` | 20 | 20 | 0.996 | 1.000 | 0 | $0.0340 | 30 s |
| `core-llm-simple-sep` | 20 | 20 | 0.973 | 1.000 | 0 | $0.0268 | 26 s |
| `enforce-off` | 20 | 20 | 0.997 | 1.000 | 0 | $0.0429 | 25 s |
| `enforce-warn` | 20 | 20 | 0.997 | 1.000 | 0 | $0.0410 | 26 s |
| `enforce-escalate` | 20 | 20 | 0.996 | 1.000 | 0 | $0.0411 | 25 s |
| `force-off` | 20 | 20 | 0.997 | 1.000 | 0 | $0.0410 | 26 s |
| `force-on` | 20 | 20 | 0.996 | 1.000 | 0 | $0.0445 | 25 s |
| `restate-on` | 20 | 20 | 0.997 | 1.000 | 0 | $0.0880 | 30 s |
| `restate-off` | 20 | 20 | 0.998 | 1.000 | 0 | $0.0787 | 30 s |
| `split-llm` | 20 | 20 | 0.988 | 1.000 | 0 | $0.0410 | 25 s |
| `split-disabled` | 20 | 20 | 0.996 | 1.000 | 0 | $0.0410 | 25 s |

### RealKIE-FCC-Verified (20 docs, 1 class) — extraction Sonnet 4.6 (control), v0.6.8
| cell | docs | completed | weighted accuracy | class accuracy | parse failures | mean conf | cost/doc | wall |
|---|---|---|---|---|---|---|---|---|
| `core-tt-simple-sep` | 20 | 20 | 0.805 | 1.000 | 0 | 0.908 | $0.1434 | 42 s |
| `core-tt-simple-int` | 20 | 20 | 0.805 | 1.000 | 0 | 0.879 | $0.1433 | 40 s |
| `core-tt-simple-off` | 20 | 20 | 0.805 | 1.000 | 0 | not scored | $0.1401 | 29 s |
| `core-tt-adv-sep` | 20 | 20 | 0.783 | 1.000 | 0 | 0.860 | $0.3796 | 69 s |
| `core-tt-adv-int` | 20 | 20 | 0.770 | 1.000 | 0 | 0.966 | $0.4480 | 68 s |
| `core-tl-simple-sep` | 20 | 20 | 0.788 | 1.000 | 0 | 0.903 | $0.0883 | 36 s |
| `core-tl-adv-sep` | 20 | 20 | 0.797 | 1.000 | 0 | 0.912 | $0.2700 | 60 s |
| `core-bda-simple-sep` | 20 | 20 | 0.806 | 1.000 | 0 | 0.900 | $0.1434 | 40 s |
| `core-bda-adv-sep` | 20 | 20 | 0.784 | 1.000 | 0 | 0.913 | $0.2852 | 68 s |
| `core-llm-simple-sep` | 20 | 20 | 0.792 | 1.000 | 0 | 0.878 | $0.1137 | 57 s |
| `enforce-off` | 20 | 20 | 0.808 | 1.000 | 0 | 0.904 | $0.1437 | 41 s |
| `enforce-warn` | 20 | 20 | 0.799 | 1.000 | 0 | 0.898 | $0.1418 | 42 s |
| `enforce-escalate` | 20 | 20 | 0.801 | 1.000 | 0 | 0.910 | $0.1991 | 40 s |
| `force-off` | 20 | 20 | 0.804 | 1.000 | 0 | 0.895 | $0.1420 | 41 s |
| `force-on` | 20 | 20 | 0.798 | 1.000 | 0 | 0.924 | $0.1465 | 45 s |
| `restate-on` | 20 | 20 | 0.780 | 1.000 | 0 | 0.884 | $0.3824 | 69 s |
| `restate-off` | 20 | 20 | 0.784 | 1.000 | 0 | 0.860 | $0.3579 | 65 s |
| `split-llm` | 20 | 20 | 0.805 | 1.000 | 0 | 0.903 | $0.1433 | 40 s |
| `split-disabled` | 20 | 20 | 0.842 | 1.000 | 0 | 0.929 | $0.1260 | 44 s |

### OmniAI-OCR-Benchmark (20 docs, 9 classes) — extraction Sonnet 4.6 (control), v0.6.8
| cell | docs | completed | weighted accuracy | class accuracy | parse failures | mean conf | cost/doc | wall |
|---|---|---|---|---|---|---|---|---|
| `core-tt-simple-sep` | 20 | 20 | 0.997 | 1.000 | 0 | 0.960 | $0.0334 | 24 s |
| `core-tt-simple-int` | 20 | 20 | 0.997 | 1.000 | 0 | 0.961 | $0.0334 | 24 s |
| `core-tt-simple-off` | 20 | 20 | 0.997 | 1.000 | 0 | not scored | $0.0328 | 19 s |
| `core-tt-adv-sep` | 20 | 20 | 0.997 | 1.000 | 0 | 0.964 | $0.0575 | 34 s |
| `core-tt-adv-int` | 20 | 20 | 0.998 | 1.000 | 0 | 0.980 | $0.1016 | 38 s |
| `core-tl-simple-sep` | 20 | 20 | 0.998 | 1.000 | 0 | not measurable | $0.0223 | 24 s |
| `core-tl-adv-sep` | 20 | 20 | 0.997 | 1.000 | 0 | 0.963 | $0.0494 | 34 s |
| `core-bda-simple-sep` | 20 | 20 | 0.998 | 1.000 | 0 | not measurable | $0.0223 | 24 s |
| `core-bda-adv-sep` | 20 | 20 | 0.998 | 1.000 | 0 | 0.966 | $0.0409 | 34 s |
| `core-llm-simple-sep` | 20 | 20 | 0.973 | 1.000 | 0 | 0.972 | $0.0190 | 24 s |
| `enforce-off` | 20 | 20 | 0.997 | 1.000 | 0 | 0.961 | $0.0338 | 24 s |
| `enforce-warn` | 20 | 20 | 0.997 | 1.000 | 0 | 0.959 | $0.0334 | 24 s |
| `enforce-escalate` | 20 | 20 | 0.997 | 1.000 | 0 | not measurable | $0.0334 | 24 s |
| `force-off` | 20 | 20 | 0.997 | 1.000 | 0 | 0.963 | $0.0335 | 24 s |
| `force-on` | 20 | 20 | 0.998 | 1.000 | 0 | 0.959 | $0.0350 | 24 s |
| `restate-on` | 20 | 20 | 0.997 | 1.000 | 0 | 0.963 | $0.0756 | 34 s |
| `restate-off` | 20 | 20 | 0.995 | 1.000 | 0 | 0.961 | $0.0797 | 37 s |
| `split-llm` | 20 | 20 | 0.997 | 1.000 | 0 | 0.963 | $0.0334 | 24 s |
| `split-disabled` | 20 | 20 | 0.997 | 1.000 | 0 | not measurable | $0.0334 | 24 s |

**Findings**

1. **The OCR benchmark is at ceiling for every configuration but one.** 0.996–0.998 weighted
   accuracy in 18 of 19 cells at both models (v0.6.0: ≈0.87); the one exception is
   Bedrock-LLM OCR at **0.973**, the same backend that gets values wrong on the synthetic
   grid. Mode, assessment, forcing, restatement and splitting make no measurable difference
   on these documents, and the whole corpus costs $0.02–0.05 per document in simple mode.
2. **RealKIE is a harder corpus, and the control model is slightly better on it than the
   default.** Sonnet 4.6 scores 0.77–0.84 across the cells; Sonnet 5 0.73–0.80 — a
   consistent 2–5 points lower on 18 of 19 cells, at 30–40% higher cost. This is the one
   place in the study where the shipped default is measurably worse than the cheaper model,
   and the reason the model-selection table in §5.5 recommends Sonnet 4.6 for single-class
   real forms.
3. **Advanced mode buys nothing on real forms.** RealKIE 0.77–0.80 advanced against
   0.76–0.81 simple at 2.1–3.0× the cost; the OCR benchmark is identical. The agentic path
   earns its premium on long lists (§3), not on forms.
4. **`split-disabled` is the best RealKIE cell at the control model (0.842 vs 0.805) and
   the cheapest** — on a single-class corpus of one-document files, the classifier's
   boundary pass can only introduce error, and here it introduces about 4 points of it. At
   Sonnet 5 the effect is within noise (0.771 vs 0.764). It remains wrong for packets (§7).
5. **`enforce-escalate` costs +40% on RealKIE at the control model and +30% at Sonnet 5
   ($0.199 / $0.252) for 0.0–0.7 points** — unlike the synthetic grid, real forms *do*
   produce validation failures, and every one is a paid re-extraction. This is what the arm's
   price looks like on a corpus that exercises it.
6. **Forcing is the best Sonnet 5 cell on RealKIE (0.803 vs 0.764 for `force-off`)** and
   neutral at the control model (0.798 vs 0.804). One draw per document, so read the 4 points
   as suggestive; it is consistent with the v0.6.7 real-corpus A/B, which found forcing
   accuracy-neutral-to-positive on 322 paired documents.

**Two columns above read as words rather than as numbers.**

The `class accuracy` column reads 1.000 everywhere because both corpora are single-class
per document as configured here.

`mean conf` reads **`not scored`** for `core-tt-simple-off` in both control tables, and that
is the whole of what happened: that arm turns the confidence pass off, so no confidence was
produced to average. It is not a confidence of zero, and the distinction matters — a zero
there would say the grader was certain of nothing.

`mean conf` reads **`not measurable`** in four OCR-benchmark cells —
`core-tl-simple-sep`, `core-bda-simple-sep`, `enforce-escalate` and `split-disabled` — and
that needs the cause spelled out, because a confidence is a probability and these cells
previously published values above 1.0.

- **The arithmetic is correct; one input was off scale.** A single grader leaf answered on
  a 0–100 scale where the scale is 0.0–1.0, and the row mean is taken over all fifteen
  leaves of the document, so one `100` drags the row far above 1.0 and the per-cell mean
  with it. Every affected row records `n_conf_leaves: 15` and `pct_conf_below_0.9: 0.0`, and
  the multiset `[0.9839] × 12 + [0.9841] × 2 + [100.0]` reproduces the committed
  `7.585 / 15 / 0.0` triple exactly
  ([#1395](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1395),
  whose fix makes the harness refuse an out-of-range leaf rather than average it).
- **It is 21 rows of the 11,080 committed rows that carry a confidence at all** (0.19%),
  two documents — both bank checks — in four of the 121 committed grids. Rendering those four
  grids reproduces **19** such cells in total, of which the four above are the ones this
  page publishes. Values at or below 1.0 are unaffected: the defect can only push a mean
  up, never down.
- **No corrected number is published for these four**, because a correct one would need the
  grids re-scored against the extracted values, and the rows do not carry them. Naming the
  cells is the honest alternative to quietly re-rendering the table with the affected rows
  dropped, which would move a published figure with nothing saying so.

Both spellings are what `aggregate.py --reference-tables` prints, so the `mean conf`
column above remains a transcription of the committed artifact rather than an editorial
reading of it. Render the control grid for yourself with

```bash
python3 benchmarks/harness/aggregate.py \
    --reference-tables benchmarks/results/v0.6.8/core/summary.json
```


---

## 2.1 The `integrated` + simple hazard — closed at v0.6.8

**What v0.6.8 measures.** On the 7-document grid, `simple/integrated` recall is **1.000 on
every document**, including `manylists_400` (0.552 at v0.6.7) and the 800-row
`large_narrow`. The repeated-measures suite that established the hazard (`intconf`: the
integrated cell and a `separate` control on the same 100-row document, 4× each) now reads:

| extraction model | cell | recall per repeat | cost/doc | mean conf |
|---|---|---|---|---|
| **Sonnet 5** (shipped default) | simple / **integrated** | **1.000 ×4** | $0.304 | 0.999–1.000 |
| Sonnet 5 | simple / separate | 1.000 ×4 | $0.303 | 0.999–1.000 |
| Sonnet 4.6 (control) | simple / **integrated** | **1.000 ×4** | $0.208 | 0.999 |
| Sonnet 4.6 | simple / separate | 1.000 ×4 | $0.208 | 0.999 |

At v0.6.5 the first row read **0.000 ×4** (1–10 rows returned, none matching). The reason
the two cells now cost the same to the cent is that they *are* the same mechanism: a
Simple-mode section whose class declares a list field is routed to the separate confidence
pass regardless of the `integrated` setting (#795; `metadata.confidence_mode_effective`
records it). A class can opt back into true inline scoring with
`x-aws-idp-allow-integrated-lists: true`, and these numbers do not cover that path.

**The advanced-mode counterpart is closed too.** `advverify` (the agentic cell with
`integrated` and `separate` confidence on `longdesc_100`, 4× each, Sonnet 5): **8 of 8 runs
complete**, recall 1.000, cell accuracy 1.000, $0.61–1.93/doc. At v0.6.5 the agent declined
the table tool over one bad column and returned the whole 100-row list as `null` — that is
the tool-decline hazard the v0.6.6/v0.6.7 editions tracked (#666/#668), and it did not
recur.

> These are repeated measures on the one document each hazard fired on. The grid adds
> single draws on six more; none fired. The v0.6.5 evidence tables that documented the
> mechanism are retained in that edition (`git checkout` per Appendix A) and are not
> restated here.

---

## 3. Scaling: where extraction hits limits (synthetic, exact GT)

Simple vs advanced, Textract TABLES + separate confidence, one transaction list of N rows
(~48 rows/page), one section per document at every size. Extraction model **Sonnet 5**; the
control-model series is below it. `n` is the number of draws behind each simple-mode cell
(the `scaling` suite plus three repeats of `scalingsimple`).

| rows | pages | SIMPLE recall (n=2) | simple $ | wall | ADVANCED recall | adv $ | wall |
|-----:|------:|------------------:|---------:|-----:|----------------:|------:|-----:|
| 25 | 1 | 1.000, 1.000 | $0.060 | 29 s | 1.000 | $0.130 | 35 s |
| 100 | 3 | 1.000, 1.000 | $0.197–0.198 | 62 s | 1.000 | $0.235 | 69 s |
| 400 | 9 | 1.000, 1.000 | $0.704–0.712 | 268 s | 1.000 | $0.812 | 157 s |
| 800 | 17 | **0.126, 1.000** — bimodal | $0.827, $1.972 | 192 s | 1.000 | $1.493 | 151 s |
| 1,200 | 25 | **0.084, 0.036** (101 and 43 of 1,200 rows) | $1.005–1.120 | 233 s | 1.000 | $2.176 | 151 s |
| 1,600 | 33 | **0.027, 0.058** (43 and 92 of 1,600) | $1.341–1.378 | 290 s | 1.000 | $2.925 | 257 s |
| 3,200 | 66 | **FAILED**, FAILED | $1.056 | 51 s | 1.000 | **$5.696** | 417 s |

Control model (Sonnet 4.6), one draw per size: simple 1.000 through 800 rows ($1.63 at 800),
failing from 1,200; advanced 1.000 throughout at $0.143 → $5.004, 45 → 408 s.

Per-row **cell accuracy is 1.000 in every completed run of both modes** — at no size does
either mode return a row with a *wrong* value. Every loss here is a missing row.

> ⚠️ **Behaviour change at v0.6.9, and it is the one number in this section to read carefully.**
> On v0.6.8 simple mode **refused** documents from 1,200 rows, in 21–55 s, in 12 of 12 draws
> across both models. On v0.6.9 the same cells **return `COMPLETED`** carrying 43–101 of the
> requested 1,200–1,600 rows: the status is success, and a consumer reading status alone sees
> a completed document with 3–8% of its rows. Advanced mode is unaffected and returns 1.000 at
> every size, so the practical guidance below does not change. From 0.6.10 a deployment can
> make the outcome a **failure** again with `extraction.row_shortfall_action: fail`; the
> default stays `warn`, so the numbers in this table are still what ships by default. See
> *What changed, and what ships now* below.
>
> ⚠️ **The v0.6.8 refusal was not an input-size decision, and this is worth knowing before
> tuning anything.** Simple mode's pre-flight estimate has never refused a request — it logs
> and sends, in both releases. What refused these documents was Bedrock, on the **size of the
> request its page images made**, which is what #994 fixed by clamping every image in a
> many-image request to 2,000 px per side. Two separate Bedrock limits bind there: a stricter
> per-image dimension cap once a request carries more than 20 image blocks, and a cap on the
> total request payload. It is the **payload** limit that produces
> `ExtractionInputTooLarge`, because Bedrock reports an oversized payload with the same
> *"Input is too long for requested model"* wording it uses for a context overflow; the
> per-image dimension rejection has its own distinct wording and is matched separately (see
> the note in `idp_common/utils/bedrock_utils.py`). The clamp cuts pixel area, so it relieves
> both.
>
> The extraction-phase input tokens in this suite give the arithmetic. Between 3 and 17 pages
> the request costs 6,622 tokens per page (the two-point slope over 9→17 pages; a four-point
> fit over 1/3/9/17 gives 6,586, and the 1-page point sits 14.8% above the line, so the claim
> is scoped to 3–17 pages, where it holds to 0.25%). The 17-page figure is **identical**
> across the two releases — 111,083 — because at 17 images no many-image cap applies. At 25
> and 33 pages v0.6.9 measures 146,142 and 193,745 against a projected uncapped 164,060 and
> 217,037: a saving of 717 and 706 tokens per image, two figures 1.5% apart, present where the
> cap applies and absent where it does not. And 164,060 is **under** Sonnet 5's
> 200,000-token window — 82% of it — so at 25 pages there was no context overflow available to
> refuse. The v0.6.8 failures also billed **no extraction tokens at all**, their cost being
> the OCR spend plus the classification pass that had already run, which is a request rejected
> before extraction inference rather than one that ran. At 33 pages both limits bound and the
> clamp relieved both. At 66 pages the projection is ~389K even clamped, which is why 3,200
> rows still fails.

### What changed, and what ships now

The consequence of the above is that **restoring a pre-flight refusal would not have fixed
this**. At 25 pages the request is ~146K estimated input tokens against a 200K window — 73%
of it by the pre-flight's own estimate, 82% by the billed figure — so a gate keyed on "the
estimate exceeds the model's window" is silent on exactly the cases in this table, and a gate
tightened until it were not would refuse documents that complete today. Nor would a
payload-size gate help: it would re-refuse precisely what #994 deliberately made legal. The
truncation is an **output** event: the model accepts a request that fits and stops after ~100
rows, well short of its 128K output cap (5,213 output tokens at 1,200 rows). It is also not
new — 800 rows was already bimodal on v0.6.8, at a page count where no many-image cap applied.
What #994 changed is the *range of sizes over which the request is accepted*, which exposed a
pre-existing truncation at 1,200 and 1,600 rows.

So 0.6.10 makes the observed shortfall able to decide the outcome, via
`extraction.row_shortfall_action`. The `extraction_rows_below_ocr_estimate` detection is
unchanged — the rows extracted against the rows in the section's OCR tables of the same width,
a floor of 30 and a "fewer than half" ratio — and under `fail` the partial rows and the
diagnosis are written before the section fails, so a truncated run in this table reports
`FAILED` with a message naming the rows extracted, the OCR estimate and the remedy.

**The default is `warn`, so this table's numbers still describe what ships by default.** The
reason is the check's evidence, not caution: matched tables are summed over the whole section
on width alone, and a 2- or 3-property array modelling an entity *group* is structurally
identical to one modelling table rows. On the default preset a completely correct extraction
of `Bank-Statement.account_summary` (2 properties, 5 rows) scores 0.13, because a monthly
statement's 31-row two-column Daily Balance table is counted as evidence about it. Nine such
fields ship in the config library, so `fail` as a default would fail correct extractions.
[Extraction and confidence](../extraction-and-confidence.md#why-fail-is-opt-in-and-what-to-check-before-turning-it-on)
lists the shapes to check before turning it on.

Two notes on the threshold. It is **not a new number**: the failure fires exactly where the
warning already fired, so no second threshold was chosen to make these cells come out right.
And across the 3,631 recorded benchmark runs that reach the check's population, non-zero
recall is strongly bimodal — 65 runs below 0.5, 3,368 at ~1.000, and **one single run**
anywhere in [0.3, 0.5) — so the outcome is insensitive to the ratio's exact value within that
band. Of those 65, three are Advanced-mode runs (of 1,231 Advanced runs in the population) and
all three lost real rows, though only one lost them as a clean stop-early cut: the other two
have `truncation_prefix = 0`, meaning the loss is scattered through the list rather than a
prefix. What none of this establishes is the **false-failure** rate against real corpora: the
65 are all runs that genuinely lost more than half their rows, and the group-shaped-array
problem above was found by reading the shipped schemas, **not measured** on a document set.

### Where the cliff is, and why "COMPLETED" is not the signal to trust

At v0.6.7 this table showed simple mode "complete" at 1,200 rows and recovering 0.72–0.79 at
3,200, and the edition spent a section explaining that the completeness was an artefact:
the classifier split every large document into 6–18 sections, each small enough to
extract, and the rows arrived as N per-section lists that a consumer had to reassemble.
With #726/#817 every document is one section and that scaffolding is gone. Three things
follow, each measured here:

1. **From 1,200 rows / 25 pages, simple mode returns a fraction of the document.** 43–101
   rows of 1,200–1,600, in 4 of 4 draws, for $1.00–1.38. Only 3,200 rows / 66 pages exceeds
   the model's input window and is refused before extraction inference. These runs report
   `COMPLETED`, and they still do by default; setting `extraction.row_shortfall_action: fail`
   makes them resolve to `FAILED` with the rows extracted, the OCR row estimate and the remedy
   in the message, which is the outcome a mode that cannot shard should give. Either way the
   rows it did extract are written to the section's `result.json`, so nothing measured here is
   lost — only the claim of success is in question.
2. **800 rows / 17 pages is the boundary, and it is a coin flip.** The TABLES + Sonnet 5
   cell completed 800 rows in this table's 4 draws and §2's grid run, then returned **43 of
   800 in 3 of 5 repeats** of the identical cell inside the §5.2 premium study — **8 of 11
   overall** — and on the prerelease `dev2` build it returned 43
   ([releases/v0.6.8.md](releases/v0.6.8.md), prerelease section). The same document in the
   same grid under BDA OCR, Bedrock-LLM OCR, or forced tool use returned 43, 54 and 92 rows;
   under Sonnet 5 `:1m`, 43; under Astra, nothing at all (§5). So the honest statement is
   not "complete at 800" but **"800 rows is where the model's single response stops being
   reliably long enough; the same request lands on either side of it run to run, and small
   changes to the input text or output format shift the odds."** Treat ~400 rows / ~10
   pages as the safe simple-mode envelope, and use advanced mode above it.
3. **A truncated run is not silent, but by default it is still `COMPLETED`.** Every 43–92-row
   result carries `extraction_rows_below_ocr_estimate` (#843) in its processing issues, and
   the status-tracking record counts it — but reading it needs a dashboard or a downstream
   rule, because a processing issue does not change a document's status at **any** severity,
   and a truncated run is *cheaper* than a complete one, so neither status nor cost flags it.
   `extraction.row_shortfall_action: fail` is what makes the status trustworthy on its own,
   and it is opt-in for the reason given above.

### Advanced mode: completeness holds; cost and wall-clock are the limits

Advanced (agentic sharding) holds **recall 1.000 and cell accuracy 1.000 through 3,200 rows
/ 66 pages** at both Claude models — and, in the premium study (§5), at Opus 5 and GPT-6
Astra too. Sharding keeps each call small, so neither the truncation nor an input-context
limit is hit. The practical limits are **cost** (up to ~$25/doc at 3,200 rows on Sonnet 5,
~$11 on Sonnet 4.6) and **wall-clock** (~19 min at 3,200 rows). Cost grows roughly
linearly in rows to 1,600 and then jumps (×3.6 from 1,600 to 3,200 rows on Sonnet 5), which
is the agent re-reading a growing conversation.

> **The confidence pass no longer fails at scale.** The v0.6.7 edition's one aborted run
> (simple @800: extraction complete, then five 900-second Assessment timeouts) did not
> recur in any of the 11 Sonnet 5 draws at 800 rows this edition. The Nova Lite batch ceiling and
> per-batch budget shipped in #861 (§7) are why: the 800-row confidence pass now reports
> `assessment_recovered_with_retries` with 7 rows recovered on one retry, not a ladder that
> never converges.

---

## 4. Cost: level AND variance (n=5 repeats, same 400-row doc)

Agentic-advanced cost is **non-deterministic run-to-run** (the agent's turn count varies),
so a single sample cannot resolve a cost difference between configs. The suite measures cost
with repeats and reports mean ± stdev + coefficient of variation (CV); a cost difference is
only trustworthy when it exceeds the sampling spread.

Same document (`med_narrow`, 400 rows / 9 pages), 5 repeats per cell, 25 runs, extraction
model **Sonnet 5** (the shipped default). All 25 returned `COMPLETED` with **recall 1.000**
and **one section** — so these are like-for-like cost comparisons of configurations that all
did the job.

| config (OCR / mode / assessment) | cost mean ± stdev | CV | min–max | sections/run |
|----------------------------------|-------------------|---:|---------|---|
| Textract TABLES / **simple** / separate | **$0.730 ± $0.030** | **4.1%** | $0.702–0.775 | 1 ×5 |
| Textract LAYOUT / advanced / separate | $1.325 ± $0.457 | 34.5% | $0.793–1.971 | 1 ×5 |
| BDA / advanced / separate | $1.501 ± $0.381 | 25.4% | $1.238–2.162 | 1 ×5 |
| Textract TABLES / advanced / integrated | $1.962 ± $0.662 | 33.7% | $1.380–2.925 | 1 ×5 |
| Textract TABLES / advanced / separate | $2.034 ± $0.811 | **39.9%** | $1.329–**3.353** | 1 ×5 |

The same five cells at the **control model, Sonnet 4.6** (also 5 repeats, all complete,
one section each):

| config | cost mean ± stdev | CV | min–max |
|---|---|---:|---|
| Textract TABLES / simple / separate | $0.526 ± $0.001 | **0.2%** | $0.526–0.527 |
| Textract LAYOUT / advanced / separate | $1.115 ± $0.077 | 6.9% | $1.027–1.197 |
| BDA / advanced / separate | $1.249 ± $0.041 | **3.3%** | $1.204–1.285 |
| Textract TABLES / advanced / integrated | $1.450 ± $0.091 | 6.2% | $1.343–1.551 |
| Textract TABLES / advanced / separate | $1.351 ± $0.172 | 12.7% | $1.167–1.541 |

**Findings**

- **Simple mode is 1.8–2.8× cheaper than advanced at Sonnet 5 and essentially
  deterministic** (CV 4.1% vs 25–40%). For budgeting, simple is both cheaper and
  predictable; agentic cost must still be planned as a *range*.
- **The classifier's share of the variance is gone; the model's share is not.** At v0.6.7
  the same document was classified 1 to 5 ways across five identical runs and each spurious
  section was a whole agent loop (`r(sections, cost) = 0.37`, ≈$0.21 per extra section). This
  edition every run is one section, and at Sonnet 4.6 the advanced-mode CV collapsed from
  5.5–47.3% to **3.3–12.7%**. At Sonnet 5 it did not: **25–40%**, with `tt-adv-sep` ranging
  $1.33 → $3.35 on the same document with the same one section. That spread is the agent
  itself — Sonnet 5 takes a variable number of turns on the same input — and no
  configuration knob in this study removes it.
- **The worst case still matters more than the mean.** A capacity model built on the Sonnet 5
  mean will be 65% under the run-to-run maximum on `tt-adv-sep`.
- **BDA / advanced is the most stable advanced option at both models** (CV 3.3% / 25.4%),
  LAYOUT / advanced the cheapest at the low end ($0.79 on Sonnet 5 when the agent finishes
  quickly).
- **Why advanced costs more even with the deterministic table tool:** it is a multi-turn
  agent loop, and each turn re-sends the growing conversation as *input* tokens. With one
  section per document that loop now runs once, which is why the advanced premium fell from
  2.6–4.1× (v0.6.7) to 1.8–2.8×.

> **Model note.** §4's Sonnet 5 table is directly comparable with §2. The Sonnet 4.6 table
> is comparable with the [release audit](releases/v0.6.8.md), which is measured at that
> control model; do not compare absolute dollars across the two tables, only shapes.

> Methodology note: run these cells with `--suite cost` (or `--repeats ≥5`); the harness
> flags any cell with cost CV > 0.25 as unreliable-at-current-n, and
> `aggregate.py --compare` only reports a cost regression when the mean shift exceeds the
> combined sampling spread. That treatment covers accuracy and completeness too
> ([index.md](index.md)).

---

## 5. Which model, for which documents — the model axis, measured

Everything above holds the extraction model at the shipped default. This section varies it —
and the classification and confidence models — on the **same 19-cell × 7-document grid**
(`coresynth`, 133 runs per model, one section per document throughout), plus a dedicated
premium study on the documents where a premium model *could* matter. Every model row below is
computed by the same scorer on the same documents; rows are directly comparable.

Models: Amazon Nova Lite and Nova Pro (the cheap end), Claude Sonnet 4.6 (the study's
cross-version control), **Claude Sonnet 5 (the shipped default)**, Sonnet 5 `:1m` (the
1M-token-context variant), Claude Opus 5, Claude Opus 5.5, Claude Haiku 5.5,
**Claude Sonnet 5.5 (new in v0.6.12)**, and OpenAI GPT-6 Astra (`us.` and `global.`).
Nova 2 Lite classification and Nova Lite confidence are held at their defaults except where
they are the axis.

### 5.1 Extraction model — the full grid

**All nine swept models were measured on v0.6.12**, including Claude Sonnet 5.5, whose own
changelog entry states it was in no benchmark sweep — it was added to the axis for this
edition. Seven ran the 19-cell × 7-document `coresynth` grid; the two Nova models cannot
complete the agentic path
([#895](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/895))
and ran `simplegrid` instead, so they are reported separately below and never averaged in.
Note the grid's documents run to **800 rows**; the larger sizes where models diverge most
are §3's territory, not this table's.

| extraction model | runs | completed | mean recall | mean cell accuracy | `cells_compared` | grid cost | % conf leaves < 0.9 |
|---|---:|---:|---|---|---:|---|---|
| **Claude Opus 5.5** | 133 | 130 | **1.0000** | **1.0000** | **83,790** | **$101.67** | 0.020% |
| GPT-6 Astra | 133 | 133 | **1.0000** | 0.9737 | 83,788 | $209.28 ◊ | 0.004% |
| Claude Opus 5 | 133 | 132 | 0.9925 | **1.0000** | 82,190 | $126.55 | 0.010% |
| Claude Sonnet 5 *(shipped default)* | 133 | 133 | 0.9563 | 0.9996 | 74,652 | $86.10 | 0.007% |
| Claude Sonnet 5 `:1m` | 133 | 133 | 0.9484 | 0.9992 | 73,334 | $87.42 | 0.006% |
| **Claude Sonnet 5.5** *(new)* | 133 | 132 | **0.9041** ※ | 1.0000 ※ | 73,570 | $62.85 ‡ | 0.011% |
| Claude Haiku 5.5 | 133 | 133 | 0.9269 | 0.9990 | 70,406 | $26.30 † | 0.025% |

⚠️ **Read `cells_compared` alongside the accuracy columns, not after them.** A cell the
arm never returned is a cell it is not scored on, so an arm that returns fewer rows is
graded on a smaller denominator — the range here is 70,406 to 83,790, a 19% spread. That
is why the recall column is the one to rank on and the accuracy column is a statement
about the rows an arm *did* return. A 1.0000 cell accuracy over 73,570 cells (Sonnet 5.5)
and the same figure over 83,790 (Opus 5.5) are not the same achievement.

**The ranking is not the one the price list predicts, and the top of it is new.**
**Claude Opus 5.5 is the only arm with both perfect recall and perfect cell accuracy** —
1.0000 and 1.0000 over 83,790 cells, the largest denominator of any arm — for **$101.67
against GPT-6 Astra's $209.28 over the same grid**. Astra also returns every row but has
the **lowest cell accuracy of the premium set** (0.9737 over 83,788 cells) — not a
denominator artefact, since it compared essentially as many cells as Opus 5.5 did. So the
most expensive arm is matched on recall and beaten on cell accuracy by an arm costing **at
least 51% less**. That gap is stated as a bound rather than a ratio because Astra's
$209.28 is itself a lower bound (◊): its true cost can only be higher, so the saving can
only be larger.

⚠️ **Opus 5.5's three incomplete runs are not a model property.** All three are the
`AssessmentStep` memory failure on one 17-page document
([#1488](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1488)),
which struck eleven different cells across seven arms in this session and is unrelated to
which extraction model is in use. Opus 5 (132) and Sonnet 5.5 (132) carry the same cause.

※ ⚠️ **Do not read Sonnet 5.5's row as "cheap and perfectly accurate". Its mean recall
hides a bimodal failure, and its cell accuracy is computed on the rows it chose to
return.** 13 of its 133 runs lose almost the entire list, and they are not marginal:

| recall | rows returned | documents | cells compared | cell accuracy on those runs |
|---|---|---|---:|---|
| 0.005 | **2 of 400** | `med_narrow` × 4 cells | *none recorded* | *not scored* |
| 0.025 | **10 of 400** | `med_narrow`, `wide_400`, `manylists_400` × 9 cells | 20 | 1.000 |

So "cell accuracy 1.0000" on those runs means **100% correct on the 10 rows it returned
out of 400**. Every one of the 13 is a **400-row** document; the smaller documents in the
grid are unaffected, which is why a mean of 0.9041 looks survivable and is not. On a
400-row list this model is the **worst** arm in the table, not the cheapest good one.

**Detection is mostly there, but not at the extreme.** 9 of the 13 carry
`extraction_rows_below_ocr_estimate`. The four worst — the 2-of-400 runs — carry only
`extraction_validation_failed` on three of them and **no completeness issue at all** on
the fourth, and the coverage walk recorded no denominator for any of the four, so the
coverage rung could not speak either. Those four are also why
`benchmarks/tests/test_coverage_threshold_evidence.py::test_an_undefined_coverage_run_extracted_nothing`
fails on this data set: its premise is that a run with no coverage denominator extracted
nothing, and these extracted 2 rows.

**Recommendation: do not select Claude Sonnet 5.5 for list extraction on documents of a
few hundred rows or more until this is understood.** It is sound on the smaller documents
in this grid. Three further properties of the model make its remaining columns
non-comparable, independent of the above:

- It **rejects a forced `toolChoice`**, so Schema Enforcement falls back to the prose
  schema and `extraction.forced_tool` is recorded as skipped. Any `forcing` or
  `enforcement` comparison involving this model measures the prose path whatever the knob
  says.
- It rejects `temperature`/`top_p`/`top_k`; the accelerator strips them for this model id,
  so this is invisible here but will fail a call made by any other route.
- **1M is its default window with no `:1m` id**, which makes the derived agentic shard
  budget 578,400 tokens against Sonnet 5's 18,400. Wide shards defeat prompt caching, so
  its cost per document is not comparable with Sonnet 5's on the agentic path unless
  `extraction.agentic.shard_token_budget` is pinned.

† No cost *ratio* is published for Haiku 5.5 — two-band pricing, see the header. Its
dollar figure is a lower bound.
‡ No cost *ratio* is published for Sonnet 5.5 either, for a different reason: its
published per-token prices are **identical to Sonnet 5's**, while `pricing.yaml` prices
Sonnet 5 at Sonnet 4.6's rates. The $62.85 against Sonnet 5's $86.10 is therefore a
pricing-table artefact and **not** evidence that Sonnet 5.5 is cheaper.
◊ GPT-6 Astra's $209.28 is a lower bound for the third reason in the header — it moves to
a long-context band above 272,000 input tokens that `pricing.yaml` cannot express — so no
cost *ratio* is published for it either, and the one cost comparison drawn against it
above is a one-sided bound.

**The cheap end does not fail, it silently under-returns**, which is the more dangerous
shape:

| extraction model (`simplegrid`) | runs | completed | mean recall | mean cell accuracy | `cells_compared` | grid cost | % confidence leaves < 0.9 |
|---|---:|---:|---|---|---:|---|---|
| `simplegrid` control (Sonnet 4.6) | 63 | 61 | 0.9962 | 0.9990 | 39,462 | $29.17 | — |
| Amazon Nova Pro | 63 | **63** | **0.4809** | 0.9361 | 6,960 | $10.89 | 0.007% |
| Amazon Nova Lite | 63 | **63** | **0.4690** | 0.7525 | 8,418 | $8.76 | 0.707% |

Both Nova arms completed **every run with no error** and returned **less than half the
rows**. Nova Pro's 0.9361 cell accuracy is computed over 6,960 cells against the control's
39,462 — it is "94% accurate on the rows it chose to return", not 94% accurate.

⚠️ **And the confidence signal does not catch it.** The `% conf leaves < 0.9` column is the
one to read carefully here, because the numbers are small and easy to inflate: Nova Lite
flags **0.707%** of its confidence leaves and Nova Pro **0.007%**, on arms that returned
**less than half the rows**. A 0.9 threshold is therefore close to inert on this corpus —
the under-return is silent on *both* arms, not just one. There is no safety net in the
model's own confidence here, and a deployment relying on the confidence-alerts filter to
notice a half-empty extraction would not be alerted. The completeness signals in
`extraction_*` are what catch this, and §2's detection discussion is where they are
measured. **Neither model
is viable for list extraction at any size this corpus covers**, and their dollar figures
are not comparable with the seven-arm grid above: they ran the smaller `simplegrid`
matrix, so the only row they can be priced against is that matrix's own Sonnet 4.6
control ($29.17 over 39,462 cells).

Two value statements follow from the seven-arm grid above, both anchored to its own
numbers and their denominators.

**Claude Opus 5 is dominated by Opus 5.5 on this corpus.** It is one of the three arms at
a perfect 1.0000 cell accuracy, over 82,190 cells, at recall 0.9925 — against the shipped
default that is 3.6 points of row recall (0.9925 against 0.9563) for $126.55 against
$86.10 over the same grid, 47% more. Opus 5.5 reaches a *higher* recall (1.0000) over a
*larger* denominator (83,790 cells against 82,190) for **$101.67 against Opus 5's
$126.55, 20% less** — more complete, no less accurate, and cheaper, at once. Both are
flat-rate measurements, which is why this one pair can be quoted as a ratio at all.

**Claude Haiku 5.5 matches the default's per-cell accuracy, and its cost cannot be
quoted.** At 0.9990 cell accuracy over 70,406 cells it is indistinguishable from Sonnet
5's 0.9996 over 74,652 — but the denominators are the point: what it gives up is **row
recall, 0.9269 against 0.9563**, which leaves it the smallest denominator of the seven
arms (70,406 cells against the grid's largest, 83,790). It also carries the highest share
of low-confidence leaves of the seven (2.54%). Its $26.30 is the cheapest figure in the
table and is a **lower bound** (†) — see the banded-pricing warning in the header — so no
cost *ratio* for it appears in this paper. Choose it for accuracy parity on the rows it
does return, never on a quoted saving.

> ⚠️ **The v0.6.8 block below is superseded for the model *ranking*, and is retained only
> for its per-document-size detail.** Every swept model has a v0.6.12 row in the tables
> above, so for *which model to pick* read those. What the v0.6.8 grid still carries that
> the v0.6.12 grid does not is a breakdown **by document size**: its simple-mode and
> advanced-mode tables report each model per document, from a 5-row form to an 800-row,
> 17-page statement, where the v0.6.12 table reports one grid-wide mean per arm. That
> shape is the only reason to read it.
>
> **Do not mix rows across the two generations.** They were produced on different releases
> and, as the header notes, with a different evaluation comparator (`stickler-eval` 0.5.0 →
> 1.0.0 now infers comparators per field), so a v0.6.8 accuracy is not like-for-like with a
> v0.6.12 one, and the dollars are from a different `pricing.yaml`. Claude Opus 5.5, Claude
> Haiku 5.5 and Claude Sonnet 5.5 have no row below at all: their only measured figures are
> in the tables above, plus — for Opus 5.5 against Opus 5 on the two small documents — the
> `opus55value` pair, which has run (40 runs, recall 1.0000, cell accuracy 1.0000 over
> 20,000 cells, $24.18).
>
> ⚠️ **Where a per-document claim below disagrees with the v0.6.12 grid, the grid is the
> current result.** One does: finding 6 and the simple-mode table record GPT-6 Astra
> returning an **empty response** on the 800-row, 17-page `large_narrow` document in every
> simple-mode cell. That is not Astra's behaviour on v0.6.12 — it completed 133 of 133 runs
> at recall **1.0000** over **83,788** cells, which is the full grid including that
> document, so a whole document returning nothing is arithmetically excluded. Read that
> finding as a v0.6.8 observation of a behaviour this release does not reproduce, not as a
> standing property of the model.

#### Carried over from v0.6.8 — the full seven-model grid

| extraction model | runs | fails | recall (a failure counts 0) | cell acc (completed runs) | cost/doc | wall/doc | mean conf |
|---|---|---|---|---|---|---|---|
| **Nova Lite** | 133 | **32** | **0.379** | **0.535** | $0.162 | 559 s | 0.992 |
| **Nova Pro** (simple cells only, see note) | 63 | 0 | **0.482** | 0.96 on the rows it returns; 3 of 7 documents return **0 rows** | $0.174 | 143 s | 0.998 |
| Sonnet 4.6 (control) | 133 | 0 | 0.998 | 1.000 | $0.728 | 199 s | 0.995 |
| **Sonnet 5 (default)** | 133 | 0 | 0.977 | 0.999 | $0.920 | 222 s | 0.995 |
| Sonnet 5 `:1m` | 133 | 0 | 0.964 | 1.000 | $1.404 ⚠️ inflated, see finding 4 | 212 s | 0.994 |
| **Opus 5** | 133 | 0 | **0.993** | 1.000 | $1.291 | 284 s | 0.994 |
| **GPT-6 Astra** | 133 | 0 | 0.924 | **0.902** | $1.286 | 193 s | 0.995 |

The two core cells, which are what a customer actually chooses between:

**Simple mode** (Textract TABLES, separate confidence; 7 documents, 5 → 800 rows):

| model | 5 rows / 1 p | 100 / 3 p | 100 / 4 p (long text) | 400 / 9 p | 400 wide / 9 p | 400 in 4 lists / 9 p | **800 / 17 p** | cost/doc (mean) |
|---|---|---|---|---|---|---|---|---|
| Nova Lite | 1.00 · 1.00 | 1.00 · **0.88** | 1.00 · **0.88** | **0.00** | **0.25** · 0.39 | **0.00** | **0.25** · 0.63 | $0.169 |
| Nova Pro | 1.00 · 1.00 | 0.98 · 0.99 | 1.00 · 1.00 | **0.00** | **0.50** · 0.87 | **0.00** | **0.00** | $0.198 |
| Sonnet 4.6 | 1.00 · 1.00 | 1.00 · 1.00 | 1.00 · 1.00 | 1.00 · 1.00 | 1.00 · 1.00 | 1.00 · 1.00 | 1.00 · 1.00 | **$0.537** |
| **Sonnet 5** | 1.00 · 1.00 | 1.00 · 1.00 | 1.00 · 1.00 | 1.00 · 1.00 | 1.00 · 1.00 | 1.00 · 1.00 | 1.00 · 1.00 | $0.721 |
| Sonnet 5 `:1m` | 1.00 · 1.00 | 1.00 · 1.00 | 1.00 · 1.00 | 1.00 · 1.00 | 1.00 · 1.00 | 1.00 · 1.00 | **0.05** · 1.00 | $0.795 ⚠️ inflated, see finding 4 |
| Opus 5 | 1.00 · 1.00 | 1.00 · 1.00 | 1.00 · 1.00 | 1.00 · 1.00 | 1.00 · 1.00 | 1.00 · 1.00 | 1.00 · 1.00 | $0.885 |
| GPT-6 Astra | 1.00 · 1.00 | 1.00 · 1.00 | 1.00 · 1.00 | 1.00 · 1.00 | 1.00 · 1.00 | 1.00 · 1.00 | **0.00** (empty response) | $0.662 |

(each cell is `recall · cell accuracy`; a single value means both are equal)

**Advanced mode** (same OCR and confidence):

| model | 5 / 1 p | 100 / 3 p | 100 / 4 p | 400 / 9 p | 400 wide | 400 × 4 lists | 800 / 17 p | cost/doc (mean) |
|---|---|---|---|---|---|---|---|---|
| Nova Lite | 1.00 | **FAILED** | **FAILED** | **FAILED** | **FAILED** | **FAILED** | **FAILED** | — (6 of 7 fail) |
| Sonnet 4.6 | 1.00 · $0.09 | 1.00 · $0.38 | 1.00 · $0.62 | 1.00 · $1.43 | 1.00 · $1.91 | 1.00 · $1.38 | 1.00 · $2.72 | **$1.217** |
| **Sonnet 5** | 1.00 · $0.12 | 1.00 · $0.47 | 1.00 · $0.76 | 1.00 · $1.98 | 1.00 · $2.03 | 1.00 · $1.50 | 1.00 · $4.77 | $1.661 |
| Sonnet 5 `:1m` ⚠️ dollars inflated, see finding 4 | 1.00 · $0.06 | 1.00 · $0.66 | 1.00 · $1.29 | 1.00 · $3.89 | 1.00 · $4.76 | 1.00 · $2.76 | 1.00 · $4.10 | $2.504 ⚠️ |
| Opus 5 | 1.00 · $0.18 | 1.00 · $0.67 | 1.00 · $0.80 | 1.00 · $2.75 | 1.00 · $3.39 | 1.00 · $2.61 | 1.00 · $6.04 | $2.348 |
| GPT-6 Astra | 1.00 · $0.18 | 1.00 · $0.88 | 1.00 · $1.24 | 1.00 · $3.58 | 1.00 · $2.95 | 1.00 · $3.17 | 1.00 · $6.57 | $2.653 |

(recall and cell accuracy are 1.000 in every completed advanced run of every model; each cell is `recall · $/doc`)

**Findings**

1. **Every Claude model and Astra reach ceiling accuracy on the agentic path; they differ
   only in price.** Advanced mode returned every row with every value right — 4,410 cells
   compared per model — on Sonnet 4.6, Sonnet 5, Sonnet 5 `:1m`, Opus 5 and Astra, at
   $1.22 / $1.66 / $2.50 / $2.35 / $2.65 per document. Opus 5 costs 41% more than Sonnet 5
   for the same result; Astra **at least** 60% more, a bound rather than a ratio because
   its dollar figure is a lower bound (see the header). ⚠️ The `:1m` figure ($2.50) is inflated by the
   pricing defect described in finding 4 and is not the variant's price — `:1m` is billed
   at exactly plain Sonnet 5's rates. The other four figures are unaffected.
2. **The cheap end is not usable on lists.** Nova Lite in simple mode returns every row of a
   5-row form and a 100-row statement (with 12% of values wrong) and **none** of a 400-row
   one — two of the three 400-row documents came back with 0 matching rows, `COMPLETED`. On
   the agentic path it fails 6 of 7 documents outright after **247 `invalid sequence as part
   of ToolUse` stream errors**, each retried as transient
   ([#895](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/895));
   its grid took 6 hours where the others took 2.5. **Nova Pro is a step up, not a solution:** on the simple-mode grid it is at or near
   1.000 on the three ≤100-row documents (0.98–1.00 recall, 0.985–1.000 accuracy) and
   returns **0 rows on three of the four documents over 100 rows** and half of the fourth —
   `COMPLETED` every time. Its full grid could not be completed: the advanced cells sat in
   Bedrock stream-error retries (`The system encountered an unexpected error during
   processing`, 105 transient errors in 2.5 hours) with 1 of 7 documents finishing in 28
   minutes, so the grid was stopped and re-run as a simple-mode-only `simplegrid` suite (63
   runs, all `COMPLETED`). Both Nova models are **simple-mode, ≤100-row** models on this
   evidence.
3. **Sonnet 4.6 is the best value in this grid, and Sonnet 5 buys nothing on these
   documents.** Both are at recall 1.000 / accuracy 1.000 on every simple and advanced
   core cell; Sonnet 5 costs **34% more** (simple) and **36% more** (advanced). Sonnet 5 is
   the shipped default for reasons outside this corpus (it is the model the product's
   prompts and tool schemas are tuned on, and #839's Payslip variance is a Sonnet-5-only
   behaviour on real forms), so this is not a recommendation to switch — it is the measured
   price of the default on transaction lists.
4. **The 1M-context variant buys capacity this corpus never needs.** Sonnet 5 `:1m`
   matched Sonnet 5's accuracy at every size that fits — and it *truncated* the 800-row
   document in simple mode (43 rows) where Sonnet 5 returned 800. Its window only helps a
   request that would otherwise be refused, and §3 shows the product refuses at 25 pages
   regardless of model, so on this corpus there is no request it rescues. Choose it for
   documents between ~200K and ~1M tokens in a single section, and pair it with advanced
   mode.

   ⚠️ **The costs recorded for `:1m` in this edition's tables are inflated and should not
   be read as a price of the variant.** This edition ran before
   [#899](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/899)
   was fixed, when `pricing.yaml` charged a 2× input / 1.5× output premium on every `:1m`
   request. That premium does not exist on these models: the 1M context window is priced
   at the model's standard per-token rates, so `:1m` and plain Sonnet 5 are billed
   identically at every request size. The +10% (simple) / +51% (advanced) deltas this
   edition reported against plain Sonnet 5 are therefore not the price of the variant —
   what is left of the difference is token volume, not rate (in simple mode the `:1m` arm
   also truncated, so it did less work). The rows have not been re-run or repriced. Token
   volumes, accuracy, recall and timings are unaffected; only the dollar columns for
   `:1m` rows are.
5. **Opus 5 is the most complete model in the grid, by one run.** 0.993 grid recall against
   Sonnet 5's 0.977: the difference is that Opus 5 truncated one 400-row document once
   (`enforce-warn` / `wide_400`, 43 rows) where Sonnet 5 truncated the 800-row document
   three times under three OCR/format variants (§2, finding 2). Both are at cell accuracy
   1.000. At +23% (simple) / +41% (advanced) it is a price for consistency on long lists in
   simple mode, not for accuracy.
6. **GPT-6 Astra gets values wrong where the Claude models do not, and returns nothing on
   the 17-page document.** Grid cell accuracy **0.902**: with enforcement *off* Astra emits
   amounts as strings (`"0.00"`, `"-13.70"`) on every document, which the typed match
   rejects — turn coercion on and they pass, which is a real reason to keep
   `extraction.validation`/`coercion` at their defaults with this model. And in **every**
   simple-mode cell Astra returned an **empty response** on `large_narrow` (`raw_output: ""`,
   `parsing_succeeded: false`, an `extraction_incomplete` warning, status `COMPLETED`) — 8 of
   8 cells here and 5 of 5 repeats in §5.2. Its larger window is exactly what lets that
   request be *accepted*; Sonnet 5 refuses it or truncates it.

### 5.2 Is a premium model worth it? (`astravalue`, `astracap`, `opus55value`)

Three premium questions, one method: hold everything but `extraction.model` fixed, choose
the documents by **page count** — page images, not OCR text, are what fill a request
(~11,000 tokens per page at 300 DPI against ~600 of text) — and repeat every cell five
times, so that "no" is a reportable answer. All three suites ran on v0.6.12, and
`opus55value` ran for the first time in any edition.

| suite | the pair, and what it asks | runs | completed | mean recall | mean cell accuracy | `cells_compared` | suite cost |
|---|---|---:|---:|---|---|---:|---|
| `opus55value` | Opus 5.5 against Opus 5 — is the newer model cheaper for the same result? | 40 | **40** | **1.0000** | **1.0000** | 20,000 | $24.18 |
| `astravalue` | Sonnet 5 against GPT-6 Astra — does a larger window buy data? | 100 | 99 | 0.8747 | 0.9998 | 68,448 | $141.63 ◊ |
| `astracap` | both models at the 66-page / 3,200-row ceiling | 12 | **8** | 0.5841 | 1.0000 | 44,858 | $92.64 ◊ |

Mean confidence over the premium study is 0.9919 on `opus55value` and 0.9901 on
`astravalue`, and almost nothing is scored low anywhere in it: about **2 of 30,276**
confidence leaves fall below 0.9 on `opus55value`, **77 of 108,468** on `astravalue` and
**0 of 68,078** on `astracap`. A review queue fed by a 0.9 threshold would see essentially
nothing from any of these arms — including, as the Astra columns below show, from the runs
that returned no usable row at all.

◊ Both Astra-bearing suite totals are **lower bounds** and carry no cost *ratio*, for the
reason given in the header: above 272,000 input tokens Astra moves to a long-context band
`pricing.yaml` cannot express. Where this section compares Astra on cost it states two
dollar figures from the same suite and a **one-sided bound** over them, which is what a
lower bound supports.

⚠️ **`astracap`'s four incomplete runs are the measured outcome, not a failure of the
run.** That suite exists to find the 66-page ceiling, and all four are the same event: the
66-page `scale_3200.pdf` in a **simple-mode** cell — two repeats of Sonnet 5 and two of
Astra — refused up front with `ExtractionInputTooLarge`, nothing attempted and nothing
wrong returned. Read the suite's 0.5841 mean recall with that in front of it: it is four
deliberate refusals averaged in with eight runs that did execute, and on its own it looks
like a quality result and is not. The eight that executed are the four advanced cells, and
they carry the ceiling answer: every default-shard run returned 3,200 of 3,200 rows, and
three of the four runs on the wider shard setting came back short, which the ceiling table
below singles out.

#### Opus 5.5 against Opus 5 — measured for the first time

The comparator is Opus 5 rather than the shipped default because the claim under test names
Opus 5: cheaper input and output ($4/$20 per 1M against $5/$25), a cache read at 0.05×
input where every other model here is 0.1×, and fewer tokens for the same task. Those three
compound, and the rate card predicts about −20%. The suite is **two documents** —
`small_narrow` (3 pp / 100 rows) and `med_narrow` (9 pp / 400 rows) — × two modes × two
models × 5 repeats. The two larger documents `astravalue` uses are deliberately absent:
Opus 5 and Opus 5.5 share one window and one sizing budget, so in simple mode both arms
would be refused on both documents and an A/B whose arms fail identically measures nothing
while still being billed. The ceiling question belongs to `astracap`, which owns it for
every model.

| arm | 3 pp / 100 rows | 9 pp / 400 rows | `cells_compared` | arm cost |
|---|---|---|---:|---|
| Opus 5 · simple | 1.0000 · 1.0000 · $0.26 | 1.0000 · 1.0000 · $0.96 | 5,000 | $6.10 |
| **Opus 5.5 · simple** | 1.0000 · 1.0000 · **$0.22** | 1.0000 · 1.0000 · **$0.82** | 5,000 | **$5.20** |
| Opus 5 · advanced | 1.0000 · 1.0000 · $0.31 | 1.0000 · 1.0000 · $1.01 | 5,000 | $6.60 |
| **Opus 5.5 · advanced** | 1.0000 · 1.0000 · **$0.26** | 1.0000 · 1.0000 · **$1.00** | 5,000 | **$6.27** |

(each document column: `recall · cell accuracy · cost/doc`, means of 5. `cells_compared`
and `arm cost` are totals over that arm's 10 runs — 1,000 cells per run on the 3-page
document, 4,000 on the 9-page one)

**Quality is a tie at the ceiling, and it is a tie on identical denominators.** All 40 runs
reached `COMPLETED`, every one returned every row, and every returned value was right:
recall 1.0000 and cell accuracy 1.0000 over **10,000 cells compared per model**. So the
pair reduces to price, and both models are flat-rate, which is why this one can be quoted
as a ratio at all: **$11.47 against $12.70 over the same 10,000 cells — 10% less.**

**The saving lands in simple mode, not on the agentic path, which is the opposite of what
the rate card suggests.** Simple mode is $5.20 against $6.10, **15% less**; advanced mode is
$6.27 against $6.60, **5% less**. Simple mode is one large request, so the cheaper input
rate reaches the whole bill. Advanced mode re-sends a prompt prefix per shard, which is
where the 0.05× cache read was expected to compound, and it measurably does not. The
predicted −20% is therefore the optimistic end: a deployment whose documents go through
advanced mode should budget the 5% figure.

Both arms resolve to the same `reasoning_effort` in the committed cell configuration (`low`
on all four cells), so the pair is matched on that axis as it ran. To compare the two models
at high effort instead, re-run with `--set reasoning_effort=high` on both arms.

#### Sonnet 5 against GPT-6 Astra — does the larger window buy data?

Four documents chosen so that the window is the variable. `small_narrow` (3 pp) and
`med_narrow` (9 pp) fit both models; `large_narrow` (17 pp, ~203K tokens) is the first
document Sonnet 5 cannot reliably take in one request; `dense_250` (26 pp, ~304K, 8
columns, 4 interleaved lists, OCR noise) is the one Astra's window reaches most
comfortably. 5 repeats per cell, 100 runs.

| cell | 3 pp / 100 rows | 9 pp / 400 rows | **17 pp / 800 rows** | **26 pp / 250 rows, dense** |
|---|---|---|---|---|
| Sonnet 5 · simple | 1.0000 · 1.0000 · $0.20 (1,000) | 1.0000 · 1.0000 · $0.70 (4,000) | **0.4935** · 1.0000 · $0.97 (3,948) | 1.0000 · 1.0000 · $1.42 (2,500) |
| Astra · simple | 1.0000 · 1.0000 · $0.39 (1,000) | 1.0000 · 1.0000 · $1.31 (4,000) | **1.0000** · 1.0000 · $3.07 (8,000) | **0.0000** · *not scored* · $1.32 (0) |
| `global.` Astra · simple | 1.0000 · 1.0000 · $0.31 (1,000) | 1.0000 · 1.0000 · $1.10 (4,000) | **1.0000** · 1.0000 · $2.60 (8,000) | **0.0000** · *not scored* · $1.02 (0) |
| **Sonnet 5 · advanced** | 1.0000 · 1.0000 · $0.21 (1,000) | 1.0000 · 1.0000 · $0.69 (4,000) | **1.0000 · 1.0000 · $1.36** (8,000) | **1.0000 · 0.9992 · $1.86** (2,500) |
| Astra · advanced | 1.0000 · 1.0000 · $0.31 (1,000) | 1.0000 · 1.0000 · $1.96 (4,000) | 1.0000 · 1.0000 · $3.36 (8,000) | 1.0000 · 0.9976 · $4.17 (2,500) |

(each cell: `recall · cell accuracy · cost/doc`, means of 5, with that cell's
`cells_compared` in parentheses. *not scored* means no row matched a truth key, so there
was no cell to grade)

**Verdict: still no — and the reason is not the one earlier editions gave.** Read it column
by column.

- **Where both models fit (3 and 9 pages)** both are at ceiling on identical denominators
  (1,000 and 4,000 cells), and Astra costs more in every cell. That is the control, and it
  behaves as a control should.
- **On the 17-page document the window does buy data in simple mode.** Astra returns all
  800 rows in 5 of 5 runs, 8,000 cells at accuracy 1.0000, and so does the `global.`
  endpoint. Sonnet 5 is **bimodal** there — two runs returned all 800 rows, three returned
  43, 141 and 190 — for a cell mean of 0.4935 over 3,948 cells. All three short runs carry
  `extraction_rows_below_ocr_estimate`, so the shortfall is flagged rather than silent. One
  wrinkle worth knowing: the single run in this whole suite that did **not** reach
  `COMPLETED` is one of the two Sonnet 5 runs that returned every row, so on this cell the
  completion count and the completeness move in opposite directions.
- **On the 26-page document the window buys nothing, and it fails silently.** Sonnet 5 in
  simple mode returns all 250 rows in 5 of 5 at cell accuracy 1.0000 over 2,500 cells.
  Astra returns **no row that matches a truth key** — 0 of 250, all 250 truth ids recorded
  as gaps, no cell scored, status `COMPLETED` — on both the `us.` and the `global.`
  endpoint, 5 of 5 each, for $1.32 and $1.02 a document. It is not an empty response: the
  run carries **757 confidence leaves, the same count as the advanced runs that returned
  all 250 rows**, so the model produced a full-looking result whose every row key is
  unusable. Nothing in the run reports a problem.
- **Advanced Sonnet 5 does the job on all four documents** — recall 1.0000 everywhere, cell
  accuracy 0.9992–1.0000 — and it is the cheapest arm that does. Over the two advanced
  arms' identical **15,500 cells** it is **$20.62 against Astra's $49.04, at least 58%
  less**, a bound rather than a ratio because Astra's figure can only be higher, so the
  saving can only be larger. The accuracy gained per extra dollar is zero or negative in
  every cell.

**What Astra is measurably good at here:** returning a complete 800-row list in *simple*
mode where Sonnet 5 truncates, cell accuracy 1.0000 on the agentic path at every size, a
`global.` endpoint that cost less than `us.` on the same documents ($25.08 against $30.40
over the same 13,000 cells), and implicit prompt caching with no `<<CACHEPOINT>>` markers.
What it is not, on this evidence, is a reason to pay for extraction that advanced mode
already gets right at a fraction of the price.

#### The ceiling (`astracap`) — 66 pages, 3,200 rows, 6 cells × 2 repeats

| cell | repeat 0 | repeat 1 | `cells_compared` | $/doc |
|---|---|---|---:|---|
| Sonnet 5 · simple | **FAILED** `ExtractionInputTooLarge` | **FAILED** | 0 | $1.05 · $1.05 |
| Astra · simple | **FAILED** `ExtractionInputTooLarge` | **FAILED** | 0 | $1.05 · $1.05 |
| **Sonnet 5 · advanced, default shards** | **1.0000 · 1.0000** | **1.0000 · 1.0000** | 12,800 | **$5.80 · $5.82** |
| Astra · advanced, default shards | 1.0000 · 1.0000 | 1.0000 · 1.0000 | 12,800 | $18.39 · $16.25 |
| Sonnet 5 · advanced, 25-page "wide" shards | **0.7550** · 1.0000 | **0.6347** · 1.0000 | 8,894 | $8.31 · $7.98 |
| Astra · advanced, 25-page "wide" shards | 1.0000 · 1.0000 | **0.6194** · 1.0000 | 10,364 | $14.79 · $11.09 |

(each repeat: `recall · cell accuracy`; `cells_compared` is the total over both repeats)

Three results, in the order they matter.

**Simple mode refuses at this size, and refusing is the right behaviour.** All four
simple-mode runs are turned away up front with `ExtractionInputTooLarge` — ~794,000
estimated tokens against Astra's nominal 1,050,000 window, so the nominal figure is not the
usable one once page images are in the request. Nothing is attempted and nothing wrong is
returned: the whole $1.05 is the $0.99 of OCR and $0.06 of classification that preceded the
refusal, with **$0.00 recorded against extraction or assessment** in all four runs.

**Both models hold the ceiling on the agentic path at default shards:** 3,200 of 3,200 rows
at cell accuracy 1.0000 over 12,800 cells each. Sonnet 5 costs **$5.80 and $5.82** against
Astra's **$18.39 and $16.25** on the same document, so Sonnet 5 is **at least 66% less** —
a bound, not a ratio. At the ceiling the premium model buys nothing and costs several times
as much.

⚠️ **The 25-page "wide" shard — the setting whose purpose is to let a large-window model use
its window on the agentic path — came back short on three of its four runs, across both
models, and did it silently.** All four reached `COMPLETED`, so a completion count says
nothing here, and they
returned 2,416 and 2,031 of 3,200 rows on Sonnet 5 and 3,200 and 1,982 on Astra, against
3,200 of 3,200 in all four default-shard runs. Every row they did return was correct — cell
accuracy 1.0000 throughout, on denominators 30% and 19% smaller than the default-shard
arms' 12,800 — which is exactly the shape §3 warns about: the accuracy column stays perfect
while the data goes missing. **Leave `shard_pages` at its default at this size**; widening
it trades completeness for a cost that is not even lower on Sonnet 5 ($8.31 and $7.98
against $5.80 and $5.82).

### 5.3 Classification model — the shipped default is enough now

The v0.6.7 edition recommended Claude Haiku 4.5 for classification because Nova 2 Lite
over-split every document 2–3×. That was a prompt defect, fixed in #817, and the axis has
been re-measured with the fixed prompt ever since. On v0.6.12 the axis is **Nova 2 Lite ·
Claude Haiku 5.5 · Claude Sonnet 5**, each over the full 19-cell × 7-document `coresynth`
grid at the control extraction model (Sonnet 4.6), 133 runs per arm, with one section the
truth for every document.

| classification model | runs | completed | sections correct | mean recall | mean cell accuracy | `cells_compared` | classification $/doc | total $/doc | grid cost |
|---|---:|---:|---:|---|---|---:|---|---|---|
| **Nova 2 Lite** *(shipped default)* | 133 | 130 | 132 / 133 | **0.9982** | 0.9995 | 83,562 | **$0.0069** | **$0.663** | **$88.15** |
| Claude Haiku 5.5 | 133 | 130 | **133 / 133** | 0.9974 | 0.9995 | 83,472 | $0.0086 † | $0.662 † | $88.07 † |
| Claude Sonnet 5 | 133 | 132 | 132 / 133 | **0.9982** | 0.9991 | 83,560 | $0.1883 | $0.856 | $113.81 |

† Haiku 5.5's dollar figures are **lower bounds** and carry no cost *ratio* — banded
pricing, see the header. Its true classification cost can only be higher than $0.0086 per
document, which is why it appears here as a quality result and never as a saving.

**All three arms are graded on within 0.2% of the same number of cells** — 83,472 to 83,562
— so unlike the extraction grid in §5.1 these rows need no denominator caveat, and the
differences between them are as small as they look. The incomplete runs are not a classifier
property either: every one of the seven is the 17-page `large_narrow` document — the same
document every incomplete run in §5.1 lands on — and the arm that lost the fewest (Sonnet 5,
one run) is not the arm with the best boundary score.

**Guidance: keep Nova 2 Lite. Upgrading the classification model buys nothing measurable
and costs 29% more.** The classifier is at its ceiling on this corpus in all three arms: the
boundary is right on 132 of 133 runs under the default, 133 of 133 under Haiku 5.5 and 132
of 133 under Sonnet 5 — and the two single misses are on different documents
(`wide_400` under the default, `manylists_400` under Sonnet 5), so the premium arm does not
even fix the one case the default gets wrong. Downstream, recall is identical to four
decimal places between the default and Sonnet 5 (0.9982 against 0.9982) and cell accuracy
is marginally *worse* (0.9991 against 0.9995) on an all-but-identical denominator.

What the upgrade does change is the bill. Sonnet 5's classification phase costs **$0.1883
per document against the default's $0.0069 — 27×** — which lifts the whole-grid cost from
**$88.15 to $113.81, 29% more**, for a result that is not distinguishable. That is the
clearest "do not touch this knob" in the paper.

The one boundary shape the default still gets wrong — a reprinted running header, §7 —
Sonnet 5 also gets wrong at both prompts (§7's boundary table is Nova 2 Lite; the v0.6.7
probe measured Haiku 4.5 and Sonnet 5 failing the same shape), so upgrading the classifier
is not the fix for #750 either. Where the classifier *does* differ is the confidence score
it emits: the v0.6.7 edition measured Haiku 4.5's calibration separation at 0.207 against
Nova 2 Lite's 0.044 on DocSplit-Poly-Seq, and nothing here changes that — if you *act* on
classification confidence, that measurement is the one to read. Haiku 4.5 itself is no
longer in the sweep; `--set classification_model=haiku45` reproduces it on demand, and its
figures would not be comparable with the rows above (different release, different
`pricing.yaml`, and the `stickler-eval` comparator change described in the header).

### 5.4 Confidence model — Nova Lite is enough, and the alternatives are not measurably better

Re-measured in full on v0.6.12 — the whole 19-cell × 7-document `coresynth` grid, 133 runs
per model, extraction held at the control (Sonnet 4.6):

| confidence model | runs | completed | mean recall | mean cell accuracy | `cells_compared` | mean confidence | leaves below 0.9 | coverage | assessment $/doc | total $/doc | grid cost | wall |
|---|---:|---:|---|---|---:|---|---|---|---|---|---|---|
| **Nova Lite** *(shipped default)* | 133 | **133** | 0.9945 | 0.9991 | 83,396 | 0.9955 | ~**8** of 119,943 | 0.9999 | **$0.102** | **$0.686** | **$91.20** | 182 s |
| Nova 2 Lite | 133 | **133** | **0.9989** | 0.9992 | **83,636** | 0.9981 | **0** of 119,949 | **1.0000** | $0.146 | $0.726 | $96.50 | **135 s** |
| Claude Sonnet 5 | 133 | **133** | 0.9982 | **0.9995** | 83,562 | 0.9754 | ~**283** of 119,980 | **1.0000** | $0.912 | $1.503 | **$199.95** | 146 s |

**All three arms completed every run and were graded on within 0.3% of the same number of
cells** (83,396 to 83,636), so these rows compare directly. The low-confidence column is
given as a **count of confidence leaves** rather than a share, because the counts are what
decides whether a review queue is worth having and the three arms differ by a factor rather
than in level.

⚠️ **Confidence *discrimination* cannot be measured on this corpus, so no AUROC column is
published.** The extraction is too accurate for it: across ~79,000 calibration observations
per arm there are only **62 to 96 errors — an error rate of 0.08% to 0.12%** — and
`calibration_reliable` is **false on 108 of 126 scored runs** in both Nova arms and 103 of
126 in the Sonnet 5 arm. Computed regardless, on the four or five runs per arm that clear
the reliability bar, both Nova models return AUROC exactly **0.5000** — the degenerate value
when there is nothing to rank — and Sonnet 5 returns 0.7817 on the strength of a few dozen
errors. Neither figure supports a conclusion about confidence quality. **On this corpus the
choice rests on cost, coverage and threshold behaviour**; establishing discrimination needs
a corpus with a materially higher error rate.

**Guidance: keep Nova Lite, and read the low-confidence column before changing it.** The
default is the cheapest of the three at **$0.686 per document** total, its coverage is
0.9999, and nothing measurable here distinguishes its confidence *quality* from the
alternatives. **Nova 2 Lite is the marginal improvement at +5.8%** — $96.50 against the
default's $91.20 over the same grid — and it earns that in three small ways rather than
one: the highest recall of the three (0.9989 on the largest denominator, 83,636 cells),
complete 1.0000 coverage, and the fastest wall at 135 s against 182 s. Take it if any of
those three matter; the accuracy difference alone does not justify it.

**Sonnet 5 as the confidence model is not an accuracy upgrade. It is a far more
conservative scorer, and that is the whole of what it buys.** Its cell accuracy (0.9995
over 83,562 cells) is a rounding difference from the default's (0.9991 over 83,396), and
its recall is lower than Nova 2 Lite's. What it does differently is score low: about **283
of 119,980** confidence leaves below 0.9, against the default's **8 of 119,943** and Nova 2
Lite's **0 of 119,949** — roughly **35× as many flagged fields as the default** — for
**$199.95 against $91.20 over the same grid, 2.2×**, almost all of it in the assessment
phase ($0.912 per document against $0.102, **9×**, which more than doubles the total bill).
Choose the confidence model and the review threshold together: on either Nova arm a 0.9
threshold is close to inert on this corpus, and on Sonnet 5 it is the one setting that
actually populates the confidence-alerts filter.

**Whether that conservatism is worth paying for depends on whether your corpus has anything
wrong in it**, and the one grid cell that does is Bedrock-LLM OCR (`core-llm-simple-sep`,
7 runs per arm), where 1–2% of cells are corrupted:

| confidence model | cell accuracy on that cell | `cells_compared` | mean confidence there | leaves below 0.9 |
|---|---|---:|---|---|
| Nova Lite *(default)* | 0.9824 | 4,016 | 0.9961 | ~**3** of 6,667 |
| Nova 2 Lite | 0.9843 | 4,256 | 1.0000 | **0** of 6,661 |
| **Claude Sonnet 5** | 0.9909 | 4,182 | **0.9588** | **150** of 6,669 |

Two things follow, and they point in opposite directions. **The two cheap confidence models
are effectively blind here**: with 1.6–1.8% of cells wrong they still score everything at
0.996–1.000 and flag 3 leaves and 0 leaves respectively, so a review queue fed by them would
have caught almost none of the corrupted identifiers. **Sonnet 5 does see them** — 150 of
6,669 leaves on that cell, against 283 across the whole grid, so more than half of
everything it flags anywhere is concentrated on the one cell that has errors to find. That
is the right direction and roughly the right magnitude, and it is the only positive evidence
in this edition that any of the three scores wrongness rather than just scoring low.

So: **Nova Lite at $0.102/doc is right where the extraction is trustworthy and the score is
for after-the-fact triage.** If the score gates human review *and* the corpus produces wrong
values — an OCR backend that corrupts identifiers, a model that fabricates — Sonnet 5 is the
only confidence model in this grid that measurably lowered its score on them, and it costs
nine times the assessment it replaces. Measure the separation on your own error-bearing
sample before paying that. The v0.6.7 edition's
[classification-confidence study](studies/classification-confidence.md) on DocSplit-Poly-Seq
remains the reference for how these models separate right from wrong when there is enough
wrong to separate.

### 5.5 Recommendations by document profile

**The headline of this edition's model axis is that on a large document the *mode* matters
more than the model.** Measured on `large_narrow` (800 rows of ground truth, 17 pages),
**all seven `coresynth` arms return 800 of 800 rows in advanced mode** — 6 of 6 runs each,
recall 1.0000 and cell accuracy 1.0000 over 9,600 cells per arm, no exceptions. In the
thirteen simple-mode cells the same seven split into two groups, and the shipped default is
in the wrong one:

| arm | simple mode (13 cells) | advanced mode (6 cells) |
|---|---|---|
| **Claude Sonnet 5.5** | 12/13 completed, **800 / 800 (1.0000)**, 20,800 cells, acc 1.0000 | 6/6, 800/800 (1.0000), 9,600 cells, acc 1.0000 |
| **Claude Opus 5.5** | 10/13 completed, **800 / 800 (1.0000)**, 20,800 cells, acc 1.0000 | 6/6, 800/800 (1.0000), 9,600 cells, acc 1.0000 |
| GPT-6 Astra | 13/13 completed, **800 / 800 (1.0000)**, 20,800 cells, acc **0.9615** | 6/6, 800/800 (1.0000), 9,600 cells, acc 1.0000 |
| Claude Opus 5 | 12/13 completed, ~738 / 800 (0.9231), 19,200 cells, acc 1.0000 | 6/6, 800/800 (1.0000), 9,600 cells, acc 1.0000 |
| **Claude Sonnet 5** *(shipped default)* | 13/13 "succeeded", **~454 / 800 (0.5680)**, 11,814 cells, acc 0.9989 | 6/6, 800/800 (1.0000), 9,600 cells, acc 1.0000 |
| Claude Sonnet 5 `:1m` | 13/13 "succeeded", ~401 / 800 (0.5009), 10,420 cells, acc 0.9985 | 6/6, 800/800 (1.0000), 9,600 cells, acc 1.0000 |
| Claude Haiku 5.5 | 13/13 "succeeded", ~351 / 800 (0.4392), 9,136 cells, acc 1.0000 | 6/6, 800/800 (1.0000), 9,600 cells, acc 1.0000 |

(the two Nova arms are not in this table: they ran the smaller `simplegrid` matrix and
neither finishes the agentic path, #895 — see §5.1)

⚠️ **The three arms at the bottom do not fail on this document — they truncate, and report
success.** Thirteen simple-mode runs out of thirteen reach `COMPLETED` on each of Sonnet 5,
Sonnet 5 `:1m` and Haiku 5.5 while discarding between 43% and 56% of the rows, and the
`cell_accuracy` column stays at 0.9985–1.0000 throughout **because it is computed on the
rows they did return** — the denominators are 9,136 to 11,814 cells against the complete
arms' 20,800. Nothing downstream flags it. Note also that Haiku 5.5, the arm that lost the
most rows, is the one with a perfect cell accuracy here. This is §3's "`COMPLETED` is not the
signal to trust" reappearing on the model axis: **rank by completeness, never by completion
rate, and never by an accuracy figure without its denominator.**

The converse also holds on this release, and it is why a completion count is the wrong
summary in both directions: **Opus 5.5 has the lowest simple-mode completion count in the
table (10 of 13) and returns every row in the runs that completed.** Its three
non-completions are the `AssessmentStep` memory failure of
[#1488](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1488),
which struck eleven cells across seven arms in this session and is **not a property of this
model** — Opus 5 and Sonnet 5.5 carry the same cause. Across the whole grid Opus 5.5 is
**130 of 133** for the same reason, and at recall 1.0000.

With that in front of it, the per-profile answer. "Cheapest arm at ceiling" means the
cheapest arm that reaches recall 1.0000 (or within noise of it) *and* cell accuracy 1.0000
on that profile's documents, on a denominator stated in the same cell.

| Document profile | Cheapest arm at ceiling | Mode | Measured basis | Notes |
|---|---|---|---|---|
| Small forms, ≤ 1 page, few fields | **any model, Nova Lite included** | simple | `tiny_form`: all seven `coresynth` arms 19/19 at recall 1.0000, cell accuracy 1.0000 over 190 cells (Astra 0.9737); both Nova arms 9/9 at 1.0000 / 1.0000 over 90 cells on the `simplegrid` matrix | Genuinely free. Nova Lite at $0.014/doc and the default at $0.056/doc produce the same result; pick on price |
| Statements ≤ ~100 rows / ≤ 4 pages | **the shipped default** (Sonnet 5) | simple | `longdesc_100` and `small_narrow`: Sonnet 5 at recall 1.0000, cell accuracy 1.0000 over **3,800 cells each**, $0.33 and $0.21/doc | **Not** the cheap end. Nova Lite completes every run and gets 12.5% of values wrong (cell accuracy 0.8750 over 1,800 cells on `longdesc_100`); Haiku 5.5 matches the default's accuracy but drops to recall 0.980 on `small_narrow` (3,724 cells against 3,800) and its cost cannot be quoted as a saving |
| Statements 400–800 rows / 9–17 pages | **the shipped default** at 400 rows; **advanced mode** by 800 | simple to 400, **advanced** at 800 | At 400 rows (`med_narrow`, `wide_400`, `manylists_400`) Sonnet 5 is 19/19 at recall 0.9950–1.0000 over 15,124–15,200 cells for $0.77–1.01/doc, and Sonnet 5 `:1m` reaches a clean 1.0000 over 15,200 on all three for about the same money ($0.75–1.07). At 800 rows see the table above | ⚠️ **Do not use Claude Sonnet 5.5 here.** It is the one arm that collapses in this band — recall 0.6366 on `med_narrow` (9,660 cells) and 0.7434 on `manylists_400` (11,300) — see the `※` block in §5.1 and [#1494](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1494). Haiku 5.5 dips to 0.948–0.949 on two of the three |
| **Statements ≥ ~800 rows, or 25+ page packets** | **any model — but only in `advanced` mode** | **advanced** | `large_narrow`: all seven arms 6/6 at 800/800 rows, 9,600 cells, accuracy 1.0000 in advanced; four of the seven return 44–92% in simple | **This is the configuration decision, not a model decision.** In simple mode the default loses 43% of the rows and reports success |
| 1,000+ rows / 25–66 pages | **the shipped default, advanced, at `shard_pages: default`** | **advanced** | `astracap` on 3,200 rows / 66 pages: Sonnet 5 and Astra both 2/2 at recall 1.0000 and cell accuracy 1.0000 over 12,800 cells, for **$5.80–5.82 against $16.25–18.39** per document. Both *simple* cells refuse outright with `ExtractionInputTooLarge`. §3's `scaling` series holds 1.0000/1.0000 through the same ceiling at the control model | Simple mode refuses rather than truncating at this size, which is the correct behaviour. ⚠️ **Do not widen the shard:** the 25-page "wide" shard completed every run and returned 1,982–3,200 of 3,200 rows, silently, at a higher price — §5.2 |

**Where a premium model is actually required: nowhere in this corpus, once advanced mode is
set.** The one thing the premium end buys is a complete 800-row list in *simple* mode, where
only Sonnet 5.5, Opus 5.5 and Astra reach 1.0000 — and advanced mode gets all seven arms
there at 9,600 cells and accuracy 1.0000 for a fraction of the price. If simple mode is a
hard constraint on a document that size, **Opus 5.5 is the arm to pick**: recall 1.0000 and
cell accuracy 1.0000 over 20,800 cells, where Sonnet 5.5 is sound at 800 rows but unusable
at 400 and Astra is the only arm in the grid below 0.99 cell accuracy (0.9737 over 83,788
cells).

**Over the grid as a whole the accuracy-per-dollar and the completeness-per-dollar answers
are not the same arm, and the gap between them is 18%.** **Claude Opus 5.5 is the only arm
with both perfect recall and perfect cell accuracy** — 1.0000 and 1.0000 over **83,790
cells, the largest denominator of any arm** — for **$101.67**, which is **at least 51% below
GPT-6 Astra's $209.28** and **18% above the shipped default's $86.10**. Astra over that same
grid is **at least 2.4× the default** ($209.28 against $86.10), stated as a bound rather
than a ratio because Astra's figure is itself a lower bound, so the multiple can only be
larger — and it is the one arm the extra money buys *less* accuracy on.

So the two answers, plainly. **Keep the shipped default and turn on advanced mode for large
documents** is right on cost, and it is what the per-profile table above recommends
everywhere. **Pay 18% more for Opus 5.5** is right where a complete, exactly-correct list
is worth 18% — and it is the only arm in this edition that delivers one on every document in
the grid. Nothing above those two is worth buying: the most expensive arm in the sweep is
matched on recall and beaten on cell accuracy by one costing at least half as much.

## 6. Product improvement backlog (surfaced by this study)

Items closed since the v0.6.7 edition are struck through and dated; the open ones are
ordered by how much data they can cost a customer.

1. **🚨 A class wrapped with `x-aws-idp-multi-instance` whose instances hold a long list
   cannot finish confidence assessment —
   [#894](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/894) (P0, new).**
   The confidence sizer batches over the *outer* instance list ("cols=2, per_row ~80"), so one
   "row" is a whole statement carrying a 100-row `Transactions` list, every Nova Lite call
   truncates, halving the batch cannot help because one row alone does not fit, and the
   ladder runs until the 900-second Lambda dies — three times. 3 of 3 repeats on a 100-row
   single statement failed; the 20-row packet passed (§7). Fix: batch over the inner list per
   instance, and give up (and report) when a batch of one still truncates.
2. **🚨 Simple mode still reports `COMPLETED` on a truncated list (P0, narrowed).** #843 made
   the truncation *visible* (`extraction_rows_below_ocr_estimate` on every truncated run in
   this study) but not *terminal*: a 43-of-800 result completes and costs less than a full
   one. §3 shows the same document completing or truncating depending on OCR backend and
   output format. A row-count check against the OCR estimate that fails the section (or
   routes it to advanced mode) would close the last silent path.
3. **Nova Lite cannot run the agentic path, and the product treats each failure as
   transient —
   [#895](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/895) (P1, new).**
   247 `Model produced invalid sequence as part of ToolUse` stream errors in one 133-run
   grid, every one on `us.amazon.nova-lite-v1:0`, each retried by the agentic ladder and then
   by Step Functions; advanced-mode documents sat in the shard map for over 45 minutes. The
   error should be classified as non-retryable for the model, and Nova Lite documented as
   simple-mode-only (§5).
4. **GPT-6 Astra's 1.05M window is not usable in one request with page images (P2, new).**
   A 66-page simple request estimated at ~794,000 tokens is refused by Bedrock as too long,
   and a 25-page agentic shard is refused the same way, on Astra as on Sonnet 5 (§5). Either
   the token estimator under-counts Astra's image tokens or the model's per-request limit
   with images is below its nominal window; either way the `max_pages_per_shard` guidance
   for Astra should not assume the nominal figure.
5. **Astra in simple mode returns an empty response on a 17-page document, 13 of 13 (P2,
   new).** `raw_output: ""`, `parsing_succeeded: false`, an `extraction_incomplete` warning —
   and `COMPLETED`. The same request on Sonnet 5 returns 800 rows or truncates; Astra returns
   nothing. Whether this is a model behaviour on long inputs or a response-format mismatch
   is not established here; until it is, Astra is an advanced-mode-only model in this guide.
6. **The agentic cost variance at Sonnet 5 is the model, not the classifier (P2).** With one
   section per document the Sonnet 4.6 advanced cells run at CV 3–13%; Sonnet 5's run at
   25–40% on the same document (§4). Bounding the agent's turn count, or surfacing it in the
   metering so a customer can see why one document cost 2.5× another, would make the shipped
   default budgetable.
7. **The TestRunner rejected any compressed configuration carrying a float —
   [#892](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/892).**
   Found because the OCR-benchmark preset has `temperature: 0.0`; every Test Studio run
   against such a profile failed at submit. **Fix in review** (PR #893,
   `parse_float=Decimal`); hot-patched on the benchmark stack for this edition.
8. ~~**`sectionSplitting: llm_determined` over-split single documents (#726).**~~ **Closed
   by #817** (2026-09-10): 7 of 7 sections in every cell of §2, 1 of 1 at every size in §3,
   5/5 on the unpaginated statement in §7's boundary A/B. The reprinted running-header shape
   (#750) is still open: 0/5 at both prompts.
9. ~~**Forced tool use serialised group attributes to JSON strings on Sonnet 5 (#783).**~~
   **Closed by #794**: valid rate 1.000 on every forced section in §2 and §7.
10. ~~**Integrated confidence + simple extraction returned partial lists.**~~ **Closed by
    #795**: 1.000 on 7 of 7 documents and 8 of 8 repeats (§2.1).
11. ~~**The confidence batch-splitting ladder could outlive its Lambda on an 800-row
    list.**~~ **Closed by #861** for the ordinary case: none of the 11 Sonnet 5 draws at 800
    rows lost its document in the confidence pass (the 3 that came back short were extraction
    truncations, §3), and the shipped batch ceiling now matches the tuned setting (§7). Item 1 is the same
    ladder failing on a different shape.
12. **Reference-corpus cells in the standard release run.** This edition ran them (§2c) at a
    cost of about 900 document runs per model. A sampled variant (5 documents per corpus)
    would keep real-world accuracy in every release audit at a tenth of the price.

---

## 7. Configuration options — feature A/Bs on one stack, one build (measured 2026-09-12)

Each pair below differs on exactly one config key, on the same deployed stack with identical
code, `repeats: 3` or more. Extraction model Sonnet 4.6 (the matrix's control) unless stated;
the point of each is the *delta*, not the absolute.

### `extraction.validation` / `extraction.coercion` (`enforcement`) — free, and clean

| arm | corpus | n | recall | cell acc | typed acc | coercions | validation errors | valid rate | cost/doc |
|---|---|---|---|---|---|---|---|---|---|
| `enforce-off` | bank statements (100-row, noisy-value) | 6 | 1.000 | 1.000 | — | 0 | n/a (off) | n/a | $0.146 |
| `enforce-warn` *(default)* | same | 6 | 1.000 | 1.000 | — | 0 | **0** | **1.000** | $0.145 |
| `enforce-off` | `kv_form` (flat key/value form) | 3 | — | — | 1.000 | 0 | n/a | n/a | $0.030 |
| `enforce-warn` | `kv_form` | 3 | — | — | 1.000 | 0 | 0 | 1.000 | $0.028 |

**Guidance: leave `warn` on.** It costs nothing (within noise on both corpora) and, with
one section per document, it produces **zero** spurious errors — the v0.6.7 edition's
0.54–0.71 simple-mode valid rate was entirely the over-splitting. The `valuenoise_100`
document, whose values carry deliberate OCR noise, drew **0 coercions** at either setting:
Sonnet 4.6 emits typed values that pass the schema without repair. `escalate` (§2b) is
priced only when validation fails, and on a clean corpus it never does.

### `extraction.forced_tool.enabled` (`forcing`) — honoured, neutral, still not a default

⚠️ **Claude Opus 5.5 rejects a forced `toolChoice`, so this axis has no measurement for
it.** Bedrock returns `tool_choice: type "tool" and "any" are not supported for this model.
Use toolChoice auto and steer from the prompt`. Its `force-on` cells still reach
`COMPLETED` — they run with `toolChoice: auto` — so the completion count is not a result on
this axis; the harness records `forced_tool_attempted = 0` and
`forced_tool_honored_rate = None` for them.

Every other arm **honoured forcing at rate 1.00**: `core` at 224/224 attempts across 138
cells, and all eleven sweep arms at 7/7 or 8/8. That is what makes the neutrality finding
below a measurement rather than an absence — without a confirmed honour rate, "no effect"
and "never applied" produce the same numbers.

| arm | corpus | n | honoured | recall | cell acc | typed acc | valid rate | cost/doc |
|---|---|---|---|---|---|---|---|---|
| `force-off` *(default)* | bank statements (100-row ×2) | 6 | — | 1.000 | 1.000 | — | 1.000 | $0.178 |
| `force-on` | same | 6 | **6/6** | 1.000 | 1.000 | — | **1.000** | $0.178 |
| `force-off` | `kv_form` | 3 | — | — | — | 1.000 | 1.000 | $0.030 |
| `force-on` | `kv_form` | 3 | **3/3** | — | — | 1.000 | 1.000 | $0.037 |

**Guidance: unchanged from the v0.6.7 real-corpus measurement — accuracy-neutral, cost-neutral
to slightly more expensive on a tiny form (+23% at n=3, i.e. cents), honoured on every
call, and schema-valid now that #783 is fixed.** Enable it for the structural guarantee (a
malformed-JSON parse failure becomes impossible for declared fields) if your corpus produces
parse failures; do not enable it for accuracy. One caution from §2: in the Sonnet 5 grid the
forced arm was the one that truncated the 800-row document (92 of 800) where the prose arm
returned all 800 — a single draw, but consistent with a tool-call response being a different
length budget than prose. Measure on your own long lists before forcing them.

### `classification.sectionSplitting` and the boundary prompt (`boundaryab`) — #817 confirmed, #750 open

Boundary detection judged on `sections_correct` (1.0/0.0 per run; the mean over 5 repeats
is the pass rate). Classification model Nova 2 Lite (the shipped default).

| cell | one 3-page statement (unpaginated) | same, paginated | reprinted running header (#750) | two statements in one file |
|---|---|---|---|---|
| `split-llm` *(v0.6.8 prompt, default)* | **1.00** (1,1,1,1,1) | 1.00 | **0.00** (3,3,3,3,3) | **1.00** (2,2,2,2,2) |
| `split-llm-v067prompt` *(control: the v0.6.7 prompt)* | **0.20** (2,1,2,2,2) | 1.00 | 0.00 (3,3,3,3,3) | 1.00 |
| `split-disabled` | 1.00 | 1.00 | 1.00 | **0.00** (1,1,1,1,1 — merged) |

Three results, each at 5 of 5:

- **The #817 TABLE CONTINUATION rule works on the shipped classifier.** The unpaginated
  statement goes from 1/5 correct under the v0.6.7 prompt to **5/5**, same stack, same
  model, prompt text the only difference. This is the fix that produced "7 of 7 sections" in
  §2 and "1 section at every size" in §3.
- **The reprinted running header (#750) is still split into three, under both prompts.** A
  document whose every page re-prints the title-and-account block reads as three documents.
  This is the remaining over-split shape, and `sectionSplitting: disabled` is not the
  answer to it (next point).
- **`disabled` is wrong by construction on a packet**: two statements in one file become one
  section, 5 of 5, losing the split silently (recall stays 1.0 because all the rows are
  there). On a single-class corpus of single documents it is harmless and now buys nothing
  (§2b: $0.714 vs $0.717).

**Guidance: keep `llm_determined` and the shipped prompt.** If your corpus has a reprinted
running header on every page, expect 3 sections per document until #750 lands, and prefer
advanced mode (which rejoins the section before validation) over turning splitting off.
The Haiku 4.5 classifier recommendation of the v0.6.7 edition is re-measured in §5.

### `x-aws-idp-multi-instance` and `extraction.multi_instance_detection` — one new failure

Same design as the v0.6.7 study: `twodocs_2x20` is two complete 20-row statements in one
forced section with globally unique `SEQ` tags; `small_narrow` is one 100-row statement.
`repeats: 3`, simple mode, Sonnet 4.6.

| cell | document | runs complete | rows extracted | recall | scalar acc | cost/doc |
|---|---|---|---|---|---|---|
| `mi-silent` (wrapper off, detection off) | two statements | 3/3 | 40, 40, 40 | 1.00 | 1.00 | $0.077 |
| `mi-detected` (detection on) | two statements | 3/3 | 40, 20, 20 | **0.67** | 1.00 | $0.066 |
| `mi-wrapped` (`x-aws-idp-multi-instance: true`) | two statements | 3/3 | 40, 40, 40 | **1.00** | **1.00** | $0.079 |
| `mi-silent` | one 100-row statement | 3/3 | 100 ×3 | 1.00 | 1.00 | $0.142 |
| `mi-detected` | one 100-row statement | 3/3 | 100 ×3 | 1.00 | 1.00 | $0.142 |
| **🚨 `mi-wrapped`** | one 100-row statement | **0/3** — Assessment timed out 3 × 900 s | 100 ×3 (extraction was complete) | — | — | $0.045 + retries |

- **`mi-silent`'s 40 rows are still the trap**, not the control: it merges two accounts'
  transactions into one statement's list, recall 1.00, semantically wrong, no warning. A
  completeness metric prefers the arm that is quietly wrong.
- **The wrapper recovers the records correctly on the packet** (40 rows as two statements,
  3 of 3) — **and fails the document outright on a single long statement**, which is
  [#894](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/894):
  the confidence pass sizes its batches over the *instance* list, so one "row" is a whole
  100-transaction statement, every Nova Lite call truncates, and the recovery ladder runs
  until the Lambda dies, three times. Extraction itself was complete every time.
- **Detection (`extraction.multi_instance_detection`) is cost-free and accuracy-neutral where
  there is nothing to detect**: on the 9-run `midetect` grid (three single-record documents,
  3 repeats) detection on and off are identical to the cent ($0.127) with recall and scalar
  accuracy 1.000 both ways. On the packet it still under-extracts 2 of 3 times when the class
  is not wrapped — detection *warns*, it does not *extract*.

**Guidance:** the v0.6.7 advice stands with one addition. Run detection once as a diagnostic;
where it fires and the records are genuinely absent, set `x-aws-idp-multi-instance: true`
and migrate baselines — **but not on a class that carries a list of more than a few dozen
rows until #894 is fixed**, or the document will fail at assessment. Use
`x-aws-idp-instance-array` when the records are already inside a declared array.

### `extraction.confidence.list_batch_size` (`sizerab`) — the shipped default now equals the tuned one

At v0.6.8-dev2 the shipped sizing paid one truncated Nova Lite call per 100-row section and a
pinned batch of 8 removed it (−29% assessment cost). #861 shipped a ceiling of 12 and a
Nova-only output budget. Re-measured on the same fixture (`small_narrow`, 5 repeats per arm,
Sonnet 4.6 extraction, Nova Lite confidence):

| `list_batch_size` | n | recall | assessment $/doc | total $/doc | wall s/doc | mean conf |
|---|---|---|---|---|---|---|
| shipped default (ceiling 12) | 10 | 1.000 | **$0.0030** | $0.087 | 37 | 0.999 |
| pinned 13 | 10 | 1.000 | $0.0030 | $0.087 | 38 | 0.999 |
| pinned 8 | 10 | 1.000 | $0.0033 | $0.087 | 36 | 0.999 |

**Guidance: leave it alone.** The default is now within $0.0003/doc of the best pinned
value and the truncation penalty is gone; pinning 8 is marginally *more* expensive (more
calls). The dev2 measurement is retained in [releases/v0.6.8.md](releases/v0.6.8.md) as the
before picture.

### `extraction.agentic.restate_schema_in_system_prompt` — free on quality, saves input tokens, costs a few percent in dollars

From the §2 grid (advanced, Sonnet 5, 7 documents): `restate-on` $1.523, `restate-off`
$1.548, recall and cell accuracy 1.000 both.

A dedicated 50-run A/B on Sonnet 4.6 resolves what that $0.025 difference was — it is
real, and it has a mechanism. Details and the full tables are in
[studies/schema-restatement-tokens.md](studies/schema-restatement-tokens.md); the
decision-relevant numbers, measured 2026-09-20 on v0.6.10 with 25 runs per arm across
three synthetic documents:

| | Result | Significance |
|---|---|---|
| Quality | `completeness_recall`, `cell_accuracy` and `scalar_accuracy` **1.000 in all 50 runs**, both arms, zero failures, identical `cells_compared` | both arms at the ceiling; a degradation affecting fewer than ~1 run in 10 is not excluded |
| Input-side tokens | **−5.2%** on `manylists_400` (175,912 → 166,731) and **−6.7%** on `longdesc_100` | *p* = 1.3 × 10⁻⁸ and *p* = 0.008 (distributions completely separated) |
| Input-side tokens, third document | **+9.5%** on `valuenoise_100` — the opposite direction | *p* = 0.22, **not significant**; within-arm spread on that document is 20%, so the arm effect is swamped |
| Extraction **output** tokens | **+17.4%** on `manylists_400` (13,501 → 15,849), **+14.1%** on `valuenoise_100` | *p* = 1.2 × 10⁻⁵ and *p* = 0.016 |
| Extraction cost | **+7.5%** on `manylists_400`; +8.2% and +2.6% on the others | **not significant** on any document (*p* ≥ 0.13) |
| Total cost | **+5.3%**, +6.6%, +1.8% | **not significant** |

**Turn it off for shard headroom, not for dollars.** The input saving is genuine but
cheap: it is almost entirely **cache reads**, so 9,181 fewer input-side tokens is worth
about **$0.007** — while 2,347 more output tokens cost about **$0.039**. Those two
figures reconcile the measured extraction-cost difference (+$0.0319/doc) to the cent. The
release grid in §2b reaches the same conclusion independently, on a different corpus and a
different edition: `restate-off` $1.548 against `restate-on` $1.523, the same direction and a
comparable per-document size. Two grids agreeing on the sign is the reason to trust it, given
neither reaches significance on cost alone.
Since #775 the reclaimed tokens are real shard budget, so headroom is the reason to
use this knob; whether it moves a shard count still depends on the document sitting
near a boundary.

⚠️ **The output-token increase is the part to watch, and it is bimodal rather than
uniform.** On `manylists_400` every `restate-on` run emitted 13,406–13,590 output
tokens, while five of fifteen `restate-off` runs emitted 20,201–20,368 — a mode the
`on` arm never entered (Fisher *p* = 0.042). The remaining ten sat only 100–250 tokens
above the `on` arm. So the risk of turning it off is not a slightly chattier agent
every time; it is a roughly one-in-three chance of a run that emits half again as much.
Measured on one model and one document shape.

### `classification.model` — see §5.3

Re-measured on the full grid with the #817 prompt: Nova 2 Lite, Sonnet 5 and Haiku 4.5 are
compared in §5.3. The v0.6.7 recommendation to switch the classifier to Haiku 4.5 for #726 is
withdrawn there; the running-header shape (#750) is the one case still open, and it is not
model-dependent.

### `extraction.confidence.model` — see §5.4

Nova Lite (default), Nova 2 Lite and Sonnet 5 as the confidence model, on the same grid and
on the one cell with wrong values to be found. Short version: the default is right unless
your review queue depends on the score catching wrong values, in which case only Sonnet 5
lowered its score on them — at 11× the assessment cost.

---

## Appendix A — Data & reproduction

Every number re-measured in this edition is in the working tree under
**`benchmarks/results/v0.6.12/`** — 29 result sets, named `<suite>` or
`<suite>__<axis>-model-<value>`, where the suffix is the `--set` override the suite ran
with. Sections marked *carried over* in the scope table at the top of this page cite an
earlier release's directory instead, which is in git history. Each set holds
`summary.json` (per-run rows plus a `meta` block recording the stack, commit and
`pricing.yaml` hash), `summary.csv` and `cell_stats.csv`.

| Section | Directory (under `benchmarks/results/v0.6.12/`) | Suite / overrides |
|---|---|---|
| §2, §2b | `core/` — synthetic slice | `core` at the control model, 133 synthetic runs with exact ground truth. The v0.7 feature arms of §2b are cells of this same grid (`enforce-*`, `force-*`, `restate-*`, `split-*`) |
| §2c | `core/` — reference slice | the same run's two reference corpora, 760 documents (756 scored). **ONE arm only:** the control model, so §2c is a cross-configuration result on this release and not a model comparison |
| §2.1, §6 | `intconf/`, `advverify__extraction-model-sonnet5/` | 8 runs each; both standing hazards closed, 8/8 |
| §3 | `scaling/` | 14 runs, 25 → 3,200 rows, at the control model |
| §4 | `cost/`, `cost__extraction-model-sonnet5/` | 5 repeats × 5 cells at the control and at the shipped default |
| §5.1 — the seven-arm grid | `coresynth__extraction-model-sonnet5/`, `…-sonnet5_1m/`, `…-sonnet55/`, `…-haiku55/`, `…-opus5/`, `…-opus55/`, `…-astra/` | `coresynth --set extraction_model=<m>`, 133 runs each |
| §5.1 — the two Nova arms | `simplegrid/`, `simplegrid__extraction-model-nova_lite/`, `simplegrid__extraction-model-nova_pro/` | 63 runs each; `simplegrid/` is their Sonnet 4.6 control. Neither Nova model finishes the agentic path (#895), so they ran this smaller matrix and are never averaged into the seven-arm grid |
| §5.2 | `opus55value/`, `astravalue/`, `astracap/` | 40 + 100 + 12 runs |
| §5.3 | `coresynth__classification-model-nova_2_lite/`, `…-haiku55/`, `…-sonnet5/` | `coresynth --set classification_model=<m>`, 133 runs each. The shipped default is an explicit arm this edition, so all three rows have the same shape |
| §5.4 | `coresynth__confidence-model-nova_lite/`, `…-nova_2_lite/`, `…-sonnet5/` | `coresynth --set confidence_model=<m>`, 133 runs each, default included as an explicit arm |
| §7 | `enforcement/`, `enforcecost/`, `forcing/`, and the `restate-on`/`restate-off` cells of `core/` | the four knobs re-measured on v0.6.12. **`splitcost`, `advsplitcost`, `boundaryab`, `multiinstance` and `sizerab` did not run**, so those subsections carry earlier editions' figures, say so in place, and have **no** v0.6.12 directory |
| — | `corefast/` | the 3-document, 19-cell pre-flight of the main grid, run on the preceding commit to validate the stack before the session. **No section cites it**; it is listed so that every one of the 29 directories is accounted for |

- The restatement axis of §7 has a second, dedicated source: it was re-measured on its own
  grid at v0.6.10 (`benchmarks/results/v0.6.10/restate710*/`, Sonnet 4.6, 25 runs per arm)
  and written up in
  [studies/schema-restatement-tokens.md](studies/schema-restatement-tokens.md). That is the
  one set of figures in this edition from a different release, and its rows carry that date.
- Earlier releases' slices are pruned per
  [`RETENTION.md`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/benchmarks/results/RETENTION.md);
  restore the v0.6.8 set the carried-over blocks cite with
  `git checkout <pre-v0.6.9-audit sha> -- benchmarks/results/v0.6.8/`.
- Corpus manifest + generators: `benchmarks/corpus/` (regenerable; PDFs/configs gitignored).
  Matrices + methodology: `benchmarks/matrices/`.
- **Scale of this edition:** **3,248 scored rows from 2,488 runs across 29 result sets**, of
  which `core`'s two reference corpora are 760 rows. Every model on the sweep axes was run;
  the five §7 suites named above were not.
- One stack, one session: `IDPUpg0611to0612` at v0.6.12, 2026-10-17 23:37 → 2026-10-18 06:22
  UTC, running the published release template's build.

```bash
source .venv/bin/activate && export PYTHONPATH=$PWD/lib/idp_common_pkg
python3 benchmarks/harness/gen_corpus.py
M=benchmarks/harness/make_configs.py; R="python3 benchmarks/harness/run_matrix.py --stack <S> --native-upload --max-inflight 4"

# §2 grid, §2b feature arms, §2c real corpora, §7 restatement arms — at the CONTROL model,
# one suite: the three classes are the synthetic corpus and the two reference test sets
for c in bank_statement realkie ocr_bench; do python3 $M --suite core --class $c; done; $R --suite core
# §3 scaling, §4 cost, §2.1 + §6 hazards
for s in scaling cost intconf; do python3 $M --suite $s --class bank_statement; $R --suite $s; done
for s in cost advverify; do python3 $M --suite $s --class bank_statement --set extraction_model=sonnet5; $R --suite $s --set extraction_model=sonnet5; done
# §5.1 extraction sweep — seven arms on coresynth, two Nova arms plus their control on simplegrid
for m in sonnet5 sonnet5_1m sonnet55 haiku55 opus5 opus55 astra; do python3 $M --suite coresynth --class bank_statement --set extraction_model=$m; $R --suite coresynth --set extraction_model=$m; done
python3 $M --suite simplegrid --class bank_statement; $R --suite simplegrid
for m in nova_lite nova_pro; do python3 $M --suite simplegrid --class bank_statement --set extraction_model=$m; $R --suite simplegrid --set extraction_model=$m; done
# §5.3 classification sweep, §5.4 confidence sweep — default is an explicit arm in both
for m in nova_2_lite haiku55 sonnet5; do python3 $M --suite coresynth --class bank_statement --set classification_model=$m; $R --suite coresynth --set classification_model=$m; done
for m in nova_lite nova_2_lite sonnet5; do python3 $M --suite coresynth --class bank_statement --set confidence_model=$m; $R --suite coresynth --set confidence_model=$m; done
# §5.2 premium studies
for s in opus55value astravalue astracap; do python3 $M --suite $s --class bank_statement; $R --suite $s; done
# §7 feature A/Bs
for s in enforcement forcing; do for c in bank_statement kv_form; do python3 $M --suite $s --class $c; $R --suite $s --class $c; done; done
python3 $M --suite enforcecost --class bank_statement; $R --suite enforcecost
# score: python3 benchmarks/harness/aggregate.py --run benchmarks/results/run-<stamp> --out benchmarks/results/v0.6.12/<suite>[__<slug>]
```
