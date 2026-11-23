---
title: "Configuration Guidance"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

> **This is the evergreen "which configuration should I pick?" paper** — a cross-config
> comparison at the current release. For **release-over-release** comparisons (is the
> upgrade safe / cheaper / faster?), see the [Release Benchmark Audit Trail](releases/).

# GenAIIDP Configuration Guidance — Empirical Guidance for Document Extraction at Scale

**Release:** v0.6.14 · **Region:** us-west-2 · **Stack:** `IDPUpg0613to0614` (created from the
published `idp-main_0.6.13.yaml` and upgraded in place to the published `idp-main_0.6.14.yaml`;
the v0.6.14 record in [release-validation](../release-validation/) carries the stack's
provenance)
**Models:** extraction Claude Sonnet 4.6 — **the cross-version control, not the shipped
default** — in §2–§4 and §7; the extraction, classification and confidence models are
*varied* in §5 · classification Nova 2 Lite (the shipped default) · confidence Nova Lite
(the shipped default) · summarization disabled (unscored)
**Pricing:** `config_library/pricing.yaml` (sha256 `44a79aa06c15…`, unchanged since the
v0.6.12 edition; rates as of 2026-10; intro pricing may apply), except that **every Claude
Sonnet 5 dollar figure measured on v0.6.14 is re-priced at Sonnet 5's published rates** — see
the header note below
**Measured:** 2026-11-07 and 2026-11-09/10 (run launch times), on one stack at one build —
**3,338 scored rows across 31 result sets** (2,578 synthetic runs and 760 reference-corpus
documents, $1,970.57 by the rows' own cost as recorded, $1,854.45 with the Sonnet 5 share
re-priced) plus a classification-variant benchmark on a
20-packet labelled corpus. A host restart on 2026-11-07 killed six suites mid-run; their
partial runs were discarded and the suites re-run in full (Appendix A).

⚠️ **Two models' cost figures are lower bounds, and every Sonnet 5 figure is re-priced.**
`pricing.yaml` holds one flat rate per model, and that is wrong for two of the swept models:

- **Claude Haiku 5.5** is billed 5× on every token type above 100,000 input tokens, and
  that threshold is crossed in practice here. Its figures are **lower bounds**.
- **GPT-6 Astra** moves to a long-context band above 272,000 input tokens. Its figures are
  **lower bounds**.

**Claude Sonnet 5 and Sonnet 5 `:1m`**, the shipped default and its 1M twin, ran with
`pricing.yaml` rows that carried Sonnet 4.6's rates, exactly **1.5× the published Sonnet 5
price on every token type** ($3.30/$16.50 per MTok against $2.20/$11.00, `us.`/`eu.`). Those
rows now carry the published rates
([#1830](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1830)),
and every Sonnet 5 dollar figure measured on v0.6.14 is re-priced to match: each run's Sonnet 5
spend (its `cost_by_key` entry for `bedrock/us.anthropic.claude-sonnet-5`) is divided by 1.5
and the rest of its cost is unchanged. Because the old rows were 1.5× high on all four token
types, that division is exact for any token mix. The `summary.json` files under
`benchmarks/results/v0.6.14/` still record the cost as it was priced at run time. Sonnet 5
and **Claude Sonnet 5.5** are priced identically on input and output ($2.20/$11.00) and
Sonnet 5.5 is half the price on cache reads, so the two now compare directly. Figures this
page carries over from earlier editions (the v0.6.8 blocks, and v0.6.13 figures quoted for
comparison) are as priced when they were measured and are not re-priced here.

Where this page compares Haiku 5.5 or Astra on cost it states two dollar figures from
the same grid and a **one-sided bound** over them ("at least X% less") in the direction the
bias allows, which is what a bound supports and a ratio is not. Every other arm has a single
flat rate and its cost figure is a true measurement. See each model's row in
[Benchmarking](index.md#which-models-are-actually-measured).

### Scope of this edition

Every number below is either re-measured on v0.6.14 or explicitly marked as carried over. Read
this table before citing a figure.

| Section | On v0.6.14? | Data |
|---|---|---|
| §2 configuration matrix (19 cells × 7 synthetic docs) | ✅ re-measured | `core` synthetic slice (133 runs, 0 failures, exact ground truth), at the **control** model — that suite carries no model override, so §2 is a cross-*configuration* result and the shipped default's own grid is §5.1's |
| §2c real-corpus accuracy | ✅ **re-measured, ONE arm** | `core` reference slice — **760 scored rows over 760 documents** across both corpora, at the **control** model. ⚠️ The *two-model* comparison the v0.6.8 edition published is **not** refreshed: only the control arm ran, so a Sonnet-5 side would be a cross-release figure. Treat §2c as a cross-configuration result on this release, not a model comparison |
| §2.1 integrated + simple hazard | ✅ **re-measured, one model per mode** | `intconf` (8 runs, **control** model) — **closed**, 8/8 complete — and `advverify__extraction-model-sonnet5` (8 runs, shipped default) for the advanced-mode counterpart. ⚠️ No Sonnet 5 `intconf` arm ran, so the simple-mode half is a **one-model** result |
| §3 scaling | ✅ **re-measured, 1 repeat, control only** | `scaling` (14 runs, 25 → 3,200 rows, **one draw** per mode per size — the suite default, where the v0.6.13 edition ran three), at the **control** model. Sonnet 5 at the 3,200-row point is §5.2's `astracap` |
| §4 cost level and variance | ✅ re-measured | `cost` (control) and `cost__extraction-model-sonnet5`, n=5 each, 25 runs each, all 50 complete at recall 1.000 |
| §5.1 extraction-model sweep | ✅ **all 9 models** | `coresynth__extraction-model-*` for `sonnet5`, `sonnet5-1m`, `opus5`, `opus55`, `astra`, `haiku55`, `sonnet55` (133 runs each); `simplegrid__extraction-model-*` for `nova-lite` and `nova-pro` (63 each — neither finishes the agentic path, #895), with `simplegrid` as their comparable control |
| §5.2 premium head-to-head | ✅ **all three pairs** | `opus55value` (40 runs), `astravalue` (100), `astracap` (12 — **2 repeats** per cell, the suite default, against 3 in the v0.6.13 edition) |
| §5.3 classification model | ✅ **all 3** | `nova_2_lite` (default), `sonnet5`, `haiku55`, 133 runs each |
| §5.4 confidence model | ✅ **all 3** | `nova_lite` (default), `nova_2_lite`, `sonnet5`, 133 runs each |
| §5.6 classification granularity, modality and strategy | ✅ re-measured — second measurement | `clsvariants`: 7 variants × 20 DocSplit packets, Nova 2 Lite, `topk` class confidence. **n = 20 documents**; 19 scored by every variant |
| §6 standing hazards | ✅ re-measured | `intconf`, `advverify__extraction-model-sonnet5` — **both closed**, 8/8 each |
| §7 knob A/Bs | ⚠️ **5 of the set** | `enforcement`, `enforcecost`, `forcing`, `boundaryab` and `boundary`, plus the restatement arms inside `core`, were re-measured — on bank statements only, so the `kv_form` rows are **not** refreshed. **`splitcost`, `advsplitcost`, `multiinstance` and `sizerab` did not run**, so those subsections carry figures from earlier editions and say so in place |

⚠️ **`typed_accuracy` is absent from every table in this edition, and that is a gap rather
than a choice.** It is defined only for a document whose truth declares typed scalar
fields, which is the `kv_form` generator alone, and no `kv_form` document ran in this
edition ([#1490](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1490)).
It is the one metric that would separate a value extracted with the right *type* from one
that merely matches as a string.

Appendix A lists the exact directory behind every section.

> Reproducible via the `benchmarks/` harness (run the `run-benchmarks` skill). Every number
> here is produced from live runs by `benchmarks/harness/aggregate.py` (and, for §5.6,
> `benchmarks/harness/run_classification_bench.py`); none are recalled from memory. The data
> for every section is in the working tree under `benchmarks/results/v0.6.14/` — see
> Appendix A for the exact directory per section. The one exception is the
> `restate_schema_in_system_prompt` axis in §7, which was also re-measured on its own grid
> at v0.6.10 (`benchmarks/results/v0.6.10/restate710*/`, Sonnet 4.6, 25 runs per arm) and is
> written up in [studies/schema-restatement-tokens.md](studies/schema-restatement-tokens.md);
> its rows carry that date. Per
> [`benchmarks/results/RETENTION.md`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/benchmarks/results/RETENTION.md)
> only one complete set is retained per release, so the v0.6.8 slices the carried-over blocks
> cite are in git history (`git checkout <sha> -- benchmarks/results/v0.6.8/`).
>
> **What changed in the measurement itself since the v0.6.13 edition.** The scorer did not
> change. `run_matrix.py --native-upload` now uploads a generated configuration unchanged when
> the release it validates against reads the nested classification blocks
> ([#1674](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1674)),
> so every suite here was validated against v0.6.13 itself rather than an older release. And
> four suites ran at their committed default repeats rather than the `--repeats 3` override the
> v0.6.13 edition used — `scaling` 1, `astracap` 2, `intconf` and `advverify` 4 — which the
> sections that use them say in place.

## Abstract

We benchmark the GenAI IDP accelerator across a controlled matrix of **configuration
options** (OCR backend, extraction mode, assessment mode, the feature-option arms, the
extraction, classification and confidence **models**, and the
classification **granularity, modality and strategy**) and **document types and sizes**
(synthetic documents with exact ground truth, plus three real labelled corpora). We quantify
seven dimensions per configuration: success/failure, list completeness, per-row field
accuracy, confidence calibration, latency, token use, and cost.

Headline results at v0.6.14:

1. **Both Opus models are at ceiling on the whole extraction grid for the second edition
   running, and Opus 5.5 is still the cheaper of the two.** On the 19-cell × 7-document grid
   Claude Opus 5.5 and Claude Opus 5 each completed **133 of 133 runs at recall 1.0000 and cell
   accuracy 1.0000 over 83,790 `cells_compared`**, the grid's full denominator — for **$105.95
   against $127.08, 17% less** for the same result, a ratio that can be quoted because both are
   flat-rate. GPT-6 Astra, the most expensive arm, is at recall 0.9924 and the lowest cell
   accuracy of the grid, 0.9810 over 82,184 cells, for $191.85, which is a lower bound — so Opus
   5.5 is **at least 45% less** than Astra and more accurate (§5.1, §5.5).
2. **The shipped default still truncates the 17-page document in simple mode, and still reports
   success.** Sonnet 5's grid recall is **0.9463 over 72,364 cells**, and all of the shortfall is
   one document: on the 800-row `large_narrow` in simple mode it returned all 800 rows on **5 of
   13 runs** and 43 to 190 rows on the other 8, each `COMPLETED` and each flagged
   `extraction_rows_below_ocr_estimate`. In advanced mode the same model returned 800 of 800 on 6
   of 6. The rule from the last three editions holds: **use advanced mode above ~400 rows** (§5.5,
   §3).
3. **The confidence pass now covers most of a large simple-mode list.** In the scaling study the
   1,200- and 1,600-row simple-mode documents complete with every row at cell accuracy 1.000 and
   **95% and 80% of rows confidence-scored**, against 33–38% and 19–23% in the v0.6.13 edition —
   one draw per size here, so read it as a direction. The same movement shows at the 3,200-row
   ceiling, where Sonnet 5's wide-shard confidence coverage went from 22–78% to 97–99% (§5.2),
   and **every one of the 931 runs of the seven-arm extraction grid completed**, against two
   `AssessmentStep` memory kills there at v0.6.13
   ([#1678](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1678)).
   Advanced mode holds recall 1.000 **and** full confidence coverage at every size from 25 to
   3,200 rows (§3).
4. **Both classification-variant failure modes are closed, and packet granularity's boundary
   advantage is smaller than first measured.** Every image-bearing packet variant now classifies
   the 18–20-page DocSplit packets it failed at v0.6.13
   ([#1676](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1676)),
   and text-only page granularity no longer fails a page with no text
   ([#1677](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1677)).
   Over the 19 packets every variant scored, the packet-level variants place section boundaries
   at **0.600–0.710** against the page-level default's **0.509**, at 29–59% of its
   classification cost per page; the v0.6.13 edition measured 0.711–0.821 against 0.446. Twenty
   documents, one run each, so read this as direction rather than ranking (§5.6).
5. **On a real labelled corpus simple mode is still at least as accurate as advanced at half the
   cost, by a margin now inside the noise.** On RealKIE-FCC-Verified (20 documents, all 19 cells,
   scored by the product's own evaluation) the matched pair scores **0.798 simple against 0.774
   advanced** weighted accuracy, at **$0.1428 against $0.2825** per document — but four cells
   running the identical simple configuration span **0.771–0.799**, so the accuracy gap is one
   draw's noise and the cost gap is the finding. `sectionSplitting: disabled` is the best cell
   for the third edition running, at 0.836 (§2c).
6. **🚨 Claude Sonnet 5.5 still loses most of a 400-row list**
   ([#1494](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1494)).
   15 of its 133 grid runs return between 2 and 20 rows of 400, every one in simple mode on a
   400-row document. Per-document recall is **0.7413 on `med_narrow`, 0.8974 on `wide_400` and
   0.5947 on `manylists_400`**, against 1.0000 on every other document. 13 of the 15 now carry a
   completeness issue; the two that do not are 10-row runs on the 8-column `wide_400`, where there
   is no exact-width OCR table to check a row count against (§5.1).
7. **Neither premium knob on the other two model axes earns its price.** Upgrading the
   **classification** model buys nothing measurable: every arm produces downstream recall 1.0000
   over the same 83,790 cells, and each arm gets 132–133 of 133 boundaries right; the grid bill
   rises from **$89.93 to $106.11** under Sonnet 5. Sonnet 5 as the **confidence** model is **a
   far more conservative scorer, not a more accurate one**: about **638 of 119,960** confidence
   leaves below 0.9 against the default's **0 of 119,937**, for **$160.70 against $90.33**, with
   identical cell accuracy (0.9997 over 83,790 cells in all three arms) (§5.3, §5.4).
8. **Both standing list-loss hazards stay closed, and every §2 run produced one section.**
   `integrated` confidence with simple extraction is recall 1.000 and cell accuracy 1.000 on **4
   of 4 repeats**, at the same cost to the cent as `separate`, and the advanced-mode tool-decline
   list loss is **0 of 8** at the shipped default (§2.1). **All 133 grid runs produced exactly one
   section** (§2).
9. **Bedrock-LLM OCR is still the one backend that gets values wrong.** It returns every row —
   recall 1.000 over 4,410 cells — at **cell accuracy 0.990**, the only cell of the nineteen below
   1.000, with **10 of 14 scalar fields right** where every other cell gets 12 to 14, and the
   grid's only validation error. It is also the cheapest cell, at $0.3821 per document. Do not
   use it where identifiers or amounts matter (§2, finding 4).
10. **The cheap end completes everything and returns half the rows.** Neither Nova model can
    finish the agentic path
    ([#895](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/895)),
    so both ran the smaller `simplegrid` matrix, where they completed **63 of 63 runs** at mean
    recall **0.5035** (Nova Pro) and **0.4980** (Nova Lite). Nova Pro's 0.9225 cell accuracy is
    over **7,858 cells** against the same matrix's Sonnet 4.6 control at **39,690** (§5.1).

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
  release** (`core` suite), at the control model only.
- **Classification corpus (real, labelled per page):** the first 20 packets of
  `DocSplit-Poly-Seq` (RVL-CDIP-derived, 13 classes, 298 pages), run through
  `benchmarks/harness/run_classification_bench.py` and scored per page and per section
  against its labels (§5.6).
- **Config matrix:** 19 curated *core* cells — the OCR × mode × assessment decision space
  plus the feature-option arms (enforcement, forcing, schema restatement, section splitting) —
  a two-cell scaling series, a repeated-measures cost suite, the two hazard re-verification
  suites (`intconf`, `advverify`), the feature A/B suites for the knobs named in the scope
  table, and the model sweeps. Control arms live in a separate `control_cells:` block and
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
| **Extraction model** (§5) | Nova Lite · Nova Pro · **Sonnet 5** (default) · Sonnet 5 `:1m` · Sonnet 5.5 · Opus 5 · Opus 5.5 · Haiku 5.5 · GPT-6 Astra (`us.` and `global.`) · Sonnet 4.6 (control) — all nine swept models measured on this release |
| **Classification model** (§5) | **Nova 2 Lite** (default) · Sonnet 5 · Haiku 5.5 |
| **Confidence model** (§5) | **Nova Lite** (default) · Nova 2 Lite · Sonnet 5 |
| **Classification variant** (§5.6) | `page` or `packet` granularity × text and/or images × `combined` or `split_then_classify` — seven named combinations, Nova 2 Lite |
| Confidence batch size | shipped (ceiling 12 since #861) · pinned 8 · pinned 13 — *not re-run, §7* |
| Geometry | ocr_only (all cells) |
| Reasoning effort | low (all cells) |

**Axes not varied.** OCR DPI is held at 300 (the shipped default since #740); geometry at
`ocr_only`; multi-instance settings are measured separately in §7. Classification
granularity, modality and strategy are held at the shipped default — `page` granularity,
text and images, `combined` — in every pipeline cell of §2–§5.5; §5.6 is the only place
they vary, on a different corpus and through a different driver.

---

## 2. Configuration matrix (19 configurations, 7 synthetic documents with exact ground truth)

Mean over 7 bank-statement (transaction-list) documents spanning 5 → 800 rows and varying
row width, list count and description length (`tiny_form`, `small_narrow`, `med_narrow`,
`large_narrow`, `wide_400`, `manylists_400`, `longdesc_100`). `recall` = distinct
ground-truth rows recovered ÷ total (exact, via SEQ tags); `cell acc` = per-row typed value
match, keyed by SEQ tag, reported beside the `cells_compared` it was computed over.
Extraction model **Sonnet 4.6, the cross-version control** — the `core` suite carries no
model override, so §2 is a cross-*configuration* result at one model, and the shipped
default's own grid is §5.1's. **133 runs, 0 failures, and all 133 produced exactly one
section** — the two-section split of `wide_400` that two v0.6.13 cells drew did not recur.

**All nineteen cells are at recall 1.000 over 4,410 `cells_compared` each, on all 7
documents — including the 800-row, 17-page one — and eighteen are also at cell accuracy
1.000.** Every figure below sits inside that result, so the grid separates the
configurations on **cost, wall-clock and value fidelity** rather than on completeness.

### 2a. OCR backend, extraction mode and confidence mode

`leaves < 0.9` is a **count** of confidence leaves below the 0.9 alert threshold, not a
percentage, over the per-cell leaf total in the next column — on this grid the fractions are
small enough that a percentage reads as zero almost everywhere.

| OCR / mode / assessment | recall | cell acc | `cells_compared` | cost/doc | mean conf | leaves < 0.9 | conf leaves | valid rate | wall_s | fails |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Textract TABLES / simple / separate | 1.000 | 1.000 | 4,410 | $0.5238 | 0.9927 | 0 | 6,658 | 1.000 | 257 | 0 |
| Textract TABLES / simple / off | 1.000 | 1.000 | 4,410 | **$0.4006** | n/a | n/a | 0 | 1.000 | **80** | 0 |
| Textract TABLES / simple / integrated | 1.000 | 1.000 | 4,410 | $0.5554 | 0.9934 | 0 | 6,664 | 1.000 | 204 | 0 |
| Textract TABLES / advanced / separate | 1.000 | 1.000 | 4,410 | $0.6103 | 0.9892 | 0 | 6,664 | 1.000 | **92** | 0 |
| ⚠️ Textract TABLES / advanced / integrated | 1.000 | 1.000 | 4,410 | **$2.9081** | 0.9874 | 64 | 6,664 | 1.000 | **451** | 0 |
| Textract LAYOUT / simple / separate | 1.000 | 1.000 | 4,410 | $0.4341 | 0.9931 | 0 | 6,655 | 1.000 | 217 | 0 |
| Textract LAYOUT / advanced / separate | 1.000 | 1.000 | 4,410 | $0.9750 | 0.9877 | 0 | 6,664 | 1.000 | 168 | 0 |
| BDA / simple / separate | 1.000 | 1.000 | 4,410 | $0.5556 | 0.9936 | 0 | 6,664 | 1.000 | 196 | 0 |
| BDA / advanced / separate | 1.000 | 1.000 | 4,410 | $0.6136 | 0.9883 | 0 | 6,664 | 1.000 | **94** | 0 |
| **⚠️ Bedrock-LLM / simple / separate** | 1.000 | **0.990** | 4,410 | **$0.3821** | 0.9983 | 0 | 6,664 | **0.857** | 204 | 0 |

`cells_compared` is the **total** over the 7 documents (2 scored values per row, so
2 × 2,205 truth rows), while the cost, confidence and wall columns are means. Cost CV within
a cell is large because the 7 documents differ 160× in row count; that is *between-document*
spread and tells you nothing about reproducibility. §4 measures cost variance properly, with
repeats on one document.

⚠️ **Mean confidence on the simple-mode cells is about 0.006 lower than in the v0.6.13
edition** (for example 0.9990 → 0.9927 on `core-tt-simple-sep`, lower on every one of its
7 documents), with the same confidence model, the same documents and cell accuracy unchanged
at 1.000. It is measured and not attributed: the run does not record why the confidence model
scored the same correct values lower. It moves no cell across the 0.9 alert threshold.

### 2b. Feature-option arms: enforcement, forcing, schema restatement, section splitting

All are Textract TABLES + `separate`; `restate-*` are advanced, the rest simple. `sections`
is the total across the 7 documents, where the ground truth is **7** (one per document).
Every arm is at recall 1.000 and cell accuracy 1.000 over **4,410 `cells_compared`**, so the
columns that discriminate are cost and the validation pair.

| cell | recall | cell acc | `cells_compared` | cost/doc | sections (truth 7) | valid rate | val errors |
|---|---:|---:|---:|---:|---:|---:|---:|
| `enforce-off` | 1.000 | 1.000 | 4,410 | $0.5600 | 7 | not measured (validation off) | — |
| `enforce-warn` *(shipped default)* | 1.000 | 1.000 | 4,410 | $0.5577 | 7 | 1.000 | 0 |
| `enforce-escalate` | 1.000 | 1.000 | 4,410 | $0.5550 | 7 | 1.000 | 0 |
| `force-off` *(shipped default)* | 1.000 | 1.000 | 4,410 | $0.5220 | 7 | 1.000 | 0 |
| `force-on` | 1.000 | 1.000 | 4,410 | $0.5137 | 7 | 1.000 | 0 |
| `restate-on` *(shipped default)* | 1.000 | 1.000 | 4,410 | $0.6136 | 7 | 1.000 | 0 |
| `restate-off` | 1.000 | 1.000 | 4,410 | $0.6343 | 7 | 1.000 | 0 |
| `split-llm` *(shipped default)* | 1.000 | 1.000 | 4,410 | $0.5562 | 7 | 1.000 | 0 |
| `split-disabled` | 1.000 | 1.000 | 4,410 | $0.5505 | 7 | 1.000 | 0 |

⚠️ **Four rows of this table run the identical configuration, so read the spread between them
as the noise floor.** `enforce-warn`, `force-off` and `split-llm` each resolve to exactly the
same settings as §2a's `core-tt-simple-sep` — every one is the shipped default on the axis it
names. Those four cost **$0.5220–0.5577 per document** on one draw each. A difference smaller
than that band between two cells of this table is not a measured effect.

**Findings**

1. **Completeness is solved across the whole decision space, up to and including the 800-row
   / 17-page document.** All ten decision cells — every OCR backend, both modes, all three
   assessment settings — are at recall 1.000 over 4,410 `cells_compared` each, on all 7
   documents. Mode is therefore a **cost and latency** decision at these sizes rather than a
   completeness one. On matched OCR backends with `separate` assessment, simple → advanced is
   **$0.5238 → $0.6103 for TABLES (1.17×)**, **$0.4341 → $0.9750 for LAYOUT (2.25×)** and
   **$0.5556 → $0.6136 for BDA (1.10×)**. Advanced mode is also the **faster** of the two on
   all three matched pairs — 92 s against 257 s on TABLES, 168 s against 217 s on LAYOUT,
   94 s against 196 s on BDA — because simple mode's single long response, and its
   confidence pass over the whole list, dominate the wall-clock on the larger documents.
2. **There is no completeness cliff in this grid at the control model.** All nineteen cells
   returned **800 of 800 rows on `large_narrow`**, one draw each. What this does *not*
   establish is that 800 rows is comfortable in simple mode: §3's size series and §5.1's
   per-model grid both show the same request landing on either side of a single-response
   limit, and §5.1's Sonnet 5 grid still truncates that document in simple mode. Read §2 as
   "the control model clears this corpus" and §3 and §5.1 as where the limit actually is.
   Three simple-mode cells (`core-tt-simple-sep`, `force-off`, `force-on`) report
   `assessment_incomplete` on `large_narrow` with **798 of 800** rows scored, and the LAYOUT
   simple cell 797 of 800: the time-bounded confidence pass (#1488) stopping a few rows short,
   not a row loss.
3. **`integrated` + simple costs the same as `separate`, because it *is* `separate`.**
   Recall 1.000 and cell accuracy 1.000 over 4,410 cells on all 7 documents, **0 of 6,664
   confidence leaves below 0.9**, at $0.5554 against `separate`'s $0.5238 — inside the
   identical-default band above — because a simple-mode section whose class declares a list
   is routed to the separate confidence pass automatically (#795). §2.1 has the
   repeated-measures confirmation. Choosing `integrated` in simple mode costs you nothing and
   changes nothing for list-bearing classes.

   ⚠️ **On the *agentic* path `integrated` is the most expensive cell in the grid by a wide
   margin: $2.9081 against `advanced`/`separate`'s $0.6103, 4.8×, at 451 s against 92 s.**
   Completeness is unaffected, so this is a cost and latency finding, and it is concentrated
   on the four documents of 400 rows or more, which cost **$2.98–8.08** each against
   $0.11–0.43 for the three smaller ones. Every one of those four runs carries
   `extraction_output_truncated`, with extraction output of **147,492–413,307 tokens**: the
   agent is emitting inline assessment, overrunning its output budget and retrying. It is
   also the only cell with confidence leaves below 0.9, all 64 of them on `longdesc_100`. §4
   reproduces the cost shape with five repeats on one document. **Use `separate` on the
   agentic path.**
4. **⚠️ Bedrock-LLM OCR is the only backend that gets *values* wrong.** It returns every
   row — recall 1.000 over 4,410 cells — at **cell accuracy 0.990**, the only one of the
   nineteen cells below 1.000: 0.955 on `manylists_400`, 0.986–0.995 on the other three
   documents of 400 rows or more and 1.000 on the three smaller ones. Its scalar fields,
   which compare the document's `Account Number` and `Statement Period` by label, are right
   **10 times in 14**, where every other cell gets 12 to 14 of 14; `Account Number` is
   exactly the fixed-width identifier this backend perturbs — the digit insertion root-caused
   in the [v0.6.6 audit](releases/v0.6.6.md). It is also the only cell with a validation
   error (`manylists_400`, `extraction_validation_failed`, valid rate 0.857). The scorer
   repaired **42 perturbed row identifiers** on each of `med_narrow`, `large_narrow` and
   `wide_400` before joining (`rows_id_repaired`, #1500), which is what makes those accuracy
   figures a comparison of values rather than an artefact of the join. It is the cheapest
   cell in the grid ($0.3821). **Do not use it where identifiers or amounts matter.**
5. **LAYOUT-only is the cheapest complete option in simple mode and the dearest in advanced**
   ($0.4341 simple, $0.9750 advanced) with confidence indistinguishable from TABLES (0.9931 /
   0.9877). In simple mode TABLES costs 21% more and bought no completeness here. In advanced
   mode the ordering inverts: LAYOUT is **60% more than TABLES** ($0.9750 against $0.6103)
   and 59% more than BDA, which §4 reproduces with repeats on one document. The LAYOUT text
   is cheaper to produce and more expensive for the agent to extract from. Every LAYOUT run
   of 100 rows or more also carries `extraction_row_count_unverified` (#1489), and correctly:
   LAYOUT emits no Markdown tables, so there is nothing to check a row count against. That
   issue is `info` and reports the absence of evidence, not a shortfall.
6. **Validation is clean everywhere except the Bedrock-LLM cell.** Every other cell with
   validation on reports `valid rate` 1.000 with 0 errors. In the v0.6.13 edition
   `enforce-warn` and `force-off` each split `wide_400` into two sections (288 + 112 rows)
   and raised one validation error each; on this release neither split, and no cell of the
   133 runs did.
7. **`force-on` (forced tool use) is schema-valid, honoured, and complete.** Valid rate
   1.000, **attempted and honoured on 7 of 7 documents with no skips** (76 of 76 attempts
   across `core`, reference corpora included), recall 1.000 and cell accuracy 1.000 over
   4,410 cells, at $0.5137 — below the identical-default band ($0.5220–0.5577), so not
   measurably more expensive. §7 has the dedicated A/B.
8. **`enforce-escalate` costs nothing on clean documents** ($0.5550, inside the
   identical-default band) because there is nothing to escalate: 0 validation errors in that
   cell means 0 re-extractions. Its price is paid only when validation actually fails.
9. **Schema restatement off is neutral** ($0.6343 against $0.6136, n=7, well inside the
   agentic spread §4 measures).
10. **`split-disabled` is a no-op on this corpus** (7 sections either way, $0.5505 against
    $0.5562), because every document is a single statement. It is wrong by construction on
    packets, where it also **loses rows** (§7).

### 2c. Real-corpus accuracy (RealKIE-FCC-Verified, OmniAI-OCR-Benchmark)

Both reference corpora run as Test Studio test sets — 20 documents each, every one of the 19
cells — and are scored by the product's own evaluation against their labels
(`weighted_accuracy` for extracted fields, `class_accuracy` for classification; the
harness's exact-key metrics do not apply to a document whose truth lives on the stack
rather than in `corpus/docs/`, so `cells_compared`, `cell_accuracy`, `scalar_accuracy` and
`completeness_recall` are absent on these rows — absent, not zero. Each row says which
scorer produced it in `scored_against`).

v0.6.14 ran both corpora at the **control only** — **760 scored rows over 760 (cell,
corpus, document) triples**, 0 parse failures and 0 scoring refusals — and reproduces the
control picture: RealKIE **0.761–0.836** weighted accuracy across the 19 cells, OCR-Benchmark
**0.975–0.998**, class accuracy **1.000 in every cell of both corpora**. Render the
per-cell tables from the committed artifact with

```bash
python3 benchmarks/harness/aggregate.py \
    --reference-tables benchmarks/results/v0.6.14/core/summary.json
```

**The four tables further down are the v0.6.8 pair** — one grid at the shipped default and
one at the control, on one stack, which is what makes the model comparison in their
finding 2 a comparison of models rather than of releases. **1,520 documents, 1,520
completed, 0 parse failures.** They are not re-measured: no Sonnet 5 arm ran on these
corpora in this edition.

#### ⚠️ On real documents simple mode is at least as accurate as advanced mode, at about half the cost — but the gap is now inside the noise floor

This is the comparison the synthetic grid **cannot** make, and the reason running these
corpora matters more than the row count suggests. §2's 19 cells score 1.000 on recall
everywhere on exact-ground-truth synthetic documents, so the grid ranks nothing. RealKIE
ranks them:

| RealKIE cell | mode | weighted accuracy | cost/doc | wall |
|---|---|---|---|---|
| `split-disabled` | simple | **0.836** | $0.1256 | 50 s |
| `core-tt-simple-off` | simple | 0.800 | $0.1388 | 26 s |
| `core-tt-simple-sep` | simple | 0.798 | $0.1428 | 41 s |
| `force-off` *(same configuration as the row above)* | simple | 0.771 | $0.1424 | 44 s |
| `core-tl-adv-sep` | advanced | 0.792 | $0.2191 | 48 s |
| `restate-off` | advanced | 0.794 | $0.3023 | 62 s |
| `core-tt-adv-sep` | advanced | **0.774** | $0.2825 | 57 s |
| `core-bda-adv-sep` | advanced | 0.764 | $0.2927 | 57 s |
| `core-tt-adv-int` | advanced | 0.761 | $0.4179 | 52 s |
| `restate-on` | advanced | 0.761 | $0.3196 | 60 s |

**The matched pair still favours simple mode**: `core-tt-simple-sep` at 0.798 for $0.1428
against `core-tt-adv-sep` at 0.774 for $0.2825 — 2.0× the cost for 2.4 points less. The
thirteen simple-mode cells span **0.771–0.836** and the six advanced cells **0.761–0.794**,
so the best advanced cell sits inside the simple band rather than below all of it, which is
weaker than the v0.6.13 edition's result (five of six advanced cells below every simple
cell). ⚠️ **Four simple cells here run the identical configuration** — `core-tt-simple-sep`,
`enforce-warn`, `force-off` and `split-llm` — and they score **0.771–0.799**, so a 2.8-point
spread on this corpus is one draw's noise. The matched-pair gap is smaller than that. What
three consecutive editions agree on is the **cost**: advanced mode is 2.0–2.3× the price on
these documents and has not once been measurably more accurate. The two readings this does
*not* support: it says nothing about completeness at scale, which is §3's territory and
where advanced mode is unambiguously better; and RealKIE's documents are not long lists, so
this is a statement about labelled real-world forms rather than about tabular extraction
generally.

**`split-disabled` is the best RealKIE cell again**, at 0.836 — 3.7 points above the next
cell, outside the identical-configuration spread — and the cheapest simple cell. It was the
best cell at v0.6.13 (0.811) and in the v0.6.8 control grid (0.842), so three editions agree
on it: on a single-class corpus of single-section documents the classifier's boundary pass
can only introduce error. It remains wrong for packets (§7).

OCR-Benchmark does not discriminate the same way — every cell lands between 0.995 and
0.998, with `core-llm-simple-sep` the only one below 0.99 (0.975). These reference-corpus
figures are scored by the product's own evaluation against labels rather than by a row
identifier, so they were never affected by the identifier defect described in §2 finding 4.

⚠️ **This is one arm, so it is not a model comparison.** Only the control ran, so the
v0.6.8 two-model pair below is still the only like-for-like comparison of the shipped
default against the control on these corpora. Comparing a v0.6.14 control figure with a
v0.6.8 default figure would be a cross-release difference wearing a model label.

**All 760 triples produced a score.** The v0.6.13 edition lost four OCR-Benchmark rows to
`SCORE_ERROR`, each a grader leaf that answered on a 0–100 scale where the scale is 0.0–1.0
and that the harness refuses rather than averages
([#1395](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1395)).
None occurred in this run. Which documents trip that refusal has varied from run to run, so
its absence here is one draw rather than a fix.

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

**Findings from the v0.6.8 pair** (the four tables above; re-measured figures for the
control arm are in the paragraphs before them)

1. **The OCR benchmark is at ceiling for every configuration but one.** 0.996–0.998 weighted
   accuracy in 18 of 19 cells at both models (v0.6.0: ≈0.87); the one exception is
   Bedrock-LLM OCR at **0.973**, the same backend that gets values wrong on the synthetic
   grid. Mode, assessment, forcing, restatement and splitting make no measurable difference
   on these documents, and the whole corpus costs $0.02–0.05 per document in simple mode.
2. **RealKIE is a harder corpus, and the control model is slightly better on it than the
   default.** Sonnet 4.6 scores 0.77–0.84 across the cells; Sonnet 5 0.73–0.80 — a
   consistent 2–5 points lower on 18 of 19 cells, at 30–40% higher cost. This is the one
   place in the study where the shipped default is measurably worse than the cheaper model,
   and the reason the v0.6.8 edition's model-selection table recommended Sonnet 4.6 for
   single-class real forms. No Sonnet 5 arm ran on these corpora since, so that comparison is
   not re-measured.
3. **Advanced mode buys nothing on real forms.** RealKIE 0.77–0.80 advanced against
   0.76–0.81 simple at 2.1–3.0× the cost; the OCR benchmark is identical. The agentic path
   earns its premium on long lists (§3), not on forms.
4. **`split-disabled` is the best RealKIE cell at the control model (0.842 vs 0.805) and
   the cheapest** — on a single-class corpus of single-section documents, the classifier's
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
that needs the cause spelled out, because a confidence is a probability and the v0.6.8
grid's means for these cells are above 1.0 (1.29–1.63 in `benchmarks/results/v0.6.8/core/`).

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

## 2.1 Integrated confidence with simple mode: list truncation fixed in v0.6.8

**What v0.6.14 measures.** On the 7-document grid, `simple/integrated` recall is **1.000 on
every document** at cell accuracy 1.000 over 4,410 `cells_compared`, including
`manylists_400` (0.552 at v0.6.7) and the 800-row `large_narrow`. The repeated-measures
suite that established the hazard (`intconf`: the integrated cell and a `separate` control
on the same 100-row document, `longdesc_100`, at its suite default of 4× each this edition,
against 3× in the v0.6.13 edition) reads, at the control model — the only arm that ran:

| extraction model | cell | recall per repeat | cell acc (`cells_compared`) | cost/doc | min–max | mean conf |
|---|---|---|---|---|---|---|
| Sonnet 4.6 (control) | simple / **integrated** | **1.000 ×4** | 1.000 (200 ×4) | $0.2053 | $0.2050–0.2057 | 0.9973–0.9984 |
| Sonnet 4.6 (control) | simple / separate | 1.000 ×4 | 1.000 (200 ×4) | $0.2054 | $0.2051–0.2056 | 0.9977–0.9983 |

At v0.6.5 the first row read **0.000 ×4** (1–10 rows returned, none matching). The reason
the two cells cost the same to within $0.0001 is that they *are* the same mechanism: a
simple-mode section whose class declares a list field is routed to the separate confidence
pass regardless of the `integrated` setting (#795; `metadata.confidence_mode_effective`
records it). A class can opt back into true inline scoring with
`x-aws-idp-allow-integrated-lists: true`, and these numbers do not cover that path. Both
arms produced one section per run and **0 of 2,456 confidence leaves below 0.9**.

**The advanced-mode counterpart is closed too, and it is the arm measured at the shipped
default.** `advverify__extraction-model-sonnet5` (the agentic cell with `integrated` and
`separate` confidence on `longdesc_100`, 4× each, Sonnet 5): **8 of 8 runs complete**,
recall 1.000 and cell accuracy 1.000 over 200 `cells_compared` per run, one section each.
Cost is `integrated` **$0.3528** mean ($0.2636–0.5329) against `separate` **$0.2477**
($0.2344–0.2863), so integrated is about 42% dearer on the agentic path at this size, with
the widest single-run spread of the suite. At v0.6.5 the agent declined the table tool over
one bad column and returned the whole 100-row list as `null` — the tool-decline hazard the
v0.6.6/v0.6.7 editions tracked (#666/#668) — and it did not recur.

> These are repeated measures on the one document each hazard fired on. The grid adds
> single draws on six more; none fired. The v0.6.5 evidence tables that documented the
> mechanism are retained in that edition (`git checkout` per Appendix A) and are not
> restated here.

---

## 3. Scaling limits by document size

Simple vs advanced, Textract TABLES + separate confidence, one transaction list of N rows
(~48 rows/page), and exactly one section in every run that produced one at all — 13 of the
14, the exception being the 3,200-row simple run, which produced none. Extraction model
**Sonnet 4.6, the cross-version control**: the `scaling` suite carries no model override.
⚠️ **One draw per mode per size this edition** — the suite's committed default — where the
v0.6.13 edition ran three, so no figure below has a range and a single draw on either side of
a limit is not distinguishable from a shift in the limit. For the shipped default at the top
of this range, §5.2's `astracap` suite is the measurement.

⚠️ **Read the `conf coverage` column beside the recall column.** At 1,200 and 1,600 rows
simple mode returns every row and reports `COMPLETED`, and some of those rows carry no
confidence score.

| rows | pages | SIMPLE status | simple recall | simple cell acc (`cells_compared`) | conf coverage | simple $ | wall | ADVANCED status | adv recall | adv cell acc (`cells_compared`) | adv $ | wall |
|-----:|------:|---|---:|---|---:|---:|-----:|---|---:|---|------:|-----:|
| 25 | 1 | COMPLETED | 1.000 | 1.000 (50) | 1.000 | $0.0431 | 26 s | COMPLETED | 1.000 | 1.000 (50) | $0.0958 | 37 s |
| 100 | 3 | COMPLETED | 1.000 | 1.000 (200) | 1.000 | $0.1440 | 52 s | COMPLETED | 1.000 | 1.000 (200) | $0.1759 | 59 s |
| 400 | 9 | COMPLETED | 1.000 | 1.000 (800) | 1.000 | $0.5255 | 209 s | COMPLETED | 1.000 | 1.000 (800) | $0.6694 | 140 s |
| 800 | 17 | COMPLETED | 1.000 | 1.000 (1,600) | 1.000 | $1.6192 | 555 s | COMPLETED | 1.000 | 1.000 (1,600) | $1.3260 | 126 s |
| 1,200 | 25 | **COMPLETED ⚠️** | 1.000 | 1.000 (2,400) | **0.948** | $2.2882 | 1,002 s | COMPLETED | 1.000 | 1.000 (2,400) | $1.9050 | 147 s |
| 1,600 | 33 | **COMPLETED ⚠️** | 1.000 | 1.000 (3,200) | **0.798** | $3.2134 | 1,079 s | COMPLETED | 1.000 | 1.000 (3,200) | $2.5529 | 221 s |
| 3,200 | 66 | **FAILED** | 0.000 | — (0 rows) | — | $1.0543 ‡ | 47 s | COMPLETED | 1.000 | 1.000 (6,400) | **$5.0135** | **374 s** |

`cells_compared` is per run. ‡ A refused document bills no extraction or assessment phase,
so this figure is **OCR plus classification only** and is not the cost of the work
attempted. Advanced-mode confidence coverage is 1.000 at every size.

**Advanced mode is at recall 1.000, cell accuracy 1.000 and full confidence coverage at every
size from 25 to 3,200 rows, on 7 of 7 runs**, one section per document throughout. Sharding
keeps each call small, so neither the single-response truncation nor an input-context limit
nor the confidence pass's time budget is reached.

Per-row **cell accuracy is 1.000 in every run of both modes that returned rows at all** — at
no size does either mode return a row with a *wrong* value.

### How simple mode behaves as documents grow

The simple-mode column above is not a gradient. It is three distinct behaviours with two
sharp boundaries, and they want different responses from a deployment:

1. **25 → 800 rows / 1 → 17 pages: it completes, with everything.** `COMPLETED`, recall
   1.000, cell accuracy 1.000, every row confidence-scored. At 800 rows the confidence pass
   is already the larger part of the bill — **$0.67 of $1.62 per document**, against $0.04
   for the same pass in advanced mode — and of the wall-clock: 555 s against 209 s at 400
   rows. The batch-shrinking ladder converges there (`assessment_recovered_with_retries`).
2. ⚠️ **1,200 and 1,600 rows / 25 and 33 pages: every row is extracted and returned, and
   some of them have no confidence.** Both runs report `COMPLETED`, recall 1.000 and cell
   accuracy 1.000 over 2,400 and 3,200 `cells_compared`, validated clean. And both carry
   `assessment_incomplete`: the confidence pass scored **1,138 of 1,200 rows and 1,276 of
   1,600**, then stopped to stay inside the Lambda's time budget, so the remaining **62 and
   324 rows come back with `confidence: null`** and a per-row reason naming the time budget
   ([#1488](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1488)).
   That is a large move from the v0.6.13 edition, where the same pass scored only 33–38% of
   the rows at 1,200 and 19–23% at 1,600 over three draws each: **95% and 80% coverage on
   this release**. One draw per size here, so read it as a direction until it is repeated.
   It is consistent with §5.2, where Sonnet 5's agentic `adv-wide` cell on 3,200 rows moved
   from 22–78% confidence coverage to 97–99% between the two releases, and with this
   release's confidence-step memory fix
   ([#1678](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1678)),
   but the run records no per-batch timings, so that attribution is an inference. A consumer
   that gates review on confidence still has to handle the unscored rows, because an
   unscored row raises no confidence alert.
3. **3,200 rows / 66 pages: refused before extraction inference, which is correct.**
   `FAILED` in about 47 s having returned **0 rows and 0 sections**, with no extraction
   tokens billed at all — the spend is the OCR and classification that had already run. The
   request is too long for one call; `benchmarks/matrices/config_matrix.yaml` records this
   size as an expected rejection rather than a regression, and §5.2's `astracap` reproduces
   it 4 times over, twice on Sonnet 5 and twice on GPT-6 Astra. Refusing is the right
   outcome: nothing is attempted and nothing wrong is returned.

⚠️ **So in regime 2 neither `status` nor `completeness_recall` tells you what is missing —
`conf_coverage` and the `assessment_incomplete` issue do.** A reader who ranks by recall or
by completion rate concludes simple mode is flawless at 1,600 rows, and on the extraction it
is. What is missing is the confidence on one row in five at 1,600.

> ⚠️ **No per-page input-token budget is published for the refused size.** A refused document
> bills **no extraction tokens**, so there is no record of the request. What the suite does
> measure is the simple-mode extraction input on the six sizes that completed — **2,552,
> 7,958, 26,616, 51,433, 76,482 and 101,733 tokens at 1, 3, 9, 17, 25 and 33 pages**,
> identical to the v0.6.13 edition to the token and about **3,100 tokens per page** over the
> 3–33-page span, with the 1-page point below that line because fixed overhead dominates it.
> Extrapolating it to 66 pages would produce a number this suite does not support, so none is
> given.
>
> The product mechanism behind regime 3 is unchanged and is documented rather than measured
> here. Simple mode's pre-flight estimate logs and sends; what refuses an oversized request
> is Bedrock, on the size of the request its page images make, and #994 relieved it by
> clamping every image in a many-image request to 2,000 px per side. Two separate Bedrock
> limits bind: a stricter per-image dimension cap once a request carries more than 20 image
> blocks, and a cap on the total request payload. It is the **payload** limit that produces
> `ExtractionInputTooLarge`, because Bedrock reports an oversized payload with the same
> *"Input is too long for requested model"* wording it uses for a context overflow; the
> per-image dimension rejection has its own distinct wording and is matched separately (see
> the note in `idp_common/utils/bedrock_utils.py`). The clamp cuts pixel area, so it
> relieves both.

### The row-shortfall setting (not exercised by this table)

`extraction.row_shortfall_action` is the knob that lets an observed row shortfall decide the
outcome, added at 0.6.10 for the truncated-but-`COMPLETED` shape — a run that returns a
fraction of a list and reports success, which §5.1 measures on four extraction models at
17 pages and on Sonnet 5.5 at 9. **Nothing in this suite exercises it**: no run here lost a
row, so `extraction_rows_below_ocr_estimate` never fired. The knob is described here because
it is the control a reader reaches for after reading about truncation, not because this table
measures it.

The `extraction_rows_below_ocr_estimate` detection is unchanged — the rows extracted against
the rows in the section's OCR tables of the same width, a floor of 30 and a "fewer than half"
ratio — and under `fail` the partial rows and the diagnosis are written before the section
fails, so a truncated run reports `FAILED` with a message naming the rows extracted, the OCR
estimate and the remedy. Since v0.6.13 the case where that check *cannot run* is reported
too: when the matched evidence is under 30 rows while 30 or more rows came back, the section
carries `extraction_row_count_unverified` (`info`) instead of silence (#1489). It reports
the absence of evidence and never a shortfall. ⚠️ **Its floor cuts both ways**: a list
truncated to *fewer* than 30 rows on a document without exact-width tables is reported by
neither issue, which is exactly the shape of two of Sonnet 5.5's 10-row runs on `wide_400`
in §5.1.

**The default is `warn`.** The reason is the check's evidence, not caution: matched tables
are summed over the whole section
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

### Simple-mode size limit, and what to do beyond it

On v0.6.7 simple mode reported "complete" at 1,200 rows and recovered 0.72–0.79 of the rows
at 3,200 — and that completeness was an artefact of a different behaviour: the classifier
split every large document into 6–18 sections, each small enough to extract. With #726/#817
every document is one section and that scaffolding is gone. Three things follow:

1. **The simple-mode envelope for a complete result on this corpus is 800 rows / 17 pages,
   and the 800-row point is the one to hold loosely.** At the control model it completes
   with all 800 rows and full confidence here and on all 19 of §2's cells — but §5.1
   measures the *same* 17-page document in simple mode returning between 0 and 190 of 800
   rows on some runs of four of the seven extraction models while reporting `COMPLETED`, the
   shipped default among them (8 of its 13 runs). The single long response is what varies,
   and which side of the limit a run lands on moves with the model and with the input text.
   **Treat ~400 rows / ~9 pages as the comfortable simple-mode envelope and 800 as the edge
   of it**; use advanced mode above that.
2. **Above the envelope, simple mode returns the rows without all of the confidence, then
   stops being available.** At 1,200 and 1,600 rows the extraction is complete and 5% and 20%
   of the rows are unscored on this release's single draw (regime 2); at 3,200 rows the
   request is refused outright (regime 3). Neither is improved by tuning the extraction: the
   remedy at every size above the envelope is advanced mode, which returns every row with
   confidence at all seven sizes. The `assessment_incomplete` message names two other
   remedies — more Lambda time, or less work per section — and on this evidence they treat
   the confidence half only.
3. ⚠️ **No single column summarises a simple-mode run at size.** `COMPLETED` is not evidence
   of completeness — §5.1's truncated-but-completed runs are the proof — and recall 1.000 is
   not evidence that the document is review-ready, because at 1,200 and 1,600 rows some rows
   are unscored. Read `status`, `completeness_recall`, `cells_compared` **and**
   `conf_coverage` together.

### Advanced mode: cost and processing time as documents grow

Advanced (agentic sharding) holds **recall 1.000, cell accuracy 1.000 and full confidence
coverage from 25 through 3,200 rows / 66 pages** at the control model, on 7 of 7 runs, on
denominators from 50 to 6,400 `cells_compared` per run — and, in the premium study (§5.2), at
Sonnet 5 and GPT-6 Astra too.

The practical limits are **cost and wall-clock**, and at the control model both stay modest:
**$5.0135 and 374 s at 3,200 rows** (one draw; the v0.6.13 edition's three draws ranged
$4.39–4.99 and 465–747 s), against $0.0958 and 37 s at 25 rows. Cost is close to **linear
in rows from 400 upward** — $0.00167, $0.00166, $0.00159, $0.00160 and $0.00157 per row at
400, 800, 1,200, 1,600 and 3,200 rows — so there is no cost cliff at the top of this series;
the 25-row point is the outlier, at $0.00383 per row, because the fixed per-document overhead
dominates a one-page document.

**From 800 rows upward advanced mode is the cheaper mode as well as the complete one**:
$1.3260 against simple's $1.6192 at 800 rows, $1.9050 against $2.2882 at 1,200 and $2.5529
against $3.2134 at 1,600 — and simple mode at those two sizes still leaves rows unscored that
advanced mode scores in full. The difference is almost all the confidence pass, which costs
$0.04–0.08 per document in advanced mode across that range and $0.67–1.34 in simple mode.
For the shipped default at the 3,200-row size, §5.2's `astracap` measures Sonnet 5 at
**$3.76 and $4.20 over two repeats, 434 s mean**.

---

## 4. Cost and its run-to-run variation (5 repeats of one 400-row document)

Agentic-advanced cost is **non-deterministic run-to-run** (the agent's turn count varies),
so a single sample cannot resolve a cost difference between configs. The suite measures cost
with repeats and reports mean ± stdev + coefficient of variation (CV); a cost difference is
only trustworthy when it exceeds the sampling spread.

Same document (`med_narrow`, 400 rows / 9 pages), 5 repeats per cell, 25 runs per model —
one suite at the shipped default and one at the control. In **both** suites all 25 runs
returned `COMPLETED` at **recall 1.000**, **cell accuracy 1.000 over 800 `cells_compared`
per run** and **one section**, so every figure below is a cost comparison between
configurations that all did the job, with no completeness difference confounding it.

Extraction model **Sonnet 5**, the shipped default (`cost__extraction-model-sonnet5`),
re-priced at Sonnet 5's published rates (see the header):

| config (OCR / mode / assessment) | cost mean ± stdev | CV | min–max | sections/run |
|----------------------------------|-------------------|---:|---------|---|
| BDA / advanced / separate | **$0.501 ± $0.032** | 6.5% | $0.485–0.559 | 1 ×5 |
| Textract TABLES / advanced / separate | $0.508 ± $0.046 | 9.0% | $0.485–0.589 | 1 ×5 |
| Textract TABLES / **simple** / separate | $0.534 ± $0.003 | **0.5%** | $0.533–0.539 | 1 ×5 |
| Textract TABLES / advanced / integrated | $0.684 ± $0.094 | 13.7% | $0.549–0.780 | 1 ×5 |
| Textract LAYOUT / advanced / separate | $0.861 ± $0.197 | **22.9%** | $0.624–**1.092** | 1 ×5 |

The same five cells at the **control model, Sonnet 4.6** (`cost`, also 5 repeats, all
complete, one section each):

| config | cost mean ± stdev | CV | min–max |
|---|---|---:|---|
| Textract TABLES / simple / separate | $0.527 ± $0.000 | **0.1%** | $0.527–0.527 |
| Textract TABLES / advanced / separate | $0.574 ± $0.003 | **0.5%** | $0.572–0.577 |
| BDA / advanced / separate | $0.575 ± $0.003 | **0.5%** | $0.572–0.578 |
| Textract LAYOUT / advanced / separate | $1.135 ± $0.225 | **19.9%** | $0.973–1.468 |
| ⚠️ Textract TABLES / advanced / integrated | **$3.256 ± $0.870** | **26.7%** | $2.849–**4.812** |

**Findings**

- **At 400 rows mode is not a price decision.** On the matched Textract TABLES pair, advanced
  costs **$0.508 against simple's $0.534** at Sonnet 5 — advanced is the cheaper one, inside
  its own spread — and **$0.574 against $0.527, 1.09×** at the control. **BDA / advanced at
  $0.501 is the cheapest cell at the shipped default.** §3's size series shows the advanced
  premium *shrinking* with size at the control — 2.22× at 25 rows, 1.22× at 100, 1.27× at
  400 on that single draw — and **inverting at 800**, where advanced costs $1.3260 against
  simple's $1.6192. Advanced mode is the expensive option on short documents, not on long
  lists.
- **Simple mode is the predictable one at both models.** The control's simple cell is flat to
  the cent across five runs (CV 0.1%), and at Sonnet 5 the same cell is at CV 0.5%. In the
  v0.6.13 edition the Sonnet 5 simple cell ran at CV 4.4% because two of its five runs needed
  the confidence pass's batch-shrinking ladder; none did here.
- **The run-to-run spread is a property of the agentic path, and it is wider at the shipped
  default.** Advanced cells run at CV **6.5–22.9%** at Sonnet 5, and at the control **0.5%**
  on TABLES and BDA but **19.9%** on LAYOUT and **26.7%** on the integrated cell. The widest
  single cell at the shipped default is LAYOUT / advanced, which ranged **$0.624 → $1.092**
  across five identical runs of the same document, a **1.75×** swing with no configuration
  difference behind it. Every run in both suites is one section, so what is left is the
  agent's own turn count.
- **Budget the maximum, not the mean.** The largest gap between a cell's mean and its own
  worst run is the control's agentic + `integrated` cell, **$4.812 against a $3.256 mean, 48%
  above it**; at the shipped default it is LAYOUT / advanced, $1.092 against $0.861, **27%
  above**. ⚠️ **One cell exceeds the harness's CV > 0.25 unreliable-at-current-n flag** — the
  control's agentic + `integrated` cell at 0.267, as in the v0.6.13 edition — so its mean is
  not resolved at n=5. The other nine are.
- ⚠️ **The agentic + `integrated` cell is not a clean configuration cost at either model.** At
  Sonnet 4.6 it is **$3.256 against the `separate` cell's $0.574 — 5.7× —** and all five runs
  carry `extraction_output_truncated`, with extraction output averaging **171,835 tokens
  against the `separate` cell's 13,524**: the agent is emitting inline assessment, overrunning
  its output budget and retrying, and the dollar figure is mostly that retry. At Sonnet 5 the
  same cell reads $0.684 but carries `assessment_incomplete` on 5 of 5 runs and **no
  assessment spend at all** ($0.0000 in the Assessment phase), so the confidence work it is
  priced for did not land — the same shape as the v0.6.13 edition. Both arms reach recall
  1.000 and cell accuracy 1.000 over 800 cells, by different routes. Use `separate` on the
  agentic path; §2a measures the same contrast across seven document sizes.
- **BDA / advanced is the cheapest advanced option at the shipped default** ($0.501, CV 6.5%)
  and within a tenth of a cent of the cheapest at the control ($0.575 against TABLES'
  $0.574).
- **LAYOUT / advanced is the dearest advanced option at both models, not the cheapest.** Its
  *minimum* run ($0.624 at Sonnet 5, $0.973 at the control) sits above the TABLES advanced
  cell's *maximum* ($0.589, $0.577), so the ordering holds run-to-run rather than only on the
  mean. Extraction is where it goes: $0.795 of its $0.861 at Sonnet 5, against $0.343 of
  $0.508 for TABLES.
- **Why advanced costs anything extra at all, given the deterministic table tool:** it is a
  multi-turn agent loop, and each turn re-sends the growing conversation as *input* tokens.
  With one section per document that loop now runs once.

> **Model note.** §4's Sonnet 5 table is the only Sonnet 5 measurement in §2–§4; §2 and §3
> are at the control, so compare §4's control table with those and read the Sonnet 5 table
> against §5. Do not compare absolute dollars across the two tables — only shapes.

> Methodology note: run these cells with `--suite cost` (or `--repeats ≥5`); the harness
> flags any cell with cost CV > 0.25 as unreliable-at-current-n, and
> `aggregate.py --compare` only reports a cost regression when the mean shift exceeds the
> combined sampling spread. That treatment covers accuracy and completeness too
> ([index.md](index.md)).

---

## 5. Choosing models by document type

Everything above holds the extraction model at the cross-version control. This section
varies it — and the classification and confidence models — on the **same 19-cell ×
7-document grid** (`coresynth`, 133 runs per model), plus a dedicated premium study on the
documents where a premium model *could* matter, plus (§5.6) the classification
configuration on a labelled packet corpus. Every model row below is computed by the same
scorer on the same documents; rows are directly comparable.

Models: Amazon Nova Lite and Nova Pro (the cheap end), Claude Sonnet 4.6 (the study's
cross-version control), **Claude Sonnet 5 (the shipped default)**, Sonnet 5 `:1m` (the
1M-token-context variant), Claude Opus 5, Claude Opus 5.5, Claude Haiku 5.5, Claude Sonnet
5.5, and OpenAI GPT-6 Astra (`us.` and `global.`). No selectable model was added in v0.6.14,
so the axis is the same as the v0.6.13 edition's. Nova 2 Lite classification and Nova Lite
confidence are held at their defaults except where they are the axis.

### 5.1 Extraction model — the full grid

**All nine swept models were measured on v0.6.14.** Seven ran the 19-cell × 7-document
`coresynth` grid; the two Nova models cannot complete the agentic path
([#895](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/895))
and ran `simplegrid` instead, so they are reported separately below and never averaged in.
The grid's documents run to **800 rows**; the larger sizes are §3's and §5.2's territory.
**All 931 runs of the seven-arm grid completed** — the v0.6.13 edition had two
non-completions, an Astra and a Haiku 5.5 run that failed in the confidence pass (#1678).

| extraction model | runs | completed | mean recall | mean cell accuracy | `cells_compared` | grid cost | wall/doc | % conf leaves < 0.9 |
|---|---:|---:|---|---|---:|---|---:|---|
| **Claude Opus 5.5** | 133 | **133** | **1.0000** | **1.0000** | **83,790** | **$105.95** | 228 s | 0.000% |
| Claude Opus 5 | 133 | **133** | **1.0000** | **1.0000** | **83,790** | $127.08 | 284 s | 0.018% |
| GPT-6 Astra | 133 | 133 | 0.9924 | 0.9810 | 82,184 | $191.85 ◊ | 211 s | 0.000% |
| Claude Sonnet 5 `:1m` | 133 | 133 | 0.9524 | 0.9995 | 74,188 | $67.62 § | 212 s | 0.016% |
| Claude Haiku 5.5 | 133 | 133 | 0.9502 | 0.9998 | 73,906 | $25.76 † | 171 s | 0.002% |
| Claude Sonnet 5 *(shipped default)* | 133 | 133 | 0.9463 | 0.9998 | 72,364 | $65.00 § | 200 s | 0.003% |
| **Claude Sonnet 5.5** | 133 | 133 | **0.8905** ※ | 1.0000 ※ | 72,130 | $62.21 ‡ | 170 s | 0.000% |

⚠️ **Read `cells_compared` alongside the accuracy columns, not after them.** A cell the arm
never returned is a cell it is not scored on, so an arm that returns fewer rows is graded on
a smaller denominator — the range here is 72,130 to 83,790, a 16% spread. That is why the
recall column is the one to rank on and the accuracy column is a statement about the rows an
arm *did* return. The shipped default's 0.9998 over 72,364 cells and Opus 5.5's 1.0000 over
83,790 are not the same achievement.

**Two arms are at the ceiling of this grid, and the cheaper one is the newer — for the second
edition running.** Claude Opus 5.5 and Claude Opus 5 both completed **133 of 133** runs and
both scored **recall 1.0000 and cell accuracy 1.0000 over the full 83,790 cells**. Opus 5.5
did it for **$105.95 against $127.08 — 17% less** — and both are flat-rate, so that one is a
true ratio. It is also the faster of the two, 228 s against 284 s per document (median 161 s
against 171 s). The v0.6.13 edition measured the same pair at $107.67 against $128.52, so
the ratio has held across two releases.

**GPT-6 Astra is the most expensive arm and the least accurate on values.** Its 0.9810 cell
accuracy over 82,184 cells is the lowest in the table on an essentially full denominator, so
it is not a denominator artefact: on five of the seven documents it is 0.974, against 1.0000
for both Opus arms. That is up from 0.9735 in the v0.6.13 edition, inside the draw-to-draw
spread of a one-draw-per-cell grid. Its recall is 0.9924 because of one simple-mode run on
the 17-page document that came back empty (`extraction_incomplete`, 0 rows, `COMPLETED`) —
the v0.6.8 edition's empty-response shape, on one of 13 draws again — and one that returned
799 of 800. Its $191.85 is a **lower bound** (◊), so Opus 5.5 is **at least 45% less** than
Astra for a better result.

**The shipped default's whole shortfall is still one document in one mode.** Sonnet 5 is at
recall 1.0000 on every document of 400 rows or fewer (19 of 19 runs each), and on
`large_narrow` in advanced mode (6 of 6). In **simple** mode on `large_narrow` it returned all
800 rows on **5 of 13** runs, and 43 to 190 rows on the other 8, each `COMPLETED` and each
carrying `extraction_rows_below_ocr_estimate` — so the truncation is flagged, not silent. That
is per-document recall 0.624 on `large_narrow`, against 3 of 13 complete in the v0.6.13
edition; one draw per cell, so read the difference as the variability of a bimodal behaviour
rather than as a release effect. Sonnet 5 `:1m` and Haiku 5.5 show the same shape at a
similar rate (6 short runs of 13 each, per-document recall 0.701 and 0.698), and each also
lost rows on one or two `wide_400` runs.

※ ⚠️ **Do not read Sonnet 5.5's row as "cheap and perfectly accurate". Its mean recall hides
a bimodal failure, and its cell accuracy is computed on the rows it chose to return.** 15 of
its 133 runs lose most of the list, every one of them on a 400-row document and every one in
simple mode
([#1494](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1494)):

| rows returned of 400 | runs | documents | cells compared per run | what the run reports |
|---|---:|---|---:|---|
| **2** | 2 | `med_narrow` | 0 | `extraction_list_field_not_a_list` and `extraction_validation_failed` |
| **10** | 9 | `manylists_400` (4), `med_narrow` (3), `wide_400` (2) | 20 | `extraction_rows_below_ocr_estimate` on 7; **nothing** on the 2 `wide_400` runs |
| **20** | 4 | `manylists_400` | 40 | `extraction_rows_below_ocr_estimate` |

So "cell accuracy 1.0000" on those runs means **100% correct on the 10 or 20 rows it returned
out of 400**. Per-document recall is **0.7413 on `med_narrow` (11,260 cells), 0.8974 on
`wide_400` (13,640) and 0.5947 on `manylists_400` (9,040)**, against 1.0000 on every smaller
document and on the 800-row one (19 of 19 runs complete there). On a 400-row list this model
is the **worst** arm in the table. Against the v0.6.13 edition (16 short runs of 133, 0.6900 /
0.7108 / 0.7947) the count is the same and the documents it falls on moved, which is what a
bimodal behaviour measured one draw per cell looks like.

**Detection is better than in the v0.6.13 edition and still has the structural gap.** 13 of
the 15 short runs carry an `error`- or `warning`-severity completeness issue, against 8 of 16
before. What stays silent are **two 10-row runs on `wide_400`**, whose OCR tables are 8
columns wide against a 3-property item schema: there is no exact-width table to check a row
count against, and 10 returned rows is under the 30-row floor below which even
`extraction_row_count_unverified` does not fire. A short list on a document without matching
tables is reported by nothing.

**Recommendation: do not select Claude Sonnet 5.5 for list extraction on documents of a few
hundred rows until #1494 is understood.** It is sound on the smaller documents and on the
800-row one. Three further properties of the model make its remaining columns non-comparable,
independent of the above:

- It **rejects a forced `toolChoice`**, so Schema Enforcement falls back to the prose schema and
  `extraction.forced_tool` is recorded as skipped (0 of 7 attempts on this grid). Any `forcing`
  or `enforcement` comparison involving this model measures the prose path whatever the knob
  says.
- It rejects `temperature`/`top_p`/`top_k`; the accelerator strips them for this model id, so
  this is invisible here but will fail a call made by any other route.
- **1M is its default window with no `:1m` id**, which makes the derived agentic shard budget
  578,400 tokens against Sonnet 5's 18,400. Wide shards defeat prompt caching, so its cost per
  document is not comparable with Sonnet 5's on the agentic path unless
  `extraction.agentic.shard_token_budget` is pinned.

† No cost *ratio* is published for Haiku 5.5 — two-band pricing, see the header. Its dollar
figure is a lower bound.
‡ Sonnet 5.5's $62.21 and Sonnet 5's $65.00 are both at published rates, which are identical on
input and output; Sonnet 5.5 is 4% cheaper over the grid, which is consistent with its
half-price cache reads, and it returns fewer cells (※).
§ Re-priced at Sonnet 5's published rates (see the header). As priced at run time these were
$89.71 and $86.38.
◊ GPT-6 Astra's $191.85 is a lower bound — it moves to a long-context band above 272,000 input
tokens that `pricing.yaml` cannot express — so the one cost comparison drawn against it above
is a one-sided bound.
`wall/doc` is the mean of each run's end-to-end `wall_s` (OCR, classification, extraction and
the confidence pass together), populated on all 133 rows of every arm. The arms ran in
parallel lanes on one stack, so wall-clock carries load from the other lanes; compare it within
this table rather than with other sections. ⚠️ Four of the seven arms (Opus 5, Opus 5.5,
Sonnet 5 `:1m`, Astra) ran on 2026-11-07 and three (Sonnet 5, Haiku 5.5, Sonnet 5.5) on
2026-11-09, with different lanes beside them, so their wall columns carry different load.

**The cheap end does not fail, it silently under-returns**, which is the more dangerous shape:

| extraction model (`simplegrid`) | runs | completed | mean recall | mean cell accuracy | `cells_compared` | grid cost | % confidence leaves < 0.9 |
|---|---:|---:|---|---|---:|---|---|
| `simplegrid` control (Sonnet 4.6) | 63 | 63 | 1.0000 | 0.9993 | 39,690 | $31.13 | 0.006% |
| Amazon Nova Pro | 63 | **63** | **0.5035** | 0.9225 | 7,858 | $10.81 | 0.025% |
| Amazon Nova Lite | 63 | **63** | **0.4980** | 0.7334 | 9,404 | $8.41 | 0.549% |

Both Nova arms completed **every run with no error** and returned **about half the rows**.
Nova Pro's 0.9225 cell accuracy is computed over 7,858 cells against the control's 39,690 — it
is "92% accurate on the rows it chose to return", not 92% accurate. On the three documents of
100 rows or fewer Nova Pro returns every row but not every value (cell accuracy 0.881 on
`small_narrow`, 0.987 on `longdesc_100`), and Nova Lite returns 89% of `small_narrow`'s rows
at 0.828 accuracy and 0.847 on `longdesc_100`. Above 100 rows both return about half of the
list at best — per-document mean recall from 0.000 to 0.445, and no single run above recall
0.5025 (201 of 400 rows, Nova Pro on `wide_400`) — `COMPLETED` every time.

⚠️ **And the confidence signal does not catch it.** Nova Lite flags **0.549%** of its
confidence leaves and Nova Pro **0.025%**, on arms that returned about half the rows. There is
no safety net in the model's own confidence here; the completeness signals in `extraction_*`
are what catch it. **Neither model is viable for list extraction above 100 rows, and neither is
accurate on values at 100**; their dollar figures can be priced only against this matrix's own
Sonnet 4.6 control ($31.13 over 39,690 cells), never against the seven-arm grid.

Two value statements follow from the seven-arm grid, both anchored to its own numbers and their
denominators.

**Claude Opus 5 is dominated by Opus 5.5 on this corpus.** Same completeness, same accuracy,
same denominator — and 20% dearer ($127.08 against $105.95) and slower. There is no profile in
this grid where Opus 5 is the better pick.

**Claude Haiku 5.5 matches the default's per-cell accuracy and recall, and its cost cannot be
quoted.** At 0.9998 cell accuracy over 73,906 cells it is indistinguishable from Sonnet 5's
0.9998 over 72,364, and its recall is within a point (0.9502 against 0.9463) — both short of
the Opus arms for the same reason, simple-mode truncation on the 17-page document, and Haiku
5.5 also drops to 0.953 on `wide_400`. Its $25.76 is the cheapest figure in the table and a
**lower bound** (†), so no cost ratio for it appears in this paper. Choose it for accuracy
parity on the rows it does return, never on a quoted saving.

> ⚠️ **The v0.6.8 block below is superseded for the model *ranking*, and is retained only for
> its per-document-size detail.** Every swept model has a v0.6.14 row in the tables above, so
> for *which model to pick* read those, and §5.5 for the per-document breakdown on this release.
> What the v0.6.8 grid still carries is its separate simple-mode and advanced-mode tables per
> document.
>
> **Do not mix rows across the two generations.** They were produced on different releases and
> with a different evaluation comparator (`stickler-eval` 0.5.0 → 1.0.0 infers comparators per
> field), so a v0.6.8 accuracy is not like-for-like with a v0.6.14 one, and the dollars are from
> a different `pricing.yaml`. Claude Opus 5.5, Claude Haiku 5.5 and Claude Sonnet 5.5 have no row
> below at all.
>
> ⚠️ **Where a per-document claim below disagrees with the v0.6.14 grid, the grid is the current
> result.** Finding 6 and the simple-mode table record GPT-6 Astra returning an **empty
> response** on the 800-row `large_narrow` document in every simple-mode cell. On v0.6.14, as
> on v0.6.13, it returned all 800 rows on 11 of its 13 simple-mode runs there and 799 on a
> twelfth, with one empty response — so the shape still occurs, on one draw in 13 rather than
> every time.

#### Seven-model grid from v0.6.8

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
   at exactly plain Sonnet 5's rates. The other four carry no `:1m` premium, but this
   block's Sonnet 5 figures were priced at the Sonnet 5 rows as they stood before #1830 (1.5×
   the published rate, and 3× for `:1m`) and are not re-priced here, so every Opus 5 and Astra
   premium over Sonnet 5 in this block is understated.
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
   request that would otherwise be refused, and the v0.6.8 §3 showed the product refusing at
   25 pages regardless of model, so on this corpus there is no request it rescues. Choose it for
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
   three times under three OCR/format variants (the v0.6.8 §2, finding 2). Both are at cell accuracy
   1.000. At +23% (simple) / +41% (advanced) — understated, like every Opus 5 premium in this
   block (finding 1) — it is a price for consistency on long lists in simple mode, not for
   accuracy.
6. **GPT-6 Astra gets values wrong where the Claude models do not, and returns nothing on
   the 17-page document.** Grid cell accuracy **0.902**: with enforcement *off* Astra emits
   amounts as strings (`"0.00"`, `"-13.70"`) on every document, which the typed match
   rejects — turn coercion on and they pass, which is a real reason to keep
   `extraction.validation`/`coercion` at their defaults with this model. And in **every**
   simple-mode cell Astra returned an **empty response** on `large_narrow` (`raw_output: ""`,
   `parsing_succeeded: false`, an `extraction_incomplete` warning, status `COMPLETED`) — 8 of
   8 cells here and 5 of 5 repeats in the v0.6.8 §5.2. Its larger window is exactly what lets that
   request be *accepted*; Sonnet 5 refuses it or truncates it.

### 5.2 Is a premium model worth it?

Three premium questions, one method: hold everything but `extraction.model` fixed, choose the
documents by **page count** — page images, not OCR text, are what fill a request (~11,000
tokens per page at 300 DPI against ~600 of text) — and repeat every cell, so that "no" is a
reportable answer. All three suites ran on v0.6.14, on 2026-11-07.

| suite | the pair, and what it asks | runs | completed | mean recall | mean cell accuracy | `cells_compared` | suite cost |
|---|---|---:|---:|---|---|---:|---|
| `opus55value` | Opus 5.5 against Opus 5 — is the newer model cheaper for the same result? | 40 | **40** | **1.0000** | **1.0000** | 20,000 | $22.86 |
| `astravalue` | Sonnet 5 against GPT-6 Astra — does a larger window buy data? | 100 | **100** | 0.8905 | 0.9999 | 70,986 | $127.29 ◊ |
| `astracap` | both models at the 66-page / 3,200-row ceiling, 2 repeats | 12 | **8** | 0.6667 | 1.0000 | 51,200 | $82.68 ◊ |

⚠️ `astracap` ran at its committed default of **2 repeats** per cell this edition; the v0.6.13
edition overrode it to 3. Its run count and refusal count are therefore 12 and 4 here against
18 and 6 there — the same refusal on every simple-mode draw at both releases, not two runs that
now complete.

Mean confidence over the premium study is 0.9900 on `opus55value`, 0.9880 on `astravalue` and
0.9814 on `astracap`, and almost nothing is scored low anywhere in it: **0 of 30,280**
confidence leaves fall below 0.9 on `opus55value`, **60 of 114,359** on `astravalue` and **0 of
76,239** on `astracap`. A review queue fed by a 0.9 threshold would see essentially nothing
from any of these arms — including, as the Astra columns below show, from the runs that
returned no usable row at all.

◊ Both Astra-bearing suite totals are **lower bounds** and carry no cost *ratio*, for the reason
given in the header; the Sonnet 5 arms inside them are re-priced at published rates. Where this
section compares the two on cost it states two dollar figures from the same suite and a
**one-sided bound** over them; Astra's bias makes "Sonnet 5 is at least X% less" safe.

⚠️ **`astracap`'s four incomplete runs are the measured outcome, not a failure of the run.** All
four are the 66-page `scale_3200.pdf` in a **simple-mode** cell — two repeats of Sonnet 5 and
two of Astra — refused as too long for one request, nothing attempted and nothing wrong
returned. Read the suite's 0.6667 mean recall with that in front of it: it is four deliberate
refusals averaged in with eight runs that did execute, every one of which returned 3,200 of
3,200 rows.

#### Opus 5.5 compared with Opus 5

The comparator is Opus 5 rather than the shipped default because the claim under test names Opus
5: cheaper input and output ($4/$20 per 1M against $5/$25), a cache read at 0.05× input where
every other model here is 0.1×, and fewer tokens for the same task. Those three compound, and the
rate card predicts about −20%. The suite is **two documents** — `small_narrow` (3 pp / 100 rows)
and `med_narrow` (9 pp / 400 rows) — × two modes × two models × 5 repeats. The two larger
documents `astravalue` uses are deliberately absent: Opus 5 and Opus 5.5 share one window and one
sizing budget, so in simple mode both arms would be refused on both documents.

| arm | 3 pp / 100 rows | 9 pp / 400 rows | `cells_compared` | arm cost |
|---|---|---|---:|---|
| Opus 5 · simple | 1.0000 · 1.0000 · $0.26 | 1.0000 · 1.0000 · $0.96 | 5,000 | $6.09 |
| **Opus 5.5 · simple** | 1.0000 · 1.0000 · **$0.22** | 1.0000 · 1.0000 · **$0.81** | 5,000 | **$5.17** |
| Opus 5 · advanced | 1.0000 · 1.0000 · $0.29 | 1.0000 · 1.0000 · $0.94 | 5,000 | $6.13 |
| **Opus 5.5 · advanced** | 1.0000 · 1.0000 · **$0.25** | 1.0000 · 1.0000 · **$0.84** | 5,000 | **$5.47** |

(each document column: `recall · cell accuracy · cost/doc`, means of 5. `cells_compared` and
`arm cost` are totals over that arm's 10 runs)

**Quality is a tie at the ceiling, on identical denominators.** All 40 runs reached `COMPLETED`,
every one returned every row, and every returned value was right: recall 1.0000 and cell
accuracy 1.0000 over **10,000 cells compared per model**. So the pair reduces to price, and both
models are flat-rate: **$10.64 against $12.22 over the same 10,000 cells — 13% less** (v0.6.13:
6% less; v0.6.12: 10% less).

**The saving lands in both modes on this release.** Simple mode is $5.17 against $6.09, **15%
less**; advanced mode is $5.47 against $6.13, **11% less**. In the v0.6.13 edition the
advanced-mode saving was under 1% ($6.14 against $6.18), so the claim that the 0.05× cache read
does not compound on the agentic path does not survive a second measurement: across the two
editions advanced mode has measured 1% and 11% less, which says the advanced figure is noisy
at n=5 rather than that it is zero. The predicted −20% remains the optimistic end. The
whole-grid figure in §5.1 (17% less) is larger because that grid includes the 17-page
document, where simple mode dominates the bill.

On wall-clock the 9-page document is a near tie in simple mode (277 s against 289 s, Opus 5.5
the faster) and Opus 5.5 is the slower in advanced (170 s against 150 s), means of 5. The suite
ran in a parallel lane, so wall-clock here carries load and does not settle which is faster; the
cost and accuracy figures are unaffected by load.

Both arms ran at the same `reasoning_effort`, `low`: no cell names the axis, so all four inherit
`default_cell`'s value, and `rows[].resolved.reasoning_effort` is `low` on all 40 rows of
`opus55value/summary.json` (and on all 133 rows of each `coresynth` Opus arm in §5.1). The
pair is therefore matched on effort, and the models' own defaults (`medium` for Opus 5.5 and
`high` for Opus 5, per the model cards) do not apply. `low` is also the shipped extraction
default, so this is the comparison a user who changes only the model gets.

#### Sonnet 5 compared with GPT-6 Astra: does the larger context window extract more?

Four documents chosen so that the window is the variable. `small_narrow` (3 pp) and
`med_narrow` (9 pp) fit both models; `large_narrow` (17 pp, ~203K tokens) is the first document
Sonnet 5 cannot reliably take in one request; `dense_250` (26 pp, ~304K, 8 columns, 4
interleaved lists, OCR noise) is the one Astra's window reaches most comfortably. 5 repeats per
cell, 100 runs, **100 completed**.

| cell | 3 pp / 100 rows | 9 pp / 400 rows | **17 pp / 800 rows** | **26 pp / 250 rows, dense** |
|---|---|---|---|---|
| Sonnet 5 · simple | 1.0000 · 1.0000 · $0.15 (1,000) | 1.0000 · 1.0000 · $0.54 (4,000) | **0.8107** · 1.0000 · $1.28 (6,486) | 1.0000 · 1.0000 · $1.11 (2,500) |
| Astra · simple | 1.0000 · 1.0000 · $0.27 (1,000) | 1.0000 · 1.0000 · $0.96 (4,000) | **1.0000** · 0.9999 · $2.10 (8,000) | **0.0000** · *not scored* · $1.30 (0) |
| `global.` Astra · simple | 1.0000 · 1.0000 · $0.25 (1,000) | 1.0000 · 1.0000 · $0.90 (4,000) | **1.0000** · 0.9999 · $1.98 (8,000) | **0.0000** · *not scored* · $0.99 (0) |
| **Sonnet 5 · advanced** | 1.0000 · 1.0000 · $0.15 (1,000) | 1.0000 · 1.0000 · $0.51 (4,000) | **1.0000 · 1.0000 · $1.01** (8,000) | **1.0000 · 0.9992 · $1.22** (2,500) |
| Astra · advanced | 1.0000 · 1.0000 · $0.43 (1,000) | 1.0000 · 1.0000 · $2.56 (4,000) | 1.0000 · 1.0000 · $3.32 (8,000) | 1.0000 · 0.9980 · $4.42 (2,500) |

(each cell: `recall · cell accuracy · cost/doc`, means of 5, with that cell's `cells_compared` in
parentheses. *not scored* means no row matched a truth key, so there was no cell to grade)

**Verdict: still no.** Read it column by column.

- **Where both models fit (3 and 9 pages)** both are at ceiling, and Astra costs more in every
  cell.
- **On the 17-page document the window buys data in simple mode.** Astra returns all 800 rows
  on **10 of its 10** simple runs across the two endpoints (v0.6.13: 8 of 10, and 798 on the
  other two). Sonnet 5 returned all 800 rows on **4 of 5** runs and 43 on the fifth (v0.6.13:
  4 of 5, 92 on the fifth), and the short run carries `extraction_rows_below_ocr_estimate`, so
  it is flagged rather than silent. Three of Sonnet 5's four complete runs carry
  `assessment_incomplete`.
- **On the 26-page document the window buys nothing, and it fails without a usable signal.**
  Sonnet 5 in simple mode returns all 250 rows in 5 of 5 at cell accuracy 1.0000 over 2,500
  cells. Astra returns **no row that matches a truth key** — 0 of 250, all 250 truth ids recorded
  as gaps, no cell scored, status `COMPLETED` — on both the `us.` and the `global.` endpoint, 5 of
  5 each, as in the v0.6.13 edition. The run carries **757 confidence leaves, exactly the count
  the complete runs carry**, mean confidence 0.99 and coverage 1.000. Its one processing issue
  is `extraction_row_count_unverified`, and ⚠️ **that is not a detection**: the same `info` issue
  appears on all ten Sonnet 5 `dense_250` runs, which returned every row, because this
  document's 8-column tables do not match the item schema's width. Nothing distinguishes the
  empty Astra result from a complete one.
- **Advanced Sonnet 5 does the job on all four documents** — recall 1.0000 everywhere, cell
  accuracy 1.0000 on three and 0.9992 on `dense_250` (Astra advanced 0.9980 there) — and it is
  the cheapest arm that does. Over the two advanced arms' identical **15,500 cells** it is
  **$14.42 against Astra's $53.63, at least 73% less**. The accuracy gained per extra dollar is
  zero or negative in every cell.

**What Astra is measurably good at here:** a complete 800-row list in *simple* mode more often
than Sonnet 5, a `global.` endpoint that cost less than `us.` on the same documents ($20.62
against $23.19 over the same 13,000 cells), and implicit prompt caching with no
`<<CACHEPOINT>>` markers. What it is not, on this evidence, is a reason to pay for extraction
that advanced mode already gets right at a fraction of the price.

#### Largest document tested: 66 pages, 3,200 rows

| cell | repeats 0 · 1 (`recall · cell accuracy`) | conf coverage | `cells_compared` (total) | $/doc |
|---|---|---|---:|---|
| Sonnet 5 · simple | **FAILED** · **FAILED** (too long) | — | 0 | $1.05 |
| Astra · simple | **FAILED** · **FAILED** (too long) | — | 0 | $1.05 |
| **Sonnet 5 · advanced, default shards** | **1.0000 · 1.0000** ×2 | **0.999 · 1.000** | 12,800 | **$3.76 · $4.20** |
| Astra · advanced, default shards | 1.0000 · 1.0000 ×2 | 1.000 ×2 | 12,800 | $21.89 · $15.14 |
| Sonnet 5 · advanced, 25-page "wide" shards | 1.0000 · 1.0000 ×2 | **0.971 · 0.992** | 12,800 | $5.14 · $6.32 |
| Astra · advanced, 25-page "wide" shards | 1.0000 · 1.0000 ×2 | 0.999 · 0.978 | 12,800 | $9.28 · $12.74 |

Three results, in the order they matter.

**Simple mode refuses at this size, and refusing is the right behaviour.** All four simple-mode
runs are turned away up front as too long for one request — on Astra as on Sonnet 5, although
Astra's nominal window is 1,050,000 tokens, so the nominal figure is not the usable one once page
images are in the request. Nothing is attempted and nothing wrong is returned: the whole $1.05 is
the OCR and classification that preceded the refusal.

**Both models hold the ceiling on the agentic path at default shards:** 3,200 of 3,200 rows at
cell accuracy 1.0000 with essentially every row confidence-scored, on all four runs. Sonnet 5
costs **$3.76–4.20** against Astra's **$15.14–21.89** on the same document, so Sonnet 5 is **at
least 72% less** — a bound, not a ratio. At the ceiling the premium model buys nothing and
costs several times as much.

**The 25-page "wide" shard now keeps both the rows and the confidence on Sonnet 5.** At v0.6.12
three of its four runs came back with 1,982–2,416 of 3,200 rows; at v0.6.13 all six returned
every row but Sonnet 5's confidence pass scored only 22%, 78% and 65% of them. On v0.6.14 both
Sonnet 5 wide-shard runs return 3,200 of 3,200 with **97% and 99%** of rows scored, at $5.14 and
$6.32 (the v0.6.13 edition's $11.37–18.12 is as priced then, not re-priced). Two draws, so read
it as a direction; it is the same movement §3 measures at 1,200 and 1,600 rows in simple mode. It is still dearer than
default-shard Sonnet 5 on both runs, so **leave `shard_pages` at its default on Sonnet 5**. On
Astra the wide shard remains the cheaper Astra configuration ($9.28 and $12.74 against $15.14
and $21.89), and is still dearer than default-shard Sonnet 5 on every run.

### 5.3 Classification model

At v0.6.7 Nova 2 Lite over-split every document 2–3×, which made Claude Haiku 4.5 the
better classifier on that release. That was a prompt defect, fixed in #817, and the axis has
been measured with the fixed prompt ever since. On v0.6.14 the axis is **Nova 2 Lite · Claude Haiku
5.5 · Claude Sonnet 5**, each over the full 19-cell × 7-document `coresynth` grid at the control
extraction model (Sonnet 4.6), 133 runs per arm, with one section the truth for every document.

| classification model | runs | completed | sections correct | mean recall | mean cell accuracy | `cells_compared` | classification $/doc | total $/doc | grid cost |
|---|---:|---:|---:|---|---|---:|---|---|---|
| **Nova 2 Lite** *(shipped default)* | 133 | **133** | 132 / 133 | **1.0000** | 0.9998 | 83,790 | **$0.0069** | **$0.676** | **$89.93** |
| Claude Haiku 5.5 | 133 | **133** | **133 / 133** | **1.0000** | 0.9997 | 83,790 | $0.0086 † | $0.681 † | $90.59 † |
| Claude Sonnet 5 | 133 | **133** | **133 / 133** | **1.0000** | 0.9997 | 83,790 | $0.1263 § | $0.798 § | $106.11 § |

† Haiku 5.5's dollar figures are **lower bounds** — banded pricing, see the header. § Sonnet 5's
are re-priced at its published rates (see the header).

**All three arms are graded on the identical 83,790 cells and complete every run**, so these rows
need no denominator caveat and the differences between them are as small as they look.

**Guidance: keep Nova 2 Lite. Upgrading the classification model buys nothing measurable.** The
classifier is at its ceiling on this corpus: the boundary is right on 133 of 133 runs under
Haiku 5.5 and Sonnet 5, and on **132 of 133 under the default**, which split the 8-column
`wide_400` in two on one cell (`core-tt-simple-off`). In the v0.6.13 edition it was Sonnet 5
that missed two boundaries and the default that missed none, so across two editions each arm
has missed 0–2 boundaries in 133 — one classifier draw in about a hundred, with no arm
consistently better. Downstream recall is 1.0000 in all three and cell accuracy differs in the
fourth decimal.

What the upgrade does change is the bill. Sonnet 5's classification phase costs **$0.1263 per
document against the default's $0.0069 — 18×** — which lifts the whole-grid cost from **$89.93 to
$106.11, 18% more**: classification is about a sixth of the Sonnet 5 arm's per-document bill
against about 1% of the default's. Haiku 5.5's grid is $0.66 above the
default's, of which its classification phase accounts for about $0.23.

The one boundary shape the default still gets wrong — a reprinted running header, §7 — is not a
model problem either (§7's boundary table is Nova 2 Lite; the v0.6.7 probe measured Haiku 4.5 and
Sonnet 5 failing the same shape). Where classifiers *do* differ is the confidence score they emit:
the v0.6.7 [classification-confidence study](studies/classification-confidence.md) measured Haiku
4.5's calibration separation at 0.207 against Nova 2 Lite's 0.044 on DocSplit-Poly-Seq, and §5.6
re-measures Nova 2 Lite on that corpus at the shipped default — so if you *act* on
classification confidence, read that study. Haiku 4.5 itself is not in the sweep;
`--set classification_model=haiku45` reproduces it on demand.

### 5.4 Confidence model

Re-measured in full on v0.6.14 — the whole 19-cell × 7-document `coresynth` grid, 133 runs per
model, extraction held at the control (Sonnet 4.6):

| confidence model | runs | completed | mean recall | mean cell accuracy | `cells_compared` | mean confidence | leaves below 0.9 | coverage | assessment $/doc | total $/doc | grid cost | wall |
|---|---:|---:|---|---|---:|---|---|---|---|---|---|---|
| **Nova Lite** *(shipped default)* | 133 | **133** | 1.0000 | 0.9997 | 83,790 | 0.9921 | **0** of 119,937 | **1.0000** | **$0.094** | **$0.679** | **$90.33** | 188 s |
| Nova 2 Lite | 133 | **133** | 1.0000 | 0.9997 | 83,790 | 0.9954 | ~**2** of 119,952 | **1.0000** | $0.152 | $0.736 | $97.91 | **157 s** |
| Claude Sonnet 5 | 133 | **133** | 1.0000 | 0.9997 | 83,790 | 0.9724 | ~**638** of 119,960 | **1.0000** | $0.633 § | $1.208 § | **$160.70** § | 159 s |

§ re-priced at Sonnet 5's published rates (see the header).

**All three arms completed every run on the identical 83,790 cells at identical recall and cell
accuracy**, so on this corpus the confidence model changes nothing about what is extracted; the
columns that differ are the score itself, its cost and its wall-clock. The low-confidence column
is given as a **count of leaves** rather than a share, because the counts are what decides
whether a review queue is worth having.

⚠️ **Confidence *discrimination* cannot be measured on this corpus, so no AUROC column is
published.** The extraction is too accurate for it: across ~79,400 calibration observations per
arm there are only **37 to 41 errors — an error rate of about 0.05%**, every one of them on the
Bedrock-LLM OCR cell — and `calibration_reliable` is **true on only 18 of 126 scored runs** in
both Nova arms and 41 of 126 in the Sonnet 5 arm. On every reliable Nova run the AUROC is
undefined, because the run has no error to rank; Sonnet 5 reports one on three runs, at 0.90,
0.90 and 0.98 (v0.6.13: four runs, 0.58–1.00). Three runs do not support a conclusion about
confidence quality. **On this corpus the choice rests on cost, coverage and threshold
behaviour.**

**Guidance: keep Nova Lite, and read the low-confidence column before changing it.** The default
is the cheapest at **$0.679 per document**, its coverage is now 1.0000 like the alternatives', and
nothing measurable here distinguishes its confidence *quality* from theirs. **Nova 2 Lite is
+8.4%** ($97.91 against $90.33 over the same grid) for a faster wall (157 s against 188 s). Take
it if that matters.

**Sonnet 5 as the confidence model is not an accuracy upgrade. It is a far more conservative
scorer, and that is the whole of what it buys.** It scores about **638 of 119,960** leaves below
0.9, against the default's **0** and Nova 2 Lite's **~2**, for **$160.70 against $90.33** over the
same grid, 1.78×, almost all of it in the assessment phase ($0.633 per document against $0.094). That
is more than twice the 285 it flagged in the v0.6.13 edition on the same grid, with the same
cell accuracy; the run does not record why. The assessment phase is several times the
default's. Choose the confidence model and the review threshold together: on either Nova arm a
0.9 threshold is close to inert on this corpus, and on Sonnet 5 it is the one setting that
actually populates the confidence-alerts filter.

**Whether that conservatism is worth paying for depends on whether your corpus has anything wrong
in it**, and the one grid cell that does is Bedrock-LLM OCR (`core-llm-simple-sep`, 7 runs per
arm), where about 0.5% of cells are wrong:

| confidence model | cell accuracy on that cell | `cells_compared` | mean confidence on that cell | leaves below 0.9 |
|---|---|---:|---|---|
| Nova Lite *(default)* | 0.9952 | 4,410 | 0.9980 | **0** of 6,664 |
| Nova 2 Lite | 0.9945 | 4,410 | 1.0000 | **0** of 6,664 |
| **Claude Sonnet 5** | 0.9950 | 4,410 | **0.9469** | **173** of 6,668 |

Two things follow, and they point in opposite directions. **The two cheap confidence models are
blind here**: with about 0.5% of cells wrong they score everything at 0.998–1.000 and flag
nothing, so a review queue fed by them would have caught none of the corrupted identifiers.
**Sonnet 5 does see them** — 173 of 6,668 leaves on that cell, about 3% of its leaves there
against about 0.4% on the rest of the grid. That is the right direction, the same as in the
v0.6.13 edition (155 of 6,664 there), and it is the only positive evidence in this edition that
any of the three scores wrongness rather than just scoring low. It does not establish that the
173 flagged leaves are the wrong ones; that needs a per-leaf join this cell's sample is too
small to carry. On this release most of what Sonnet 5 flags (about 465 of 638 leaves) is on
cells with no wrong value at all.

So: **Nova Lite is right where the extraction is trustworthy and the score is for after-the-fact
triage.** If the score gates human review *and* the corpus produces wrong values, Sonnet 5 is the
only confidence model in this grid that measurably lowered its score on them, at several times
the assessment cost. Measure the separation on your own error-bearing sample before paying that.

### 5.5 Recommendations by document profile

**The headline of this edition's model axis is the same as the last two: on a large document the
*mode* matters more than the model.** Measured on `large_narrow` (800 rows of ground truth, 17
pages), **all seven `coresynth` arms return 800 of 800 rows in advanced mode** — 6 of 6 runs each,
recall 1.0000 and cell accuracy 1.0000 over 9,600 cells per arm. In the thirteen simple-mode cells
the same seven split into two groups, and the shipped default is in the wrong one:

| arm | simple mode (13 cells) | advanced mode (6 cells) |
|---|---|---|
| **Claude Opus 5.5** | 13/13 completed, **800 / 800 (1.0000)**, 20,800 cells, acc 1.0000, 686 s | 6/6, 800/800, 9,600 cells, acc 1.0000, 257 s |
| Claude Opus 5 | 13/13 completed, **800 / 800 (1.0000)**, 20,800 cells, acc 1.0000, 1,216 s | 6/6, 800/800, 9,600 cells, acc 1.0000, 214 s |
| Claude Sonnet 5.5 | 13/13 completed, **800 / 800 (1.0000)**, 20,800 cells, acc 1.0000, 570 s | 6/6, 800/800, 9,600 cells, acc 1.0000, 215 s |
| GPT-6 Astra | 13/13 "succeeded", ~738 / 800 (0.9230), 19,198 cells, acc 0.9997 | 6/6, 800/800, 9,600 cells, acc 1.0000 |
| Claude Sonnet 5 `:1m` | 13/13 "succeeded", ~451 / 800 (0.5632), 11,716 cells, acc 1.0000 | 6/6, 800/800, 9,600 cells, acc 1.0000 |
| Claude Haiku 5.5 | 13/13 "succeeded", ~447 / 800 (0.5591), 11,630 cells, acc 0.9992 | 6/6, 800/800, 9,600 cells, acc 1.0000 |
| **Claude Sonnet 5** *(shipped default)* | 13/13 "succeeded", **~361 / 800 (0.4507)**, 9,374 cells, acc 0.9990 | 6/6, 800/800, 9,600 cells, acc 1.0000, 293 s |

(the two Nova arms are not in this table: they ran the smaller `simplegrid` matrix and neither
finishes the agentic path, #895 — see §5.1. Wall-clock is given where it informs a choice.)

⚠️ **The arms at the bottom do not fail on this document — they truncate, and report success.**
Sonnet 5 returns 43 to 190 rows on 8 of its 13 simple-mode runs, `:1m` and Haiku 5.5 on 6 of 13
each (Haiku 5.5's lowest at 0 rows), and Astra on 2 of 13 (0 and 799), each `COMPLETED`, and the
`cell_accuracy` column stays at 0.999–1.000 **because it is computed on the rows they did
return** — the denominators are 9,374 to 19,198 cells against the complete arms' 20,800. The
truncated runs do carry `extraction_rows_below_ocr_estimate`, so a deployment reading processing
issues sees them; one reading status does not. **Rank by completeness, never by completion
rate, and never by an accuracy figure without its denominator.**

With that in front of it, the per-profile answer. "Cheapest arm at ceiling" means the cheapest arm
that reaches recall 1.0000 *and* cell accuracy 1.0000 on that profile's documents, on a
denominator stated in the same cell, among arms whose cost can be quoted.

| Document profile | Cheapest arm at ceiling | Mode | Measured basis | Notes |
|---|---|---|---|---|
| Small forms, ≤ 1 page, few fields | **any model, Nova Lite included** | simple | `tiny_form`: all seven `coresynth` arms 19/19 at recall 1.0000, cell accuracy 1.0000 over 190 cells (Astra 0.9737); both Nova arms 9/9 at 1.0000 / 1.0000 over 90 cells on the `simplegrid` matrix | Genuinely free. Nova Lite at $0.013/doc and the default at $0.045/doc produce the same result; pick on price |
| Statements ≤ ~100 rows / ≤ 4 pages | **the shipped default** (Sonnet 5) | simple | `small_narrow` and `longdesc_100`: Sonnet 5 at recall 1.0000, cell accuracy 1.0000 over **3,800 cells each**, $0.16 and $0.26/doc. Every Claude arm and Astra's recall are at 1.0000 here | **Not** the cheap end: Nova Lite and Nova Pro return most or all rows and get 1–17% of values wrong (§5.1). Haiku 5.5 and Sonnet 5.5 are also at 1.0000 / 1.0000 over 3,800 cells each; Haiku 5.5's cost cannot be quoted as a saving, and Sonnet 5.5 is unsafe one size up |
| Statements ~400 rows / ~9 pages | **the shipped default** | simple, or advanced | `med_narrow`, `wide_400`, `manylists_400`: Sonnet 5 19/19 each at recall 1.0000 over 15,200 cells, cell accuracy 0.9994–0.9999, $0.55–0.84/doc. Opus 5.5 is 1.0000 / 1.0000 over the same 15,200 at $0.84–1.15/doc | ⚠️ **Do not use Claude Sonnet 5.5 here** — recall 0.59–0.90, two of its short runs unreported (§5.1, [#1494](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1494)). Haiku 5.5 and Sonnet 5 `:1m` dip to 0.95–0.97 on `wide_400` |
| **Statements ~800 rows / ~17 pages** | **any model — but in `advanced` mode** | **advanced** | `large_narrow`: all seven arms 6/6 at 800/800 rows, 9,600 cells, accuracy 1.0000 in advanced; four of the seven fall short in simple | **This is the configuration decision, not a model decision.** In simple mode the default returned the whole list on 5 of 13 runs |
| 1,000+ rows / 25–66 pages | **the shipped default, advanced, at `shard_pages: default`** | **advanced** | `astracap` on 3,200 rows / 66 pages: Sonnet 5 2/2 at recall 1.0000, cell accuracy 1.0000 over 12,800 cells and full confidence coverage, for **$3.76–4.20 against Astra's $15.14–21.89** per document. §3's `scaling` series holds 1.000 / 1.000 with full coverage through the same ceiling at the control model | Simple mode **refuses** at 66 pages, and at 25–33 pages returns every row with **5–20% unscored** on this release's single draw (§3). The 25-page wide shard on Sonnet 5 now keeps 97–99% of the confidence but still costs more than the default shard (§5.2) |

**Where a premium model is actually required: nowhere in this corpus, once advanced mode is set.**
The one thing the premium end buys is a complete 800-row list in *simple* mode, where Opus 5.5,
Opus 5 and Sonnet 5.5 reach 1.0000 on 13 of 13 — and advanced mode gets all seven arms there at
9,600 cells and accuracy 1.0000. If simple mode is a hard constraint on a document that size,
**Opus 5.5 is the arm to pick**: complete and exact on every document in the grid, cheaper and
faster than Opus 5 on that document (686 s against 1,216 s), where Sonnet 5.5 is complete at 800
rows but unusable at 400.

**Over the grid as a whole the answers are two, and they differ by 63%.** **Claude Opus
5.5 is complete and exact on every document of the grid in both modes** — recall 1.0000 and cell
accuracy 1.0000 over **83,790 cells** — for **$105.95**, which is **at least 45% below GPT-6
Astra's $191.85** (a bound, because Astra's figure is a lower bound) and **63% above the shipped
default's $65.00**. The default over the
same grid reaches recall 0.9463 over 72,364 cells, all of the gap being simple mode on the 17-page
document.

So, plainly. **Keep the shipped default and turn on advanced mode for documents above ~400 rows**
is right on cost, and it is what the per-profile table recommends everywhere. **Pay 63%
more for Opus 5.5** where a complete, exactly-correct list in either mode is worth it — it is the
cheapest of the two arms in this edition that delivers one on every document in the grid, and the
other (Opus 5) costs 20% more for the same result. Nothing above those two is worth buying: the
most expensive arm in the sweep is beaten on completeness and on cell accuracy by one costing at
least 45% less.

### 5.6 Classification granularity, modality and strategy

**This is the second measurement of these settings**, the first being the v0.6.13 edition's.
`benchmarks/harness/run_classification_bench.py --variants` ran seven named combinations on the
first 20 packets of `DocSplit-Poly-Seq` (RVL-CDIP-derived, 13 classes, 298 pages, 5–20 pages per
packet), with Nova 2 Lite — the shipped classification default — on every call and `topk` class
confidence (3 candidates). One run per variant per packet, 140 document runs, on 2026-11-10.
The scores are classification scores; the pipeline runs on to extraction, which matters only
where an extraction failure takes a document out of the scored set (below).

| variant | granularity | sees | strategy |
|---|---|---|---|
| `page-both` *(the shipped default)* | page | text + images | `combined` |
| `page-text` | page | text | `combined` |
| `packet-both` | packet | text + images | `combined` |
| `packet-text` | packet | text | `combined` |
| `packet-images` | packet | images | `combined` |
| `split-then-classify-packet` | packet | text + images | `split_then_classify` |
| `split-then-classify-page` | page | text + images | `split_then_classify` |

Three metrics. **`class_accuracy`** is the share of pages given their true class.
**`boundary_accuracy`** is the share of true sections whose page range the variant reproduced,
whatever class it gave them, and **`label_accuracy`** is the share of those boundary-matched
sections that got the right class; the product `split_accuracy_without_order` is exactly
`boundary × label`. `label_accuracy` is undefined on a packet where no boundary matched, so its
document count can be lower than the other two.

**Both failure modes the v0.6.13 edition reported are gone.** There, every packet variant that
sends page images failed the same three 18–20-page packets with `Input is too long for requested
model` ([#1676](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1676)),
and `page-text` failed two packets on a page with no OCR text
([#1677](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1677)).
On v0.6.14 `packet-images` and `split-then-classify-packet` score all 20 packets, `packet-both`
scores those three long packets, and `page-text` scores both of its previous failures. That is
what this release's packet-window recovery and its `unclassified` label for a page with no text
were for.

⚠️ **One packet goes unscored on four variants, for a reason outside classification.**
`packet_0018` (12 pages) is missing from `page-both`, `page-text`, `packet-both` and
`split-then-classify-page`: on those four the document **failed in extraction**, on its first
section (a one-page `news_article`), with Bedrock answering `Output blocked by content filtering
policy`. Classification had completed, but the harness scores only documents that finish. The
three variants that drew a different first section scored it. This did not happen in the
v0.6.13 edition, where `page-both` scored all 20; the run does not establish why the filter
fired now.

So the comparison below is over the **19 packets every variant scored**; the all-documents
figures each variant scored are in the committed summary.

| variant | packets unscored (of 20) | class accuracy | boundary accuracy | label accuracy | calibration separation | classification $/page | $/document |
|---|---:|---:|---:|---:|---:|---:|---:|
| `page-both` *(default)* | 1 | 0.815 | **0.509** | **0.926** | 0.034 | $0.00097 | $0.0145 |
| `page-text` | 1 | 0.784 | 0.463 | 0.902 | **0.055** | $0.00095 | $0.0141 |
| `packet-both` | 1 | 0.747 | **0.710** | 0.869 | 0.032 | $0.00057 | $0.0085 |
| `packet-text` | 0 | 0.744 | 0.605 | 0.896 | 0.051 | $0.00036 | $0.0054 |
| `packet-images` | 0 | **0.846** | 0.600 | 0.853 | 0.039 | **$0.00028** | **$0.0042** |
| `split-then-classify-packet` | 0 | 0.723 | 0.688 | 0.854 | 0.013 | $0.00070 | $0.0105 |
| `split-then-classify-page` | 1 | 0.782 | 0.684 | 0.921 | 0.007 | $0.00097 | $0.0145 |

(accuracy columns: means over the 19 common packets. Calibration separation is
`mean(conf | right) − mean(conf | wrong)` over all pages the variant scored, and the cost columns
are over the documents whose metering read)

**What this measures, with n = 20.**

1. **Packet granularity still places boundaries better than the default, by less.** The four
   packet-level variants reproduce **0.600–0.710** of true section boundaries against **0.509**
   for `page-both`, and `split-then-classify-page` sits at 0.684. In the v0.6.13 edition the gap
   was 0.711–0.821 against 0.446. On the **14 packets both editions scored on every variant**, the
   default moved 0.421 → 0.477 and the packet variants fell — `packet-both` 0.836 → 0.757,
   `packet-text` 0.756 → 0.612, `packet-images` 0.705 → 0.565. With one run per packet a single
   packet moves a 14-packet mean by up to 0.071, and the two `packet-*` falls are about twice
   that. This release changed how packet classification forms windows (an oversized window is
   now split and retried, and a page returned twice is de-duplicated), which is a plausible cause
   for a packet-only movement; the run does not record per-window boundaries, so that is an
   inference. The direction — packet granularity bounds sections better — holds in both editions.
2. **The boundary gain is not free on labels.** Every packet variant gives the right class to
   fewer of the sections it bounds correctly (0.853–0.896, against the default's 0.926), and only
   `packet-images` matches the default on page class accuracy (0.846 against 0.815). In the
   v0.6.13 edition `packet-images` led every accuracy column; here it leads only class accuracy,
   and on the 14 shared packets its label accuracy fell from 0.971 to 0.889.
3. **Packet granularity is cheaper per page**, as the token arithmetic said: `packet-images` costs
   29% of the default's classification spend per page, `packet-text` 37% and `packet-both` 59%
   (41% in the v0.6.13 edition — the long packets it now completes are in this figure).
   `split-then-classify-packet` costs 72% of it, because it makes a splitting call and then one
   classification call per section. Classification is a small share of a document's total bill
   on the default model (the v0.6.7 study measured about 3%), so this is a saving on a small line.
4. **`split_then_classify` reports a confidence that barely separates right from wrong.** Its
   calibration separation is **0.007–0.013** at the two granularities, against 0.032–0.055 for
   every `combined` variant. A score that is nearly the same for right and wrong pages gives a
   review queue little to act on. Every figure here is small next to the 0.207 the v0.6.7 study
   measured for Haiku 4.5.

**Guidance, and its limits.** On this corpus packet granularity is the better boundary detector
and the cheaper call, and on this release the image-bearing packet variants complete packets of
18–20 pages, so packet length is no longer a reason to avoid them on Nova 2 Lite. **Keep the shipped `page-both` default if section labels
matter more than boundaries** — it has the best label accuracy in both editions. Treat every
number in this table as **direction, not ranking**: 19 packets, one run each, one model, and two
editions that disagree on the order among the packet variants by more than one packet's worth. A
second model, repeats, and a corpus with longer packets are what would turn this into a
recommendation to change the shipped default.

---

## 6. Product improvement backlog (surfaced by this study)

Items closed since earlier editions are struck through; the open ones are ordered by how much
data they can cost a customer. Each says whether this edition re-measured it.

1. **🚨 Simple mode still reports `COMPLETED` on a truncated list, and one shape of it is
   reported by nothing (P0, re-measured).** #843 made the common case *visible*:
   `extraction_rows_below_ocr_estimate` is on all eight of the shipped default's truncated
   simple-mode runs on the 17-page document (§5.5), and `extraction_list_field_not_a_list`
   catches the prose-refusal shape (§5.1). Neither makes it *terminal* by default — that is
   `extraction.row_shortfall_action: fail`, which §3 explains is opt-in for a reason. What is
   still **silent** is a list truncated below 30 rows on a document with no exact-width OCR
   table — two of Sonnet 5.5's 10-row runs on `wide_400` (§5.1) — because both row checks and
   the `extraction_row_count_unverified` report sit behind the same 30-row floor. A floor on the
   *expected* rows rather than the *returned* rows would close it.
2. **In simple mode at 25–33 pages the confidence pass still leaves rows unscored (P1,
   re-measured, much smaller).** §3 measures **5% unscored at 1,200 rows and 20% at 1,600** on
   one draw each, against 62–81% in the v0.6.13 edition, reported as `assessment_incomplete`. An
   unscored row raises no confidence alert, so a review queue fed by confidence alerts sees none
   of them. Advanced mode has no such gap at any size measured, and its confidence pass costs
   $0.04–0.08 where the simple-mode one costs $0.67–1.34 at those sizes; routing a simple-mode
   section of that size to advanced mode would close it.
3. ~~**The `AssessmentStep` memory failure on large documents (#1678).**~~ **Not observed on
   this release**: every run of the seven-arm extraction grid completed, including all 91
   simple-mode runs on the 17-page document, against 2 `Runtime.OutOfMemory` failures there in
   the v0.6.13 edition. This release's changelog carries the fix; one edition of zero is a
   measurement of absence, not a proof.
4. ~~**Packet-level classification with images fails 18–20-page packets on Nova 2 Lite
   (#1676).**~~ **Closed and measured**: all three image-bearing packet variants score those
   packets on this release (§5.6).
5. ~~**Text-only page classification fails the whole document on a page with no OCR text
   (#1677).**~~ **Closed and measured**: `page-text` scores both packets it failed before
   (§5.6).
6. **Packet-level boundary accuracy fell between the two editions that measured it (P2, new,
   unconfirmed).** On the 14 DocSplit packets both editions scored, `packet-text` fell from 0.756
   to 0.612 and `packet-images` from 0.705 to 0.565, about twice what one packet can move a
   mean, while the page-level default rose (§5.6). One run per packet in each edition; repeats
   would settle whether this is the packet-window change in this release or draw-to-draw
   variation.
7. **A document can fail in extraction on a content-filter refusal (P2, new).** One DocSplit
   packet failed on four of seven classification variants because the extraction model's
   response to its one-page news-article section was `Output blocked by content filtering
   policy` (§5.6). The section is marked `extraction_failed`, so it is not silent; whether such
   a refusal should fail the whole document is the product question.
8. **`sectionSplitting: disabled` on a packet loses the second document's rows, not just its
   boundary (P2, re-measured).** Merging two statements into one section is the known,
   by-construction cost of `disabled`; the extraction of that merged section also returns only
   the first statement's 20 rows of 40 on 7 of 10 runs on v0.6.14, 8 of 10 on v0.6.13, and on
   4 of 5 in the committed v0.6.8 grid of the same suite, so this is a standing behaviour rather
   than a regression (§7). Every merged run carries `extraction_multi_instance_suspected`, and
   the short ones `extraction_rows_below_ocr_estimate` as well, so it is reported — but as a
   warning on a run that completes.
9. **GPT-6 Astra returns a full-looking result whose every row key is unusable on a 26-page
   document (P2, re-measured).** 10 of 10 simple-mode runs on `dense_250` across both endpoints,
   in both editions: 0 of 250 rows match a truth key, while the run carries 757 confidence
   leaves, coverage 1.000 and mean confidence 0.99. Its one processing issue,
   `extraction_row_count_unverified`, also appears on every complete Sonnet 5 run of the same
   document, so it does not distinguish the two (§5.2). Whether the keys are a response-format
   mismatch or a model behaviour on long inputs is not established; what is clear is that the
   failure needs to be *detectable*.
10. **The 25-page "wide" agentic shard costs more than the default shard on Sonnet 5 (P3,
    re-measured, smaller).** On v0.6.14 it returns every row and scores 97–99% of them, against
    22–78% in the v0.6.13 edition, at $5.14–6.32 against the default shard's $3.76–4.20 (§5.2).
    On Astra the same setting is the cheaper Astra configuration, so the `max_pages_per_shard`
    guidance should be per model.
11. **The agent's turn count is not visible in the metering, so agentic cost is a range a
    customer cannot explain (P2, re-measured).** Advanced cells run at CV **6.5–22.9%** at the
    shipped default and up to **26.7%** at the control, against simple mode's 0.1% and 0.5% on
    the same document (§4). The widest single cell at the shipped default ranged **$0.624 →
    $1.092 across five identical runs** of one 400-row, one-section document. Bounding it, or
    surfacing it, would make an agentic budget explainable instead of empirical.
12. **Nova Lite cannot run the agentic path —
    [#895](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/895)
    (not re-measured).** The issue is closed; both Nova models ran only the simple-mode
    `simplegrid` matrix in this edition, so nothing here tests the agentic path on them.
13. **A class wrapped with `x-aws-idp-multi-instance` whose instances hold a long list could not
    finish confidence assessment —
    [#894](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/894)
    (closed; not re-measured).** The `multiinstance` suite did not run in this edition, so §7's
    figures for it are v0.6.8's and the fix has no benchmark measurement yet.
14. ~~**The TestRunner rejected any compressed configuration carrying a float (#892).**~~
    **Closed by #893** (`parse_float=Decimal`).
15. ~~**`sectionSplitting: llm_determined` over-split single documents (#726).**~~ **Closed by
    #817**: 133 of 133 single-section documents in §2 at one section, 1 of 1 at every size in
    §3, 5/5 on the unpaginated statement in §7's boundary A/B. The reprinted running-header shape
    is still split by the generic prompt (0/5 at both prompts, §7); #750 was closed by a
    per-class `BOUNDARY:` description in the one configuration it was reported on, not by a
    prompt change.
16. ~~**Forced tool use serialised group attributes to JSON strings on Sonnet 5 (#783).**~~
    **Closed by #794**: valid rate 1.000 on every forced section in §2 and §7.
17. ~~**Integrated confidence + simple extraction returned partial lists.**~~ **Closed by #795**:
    1.000 on 7 of 7 documents and 4 of 4 repeats (§2.1).
18. ~~**The bedrock_llm backend appeared to lose rows (#1500).**~~ **Closed** as a scorer defect:
    with identifiers repaired that cell is at recall 1.000 (§2, finding 4). Its real defect — wrong
    values, 0.990 cell accuracy and 10 of 14 scalar fields — is unchanged.
19. **Reference-corpus cells in the standard release run.** This edition ran them (§2c) at the
    control model only. A sampled variant (5 documents per corpus) at the shipped default would
    restore the two-model comparison in every release audit at a fraction of the price.

---

## 7. Individual configuration options, A/B tested

Each pair below differs on exactly one config key, on the same deployed stack with identical
code, `repeats: 3` or more. Extraction model Sonnet 4.6 (the matrix's control) unless stated;
the point of each is the *delta*, not the absolute. Each subsection says whether it was
re-measured on v0.6.14 (2026-11-09 and 2026-11-10) or carries an earlier edition's figures.

### `extraction.validation` and `extraction.coercion`

**Re-measured on v0.6.14 for bank statements; the `kv_form` rows are carried from v0.6.8**,
because no `kv_form` document has run since.

| arm | corpus | n | recall | cell acc | typed acc | coercions | validation errors | valid rate | cost/doc |
|---|---|---|---|---|---|---|---|---|---|
| `enforce-off` | bank statements (`small_narrow`, `valuenoise_100`) | 6 | 1.000 | 1.000 | — | not recorded | n/a (off) | n/a | $0.1450 |
| `enforce-warn` *(default)* | same | 6 | 1.000 | 1.000 | — | not recorded | **0** | **1.000** | $0.1450 |
| `enforce-warn` | `valuenoise_100` (`enforcecost` suite) | 5 | 1.000 | 1.000 | — | not recorded | 0 | 1.000 | $0.1453 |
| `enforce-escalate` | same | 5 | 1.000 | 1.000 | — | not recorded | 0 | 1.000 | $0.1454 |
| `enforce-off` | `kv_form` (v0.6.8) | 3 | — | — | 1.000 | 0 | n/a | n/a | $0.030 |
| `enforce-warn` | `kv_form` (v0.6.8) | 3 | — | — | 1.000 | 0 | 0 | 1.000 | $0.028 |

**Guidance: leave `warn` on.** It costs nothing ($0.1450 against $0.1450) and, with one section
per document, produces **zero** spurious errors. `escalate` costs nothing either on this corpus
($0.1454 against $0.1453), because on a clean document validation never fails and nothing is
re-extracted; its price is paid only when validation fails, which RealKIE (§2c, v0.6.8) shows
real forms do. The coercion count is not recorded on the bank-statement rows, so this table
says nothing about how often coercion repairs a value on that corpus; the v0.6.8 `kv_form`
measurement recorded 0 at either setting.

### `extraction.forced_tool.enabled`

**Re-measured on v0.6.14** (`forcing`, bank statements; the `kv_form` rows are not re-run).

⚠️ **Claude Opus 5.5 and Claude Sonnet 5.5 reject a forced `toolChoice`, so this axis has no
measurement for either.** Bedrock refuses `tool_choice` of type `tool` or `any` for both models.
Their `force-on` cells still reach `COMPLETED` — they run with `toolChoice: auto` — so the
completion count is not a result on this axis; the harness records `forced_tool_attempted = 0`
and the refusal in `forced_tool_skips` (0 of 7 attempts on each model's grid).

Every other arm **honoured forcing at rate 1.00**: `core` at 76 of 76 attempts, synthetic and
reference documents together, and every other sweep arm at 7 of 7. That is what makes the
neutrality finding below a measurement rather than an absence — without a confirmed honour
rate, "no effect" and "never applied" produce the same numbers.

| arm | corpus | n | honoured | recall | cell acc | valid rate | cost/doc |
|---|---|---|---|---|---|---|---|
| `force-off` *(default)* | bank statements (`longdesc_100`, `valuenoise_100`) | 6 | — | 1.000 | 1.000 | 1.000 | $0.1756 |
| `force-on` | same | 6 | **6/6** | 1.000 | 1.000 | **1.000** | $0.1770 |

**Guidance: accuracy-neutral and cost-neutral (+0.8%), honoured on every call, and
schema-valid.** Enable it for the structural guarantee (a malformed-JSON parse failure becomes
impossible for declared fields) if your corpus produces parse failures; do not enable it for
accuracy. One caution, the same as the last two editions': on the 800-row, 17-page document in
the **Sonnet 5** grid (§5.1) *both* arms truncate, and the forced one truncates less —
`force-on` returned **190 of 800** against `force-off`'s **92 of 800**, each `COMPLETED` and each
carrying `extraction_rows_below_ocr_estimate`. At the **control** model (§2) both arms return
all 800 rows. Neither arm is safe at that size in simple mode.

### `classification.document_shape.sectionSplitting` and the boundary prompt

**Re-measured on v0.6.14** (`boundaryab`, 60 runs, and `boundary`, 30 runs, which repeats three
of its cells). Boundary detection judged on `sections_correct` (1.0/0.0 per run; the mean over
5 repeats is the pass rate). Classification model Nova 2 Lite (the shipped default).

| cell | one 3-page statement (unpaginated) | same, paginated | reprinted running header | two statements in one file |
|---|---|---|---|---|
| `split-llm` *(current prompt, default)* | **1.00** (1,1,1,1,1) | 1.00 | **0.00** (3,3,3,3,3) | **1.00** (2,2,2,2,2) |
| `split-llm-v067prompt` *(control: the v0.6.7 prompt)* | **1.00** (1,1,1,1,1) | 1.00 | 0.00 (3,3,3,3,3) | 1.00 |
| `split-disabled` | 1.00 | 1.00 | 1.00 | **0.00** (1,1,1,1,1 — merged), **recall 0.60** |

The `boundary` suite's repeat of `split-llm` and `split-disabled` on three of these documents
agrees on every boundary: 5 of 5 correct for `split-llm` on each, and 5 of 5 merged for
`split-disabled` on the two-statement file.

Three results:

- ⚠️ **The prompt A/B did not discriminate on this draw.** The unpaginated statement went from
  2/5 correct under the v0.6.7 prompt to 5/5 under the current one in the v0.6.13 edition; on
  v0.6.14 both prompts get it right 5 of 5. Same stack, same model, prompt text the only
  difference, so the control arm's 2/5 there and 5/5 here are two draws of the same
  configuration. What both editions agree on is that the **current** prompt keeps the
  unpaginated statement whole (10 of 10 runs), which is the shape #817 targeted and the reason
  §2 produces one section on every run.
- **The reprinted running header is still split into three, under both prompts.** A document
  whose every page re-prints the title-and-account block is split into three sections, 5 of 5.
  This is the remaining over-split shape of the generic prompt, and `sectionSplitting: disabled`
  is not the answer to it (next point).
- ⚠️ **`disabled` is wrong by construction on a packet, and it loses rows there, not only the
  boundary.** Two statements in one file become one section on 10 of 10 runs across the two
  suites — and on **7 of the 10** only the first statement's 20 rows of 40 come back (recall
  0.500; 4 of 5 in `boundaryab`, 3 of 5 in `boundary`). Every merged run carries
  `extraction_multi_instance_suspected`, and the seven short ones also
  `extraction_rows_below_ocr_estimate`, so the loss is reported as a warning on a run that
  completes. That is not new in this release: the v0.6.13 edition measured 8 of 10 and the
  committed v0.6.8 `boundaryab` grid 4 of 5. `split-llm` splits the file correctly and returns
  all 40 on 10 of 10. On a single-class corpus of single documents `disabled` is harmless and
  buys nothing (§2b: $0.5505 against $0.5562).

**Guidance: keep `llm_determined` and the shipped prompt.** If your corpus has a reprinted running
header on every page, expect 3 sections per document from the generic prompt; a class-level
`BOUNDARY:` sentence in the class description is the measured fix for that shape (#750), and
advanced mode (which rejoins the section before validation) is the fallback. Do not turn
splitting off on a corpus that can contain more than one document per file.

**Two values this cell does not cover.** `sectionSplitting` has a third value, `page`, which
asserts one document per page; it is not in this matrix. Like `disabled` it is an assertion
rather than a preference, and it is the right answer only for a corpus of single-page documents.
Granularity is a separate axis from this cell and is measured in §5.6 on a labelled packet
corpus, where `packet` granularity placed boundaries correctly more often than `page`; see
also [Classification](../classification.md#page-versus-packet-granularity-opposite-failure-modes).

### `x-aws-idp-multi-instance` and `extraction.multi_instance_detection`

**Not re-measured on v0.6.14** — the `multiinstance` and `midetect` suites did not run, so the
table and guidance below are the v0.6.8 measurement. #894, the 🚨 row, has since been closed and
has no benchmark measurement of the fix yet.

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
and migrate baselines — **but on a class that carries a list of more than a few dozen rows,
measure it on your own documents first**: #894 is closed, and no benchmark has yet measured the
fix on this shape. Use
`x-aws-idp-instance-array` when the records are already inside a declared array.

### `extraction.confidence.list_batch_size`

**Not re-measured on v0.6.14** — the `sizerab` suite did not run, so the figures below are the
v0.6.8 measurement. v0.6.13 changed how list batches are sized (leaf by leaf rather than by
top-level column count), so treat the table as the before picture for that change.

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

### `extraction.agentic.restate_schema_in_system_prompt`

From the §2 grid (advanced, Sonnet 4.6 control, 7 documents, re-measured on v0.6.14):
`restate-on` $0.6136, `restate-off` $0.6343, recall and cell accuracy 1.000 both.

A dedicated 50-run A/B on Sonnet 4.6 at v0.6.10 measured what the difference is. Details and the
full tables are in [studies/schema-restatement-tokens.md](studies/schema-restatement-tokens.md);
the decision-relevant numbers, measured 2026-09-20 on v0.6.10 with 25 runs per arm across three
synthetic documents (not re-measured on v0.6.14):

| | Result | Significance |
|---|---|---|
| Quality | `completeness_recall`, `cell_accuracy` and `scalar_accuracy` **1.000 in all 50 runs**, both arms, zero failures, identical `cells_compared` | both arms at the ceiling; a degradation affecting fewer than ~1 run in 10 is not excluded |
| Input-side tokens | **−5.2%** on `manylists_400` (175,912 → 166,731) and **−6.7%** on `longdesc_100` | *p* = 1.3 × 10⁻⁸ and *p* = 0.008 (distributions completely separated) |
| Input-side tokens, third document | **+9.5%** on `valuenoise_100` — the opposite direction | *p* = 0.22, **not significant**; within-arm spread on that document is 20%, so the arm effect is swamped |
| Extraction **output** tokens | **+17.4%** on `manylists_400` (13,501 → 15,849), **+14.1%** on `valuenoise_100` | *p* = 1.2 × 10⁻⁵ and *p* = 0.016 |
| Extraction cost | **+7.5%** on `manylists_400`; +8.2% and +2.6% on the others | **not significant** on any document (*p* ≥ 0.13) |
| Total cost | **+5.3%**, +6.6%, +1.8% | **not significant** |

**Turn it off for shard headroom, not for dollars.** The input saving is genuine but cheap: it is
almost entirely **cache reads**, so 9,181 fewer input-side tokens is worth about **$0.007** —
while 2,347 more output tokens cost about **$0.039**. Those two figures reconcile the measured
extraction-cost difference (+$0.0319/doc) to the cent. ⚠️ **The release grids do not settle
the sign.** At one draw per document they put `restate-off` *above* `restate-on` on this release
($0.6343 against $0.6136, +3.4%) and *below* it in the two before — $0.6164 against $0.6188 at
v0.6.13, $0.6142 against $0.6292 at v0.6.12 — three differences of 0.4–3.4% in alternating
directions, all inside the agentic spread §4 measures. So the cost claim rests on the dedicated
A/B alone, which itself does not reach significance on cost. Since #775 the reclaimed tokens are
real shard budget, so headroom is the reason to use this knob; whether it moves a shard count
still depends on the document sitting near a boundary.

⚠️ **The output-token increase is the part to watch, and it is bimodal rather than
uniform.** On `manylists_400` every `restate-on` run emitted 13,406–13,590 output
tokens, while five of fifteen `restate-off` runs emitted 20,201–20,368 — a mode the
`on` arm never entered (Fisher *p* = 0.042). The remaining ten sat only 100–250 tokens
above the `on` arm. So the risk of turning it off is not a slightly chattier agent
every time; it is a roughly one-in-three chance of a run that emits half again as much.
Measured on one model and one document shape.

### `classification.combined.llm.model` — see §5.3

Re-measured on the full grid: Nova 2 Lite, Haiku 5.5 and Sonnet 5 are compared in §5.3, and the
default stays. The running-header shape is the one boundary case still open in the generic
prompt, and it is not model-dependent.

### `extraction.confidence.model` — see §5.4

Nova Lite (default), Nova 2 Lite and Sonnet 5 as the confidence model, on the same grid and on
the one cell with wrong values to be found. Short version: the default is right unless your
review queue depends on the score catching wrong values, in which case only Sonnet 5 lowered its
score on them — at several times the assessment cost.

---

## Appendix A — Data & reproduction

Every number re-measured in this edition is in the working tree under
**`benchmarks/results/v0.6.14/`** — 32 result sets, named `<suite>` or
`<suite>__<axis>-model-<value>`, where the suffix is the `--set` override the suite ran with,
spelled the way `make_configs.py`'s `override_slug` spells it (hyphens for underscores, so
`sonnet5_1m` becomes `sonnet5-1m`). Sections marked *carried over* in the scope table at the top
of this page cite an earlier release's directory instead, which is in git history. Each
pipeline set holds `summary.json` (per-run rows plus a `meta` block recording the stack, commit
and `pricing.yaml` hash), `summary.csv` and `cell_stats.csv`; `clsvariants/` holds the
classification driver's `summary.json`, keyed by variant with a `per_doc` list.

| Section | Directory (under `benchmarks/results/v0.6.14/`) | Suite / overrides |
|---|---|---|
| §2, §2b | `core/` — synthetic slice | `core` at the control model, 133 synthetic runs with exact ground truth. The feature-option arms of §2b are cells of this same grid (`enforce-*`, `force-*`, `restate-*`, `split-*`) |
| §2c | `core/` — reference slice | the same run's two reference corpora, 760 documents, all scored. **ONE arm only:** the control model |
| §2.1, §6 | `intconf/`, `advverify__extraction-model-sonnet5/` | 8 runs each (suite default, 4 repeats per arm); both standing hazards closed, 8/8 |
| §3 | `scaling/` | 14 runs, 25 → 3,200 rows, **1 repeat** (suite default), at the control model |
| §4 | `cost/`, `cost__extraction-model-sonnet5/` | 5 repeats × 5 cells at the control and at the shipped default |
| §5.1 — the seven-arm grid | `coresynth__extraction-model-sonnet5/`, `…-sonnet5-1m/`, `…-sonnet55/`, `…-haiku55/`, `…-opus5/`, `…-opus55/`, `…-astra/` | `coresynth --set extraction_model=<m>`, 133 runs each |
| §5.1 — the two Nova arms | `simplegrid/`, `simplegrid__extraction-model-nova-lite/`, `simplegrid__extraction-model-nova-pro/` | 63 runs each; `simplegrid/` is their Sonnet 4.6 control |
| §5.2 | `opus55value/`, `astravalue/`, `astracap/` | 40 + 100 + 12 runs (`astracap` at its default 2 repeats) |
| §5.3 | `coresynth__classification-model-nova-2-lite/`, `…-haiku55/`, `…-sonnet5/` | `coresynth --set classification_model=<m>`, 133 runs each, default included as an explicit arm |
| §5.4 | `coresynth__confidence-model-nova-lite/`, `…-nova-2-lite/`, `…-sonnet5/` | `coresynth --set confidence_model=<m>`, 133 runs each, default included as an explicit arm |
| §5.6 | `clsvariants/` | `run_classification_bench.py --testset docsplit --n 20 --models nova2lite --mode topk --variants <all seven>`, 140 document runs |
| §7 | `enforcement/`, `enforcecost/`, `forcing/`, `boundaryab/`, `boundary/`, and the `restate-on`/`restate-off` cells of `core/` | the knobs re-measured on v0.6.14. **`splitcost`, `advsplitcost`, `multiinstance` and `sizerab` did not run**, so those subsections carry earlier editions' figures, say so in place, and have **no** v0.6.14 directory |
| release A/B | `corefast/`, and `benchmarks/results/v0.6.13-rvprev/corefast/` | the two sides of [releases/v0.6.14.md](releases/v0.6.14.md); no section of this page cites them |

- The restatement axis of §7 has a second, dedicated source: it was re-measured on its own grid
  at v0.6.10 (`benchmarks/results/v0.6.10/restate710*/`, Sonnet 4.6, 25 runs per arm) and written
  up in [studies/schema-restatement-tokens.md](studies/schema-restatement-tokens.md).
- Earlier releases' slices are pruned per
  [`RETENTION.md`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/benchmarks/results/RETENTION.md);
  restore the v0.6.8 set the carried-over blocks cite with
  `git checkout <pre-v0.6.9-audit sha> -- benchmarks/results/v0.6.8/`.
- Corpus manifest + generators: `benchmarks/corpus/` (regenerable; PDFs/configs gitignored).
  Matrices + methodology: `benchmarks/matrices/`.
- **Scale of this edition:** **3,338 scored rows across 31 pipeline result sets** — 2,578
  synthetic runs and 760 reference-corpus documents, $1,970.57 by the rows' own cost — plus
  `clsvariants/`. 5 rows did not complete (0.15%), all of them the expected simple-mode refusal
  on the 66-page document (one in `scaling`, four in `astracap`). The v0.6.13 edition had 15 of
  3,368 (0.45%), of which nine were the same refusal at three repeats instead of one and two,
  four were reference-corpus `SCORE_ERROR`s and two were #1678 memory kills.
- One stack: `IDPUpg0613to0614` at v0.6.14, created from the published 0.6.13 template and
  upgraded in place to the published `idp-main_0.6.14.yaml`; every pipeline set's `meta` records
  the same commit (the `v0.6.14` tag) and the same `pricing.yaml` hash. **Two sessions.** The
  release A/B and nine suites ran on 2026-11-07 (`corefast`, `core`, `astravalue`, `astracap`,
  `opus55value`, and the `sonnet5-1m`, `opus5`, `opus55`, `astra` extraction arms and
  `simplegrid__extraction-model-nova-lite`); a host restart at 06:03 UTC killed the six lanes then
  in flight, whose partial runs were discarded, not scored. Those six and the fifteen suites not
  yet started ran in full from 2026-11-09 20:56 UTC, and `clsvariants` on 2026-11-10. Up to six
  suites ran concurrently on the stack (`--max-inflight 4` each), so wall-clock figures carry
  load from the other lanes; cost and accuracy do not.
- Every pipeline suite ran with `--native-upload --validate-against-release v0.6.13`. Both sides
  of the release A/B are config format 0.9 or later, so no flat classification keys were written.

```bash
source .venv/bin/activate && export PYTHONPATH=$PWD/lib/idp_common_pkg
python3 benchmarks/harness/gen_corpus.py
M=benchmarks/harness/make_configs.py
R="python3 benchmarks/harness/run_matrix.py --stack <S> --native-upload --validate-against-release v0.6.13 --max-inflight 4"

# §2 grid, §2b feature arms, §2c real corpora, §7 restatement arms — at the CONTROL model,
# one suite: the three classes are the synthetic corpus and the two reference test sets
for c in bank_statement realkie ocr_bench; do python3 $M --suite core --class $c; done; $R --suite core
# §3 scaling, §4 cost, §2.1 + §6 hazards — every suite at its committed default repeats in this edition
python3 $M --suite cost --class bank_statement; $R --suite cost
for s in scaling intconf; do python3 $M --suite $s --class bank_statement; $R --suite $s; done
python3 $M --suite cost --class bank_statement --set extraction_model=sonnet5; $R --suite cost --set extraction_model=sonnet5
python3 $M --suite advverify --class bank_statement --set extraction_model=sonnet5; $R --suite advverify --set extraction_model=sonnet5
# §5.1 extraction sweep — seven arms on coresynth, two Nova arms plus their control on simplegrid
for m in sonnet5 sonnet5_1m sonnet55 haiku55 opus5 opus55 astra; do python3 $M --suite coresynth --class bank_statement --set extraction_model=$m; $R --suite coresynth --set extraction_model=$m; done
python3 $M --suite simplegrid --class bank_statement; $R --suite simplegrid
for m in nova_lite nova_pro; do python3 $M --suite simplegrid --class bank_statement --set extraction_model=$m; $R --suite simplegrid --set extraction_model=$m; done
# §5.3 classification sweep, §5.4 confidence sweep — default is an explicit arm in both
for m in nova_2_lite haiku55 sonnet5; do python3 $M --suite coresynth --class bank_statement --set classification_model=$m; $R --suite coresynth --set classification_model=$m; done
for m in nova_lite nova_2_lite sonnet5; do python3 $M --suite coresynth --class bank_statement --set confidence_model=$m; $R --suite coresynth --set confidence_model=$m; done
# §5.2 premium studies
for s in opus55value astravalue astracap; do python3 $M --suite $s --class bank_statement; $R --suite $s; done
# §5.6 classification variants (a different driver, a labelled packet corpus)
python3 benchmarks/harness/run_classification_bench.py --stack <S> --testset docsplit --n 20 \
    --models nova2lite --mode topk \
    --variants page-both,page-text,packet-both,packet-text,packet-images,split-then-classify-packet,split-then-classify-page
# §7 feature A/Bs
for s in enforcement forcing enforcecost boundaryab boundary; do python3 $M --suite $s --class bank_statement; $R --suite $s; done
# score: python3 benchmarks/harness/aggregate.py --run benchmarks/results/run-<stamp> --out benchmarks/results/v0.6.14/<suite>[__<slug>]
```
