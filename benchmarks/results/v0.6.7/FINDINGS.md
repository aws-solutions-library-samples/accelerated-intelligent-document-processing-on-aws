# Wave 0 — what the re-instrumented enforcement A/B actually measures

Measured live on IDP1 (`v0.6.7.dev3`, us-west-2, account 912625584728), 2026-09-02.
28 document runs across three suites. Every cell pair differs on **one axis**, on
one deployed stack with identical code, so a delta is attributable to the knob.

The point of this run was not to re-answer "does coercion help". It was to find out
whether the harness could *see* the answer — the previous A/B could not, in three
independent ways, and all three are fixed here. See the commit for
`benchmarks/harness/analyze.py`.

---

## 1. Coercion changes accuracy by nothing — now measured with metrics that could see it

`valuenoise_100`: 100 transaction rows rendered the way real statements render them
(`$1,234.00`, `EUR 1.234,56`, `(13.70)`, `1'234.56`, `13 Feb 2024`, `Feb 13, 2024`)
with the **typed** value as ground truth. **200 cells compared per run.**

| cell | runs | `cell_accuracy` | cells compared | `completeness_recall` |
|---|---|---|---|---|
| `enforce-off` | 3 | **1.0000** | 200 | 1.0 |
| `enforce-warn` | 3 | **1.0000** | 200 | 1.0 |

`kv_form`: 8 typed scalar fields (`"$685.50"` → `685.5`, `"3.90%"` → `3.9`,
`"05/14/1985"` → `"1985-05-14"`).

| cell | runs | `typed_accuracy` | `scalar_accuracy` |
|---|---|---|---|
| `enforce-off` | 3 | **1.0000** | 0.6800 |
| `enforce-warn` | 3 | **1.0000** | 0.6800 |

**The model already normalizes.** With coercion switched fully **off**, Sonnet 4.6
returned `1234.0` from `$1,234.00`, `1234.56` from the European `1.234,56`, `-13.7`
from `(13.70)`, and ISO dates from `13 Feb 2024` — at 200 cells per run, perfectly.
Coercion had nothing to repair, so the arms are identical.

This is a *stronger* version of the earlier finding, not a repeat of it. Previously
"zero delta" was unfalsifiable because nothing measured per-row values; now 200
cells per run are compared and both arms are perfect.

### The 0.6800 is the mis-instrumentation, quantified

`typed_accuracy` and `scalar_accuracy` disagree on the **same, perfect** extraction:
1.0000 vs 0.6800. `scalar_accuracy` compares against the text the document
*renders*, so a pipeline that correctly returns the number `685.5` is scored wrong
for not returning the string `"$685.50"`. **A metric like that reports a correct
value-normalizing pipeline as 32% wrong.** That is why the first enforcement run
looked uninformative rather than mis-instrumented, and it is the reason
`typed_accuracy` was added alongside rather than replacing it.

### Cost: no reportable difference, as designed

| cell | n | mean | min–max | CV |
|---|---|---|---|---|
| `enforce-off` | 6 | $0.1522 | 0.1456–0.1641 | 0.045 |
| `enforce-warn` | 6 | $0.1547 | 0.1442–0.1640 | 0.051 |

The +1.6% mean gap sits well inside the per-run spread, so it is **not** a
measurement of anything. The real guarantee that `warn` is free is structural and
asserted directly in unit tests (`test_warn_costs_no_inference`,
`test_reject_marks_the_section_failed_without_inference` — `spy.assert_not_called()`):
zero extra Bedrock calls. A call-count assertion is stronger than a noisy dollar
delta.

**Wall-clock is not reportable at n=3.** `enforce-warn` on `small_narrow` ranged
43.3 s → 365.5 s. Do not read the means.

---

## 2. `sectionSplitting: llm_determined` over-splits — filed as #726

The measurement this suite was built for turned up something bigger than cost.

`small_narrow` is **one** 3-page bank statement. With `llm_determined` (the
shipped default, now doing real work for single-class configs after the #686 fix)
it is fragmented into 2–3 sections in **14 of 17 runs (82%)**:

| cell | runs | sections per run | rows recovered | recall |
|---|---|---|---|---|
| `split-disabled` | 5 | `1,1,1,1,1` | 100 ×5 | 1.0 ×5 |
| `split-llm` | 5 | `1,2,3,2,2` | 100 ×5 | 1.0 ×5 |
| `enforce-off` | 6 | `3,3,3,2,2,3` | 100 ×6 | 1.0 ×6 |
| `enforce-warn` | 6 | `3,2,1,1,2,3` | 100 ×6 | 1.0 ×6 |

**Confirmed from the persisted `document_boundary` signal**, not inferred — the
diagnostic PR #694 added for exactly this:

```
3-section run : boundaries={1:'start', 2:'start',    3:'start'}    -> 3 sections
1-section run : boundaries={1:'start', 2:'continue', 3:'continue'} -> 1 section
disabled      : boundaries={1:None,    2:None,       3:None}       -> 1 section
```

So the splitting logic is faithful; **boundary-detection accuracy** on a
homogeneous multi-page document is the problem. With one class there is no type
change to anchor on, and page 2 of a repeating table looks like a new statement.

It is silent: recall is 1.0 in every run, status is COMPLETED, cost is unchanged.
What changes is the output *shape* — 3 result files, 3 evaluation sections, 3
reporting rows, 3 UI sections for one statement, with `Account Number: null` on
the continuations, which reads as extraction failure rather than continuation.

---

## 3. The #686 cost increase, measured: real but small **here**

| cell | n | classification cost | total cost |
|---|---|---|---|
| `split-disabled` | 5 | **$0.00002** | $0.1482 |
| `split-llm` | 5 | **$0.00228** | $0.1484 |

A ~114× increase in the classification phase, but **+1.5% of the document** —
extraction dominates at ~$0.148.

**Do not generalize that to "negligible".** Classification cost scales with **page
count** (~$0.0008/page here); extraction cost scales with **content**. On a
page-heavy, field-light corpus — a 100-page scanned packet with a dozen fields —
the same per-page charge becomes the dominant cost rather than a rounding error.
The honest summary is "$0.0008/page, compare it against your own extraction cost",
not a single percentage.

---

## Conclusions

1. **Coercion is a safety net, not an accuracy improver.** Confirmed at 200
   cells/run and 8 typed scalars, with both arms perfect. Keep it on: it costs no
   inference and its value accrues to consumers that are *not* format-tolerant
   (Athena column typing, rule validation, the SDK `fields` contract, HITL
   display), not to the evaluator.
2. **Validation at `warn` is free by construction**, proven by call-count
   assertions rather than by a dollar delta the data cannot resolve.
3. **The release's real risk is not enforcement — it is `llm_determined`
   over-splitting** (#726). That is the finding a working harness bought.
4. **A benchmark can be confidently wrong.** Two of the three metrics that matter
   here did not exist a day ago, and the one that did would have scored a perfect
   extraction at 0.68. Any future "no change" result should first be asked whether
   the instrument could have detected a change at all.

## Supersedes the first (mis-instrumented) enforcement run

The earlier `v0.6.7.dev1/enforcement/` set is **removed**, per
`benchmarks/results/RETENTION.md` (one set per release; a superseded slice is not
retained once its finding is in prose). Its conclusion — "coercion is a safety net,
not an accuracy improver" — is carried forward above and now rests on 200 measured
cells per run instead of a metric that could not see the feature. Recover it with:

```bash
git checkout d1d354e06 -- benchmarks/results/v0.6.7.dev1/
```

## Reproducing

```bash
python3 benchmarks/harness/gen_corpus.py --only kv_form,valuenoise_100,small_narrow
python3 benchmarks/harness/make_configs.py --suite enforcement --class bank_statement
python3 benchmarks/harness/make_configs.py --suite enforcement --class kv_form
python3 benchmarks/harness/make_configs.py --suite splitcost   --class bank_statement
AWS_PROFILE=default python3 benchmarks/harness/run_matrix.py --stack IDP1 --suite enforcement --class bank_statement
AWS_PROFILE=default python3 benchmarks/harness/run_matrix.py --stack IDP1 --suite enforcement --class kv_form
AWS_PROFILE=default python3 benchmarks/harness/run_matrix.py --stack IDP1 --suite splitcost   --class bank_statement
```

Costs are estimates from `config_library/pricing.yaml`. The `enforcecost` suite
(the `escalate` arm — the only enforcement cell that spends money per failure) was
**not** run here and remains unmeasured.
