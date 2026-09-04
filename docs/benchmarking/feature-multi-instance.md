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
(`benchmarks/matrices/config_matrix.yaml`), plus four **Test Studio** runs over the
`OmniAI-OCR-Benchmark` and `RealKIE-FCC-Verified` test sets for §2.
**Scored data:** `benchmarks/results/v0.6.7/{multiinstance,midetect,midetectlong}/`.
**Pricing:** `config_library/pricing.yaml`, rates as of 2026-09.

> Every number here comes from a live run: §1 via `benchmarks/harness/aggregate.py`,
> §2 from the Test Studio runs' own evaluation reports and metering. None are
> recalled from memory.

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

## 2. Detection, measured on real labeled corpora

The synthetic grid could not answer this properly: three documents with a
**two-field** accuracy denominator, where one field flip moves a cell mean by
0.033. So the question was re-asked with **Test Studio** — the product's own test
execution — over two real labeled corpora, two configuration profiles that differ
in nothing but `extraction.multi_instance_detection.enabled`, and `numberOfFiles`
taking the same deterministic first N. **80 paired runs**, scored against each test
set's committed baselines. Paired on document, because document difficulty
dominates variance on a real corpus.

### 2a. Does it find real multi-record documents? Yes — perfectly.

`OmniAI-OCR-Benchmark`, first 40 documents (all `BANK_CHECK` — scanned check
images, some holding several checks on one sheet). The class's baseline is a
`checks` array, so ground truth states exactly how many checks each image contains.

| | |
|---|---|
| true positives | **18** |
| false positives | **0** |
| false negatives | **0** |
| correct silences | **22** |
| precision / recall | **1.000 / 1.000** |
| count reported **exactly** right | **18 of 18** (counts of 2, 3, 4, 5, 6, 7 and 8) |

It flagged every multi-check image, stayed silent on every single-check image, and
got the number right every time.

**But it did not avert any data loss here, and an earlier draft of this document
claimed it did.** That claim — "without it each of those 18 documents silently ships
1 to 7 checks fewer than it contains" — was **false**. Measured directly from both
arms' `inference_result` afterwards:

| | detection ON | detection OFF |
|---|---|---|
| checks missing vs ground truth, summed over the 18 flagged documents | **0** | **0** |

Every check was extracted in both arms. The reason is in the class schema:
`BANK_CHECK` has exactly **one** property, an array called `checks`, so the class
*already* models several checks per sheet. Nothing was ever collapsing to one
record on this corpus, so there was no loss for detection to prevent. The 0.938 /
0.946 weighted accuracy should have been the tell — 18 of 40 documents shedding most
of their content cannot coexist with a 0.94 score.

So what are the 18 flags, if not averted loss? They are **correct and useful, but
about configuration rather than about data**. Detection counts *documents*; it
compares that against `Section.instance_count`, which is 1 because `BANK_CHECK`
declares no instance axis. "The model says 6 documents, the section reports 1
instance" is true as stated, and the right response is to set
`x-aws-idp-instance-array: checks` on the class — free, no schema change, no
baseline migration — after which the count is reported properly and the warning
stops. Detection found a real misconfiguration in a shipped preset. It did not find
lost records, because there were none.

This matters for the recommendation, not just the bookkeeping: on a corpus like this
one the honest use of detection is **diagnostic and temporary** — turn it on, act on
what it names, turn it off. It is a permanent setting only where the classes really
do collapse several records into one.

> **Two near-misses on the same 18 documents, in opposite directions.** First
> reading: "18 false positives — the probe misfires badly on bank checks." Wrong,
> and one query from being the headline; the ground truth showed the baseline's
> `checks` array had 2–8 entries on exactly those documents. Second reading, having
> over-corrected: "18 real catches, each averting 1–7 lost checks." Also wrong, and
> it survived into a merged PR, a closed issue and this document before anyone
> counted the extracted rows. The same discipline fixes both: a plausible story
> about the mechanism is not a measurement of it. The count above is a measurement.

### 2b. What does it cost?

| metric | detection off | detection on | delta |
|---|---|---|---|
| **OmniAI-OCR-Benchmark** (40 paired docs) | | | |
| weighted accuracy | 0.9380 | 0.9461 | **+0.0081** |
| better / worse / identical | — | 7 / 6 / 27 | sign test **p = 1.000** |
| input tokens | 7,759 | 7,900 | **+1.82 %** |
| output tokens | 1,580 | 1,571 | **−0.53 %** |
| **RealKIE-FCC-Verified** (40 paired docs) | | | |
| weighted accuracy | 0.7678 | 0.7552 | **−0.0126** |
| better / worse / identical | — | 1 / **14** / 25 | sign test **p = 0.0010** |
| input tokens | 95,020 | 94,262 | **−0.80 %** |
| output tokens | 6,833 | 6,609 | **−3.28 %** |

- **Tokens and cost are a non-issue** on both corpora: ±2 %, in both directions.
  The probe is one extra output integer.
- **On the OCR benchmark there is no accuracy effect at all** — p = 1.000, and the
  point estimate slightly favours detection *on*.
- **On RealKIE there is a real one.** Worse on 14 of 40 documents and better on 1
  is significant at p = 0.001; it is not noise. The loss is **diffuse** —
  `AgencyCommission` −2 documents, `PaymentTerms` −2, `Agency` −1, `LineItems` −1 —
  so it reads as a small general perturbation from adding a question to the
  request, not a specific failure mode. RealKIE is a single-class forms corpus with
  **no** multi-record documents, so on it the feature is pure cost with zero
  benefit, which is exactly the shape of deployment a default has to protect.

### 2c. Why the earlier synthetic result was not good enough

The synthetic grid reported "scalar accuracy consistently worse, 5/10 vs 2/10 on
one document, not significant" and **zero** false positives. Both readings were too
weak to act on, and one was misleading:

- the FP result came from three synthetic bank-statement documents, a corpus with
  no multi-record instances in it at all — it could not have found a false
  positive, so "0" carried no information;
- the accuracy signal came from a two-field metric on one unstable document.

The real corpora answered both in one pass, and answered them differently: FP is 0
because detection is *accurate*, not because the test was blind, and the accuracy
cost is real but corpus-dependent.

## 3. So detection ships OFF by default — and the guidance is now specific

Not "gated on evidence we could not resolve". The evidence resolved:

> **Turn it on when a section can hold several documents of the same class *and the
> class schema describes only one*.** That is the combination that loses records. It
> will find them, count them correctly, and cost about 2 % more input tokens.
> **Leave it off when either half is untrue** — on a single-record corpus it buys
> nothing and costs about a point of accuracy, and on a corpus whose classes already
> declare a record array (like `BANK_CHECK`) it reports a count discrepancy that is
> real but is a *configuration* finding, not data loss.

The second half of that condition is the correction §2a records: an earlier draft
said only "when a section can hold several documents", which reads as a
recommendation to leave it on for `ocr-benchmark` — where it would flag 18 documents
a month, indefinitely, none of which is losing anything.

**So there are two distinct uses, and only one of them is a setting:**

| use | how long | what you do with it |
|---|---|---|
| **Diagnostic** — "do any of my classes silently collapse records?" | run once, then off | act on what it names: set `x-aws-idp-instance-array` on a class that already lists records, or `x-aws-idp-multi-instance` on one that does not |
| **Standing guard** — a corpus that genuinely keeps producing merged same-class sections | leave on | accept ~2 % input tokens for a warning on every affected section |

It is per configuration profile, so a multi-record corpus can have it while the
rest of a deployment does not. With it off the extraction prompt and the forced
toolSpec are byte-identical to earlier releases.

## Honesty notes

- **The headline claim of §2a was wrong in the first published version of this
  document, and it took a fourth look to catch.** "Each of those 18 documents
  silently ships 1 to 7 checks fewer than it contains" was asserted from the
  *detection* numbers alone — probe count vs `instance_count` — without ever
  counting the extracted rows. Counting them gives 0 missing in both arms. The
  error is instructive: `instance_count` is 1 for a class with no declared
  instance axis *whether or not anything was lost*, so "probe 6 vs instance 1"
  cannot distinguish "5 records dropped" from "5 records present inside a declared
  array". Nothing in the detection instrument can tell those apart — that is what
  the extracted-row count is for, and it was one query away the whole time.
  It survived into a merged PR and a closed issue before being checked.
- **The synthetic grid nearly produced the wrong conclusion twice.** First it
  reported 0 false positives from a corpus that contained no multi-record
  documents — a number with no information in it that read like a clean bill of
  health. Then the real run's 18 warnings were nearly reported as false alarms
  before the ground truth was checked. Both would have been confidently wrong.
  The lesson is the same one both times: an instrument that cannot see the
  phenomenon will still return a number.
- **§2 used Test Studio rather than the benchmark harness**, because the harness
  cannot run reference corpora: `run_matrix.py` launches one local PDF per run and
  a reference corpus is a test set on the stack, while `analyze.score_reference`
  sits fully implemented. So a suite naming `realkie` or `ocr_bench` — including
  `core_docs` — runs nothing for it. Not fixed here (out of scope for this
  feature); filed as GitHub #766, which has since made the shortfall **visible**
  (the launcher names it and records it in the runmap) without adding a launch
  path — so this remains the way to get numbers on a real corpus.
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

## Reproduce the Test Studio A/B (§2)

Two profiles per corpus differing only in the toggle, then the TestRunner Lambda —
the same entry point the Test Studio UI uses. `numberOfFiles` takes the first N
deterministically, so both arms see identical documents.

```bash
# one config profile per arm, derived from each corpus's own shipped config and
# differing ONLY in extraction.multi_instance_detection.enabled
idp-cli config-upload --stack-name <STACK> --config-file mid-off-ocr.yaml \
    --config-profile mid-off-ocr --version-description "detection off"

python3 benchmarks/harness/detection_ab_teststudio.py --stack <STACK> launch --n 40 \
    --pair ocr-benchmark:mid-off-ocr:mid-on-ocr \
    --pair realkie-fcc-verified:mid-off-rk:mid-on-rk

python3 benchmarks/harness/detection_ab_teststudio.py --stack <STACK> analyse
```

`idp-cli test-result` / `test-compare` give the same runs' reports interactively.

Accuracy per document is `overall_metrics.weighted_overall_score` from each
document's own `evaluation/results.json`; the detection verdict is the presence of
`extraction_multi_instance_suspected` on the section, checked against the
baseline's record count.

## Reproduce the synthetic suites (§1)

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
