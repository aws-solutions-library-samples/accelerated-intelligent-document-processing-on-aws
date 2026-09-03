# #653 boundary detection — a Sonnet-5 factorial with no detection power

> ## ⚠️ Superseded. Read this first.
>
> This file originally concluded that the #653 prompt block was **unvalidated** and
> that the "0% → 60%" figure should be **retracted**. Both conclusions were wrong,
> and the error was mine:
>
> * **The block is validated.** GitLab **!769** measured the same rules on
>   **DocSplit-Poly-Seq** — 500 packets, 7,330 pages, 2,027 sections, 5,000
>   packet-runs, five models, 0 failures. Split accuracy improves on four of five
>   models (Qwen3-VL +0.117, Opus 5 +0.040, Nova 2 Lite +0.030, Sonnet 5 +0.013,
>   all p<0.05) and **regresses on none**; under-split rate is 0.000 in all ten
>   cells. On #653's reported 2-page form Sonnet 5 goes 6/24 → 10/10; on a 4-page
>   packet of two copies of one form, 1/10 → 5/5. See
>   [../boundary/FINDINGS.md](../boundary/FINDINGS.md) § Superseded by !769.
> * **The 0% → 60% figure stands.** It was measured on **Nova 2 Lite** (the shipped
>   default). This factorial ran on **Sonnet 5**. I retracted a Nova-2-Lite result
>   because a Sonnet-5 run did not reproduce it — a cross-model comparison, which
>   is not a retraction of anything. !769 independently measures the unpaginated
>   case at **0/5 → 3/5**, i.e. the same 0% → 60%.
> * **Why this run found nothing.** !769 puts Sonnet 5 at **+0.013**, the smallest
>   significant effect of the five models. Three clean synthetic documents at n=5
>   cannot resolve +0.013. This is a null result from an underpowered test, not
>   evidence of absence — and the corpus limitation was already documented in the
>   sibling findings file before I wrote this one.
>
> What still stands from this run, unaffected: the `split-disabled` result below,
> and the fact that no arm loses rows.


**Run:** 2026-09-03, stack `IDP1` (us-west-2, acct 912625584728), develop @ v0.6.7.dev5
plus PR #744. Classification model `us.anthropic.claude-sonnet-5` throughout — the
model #653 was reported against, and the stricter test because Sonnet 5 rejects
`temperature`/`top_p`/`top_k` (they are stripped) and therefore samples.
**Costs are estimates** from `config_library/pricing.yaml`, rates as of 2026-09-02.

## Result: 90 runs, six arms, no difference anywhere

`sections_correct` is 1.0/0.0 per run, so the mean over 5 repeats **is** the pass
rate. Expected sections: `paginated_3pg` 1, `small_narrow` 1, `twodocs_2x20` 2.

| prompt | `classification.confidence` | `ocr.image.dpi` | paginated_3pg | small_narrow | twodocs_2x20 |
|---|---|---|---|---|---|
| pre-#653 | `topk` | 300 | 1.00 | 1.00 | 1.00 |
| post-#653 | `topk` | 300 | 1.00 | 1.00 | 1.00 |
| pre-#653 | `off` | 300 | 1.00 | 1.00 | 1.00 |
| post-#653 | `off` | 300 | 1.00 | 1.00 | 1.00 |
| pre-#653 | `off` | 150 | 1.00 | 1.00 | 1.00 |
| post-#653 | `off` | 150 | 1.00 | 1.00 | 1.00 |

Completeness recall was 1.0 in every run of every arm — no arm loses rows.

**The instrument is not vacuous.** In the same runs, `split-disabled` on
`twodocs_2x20` scores **0.00 in every condition** (it emits one all-pages section
where two are correct). So `sections_correct` does discriminate, and the 1.00s are
a real result rather than a broken metric.

## What this run does and does not show

It shows that on **three clean synthetic documents at Sonnet 5**, the pre-fix and
post-fix prompts are indistinguishable — 1.00 everywhere. Given !769 measures
Sonnet 5's true effect at +0.013, that is the expected outcome of a test with no
power at that effect size, and it should not have been written up as though the
feature were unsupported.

**The instrument is not vacuous.** In the same runs, `split-disabled` on
`twodocs_2x20` scores **0.00 in every condition** (it emits one all-pages section
where two are correct). So `sections_correct` does discriminate; the 1.00s are a
real measurement of a real null, on documents both prompts handle.

Completeness recall was 1.0 in every run of every arm — no arm loses rows.

## Why the corpus probably cannot reproduce it

The generator writes *clean* documents: unambiguous opening header blocks, a
distinct account number per copy, and (for `paginated_3pg`) explicit `Page N of M`
footers. Both prompts get such documents right. #653 was reported against a real
4-page file containing two documents of the **same type**, which is the one shape
that stresses the `CRITICAL - consecutive documents of the same type` clause; that
file was requested on the issue and never obtained, and `twodocs_2x20` is a
synthetic stand-in whose copies are easier to tell apart than a real pair.

Both `class_confidence` and `ocr_dpi` were added as axes here specifically to rule
them out as confounds, and both were ruled out.

## Recommended next step

**Keep the block.** !769 is the deciding evidence and it is overwhelming relative to
this run.

The useful lesson here is about the corpus, not the fix: this synthetic corpus
cannot detect a boundary-prompt effect at Sonnet 5, because its documents carry
unambiguous opening header blocks, distinct account numbers per copy, and explicit
`Page N of M` footers. Any future boundary work should be measured on
DocSplit-Poly-Seq (or a corpus like it) rather than here, or on this corpus only at
a model where the effect is large (Qwen3-VL, +0.117).

The `split-disabled` result is separately actionable and unaffected: **it is wrong by
construction on multi-document files** (0/5, one section where two are correct), so it
must not be recommended as a workaround for packets.

## Reproducing

```bash
python3 benchmarks/harness/gen_corpus.py --only small_narrow,paginated_3pg,twodocs_2x20
# one line per arm; --set values are namespaced into the config + index filenames
for S in boundary boundaryctl; do
  python3 benchmarks/harness/make_configs.py --suite $S --class bank_statement \
     --set classification_model=sonnet5 --set class_confidence=off --set ocr_dpi=150
  AWS_PROFILE=default python3 benchmarks/harness/run_matrix.py --stack <STACK> \
     --suite $S --class bank_statement \
     --set classification_model=sonnet5 --set class_confidence=off --set ocr_dpi=150 \
     --max-inflight 20
done
```

Drop `--set ocr_dpi=...` / `--set class_confidence=...` for the shipped-default arms.
