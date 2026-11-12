---
title: "Extraction Scaling Guide"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Extraction Scaling Guide — Simple vs Advanced Mode, Document Size & List Limits

This guide characterizes how the IDP extraction pipeline behaves as documents and
their embedded lists/tables grow, so you can choose **simple** vs **advanced
(agentic)** extraction mode with confidence and predict cost. Its size limits, cost and
wall-clock figures are from the v0.6.14 benchmark
([Configuration Guidance](./benchmarking/config-guidance.md)); its 6,400-row result and
part of its truncation table are from an earlier controlled scaling study (v0.6.0, Claude
Sonnet 5 extraction, Textract TABLES OCR), shown beside the v0.6.14 result where the two
disagree. Both use synthetic bank statements with an exact known
number of transaction rows.

> **Companion:** see [extraction-and-confidence.md](extraction-and-confidence.md)
> for how to configure `extraction.mode`, agentic sharding, and confidence/assessment.

> ⚠️ **Simple mode can return part of a list above about 400 rows and still report
> `COMPLETED`.** The Simple-mode list-size threshold on this page comes from the v0.6.14
> benchmark grid with the shipped default extraction model (Claude Sonnet 5); the cost and
> wall-clock figures further down are from the same v0.6.14 benchmark, and the
> 6,400-row result comes from the earlier study described below and has not been
> re-measured, and the truncation table below sets that study beside the v0.6.14
> control-model result. In that grid Simple mode returned every row of each of three
> 400-row, 9-page statements on every run, but on an 800-row, 17-page statement it
> returned the whole list on only **5 of 13** runs and **43 to 190 rows** on the other
> 8, each one `COMPLETED`
> ([config-guidance §5.5](./benchmarking/config-guidance.md#55-recommendations-by-document-profile)).
> No size between 400 and 800 rows was measured, so **use Advanced mode for any section
> that can hold more than about 400 list rows.** The limit depends on the model: Claude
> Sonnet 5.5 lost most of a 400-row list on 15 of its 133 runs, and both Amazon Nova
> models return about half of a list or less above 100 rows
> ([config-guidance §5.1](./benchmarking/config-guidance.md#51-extraction-model--the-full-grid)).
> Check completeness rather than completion status. A list that came back with fewer
> than half the rows the section's matching OCR tables show carries the
> `extraction_rows_below_ocr_estimate` processing issue (a warning by default; all 8
> short runs above carry it), and a smaller loss of 30 or more rows carries
> `extraction_partial_row_shortfall` (up to 29 rows can go missing unreported); a list with no matching OCR table may be reported
> by nothing.
> [`extraction.row_shortfall_action: fail`](./extraction-and-confidence.md#making-a-materially-incomplete-list-fail-the-section--extractionrow_shortfall_action)
> makes a section carrying the first issue fail, but it can also fail correct
> extractions of small 2- or 3-property arrays such as the default preset's
> `Bank-Statement.account_summary`, so check
> [the shapes it misjudges](./extraction-and-confidence.md#why-fail-is-opt-in-and-what-to-check-before-turning-it-on)
> before turning it on.
> [`minItems`](./extraction-and-confidence.md#minitems-on-a-list-field--a-warning-in-simple-mode-a-hard-floor-in-advanced)
> behaves differently in the two modes.

---

## TL;DR — which mode, what size?

| Document / list size | Recommended mode | Why |
|----------------------|------------------|-----|
| ≤ ~400 list rows, ≤ ~9 pages per section | **Simple** | Complete and accurate at this size |
| > ~400 list rows | **Advanced** | Simple returns a partial list on some runs and reports `COMPLETED` (5 of 13 runs complete at 800 rows with the shipped default); advanced shards the work and stays complete |
| > ~3,000 rows / > ~60 pages | **Advanced, and split the document if it is much larger** | Advanced stays complete: $3.76–4.20 per 3,200-row, 66-page document at the shipped default, about 7 min. Simple mode refuses a document of this size. The earlier study's 131-page document failed at summarization (below) |

**Key safety point:** when simple mode exceeds its limit it **returns a partial list
with no error** and the document reports `COMPLETED` — it looks successful but can drop
most rows. The section carries an `extraction_rows_below_ocr_estimate` processing issue
when the OCR text shows the missing rows, so read processing issues, not status. If your
documents can contain large tables, either use advanced mode or validate row counts
downstream. A schema `minItems` constraint is the cheapest reconciliation signal, and
what it costs differs by mode: in **simple** mode it is advisory — a shortfall raises
a warning and the rows are kept — while in **advanced** mode it is enforced at the
agent's tool boundary, so a floor the section cannot reach spends the agent's
correction rounds and then **fails** the extraction, keeping no rows. Set a floor you
are willing to fail on, or use
[`extraction.row_shortfall_action`](./extraction-and-confidence.md#making-a-materially-incomplete-list-fail-the-section--extractionrow_shortfall_action),
which saves the partial rows and the diagnosis before failing the section. See
[`minItems` in Simple vs Advanced mode](./extraction-and-confidence.md#minitems-on-a-list-field--a-warning-in-simple-mode-a-hard-floor-in-advanced).

---

## The two modes

- **Simple** (`extraction.mode: simple`): one LLM call extracts the whole document
  section. Cheapest and lowest-latency. Bounded by the model's context window (input)
  and by a long-context degradation effect (output).
- **Advanced / agentic** (`extraction.mode: advanced`): an agent shards large sections
  across multiple LLM calls (Step Functions distributed map), can use a deterministic
  table-parsing tool, and enforces schema completeness. Handles far larger documents at
  proportionally higher cost and latency.

---

## Simple mode: two limits

### 1. Output truncation, from somewhere between 400 and 800 rows
As the single extraction call's input grows, the model begins **abandoning the list
early** and returns fewer rows than are present — with no error and a `COMPLETED`
status. With the shipped default model on the v0.6.14 grid that starts above 400 rows:
every run was complete at 400 rows / 9 pages, and at 800 rows / 17 pages 8 of 13 runs
returned 43 to 190 rows
([config-guidance §5.5](./benchmarking/config-guidance.md#55-recommendations-by-document-profile)).
Whether a given run at that size truncates varies from run to run, so one complete run
says little about the next.

Above 800 rows the two studies this page draws on disagree, and they used different
extraction models, so neither is a prediction for the other. Both ran one document per
size:

| Rows in doc | Pages | v0.6.0 study, Claude Sonnet 5 (rows returned) | v0.6.14 `scaling` suite, Claude Sonnet 4.6 control model (rows returned) |
|-------------|-------|----------------------------------------------|---------------------------------------------------------------------------|
| 400   | 9  | 400 ✅ | 400 ✅ |
| 800   | 17 | 800 ✅ | 800 ✅ |
| 1,000 | 21 | 239 ⚠️ silent truncation | not measured |
| 1,200 | 25 | 92 ⚠️ | 1,200 ✅, 5% of rows without a confidence score |
| 1,600 | 33 | 43 ⚠️ | 1,600 ✅, 20% of rows without a confidence score |

The v0.6.14 figures are from
[config-guidance §3](./benchmarking/config-guidance.md#3-scaling-limits-by-document-size),
and they are the more recent measurement. The shipped default, Claude Sonnet 5, was not
run in Simple mode between 800 and 3,200 rows on v0.6.14 (at 3,200 rows it was refused, as
described below), so whether its v0.6.0 truncation at 1,000 to 1,600 rows still holds is not
known; what v0.6.14 did measure for it is the partial lists at 800 rows described above. Treat the upper end of Simple mode as dependent on the
model and the release rather than as a fixed row count.

The v0.6.0 truncations were **not** a max-tokens setting: those runs emitted only
2.5K–12K output tokens, far below the model's 128K output cap, so the model stopped the
list early rather than running out of output budget.

### 2. Request too large: refused at 66 pages
A document too large for one request is refused before extraction runs, with Bedrock's
*Input is too long for requested model*, raised as `ExtractionInputTooLarge`. On the
v0.6.14 benchmark the 3,200-row, 66-page statement was refused this way on every
Simple-mode run, at the shipped default and at the control model, in 47–58 s, with 0
rows returned and only the OCR and classification billed (about $1.05). The 1,600-row,
33-page statement was not refused at the control model. Sizes between 33 and 66 pages were not measured
([config-guidance §3](./benchmarking/config-guidance.md#3-scaling-limits-by-document-size), [§5.2](./benchmarking/config-guidance.md#52-is-a-premium-model-worth-it)).
What fills the request is mostly page images rather than OCR text, so the limit is
closer to a page count than a row count. Unlike truncation, this fails loudly (the
document errors out rather than returning partial data).

## Advanced mode: limits are cost and downstream steps, not completeness
Advanced (agentic sharding) returned every row at every size measured on v0.6.14, from
25 to 3,200 rows / 66 pages, and the earlier study kept **100% extraction recall through
6,400 rows / 131 pages** — the extraction step itself has no completeness ceiling in the
range tested. Because it splits the document into small shards, neither the
output-truncation nor the request-size limit is hit per call.

The tradeoffs are **cost, latency, and a downstream ceiling**. The figures below are the
v0.6.14 benchmark: the `scaling` suite at the study's control extraction model (Claude
Sonnet 4.6, Textract TABLES, `separate` confidence, one run per size), with the shipped
default (Claude Sonnet 5) beside it where it was measured
([config-guidance §3](./benchmarking/config-guidance.md#3-scaling-limits-by-document-size), [§4](./benchmarking/config-guidance.md#4-cost-and-its-run-to-run-variation-5-repeats-of-one-400-row-document), [§5.2](./benchmarking/config-guidance.md#52-is-a-premium-model-worth-it)).
Sonnet 5 figures are at its published rates.

| Rows | Pages | Simple cost (control) | Advanced cost (control) | Advanced wall-clock (control) | Advanced cost (Sonnet 5) |
|------|-------|-----------------------|-------------------------|-------------------------------|--------------------------|
| 25    | 1  | $0.0431 | $0.0958 | 37 s  | not measured |
| 100   | 3  | $0.1440 | $0.1759 | 59 s  | $0.15 (Simple $0.15), on a different 100-row, 3-page statement |
| 400   | 9  | $0.5255 | $0.6694 | 140 s | $0.508 (Simple $0.534), mean of 5 |
| 800   | 17 | $1.6192 | $1.3260 | 126 s | $1.01 (Simple $1.28, part of the list on 1 of 5 runs) |
| 1,200 | 25 | $2.2882, 5% of rows unscored | $1.9050 | 147 s | not measured |
| 1,600 | 33 | $3.2134, 20% of rows unscored | $2.5529 | 221 s | not measured |
| 3,200 | 66 | refused | $5.0135 | 374 s | $3.76–4.20, 434 s mean, 2 runs |

So at the control model Advanced mode costs more than Simple on short documents and less
from 800 rows upward. At the shipped default the two cost the same at 100 rows, are within
the run-to-run spread at 400, and Advanced is the cheaper at 800. Advanced cost per row is close to flat from 400 rows upward, at
$0.00157–0.00167 per row including OCR at the control model.

In the earlier study (v0.6.0), a 6,400-row, 131-page document extracted completely but
failed at summarization, which is not measured on v0.6.14:

**The true advanced-mode ceiling is a downstream step, not extraction.** At 131 pages,
all 6,400 rows extracted correctly across 10 shards, but the **summarization** step —
which is a *single, non-sharded* LLM call over the whole document — overflowed the input
window and failed the entire document (`Input Tokens Exceeded`). So a very large document
can incur a full, expensive extraction and still fail at the end.

**Mitigations for very large documents:**
- **Split the document** (by statement period, account, or page range) if it is much
  larger than the 3,200 rows / 66 pages measured on v0.6.14 — that keeps the
  summarization step within bounds; extraction cost per row stays about flat with size.
- **Disable summarization** (`summarization` off) if you only need extraction — this
  avoids the downstream ceiling for large documents.
- Budget about $0.0012–0.0017 per row, OCR included (v0.6.14, 400 to 3,200 rows, the
  shipped default and the control model); expect 2–8 min for thousand-to-few-thousand-row
  documents.

---

## How list content changes the limits

The simple-mode cliff is driven by **token count**, not table shape:

- **Row count and row width both matter** (they add output/input tokens). A wide
  8-column table reaches the cliff at a *lower* row count than a narrow 3-column table.
  Noisy OCR and long free-text cells also push tokens up and pull the cliff earlier.
  Treat "~400 rows" as the limit for clean narrow rows at the shipped default model —
  expect less for dense rows.
- **Splitting one big table into several smaller lists makes little cost difference in
  advanced mode.** On v0.6.14 at the shipped default, a 400-row statement holding 8 lists
  of 50 cost $0.632 per document against $0.691 for one 400-row list, means over the six
  advanced-mode cells of the grid (`coresynth__extraction-model-sonnet5`, at Sonnet 5's
  published rates).

### Advanced-mode caveat: the table tool can be declined
When OCR corrupts a table (e.g. merged columns), the agentic table-parsing tool may be
**declined** by the agent ("could not produce reliable values, chose not to fabricate").
In that case the agent may return the list as **null** rather than partial data, and the
behavior can **vary run-to-run**. It did not recur on v0.6.14: on the 100-row document
where it had fired, all 8 Advanced-mode runs at the shipped default returned every row
([config-guidance §2.1](./benchmarking/config-guidance.md#21-integrated-confidence-with-simple-mode-list-truncation-fixed-in-v068)).
If your OCR quality is marginal on tabular data:
- Prefer **Textract TABLES** (or BDA) OCR so the tool gets clean cell structure.
- Consider a schema `minItems` constraint so incomplete extractions are flagged.
- For borderline cases, simple mode can be *more* consistent (it extracts what it can).

---

## Practical guidance

1. **Right-size the mode.** Default to **simple** for typical documents (≤ ~400 list
   rows, ≤ ~9 pages per section), where it is complete and accurate. Enable
   **advanced** for documents with larger lists or multi-page tables.
2. **Guard against truncation.** If large tables are possible in simple mode, prefer
   advanced mode. Otherwise read the section's processing issues rather than its status,
   add a schema `minItems` on the list, or reconcile extracted row counts against an
   expected total downstream. `extraction.row_shortfall_action: fail` makes a list that
   lost most of its rows fail the section instead of reporting `COMPLETED`, but on the
   default bank-statement preset it also fails correct extractions of the two-property
   `account_summary` unless a declared `maxItems` bounds its evidence; see
   [the shapes it misjudges](./extraction-and-confidence.md#why-fail-is-opt-in-and-what-to-check-before-turning-it-on)
   and [the `maxItems` remedy](./extraction-and-confidence.md#maxitems-bounds-the-evidence-and-is-the-per-field-remedy). Advanced mode checks `minItems` inside its extraction loop, so the
   agent gets correction rounds rather than silently returning a short list — but a floor
   it cannot reach fails the section and keeps no rows, so pick a floor you are willing to
   fail on.
3. **Budget advanced mode.** Estimate about $0.0012–0.0017 per row, OCR included; expect
   2–8 min for thousand-to-few-thousand-row documents (v0.6.14). Split documents much
   larger than the 3,200 rows / 66 pages measured.
4. **Feed the table tool clean OCR.** Use Textract TABLES or BDA for tabular documents so
   the agentic parser has structured cells and does not decline.

---

## Study methodology (for reproducibility)

Synthetic bank-statement PDFs (reportlab) with each transaction Description tagged
`SEQnnnnn` for exact completeness measurement. ~50 rows/page. OCR = Textract TABLES,
extraction = Claude Sonnet 5, confidence = Nova Lite with escalation disabled (to isolate
extraction limits from assessment cost). Sweeps: row count {25…6,400}, row width (3 vs 8
cols), list count (1 vs 8), description length. Metric: recall = distinct SEQ recovered ÷
ground-truth count; truncation point = longest contiguous prefix; plus cost and wall-clock
from per-document metering. Full data and the validation battery are in the internal report.
