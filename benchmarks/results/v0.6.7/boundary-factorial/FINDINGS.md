# #653 boundary detection — the prompt change is unvalidated

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

## What this means, stated carefully

The honest conclusion is **not** "the #653 prompt block does nothing". It is:

> **This corpus cannot reproduce the bug #653 reports, so it cannot validate the
> fix either.** The prompt change shipped on-by-default and is currently supported
> by no measurement.

Two things follow:

1. The `<boundary-detection-rules>` block (~652 tokens, inside the cacheable
   classification prefix, sent per page) is **unjustified by evidence**. It is also
   not harmful here: no regression on any shape, no row loss.
2. An earlier, less controlled measurement in the development of PR #737 reported
   the unpaginated 3-page case going 0% → 60%. **That does not reproduce.** It was
   taken before #731 (classification confidence) and #740 (OCR dpi 150 → 300)
   landed, and without the pre-fix control arm this factorial provides. Treat the
   0% → 60% figure as retracted.

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

Do **not** draw a conclusion about the prompt from this. Either obtain a
reproducing document and re-run this factorial against it, or revert the block as
unjustified. Keeping it silently as "probably helps" is the one option the evidence
does not support.

The `split-disabled` result is separately actionable and solid: **it is wrong by
construction on multi-document files** (0/5, one section where two are correct), so
it must not be recommended as a workaround for packets.

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
