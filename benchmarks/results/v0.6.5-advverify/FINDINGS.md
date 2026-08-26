# Live verification: the tool-decline data-loss fix (#666 / #668)

Targeted re-run of the **exact** cell/doc/model that lost 100 rows, on the same
stack. 2 cells x 1 doc x 4 repeats = 8 runs.

- Stack: IDPBench065, `stack_version` 0.6.6.dev1 (deployed from `develop` after
  #665/#666/#667 merged)
- Doc: `longdesc_100.pdf` (100-row transaction list, exact ground truth)
- Model: Sonnet 5 (`--set extraction_model=sonnet5`), `config integrity: 2 cell(s)
  match their index`

## Result: 8/8 returned 100/100 rows

| cell | repeat | rows | table tool used | agent declined a recommended tool | `completeness_check.complete` |
|---|---|---|---|---|---|
| core-tt-adv-int | 0 | 100 | no | **yes** | true |
| core-tt-adv-int | 1 | 100 | no | **yes** | true |
| core-tt-adv-int | 2 | 100 | no | **yes** | true |
| core-tt-adv-int | 3 | 100 | no | **yes** | true |
| core-tt-adv-sep | 0 | 100 | no | **yes** | true |
| core-tt-adv-sep | 1 | 100 | yes | no | true |
| core-tt-adv-sep | 2 | 100 | no | **yes** | true |
| core-tt-adv-sep | 3 | 100 | yes | no | true |

Recall 1.000 and scalar accuracy 1.000 in all 8.

**Before** (`bench-longdesc_100-20260826-155001`, same cell/doc/model/stack):
`Transactions: null` — 0 of 100 rows, `completeness_check.summary` = *"All schema
constraints satisfied"*, status COMPLETED, scalar accuracy 1.000.

## What this isolates

**In 6 of 8 runs the agent still DECLINED the recommended table tool** — for
substantially the same reason as the failure (*"The parsed table columns were
unnamed/generic and the Amount column was heavily OCR-corrupted mixed with
invoice/terminal text, so automatic column mapping wasn't reliable"*) — **and
extracted the table directly instead.** Same decision, opposite outcome. That is
the prompt rule doing exactly its job: declining the tool is no longer treated as
license to return nothing.

`metadata.validation` is `null` in every run, confirming
`extraction.agentic.validation.enabled` was false — so the in-loop retry (#666
layer 2) was **inert in this build**, and the recovery is attributable to the
prompt rule (layer 1) alone. #668 un-gates that retry; on this evidence it is a
safety net rather than the load-bearing fix, which is the better outcome.

Layer 3 is confirmed working too: `completeness_check` now carries the new
`unexplained_empty_lists` / `complete` fields, and `population_check` reports 4/4
fields populated (it was 3/4 with `Transactions` empty).

## Residual, NOT caused by these fixes

3 of 4 `core-tt-adv-int` runs raise `assessment_incomplete` (severity **error**):
*"1 list row(s) could not be confidence-scored"* — 1 row of 100, confined to the
**integrated** cell, absent from all four `separate` runs. This is confidence
*coverage*, not extraction data loss, and it is consistent with the standing
guidance to prefer `separate` on list-bearing schemas. It is not comparable to the
pre-fix run, which had zero rows to score, so no before/after claim is made here —
it needs its own investigation.

## Cost note

Per-cell cost CV was 0.25 at n=4 on both cells, so these 8 runs do **not** support
a cost comparison between the cells (the harness said so: *"high cost variance
(CV>0.25) — increase repeats"*). Use the `cost` suite for that.
