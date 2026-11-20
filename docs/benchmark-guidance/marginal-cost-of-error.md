---
title: "Marginal Cost of Error"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Marginal Cost of Error

The marginal cost of error (MCoE) is what you pay for each error a more accurate
configuration prevents. It answers the question: how much extra are we paying to prevent
each error?

```
MCoE = (c₂ − c₁) / (ε₁ − ε₂)
```

Configuration 1 is the cheaper one and configuration 2 the more accurate one. c is the cost
per document, and ε is the error rate: 1 minus accuracy.

## A worked example: two models on 1,000 documents

Model B scores 90% accuracy at $0.10 per document. Model A scores 99% at $0.25.

| | Model B | Model A | Difference |
|---|---|---|---|
| Errors in 1,000 documents | 100 | 10 | 90 fewer |
| Cost of 1,000 documents | $100 | $250 | $150 more |

MCoE = $150 / 90 = $1.67 per error prevented.

If handling one error costs your business more than $1.67, Model A pays for itself. If it
costs less, keep Model B.

## What counts as an error

We score accuracy with Stickler, the open-source library the accelerator uses to compare
results with ground truth. Stickler scores each field and averages the scores by field
weight, giving each document a score from 0 to 1. The error rate is 1 minus that score.

One error is one document's worth of wrong field weight. 1,000 documents with a mean score
of 0.80 carry 200 errors.

Splitting and classification count sections instead. Section accuracy is the share of
sections with exactly the right pages and class, so one error is one wrong section. A
DocSplit packet holds about four sections, so 1,000 packets with a section accuracy of
0.80 carry about 800 errors, and the cost per error prevented is per section.

## How to read the lines on our charts

Each chart on our benchmark pages plots accuracy against cost per 1,000 documents, one point
per value of a setting. [Key Information Extraction](extraction.md) has examples. We compute
the costs from the on-demand Amazon Bedrock and Amazon Textract prices in
[config_library/pricing.yaml](../../config_library/pricing.yaml), for the one region and on
the date each chart shows.

Lines run from the cheapest point to every other point:

- **A line labeled with a dollar figure** leads to a point that scores higher. The figure is
  the MCoE of moving from the cheapest value to that one.
- **A line labeled "no measurable gain"** leads to a point that scores higher by less than two
  standard errors. The difference may be noise, so we don't price it.
- **No line** means the point costs more and scores no higher. Choose the cheaper value.

To compare two points when neither is the cheapest, apply the formula to their costs and
accuracies directly.

## Limits of MCoE

- **It needs a real accuracy gain.** A gain inside the noise makes the denominator tiny and
  the MCoE large and meaningless. That is why the charts say "no measurable gain" instead.
  See [Benchmark process](index.md#benchmark-process).
- **The costs are on-demand Bedrock and Textract prices only**, for the one region and on the
  date each chart shows, from
  [config_library/pricing.yaml](../../config_library/pricing.yaml). They leave out the other
  AWS services the stack runs on, such as AWS Lambda, AWS Step Functions and Amazon S3, and
  any discount you have.

## Is the upgrade worth it? Compare MCoE with what an error costs you

MCoE needs no business data, so we can report it in our benchmarks. To decide, you add one
number: b, what one error costs your business, such as the time to find and fix it, or the
loss if nobody does.

The Prevention Efficiency Ratio (PER) divides one by the other:

```
PER = MCoE / b = (c₂ − c₁) / (b · (ε₁ − ε₂))
```

PER is the dollars you spend per dollar of loss you prevent. Below 1, the upgrade saves more
than it costs. Above 1, it costs more than it saves.

In the example above, if an error costs you $8, PER = $1.67 / $8 = 0.21. You spend $0.21 to
prevent $1.00 of loss.

## When some fields cost more to get wrong: weighted PER

A wrong value doesn't cost the same in every field. In freight factoring, a wrong shipment ID
might cost $500 and a wrong driver note $5. Weighted PER (wPER) uses a cost for each field:

```
wPER = (c₂ − c₁) / Σᵢ bᵢ · (ε₁ᵢ − ε₂ᵢ)
```

bᵢ is the cost of an error in field i, and εⱼᵢ is configuration j's error rate on field i.
The denominator is the loss the upgrade prevents per document.

Suppose the upgrade costs $0.15 more per document:

| Field | Cost of an error (bᵢ) | Error rate, cheaper (ε₁ᵢ) | Error rate, upgrade (ε₂ᵢ) | Loss prevented per document |
|---|---|---|---|---|
| Shipment ID | $500 | 2% | 0.5% | $500 × 0.015 = $7.50 |
| Driver note | $5 | 10% | 4% | $5 × 0.06 = $0.30 |
| **Total** | | | | **$7.80** |

wPER = $0.15 / $7.80 = 0.019. You spend about 2 cents per dollar of loss prevented, and the
shipment ID accounts for almost all of the saving.

Stickler gives you the per-field error rates. It scores every field separately, with the
comparison method and threshold you set for that field in the document schema. Test Studio
reports each field's accuracy under [Field-Level Metrics](../test-studio.md#field-level-metrics);
a field's error rate is 1 minus its accuracy. bᵢ does the weighting, so use these per-field
rates rather than the weighted document score.

## Calculate it on your own documents

Our benchmarks give you MCoE. PER and wPER are yours to calculate once you know what an error
costs you.

1. Build a test set with ground truth from your own documents. See
   [How to measure extraction on your own documents](../idp-fundamentals/extraction.md#measuring-accuracy).
2. Run it in [Test Studio](../test-studio.md) with each configuration you are considering.
   Each run reports its cost and accuracy.
3. Calculate MCoE for each pair of runs, then PER or wPER with your cost of an error.
