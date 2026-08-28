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

## Residual, NOT caused by these fixes — and NOT yet a defect

3 of 4 `core-tt-adv-int` runs raise `assessment_incomplete` (severity **error**):
*"1 list row(s) could not be confidence-scored"* — 1 row of 100, confined to the
**integrated** cell, absent from all four `separate` runs. This is confidence
*coverage*, not extraction data loss.

⚠️ **Read with the config in hand: these cells ran with the escalation rung of the
self-healing ladder switched OFF.**

```
escalation_enabled = False          # the model-escalation rung, disabled
escalation_model   = us.anthropic.claude-sonnet-5:1m
geometry.mode      = ocr_only       # the documented per-row-output remedy, already applied
```

`assessment_incomplete` fires only when rows remain unscored **after the full
ladder** (shrink batch → retry → escalate model). With `escalation_enabled: false`,
`batching.py` sets `ladder_escalation_model = None`, so the third rung never ran —
the ladder had shrink+retry only. And that is not an accident of this run: the
benchmark's `default_cell` sets `escalation: "off"` deliberately, so escalation cost
does not confound cross-cell cost comparisons.

So this is a reproducible **observation on a configuration that declines the
recovery mechanism built for exactly this case** — not a demonstrated product
defect, and not a demonstrated inherent limit either. The run that would settle it
is `advverify` with `--set escalation=on`: if the row recovers it is a config
artifact; if it does not, it is a real ceiling worth chasing.

What the data *does* support: **integrated is more exposed than separate.** That is
the expected direction — integrated carries confidence in one larger response, so a
batch is likelier to hit the output ceiling, while `separate` gives the confidence
model its own budget. It corroborates the standing guidance to prefer `separate` on
list-bearing schemas rather than revealing anything new.

No before/after claim is made against the pre-fix run either: it had zero rows to
score.

It **is** surfaced properly — `assessment_incomplete` is severity `error`, so the
processing report's status line reads `COMPLETED WITH ERRORS`, the issue is listed
with its root cause, and the section shows an `error` status indicator in the UI's
Processing Issues column. Nothing here is silent.

## Cost note

Per-cell cost CV was 0.25 at n=4 on both cells, so these 8 runs do **not** support
a cost comparison between the cells (the harness said so: *"high cost variance
(CV>0.25) — increase repeats"*). Use the `cost` suite for that.
