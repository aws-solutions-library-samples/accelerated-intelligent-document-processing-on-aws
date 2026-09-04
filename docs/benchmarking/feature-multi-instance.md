---
title: "Feature Study: multi-instance sections (#715 / #753)"
---

# Feature study — multi-instance sections

Not a release-vs-release comparison. This is a **feature A/B on one deployed stack
with identical code**, where the only thing that changes between arms is a config
toggle — which is what makes a delta attributable to the feature rather than to
anything else in a build. It answers three questions:

1. Does `x-aws-idp-multi-instance` actually recover the records a merged section
   loses? (#715)
2. What does the #753 detection probe cost on **ordinary** documents, and does it
   raise false alarms on them?
3. Should detection ship on by default?

**Stack:** `IDPMulti`, us-west-2, `v0.6.7.dev9` built from
`feature/multi-instance-sections`.
**Suites:** `multiinstance`, `midetect`, `midetectlong`
(`benchmarks/matrices/config_matrix.yaml`).
**Scored data:** `benchmarks/results/v0.6.7/{multiinstance,midetect,midetectlong}/`.
**Pricing:** `config_library/pricing.yaml`, rates as of 2026-09.

> Every number here comes from `benchmarks/harness/aggregate.py` over live runs.

---

## 1. Does the transform recover the lost records? Yes.

`twodocs_2x20` is two complete bank statements in one file — distinct account
numbers, 20 transactions each, globally unique `SEQ` tags so completeness is
exact. All three cells force **one section** (`sectionSplitting: disabled`), which
is the shape the feature exists for. `repeats: 3`.

| cell | wrapper | detection | sections | rows extracted | recall | scalar acc |
|---|---|---|---|---|---|---|
| `mi-silent` | off | off | 1 | 40 | 1.00 | 1.00 |
| `mi-detected` | off | **on** | 1 | **20** | **0.50** | 1.00 |
| `mi-wrapped` | **on** | on | 1 | 40 | **1.00** | **1.00** |

`mi-wrapped` is the result the feature is for: one section, both statements,
**every** row, exact scalars — all three runs.

**`mi-detected`'s 0.50 recall is not data loss caused by detection**, and it is the
most interesting number here. With detection on and no wrapper, the model returned
the **first statement only** — 20 of 40 rows — and the section was flagged
`extraction_multi_instance_suspected`. `mi-silent` reached 40 rows by merging *two
accounts' transactions into one statement's list*: higher recall, semantically
wrong data, no warning. So a row-count metric prefers the arm that produces the
wrong answer quietly. That is exactly the failure #753 exists to make visible, and
it is also why the metric alone cannot decide the default.

## 2. What does detection cost on ordinary documents?

`detect-off` vs `detect-on`, identical but for
`extraction.multi_instance_detection.enabled`, on genuinely **single-document**
docs (`tiny_form`, `small_narrow`, `longdesc_100`). `repeats: 5` → 15 runs/arm.

| metric | detect-off | detect-on | delta |
|---|---|---|---|
| runs completed | 15/15 | 15/15 | — |
| rows extracted | exact, every run | exact, every run | **0** |
| scalar accuracy | 1.000 (σ 0) | 0.967 (σ 0.129) | **−0.033** |
| cost / doc | $0.11634 | $0.11591 | **−0.4 %** |
| **false positives** | 0 | **0** | — |

- **Completeness is untouched.** 30/30 runs extracted the exact expected row count.
  No truncation, no gaps, no duplicates, no failures.
- **Cost is flat.** −0.4 %, inside noise. The probe is one extra output integer.
- **False-positive rate is 0**, which is #753's own acceptance criterion: no
  `extraction_multi_instance_suspected` on any genuinely single-document section, in
  any run of any arm.
- **Scalar accuracy is consistently, unexplainedly worse with it on.**

### The accuracy signal, chased down

One document (`longdesc_100`) accounted for every deviation, so it was run 10×
per arm on its own (`midetectlong`):

| arm | runs at full scalar accuracy | mean |
|---|---|---|
| `detect-off` | 8 / 10 | 0.90 |
| `detect-on` | **5 / 10** | **0.75** |

The document is unstable on one of its two scalar fields even with detection off
(2/10 failures), but it fails **more than twice as often** with detection on.
Pooled over all three batches (repeats 3, 5, 10): `detect-off` 16/18 vs
`detect-on` 11/18.

**This is not statistically significant** — Fisher's exact on the n=10 pair gives
p ≈ 0.35, pooled p ≈ 0.12 — and it may well be an artifact of the metric:
`scalar_accuracy` on this corpus has a **two-field denominator**, so a single field
flip moves a cell mean by 0.033 mechanically. But the direction was the same in
all three independent batches and never once favoured detection.

## 3. So detection ships OFF by default

#753 set the gate itself: *"must be A/B'd: adding a meta field to the response can
perturb extraction quality"* and *"false-positive rate measured on the benchmark
corpus, not assumed."* The FP criterion passed. The perturbation criterion did not
clear: a consistent, unexplained accuracy movement in the wrong direction is not a
result you ship to the extraction request of **every existing Simple-mode
section** on the strength of a diagnostic.

With `enabled: false` the extraction prompt and the forced toolSpec are
**byte-identical to earlier releases**, which is the strongest no-regression
guarantee available.

**Turn it on** for any corpus where one section can hold several documents of the
same class. There the alternative is shipping one record out of three with no
signal at all, and the trade is obviously worth it. It is per-configuration-profile,
so a multi-record corpus can have the warning while the rest of a deployment is
untouched.

## Honesty notes

- **A harness bug was found and fixed mid-study, and it mattered.**
  `analyze.py` collected scalar fields from the **top level** of
  `inference_result`, so a wrapped result — whose only top-level key is
  `instances` — could never match, and `mi-wrapped` first reported
  `scalar_accuracy = 0.0` on all six runs *while `rows_extracted` showed the data
  was complete*. That was the scorer, not the pipeline. `scalar_bearing_records`
  now unwraps; the numbers above are from the re-scored run (same inference, no
  re-run). A metric that cannot see a feature's output shape reports the feature as
  a total failure — worth remembering before trusting any single benchmark cell.
- **Cost figures in the `multiinstance` suite are unreliable.** Some runs were
  scored while still `ASSESSING`, so their metering was incomplete. The cost
  comparison in §2 comes from `midetect`, where every run was `COMPLETED`.
- **Section counts on `small_narrow` are noisy in both arms** (`detect-off`
  [2,1,1], `detect-on` [2,3,3]) — `llm_determined` boundary detection is
  non-deterministic on a document with no pagination cues, and the probe is in the
  *extraction* request, so it cannot influence classification. Not attributable to
  this change.
- n is small throughout (3–10 per cell). Every claim above is stated at the
  strength the sample supports, and the one that could not be resolved is the one
  that decided the default.

## Reproduce

```bash
python3 benchmarks/harness/gen_corpus.py
python3 benchmarks/harness/make_configs.py --suite multiinstance --class bank_statement
AWS_PROFILE=default python3 benchmarks/harness/run_matrix.py --stack <STACK> --suite multiinstance --max-inflight 6
AWS_PROFILE=default python3 benchmarks/harness/aggregate.py --run benchmarks/results/run-<stamp> \
    --out benchmarks/results/<release>/multiinstance
# and the detection A/B
python3 benchmarks/harness/make_configs.py --suite midetect --class bank_statement
AWS_PROFILE=default python3 benchmarks/harness/run_matrix.py --stack <STACK> --suite midetect --repeats 5
```
