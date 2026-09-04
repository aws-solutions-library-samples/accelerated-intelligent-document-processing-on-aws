# Boundary detection (#653/#726) — a controlled measurement

> **Superseded in part.** Two conclusions below — that the fix does nothing for
> paginated documents, and that the over-merge case could not be reproduced — held
> only for this run's 3-document corpus on one model. GitLab !769 measured the same
> prompt at a scale this run could not, and both conclusions do not survive it. See
> [§ Superseded by !769](#superseded-by-769). Everything else here stands, including
> the correction to #726 and the reason the defect was invisible.

Live on IDP1 (`v0.6.7.dev3`, us-west-2), classification on **Nova 2 Lite** (the
shipped default), `multimodalPageLevelClassification`, `contextPagesCount: 0`,
`sectionSplitting: llm_determined`, single-class `bank_statement`. 5 repeats per
cell/doc; the failure is non-deterministic, so a single run settles nothing.

`sections_correct` is scored 1.0/0.0, so the mean **is** the pass rate.

## The result

| document | pagination | expected | PRE-FIX prompt | WITH the fix | effect |
|---|---|---|---|---|---|
| `paginated_3pg` | yes | 1 | **100%** `[1,1,1,1,1]` | 100% `[1,1,1,1,1]` | **none** |
| `small_narrow` | **no** | 1 | **0%** `[2,3,2,3,2]` | **60%** `[1,2,1,1,2]` | **improved** |
| `twodocs_2x20` | yes (per copy) | 2 | **100%** `[2,2,2,2,2]` | 100% `[2,2,2,2,2]` | **none** |

`completeness_recall` is **1.0 in every arm** — no rows are lost either way. This
failure never shows up as missing data; only the section count changes.

The pre-fix arm is the identical branch with `classification.task_prompt` swapped
back to develop's text. Model, splitting strategy, corpus and OCR are the same, so
the prompt is the only variable.

## What the fix actually does

**One thing, and it is real:** an unpaginated multi-page document goes from
*never* being kept whole (0/5, sometimes fragmenting into 3 sections) to being kept
whole 3 times in 5.

**Two things it does not do**, both of which earlier uncontrolled readings had
credited to it:

- **Paginated documents were never broken.** `Page 2 of 3` is decisive on its own,
  and the pre-fix prompt already scored 5/5. An earlier run reported the fix at
  100% here and that number is meaningless without this control.
- **The over-merge direction was not broken on this corpus.** Two back-to-back
  statements, each with its own identity block, already scored 5/5 pre-fix.
  #653 measured 1/10 on that shape using a **4-page file of two 2-page documents**;
  `twodocs_2x20` is two 1-page documents, which is an easier shape. The 1/10 figure
  is not reproduced here because this is not the same test.

## Correcting #726

#726 reported "82% over-split" as a property of `llm_determined`. The controlled
data scopes that in both directions:

- On an **unpaginated** multi-page document it is worse — 0/5 correct, i.e. it
  over-splits *every* time, sometimes into more sections than there are documents.
- On a **paginated** document it does not happen at all.

So the defect is specifically **unpaginated multi-page documents**, not
`llm_determined` in general. #726's workaround advice was also wrong and is
corrected separately: `sectionSplitting: disabled` merges *all* pages, which breaks
multi-form packets — visible in the control column of the main run, where
`split-disabled` scores 0% on `twodocs_2x20` by construction.

## Verdict

The stated gate was `small_narrow` → 1 section. **At 60% it is not met.** The fix
is *partial*.

It is still worth landing: it is a **strict improvement** — 0% → 60% on the only
broken case, unchanged on the two cases that were already correct, no rows lost, and
the rules sit inside the prompt-cache prefix so they add no per-page cost. Not
landing it means shipping 0% on unpaginated documents. The residual 40% belongs on
#653 as new information, not as a reason to withhold an improvement.

**Known limitation to document:** an unpaginated multi-page document is still split
about 40% of the time. `contextPagesCount: 1` is the lever for it, at the cost of
biasing toward over-merging — which is why it is not a default.

## Reproducing

```bash
python3 benchmarks/harness/gen_corpus.py --only small_narrow,paginated_3pg,twodocs_2x20
python3 benchmarks/harness/make_configs.py --suite boundary --class bank_statement
AWS_PROFILE=default python3 benchmarks/harness/run_matrix.py --stack IDP1 --suite boundary --class bank_statement
# the pre-fix control (config written by hand; the prompt is not an axis):
AWS_PROFILE=default python3 benchmarks/harness/run_matrix.py --stack IDP1 --suite boundaryctl --class bank_statement
```

Not measured here: Sonnet 5 (#653's reported model, and the stricter test since it
rejects `temperature` and therefore samples); documents with pages scanned out of
order, which #653 notes the rules cannot fix on page evidence alone. Sonnet 5 was
subsequently measured — see below.

## Superseded by !769

GitLab !769 (Mofijul Islam, who designed the prompt this run measures) ran the same
rules on **DocSplit-Poly-Seq**: 500 packets, 7,330 pages, 2,027 sections, 5,000
packet-runs, 0 failures, across five models. That corpus is three orders of
magnitude larger than the three documents here, so where the two disagree, !769 is
the better evidence.

Split accuracy on multi-section packets, fix vs. pre-fix prompt:

| model | Δ split accuracy | |
|---|---|---|
| Qwen3-VL | **+0.117** | p<0.05 |
| Opus 5 | **+0.040** | p<0.05 |
| Nova 2 Lite | **+0.030** | p<0.05 |
| Sonnet 5 | **+0.013** | p<0.05 |
| gpt-5.6-sol | +0.004 | not significant |

Paired bootstrap + Wilcoxon. **No model regresses.** Under-split rate is 0.000 in
all ten cells, so the anti-over-merge clause holds at scale. Page-level *class*
accuracy moves at most 0.015, confirming the change touches boundaries only.

What this overturns:

- **"Paginated documents were never broken" is too strong.** It is true of
  `paginated_3pg` — a clean, synthetic, single-class 3-page statement where
  `Page 2 of 3` is unambiguous. It is not true of paginated documents in general:
  four of five models improve significantly on a corpus that contains them.
- **The over-merge case reproduces after all.** This run could not reproduce #653's
  1/10 because `twodocs_2x20` is two 1-page documents rather than #653's 4-page file
  of two 2-page documents. !769 ran that shape: **1/10 → 5/5**, and
  `contextPagesCount: 1` alone scores **0/5** on it by merging all four pages —
  which independently confirms why raising `contextPagesCount` is not the fix.
- **Sonnet 5 is measured.** #653's reporter's 2-page form: **6/24 → 10/10**. This
  also confirms the `temperature`-stripping diagnosis: Sonnet 5, Opus 5 and Claude
  4.7+ reject `temperature`/`top_p`/`top_k` so `idp_common` strips them and those
  models sample, while Sonnet 4.5 honours `temperature: 0` and so resolved the
  ambiguity identically every run. That is why #653 looked model-specific and is not.

What it does **not** overturn: the `small_narrow` 0% → 60% result, the fact that
`completeness_recall` stays 1.0 so the defect is invisible in the data, the
correction to #726, or the documented residual — an unpaginated multi-page document
is still split some of the time.

Two limits !769 adds:

- **The single-document figures do not generalise.** The rules lean on pagination
  markers, which RVL-CDIP scans mostly lack.
- **A separate, larger defect:** `llm_determined` over-splits **1.5×–2.3×** on real
  packets *regardless of prompt*. Prompt work does not address it; it needs its own
  issue.

Caveat on reproducibility: Qwen3-VL and `gpt-5.6-sol` are not selectable models in
this repo, so the five-model table cannot be re-run from this checkout. The three
Bedrock rows (Opus 5, Sonnet 5, Nova 2 Lite) can.
