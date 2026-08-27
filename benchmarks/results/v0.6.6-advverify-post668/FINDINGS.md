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

## Reproduced residual: `assessment_incomplete` (NOT caused by these fixes)

`assessment_incomplete` (severity **error**, *"1 list row(s) could not be
confidence-scored"*) fired in 3 of 4 `core-tt-adv-int` runs (repeats 0, 1, 3) and
**0 of 4** `core-tt-adv-sep` runs — matching the previous run exactly.

Across both sessions: **6 of 8 integrated runs, 0 of 8 separate runs.** That makes it
a reproducible defect rather than a one-off, confined to `integrated` confidence
mode. It is confidence *coverage*, not extraction data loss — recall is 1.000 — and
it is consistent with the standing guidance to prefer `separate` on list-bearing
schemas.

It is **not diagnosed and not fixed**, and no before/after claim is made: the
pre-fix run had zero rows to score, so there is nothing to compare against. Needs
its own investigation.

## No cost claim from this run

`core-tt-adv-int` cost CV = **0.47** at n=4 (the harness emitted its own
`high cost variance (CV>0.25) — increase repeats` warning). The `int` mean ($0.794)
reads above `sep` ($0.634), the same direction as earlier findings, but at that
spread these 8 runs do not support the comparison. Use the `cost` suite.
