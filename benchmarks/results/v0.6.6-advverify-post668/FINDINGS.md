# Post-#668 no-harm check, and a reproduced residual

Second `advverify` run, after [#668](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/pull/668)
un-gated the empty-declared-list check so it no longer sits behind
`extraction.agentic.validation.enabled` (default `false`).

- Stack: IDPBench065, `stack_version` 0.6.6.dev1, commit `612f3225b`
- Suite: `advverify` (`--set extraction_model=sonnet5`), `longdesc_100.pdf`, 2 cells x 4 repeats
- Companion run before #668: [`../v0.6.5-advverify/`](../v0.6.5-advverify/FINDINGS.md)

## What this run is for

**A no-harm check, not a new proof of the fix.** #668 made the validator callback
get built on every Advanced-mode section with table evidence, where previously it
was `None` on default configs. The risk that introduces is to the *happy path* —
an extra check running where nothing ran before. This run exercises exactly that.

It does **not** exercise the newly-reachable retry: the retry only fires when every
declared list comes back empty, and on this document the list is never empty. So
"the ungated retry works end-to-end" remains **unverified live** and is carried by
unit tests only.

## Result: no harm

| cell | runs | failures | recall | scalar accuracy |
|---|---|---|---|---|
| `core-tt-adv-int` | 4 | 0 | **1.000** | 1.000 |
| `core-tt-adv-sep` | 4 | 0 | **1.000** | 1.000 |

All 8 returned 100/100 rows and `completeness_check.complete = true`. Combined with
the 8 runs in `v0.6.5-advverify`, that is **16/16 at 100/100** against a pre-fix
baseline of `Transactions: null` (0 of 100).

The agent **declined the recommended table tool in 5 of these 8 runs** and extracted
the table directly anyway — the same behaviour the prompt rule was written to make
safe, and consistent with 6 of 8 in the previous run.

## Reproduced residual: `assessment_incomplete` — reproducible, but NOT a defect

`assessment_incomplete` (severity **error**, *"1 list row(s) could not be
confidence-scored"*) fired in 3 of 4 `core-tt-adv-int` runs (repeats 0, 1, 3) and
**0 of 4** `core-tt-adv-sep` runs — matching the previous run exactly. Across both
sessions: **6 of 8 integrated runs, 0 of 8 separate runs.**

⚠️ **Correction to the first version of this file, which called it "a reproducible
defect". It is a reproducible *observation on a configuration that switched off the
recovery mechanism built for exactly this case.*** Both cells ran with:

```
escalation_enabled = False          # the model-escalation rung, disabled
escalation_model   = us.anthropic.claude-sonnet-5:1m
geometry.mode      = ocr_only       # the documented per-row-output remedy, already applied
```

`assessment_incomplete` fires only when rows remain unscored **after the full
self-healing ladder** (shrink batch → retry → escalate model). With
`escalation_enabled: false`, `batching.py` sets `ladder_escalation_model = None`, so
the third rung never ran. That is deliberate in the benchmark, not a mistake here:
`default_cell` holds `escalation: "off"` so escalation cost does not confound
cross-cell cost comparisons — which makes these cells the wrong instrument for
judging whether the ladder can recover the row.

**The run that settles it:** `advverify` with `--set escalation=on`. If the row
recovers, this is a config artifact. If it does not, it is a real ceiling worth
chasing. Until then it is neither a product defect nor an inherent limitation.

What the data *does* support: **integrated is more exposed than separate**, the
expected direction — integrated carries confidence in one larger response, so a
batch is likelier to hit the output ceiling, while `separate` gives the confidence
model its own budget. That corroborates the standing guidance to prefer `separate`
on list-bearing schemas rather than revealing anything new.

Not diagnosed further because the raw `split_stats` (carried on the issue's
`details`, and the one thing that would say *why* that row failed) became
unreadable: the stack was torn down and its KMS key entered `PendingDeletion`
before the section artifacts were pulled. **Pull section artifacts before tearing a
stack down.**

It **is** surfaced properly. Severity `error` makes the processing report's status
line read `COMPLETED WITH ERRORS`; the issue is listed with its root cause (
confidence model, geometry mode, derived-vs-configured batch size, truncated-call
count, escalation chain); and the section renders an `error` status indicator in the
UI's Processing Issues column with a popover, plus the full text in the Processing
Report tab. Nothing about it is silent.

## No cost claim from this run

`core-tt-adv-int` cost CV = **0.47** at n=4 (the harness emitted its own
`high cost variance (CV>0.25) — increase repeats` warning). The `int` mean ($0.794)
reads above `sep` ($0.634), the same direction as earlier findings, but at that
spread these 8 runs do not support the comparison. Use the `cost` suite.
