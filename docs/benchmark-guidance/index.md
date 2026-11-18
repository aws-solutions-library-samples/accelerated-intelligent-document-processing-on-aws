---
title: "How We Benchmark Configurations"
tableOfContents:
  maxHeadingLevel: 3
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# How We Benchmark Configurations

- When is agentic extraction worth it?
- Does holistic classification beat page-by-page classification?
- Does OCR make a difference?
- Which extraction model should I use?
- Can I route review by confidence?
- What is the cheapest configuration that keeps accuracy?

We answer these questions with benchmark datasets, one page per problem set:
[Digitization](digitization.md), [Splitting and Section Classification](splitting-and-classification.md),
[Key Information Extraction](extraction.md), [Confidence Assessment](confidence-assessment.md)
and [Token Use and Cost](token-use.md).

## What we recommend

These are good places to start. Test each one on your own documents.

| Decision | Start with | Why |
|---|---|---|
| Splitting packets into documents | Holistic classification with Claude Haiku 5.5, reading text only | [0.88 of sections exactly right for $83 per 1,000 packets](splitting-and-classification.md#do-i-need-page-images-in-the-classification-prompt) ([SC-11](splitting-and-classification.md#does-holistic-classification-beat-page-by-page-classification)) |
| Extraction model | Claude Haiku 5.5 | [No model scored measurably higher, and it cost the least](extraction.md#which-extraction-model-should-i-use) |
| Single-pass or agentic extraction | Single-pass; agentic with Claude Haiku 5.5 above about 100 line items, with table parsing off | [Above 100 line items single-pass got almost none right](extraction.md#when-is-agentic-extraction-worth-it); [table parsing cost more and scored no better](extraction.md#does-deterministic-table-parsing-help-agentic-extraction) |
| What the extraction model reads | Page images and OCR text | [Without page images, W-2 forms with every field right fell from 78% to 3%](extraction.md#what-is-the-impact-of-document-modality) |
| Reasoning effort (Claude Sonnet 5.5) | Low | [Medium and high scored the same](extraction.md#does-more-reasoning-effort-improve-extraction) |
| Scoring model, if people review by confidence | Claude Haiku 5.5, with escalation off | [Caught as many errors as Claude Sonnet 5.5 for a fifth of the cost](confidence-assessment.md#which-scoring-model-should-i-use) |
| Confidence, if nobody reviews by it | Off | [Same accuracy, 13% to 28% cheaper](confidence-assessment.md#separate-scoring-pass-or-integrated-into-extraction) |
| Decision model ([Strands Decider 2B](https://huggingface.co/StrandsAgents/strands-decider-2B-qwen3.5-v1-2610), Qwen3.5, on AWS Lambda) | Not yet as the default. If you try it for section classification, give each class a one-line description of what its pages look like | [With one-line descriptions it classified 0.90 of sections right, against 0.77 with the preset's descriptions and 0.97 for Claude Haiku 5.5](splitting-and-classification.md#can-a-decision-model-classify-the-sections) (interim); [as a confidence scorer it separated right from wrong values less well than Claude Haiku 5.5, for more](confidence-assessment.md#can-a-decision-model-score-confidence) |
| Schema enforcement | Structured output once it is released ([#1785](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1785)), or off; not the forced tool | [The forced tool lowered W-2 accuracy](extraction.md#does-schema-enforcement-improve-accuracy-or-cost) |
| OCR | Amazon Textract, Layout only, at 300 DPI | [Scored best on W-2s and as well on invoices, for the lowest cost](digitization.md#which-textract-features-do-i-need) ([DG-01](digitization.md#does-the-ocr-reader-make-a-difference-to-extraction-accuracy), [DG-04](digitization.md#what-resolution-should-pages-be-rendered-at)) |
| Lowest cost | Claude Haiku 5.5 extraction, Textract Layout only, confidence off or scored by Claude Haiku 5.5 | [$21 against $135 per 1,000 invoices, at the same accuracy](token-use.md#what-is-the-cheapest-configuration-that-keeps-accuracy) |

The recommended setup as configuration overrides, on top of the preset closest to your documents:

```yaml
ocr:
  backend: textract
  features: [{name: LAYOUT}]
classification:
  strategy: combined
  combined:
    granularity: packet          # holistic
    llm:
      model: us.anthropic.claude-haiku-5-5
      modality: {text: true, images: false}
extraction:
  model: us.anthropic.claude-haiku-5-5
  mode: simple                   # single-pass; use advanced (agentic) above ~100 line items
  schema_enforcement: {mode: structured_output}   # not in a release yet; leave out until it is
  confidence:                    # or mode: off if nobody reviews by confidence
    mode: separate
    model: us.anthropic.claude-haiku-5-5
    escalation_enabled: false
```

## How we run a benchmark

Most benchmarks start from a *baseline setup*, change one setting, and run the same
documents with each value of that setting. The baseline is the recommended setup above
with one difference: its classification reads page images as well as text, because
text only came out ahead after the baseline was set (SC-02). A few, such as
`EX-01` and `TU-04`, start from each dataset's own configuration preset instead, and say
so. Each benchmark has an ID and a results table; one we haven't run yet is listed under
*Planned* on its page.

[Benchmark Datasets](datasets.md) lists each dataset with its preset and its Test Studio
test set.

Defaults differ from preset to preset. To configure your own stack, start from the
[preset](../../config_library/unified/README.md) closest to your documents.

## Why you can trust the results

- **Compare within a question.** Each question runs its own documents at its own time, so
  the same setup can differ by a few dollars per 1,000, or a point of accuracy, from one
  question to the next.
- **Known answers.** Every dataset has ground truth, generated exactly for synthetic
  documents or checked by people for public ones. See
  [Benchmark Datasets](../idp-fundamentals/benchmark-datasets.md).
- **One change at a time,** on one stack, so the setting explains the difference.
- **Same documents, repeated.** We run every value on the same documents at least three
  times and report the spread. The spread is run to run: it shows how much a result moves
  when the same documents are processed again, not how much it would move on other
  documents.
- **Real differences only.** We name a winner only when the difference exceeds twice its
  standard error.
- **Pinned to a release.** Each table names the release, month and region, and the results
  file behind it.
- **Tokens, not prices.** Tokens stay the same when prices change. Multiply by current
  prices to estimate cost.
- **Repeatable in your account.** Most datasets come with every stack as Test Studio test
  sets.

## Terms

- **Weighted accuracy:** the share of fields right, weighted by each field's importance, scored with the open-source Stickler library against ground truth.
- **Line-item score:** the share of a list's rows extracted right: matched rows over the larger of the true and returned row counts.
- **Sections exactly right:** the share of documents in a packet split at the right pages and given the right class.
- **AUROC:** how well confidence separates right values from wrong ones; 50% is a coin flip, 100% is perfect.
- **ECARB (error capture at review budget):** if people review the 30% of values with the lowest confidence, how many times as many wrong values they find as a random 30% review would. 1× is no better than random; at 30% the most possible is about 3.3×. See Stickler's [Error Capture at Review Budget](https://awslabs.github.io/stickler/Advanced/confidence-metrics/#error-capture-at-review-budget).
- **Errors caught reviewing 30% of values:** the same review, as the share of wrong values found.
- **Cost per error prevented:** the extra cost per 1,000 documents divided by the errors it removes, against the cheapest option. See [Marginal Cost of Error](marginal-cost-of-error.md).
- **Baseline setup:** what most benchmarks change one setting from; see [How we run a benchmark](#how-we-run-a-benchmark).

## What the results don't cover

- **Your documents.** Confirm a choice on your own test set; see
  [Measure with ground truth](../idp-fundamentals/index.md#4-measure-with-ground-truth).
- **BDA mode.** These benchmarks cover Pipeline mode.
- **Datasets we haven't built yet:** packets with field ground truth, and handwritten
  forms.

## Questions for every problem set

<!-- BEGIN GENERATED FAQ -->

### Planned

We haven't run these yet.

- How much do results vary between identical runs? (TR-01)

<!-- END GENERATED FAQ -->
