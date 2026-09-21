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

---

## TL;DR — which mode, what size?

| Document / list size | Recommended mode | Why |
|----------------------|------------------|-----|
| ≤ ~500 list rows, ≤ ~15 pages | **Simple** | Complete, accurate, cheapest (3–18× cheaper than advanced) |
| ~500–800 rows | Simple (watch), or Advanced if completeness is critical | Simple still complete but approaching its cliff |
| > ~800 rows or > ~15–20 pages | **Advanced** | Simple silently truncates; advanced shards the work and stays complete |
| > ~3,000 rows / > ~60 pages | **Advanced, but split the document if you can** | Advanced stays complete but costs >$40/doc and takes >14 min |

**Key safety point:** when simple mode exceeds its limit it **silently returns a
partial list with no error** — it looks successful but drops most rows. If your
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

### 1. Silent output truncation — onset ~800–1,000 rows (narrow 3-column rows)
As the single extraction call's input grows, the model begins **abandoning the list
early** and returns fewer rows than are present — with no error and a "SUCCESS" status.

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

### 2. Hard input-context overflow — ~130+ pages
A very large document's OCR text exceeds the model's input context window (200K tokens
for Sonnet 5), producing a hard failure:
`ValidationException: Input is too long for requested model`. Unlike truncation, this
fails loudly (the document errors out rather than returning partial data).

## Advanced mode: extraction completeness holds; cost and downstream steps are the limits
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
  Treat "~800 rows" as an upper bound for clean narrow rows — expect less for dense rows.
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

1. **Right-size the mode.** Default to **simple** for typical documents (≤ ~500 rows,
   ≤ ~15 pages) — it is complete, accurate, and 3–18× cheaper. Enable **advanced** only
   for documents with large multi-page tables.
2. **Guard against silent truncation.** If large tables are possible in simple mode, add
   a schema `minItems` on the list or reconcile extracted row counts against an expected
   total downstream. Advanced mode checks `minItems` inside its extraction loop, so the
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
