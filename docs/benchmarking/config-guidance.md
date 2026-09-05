---
title: "Configuration Guidance"
---

> **This is the evergreen "which configuration should I pick?" paper** — a cross-config
> comparison at the current release. For **release-over-release** comparisons (is the
> upgrade safe / cheaper / faster?), see the [Release Benchmark Audit Trail](releases/).

# GenAIIDP Configuration Guidance — Empirical Guidance for Document Extraction at Scale

**Release:** v0.6.7 · **Region:** us-west-2 · **Stack:** `IDPRel067` (deployed from the
published v0.6.7 template)
**Models:** extraction Claude Sonnet 5 (the shipped default) · classification Nova 2 Lite
(the shipped default) · confidence Nova Lite · summarization disabled (unscored)
**Pricing:** `config_library/pricing.yaml` (sha256 `aa52446a…`; rates as of 2026-09; intro
pricing may apply)

> Reproducible via the `benchmarks/` harness (run the `run-benchmarks` skill). Every number
> here is produced by `benchmarks/harness/aggregate.py` from live runs; none are recalled
> from memory. Supporting data for §2–§4 was re-measured at v0.6.7 and is in the working
> tree under `benchmarks/results/v0.6.7/` — see Appendix A for the exact directory per
> section. Per
> [`benchmarks/results/RETENTION.md`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/benchmarks/results/RETENTION.md)
> only one complete set is retained per release, so the v0.6.5 slices are no longer in the
> working tree; recover them from git with
> `git checkout ec3eb05ae -- benchmarks/results/v0.6.5-config-core/` (likewise
> `-config-scaling`, `-config-cost`, `-intconf-sonnet5`, `-intconf-sonnet46`).
>
> ⚠️ **`longdesc_100` was regenerated between the v0.6.5 and v0.6.7 editions.** Its long
> descriptions were drawn as unwrapped table cells that overprinted the Amount column, so
> every amount on that document was physically absent from OCR — see
> [METHODOLOGY §1.A](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/benchmarks/matrices/METHODOLOGY.md).
> **v0.6.5's `longdesc_100` results are not comparable with v0.6.7's**, and several of that
> edition's findings about that document were measuring the defect. Every other document is
> unchanged.

---

## Abstract

We benchmark the GenAI IDP accelerator across a controlled matrix of **configuration
options** (OCR backend, extraction mode, assessment mode, geometry, model, escalation) and
**document types and sizes** (synthetic documents with exact ground truth). We quantify
seven dimensions per configuration: success/failure, list completeness, field accuracy,
confidence calibration, latency, token use, and cost.

Headline results at v0.6.7:

1. **Extraction mode is primarily a cost decision, not an accuracy one, at these sizes.**
   Across 7 documents spanning 5 → 800 rows, **every Textract and BDA cell — simple and
   advanced — is at recall 1.000 and per-row cell accuracy 1.000**, and simple mode is
   **~2.9× cheaper** (mean $0.603 vs $1.726 per document). 133 runs, **0 failures**.
2. **The completeness picture improved substantially since v0.6.5, including the scaling
   cliff.** That edition reported simple/`integrated` at recall 0.294, advanced nulling a
   whole list, and a hard silent cliff for simple mode between 800 and 1,200 rows
   (0.199 @1,200 → 0.009 @3,200). None of the three reproduce: simple mode is now complete
   through **1,600 rows / 33 pages** and recovers 0.724 at 3,200 (§3). **The reason is
   uncomfortable** — the over-splitting in item 3 is shortening each extraction call's
   output and thereby defeating the truncation that caused the cliff, which makes
   "turn splitting off to save money" a size-dependent trade rather than a free win.
3. **🚨 The most expensive configuration is now advanced + `integrated` ($2.32/doc), and
   the shipped default `sectionSplitting: llm_determined` is why advanced costs what it
   does.** Every cell in the grid is **over-split 2–3×** — 13–23 sections where the truth
   is 7 — and on the agentic path each spurious section is a whole agent loop. This is
   worth **+22%** on advanced mode, measured separately (§4 and the
   [v0.6.7 release audit](releases/v0.6.7.md)).
4. **Two narrow completeness risks remain, both invisible to field accuracy.**
   `integrated` confidence with simple extraction still lost **45% of the rows** on one of
   seven documents (`manylists_400`, recall 0.552) while reporting `COMPLETED` and scalar
   accuracy 1.000 — much better than v0.6.5's 0.294 mean but not fixed (§2.1). And
   **Bedrock-LLM OCR** is the only backend that loses rows on ordinary documents (recall
   0.620 on a 100-row document) *and* the only one that gets per-row values wrong (cell
   accuracy 0.935–0.990 where every other backend is 1.000).
5. **`extraction.validation` now on by default is earning its keep, and simple mode fails
   it far more than advanced.** Simple-mode sections validate at **0.54–0.71**, advanced at
   **1.000**. Most of the simple-mode failures are a *consequence* of the over-splitting in
   item 3 — a continuation section legitimately has no `Account Number`, which the schema
   marks required (§2, finding 7).
6. **Per-row cell accuracy is reported here for the first time**, and it is the metric that
   matters: `completeness_recall` counts rows, and a run can return every row with an entire
   **column** empty and still score 1.000. That is not hypothetical — it is how the previous
   edition of this paper missed a corpus defect (see the ⚠️ note above). It is **1.000 in
   every completed run at every size in §2 and §3** except Bedrock-LLM.
7. **🚨 The confidence pass, not extraction, is what fails at scale.** The one run in this
   whole study that did not complete (`simple` @800 rows, §3) extracted all 800 rows and
   then lost the document in the confidence step: Nova Lite truncates a 25-row batch, the
   recovery halves the batch and retries, and the ladder does not converge inside the 900 s
   Assessment Lambda — 5 consecutive timeouts, 6,205 s, paying Bedrock every attempt. A
   second unbounded recovery ladder (the agentic one) is documented in the
   [release audit](releases/v0.6.7.md). Neither is bounded by remaining Lambda time.

> **What changed between releases** is tracked separately in the
> [Release Audit Trail](releases/) — this paper focuses on *choosing a configuration at the
> current release*.

---

## 1. Methodology (summary)

See `benchmarks/matrices/METHODOLOGY.md` for the full protocol. In brief:
- **Synthetic corpus (exact GT):** generated bank statements whose every transaction row
  carries a unique `SEQnnnnn` tag, so completeness and accuracy are measured exactly, and
  size, row width, list count, text length and OCR noise are controlled variables.
- **Config matrix:** 19 curated *core* cells — the OCR × mode × assessment decision space
  plus the v0.7 feature arms (enforcement, forcing, schema restatement, section splitting) —
  a two-cell scaling series, and a repeated-measures cost suite. Control arms (deliberately
  wrong or historical configurations) live in a separate `control_cells:` block and are
  excluded from the `core_cells` expansions, so a known-defective configuration cannot end
  up in the release grid.
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
| Enforcement | `off` · `warn` (shipped default) · `escalate` |
| Forcing | off (shipped default) · on |
| Schema restatement | on (shipped default) · off |
| Section splitting | `llm_determined` (shipped default) · `disabled` |
| Geometry | ocr_only (all cells below) |
| Extraction model | Sonnet 5 (all cells below; the shipped default) |
| Classification model | Nova 2 Lite (all cells below; the shipped default) |
| Confidence model | Nova Lite (all cells below) |
| Reasoning effort | low (all cells below) |

The one-axis sweeps over geometry / escalation / extraction model / confidence model /
reasoning effort (the `full` suite) were **not** run for this release.

**Axes not varied in §2–§4** (all have suites; see [index.md](index.md) for which metric
each is judged on). Advice for the ones that have been measured is in §7.

| Axis | Config path | Measured? |
|------|-------------|-----------|
| Boundary prompt | `classification.task_prompt` (frozen variants, control only) | yes, but underpowered here — !769 is the authority, §7 |
| Classification confidence | `classification.confidence.mode` | held `topk` (the shipped default) in every cell below; its cost is measured in §7 |
| Classification model | `classification.model` | held at Nova 2 Lite (shipped default) in §2–§4; §7's splitting measurement used Sonnet 5, which is why it disagrees — see §4 |
| Multi-instance | `x-aws-idp-multi-instance`, `extraction.multi_instance_detection` | off / on-by-default respectively; measured separately in §7 |
| OCR DPI | `ocr.image.dpi` | held 300 (the shipped default since #740) — §7 |

---

## 2. Configuration matrix (19 cells × 7 synthetic list docs, exact GT)

Mean over 7 bank-statement (transaction-list) documents spanning 5 → 800 rows and varying
row width, list count and description length (`tiny_form`, `small_narrow`, `med_narrow`,
`large_narrow`, `wide_400`, `manylists_400`, `longdesc_100`). `recall` = distinct
ground-truth rows recovered ÷ total (exact, via SEQ tags); `cell acc` = per-row typed value
match, keyed by SEQ tag. **133 runs, 0 failures.**

### 2a. The decision space: OCR × mode × assessment

| OCR / mode / assessment | recall | cell acc | cost/doc | mean conf | alert % | valid rate | wall_s | fails |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Textract TABLES / simple / separate | 1.000 | 1.000 | $0.603 | 0.994 | 0.4 | 0.588 | 217 | 0 |
| Textract TABLES / simple / off | 1.000 | 1.000 | $0.554 | n/a | n/a | 0.597 | 83 | 0 |
| **⚠️ Textract TABLES / simple / integrated** | **0.936** | 1.000 | $0.768 | **0.901** | **2.4** | 0.629 | 135 | 0 |
| Textract TABLES / advanced / separate | 1.000 | 1.000 | $1.726 | 0.984 | 0.2 | **1.000** | 286 | 0 |
| Textract TABLES / advanced / integrated | 1.000 | 1.000 | **$2.315** | 0.987 | 1.0 | **1.000** | 388 | 0 |
| Textract LAYOUT / simple / separate | 1.000 | 1.000 | **$0.516** | 0.996 | 0.2 | 0.714 | 229 | 0 |
| Textract LAYOUT / advanced / separate | 1.000 | 1.000 | **$1.354** | 0.979 | 0.3 | **1.000** | 259 | 0 |
| BDA / simple / separate | 1.000 | 1.000 | $0.590 | 0.995 | 0.3 | 0.671 | 196 | 0 |
| BDA / advanced / separate | 1.000 | 1.000 | $1.793 | 0.982 | 0.4 | **1.000** | 290 | 0 |
| **⚠️ Bedrock-LLM / simple / separate** | **0.919** | **0.982** | $0.496 | 0.987 | 0.3 | 0.604 | 194 | 0 |

Cost CV is 0.75–0.92 for every cell because the 7 documents differ 160× in row count; that
is *between-document* spread, not run-to-run noise. §4 measures cost variance properly,
with repeats on one document.

### 2b. The v0.7 feature arms

All are Textract TABLES + `separate`; `restate-*` are advanced, the rest simple. `sections`
is the total across the 7 documents, where the ground truth is **7** (one per document).

| cell | recall | cell acc | cost/doc | sections (truth 7) | valid rate | val errors |
|---|---:|---:|---:|---:|---:|---:|
| `enforce-off` | 1.000 | 1.000 | $0.601 | 13 | not measured | 0 |
| `enforce-warn` *(shipped default)* | 1.000 | 1.000 | $0.589 | 21 | 0.544 | 18 |
| `enforce-escalate` | 1.000 | 1.000 | **$0.792** | 16 | 0.636 | 9 |
| `force-off` *(shipped default)* | 1.000 | 1.000 | $0.597 | 18 | 0.664 | 11 |
| **🚨 `force-on`** | 1.000 | 1.000 | $0.578 | 19 | **0.000** | 19 |
| `restate-on` *(shipped default)* | 1.000 | 1.000 | $1.686 | 16 | 1.000 | 0 |
| `restate-off` | 1.000 | 1.000 | $1.948 | 23 | 1.000 | 0 |
| `split-llm` *(shipped default)* | 1.000 | 1.000 | $0.608 | 19 | 0.593 | 12 |
| **`split-disabled`** | 1.000 | 1.000 | $0.703 | **7** | **1.000** | **0** |

**Findings**

1. **Completeness and per-row accuracy are solved for Textract and BDA, in both modes.**
   Eight of the ten cells in 2a are at recall 1.000 *and* cell accuracy 1.000 across all
   7 documents including the 800-row / 17-page one. So mode is a **cost** decision at these
   sizes: simple $0.52–0.60, advanced $1.35–2.32, a **2.6–2.9× premium**.
2. **v0.6.5's two headline completeness failures did not reproduce.** `simple/integrated`
   was 0.294 then and 0.936 now; advanced nulling an entire list on `longdesc_100` (recall
   0.000) is now 1.000. The first improved for real (see §2.1 for what remains); the second
   was partly an artifact — the fixes in #668 landed, *and* `longdesc_100` itself was
   defective in the v0.6.5 corpus (see the ⚠️ note in the header).
3. **⚠️ `integrated` + simple is still the riskiest cell, just narrower.** Mean recall 0.936
   comes from **0.552 on `manylists_400`** with the other six at 1.000 — 45% of the rows
   gone, `COMPLETED`, scalar accuracy 1.000. It is also the worst-calibrated cell by a wide
   margin (mean confidence **0.901** and alert rate **2.4%** against 0.98–0.996 / 0.2–0.4%
   everywhere else) and, unlike at v0.6.5, it is no longer the cheapest cell — at $0.768 it
   costs **27% more** than `simple/separate`. There is now no reason to choose it. See §2.1.
4. **⚠️ Bedrock-LLM OCR is the only backend that gets *values* wrong.** It is the cheapest
   ($0.496) but it is alone in the table on two counts: recall below 1.000 (0.919, with
   **0.620 on a 100-row document** — not a size effect) and **cell accuracy 0.982**, i.e.
   per-row values corrupted. Every Textract and BDA cell is 1.000/1.000. This is the
   fixed-width-identifier corruption root-caused in the
   [v0.6.6 audit](releases/v0.6.6.md): the backend inserts a digit into an identifier and
   nothing flags it. **Do not use it where identifiers matter.**
5. **LAYOUT-only is the cheapest complete option in both modes** ($0.516 simple / $1.354
   advanced) with the best confidence (0.996 / 0.2%). Textract TABLES buys no completeness
   at this scale and costs 17% (simple) to 28% (advanced) more. Turn TABLES on for very
   large multi-page tables where it aids recovery, not by default.
6. **🚨 Every cell but `split-disabled` is over-split 2–3×.** 13–23 sections where the truth
   is 7. Recall is unaffected — the rows all come back — but the *shape* is wrong, and on the
   agentic path it is **the dominant cost driver** (§4). `split-disabled` is the only cell
   that gets 7, and it is **wrong by construction on packets** (§7), so it is a fix for
   single-class corpora only.
7. **Simple mode's validation failures are almost entirely a consequence of over-splitting,
   not an extraction problem.** Simple cells validate at 0.54–0.71; advanced at 1.000. But
   `split-disabled` — same schema, same simple extraction, one section per document —
   validates at **1.000 with 0 errors**. The failures are `'Account Number' is a required
   property` on continuation sections that legitimately do not carry it. Advanced mode
   escapes it because its sharding rejoins the section before validation. **Read a
   simple-mode validation warning as a possible splitting problem first.**
8. **🚨 `force-on` (forced tool use) produces schema-invalid output on every section.**
   `valid rate` **0.000**, 19 errors in 19 sections, and the cause is the same one every
   time: the nested object field `Account Holder Address` comes back as a **JSON string**
   rather than an object —
   `'{"Street_Number":"100",...}' is not of type 'object'`. `forced_tool.honored` is `true`
   and `renamed_properties: 3`, so the toolSpec path is working as designed and the
   *serialization* of nested groups is what is broken. Coercion sees it and refuses
   (`type_family_mismatch`: "string value in a object field"), which is defensible in
   general but leaves an obviously-recoverable value unrepaired. The list field is
   untouched (recall and cell accuracy both 1.000), which is exactly why the earlier WS-05
   study concluded "no measurable effect" — neither metric covers a nested group. **Leave
   `extraction.forced_tool.enabled` off.**
9. **`enforce-escalate` costs +35% over `warn`** ($0.792 vs $0.589) — the first measurement
   of the escalate arm's price, which `FINDINGS.md` recorded as unmeasured. `warn` remains
   free (−2% vs `off`, within spread, and free by construction: zero extra inference).
10. **Do not read the `restate-off` cost as a regression.** $1.948 vs $1.686 looks like
    turning the de-duplication *off* being more expensive, which is backwards; at one repeat
    per document on an agentic cell this is noise. #710's own A/B (recall 1.000 both arms)
    is the authority, and it found the switch safe, not cheaper.

---

## 2.1 The `integrated` + simple hazard — much reduced at v0.6.7, not gone

**What v0.6.7 measures.** On the 7-document grid, `simple/integrated` recall is **0.936**:
six documents at 1.000 and **`manylists_400` at 0.552** — 179 of 400 rows returned, status
`COMPLETED`, scalar accuracy 1.000, per-row cell accuracy 1.000 on the rows that came back.
So the failure mode is unchanged in kind (silent truncation of a list, invisible to field
accuracy) but its incidence has dropped sharply from the v0.6.5 measurement below.

Two things also changed the *decision*:

- **It is no longer the cheap option.** At v0.6.5 `simple/integrated` was the cheapest cell
  in the grid ($0.247) — precisely because it was doing less work. At v0.6.7 it costs
  **$0.768**, i.e. **27% more** than `simple/separate` ($0.603), which is complete.
- **Its confidence is the worst in the grid.** Mean 0.901 with a 2.4% alert rate, against
  0.98–0.996 / 0.2–0.4% for every other cell.

So the recommendation is unchanged and now easier to justify: **use `separate`.** It is
cheaper, complete, and better calibrated. `advanced/integrated` is complete (1.000 on all
7) but is the most expensive cell in the grid at $2.315.

> **This is a single draw per document** (`coresynth` runs `repeats: 1`). The 0.552 is one
> observation, and the six 1.000s are one observation each — the cell is known to be
> **bimodal**, so neither the failure nor the successes should be read as a rate. The
> repeated-measures evidence below is what establishes the hazard; run
> `--suite intconf` to re-establish it at v0.6.7 on a document where it fires.

### The v0.6.5 repeated-measures study (retained — this is the evidence for the hazard)

Because the failure is a *partial or empty list* rather than an error, a single run cannot
establish it. The `intconf` suite runs the integrated cell **and** a `separate` control on
the same document (`longdesc_100`, 100 rows) 4× each. **These are v0.6.5 numbers on the
pre-fix `longdesc_100`** (see the corpus note in the header) and are kept because the
mechanism they identify is what still produces the 0.552 above:

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

Simple vs advanced, Textract TABLES + separate confidence, one transaction list of N rows
(~48 rows/page). **13 of 14 runs returned `COMPLETED`** — see the failure below.
`sec` is the number of sections the classifier produced (the truth is 1 in every row).

| rows | pages | SIMPLE recall | simple $ | wall | sec | ADV recall | adv $ | wall | sec |
|-----:|------:|--------------:|---------:|-----:|----:|-----------:|------:|-----:|----:|
| 25 | 1 | 1.000 | $0.067 | 58s | 1 | 1.000 | $0.142 | 38s | 1 |
| 100 | 3 | 1.000 | $0.215 | 88s | 2 | 1.000 | $0.549 | 237s | 2 |
| 400 | 9 | 1.000 | $0.694 | 148s | 3 | 1.000 | $1.558 | 385s | 1 |
| 800 | 17 | 1.000 **(run ABORTED — see below)** | — | 6205s | 7 | 1.000 | $4.362 | 391s | 4 |
| 1200 | 25 | **1.000** | $2.106 | 715s | 6 | 1.000 | $5.218 | 400s | 6 |
| 1600 | 33 | **1.000** | $2.768 | 550s | 12 | 1.000 | $9.918 | 491s | 8 |
| 3200 | 66 | **0.724** | $4.871 | 775s | 18 | 1.000 | $22.394 | 647s | 17 |

Per-row **cell accuracy is 1.000 in every completed run of both modes** — at no size does
either mode return a row with a wrong value. Every loss here is a *missing* row.

### 🔄 The simple-mode cliff has moved out, and over-splitting is why

At v0.6.5 this table showed a hard silent cliff between 800 and 1,200 rows — recall 0.199
@1,200, 0.088 @1,600, **0.009** @3,200. At v0.6.7 simple mode is **complete through 1,600
rows / 33 pages** and recovers **0.724** at 3,200 rows instead of 0.009.

The `sec` column is the explanation, and it is an uncomfortable one: the same
`sectionSplitting: llm_determined` over-splitting that costs advanced mode 22% (§4) is
**shortening simple mode's per-call output** and thereby defeating the truncation that
caused the cliff. 1,200 rows arrive as 6 sections, 1,600 as 12, 3,200 as 18. v0.6.5's cliff
was measured with **1 section**, because splitting was skipped entirely for a single-class
configuration before #686 was fixed.

> ⚠️ **This is a mechanism, not a proven cause** — it needs a
> `sectionSplitting: disabled` × row-count A/B on one release, which was not run. But it is
> enough to make the §5 recommendation of `sectionSplitting: disabled` **conditional on
> document size**: turning splitting off to save agentic cost may put simple mode back on
> the cliff for very large single-document lists. Do not apply that setting to a corpus with
> >1,000-row tables without measuring completeness first.

The cliff itself has not been *removed*, only pushed out: 0.724 at 3,200 rows is still 883
missing rows reported as `COMPLETED`. And the "a truncated run is cheaper" property that
makes it invisible to cost monitoring is weaker but intact ($4.87 at 3,200 rows is the
highest simple cost in the table, but only 1.8× the complete 1,600-row run despite 2× the
rows).

### 🚨 The confidence pass can fail to converge inside its Lambda

**`simple` @800 rows did not complete.** Extraction finished and recovered all 800 rows
(recall 1.000), then the **Assessment step timed out at 900 s and Step Functions retried it
five times** — 6,205 s of wall clock — before the harness deadline stopped it. The document
is recorded as `ABORTED`.

The Assessment log names the mechanism:

```
Assessment output TRUNCATED at max output tokens; salvaged 3 top-level field(s)
  from the valid prefix (method=truncated_to_last_complete_element).
  Unrecovered rows will be retried over a smaller batch.
Assessment truncated for 'Transactions' over 25 rows; splitting into 12 + 13
  and retrying with smaller batches.
```

Nova Lite hits its 10,000-token output cap on a 25-row confidence batch (the prompt asks for
a confidence, an explanation *and* a bounding box per cell), so the recovery **halves the
batch and retries**. Each retry is a fresh Bedrock call at ~77 s. On this document the
ladder does not converge inside the 900-second Assessment Lambda, so the Lambda dies, Step
Functions restarts the whole section, and it dies again — paying the Bedrock spend every
time.

This is the same defect class as the agentic retry ladder described in the
[v0.6.7 release audit](releases/v0.6.7.md): **a recovery loop with no bound on total elapsed
time, running inside a fixed-duration Lambda.** Both should be bounded by remaining
execution time, and this one should additionally derive its starting `list_batch_size` from
the model's output cap rather than defaulting to 25 and discovering the cap by truncating.

It is a *confidence* failure, not an extraction failure — the rows were all extracted. But
the document ends `ABORTED`, so from the outside it is indistinguishable from losing
everything.

### Advanced mode: completeness holds; cost and wall-clock are the limits

Advanced (agentic sharding) holds **recall 1.000 through 3,200 rows / 66 pages** — sharding
keeps each call small, so neither the truncation nor an input-context limit is hit. The
practical limits are **cost** (up to ~$22/doc at 3,200 rows, ~4.6× simple) and **wall-clock**
(~11 min at 3,200 rows). Cost grows super-linearly in rows above ~800.

---

## 4. Cost: level AND variance (n=5 repeats, same 400-row doc)

Agentic-advanced cost is **high-variance run-to-run** (the agent's turn count is
non-deterministic), so a single sample cannot resolve a cost difference between configs. The
suite measures cost with repeats and reports mean ± stdev + coefficient of variation (CV); a
cost difference is only trustworthy when it exceeds the sampling spread.

Same document (`med_narrow`, 400 rows / 9 pages), 5 repeats per cell, 25 runs, extraction
model Sonnet 5 (the shipped default). All returned `COMPLETED` with **recall 1.000** — so
these are like-for-like cost comparisons of configurations that all did the job.

| config (OCR / mode / assessment) | cost mean ± stdev | CV | min–max | sections/run |
|----------------------------------|-------------------|---:|---------|---|
| Textract TABLES / **simple** / separate | **$0.723 ± $0.006** | **0.8%** | $0.717–0.730 | 2,2,2,3,3 |
| Textract LAYOUT / advanced / separate | $1.856 ± $0.529 | 28.5% | $1.053–2.533 | 1,2,2,5,5 |
| BDA / advanced / separate | $1.871 ± $0.103 | **5.5%** | $1.738–2.013 | 2,2,2,3,3 |
| Textract TABLES / advanced / separate | $2.216 ± $1.048 | 47.3% | $1.586–**4.076** | 1,2,3,3,4 |
| Textract TABLES / advanced / integrated | $2.962 ± $0.630 | 21.3% | $2.460–4.007 | 2,3,3,3,4 |

**Findings**

- **Simple mode is ~2.6–4.1× cheaper than advanced *and* essentially deterministic** (cost CV
  0.8% vs 5.5–47.3%). For budgeting, simple is both cheaper and predictable; agentic cost must
  be planned as a *range*, not a point.
- **The worst case matters more than the mean.** `tt-adv-sep` ranged **$1.59 → $4.08 on the
  same document, same config** — a 2.6× spread. A capacity or cost model built on one sample
  of an agentic cell will be wrong.
- **Part of that variance is the *classifier*, not the agent.** The `sections/run` column is
  the same one-statement document classified 1 to 5 different ways across five identical
  runs. Centred within cell, `r(sections, cost) = 0.37` on the 20 advanced runs, ≈$0.21 per
  extra section — and the two extremes are exactly what that predicts (`tl-adv-sep`: 1
  section → $1.05, 5 sections → $2.53). The simple cell's sections also vary (2–3) and its
  cost does **not** move at all (CV 0.8%), which is the control: a spurious section is nearly
  free for a single call and expensive for an agent loop. **Some of the "agentic cost is
  unpredictable" reputation is over-splitting non-determinism, and it is fixable.**
- **BDA / advanced is the most stable advanced option** (CV 5.5%) and LAYOUT / advanced the
  cheapest at the low end ($1.05 when it gets the section count right). LAYOUT's high CV
  (28.5%) is entirely its two 5-section runs.
- **Why advanced costs more even with the deterministic table tool:** advanced is a multi-turn
  agent loop, and each turn re-sends the growing conversation as *input* tokens — and that
  whole loop is paid **per section**. The v0.6.7 release A/B measures the per-section
  component directly: forcing the section count back to 1 takes `tt-adv-sep` from $1.71 to
  $1.30 (−24%) at a fixed extraction model
  ([release audit](releases/v0.6.7.md)).

> **Model note.** §4 is measured at Sonnet 5, the shipped default, so it is directly
> comparable with §2. The v0.6.6→v0.6.7 cost A/B in the
> [release audit](releases/v0.6.7.md) is measured at **Sonnet 4.6** instead, because that is
> the extraction model both releases can run — do not compare its absolute dollars with this
> table, only its deltas.

> Methodology note: run these cells with `--suite cost` (or `--repeats ≥5`); the harness
> flags any cell with cost CV > 0.25 as unreliable-at-current-n, and
> `aggregate.py --compare` only reports a cost regression when the mean shift exceeds the
> combined sampling spread — so agentic noise never masquerades as a regression. That
> treatment now covers **accuracy and completeness too**, not only cost: `--compare`
> reasons about failure *rates* and mean-vs-spread rather than a single draw
> ([index.md](index.md)). It was cost-only at v0.6.5, which is how a non-deterministic
> completeness swing got reported as an improvement in
> [releases/v0.6.5.md §4](releases/v0.6.5.md).

---

## 5. Recommendations (customer guidance)

| Situation | Recommended configuration |
|-----------|---------------------------|
| Typical documents ≤ ~1,600 rows / ≤ ~33 pages | **simple mode** (complete, per-row-accurate, ~2.6–2.9× cheaper) |
| Table-free / forms corpora | **LAYOUT-only OCR** (cheapest complete option in both modes; best-behaved confidence) |
| Large multi-page tables (> ~1,600 rows) | **advanced mode** (recall 1.000 through 3,200 rows, §3; budget cost as a *range*) |
| Very large docs (> ~3,000 rows / 60 pages) | advanced **and split the document** if feasible (~$22 and ~11 min per document at 3,200 rows, §3) |
| **Single-class configuration, documents under ~1,000 rows** | **`sectionSplitting: disabled`** — the default over-splits 2–3×, which costs advanced mode ~22% and manufactures spurious validation warnings (§2 findings 6–7, §4). ⚠️ **Not** if input can be a packet (§7), and ⚠️ **not** for >1,000-row tables — over-splitting is what currently keeps simple mode complete at that size (§3) |
| Very large lists **+ confidence** | expect the confidence pass to be the fragile part, not extraction — it failed to converge inside its Lambda on an 800-row list (§3). Lower `extraction.confidence.list_batch_size` from 25, or use `confidence.mode: off` and reconcile separately |
| Confidence needed | **`separate`** assessment. Do not use `integrated` with simple extraction (§2.1) — it is now more expensive *and* less complete *and* worse calibrated than `separate` |
| Cheapest OCR, small documents | Bedrock-LLM, **only if identifiers do not matter** — it is the one backend that returns wrong per-row *values* (§2 finding 4) |
| Packets holding several documents of the **same** class | `x-aws-idp-multi-instance` on that class (**migrate baselines**), or `x-aws-idp-instance-array` if it already lists them (free) — §7 |
| New corpus, shape unknown | run `extraction.multi_instance_detection` **once** as a diagnostic, act on what it names, then turn it off — §7 |
| Nested object (group) fields in the schema | leave **`extraction.forced_tool.enabled` off** — forcing returns groups as JSON strings, invalid against the schema on every section (§2 finding 8) |

**Safety notes — every failure mode below is silent, and each one is invisible to at least
one metric you would expect to catch it:**

1. Simple mode truncates large lists while reporting success, and a truncated run is
   *cheaper* than a complete one, so neither status nor cost will alert you. If large tables
   are possible, use advanced mode, or add a schema `minItems` constraint, or reconcile row
   counts downstream.
2. **`integrated` confidence with simple extraction returned 179 of 400 rows on one of seven
   documents at v0.6.7** (and 1–10 of 100, none matching, in 4 of 4 repeats at v0.6.5) — with
   `scalar_accuracy` 1.000 and status `COMPLETED` throughout.
3. **A section holding several documents of the same class returns only the first, and
   per-field accuracy cannot see it** — the fields that came back are scored, and they can
   all be right. A section returning 1 of 3 pay statements scores **1.000**. Nothing in
   §2–§4 of this paper would detect it; only a record count against ground truth, or the
   §7 detection probe, will.
4. **A whole *column* can be empty at recall 1.000.** `completeness_recall` counts rows, and
   the row tag lives in one field; return every row with every `Amount` null and it still
   scores 1.000, as does `scalar_accuracy` (which only reads document-level fields). §2 adds
   per-row **cell accuracy** for exactly this. If you build your own quality gate, gate on a
   per-column non-null rate, not a row count.
5. **The two extraction modes disagree about what to do with an unreadable column, and one
   of them is worse.** Given a column OCR could not read at all, simple extraction returned
   `null` for every row — which the new default validation reports as 100 required-property
   issues — while advanced returned a fabricated **`0.0`** for every row, which is
   schema-valid and passes silently. Abstention is recoverable; a plausible zero in a
   financial field is not. (Observed on a corpus document whose amounts were physically
   overprinted — see the header note — so treat it as a characterization of behaviour on
   unreadable input, not a rate.)

---

## 6. Product improvement backlog (surfaced by this study)

1. **🚨 `sectionSplitting: llm_determined` over-splits, and on the agentic path it is a
   ~22% bill increase (P0).** #726. Every cell in §2 is over-split 2–3×; §4 shows the same
   document classified 1 to 5 ways across five identical runs, and the
   [release audit](releases/v0.6.7.md) prices the fix at −24% for `tt-adv-sep`. It also
   manufactures spurious `required property` validation issues on continuation sections, and
   it is a large part of what makes agentic cost look unpredictable. The over-split is
   model-dependent (Sonnet 5 classification gets it right 5/5, Nova 2 Lite 0/5), so a
   better default prompt for the small classifier — not a bigger model — is the fix.
2. **🚨 Forced tool use serializes nested object fields to JSON strings (P0 for the
   feature).** `extraction.forced_tool.enabled: true` makes **every** section
   schema-invalid where the class has a group field (§2 finding 8). Coercion sees the value
   and refuses it as a type-family mismatch; parsing a string that is valid JSON for an
   object-typed field is a safe, obvious repair. Until both are fixed the flag cannot be
   recommended, and "experimental / off by default" is the right status.
3. **⚠️ Integrated confidence + simple extraction still truncates lists (P1, improved).**
   0.552 recall on one of seven documents at v0.6.7, versus a 0.294 grid mean at v0.6.5.
   Refuse the `integrated` + simple combination for list-bearing schemas (route to a
   separate confidence pass or sharded advanced), or fail the section loudly.
4. **Silent truncation needs detection, not just documentation (P0).** Both failure modes
   above return `COMPLETED`. Compare extracted row count against schema `minItems` (or an
   OCR-derived row estimate) and surface a completeness warning/metric. Note the recovered
   prefix *shrinks* with document size and cost *falls*, so no existing signal catches it.
5. **⚠️ Advanced mode fabricates a value where simple mode abstains.** On an unreadable
   column advanced returned `0.0` for all 100 rows — schema-valid, silent — where simple
   returned `null` and the new default validation raised 100 issues. A model that cannot read
   a cell should emit `null`, and the agent prompt should say so explicitly; a plausible
   zero in a financial field is the worse of the two failures.
6. **🚨 Two recovery ladders that outlive their Lambda (P0/P1).** Both are loops with no
   bound on total elapsed time running inside a fixed-duration function, and both were
   observed failing in this study:
   - **Confidence batch-splitting (P0, observed to lose a document).** A truncated
     confidence batch is halved and retried; on an 800-row list the ladder never converges
     inside the 900 s Assessment Lambda, so the document ends `ABORTED` after 5 timeouts and
     6,205 s (§3). Bound it by remaining execution time, **and** derive the initial
     `list_batch_size` from the confidence model's output cap instead of defaulting to 25
     and discovering the cap by truncating.
   - **Agentic network retry (P1).** `invoke_agent_with_retry` is `max_retries=50,
     max_delay=1800` inside a 900-second Extraction Lambda, so one transient Bedrock
     read-timeout loses the whole invocation and Step Functions repeats the extraction from
     scratch. Unchanged since v0.6.6 — see the [release audit](releases/v0.6.7.md).
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
**separate measurements from §2–§4** above — a different stack, a different build, and in
one case a different classification model — and they are not restated here. Costs are
estimates from `config_library/pricing.yaml`, rates as of 2026-09-02.

> ⚠️ **Where §7 and §2–§4 disagree, the difference is usually the classification model.**
> §7's `sectionSplitting` measurement reports `llm_determined` at `sections_correct` **1.00**;
> §2 finds every cell over-split 2–3× and §4 finds the same document classified 1 to 5 ways
> across five identical runs. Both are right: §7 held `classification.model` at **Sonnet 5**,
> while §2–§4 use the **shipped default Nova 2 Lite**. Holding everything else fixed and
> changing only that knob gives 1 correct section in 5 of 5 runs on Sonnet 5 and 0 of 5 on
> Nova 2 Lite (see the [release audit](releases/v0.6.7.md)). Read §7's splitting result as
> "the prompt works on a strong classifier", not "splitting is correct by default".

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

- Per-(cell,doc) scores for **this** edition are in the working tree:
  - §2 / §2.1 — `benchmarks/results/v0.6.7/coresynth__extraction-model-sonnet5/`
  - §3 — `benchmarks/results/v0.6.7/scaling__extraction-model-sonnet5/`
  - §4 — `benchmarks/results/v0.6.7/cost__extraction-model-sonnet5/`
  (`benchmarks/results/v0.6.7/cost/` is a **different** measurement: the Sonnet 4.6
  cross-version arm of the release A/B. Do not mix it with §4.)
- The v0.6.5 slices this edition replaces are pruned per
  [`RETENTION.md`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/benchmarks/results/RETENTION.md)
  (one complete set per release); restore them from git with:
  ```bash
  git checkout ec3eb05ae -- benchmarks/results/v0.6.5-config-core/ \
      benchmarks/results/v0.6.5-config-scaling/ benchmarks/results/v0.6.5-config-cost/ \
      benchmarks/results/v0.6.5-intconf-sonnet5/ benchmarks/results/v0.6.5-intconf-sonnet46/
  ```
- Figures: `images/benchmark-scaling.png` — **the committed figure plots the v0.6.5
  curve** (the cliff at 1,200 rows). §3's table supersedes it; the figure was not
  regenerated, so read the table, not the picture.
- Corpus manifest + generators: `benchmarks/corpus/` (regenerable; PDFs/configs gitignored)
- Matrices + methodology: `benchmarks/matrices/`
- Measured spend for this edition: **$131.72** (§2, 133 runs) + **$55.13** (§3, 14 runs) +
  **$48.14** (§4, 25 runs) = **$234.99** over 172 document runs, priced from `pricing.yaml`.
  The supporting release-A/B and mitigation runs cited in §4 add **$81.17** over 70 runs
  (`v0.6.6/cost`, `v0.6.7/cost`, `v0.6.7/advsplitcost__section-splitting-disabled`,
  `v0.6.7/advsplitcost__classification-model-sonnet5`). The `__<slug>` suffix is the
  `--set` override the grid ran with — see
  [`RETENTION.md`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/benchmarks/results/RETENTION.md).

```bash
source .venv/bin/activate && export PYTHONPATH=$PWD/lib/idp_common_pkg
python3 benchmarks/harness/gen_corpus.py

# §2 cross-config grid, §3 scaling, §4 cost variance — at the PRODUCT DEFAULT model
# (the committed default_cell holds extraction_model at the cross-version A/B control,
#  so a single-release study overrides it explicitly)
for s in coresynth scaling cost; do
  python3 benchmarks/harness/make_configs.py --suite $s --class bank_statement --set extraction_model=sonnet5
  AWS_PROFILE=default python3 benchmarks/harness/run_matrix.py --stack <STACK> --suite $s \
      --set extraction_model=sonnet5 --max-inflight 20
  AWS_PROFILE=default python3 benchmarks/harness/aggregate.py --run benchmarks/results/run-<stamp> --out benchmarks/results/v0.6.7/$s
done
# NOTE: --set must be repeated on run_matrix.py, not only make_configs.py — the two are
# namespaced by the override set, so omitting it there reads a DIFFERENT variant's plan.

# §2.1 the integrated-confidence hazard, with repeats + same-doc control
python3 benchmarks/harness/make_configs.py --suite intconf --class bank_statement --set extraction_model=sonnet5
AWS_PROFILE=default python3 benchmarks/harness/run_matrix.py --stack <STACK> --suite intconf --native-upload

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

**Honesty / limits.** Costs are estimates from `pricing.yaml` (rates as of 2026-09; intro
pricing may apply). §2 is one run per (cell, doc) — reliable for the *exact* completeness and
accuracy measures, not for per-cell cost, which is what §4 is for, and not for a single
sub-1.000 observation, which is one draw from a known-bimodal cell. No reference (real,
labeled) corpus and no one-axis sweeps (geometry, escalation, models, reasoning effort) were
run for this release; those sections are omitted rather than carried forward from v0.6.0.
`longdesc_100` was regenerated for this edition after a rendering defect was found in it —
see the header note; its v0.6.5 numbers are not comparable.

---
> See the [Benchmarking Guide](./index.md) for how this suite is designed and run,
> the [Release Audit Trail](releases/) for release-over-release comparisons, and the
> [Extraction Scaling Guide](../extraction-scaling-guide.md) for size-based mode selection.
