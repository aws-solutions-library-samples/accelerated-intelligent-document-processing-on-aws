---
title: "Configuration Guidance"
---

> **This is the evergreen "which configuration should I pick?" paper** — a cross-config
> comparison at the current release. For **release-over-release** comparisons (is the
> upgrade safe / cheaper / faster?), see the [Release Benchmark Audit Trail](releases/).

# GenAIIDP Configuration Guidance — Empirical Guidance for Document Extraction at Scale

**Release:** v0.6.13 · **Region:** us-west-2 · **Stack:** `IDPUpg0612to0613` (created from the
published `idp-main_0.6.12.yaml` and upgraded in place to the published `idp-main_0.6.13.yaml`;
the v0.6.13 record in [release-validation](../release-validation/) carries the stack's
provenance)
**Models:** extraction Claude Sonnet 4.6 — **the cross-version control, not the shipped
default** — in §2–§4 and §7; the extraction, classification and confidence models are
*varied* in §5 · classification Nova 2 Lite (the shipped default) · confidence Nova Lite
(the shipped default) · summarization disabled (unscored)
**Pricing:** `config_library/pricing.yaml` (sha256 `44a79aa06c15…`, unchanged since the
v0.6.12 edition; rates as of 2026-10; intro pricing may apply)
**Measured:** 2026-10-31, 04:17 → 12:03 UTC (run launch times), in one session, on one
stack — **3,368 scored rows across 31 result sets** (2,608 synthetic runs and 760
reference-corpus documents, $2,077.09 by the rows' own cost) plus a classification-variant
benchmark on a 20-packet labelled corpus, which is new in this edition.

⚠️ **Four models' cost figures are not measurements of what the price list charges, in two
opposite directions.** `pricing.yaml` holds one flat rate per model, and that is wrong for
four of the swept models:

- **Claude Haiku 5.5** is billed 5× on every token type above 100,000 input tokens, and
  that threshold is crossed in practice here. Its figures are **lower bounds**.
- **GPT-6 Astra** moves to a long-context band above 272,000 input tokens. Its figures are
  **lower bounds**.
- **Claude Sonnet 5 and Sonnet 5 `:1m`**, the shipped default and its 1M twin, are priced
  in `pricing.yaml` at Sonnet 4.6's rates, which is **1.5× the published Sonnet 5 price on
  every token type** ($3.30/$16.50 per MTok against $2.20/$11.00, `us.`/`eu.`). The file
  says so and leaves it for a separate change. So the Bedrock-token part of every Sonnet 5
  figure is **overstated**, and those figures are **upper bounds**.
- **Claude Sonnet 5.5** is priced at its published rates, which are identical to Sonnet
  5's on input and output ($2.20/$11.00) and half on cache reads. Against the inflated
  Sonnet 5 rows a report therefore shows Sonnet 5.5 as about a third cheaper than Sonnet 5
  when the price list says they are nearly the same. Compare those two from the AWS price
  list, never from here.

Where this page compares one of these models on cost it states two dollar figures from
the same grid and a **one-sided bound** over them ("at least X% less") in the direction the
bias allows, which is what a bound supports and a ratio is not. Every other arm has a single
flat rate and its cost figure is a true measurement. See each model's row in
[Benchmarking](index.md#which-models-are-actually-measured).

### Scope of this edition

Every number below is either re-measured on v0.6.13 or explicitly marked as carried over. Read
this table before citing a figure.

| Section | On v0.6.13? | Data |
|---|---|---|
| §2 configuration matrix (19 cells × 7 synthetic docs) | ✅ re-measured | `core` synthetic slice (133 runs, 0 failures, exact ground truth), at the **control** model — that suite carries no model override, so §2 is a cross-*configuration* result and the shipped default's own grid is §5.1's. The Bedrock-LLM cell's completeness and cell accuracy are **published again**: the harness now canonicalises the row identifier that backend perturbs ([#1500](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1500)), so those figures are measurements on this release |
| §2c real-corpus accuracy | ✅ **re-measured, ONE arm** | `core` reference slice — **756 scored rows over 760 documents** across both corpora, at the **control** model. ⚠️ The *two-model* comparison the v0.6.8 edition published is **not** refreshed: only the control arm ran, so a Sonnet-5 side would be a cross-release figure. Treat §2c as a cross-configuration result on this release, not a model comparison |
| §2.1 integrated + simple hazard | ✅ **re-measured, one model per mode** | `intconf` (6 runs, **control** model) — **closed**, 6/6 complete — and `advverify__extraction-model-sonnet5` (6 runs, shipped default) for the advanced-mode counterpart. ⚠️ No Sonnet 5 `intconf` arm ran, so the simple-mode half is a **one-model** result |
| §3 scaling | ✅ **re-measured, 3 repeats, control only** | `scaling` (42 runs, 25 → 3,200 rows, **3 draws** per mode per size), at the **control** model. Sonnet 5 at the 3,200-row point is §5.2's `astracap` |
| §4 cost level and variance | ✅ re-measured | `cost` (control) and `cost__extraction-model-sonnet5`, n=5 each, 25 runs each, all 50 complete at recall 1.000 |
| §5.1 extraction-model sweep | ✅ **all 9 models** | `coresynth__extraction-model-*` for `sonnet5`, `sonnet5-1m`, `opus5`, `opus55`, `astra`, `haiku55`, `sonnet55` (133 runs each); `simplegrid__extraction-model-*` for `nova-lite` and `nova-pro` (63 each — neither finishes the agentic path, #895), with `simplegrid` as their comparable control |
| §5.2 premium head-to-head | ✅ **all three pairs** | `opus55value` (40 runs), `astravalue` (100), `astracap` (18 — **3 repeats** per cell this edition, against 2 before) |
| §5.3 classification model | ✅ **all 3** | `nova_2_lite` (default), `sonnet5`, `haiku55`, 133 runs each |
| §5.4 confidence model | ✅ **all 3** | `nova_lite` (default), `nova_2_lite`, `sonnet5`, 133 runs each |
| §5.6 classification granularity, modality and strategy | ✅ **new — first measurement** | `clsvariants`: 7 variants × 20 DocSplit packets, Nova 2 Lite, `topk` class confidence. **n = 20 documents**; 15 scored by every variant |
| §6 standing hazards | ✅ re-measured | `intconf`, `advverify__extraction-model-sonnet5` — **both closed**, 6/6 each |
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
> for every section is in the working tree under `benchmarks/results/v0.6.13/` — see
> Appendix A for the exact directory per section. The one exception is the
> `restate_schema_in_system_prompt` axis in §7, which was also re-measured on its own grid
> at v0.6.10 (`benchmarks/results/v0.6.10/restate710*/`, Sonnet 4.6, 25 runs per arm) and is
> written up in [studies/schema-restatement-tokens.md](studies/schema-restatement-tokens.md);
> its rows carry that date. Per
> [`benchmarks/results/RETENTION.md`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/benchmarks/results/RETENTION.md)
> only one complete set is retained per release, so the v0.6.8 slices the carried-over blocks
> cite are in git history (`git checkout <sha> -- benchmarks/results/v0.6.8/`).
>
> **What changed in the measurement itself since the v0.6.12 edition.** One thing, and it
> matters for the Bedrock-LLM OCR cell only: the scorer now repairs a row identifier that
> differs from a truth id by a duplicated digit and records how many it repaired in
> `rows_id_repaired` (#1500). On that backend 42–45 identifiers per 400- or 800-row document
> are repaired, so its recall and cell accuracy now measure the rows rather than the
> identifier corruption. Every other cell repairs none, so its figures are like-for-like with
> the v0.6.12 edition. Cost is unaffected: the harness prices the raw metering map itself
> with exact key matching from the `pricing.yaml` named above.

## Abstract

We benchmark the GenAI IDP accelerator across a controlled matrix of **configuration
options** (OCR backend, extraction mode, assessment mode, the v0.7 feature arms, the
extraction, classification and confidence **models**, and — new in this edition — the
classification **granularity, modality and strategy**) and **document types and sizes**
(synthetic documents with exact ground truth, plus three real labelled corpora). We quantify
seven dimensions per configuration: success/failure, list completeness, per-row field
accuracy, confidence calibration, latency, token use, and cost.

Headline results at v0.6.13:

1. **Both Opus models are now at ceiling on the whole extraction grid, and Opus 5.5 is the
   cheaper of the two.** On the 19-cell × 7-document grid Claude Opus 5.5 and Claude Opus 5
   each completed **133 of 133 runs at recall 1.0000 and cell accuracy 1.0000 over 83,790
   `cells_compared`**, the grid's full denominator — for **$107.67 against $128.52, 16% less**
   for the same result, a ratio that can be quoted because both are flat-rate. Opus 5.5 is
   also the faster of the two (244 s against 270 s per document). GPT-6 Astra, the most
   expensive arm, is at recall 0.9925 and the lowest cell accuracy of the grid, 0.9735 over
   82,188 cells, for $188.81, which is a lower bound — so Opus 5.5 is **at least 43% less**
   than Astra and more accurate (§5.1, §5.5).
2. **The shipped default truncates the 17-page document in simple mode more often than it
   did, and still reports success.** Sonnet 5's grid recall is **0.9307 over 69,042 cells**,
   the second-lowest of the seven arms, and all of the shortfall is one document: on the
   800-row `large_narrow` in simple mode it returned all 800 rows on **3 of 13 runs** and
   43 to 141 rows on the other 10, each `COMPLETED` (v0.6.12: a mean of ~454 of 800). In
   advanced mode the same model returned 800 of 800 on 6 of 6. The simple-mode behaviour is
   bimodal at this size — §5.2's `astravalue` measures the same model and document at 4
   complete runs of 5 — and this grid has one draw per cell, so read the movement from
   v0.6.12 as variation between draws rather than as a release effect. The rule from the
   last two editions holds and is stronger: **use advanced mode above ~400 rows** (§5.5,
   §3).
3. **On a real labelled corpus simple mode still beats advanced at about half the cost.** On
   RealKIE-FCC-Verified (20 documents, all 19 cells, scored by the product's own evaluation)
   eleven simple-mode cells score **0.803–0.811** weighted accuracy and **five of the six
   advanced cells fall below every one of them**, at 0.775–0.786. The matched pair costs
   **$0.1437 simple against $0.3287 advanced**. The sixth advanced cell, `core-tt-adv-int`,
   reaches the simple band at 0.805 for $0.4911 per document. These rows carry no
   `cells_compared`: a reference document's truth lives on the stack, so the metric is
   `weighted_accuracy` over 20 documents per cell (§2c).
4. **🚨 Claude Sonnet 5.5 still loses most of a 400-row list**
   ([#1494](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1494)).
   16 of its 133 grid runs return between 0 and 162 rows of 400 — twelve return exactly 10 —
   and every one is a 400-row document. Per-document recall is **0.6900 on `med_narrow`
   (10,480 cells), 0.7108 on `wide_400` (10,804) and 0.7947 on `manylists_400` (12,080)**,
   against 1.0000 on every document of 100 rows or fewer *and* on the 800-row one. **6 of
   the 16 carry no completeness issue at all**, and that is structural: on the 8-column
   `wide_400` and on Textract LAYOUT text there is no exact-width OCR table to check a row
   count against, and 10 rows is under the 30-row floor below which even the new
   `extraction_row_count_unverified` report stays silent (§5.1).
5. **The confidence pass no longer discards a complete large extraction — it now returns it
   with most of the confidence missing.** In the scaling study the 1,200- and 1,600-row
   simple-mode documents, which **failed** at v0.6.12 after every row had been extracted,
   now **complete on 6 of 6 runs with every row at cell accuracy 1.000** — and with
   `assessment_incomplete` on all six, because the confidence pass now stops at its time
   budget (#1488) and scores only **33–38% of the rows at 1,200 and 19–23% at 1,600**.
   Across the whole refresh the `large_narrow` assessment failure fell from 15 of 190
   simple-mode runs to **2 of 190**, both memory kills
   ([#1678](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1678)).
   Advanced mode holds recall 1.000 **and** full confidence coverage at every size from 25 to
   3,200 rows (§3).
6. **Classification granularity, modality and strategy have their first measurement, and
   packet granularity places boundaries far better than the default.** On 20 DocSplit
   packets with Nova 2 Lite, over the 15 packets every variant scored, the shipped
   `page`-granularity default places section boundaries correctly at **0.446** and the four
   packet-level variants at **0.711–0.821**, at **23–65% of its classification cost per page**.
   `packet` + images only has the best page class accuracy, **0.909** against the default's
   0.851. ⚠️ **But every packet variant that sends page images failed the same three
   18–20-page packets** with `Input is too long for requested model`
   ([#1676](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1676)),
   and text-only page granularity failed two packets on a page with no OCR text
   ([#1677](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1677)).
   Twenty documents is a small sample, so read this as direction and failure modes rather
   than as a ranking (§5.6).
7. **Neither premium knob on the other two model axes earns its price.** Upgrading the
   **classification** model buys nothing measurable: every arm produces downstream recall
   1.0000 over the same 83,790 cells, the Nova 2 Lite default and Haiku 5.5 get the section
   boundary right on **133 of 133** runs and Sonnet 5 on **131 of 133**, and the grid bill
   rises from **$89.10 to $113.59** under Sonnet 5. Sonnet 5 as the **confidence** model is
   **a far more conservative scorer, not a more accurate one**: about **285 of 119,968**
   confidence leaves below 0.9 against the default's **0 of 119,921**, for **$199.82 against
   $89.97**, with identical cell accuracy (0.9997 over 83,790 cells in all three arms). The
   two Sonnet 5 premiums are upper bounds, because `pricing.yaml` overstates Sonnet 5 (§5.3,
   §5.4).
8. **Both standing list-loss hazards stay closed.** `integrated` confidence with simple
   extraction is recall 1.000 and cell accuracy 1.000 on **3 of 3 repeats**, at the same cost
   to the cent as `separate` ($0.2053 against $0.2052), and the advanced-mode tool-decline
   list loss is **0 of 6** at the shipped default (§2.1). **131 of the 133 grid runs produced
   exactly one section**; the two exceptions split the same 8-column 400-row statement in two
   (288 + 112 rows). They are `enforce-warn` and `force-off`, which resolve to exactly the
   same configuration as `core-tt-simple-sep` and `split-llm`, and those two did not split —
   so this is one classifier draw in four going the other way, not a configuration effect.
9. **Bedrock-LLM OCR has a readable accuracy figure again, and it is still the one backend
   that gets values wrong.** With identifiers repaired it returns every row — recall 1.000 over
   4,410 cells, the same denominator as every other cell — at **cell accuracy 0.9950**, the
   only cell of the nineteen below 1.000, and **10 of 14 scalar fields right** where every
   other cell gets 12 to 14. It is also the cheapest cell, at $0.3465 per document. Do not use
   it where identifiers or amounts matter (§2, finding 4).
10. **The cheap end completes everything and returns half the rows.** Neither Nova model can
    finish the agentic path
    ([#895](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/895)),
    so both ran the smaller `simplegrid` matrix, where they completed **63 of 63 runs** at mean
    recall **0.5054** (Nova Pro) and **0.4964** (Nova Lite). Nova Pro's 0.9324 cell accuracy is
    over **7,764 cells** against the same matrix's Sonnet 4.6 control at **39,690** (§5.1).

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
  plus the v0.7 feature arms (enforcement, forcing, schema restatement, section splitting) —
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
| **Classification variant** (§5.6, new) | `page` or `packet` granularity × text and/or images × `combined` or `split_then_classify` — seven named combinations, Nova 2 Lite |
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
default's own grid is §5.1's. **133 runs, 0 failures, and 131 of the 133 produced exactly
one section**; the two exceptions are the same 400-row document split in two, finding 6.

**All nineteen cells are at recall 1.000 over 4,410 `cells_compared` each, on all 7
documents — including the 800-row, 17-page one — and eighteen are also at cell accuracy
1.000.** Every figure below sits inside that result, so the grid separates the
configurations on **cost, wall-clock and value fidelity** rather than on completeness.

### 2a. OCR backend, extraction mode and confidence mode

`leaves < 0.9` is a **count** of confidence leaves below the 0.9 alert threshold, not a
percentage, over the per-cell leaf total in the next column — on this grid the fractions are
small enough that a percentage reads as zero everywhere.

| OCR / mode / assessment | recall | cell acc | `cells_compared` | cost/doc | mean conf | leaves < 0.9 | conf leaves | valid rate | wall_s | fails |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Textract TABLES / simple / separate | 1.000 | 1.000 | 4,410 | $0.5578 | 0.9990 | 0 | 6,664 | 1.000 | 195 | 0 |
| Textract TABLES / simple / off | 1.000 | 1.000 | 4,410 | **$0.4002** | n/a | n/a | 0 | 1.000 | **73** | 0 |
| Textract TABLES / simple / integrated | 1.000 | 1.000 | 4,410 | $0.5570 | 0.9990 | 0 | 6,664 | 1.000 | 196 | 0 |
| Textract TABLES / advanced / separate | 1.000 | 1.000 | 4,410 | $0.6184 | 0.9879 | 0 | 6,664 | 1.000 | **90** | 0 |
| ⚠️ Textract TABLES / advanced / integrated | 1.000 | 1.000 | 4,410 | **$2.9153** | 0.9939 | 0 | 6,664 | 1.000 | **460** | 0 |
| Textract LAYOUT / simple / separate | 1.000 | 1.000 | 4,410 | $0.4613 | 0.9983 | 0 | 6,655 | 1.000 | 202 | 0 |
| Textract LAYOUT / advanced / separate | 1.000 | 1.000 | 4,410 | $0.9381 | 0.9883 | 0 | 6,664 | 1.000 | 151 | 0 |
| BDA / simple / separate | 1.000 | 1.000 | 4,410 | $0.5569 | 0.9990 | 0 | 6,664 | 1.000 | 191 | 0 |
| BDA / advanced / separate | 1.000 | 1.000 | 4,410 | $0.6175 | 0.9876 | 0 | 6,664 | 1.000 | **91** | 0 |
| **⚠️ Bedrock-LLM / simple / separate** | 1.000 | **0.995** | 4,410 | **$0.3465** | 0.9975 | 0 | 6,664 | 1.000 | 170 | 0 |

`cells_compared` is the **total** over the 7 documents (2 scored values per row, so
2 × 2,205 truth rows), while the cost, confidence and wall columns are means. Cost CV within
a cell is large because the 7 documents differ 160× in row count; that is *between-document*
spread and tells you nothing about reproducibility. §4 measures cost variance properly, with
repeats on one document.

### 2b. Configurations testing v0.7 features

All are Textract TABLES + `separate`; `restate-*` are advanced, the rest simple. `sections`
is the total across the 7 documents, where the ground truth is **7** (one per document).
Every arm is at recall 1.000 and cell accuracy 1.000 over **4,410 `cells_compared`**, so the
columns that discriminate are cost and the validation pair.

| cell | recall | cell acc | `cells_compared` | cost/doc | sections (truth 7) | valid rate | val errors |
|---|---:|---:|---:|---:|---:|---:|---:|
| `enforce-off` | 1.000 | 1.000 | 4,410 | $0.5606 | 7 | not measured (validation off) | — |
| `enforce-warn` *(shipped default)* | 1.000 | 1.000 | 4,410 | $0.5201 | **8** | 0.929 | 1 |
| `enforce-escalate` | 1.000 | 1.000 | 4,410 | $0.5588 | 7 | 1.000 | 0 |
| `force-off` *(shipped default)* | 1.000 | 1.000 | 4,410 | $0.5192 | **8** | 0.929 | 1 |
| `force-on` | 1.000 | 1.000 | 4,410 | $0.5668 | 7 | 1.000 | 0 |
| `restate-on` *(shipped default)* | 1.000 | 1.000 | 4,410 | $0.6188 | 7 | 1.000 | 0 |
| `restate-off` | 1.000 | 1.000 | 4,410 | $0.6164 | 7 | 1.000 | 0 |
| `split-llm` *(shipped default)* | 1.000 | 1.000 | 4,410 | $0.5594 | 7 | 1.000 | 0 |
| `split-disabled` | 1.000 | 1.000 | 4,410 | $0.5532 | 7 | 1.000 | 0 |

⚠️ **Four rows of this table run the identical configuration, so read the spread between them
as the noise floor.** `enforce-warn`, `force-off` and `split-llm` each resolve to exactly the
same settings as §2a's `core-tt-simple-sep` — every one is the shipped default on the axis it
names. Those four cost **$0.5192–0.5594 per document**, and two of them split one document
where the other two did not. A difference smaller than that band between two cells of this
table is not a measured effect.

**Findings**

1. **Completeness is solved across the whole decision space, up to and including the 800-row
   / 17-page document.** All ten decision cells — every OCR backend, both modes, all three
   assessment settings — are at recall 1.000 over 4,410 `cells_compared` each, on all 7
   documents. Mode is therefore a **cost and latency** decision at these sizes rather than a
   completeness one. On matched OCR backends with `separate` assessment, simple → advanced is
   **$0.5578 → $0.6184 for TABLES (1.11×)**, **$0.4613 → $0.9381 for LAYOUT (2.03×)** and
   **$0.5569 → $0.6175 for BDA (1.11×)**. Advanced mode is also the **faster** of the two on
   all three matched pairs — 90 s against 195 s on TABLES, 151 s against 202 s on LAYOUT,
   91 s against 191 s on BDA — because simple mode's single long response, and its
   confidence pass over the whole list, dominate the wall-clock on the larger documents.
2. **There is no completeness cliff in this grid at the control model.** All nineteen cells
   returned **800 of 800 rows on `large_narrow`**, one draw each. What this does *not*
   establish is that 800 rows is comfortable in simple mode: §3's size series and §5.1's
   per-model grid both show the same request landing on either side of a single-response
   limit, and §5.1's Sonnet 5 grid returns **43 of 800** in this very cell. Read §2 as "the
   control model clears this corpus" and §3 and §5.1 as where the limit actually is.
3. **`integrated` + simple costs the same as `separate`, because it *is* `separate`.**
   Recall 1.000 and cell accuracy 1.000 over 4,410 cells on all 7 documents, **0 of 6,664
   confidence leaves below 0.9**, at $0.5570 against `separate`'s $0.5578 — because a
   simple-mode section whose class declares a list is routed to the separate confidence pass
   automatically (#795). §2.1 has the repeated-measures confirmation. Choosing `integrated` in
   simple mode costs you nothing and changes nothing for list-bearing classes.

   ⚠️ **On the *agentic* path `integrated` is the most expensive cell in the grid by a wide
   margin: $2.9153 against `advanced`/`separate`'s $0.6184, 4.7×, at 460 s against 90 s.**
   Completeness is unaffected, so this is a cost and latency finding, and it is concentrated
   on the four documents of 400 rows or more, which cost **$3.05–8.08** each against
   $0.09–0.41 for the three smaller ones. Every one of those four runs carries
   `extraction_output_truncated`, with extraction output of **147,492–413,136 tokens**: the
   agent is emitting inline assessment, overrunning its output budget and retrying. §4
   reproduces the same shape with five repeats on one document. **Use `separate` on the
   agentic path.**
4. **⚠️ Bedrock-LLM OCR is the only backend that gets *values* wrong, and on this release
   its figures can be read.** It returns every row — recall 1.000 over 4,410 cells — at
   **cell accuracy 0.995**, the only one of the nineteen cells below 1.000, with 0.984–0.996
   on the four documents of 400 rows or more and 1.000 on the three smaller ones. Its scalar
   fields, which compare the document's `Account Number` and `Statement Period` by label, are
   right **10 times in 14**, where every other cell gets 12 to 14 of 14; `Account Number` is
   exactly the fixed-width identifier this backend perturbs — the digit insertion root-caused
   in the [v0.6.6 audit](releases/v0.6.6.md). The scorer repaired **42–45 perturbed row
   identifiers** per document of 400 rows or more before joining (`rows_id_repaired`, #1500),
   which is what makes those accuracy figures a comparison of values rather than an artefact
   of the join. It is also the cheapest cell in the grid ($0.3465). **Do not use it where
   identifiers or amounts matter.**
5. **LAYOUT-only is the cheapest complete option in simple mode and the dearest in advanced**
   ($0.4613 simple, $0.9381 advanced) with confidence indistinguishable from TABLES (0.9983 /
   0.9883). In simple mode TABLES costs 21% more and bought no completeness here. In advanced
   mode the ordering inverts: LAYOUT is **52% more than TABLES** ($0.9381 against $0.6184)
   and as much more than BDA, which §4 reproduces with repeats on one document. The LAYOUT
   text is cheaper to produce and more expensive for the agent to extract from. Every LAYOUT
   run of 100 rows or more also carries the new `extraction_row_count_unverified` issue
   (#1489), and correctly: LAYOUT emits no Markdown tables, so there is nothing to check a
   row count against. That issue is `info` and reports the absence of evidence, not a
   shortfall.
6. **Validation is clean everywhere except one split document.** Every cell with validation
   on reports `valid rate` 1.000 with 0 errors except `enforce-warn` and `force-off`, which
   each split the 8-column `wide_400` statement into two sections of 288 and 112 rows; the
   second section carries no account header, so it raises the one validation error and an
   `extraction_sparse` note in each. All 400 rows are still returned and correct in both runs.
   That is the over-split mechanism of the v0.6.7 edition in miniature, now occurring on two
   draws of 133.
7. **`force-on` (forced tool use) is schema-valid, honoured, and complete.** Valid rate
   1.000, **attempted and honoured on 7 of 7 documents with no skips** (76 of 76 attempts
   across `core`, reference corpora included), recall 1.000 and cell accuracy 1.000 over
   4,410 cells, at $0.5668 — inside 2% of the top of the identical-default band
   ($0.5192–0.5594). §7 has the dedicated A/B.
8. **`enforce-escalate` costs nothing on clean documents** ($0.5588, inside the
   identical-default band) because there is nothing to escalate: 0 validation errors in that
   cell means 0 re-extractions. Its price is paid only when validation actually fails.
9. **Schema restatement off is neutral** ($0.6164 against $0.6188, n=7, well inside the
   agentic spread §4 measures).
10. **`split-disabled` is a no-op on this corpus** (7 sections either way, $0.5532 against
    $0.5594), because every document is a single statement. It is wrong by construction on
    packets, where it also **loses rows** (§7).

### 2c. Real-corpus accuracy (RealKIE-FCC-Verified, OmniAI-OCR-Benchmark)

Both reference corpora run as Test Studio test sets — 20 documents each, every one of the 19
cells — and are scored by the product's own evaluation against their labels
(`weighted_accuracy` for extracted fields, `class_accuracy` for classification; the
harness's exact-key metrics do not apply to a document whose truth lives on the stack
rather than in `corpus/docs/`, so `cells_compared`, `cell_accuracy`, `scalar_accuracy` and
`completeness_recall` are absent on these rows — absent, not zero. Each row says which
scorer produced it in `scored_against`).

v0.6.13 ran both corpora at the **control only** — **756 scored rows over 760 (cell,
corpus, document) triples**, 0 parse failures anywhere — and reproduces the control
picture: RealKIE **0.775–0.811** weighted accuracy across the 19 cells, OCR-Benchmark
**0.977–0.998**, class accuracy **1.000 in every cell of both corpora**. Render the
per-cell tables from the committed artifact with

```bash
python3 benchmarks/harness/aggregate.py \
    --reference-tables benchmarks/results/v0.6.13/core/summary.json
```

**The four tables further down are the v0.6.8 pair** — one grid at the shipped default and
one at the control, on one stack, which is what makes the model comparison in their
finding 2 a comparison of models rather than of releases. **1,520 documents, 1,520
completed, 0 parse failures.** They are not re-measured: no Sonnet 5 arm ran on these
corpora in this edition.

#### ⚠️ Simple mode is more accurate than advanced mode on real documents, at about half the cost

This is the finding the synthetic grid **cannot** produce, and the reason running these
corpora matters more than the row count suggests. §2's 19 cells score 1.000 on recall almost
everywhere on exact-ground-truth synthetic documents, so the grid ranks nothing. RealKIE
ranks them, and it inverts the intuition that the agentic path is the more capable one:

| RealKIE cell | mode | weighted accuracy | cost/doc | wall |
|---|---|---|---|---|
| `split-disabled` | simple | **0.811** | $0.1265 | 71 s |
| `core-tt-simple-off` | simple | 0.806 | $0.1399 | 27 s |
| `core-tt-simple-sep` | simple | 0.805 | $0.1437 | 54 s |
| `core-tt-adv-int` | advanced | 0.805 | $0.4911 | 52 s |
| `restate-off` | advanced | 0.786 | $0.3444 | 80 s |
| `core-bda-adv-sep` | advanced | 0.782 | $0.2958 | 58 s |
| `core-tl-adv-sep` | advanced | 0.780 | $0.2261 | 60 s |
| `core-tt-adv-sep` | advanced | **0.775** | $0.3287 | 72 s |
| `restate-on` | advanced | 0.775 | $0.3699 | 80 s |

**Eleven simple-mode cells score 0.803–0.811, and five of the six advanced cells fall below
every one of them**, at 0.775–0.786. The matched pair is `core-tt-simple-sep` at 0.805 for
$0.1437 against `core-tt-adv-sep` at 0.775 for $0.3287 — 2.3× the cost for 3 points less.
The sixth advanced cell, `core-tt-adv-int`, does reach the simple band (0.805), at **3.4× the
simple cell's price**. The two simple cells below the band are the two with a known handicap:
Bedrock-LLM OCR (0.776) and forced tool use (0.798). The two readings this does *not* support:
it says nothing about completeness at scale, which is §3's territory and where advanced mode
is unambiguously better; and RealKIE's documents are not long lists, so this is a statement
about labelled real-world forms rather than about tabular extraction generally. The same
shape held at v0.6.12 (eleven simple cells 0.8029–0.8119, all six advanced below them), so
two releases agree on it.

OCR-Benchmark does not discriminate the same way — every cell lands between 0.977 and
0.998, with `core-llm-simple-sep` the only one below 0.99 (0.977). These reference-corpus
figures are scored by the product's own evaluation against labels rather than by a row
identifier, so they were never affected by the identifier defect described in §2 finding 4.

⚠️ **This is one arm, so it is not a model comparison.** Only the control ran, so the
v0.6.8 two-model pair below is still the only like-for-like comparison of the shipped
default against the control on these corpora. Comparing a v0.6.13 control figure with a
v0.6.8 default figure would be a cross-release difference wearing a model label.

⚠️ **4 of the 760 triples produced no score** — `SCORE_ERROR` on `ocr_bench` in
`core-bda-simple-sep`, `core-tl-simple-sep`, `core-tt-simple-sep` and `enforce-warn`. That
is a **scoring** refusal, not a processing failure: on each document one grader leaf
answered on a 0–100 scale (99.8 or 99.9) where the scale is 0.0–1.0, and the harness refuses
such a leaf rather than averaging it
([#1395](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1395)).
Those four cells therefore read 19 of 20 completed. v0.6.12 had four as well, on the same
corpus, but only two of the cells are the same (`core-bda-simple-sep` and
`core-tl-simple-sep`), so which documents trip it varies from run to run.

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

## 2.1 Integrated confidence with simple mode: list truncation fixed in v0.6.8

**What v0.6.13 measures.** On the 7-document grid, `simple/integrated` recall is **1.000 on
every document** at cell accuracy 1.000 over 4,410 `cells_compared`, including
`manylists_400` (0.552 at v0.6.7) and the 800-row `large_narrow`. The repeated-measures
suite that established the hazard (`intconf`: the integrated cell and a `separate` control
on the same 100-row document, `longdesc_100`, 3× each this edition) reads, at the control
model — the only arm that ran:

| extraction model | cell | recall per repeat | cell acc (`cells_compared`) | cost/doc | min–max | mean conf |
|---|---|---|---|---|---|---|
| Sonnet 4.6 (control) | simple / **integrated** | **1.000 ×3** | 1.000 (200 ×3) | $0.2053 | $0.2051–0.2056 | 0.9989–1.0000 |
| Sonnet 4.6 (control) | simple / separate | 1.000 ×3 | 1.000 (200 ×3) | $0.2052 | $0.2049–0.2056 | 0.9993–1.0000 |

At v0.6.5 the first row read **0.000 ×4** (1–10 rows returned, none matching). The reason
the two cells cost the same to within $0.0001 is that they *are* the same mechanism: a
simple-mode section whose class declares a list field is routed to the separate confidence
pass regardless of the `integrated` setting (#795; `metadata.confidence_mode_effective`
records it). A class can opt back into true inline scoring with
`x-aws-idp-allow-integrated-lists: true`, and these numbers do not cover that path. Both
arms produced one section per run and **0 of 921 confidence leaves below 0.9**.

**The advanced-mode counterpart is closed too, and it is the arm measured at the shipped
default.** `advverify__extraction-model-sonnet5` (the agentic cell with `integrated` and
`separate` confidence on `longdesc_100`, 3× each, Sonnet 5): **6 of 6 runs complete**,
recall 1.000 and cell accuracy 1.000 over 200 `cells_compared` per run, one section each.
Cost is `integrated` **$0.4761** mean ($0.3590–0.6227) against `separate` **$0.3414**
($0.3139–0.3955), so integrated is about 39% dearer on the agentic path at this size, with
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
(~48 rows/page), and exactly one section in every run that produced one at all — 39 of the
42, the exceptions being the three 3,200-row simple runs, which produced none. Extraction
model **Sonnet 4.6, the cross-version control**: the `scaling` suite carries no model
override. **Three draws per mode per size this edition**, where the v0.6.12 edition had one,
so every figure below is a mean of 3 with its range where the range matters. For the shipped
default at the top of this range, §5.2's `astracap` suite is the measurement.

⚠️ **Read the `conf coverage` column beside the recall column.** On this release simple mode
returns every row at 1,200 and 1,600 rows and reports `COMPLETED` — and most of those rows
carry no confidence score.

| rows | pages | SIMPLE status | simple recall | simple cell acc (`cells_compared`) | conf coverage | simple $ | wall | ADVANCED status | adv recall | adv cell acc (`cells_compared`) | adv $ | wall |
|-----:|------:|---|---:|---|---:|---:|-----:|---|---:|---|------:|-----:|
| 25 | 1 | 3/3 COMPLETED | 1.000 | 1.000 (50) | 1.000 | $0.0430 | 25 s | 3/3 COMPLETED | 1.000 | 1.000 (50) | $0.1023 | 38 s |
| 100 | 3 | 3/3 COMPLETED | 1.000 | 1.000 (200) | 1.000 | $0.1441 | 50 s | 3/3 COMPLETED | 1.000 | 1.000 (200) | $0.1739 | 73 s |
| 400 | 9 | 3/3 COMPLETED | 1.000 | 1.000 (800) | 1.000 | $0.5244 | 196 s | 3/3 COMPLETED | 1.000 | 1.000 (800) | $0.5782 | 253 s |
| 800 | 17 | 3/3 COMPLETED | 1.000 | 1.000 (1,600) | 1.000 | $1.6437 | 819 s | 3/3 COMPLETED | 1.000 | 1.000 (1,600) | $1.1957 | 225 s |
| 1,200 | 25 | **3/3 COMPLETED ⚠️** | 1.000 | 1.000 (2,400) | **0.33–0.38** | $2.5168 | 998 s | 3/3 COMPLETED | 1.000 | 1.000 (2,400) | $1.7410 | 134 s |
| 1,600 | 33 | **3/3 COMPLETED ⚠️** | 1.000 | 1.000 (3,200) | **0.19–0.23** | $3.0151 | 1,066 s | 3/3 COMPLETED | 1.000 | 1.000 (3,200) | $2.3357 | 253 s |
| 3,200 | 66 | **3/3 FAILED** | 0.000 | — (0 rows) | — | $1.0543 ‡ | 50 s | 3/3 COMPLETED | 1.000 | 1.000 (6,400) | **$4.6034** | **602 s** |

`cells_compared` is per run. ‡ A refused document bills no extraction or assessment phase,
so this figure is **OCR plus classification only** ($0.9913 + $0.0630) and is not the cost of
the work attempted. Advanced-mode confidence coverage is 1.000 at every size.

**Advanced mode is at recall 1.000, cell accuracy 1.000 and full confidence coverage at every
size from 25 to 3,200 rows, on 21 of 21 runs**, one section per document throughout. Sharding
keeps each call small, so neither the single-response truncation nor an input-context limit
nor the confidence pass's time budget is reached.

Per-row **cell accuracy is 1.000 in every run of both modes that returned rows at all** — at
no size does either mode return a row with a *wrong* value.

### How simple mode behaves as documents grow

The simple-mode column above is not a gradient. It is three distinct behaviours with two
sharp boundaries, and they want different responses from a deployment:

1. **25 → 800 rows / 1 → 17 pages: it completes, with everything.** `COMPLETED`, recall
   1.000, cell accuracy 1.000, every row confidence-scored. At 800 rows the confidence pass
   is already the larger part of the bill — **$0.70 of $1.64 per document**, against $0.04
   for the same pass in advanced mode — and of the wall-clock: 819 s against 196 s at 400
   rows. The batch-shrinking ladder converges there (`assessment_recovered_with_retries` on
   all three runs, 3–11 rows recovered, 0 unrecoverable).
2. ⚠️ **1,200 and 1,600 rows / 25 and 33 pages: every row is extracted and returned, and
   most of them have no confidence.** All six runs report `COMPLETED`, recall 1.000 and cell
   accuracy 1.000 over 2,400 and 3,200 `cells_compared`, validated clean. And all six carry
   `assessment_incomplete` at `error` severity, with `deadline_reached`: the confidence pass
   scored **401–461 of 1,200 rows and 309–372 of 1,600**, then stopped to stay inside the
   Lambda's time budget, so the remaining **739–1,291 rows per document come back with
   `confidence: null`** and a per-row reason naming the time budget. This is the
   [#1488](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1488)
   fix working as designed. At v0.6.12 both of these sizes **failed** after a complete
   extraction, because the pass was killed by its Lambda and no degrade path ran. Now the
   document arrives, complete, with a reported gap — which is the better outcome, and which
   a consumer that gates review on confidence has to handle, because an unscored row raises
   no confidence alert.
3. **3,200 rows / 66 pages: refused before extraction inference, which is correct.**
   `FAILED` in about 50 s having returned **0 rows and 0 sections**, with no extraction
   tokens billed at all — the spend is the OCR and classification that had already run. The
   request is too long for one call; `benchmarks/matrices/config_matrix.yaml` records this
   size as an expected rejection rather than a regression, and §5.2's `astracap` reproduces
   it 6 times over, three times on Sonnet 5 and three on GPT-6 Astra. Refusing is the right
   outcome: nothing is attempted and nothing wrong is returned.

⚠️ **So in regime 2 neither `status` nor `completeness_recall` tells you what is missing —
`conf_coverage` and the `assessment_incomplete` issue do.** A reader who ranks by recall or
by completion rate concludes simple mode is flawless at 1,600 rows, and on the extraction it
is. What is missing is the confidence on up to four rows in five.

> ⚠️ **No per-page input-token budget is published for the refused size.** A refused document
> bills **no extraction tokens**, so there is no record of the request. What the suite does
> measure is the simple-mode extraction input on the six sizes that completed — **2,552,
> 7,958, 26,616, 51,433, 76,482 and 101,733 tokens at 1, 3, 9, 17, 25 and 33 pages**, about
> **3,100 tokens per page** over the 3–33-page span, with the 1-page point below that line
> because fixed overhead dominates it. Extrapolating it to 66 pages would produce a number
> this suite does not support, so none is given.
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
estimate and the remedy. What is new in this release is the case where that check *cannot
run*: when the matched evidence is under 30 rows while 30 or more rows came back, the section
now carries `extraction_row_count_unverified` (`info`) instead of silence (#1489). It reports
the absence of evidence and never a shortfall. ⚠️ **Its floor cuts both ways**: a list
truncated to *fewer* than 30 rows on a document without exact-width tables is reported by
neither issue, which is exactly the shape of Sonnet 5.5's 10-row runs on `wide_400` in §5.1.

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
   with all 800 rows and full confidence on 3 of 3 runs here and on all 19 of §2's cells —
   but §5.1 measures the *same* 17-page document in simple mode returning between 0 and
   141 of 800 rows on some runs of four of the seven extraction models while reporting
   `COMPLETED`, the shipped default among them (10 of its 13 runs). The single long response is what varies, and which side of the limit a
   run lands on moves with the model and with the input text. **Treat ~400 rows / ~10 pages
   as the comfortable simple-mode envelope and 800 as the edge of it**; use advanced mode
   above that.
2. **Above the envelope, simple mode returns the rows without the confidence, then stops
   being available.** At 1,200 and 1,600 rows the extraction is complete and up to 81% of the
   rows are unscored (regime 2); at 3,200 rows the request is refused outright (regime 3).
   Neither is improved by tuning the extraction: the remedy at every size above the envelope
   is advanced mode, which returns every row with confidence at all seven sizes. The
   `assessment_incomplete` message names two other remedies — more Lambda time, or less work
   per section — and on this evidence they treat the confidence half only.
3. ⚠️ **No single column summarises a simple-mode run at size.** `COMPLETED` is not evidence
   of completeness — §5.1's truncated-but-completed runs are the proof — and recall 1.000 is
   not evidence that the document is review-ready, because at 1,200 and 1,600 rows most rows
   are unscored. Read `status`, `completeness_recall`, `cells_compared` **and**
   `conf_coverage` together.

### Advanced mode: cost and processing time as documents grow

Advanced (agentic sharding) holds **recall 1.000, cell accuracy 1.000 and full confidence
coverage from 25 through 3,200 rows / 66 pages** at the control model, on 21 of 21 runs, on
denominators from 50 to 6,400 `cells_compared` per run — and, in the premium study (§5.2), at
Sonnet 5 and GPT-6 Astra too.

The practical limits are **cost and wall-clock**, and at the control model both stay modest:
**$4.6034 and 602 s at 3,200 rows** (range $4.39–4.99, 465–747 s), against $0.1023 and 38 s
at 25 rows. Cost is close to **linear in rows from 400 upward** — $0.00145, $0.00149,
$0.00145, $0.00146 and $0.00144 per row at 400, 800, 1,200, 1,600 and 3,200 rows — so there
is no cost cliff at the top of this series; the 25-row point is the outlier, at $0.00409 per
row, because the fixed per-document overhead dominates a one-page document.

**From 800 rows upward advanced mode is the cheaper mode as well as the complete one**:
$1.1957 against simple's $1.6437 at 800 rows, $1.7410 against $2.5168 at 1,200 and $2.3357
against $3.0151 at 1,600 — and simple mode at those two sizes buys a third or less of the
confidence advanced mode delivers in full. The difference is almost all the confidence pass,
which costs $0.04–0.08 per document in advanced mode across that range and $0.70–1.23 in
simple mode. For the shipped default at the 3,200-row size, §5.2's `astracap` measures
Sonnet 5 at **$4.95–5.58 over three repeats, 434 s mean** — and those Sonnet 5 figures are
upper bounds (see the header).

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

Extraction model **Sonnet 5**, the shipped default (`cost__extraction-model-sonnet5`) —
dollar figures are upper bounds, because `pricing.yaml` prices Sonnet 5 at 1.5× its
published rates (see the header); the shape and the CVs are unaffected:

| config (OCR / mode / assessment) | cost mean ± stdev | CV | min–max | sections/run |
|----------------------------------|-------------------|---:|---------|---|
| Textract TABLES / **simple** / separate | $0.725 ± $0.032 | **4.4%** | $0.703–0.776 | 1 ×5 |
| BDA / advanced / separate | **$0.670 ± $0.047** | 7.0% | $0.646–0.753 | 1 ×5 |
| Textract TABLES / advanced / separate | $0.679 ± $0.071 | 10.5% | $0.645–0.807 | 1 ×5 |
| Textract TABLES / advanced / integrated | $0.812 ± $0.099 | 12.2% | $0.739–0.939 | 1 ×5 |
| Textract LAYOUT / advanced / separate | $1.315 ± $0.264 | **20.1%** | $1.087–**1.690** | 1 ×5 |

The same five cells at the **control model, Sonnet 4.6** (`cost`, also 5 repeats, all
complete, one section each):

| config | cost mean ± stdev | CV | min–max |
|---|---|---:|---|
| Textract TABLES / simple / separate | $0.524 ± $0.000 | **0.0%** | $0.524–0.525 |
| BDA / advanced / separate | $0.576 ± $0.004 | **0.7%** | $0.571–0.583 |
| Textract TABLES / advanced / separate | $0.574 ± $0.003 | **0.5%** | $0.571–0.578 |
| Textract LAYOUT / advanced / separate | $1.230 ± $0.182 | **14.8%** | $0.945–1.427 |
| ⚠️ Textract TABLES / advanced / integrated | **$3.340 ± $0.866** | **25.9%** | $2.922–**4.886** |

**Findings**

- **At 400 rows mode is not a price decision.** On the matched Textract TABLES pair, advanced
  costs **$0.679 against simple's $0.725** at Sonnet 5 — advanced is the cheaper one — and
  **$0.574 against $0.524, 1.10×** at the control. **BDA / advanced at $0.670 is the cheapest
  cell at the shipped default.** §3's size series shows the advanced premium *shrinking* with
  size at the control — 2.38× at 25 rows, 1.21× at 100, 1.10× at 400 — and **inverting at
  800**, where advanced costs $1.1957 against simple's $1.6437. Advanced mode is the expensive
  option on short documents, not on long lists.
- **Simple mode is the predictable one at the control and less so at the shipped default.**
  The control's simple cell is flat to the cent across five runs (CV 0.0%). At Sonnet 5 the
  same cell has a CV of 4.4%, because two of its five runs needed the confidence pass's
  batch-shrinking ladder (`assessment_recovered_with_retries`) and cost $0.735 and $0.776,
  against $0.703–0.705 for the other three.
- **The run-to-run spread is a property of the agentic path, and it is wider at the shipped
  default.** Advanced cells run at CV **7.0–20.1%** at Sonnet 5, and at the control **0.5–0.7%**
  on TABLES and BDA but **14.8%** on LAYOUT and **25.9%** on the integrated cell. The widest
  single cell at the shipped default is LAYOUT / advanced, which ranged **$1.087 → $1.690**
  across five identical runs of the same document, a **1.55×** swing with no configuration
  difference behind it. Every run in both suites is one section, so what is left is the
  agent's own turn count.
- **Budget the maximum, not the mean.** The largest gap between a cell's mean and its own
  worst run is the control's agentic + `integrated` cell, **$4.886 against a $3.340 mean, 46%
  above it**; at the shipped default it is LAYOUT / advanced, $1.690 against $1.315, **29%
  above**. ⚠️ **One cell exceeds the harness's CV > 0.25 unreliable-at-current-n flag** — the
  control's agentic + `integrated` cell at 0.259 — so its mean is not resolved at n=5. The
  other nine are.
- ⚠️ **The agentic + `integrated` cell is not a clean configuration cost at either model.** At
  Sonnet 4.6 it is **$3.340 against the `separate` cell's $0.574 — 5.8× —** and all five runs
  carry `extraction_output_truncated` together with `assessment_recovered_with_retries`, with
  extraction output averaging **171,877 tokens against the `separate` cell's 13,431**: the
  agent is emitting inline assessment, overrunning its output budget and retrying, and the
  dollar figure is mostly that retry. At Sonnet 5 the same cell reads $0.812 but carries
  `assessment_incomplete` on 5 of 5 runs and **no assessment spend at all** ($0.0000 in the
  Assessment phase), so the confidence work it is priced for did not land. Both arms reach
  recall 1.000 and cell accuracy 1.000 over 800 cells, by different routes. Use `separate` on
  the agentic path; §2a measures the same contrast across seven document sizes.
- **BDA / advanced is the cheapest advanced option at the shipped default** ($0.670, CV 7.0%)
  and within a cent of the cheapest at the control ($0.576 against TABLES' $0.574).
- **LAYOUT / advanced is the dearest advanced option at both models, not the cheapest.** Its
  *minimum* run ($1.087 at Sonnet 5, $0.945 at the control) sits above the TABLES advanced
  cell's *maximum* ($0.807, $0.578), so the ordering holds run-to-run rather than only on the
  mean. Extraction is where it goes: $1.249 of its $1.315 at Sonnet 5, against $0.515 of
  $0.679 for TABLES.
- **Why advanced costs anything extra at all, given the deterministic table tool:** it is a
  multi-turn agent loop, and each turn re-sends the growing conversation as *input* tokens.
  With one section per document that loop now runs once.

> **Model note.** §4's Sonnet 5 table is the only Sonnet 5 measurement in §2–§4; §2 and §3
> are at the control, so compare §4's control table with those and read the Sonnet 5 table
> against §5. Do not compare absolute dollars across the two tables — only shapes, not least
> because the Sonnet 5 rates in `pricing.yaml` are 1.5× high.

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
5.5, and OpenAI GPT-6 Astra (`us.` and `global.`). No selectable model was added in v0.6.13,
so the axis is the same as the v0.6.12 edition's. Nova 2 Lite classification and Nova Lite
confidence are held at their defaults except where they are the axis.

### 5.1 Extraction model — the full grid

**All nine swept models were measured on v0.6.13.** Seven ran the 19-cell × 7-document
`coresynth` grid; the two Nova models cannot complete the agentic path
([#895](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/895))
and ran `simplegrid` instead, so they are reported separately below and never averaged in.
The grid's documents run to **800 rows**; the larger sizes are §3's and §5.2's territory.

| extraction model | runs | completed | mean recall | mean cell accuracy | `cells_compared` | grid cost | wall/doc | % conf leaves < 0.9 |
|---|---:|---:|---|---|---:|---|---:|---|
| **Claude Opus 5.5** | 133 | **133** | **1.0000** | **1.0000** | **83,790** | **$107.67** | 244 s | 0.012% |
| Claude Opus 5 | 133 | **133** | **1.0000** | **1.0000** | **83,790** | $128.52 | 270 s | 0.000% |
| GPT-6 Astra | 133 | 132 | 0.9925 | 0.9735 | 82,188 | $188.81 ◊ | 223 s | 0.009% |
| Claude Sonnet 5 `:1m` | 133 | 133 | 0.9658 | 0.9995 | 76,514 | $90.01 § | 232 s | 0.000% |
| Claude Haiku 5.5 | 133 | 132 | 0.9427 | 0.9995 | 73,106 | $25.68 † | 186 s | 0.025% |
| Claude Sonnet 5 *(shipped default)* | 133 | 133 | 0.9307 | 0.9999 | 69,042 | $84.74 § | 210 s | 0.000% |
| **Claude Sonnet 5.5** | 133 | 133 | **0.8851** ※ | 1.0000 ※ | 71,554 | $63.06 ‡ | 190 s | 0.016% |

⚠️ **Read `cells_compared` alongside the accuracy columns, not after them.** A cell the arm
never returned is a cell it is not scored on, so an arm that returns fewer rows is graded on
a smaller denominator — the range here is 69,042 to 83,790, a 21% spread. That is why the
recall column is the one to rank on and the accuracy column is a statement about the rows an
arm *did* return. The shipped default's 0.9999 over 69,042 cells and Opus 5.5's 1.0000 over
83,790 are not the same achievement.

**Two arms are at the ceiling of this grid, and the cheaper one is the newer.** Claude Opus
5.5 and Claude Opus 5 both completed **133 of 133** runs and both scored **recall 1.0000 and
cell accuracy 1.0000 over the full 83,790 cells**. Opus 5.5 did it for **$107.67 against
$128.52 — 16% less** — and both are flat-rate, so that one is a true ratio. It is also the
faster of the two, 244 s against 270 s per document (median 162 s against 177 s). On this
release Opus 5.5 completed every run; at v0.6.12 its three non-completions were the
`AssessmentStep` memory failure on one 17-page document, which has all but gone on this
release (§3, #1488).

**GPT-6 Astra is the most expensive arm and the least accurate on values.** Its 0.9735 cell
accuracy over 82,188 cells is the lowest in the table on an essentially full denominator, so
it is not a denominator artefact: on every document it is about 0.97, against 1.0000 for both
Opus arms. Its recall is 0.9925 because of one simple-mode run on the 17-page document that
came back empty (`extraction_incomplete`, 0 rows, `COMPLETED`) — the v0.6.8 edition's
empty-response shape, recurring on one of 13 draws. Its $188.81 is a **lower bound** (◊), so
Opus 5.5 is **at least 43% less** than Astra for a better result.

**The shipped default is the second-least complete arm, and all of its shortfall is one
document in one mode.** Sonnet 5 is at recall 1.0000 on every document of 400 rows or fewer
(19 of 19 runs each), and on `large_narrow` in advanced mode (6 of 6). In **simple** mode on
`large_narrow` it returned all 800 rows on only **3 of 13** runs, and 43, 92 or 141 rows on the
other 10, each `COMPLETED` and each carrying `extraction_rows_below_ocr_estimate` — so the
truncation is flagged, not silent. That is a mean of ~233 of 800 rows, against ~454 at v0.6.12
on the same cells; one draw per cell, so read the difference as the variability of a bimodal
behaviour rather than as a release effect. Sonnet 5 `:1m` and Haiku 5.5 show the same shape at
a lower rate (5 and 6 short runs of 13).

※ ⚠️ **Do not read Sonnet 5.5's row as "cheap and perfectly accurate". Its mean recall hides
a bimodal failure, and its cell accuracy is computed on the rows it chose to return.** 16 of
its 133 runs lose most of the list, every one of them on a 400-row document:

| rows returned of 400 | runs | documents | mode | cells compared per run | what the run reports |
|---|---:|---|---|---:|---|
| **0** | 1 | `wide_400` | simple | 0 | `extraction_incomplete` |
| **2** | 2 | `med_narrow` | simple | 0 | `extraction_list_field_not_a_list` (new in v0.6.13) and `extraction_validation_failed` |
| **10** | 12 | `med_narrow`, `wide_400`, `manylists_400` | simple | 20 | `extraction_rows_below_ocr_estimate` on 6; **nothing** on 6 |
| 162 | 1 | `wide_400` | advanced (`integrated`) | 324 | `extraction_row_count_unverified` (`info`) |

So "cell accuracy 1.0000" on those runs means **100% correct on the 10 rows it returned out of
400**. Per-document recall is **0.6900 on `med_narrow` (10,480 cells), 0.7108 on `wide_400`
(10,804) and 0.7947 on `manylists_400` (12,080)**, against 1.0000 on every smaller document and
on the 800-row one. On a 400-row list this model is the **worst** arm in the table.

**Detection improved and is still incomplete, and the gap is structural.** The two 2-row runs
are the prose-refusal shape the v0.6.12 edition found (#1505), and this release's new
`extraction_list_field_not_a_list` issue catches both of them at `error` severity. What stays
silent are **six 10-row runs**: four on `wide_400`, whose OCR tables are 8 columns wide against
a 3-property item schema, and two on Textract LAYOUT, which renders no tables. In both cases
there is no exact-width table to check a row count against, and 10 returned rows is under the
30-row floor below which even `extraction_row_count_unverified` does not fire. A short list on
a document without matching tables is reported by nothing.

**Recommendation: do not select Claude Sonnet 5.5 for list extraction on documents of a few
hundred rows until #1494 is understood.** It is sound on the smaller documents and, in this
edition, on the 800-row one (13 of 13 simple-mode runs complete). Three further properties of
the model make its remaining columns non-comparable, independent of the above:

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
‡ Sonnet 5.5's $63.06 is priced at its published rates, so it is a true measurement; but the
Sonnet 5 rows it would be compared with are 1.5× high (§), so the gap to Sonnet 5 in this table
is a pricing-table artefact and **not** evidence that Sonnet 5.5 is a third cheaper.
§ Sonnet 5 and Sonnet 5 `:1m` are priced in `pricing.yaml` at 1.5× their published rates, so
these two figures are **upper bounds**; any premium of another arm over them is a lower bound.
◊ GPT-6 Astra's $188.81 is a lower bound — it moves to a long-context band above 272,000 input
tokens that `pricing.yaml` cannot express — so the one cost comparison drawn against it above
is a one-sided bound.
`wall/doc` is the mean of each run's end-to-end `wall_s` (OCR, classification, extraction and
the confidence pass together), populated on all 133 rows of every arm. The arms ran in
parallel lanes on one stack, so wall-clock carries load from the other lanes; compare it within
this table rather than with other sections.

**The cheap end does not fail, it silently under-returns**, which is the more dangerous shape:

| extraction model (`simplegrid`) | runs | completed | mean recall | mean cell accuracy | `cells_compared` | grid cost | % confidence leaves < 0.9 |
|---|---:|---:|---|---|---:|---|---|
| `simplegrid` control (Sonnet 4.6) | 63 | 63 | 1.0000 | 0.9994 | 39,690 | $31.80 | 0.010% |
| Amazon Nova Pro | 63 | **63** | **0.5054** | 0.9324 | 7,764 | $10.78 | 0.058% |
| Amazon Nova Lite | 63 | **63** | **0.4964** | 0.7702 | 8,514 | $8.71 | 0.706% |

Both Nova arms completed **every run with no error** and returned **about half the rows**.
Nova Pro's 0.9324 cell accuracy is computed over 7,764 cells against the control's 39,690 — it
is "93% accurate on the rows it chose to return", not 93% accurate. On the three documents of
100 rows or fewer both return every row, but not every value: cell accuracy is 0.8867 and 0.98
for Nova Pro on the two 100-row documents and 0.8756 and 0.8806 for Nova Lite. Above 100 rows
both return between none and 42% of the list, `COMPLETED` every time.

⚠️ **And the confidence signal does not catch it.** Nova Lite flags **0.706%** of its
confidence leaves and Nova Pro **0.058%**, on arms that returned about half the rows. There is
no safety net in the model's own confidence here; the completeness signals in `extraction_*`
are what catch it. **Neither model is viable for list extraction above 100 rows, and neither is
accurate on values at 100**; their dollar figures can be priced only against this matrix's own
Sonnet 4.6 control ($31.80 over 39,690 cells), never against the seven-arm grid.

Two value statements follow from the seven-arm grid, both anchored to its own numbers and their
denominators.

**Claude Opus 5 is dominated by Opus 5.5 on this corpus.** Same completeness, same accuracy,
same denominator — and 19% dearer ($128.52 against $107.67) and slower. There is no profile in
this grid where Opus 5 is the better pick.

**Claude Haiku 5.5 matches the default's per-cell accuracy and beats its recall, and its cost
cannot be quoted.** At 0.9995 cell accuracy over 73,106 cells it is indistinguishable from
Sonnet 5's 0.9999 over 69,042, and its recall is higher (0.9427 against 0.9307) — but both
fall short of the Opus arms for the same reason, simple-mode truncation on the 17-page
document, and Haiku 5.5 also drops to 0.95 on two of the three 400-row documents. It carries the
highest share of low-confidence leaves of the seven, 0.025%, which is still far too low to be a
usable alert rate. Its $25.68 is the cheapest figure in the table and a **lower bound** (†), so
no cost ratio for it appears in this paper. Choose it for accuracy parity on the rows it does
return, never on a quoted saving.

> ⚠️ **The v0.6.8 block below is superseded for the model *ranking*, and is retained only for
> its per-document-size detail.** Every swept model has a v0.6.13 row in the tables above, so
> for *which model to pick* read those, and §5.5 for the per-document breakdown on this release.
> What the v0.6.8 grid still carries is its separate simple-mode and advanced-mode tables per
> document.
>
> **Do not mix rows across the two generations.** They were produced on different releases and
> with a different evaluation comparator (`stickler-eval` 0.5.0 → 1.0.0 infers comparators per
> field), so a v0.6.8 accuracy is not like-for-like with a v0.6.13 one, and the dollars are from
> a different `pricing.yaml`. Claude Opus 5.5, Claude Haiku 5.5 and Claude Sonnet 5.5 have no row
> below at all.
>
> ⚠️ **Where a per-document claim below disagrees with the v0.6.13 grid, the grid is the current
> result.** Finding 6 and the simple-mode table record GPT-6 Astra returning an **empty
> response** on the 800-row `large_narrow` document in every simple-mode cell. On v0.6.13 it
> returned all 800 rows on 11 of its 13 simple-mode runs there and 799 on a twelfth, with one
> empty response — so the shape still occurs, on one draw in 13 rather than every time. (One of
> the 800-row runs then failed in the confidence pass, #1678.)

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
   1.000. At +23% (simple) / +41% (advanced) it is a price for consistency on long lists in
   simple mode, not for accuracy.
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
reportable answer. All three suites ran on v0.6.13.

| suite | the pair, and what it asks | runs | completed | mean recall | mean cell accuracy | `cells_compared` | suite cost |
|---|---|---:|---:|---|---|---:|---|
| `opus55value` | Opus 5.5 against Opus 5 — is the newer model cheaper for the same result? | 40 | **40** | **1.0000** | **1.0000** | 20,000 | $23.89 |
| `astravalue` | Sonnet 5 against GPT-6 Astra — does a larger window buy data? | 100 | **100** | 0.8911 | 1.0000 | 71,074 | $143.38 ◊ |
| `astracap` | both models at the 66-page / 3,200-row ceiling, 3 repeats | 18 | **12** | 0.6667 | 1.0000 | 76,800 | $149.11 ◊ |

Mean confidence over the premium study is 0.9927 on `opus55value`, 0.9897 on `astravalue` and
0.9790 on `astracap`, and almost nothing is scored low anywhere in it: about **5 of 30,272**
confidence leaves fall below 0.9 on `opus55value`, **67 of 114,758** on `astravalue` and **0 of
102,604** on `astracap`. A review queue fed by a 0.9 threshold would see essentially nothing
from any of these arms — including, as the Astra columns below show, from the runs that
returned no usable row at all.

◊ Both Astra-bearing suite totals are **lower bounds** and carry no cost *ratio*, for the reason
given in the header, and the Sonnet 5 arms inside them are **upper bounds**. Where this section
compares the two on cost it states two dollar figures from the same suite and a **one-sided
bound** over them; both biases push the same way, so "Sonnet 5 is at least X% less" is safe.

⚠️ **`astracap`'s six incomplete runs are the measured outcome, not a failure of the run.** All
six are the 66-page `scale_3200.pdf` in a **simple-mode** cell — three repeats of Sonnet 5 and
three of Astra — refused as too long for one request, nothing attempted and nothing wrong
returned. Read the suite's 0.6667 mean recall with that in front of it: it is six deliberate
refusals averaged in with twelve runs that did execute, every one of which returned 3,200 of
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
| Opus 5 · simple | 1.0000 · 1.0000 · $0.26 | 1.0000 · 1.0000 · $0.97 | 5,000 | $6.16 |
| **Opus 5.5 · simple** | 1.0000 · 1.0000 · **$0.22** | 1.0000 · 1.0000 · **$0.86** | 5,000 | **$5.42** |
| Opus 5 · advanced | 1.0000 · 1.0000 · $0.29 | 1.0000 · 1.0000 · $0.95 | 5,000 | $6.18 |
| Opus 5.5 · advanced | 1.0000 · 1.0000 · $0.23 | 1.0000 · 1.0000 · $1.00 | 5,000 | $6.14 |

(each document column: `recall · cell accuracy · cost/doc`, means of 5. `cells_compared` and
`arm cost` are totals over that arm's 10 runs)

**Quality is a tie at the ceiling, on identical denominators.** All 40 runs reached `COMPLETED`,
every one returned every row, and every returned value was right: recall 1.0000 and cell
accuracy 1.0000 over **10,000 cells compared per model**. So the pair reduces to price, and both
models are flat-rate: **$11.56 against $12.34 over the same 10,000 cells — 6% less** (v0.6.12:
10% less).

**The saving lands in simple mode, and on the agentic path it is gone.** Simple mode is $5.42
against $6.16, **12% less**; advanced mode is $6.14 against $6.18, **under 1%**. Simple mode is
one large request, so the cheaper input rate reaches the whole bill; advanced mode re-sends a
prompt prefix per shard, which is where the 0.05× cache read was expected to compound, and it
measurably does not. The predicted −20% is therefore the optimistic end: a deployment whose
documents go through advanced mode should budget no saving. The whole-grid figure in §5.1 (16%
less) is larger because that grid includes the 17-page document, where simple mode dominates
the bill.

⚠️ **In this suite Opus 5.5 was the slower model on the 9-page document** — 560 s against 336 s
in simple mode and 508 s against 175 s in advanced, means of 5 — the opposite of §5.1's grid,
where it was the faster. The suite ran in a parallel lane, so wall-clock here carries load and
does not settle which is faster; the cost and accuracy figures are unaffected by load.

Both arms resolve to the same `reasoning_effort` in the committed cell configuration (`low` on
all four cells), so the pair is matched on that axis as it ran.

#### Sonnet 5 compared with GPT-6 Astra: does the larger context window extract more?

Four documents chosen so that the window is the variable. `small_narrow` (3 pp) and
`med_narrow` (9 pp) fit both models; `large_narrow` (17 pp, ~203K tokens) is the first document
Sonnet 5 cannot reliably take in one request; `dense_250` (26 pp, ~304K, 8 columns, 4
interleaved lists, OCR noise) is the one Astra's window reaches most comfortably. 5 repeats per
cell, 100 runs, **100 completed**.

| cell | 3 pp / 100 rows | 9 pp / 400 rows | **17 pp / 800 rows** | **26 pp / 250 rows, dense** |
|---|---|---|---|---|
| Sonnet 5 · simple | 1.0000 · 1.0000 · $0.20 (1,000) | 1.0000 · 1.0000 · $0.71 (4,000) | **0.8230** · 1.0000 · $1.73 (6,584) | 1.0000 · 1.0000 · $1.38 (2,500) |
| Astra · simple | 1.0000 · 1.0000 · $0.35 (1,000) | 0.9995 · 1.0000 · $1.16 (3,998) | **0.9995** · 0.9999 · $2.82 (7,996) | **0.0000** · *not scored* · $1.28 (0) |
| `global.` Astra · simple | 1.0000 · 1.0000 · $0.32 (1,000) | 1.0000 · 1.0000 · $0.90 (4,000) | **0.9995** · 1.0000 · $2.26 (7,996) | **0.0000** · *not scored* · $1.01 (0) |
| **Sonnet 5 · advanced** | 1.0000 · 1.0000 · $0.21 (1,000) | 1.0000 · 1.0000 · $0.68 (4,000) | **1.0000 · 1.0000 · $1.33** (8,000) | **1.0000 · 1.0000 · $1.68** (2,500) |
| Astra · advanced | 1.0000 · 1.0000 · $0.43 (1,000) | 1.0000 · 1.0000 · $1.78 (4,000) | 1.0000 · 1.0000 · $4.34 (8,000) | 1.0000 · 1.0000 · $4.13 (2,500) |

(each cell: `recall · cell accuracy · cost/doc`, means of 5, with that cell's `cells_compared` in
parentheses. *not scored* means no row matched a truth key, so there was no cell to grade)

**Verdict: still no.** Read it column by column.

- **Where both models fit (3 and 9 pages)** both are at or within one row of ceiling, and Astra
  costs more in every cell.
- **On the 17-page document the window still buys data in simple mode, by less than it did.**
  Astra returns 800 rows on 8 of its 10 simple runs across the two endpoints and 798 on the
  other two. Sonnet 5 returned all 800 rows on **4 of 5** runs and 92 on the fifth (v0.6.12: 2 of 5),
  and the short run carries `extraction_rows_below_ocr_estimate`, so it is flagged rather than
  silent.
- **On the 26-page document the window buys nothing, and it fails without a usable signal.**
  Sonnet 5 in simple mode returns all 250 rows in 5 of 5 at cell accuracy 1.0000 over 2,500
  cells. Astra returns **no row that matches a truth key** — 0 of 250, all 250 truth ids recorded
  as gaps, no cell scored, status `COMPLETED` — on both the `us.` and the `global.` endpoint, 5 of
  5 each. The run carries **757 confidence leaves, exactly the count the complete runs carry**,
  mean confidence 0.99 and coverage 1.000. Its one processing issue is the new
  `extraction_row_count_unverified`, and ⚠️ **that is not a detection**: the same `info` issue
  appears on all ten Sonnet 5 `dense_250` runs, which returned every row, because this
  document's 8-column tables do not match the item schema's width. Nothing distinguishes the
  empty Astra result from a complete one.
- **Advanced Sonnet 5 does the job on all four documents** — recall 1.0000 and cell accuracy
  1.0000 everywhere — and it is the cheapest arm that does. Over the two advanced arms' identical
  **15,500 cells** it is **$19.46 against Astra's $53.38, at least 63% less**. The accuracy
  gained per extra dollar is zero in every cell.

**What Astra is measurably good at here:** a complete 800-row list in *simple* mode more often
than Sonnet 5, cell accuracy 1.0000 on the agentic path at every size, a `global.` endpoint that
cost less than `us.` on the same documents ($22.41 against $28.05 over essentially the same
12,996 cells), and implicit prompt caching with no `<<CACHEPOINT>>` markers. What it is not, on
this evidence, is a reason to pay for extraction that advanced mode already gets right at a
fraction of the price.

#### Largest document tested: 66 pages, 3,200 rows

| cell | repeats 0 · 1 · 2 (`recall · cell accuracy`) | conf coverage | `cells_compared` (total) | $/doc |
|---|---|---|---:|---|
| Sonnet 5 · simple | **FAILED** · **FAILED** · **FAILED** (too long) | — | 0 | $1.05–1.06 |
| Astra · simple | **FAILED** · **FAILED** · **FAILED** (too long) | — | 0 | $1.05 |
| **Sonnet 5 · advanced, default shards** | **1.0000 · 1.0000** ×3 | **1.000** ×3 | 19,200 | **$5.58 · $5.20 · $4.95** |
| Astra · advanced, default shards | 1.0000 · 1.0000 ×3 | 1.000 ×3 | 19,200 | $24.17 · $19.81 · $17.57 |
| Sonnet 5 · advanced, 25-page "wide" shards | 1.0000 · 1.0000 ×3 | **0.22 · 0.78 · 0.65** | 19,200 | $18.12 · $11.71 · $11.37 |
| Astra · advanced, 25-page "wide" shards | 1.0000 · 1.0000 ×3 | 0.999 · 0.999 · 0.994 | 19,200 | $9.08 · $7.73 · $7.49 |

Three results, in the order they matter.

**Simple mode refuses at this size, and refusing is the right behaviour.** All six simple-mode
runs are turned away up front as too long for one request — on Astra as on Sonnet 5, although
Astra's nominal window is 1,050,000 tokens, so the nominal figure is not the usable one once page
images are in the request. Nothing is attempted and nothing wrong is returned: the whole $1.05 is
the $0.99 of OCR and $0.06 of classification that preceded the refusal.

**Both models hold the ceiling on the agentic path at default shards:** 3,200 of 3,200 rows at
cell accuracy 1.0000 with every row confidence-scored, on all six runs. Sonnet 5 costs **$4.95–5.58**
against Astra's **$17.57–24.17** on the same document, so Sonnet 5 is **at least 68% less** — a
bound, not a ratio. At the ceiling the premium model buys nothing and costs several times as much.

⚠️ **The 25-page "wide" shard no longer loses rows, and on Sonnet 5 it loses the confidence
instead.** At v0.6.12 three of its four runs came back with 1,982–2,416 of 3,200 rows. On v0.6.13
all six return 3,200 of 3,200 at cell accuracy 1.0000 — and all six carry `assessment_incomplete`.
On Astra that is a small gap (99.4–99.9% of rows scored) and the wide shard costs **about 40% of
Astra's default shard** on the means ($8.10 against $20.51). On Sonnet 5 it is a large gap —
**22%, 78% and 65%** of rows scored — at **2.2–3.5× the mean price** of Sonnet 5's default shard
($11.37–18.12 against $5.25). **Leave `shard_pages` at its default on Sonnet 5**; on Astra the wide
shard is the cheaper Astra configuration, and it is still dearer than default-shard Sonnet 5 on
every run ($7.49–9.08 against $4.95–5.58).

### 5.3 Classification model

The v0.6.7 edition recommended Claude Haiku 4.5 for classification because Nova 2 Lite
over-split every document 2–3×. That was a prompt defect, fixed in #817, and the axis has been
re-measured with the fixed prompt ever since. On v0.6.13 the axis is **Nova 2 Lite · Claude Haiku
5.5 · Claude Sonnet 5**, each over the full 19-cell × 7-document `coresynth` grid at the control
extraction model (Sonnet 4.6), 133 runs per arm, with one section the truth for every document.

| classification model | runs | completed | sections correct | mean recall | mean cell accuracy | `cells_compared` | classification $/doc | total $/doc | grid cost |
|---|---:|---:|---:|---|---|---:|---|---|---|
| **Nova 2 Lite** *(shipped default)* | 133 | **133** | **133 / 133** | **1.0000** | 0.9997 | 83,790 | **$0.0070** | **$0.670** | **$89.10** |
| Claude Haiku 5.5 | 133 | **133** | **133 / 133** | **1.0000** | 0.9998 | 83,790 | $0.0086 † | $0.699 † | $92.98 † |
| Claude Sonnet 5 | 133 | **133** | 131 / 133 | **1.0000** | 0.9998 | 83,790 | $0.1901 § | $0.854 § | $113.59 § |

† Haiku 5.5's dollar figures are **lower bounds** — banded pricing, see the header. § Sonnet 5's
are **upper bounds** — its `pricing.yaml` rows are 1.5× high.

**All three arms are graded on the identical 83,790 cells and complete every run**, so these rows
need no denominator caveat and the differences between them are as small as they look.

**Guidance: keep Nova 2 Lite. Upgrading the classification model buys nothing measurable.** The
classifier is at its ceiling on this corpus: the boundary is right on 133 of 133 runs under the
default and under Haiku 5.5, and **131 of 133 under Sonnet 5**, which split `manylists_400` in two
on two cells — so the premium arm is the only one that got a boundary wrong. Downstream recall is
1.0000 in all three and cell accuracy differs in the fourth decimal.

What the upgrade does change is the bill. Sonnet 5's classification phase costs **$0.1901 per
document against the default's $0.0070 — 27×** — which lifts the whole-grid cost from **$89.10 to
$113.59**. Because the Sonnet 5 rows are priced 1.5× high, the true premium is smaller than that
27% grid difference, but it is not small: classification is about a fifth of the Sonnet 5 arm's
per-document bill against about 1% of the default's. Haiku 5.5's grid is $3.88 above the
default's, of which its classification phase accounts for about $0.21; the rest is run-to-run
variation in the other phases, not a classifier cost.

The one boundary shape the default still gets wrong — a reprinted running header, §7 — is not a
model problem either (§7's boundary table is Nova 2 Lite; the v0.6.7 probe measured Haiku 4.5 and
Sonnet 5 failing the same shape). Where classifiers *do* differ is the confidence score they emit:
the v0.6.7 [classification-confidence study](studies/classification-confidence.md) measured Haiku
4.5's calibration separation at 0.207 against Nova 2 Lite's 0.044 on DocSplit-Poly-Seq, and
§5.6's re-measurement of Nova 2 Lite on that corpus reads 0.033 at the shipped default — so if
you *act* on classification confidence, read that study. Haiku 4.5 itself is not in the sweep;
`--set classification_model=haiku45` reproduces it on demand.

### 5.4 Confidence model

Re-measured in full on v0.6.13 — the whole 19-cell × 7-document `coresynth` grid, 133 runs per
model, extraction held at the control (Sonnet 4.6):

| confidence model | runs | completed | mean recall | mean cell accuracy | `cells_compared` | mean confidence | leaves below 0.9 | coverage | assessment $/doc | total $/doc | grid cost | wall |
|---|---:|---:|---|---|---:|---|---|---|---|---|---|---|
| **Nova Lite** *(shipped default)* | 133 | **133** | 1.0000 | 0.9997 | 83,790 | 0.9956 | **0** of 119,921 | 0.9999 | **$0.099** | **$0.676** | **$89.97** | 181 s |
| Nova 2 Lite | 133 | **133** | 1.0000 | 0.9997 | 83,790 | 0.9982 | ~**2** of 119,955 | **1.0000** | $0.149 | $0.716 | $95.24 | **145 s** |
| Claude Sonnet 5 | 133 | **133** | 1.0000 | 0.9997 | 83,790 | 0.9749 | ~**285** of 119,968 | **1.0000** | $0.923 § | $1.502 § | **$199.82** § | 148 s |

§ upper bounds — Sonnet 5's `pricing.yaml` rows are 1.5× high.

**All three arms completed every run on the identical 83,790 cells at identical recall and cell
accuracy**, so on this corpus the confidence model changes nothing about what is extracted; the
columns that differ are the score itself, its cost and its wall-clock. The low-confidence column
is given as a **count of leaves** rather than a share, because the counts are what decides
whether a review queue is worth having.

⚠️ **Confidence *discrimination* cannot be measured on this corpus, so no AUROC column is
published.** The extraction is too accurate for it: across ~79,400 calibration observations per
arm there are only **38 to 42 errors — an error rate of about 0.05%** — and
`calibration_reliable` is **true on only 18 of 126 scored runs** in both Nova arms and 24 of 126 in
the Sonnet 5 arm. On every reliable Nova run the AUROC is undefined, because the run has no error
to rank; Sonnet 5 reports one on four runs, ranging 0.58 to 1.00. Neither supports a conclusion
about confidence quality. **On this corpus the choice rests on cost, coverage and threshold
behaviour.**

**Guidance: keep Nova Lite, and read the low-confidence column before changing it.** The default
is the cheapest at **$0.676 per document**, its coverage is 0.9999, and nothing measurable here
distinguishes its confidence *quality* from the alternatives. **Nova 2 Lite is +5.9%** ($95.24
against $89.97 over the same grid) for complete 1.0000 coverage and a faster wall (145 s against
181 s). Take it if those matter.

**Sonnet 5 as the confidence model is not an accuracy upgrade. It is a far more conservative
scorer, and that is the whole of what it buys.** It scores about **285 of 119,968** leaves below
0.9, against the default's **0** and Nova 2 Lite's **~2**, for **$199.82 against $89.97** over the
same grid, almost all of it in the assessment phase ($0.923 per document against $0.099). Those
Sonnet 5 dollars are upper bounds, so the true premium is below the 2.2× the table shows, but the
assessment phase is still several times the default's. Choose the confidence model and the review
threshold together: on either Nova arm a 0.9 threshold is close to inert on this corpus, and on
Sonnet 5 it is the one setting that actually populates the confidence-alerts filter.

**Whether that conservatism is worth paying for depends on whether your corpus has anything wrong
in it**, and the one grid cell that does is Bedrock-LLM OCR (`core-llm-simple-sep`, 7 runs per
arm), where about 0.5% of cells are wrong:

| confidence model | cell accuracy on that cell | `cells_compared` | mean confidence on that cell | leaves below 0.9 |
|---|---|---:|---|---|
| Nova Lite *(default)* | 0.9951 | 4,410 | 0.9977 | **0** of 6,664 |
| Nova 2 Lite | 0.9947 | 4,410 | 1.0000 | **0** of 6,664 |
| **Claude Sonnet 5** | 0.9951 | 4,410 | **0.9605** | **155** of 6,664 |

Two things follow, and they point in opposite directions. **The two cheap confidence models are
blind here**: with about 0.5% of cells wrong they score everything at 0.998–1.000 and flag
nothing, so a review queue fed by them would have caught none of the corrupted identifiers.
**Sonnet 5 does see them** — 155 of 6,664 leaves on that cell, against 285 across the whole grid,
so more than half of everything it flags anywhere is on the one cell that has errors to find.
That is the right direction, and it is the only positive evidence in this edition that any of the
three scores wrongness rather than just scoring low. It does not establish that the 155 flagged
leaves are the wrong ones; that needs a per-leaf join this cell's sample is too small to carry.

So: **Nova Lite is right where the extraction is trustworthy and the score is for after-the-fact
triage.** If the score gates human review *and* the corpus produces wrong values, Sonnet 5 is the
only confidence model in this grid that measurably lowered its score on them, at several times
the assessment cost. Measure the separation on your own error-bearing sample before paying that.

### 5.5 Recommendations by document profile

**The headline of this edition's model axis is the same as the last: on a large document the
*mode* matters more than the model.** Measured on `large_narrow` (800 rows of ground truth, 17
pages), **all seven `coresynth` arms return 800 of 800 rows in advanced mode** — 6 of 6 runs each,
recall 1.0000 and cell accuracy 1.0000 over 9,600 cells per arm. In the thirteen simple-mode cells
the same seven split into two groups, and the shipped default is in the wrong one:

| arm | simple mode (13 cells) | advanced mode (6 cells) |
|---|---|---|
| **Claude Opus 5.5** | 13/13 completed, **800 / 800 (1.0000)**, 20,800 cells, acc 1.0000, 725 s | 6/6, 800/800, 9,600 cells, acc 1.0000, 257 s |
| Claude Opus 5 | 13/13 completed, **800 / 800 (1.0000)**, 20,800 cells, acc 1.0000, 983 s | 6/6, 800/800, 9,600 cells, acc 1.0000, 252 s |
| Claude Sonnet 5.5 | 13/13 completed, **800 / 800 (1.0000)**, 20,800 cells, acc 1.0000, 647 s | 6/6, 800/800, 9,600 cells, acc 1.0000, 260 s |
| GPT-6 Astra | 12/13 completed, ~738 / 800 (0.9230), 19,198 cells, acc **0.9583** | 6/6, 800/800, 9,600 cells, acc 1.0000 |
| Claude Sonnet 5 `:1m` | 13/13 "succeeded", ~520 / 800 (0.6502), 13,524 cells, acc 1.0000 | 6/6, 800/800, 9,600 cells, acc 1.0000 |
| Claude Haiku 5.5 | 12/13 completed, ~447 / 800 (0.5591), 11,630 cells, acc 1.0000 | 6/6, 800/800, 9,600 cells, acc 1.0000 |
| **Claude Sonnet 5** *(shipped default)* | 13/13 "succeeded", **~233 / 800 (0.2909)**, 6,052 cells, acc 1.0000 | 6/6, 800/800, 9,600 cells, acc 1.0000, 312 s |

(the two Nova arms are not in this table: they ran the smaller `simplegrid` matrix and neither
finishes the agentic path, #895 — see §5.1. Wall-clock is given where it informs a choice.)

⚠️ **The arms at the bottom do not fail on this document — they truncate, and report success.**
Sonnet 5 returns 43, 92 or 141 rows on 10 of its 13 simple-mode runs, `:1m` on 5 of 13 and Haiku
5.5 on 6 of 13 (one at 0 rows), each `COMPLETED`, and the `cell_accuracy` column stays at 1.0000
**because it is computed on the rows they did return** — the denominators are 6,052 to 13,524
cells against the complete arms' 20,800. The truncated runs do carry
`extraction_rows_below_ocr_estimate`, so a deployment reading processing issues sees them; one
reading status does not. **Rank by completeness, never by completion rate, and never by an
accuracy figure without its denominator.**

With that in front of it, the per-profile answer. "Cheapest arm at ceiling" means the cheapest arm
that reaches recall 1.0000 *and* cell accuracy 1.0000 on that profile's documents, on a
denominator stated in the same cell, among arms whose cost can be quoted.

| Document profile | Cheapest arm at ceiling | Mode | Measured basis | Notes |
|---|---|---|---|---|
| Small forms, ≤ 1 page, few fields | **any model, Nova Lite included** | simple | `tiny_form`: all seven `coresynth` arms 19/19 at recall 1.0000, cell accuracy 1.0000 over 190 cells (Astra 0.9737); both Nova arms 9/9 at 1.0000 / 1.0000 over 90 cells on the `simplegrid` matrix | Genuinely free. Nova Lite at $0.013/doc and the default at $0.058/doc (an upper bound) produce the same result; pick on price |
| Statements ≤ ~100 rows / ≤ 4 pages | **the shipped default** (Sonnet 5) | simple | `small_narrow` and `longdesc_100`: Sonnet 5 at recall 1.0000, cell accuracy 1.0000 over **3,800 cells each**, $0.22 and $0.37/doc (upper bounds). Every Claude arm and Astra's recall are at 1.0000 here | **Not** the cheap end: Nova Lite and Nova Pro return every row and get 2–12% of values wrong (§5.1). Haiku 5.5 and Sonnet 5.5 are also at 1.0000 / 1.0000 over 3,800 cells each; Haiku 5.5's cost cannot be quoted as a saving, and Sonnet 5.5 is unsafe one size up |
| Statements ~400 rows / ~9 pages | **the shipped default** | simple, or advanced | `med_narrow`, `wide_400`, `manylists_400`: Sonnet 5 19/19 each at recall 1.0000 over 15,200 cells, cell accuracy 0.9994–0.9999, $0.75–1.04/doc (upper bounds). Opus 5.5 is 1.0000 / 1.0000 over the same 15,200 at $0.86–1.19/doc | ⚠️ **Do not use Claude Sonnet 5.5 here** — recall 0.69–0.79, six of its short runs unreported (§5.1, [#1494](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1494)). Haiku 5.5 dips to 0.95 on two of the three |
| **Statements ~800 rows / ~17 pages** | **any model — but in `advanced` mode** | **advanced** | `large_narrow`: all seven arms 6/6 at 800/800 rows, 9,600 cells, accuracy 1.0000 in advanced; four of the seven fall short in simple | **This is the configuration decision, not a model decision.** In simple mode the default returned the whole list on 3 of 13 runs |
| 1,000+ rows / 25–66 pages | **the shipped default, advanced, at `shard_pages: default`** | **advanced** | `astracap` on 3,200 rows / 66 pages: Sonnet 5 3/3 at recall 1.0000, cell accuracy 1.0000 over 19,200 cells and full confidence coverage, for **$4.95–5.58 against Astra's $17.57–24.17** per document. §3's `scaling` series holds 1.000 / 1.000 with full coverage through the same ceiling at the control model | Simple mode **refuses** at 66 pages, and at 25–33 pages returns every row with **up to 81% unscored** (§3). ⚠️ **Do not widen the shard on Sonnet 5**: the 25-page shard returned every row but scored only 22–78% of them, at 2.2–3.5× the price (§5.2) |

**Where a premium model is actually required: nowhere in this corpus, once advanced mode is set.**
The one thing the premium end buys is a complete 800-row list in *simple* mode, where Opus 5.5,
Opus 5 and Sonnet 5.5 reach 1.0000 on 13 of 13 — and advanced mode gets all seven arms there at
9,600 cells and accuracy 1.0000. If simple mode is a hard constraint on a document that size,
**Opus 5.5 is the arm to pick**: complete and exact on every document in the grid, cheaper and
faster than Opus 5 on that document (725 s against 983 s), where Sonnet 5.5 is complete at 800
rows but unusable at 400.

**Over the grid as a whole the answers are two, and they differ by at least 27%.** **Claude Opus
5.5 is complete and exact on every document of the grid in both modes** — recall 1.0000 and cell
accuracy 1.0000 over **83,790 cells** — for **$107.67**, which is **at least 43% below GPT-6
Astra's $188.81** and **at least 27% above the shipped default's $84.74** (both directions are
bounds, because Astra's figure is a lower bound and Sonnet 5's an upper one). The default over the
same grid reaches recall 0.9307 over 69,042 cells, all of the gap being simple mode on the 17-page
document.

So, plainly. **Keep the shipped default and turn on advanced mode for documents above ~400 rows**
is right on cost, and it is what the per-profile table recommends everywhere. **Pay at least 27%
more for Opus 5.5** where a complete, exactly-correct list in either mode is worth it — it is the
cheapest of the two arms in this edition that delivers one on every document in the grid, and the
other (Opus 5) costs 19% more for the same result. Nothing above those two is worth buying: the
most expensive arm in the sweep is beaten on completeness and on cell accuracy by one costing at
least 43% less.

### 5.6 Classification granularity, modality and strategy

**This is the first measurement of these settings.** The v0.6.12 edition published no figure for
them, and the guidance in [Classification](../classification.md#how-the-classification-settings-are-organised)
was reasoned from token arithmetic. `benchmarks/harness/run_classification_bench.py --variants`
ran seven named combinations on the first 20 packets of `DocSplit-Poly-Seq` (RVL-CDIP-derived, 13
classes, 298 pages, 5–20 pages per packet), with Nova 2 Lite — the shipped classification default
— on every call and `topk` class confidence (3 candidates). One run per variant per packet.
Classification only: these packets are not extracted, so nothing here is about extraction
accuracy or cost.

| variant | granularity | sees | strategy |
|---|---|---|---|
| `page-both` *(the shipped default)* | page | text + images | `combined` |
| `page-text` | page | text | `combined` |
| `packet-both` | packet | text + images | `combined` |
| `packet-text` | packet | text | `combined` |
| `packet-images` | packet | images | `combined` |
| `split-then-classify-packet` | packet | text + images | `split_then_classify` |
| `split-then-classify-page` | page | text + images | `split_then_classify` |

Three metrics, two of them new in this release. **`class_accuracy`** is the share of pages given
their true class. **`boundary_accuracy`** is the share of true sections whose page range the
variant reproduced, whatever class it gave them, and **`label_accuracy`** is the share of those
boundary-matched sections that got the right class; the product `split_accuracy_without_order`
is exactly `boundary × label`. `label_accuracy` is undefined on a packet where no boundary
matched, so its document count can be lower than the other two.

⚠️ **Read the failures first, because they decide what the means are over.** Five of the seven
variants scored all 20 packets, and two did not, each for its own reason:

- **Every packet variant that sends page images failed the same three packets** — 18, 19 and 20
  pages — on all three of `packet-both`, `packet-images` and `split-then-classify-packet`. Nova 2
  Lite answers `Input is too long for requested model` for the single packet window, so the
  document fails
  ([#1676](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1676)).
  `packet-text`, which sends no images, completed those three. So on this model the packet-level
  image window has a ceiling somewhere below 18 pages, and a deployment with longer packets would
  see those documents fail, not degrade.
- **`page-text` failed two packets** (19 and 20 pages) because a page with no OCR text fails the
  whole document at text-only page granularity
  ([#1677](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1677)).
  The variants that also send images classify that page from its image.

So the comparison below is over the **15 packets every variant scored**; the all-documents
figures each variant scored are in the committed summary.

| variant | packets failed (of 20) | class accuracy | boundary accuracy | label accuracy | calibration separation | classification $/page | $/document |
|---|---:|---:|---:|---:|---:|---:|---:|
| `page-both` *(default)* | 0 | 0.851 | **0.446** | 0.914 | 0.033 | $0.00095 | $0.0141 |
| `page-text` | 2 | 0.759 | 0.477 | 0.869 | 0.035 | $0.00094 | $0.0140 |
| `packet-both` | **3** | 0.775 | **0.821** | 0.846 | 0.042 | $0.00039 | $0.0059 |
| `packet-text` | 0 | 0.783 | 0.772 | 0.840 | 0.070 | $0.00035 | $0.0052 |
| `packet-images` | **3** | **0.909** | 0.711 | **0.920** | **0.077** | **$0.00022** | **$0.0033** |
| `split-then-classify-packet` | **3** | 0.813 | 0.741 | 0.897 | 0.002 | $0.00061 | $0.0092 |
| `split-then-classify-page` | 0 | 0.813 | 0.658 | 0.911 | 0.002 | $0.00097 | $0.0145 |

(accuracy columns: means over the 15 common packets. Calibration separation is
`mean(conf | right) − mean(conf | wrong)` over all pages the variant scored, and the cost columns
are over the documents whose metering read — which excludes a failed document, so a variant's
cost per page is not lowered by the packets it failed)

**What this measures, with n = 20.**

1. **Packet granularity places boundaries far better than the default.** The four packet variants
   reproduce **0.711–0.821** of true section boundaries against **0.446** for `page-both` — 1.6× to
   1.8× — and `split-then-classify-page`, which finds boundaries in a dedicated pass at page
   granularity, sits between them at 0.658. This is the direction the reasoned guidance predicted,
   now measured, and it is the largest effect in the table.
2. **The boundary gain is not free on labels in every variant.** `packet-both` and `packet-text`
   give the right class to fewer of the sections they bound correctly (0.846 and 0.840, against the
   default's 0.914), and their page class accuracy is lower too (0.775–0.783 against 0.851).
   **`packet-images` is the exception: best page class accuracy (0.909), best label accuracy
   (0.920), and 0.711 boundary accuracy** — but it is one of the three variants that failed the
   long packets.
3. **Packet granularity is cheaper per page**, as the token arithmetic said: `packet-images` costs
   23% of the default's classification spend per page, `packet-text` 37% and `packet-both` 41%.
   `split-then-classify-packet` costs 65% of it, because it makes a splitting call and then one
   classification call per section. Classification is a small share of a document's total bill
   on the default model (the v0.6.7 study measured about 3%), so this is a saving on a small line.
4. **`split_then_classify` reports a confidence that does not separate right from wrong.** Its
   calibration separation is **0.002** at both granularities, against 0.033–0.077 for every
   `combined` variant. A score that is the same for right and wrong pages gives a review queue
   nothing to act on. The `combined` packet variants separate best (0.070–0.077), though every
   figure here is small next to the 0.207 the v0.6.7 study measured for Haiku 4.5.

**Guidance, and its limits.** On this corpus packet granularity is the better boundary detector
and the cheaper call; `packet-images` is the best variant on every accuracy column. **Do not adopt
an image-bearing packet variant for packets of 18 pages or more on Nova 2 Lite** until #1676 is
resolved, because those documents fail outright; `packet-text` has no such ceiling here and keeps
most of the boundary gain (0.772). Do not adopt `page-text` on a corpus that can contain blank or
image-only pages until #1677 is resolved. And treat every number in this table as **direction,
not ranking**: 15 packets, one run each, one model, so a single packet can move a variant's mean
by up to 0.067 and the order among the four packet variants is not settled. A second model, repeats, and a corpus with longer packets are what would turn
this into a recommendation to change the shipped default.

---

## 6. Product improvement backlog (surfaced by this study)

Items closed since earlier editions are struck through; the open ones are ordered by how much
data they can cost a customer. Each says whether this edition re-measured it.

1. **🚨 Simple mode still reports `COMPLETED` on a truncated list, and one shape of it is
   reported by nothing (P0, re-measured).** #843 made the common case *visible*:
   `extraction_rows_below_ocr_estimate` is on all ten of the shipped default's truncated
   simple-mode runs on the 17-page document (§5.5), and #1505's
   `extraction_list_field_not_a_list` now catches the prose-refusal shape (§5.1). Neither makes
   it *terminal* by default — that is `extraction.row_shortfall_action: fail`, which §3 explains
   is opt-in for a reason. What is still **silent** is a list truncated below 30 rows on a
   document with no exact-width OCR table — six of Sonnet 5.5's 10-row runs on 400-row
   documents (§5.1) — because both row checks and the new `extraction_row_count_unverified`
   report sit behind the same 30-row floor. A floor on the *expected* rows rather than the
   *returned* rows would close it.
2. **🚨 In simple mode at 25–33 pages the confidence pass now leaves most rows unscored
   (P1, new, re-measured).** The #1488 fix turned a discarded document into a delivered one, and
   §3 measures the price: at 1,200 and 1,600 rows every row is returned and **62–81% of them
   carry `confidence: null`**, reported as `assessment_incomplete`. An unscored row raises no
   confidence alert, so a review queue fed by confidence alerts sees none of them. Advanced mode
   has no such gap at any size measured; either the confidence pass needs to scale to the list
   (the advanced-mode pass costs $0.04–0.08 where the simple-mode one costs $0.70–1.23 at those
   sizes, so the two are not doing the same work), or a simple-mode section of that size should
   be routed to advanced mode.
3. **The `AssessmentStep` memory failure on large documents is down to 2 of 190 and not gone —
   [#1678](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1678)
   (P1, new).** Both remaining failures are `Runtime.OutOfMemory` after a successful extraction
   of the 17-page document (Astra and Haiku 5.5 arms). #1488 bounded the wall clock, not memory,
   and its changelog entry says exactly that.
4. **Packet-level classification with images fails 18–20-page packets on Nova 2 Lite —
   [#1676](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1676)
   (P1, new).** 3 of 20 DocSplit packets on every packet variant that sends images (§5.6). The
   document fails rather than degrading, so the variant with the best measured classification
   accuracy cannot be recommended for long packets until the window is split or bounded.
5. **Text-only page classification fails the whole document on a page with no OCR text —
   [#1677](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1677)
   (P2, new).** 2 of 20 DocSplit packets (§5.6).
6. **`sectionSplitting: disabled` on a packet loses the second document's rows, not just its
   boundary (P2, re-measured).** Merging two statements into one section is the known,
   by-construction cost of `disabled`; the extraction of that merged section also returns only
   the first statement's 20 rows of 40 on 8 of 10 runs on v0.6.13, and on 4 of 5 in the
   committed v0.6.8 grid of the same suite, so this is a standing behaviour rather than a
   regression (§7). A merged packet is therefore a data loss, and nothing reports it.
7. **GPT-6 Astra returns a full-looking result whose every row key is unusable on a 26-page
   document (P2, re-measured).** 10 of 10 simple-mode runs on `dense_250` across both endpoints:
   0 of 250 rows match a truth key, while the run carries 757 confidence leaves, coverage 1.000
   and mean confidence 0.99. Its one processing issue, `extraction_row_count_unverified`, also
   appears on every complete Sonnet 5 run of the same document, so it does not distinguish the
   two (§5.2). Whether the keys are a response-format mismatch or a model behaviour on long
   inputs is not established; what is clear is that the failure needs to be *detectable*.
8. **The 25-page "wide" agentic shard trades confidence for nothing on Sonnet 5 (P2,
   re-measured).** At v0.6.12 it lost up to 38% of the rows; on v0.6.13 it returns every row and
   scores only 22–78% of them, at 2.2–3.5× the default shard's price (§5.2). On Astra the same
   setting is complete, nearly fully scored and cheaper than Astra's default shard, so the
   `max_pages_per_shard` guidance should be per model.
9. **The agent's turn count is not visible in the metering, so agentic cost is a range a
   customer cannot explain (P2, re-measured).** Advanced cells run at CV **7.0–20.1%** at the
   shipped default and up to **25.9%** at the control, against simple mode's 0.0% and 4.4% on the
   same document (§4). The widest single cell at the shipped default ranged **$1.087 → $1.690
   across five identical runs** of one 400-row, one-section document. Bounding it, or surfacing
   it, would make an agentic budget explainable instead of empirical.
10. **Nova Lite cannot run the agentic path —
    [#895](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/895)
    (not re-measured).** The issue is closed; both Nova models ran only the simple-mode
    `simplegrid` matrix in this edition, so nothing here tests the agentic path on them.
11. **A class wrapped with `x-aws-idp-multi-instance` whose instances hold a long list could not
    finish confidence assessment —
    [#894](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/894)
    (closed; not re-measured).** The `multiinstance` suite did not run in this edition, so §7's
    figures for it are v0.6.8's and the fix has no benchmark measurement yet.
12. ~~**The TestRunner rejected any compressed configuration carrying a float (#892).**~~
    **Closed by #893** (`parse_float=Decimal`).
13. ~~**`sectionSplitting: llm_determined` over-split single documents (#726).**~~ **Closed by
    #817**: 131 of 133 single-section documents in §2 at one section, 1 of 1 at every size in §3,
    5/5 on the unpaginated statement in §7's boundary A/B. The reprinted running-header shape is
    still split by the generic prompt (0/5 at both prompts, §7); #750 was closed by a per-class
    `BOUNDARY:` description in the one configuration it was reported on, not by a prompt change.
14. ~~**Forced tool use serialised group attributes to JSON strings on Sonnet 5 (#783).**~~
    **Closed by #794**: valid rate 1.000 on every forced section in §2 and §7.
15. ~~**Integrated confidence + simple extraction returned partial lists.**~~ **Closed by #795**:
    1.000 on 7 of 7 documents and 3 of 3 repeats (§2.1).
16. ~~**The bedrock_llm backend appeared to lose rows (#1500).**~~ **Closed** as a scorer defect:
    with identifiers repaired that cell is at recall 1.000 (§2, finding 4). Its real defect — wrong
    values, 0.995 cell accuracy and 10 of 14 scalar fields — is unchanged.
17. **Reference-corpus cells in the standard release run.** This edition ran them (§2c) at the
    control model only. A sampled variant (5 documents per corpus) at the shipped default would
    restore the two-model comparison in every release audit at a fraction of the price.

---

## 7. Individual configuration options, A/B tested

Each pair below differs on exactly one config key, on the same deployed stack with identical
code, `repeats: 3` or more. Extraction model Sonnet 4.6 (the matrix's control) unless stated;
the point of each is the *delta*, not the absolute. Each subsection says whether it was
re-measured on v0.6.13 (2026-10-31) or carries an earlier edition's figures.

### `extraction.validation` and `extraction.coercion`

**Re-measured on v0.6.13 for bank statements; the `kv_form` rows are carried from v0.6.8**,
because no `kv_form` document has run since.

| arm | corpus | n | recall | cell acc | typed acc | coercions | validation errors | valid rate | cost/doc |
|---|---|---|---|---|---|---|---|---|---|
| `enforce-off` | bank statements (`small_narrow`, `valuenoise_100`) | 6 | 1.000 | 1.000 | — | not recorded | n/a (off) | n/a | $0.1443 |
| `enforce-warn` *(default)* | same | 6 | 1.000 | 1.000 | — | not recorded | **0** | **1.000** | $0.1445 |
| `enforce-warn` | `valuenoise_100` (`enforcecost` suite) | 5 | 1.000 | 1.000 | — | not recorded | 0 | 1.000 | $0.1448 |
| `enforce-escalate` | same | 5 | 1.000 | 1.000 | — | not recorded | 0 | 1.000 | $0.1447 |
| `enforce-off` | `kv_form` (v0.6.8) | 3 | — | — | 1.000 | 0 | n/a | n/a | $0.030 |
| `enforce-warn` | `kv_form` (v0.6.8) | 3 | — | — | 1.000 | 0 | 0 | 1.000 | $0.028 |

**Guidance: leave `warn` on.** It costs nothing ($0.1445 against $0.1443) and, with one section
per document, produces **zero** spurious errors. `escalate` costs nothing either on this corpus
($0.1447 against $0.1448), because on a clean document validation never fails and nothing is
re-extracted; its price is paid only when validation fails, which RealKIE (§2c, v0.6.8) shows
real forms do. The coercion count is not recorded on the bank-statement rows (it was not in the
v0.6.12 grid either), so this table says nothing about how often coercion repairs a value on
that corpus; the v0.6.8 `kv_form` measurement recorded 0 at either setting.

### `extraction.forced_tool.enabled`

**Re-measured on v0.6.13** (`forcing`, bank statements; the `kv_form` rows are not re-run).

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
| `force-off` *(default)* | bank statements (`longdesc_100`, `valuenoise_100`) | 6 | — | 1.000 | 1.000 | 1.000 | $0.1750 |
| `force-on` | same | 6 | **6/6** | 1.000 | 1.000 | **1.000** | $0.1763 |

**Guidance: accuracy-neutral and cost-neutral (+0.7%), honoured on every call, and
schema-valid.** Enable it for the structural guarantee (a malformed-JSON parse failure becomes
impossible for declared fields) if your corpus produces parse failures; do not enable it for
accuracy. One caution, the same as the last edition's: on the 800-row, 17-page document in the
**Sonnet 5** grid (§5.1) *both* arms truncate, and the forced one truncates less — `force-on`
returned **141 of 800** against `force-off`'s **43 of 800**, each `COMPLETED` and each carrying
`extraction_rows_below_ocr_estimate`. At the **control** model (§2) both arms return all 800
rows. Neither arm is safe at that size in simple mode.

### `classification.document_shape.sectionSplitting` and the boundary prompt

**Re-measured on v0.6.13** (`boundaryab`, 60 runs, and `boundary`, 30 runs, which repeats three
of its cells). Boundary detection judged on `sections_correct` (1.0/0.0 per run; the mean over
5 repeats is the pass rate). Classification model Nova 2 Lite (the shipped default).

| cell | one 3-page statement (unpaginated) | same, paginated | reprinted running header | two statements in one file |
|---|---|---|---|---|
| `split-llm` *(current prompt, default)* | **1.00** (1,1,1,1,1) | 1.00 | **0.00** (3,3,3,3,3) | **1.00** (2,2,2,2,2) |
| `split-llm-v067prompt` *(control: the v0.6.7 prompt)* | **0.40** (1,2,2,1,2) | 1.00 | 0.00 (3,3,3,3,3) | 1.00 |
| `split-disabled` | 1.00 | 1.00 | 1.00 | **0.00** (1,1,1,1,1 — merged), **recall 0.50** |

The `boundary` suite's repeat of `split-llm` and `split-disabled` on three of these documents
agrees on every boundary: 5 of 5 correct for `split-llm` on each, and 5 of 5 merged for
`split-disabled` on the two-statement file.

Three results:

- **The #817 TABLE CONTINUATION rule works on the shipped classifier.** The unpaginated
  statement goes from 2/5 correct under the v0.6.7 prompt to **5/5**, same stack, same model,
  prompt text the only difference. This is the fix that produced "one section" on 131 of 133
  runs in §2 and on every run in §3.
- **The reprinted running header is still split into three, under both prompts.** A document
  whose every page re-prints the title-and-account block is split into three sections, 5 of 5.
  This is the remaining over-split shape of the generic prompt, and `sectionSplitting: disabled`
  is not the answer to it (next point).
- ⚠️ **`disabled` is wrong by construction on a packet, and it loses rows there, not only the
  boundary.** Two statements in one file become one section on 10 of 10 runs across the two
  suites — and on **8 of the 10** only the first statement's 20 rows of 40 come back (recall
  0.500; 3 of 5 in `boundary`, 5 of 5 in `boundaryab`), with no processing issue. That is not new
  in this release: the committed v0.6.8 `boundaryab` grid shows the same arm returning 20 of 40
  on 4 of its 5 runs. `split-llm` splits the file correctly and returns all 40 on 10 of 10. On a single-class corpus of single documents `disabled` is harmless and buys
  nothing (§2b: $0.5532 against $0.5594).

**Guidance: keep `llm_determined` and the shipped prompt.** If your corpus has a reprinted running
header on every page, expect 3 sections per document from the generic prompt; a class-level
`BOUNDARY:` sentence in the class description is the measured fix for that shape (#750), and
advanced mode (which rejoins the section before validation) is the fallback. Do not turn
splitting off on a corpus that can contain more than one document per file.

**Two values this cell does not cover.** `sectionSplitting` has a third value, `page`, which
asserts one document per page; it is not in this matrix. Like `disabled` it is an assertion
rather than a preference, and it is the right answer only for a corpus of single-page documents.
Granularity is a separate axis from this cell and is measured in §5.6 on a labelled packet
corpus, where `packet` granularity placed boundaries correctly far more often than `page`; see
also [Classification](../classification.md#page-versus-packet-granularity-opposite-failure-modes).

### `x-aws-idp-multi-instance` and `extraction.multi_instance_detection`

**Not re-measured on v0.6.13** — the `multiinstance` and `midetect` suites did not run, so the
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

**Not re-measured on v0.6.13** — the `sizerab` suite did not run, so the figures below are the
v0.6.8 measurement. This release changed how list batches are sized (leaf by leaf rather than by
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

From the §2 grid (advanced, Sonnet 4.6 control, 7 documents, re-measured on v0.6.13):
`restate-on` $0.6188, `restate-off` $0.6164, recall and cell accuracy 1.000 both.

A dedicated 50-run A/B on Sonnet 4.6 at v0.6.10 measured what the difference is. Details and the
full tables are in [studies/schema-restatement-tokens.md](studies/schema-restatement-tokens.md);
the decision-relevant numbers, measured 2026-09-20 on v0.6.10 with 25 runs per arm across three
synthetic documents (not re-measured on v0.6.13):

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
extraction-cost difference (+$0.0319/doc) to the cent. ⚠️ **The release grids do not corroborate
the sign.** At one draw per document they put `restate-off` *below* `restate-on` in both of the
last two editions — $0.6164 against $0.6188 here, $0.6142 against $0.6292 at v0.6.12 — a 0.4% and
a 2.4% difference inside the agentic spread §4 measures. So the cost claim rests on the dedicated
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
**`benchmarks/results/v0.6.13/`** — 32 result sets, named `<suite>` or
`<suite>__<axis>-model-<value>`, where the suffix is the `--set` override the suite ran with,
spelled the way `make_configs.py`'s `override_slug` spells it (hyphens for underscores, so
`sonnet5_1m` becomes `sonnet5-1m`; the v0.6.12 directories used underscores). Sections marked
*carried over* in the scope table at the top of this page cite an earlier release's directory
instead, which is in git history. Each pipeline set holds `summary.json` (per-run rows plus a
`meta` block recording the stack, commit and `pricing.yaml` hash), `summary.csv` and
`cell_stats.csv`; `clsvariants/` holds the classification driver's `summary.json`, keyed by
variant with a `per_doc` list.

| Section | Directory (under `benchmarks/results/v0.6.13/`) | Suite / overrides |
|---|---|---|
| §2, §2b | `core/` — synthetic slice | `core` at the control model, 133 synthetic runs with exact ground truth. The v0.7 feature arms of §2b are cells of this same grid (`enforce-*`, `force-*`, `restate-*`, `split-*`) |
| §2c | `core/` — reference slice | the same run's two reference corpora, 760 documents (756 scored). **ONE arm only:** the control model |
| §2.1, §6 | `intconf/`, `advverify__extraction-model-sonnet5/` | 6 runs each; both standing hazards closed, 6/6 |
| §3 | `scaling/` | 42 runs, 25 → 3,200 rows, 3 repeats, at the control model |
| §4 | `cost/`, `cost__extraction-model-sonnet5/` | 5 repeats × 5 cells at the control and at the shipped default |
| §5.1 — the seven-arm grid | `coresynth__extraction-model-sonnet5/`, `…-sonnet5-1m/`, `…-sonnet55/`, `…-haiku55/`, `…-opus5/`, `…-opus55/`, `…-astra/` | `coresynth --set extraction_model=<m>`, 133 runs each |
| §5.1 — the two Nova arms | `simplegrid/`, `simplegrid__extraction-model-nova-lite/`, `simplegrid__extraction-model-nova-pro/` | 63 runs each; `simplegrid/` is their Sonnet 4.6 control |
| §5.2 | `opus55value/`, `astravalue/`, `astracap/` | 40 + 100 + 18 runs |
| §5.3 | `coresynth__classification-model-nova-2-lite/`, `…-haiku55/`, `…-sonnet5/` | `coresynth --set classification_model=<m>`, 133 runs each, default included as an explicit arm |
| §5.4 | `coresynth__confidence-model-nova-lite/`, `…-nova-2-lite/`, `…-sonnet5/` | `coresynth --set confidence_model=<m>`, 133 runs each, default included as an explicit arm |
| §5.6 | `clsvariants/` | `run_classification_bench.py --testset docsplit --n 20 --models nova2lite --mode topk --variants <all seven>`, 140 document runs |
| §7 | `enforcement/`, `enforcecost/`, `forcing/`, `boundaryab/`, `boundary/`, and the `restate-on`/`restate-off` cells of `core/` | the knobs re-measured on v0.6.13. **`splitcost`, `advsplitcost`, `multiinstance` and `sizerab` did not run**, so those subsections carry earlier editions' figures, say so in place, and have **no** v0.6.13 directory |
| release A/B | `corefast/`, and `benchmarks/results/v0.6.12-rvprev/corefast/` | the two sides of [releases/v0.6.13.md](releases/v0.6.13.md); no section of this page cites them |

- The restatement axis of §7 has a second, dedicated source: it was re-measured on its own grid
  at v0.6.10 (`benchmarks/results/v0.6.10/restate710*/`, Sonnet 4.6, 25 runs per arm) and written
  up in [studies/schema-restatement-tokens.md](studies/schema-restatement-tokens.md).
- Earlier releases' slices are pruned per
  [`RETENTION.md`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/benchmarks/results/RETENTION.md);
  restore the v0.6.8 set the carried-over blocks cite with
  `git checkout <pre-v0.6.9-audit sha> -- benchmarks/results/v0.6.8/`.
- Corpus manifest + generators: `benchmarks/corpus/` (regenerable; PDFs/configs gitignored).
  Matrices + methodology: `benchmarks/matrices/`.
- **Scale of this edition:** **3,368 scored rows across 31 pipeline result sets** — 2,608
  synthetic runs and 760 reference-corpus documents, $2,077.09 — plus `clsvariants/`. 15 rows
  did not complete (0.45%): nine simple-mode refusals on the 66-page document, four reference
  `SCORE_ERROR`s and two memory kills (#1678).
- One stack, one session: `IDPUpg0612to0613` at v0.6.13, 2026-10-31, running the published release
  template's build. Up to six suites ran concurrently on the stack (`--max-inflight 4` each), so
  wall-clock figures carry load from the other lanes; cost and accuracy do not.
- ⚠️ Every pipeline suite ran with `--native-upload --validate-against-release v0.6.12`, because
  `--validate-against-release v0.6.13` refuses the flat classification keys `--native-upload` writes
  ([#1674](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1674)).
  The v0.6.13 stack converts those keys on read.

```bash
source .venv/bin/activate && export PYTHONPATH=$PWD/lib/idp_common_pkg
python3 benchmarks/harness/gen_corpus.py
M=benchmarks/harness/make_configs.py
R="python3 benchmarks/harness/run_matrix.py --stack <S> --native-upload --validate-against-release v0.6.12 --max-inflight 4"

# §2 grid, §2b feature arms, §2c real corpora, §7 restatement arms — at the CONTROL model,
# one suite: the three classes are the synthetic corpus and the two reference test sets
for c in bank_statement realkie ocr_bench; do python3 $M --suite core --class $c; done; $R --suite core
# §3 scaling, §4 cost, §2.1 + §6 hazards
# (scaling, intconf, advverify and astracap ran at --repeats 3 in this edition, above their suite defaults)
python3 $M --suite cost --class bank_statement; $R --suite cost
for s in scaling intconf; do python3 $M --suite $s --class bank_statement; $R --suite $s --repeats 3; done
python3 $M --suite cost --class bank_statement --set extraction_model=sonnet5; $R --suite cost --set extraction_model=sonnet5
python3 $M --suite advverify --class bank_statement --set extraction_model=sonnet5; $R --suite advverify --set extraction_model=sonnet5 --repeats 3
# §5.1 extraction sweep — seven arms on coresynth, two Nova arms plus their control on simplegrid
for m in sonnet5 sonnet5_1m sonnet55 haiku55 opus5 opus55 astra; do python3 $M --suite coresynth --class bank_statement --set extraction_model=$m; $R --suite coresynth --set extraction_model=$m; done
python3 $M --suite simplegrid --class bank_statement; $R --suite simplegrid
for m in nova_lite nova_pro; do python3 $M --suite simplegrid --class bank_statement --set extraction_model=$m; $R --suite simplegrid --set extraction_model=$m; done
# §5.3 classification sweep, §5.4 confidence sweep — default is an explicit arm in both
for m in nova_2_lite haiku55 sonnet5; do python3 $M --suite coresynth --class bank_statement --set classification_model=$m; $R --suite coresynth --set classification_model=$m; done
for m in nova_lite nova_2_lite sonnet5; do python3 $M --suite coresynth --class bank_statement --set confidence_model=$m; $R --suite coresynth --set confidence_model=$m; done
# §5.2 premium studies
for s in opus55value astravalue; do python3 $M --suite $s --class bank_statement; $R --suite $s; done
python3 $M --suite astracap --class bank_statement; $R --suite astracap --repeats 3
# §5.6 classification variants (a different driver, a labelled packet corpus)
python3 benchmarks/harness/run_classification_bench.py --stack <S> --testset docsplit --n 20 \
    --models nova2lite --mode topk \
    --variants page-both,page-text,packet-both,packet-text,packet-images,split-then-classify-packet,split-then-classify-page
# §7 feature A/Bs
for s in enforcement forcing enforcecost boundaryab boundary; do python3 $M --suite $s --class bank_statement; $R --suite $s; done
# score: python3 benchmarks/harness/aggregate.py --run benchmarks/results/run-<stamp> --out benchmarks/results/v0.6.13/<suite>[__<slug>]
```
