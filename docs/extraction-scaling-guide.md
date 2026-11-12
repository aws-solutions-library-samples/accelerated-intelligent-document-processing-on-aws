---
title: "Extraction Scaling Guide"
---

# Extraction Scaling Guide — Simple vs Advanced Mode, Document Size & List Limits

This guide characterizes how the IDP extraction pipeline behaves as documents and
their embedded lists/tables grow, so you can choose **simple** vs **advanced
(agentic)** extraction mode with confidence and predict cost. It is based on a
controlled scaling study (v0.6, Claude Sonnet 5 extraction, Textract TABLES OCR)
using synthetic bank statements with an exact known number of transaction rows.

> **Companion:** see [extraction-and-confidence.md](extraction-and-confidence.md)
> for how to configure `extraction.mode`, agentic sharding, and confidence/assessment.

> ⚠️ **Simple mode can return part of a list above about 400 rows and still report
> `COMPLETED`.** The Simple-mode list-size threshold on this page comes from the v0.6.14
> benchmark grid with the shipped default extraction model (Claude Sonnet 5); the cost,
> wall-clock and page-count figures further down come from the earlier study described
> below and have not been re-measured here. In that grid Simple mode returned every row of each of three
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
| > ~3,000 rows / > ~60 pages | **Advanced, but split the document if you can** | Advanced stays complete but costs >$40/doc and takes >14 min |

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

The earlier study this page was built on, one run per size, shows the same shape at a
larger size:

| Rows in doc | Pages | Rows extracted (simple) | Result |
|-------------|-------|-------------------------|--------|
| 400  | 9  | 400 | ✅ complete |
| 800  | 17 | 800 | ✅ complete |
| 1,000 | 21 | 239 | ⚠️ silent truncation |
| 1,200 | 25 | 92  | ⚠️ worse |
| 1,600 | 33 | 43  | ⚠️ worst |

This is **not** a fixable max-tokens setting — the truncated runs emitted only
2.5K–12K output tokens, far below the model's 128K output cap. The model gives up on
the list as total input grows. The more rows/pages, the *earlier* it truncates.

### 2. Input context overflow, above about 130 pages
A very large document's OCR text exceeds the model's input context window (200K tokens
for Sonnet 5), producing a hard failure:
`ValidationException: Input is too long for requested model`. Unlike truncation, this
fails loudly (the document errors out rather than returning partial data).

## Advanced mode: limits are cost and downstream steps, not completeness
Advanced (agentic sharding) kept **100% extraction recall through 6,400 rows / 131
pages** — the extraction step itself has no completeness ceiling in the range tested.
Because it splits the document into small shards, neither the output-truncation nor the
input-overflow limit is hit per call.

The tradeoffs are **cost, latency, and a downstream ceiling**:

| Rows | Pages | Simple | Advanced cost | Advanced wall-clock | Advanced result |
|------|-------|--------|---------------|---------------------|-----------------|
| 100  | 3   | $0.18 complete | $0.59 | ~110s | complete |
| 400  | 9   | $0.68 complete | $6.26 | ~455s | complete |
| 800  | 17  | $1.35 complete | $7.45 | ~340s | complete |
| 1,200 | 25 | truncates (92/1200) | $16.48 | ~615s | complete |
| 3,200 | 66 | fails | $43.15 | ~870s | complete |
| 6,400 | 131 | fails (input overflow) | $89.11 | ~29 min | **extraction complete, but document fails at summarization** |

**The true advanced-mode ceiling is a downstream step, not extraction.** At 131 pages,
all 6,400 rows extracted correctly across 10 shards, but the **summarization** step —
which is a *single, non-sharded* LLM call over the whole document — overflowed the input
window and failed the entire document (`Input Tokens Exceeded`). So a very large document
can incur a full, expensive extraction and still fail at the end.

**Mitigations for very large documents:**
- **Split the document** (by statement period, account, or page range) below ~60 pages /
  ~3,000 rows — this keeps cost, latency, and the summarization step all within bounds.
- **Disable summarization** (`summarization` off) if you only need extraction — this
  avoids the downstream ceiling for large documents.
- Budget ~$0.006–0.013 per row plus OCR; expect 5–30 min for thousand-to-few-thousand-row
  documents.

---

## How list content changes the limits

The simple-mode cliff is driven by **token count**, not table shape:

- **Row count and row width both matter** (they add output/input tokens). A wide
  8-column table reaches the cliff at a *lower* row count than a narrow 3-column table.
  Noisy OCR and long free-text cells also push tokens up and pull the cliff earlier.
  Treat "~400 rows" as the limit for clean narrow rows at the shipped default model —
  expect less for dense rows.
- **Splitting one big table into several smaller lists/sections helps advanced mode a
  lot.** A 400-row table split as 8 lists of 50 cost ~$2.6 in advanced vs ~$6.3 as one
  400-row list — each fragment fits a single agent turn cleanly.

### Advanced-mode caveat: the table tool can be declined
When OCR corrupts a table (e.g. merged columns), the agentic table-parsing tool may be
**declined** by the agent ("could not produce reliable values, chose not to fabricate").
In that case the agent may return the list as **null** rather than partial data, and the
behavior can **vary run-to-run**. If your OCR quality is marginal on tabular data:
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
3. **Budget advanced mode.** Estimate ~$0.006–0.013 per row plus OCR; expect 5–15 min for
   thousand-row documents. Split documents above ~3,000 rows / ~60 pages.
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
