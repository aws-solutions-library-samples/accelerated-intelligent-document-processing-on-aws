---
title: "How We Benchmark Configurations"
tableOfContents:
  maxHeadingLevel: 3
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# How We Benchmark Configurations

A benchmark measures what one configuration choice does to accuracy, cost and time, on
documents whose correct answers are known. We run every benchmark in
[Test Studio](../test-studio.md), the same tool you can run on your own documents, and
publish the results by problem set: [Digitization](digitization.md),
[Splitting and Section Classification](splitting-and-classification.md),
[Key Information Extraction](extraction.md), [Confidence Assessment](confidence-assessment.md)
and [Token Use and Cost](token-use.md).

## Benchmark process

1. **A test set with ground truth.** A test set is a group of documents with their correct
   answers: every field's value and, for packets, every section's pages and document class.
   Every stack loads several public test sets
   ([Pre-Deployed Test Sets](../test-studio.md#pre-deployed-test-sets)), and
   [Benchmark Datasets](datasets.md) lists the ones these pages use. To build your own, see
   [Creating Test Sets](../test-studio.md#creating-test-sets) and
   [Draft labeling](../test-studio.md#draft-labeling-unlabeled-documents--ground-truth).
2. **One change from a baseline.** Each benchmark starts from the [baseline setup](#baseline-setup)
   and changes one setting, so the setting accounts for the difference. Each value of the
   setting is its own configuration
   ([Configuration Profiles and Revisions](../test-studio.md#configuration-profiles-and-revisions)).
3. **Repeated runs.** We [run the test set](../test-studio.md#running-tests) three times for
   each value, on one stack and one release. Each table states its runs and documents.
4. **Scoring.** Test Studio scores each document against its ground truth with the
   open-source [Stickler](https://github.com/awslabs/stickler) library, field by field, with
   each field's comparison method. See
   [Bulk Aggregation with Stickler](../test-studio.md#bulk-aggregation-with-stickler),
   [Field-Level Metrics](../test-studio.md#field-level-metrics),
   [Graded Packet Metrics](../test-studio.md#graded-packet-metrics-run-level) for splitting
   and [Confidence Calibration Metrics](../test-studio.md#confidence-calibration-metrics) for
   confidence.
5. **Cost and time.** Cost comes from each document's metered tokens and requests, priced at
   on-demand us-east-1 rates from [config_library/pricing.yaml](../../config_library/pricing.yaml).
   Time is the median per document.
6. **Comparison.** Each table reports the mean over the runs with two standard errors. A
   difference counts as measurable only when it exceeds twice the combined standard error.
   Each result names its release, month and region, and the results file behind it.

## Baseline setup

Most benchmarks start from the same configuration: Amazon Textract with Layout only;
holistic classification reading the page text and images; single-pass extraction; and
confidence scored in a separate pass, all with Claude Haiku 5.5 and no escalation.
Benchmarks run on a pre-release build with structured output also use it for extraction. A
few
benchmarks, such as `EX-01` and `TU-04`, start from each dataset's own
[preset](../../config_library/unified/README.md) instead, and say so.

## Supported conclusions

A benchmark shows:

- how one setting changes accuracy, cost and time on its dataset, with every other setting
  held fixed;
- whether that change is larger than the variation between repeated runs;
- what an accuracy gain costs, as the cost per error prevented
  ([Marginal Cost of Error](marginal-cost-of-error.md));
- a configuration to start your own testing from.

## Limits

- **Your documents.** A result holds for its dataset. Confirm any choice on a test set of
  your own documents; see
  [Measure with ground truth](../idp-fundamentals/index.md#4-measure-with-ground-truth).
- **Other documents of the same kind.** Most standard errors measure how much a result moves
  when the same documents run again, not how much it would move on different documents.
- **Comparisons across benchmarks.** Each benchmark runs its own documents at its own time,
  so the same configuration can differ by a few dollars per 1,000 documents, or a point of
  accuracy, between two benchmarks. Compare values within one benchmark.
- **Combined changes.** Changing one setting at a time does not show how two changes
  interact.
- **The whole process.** No dataset we use covers every problem set at once. We measure
  splitting and extraction separately, not how a split error changes the extracted values.
- **Every cost.** Some costs are floors. Claude Haiku 5.5 bills a prompt over 100,000 tokens
  at a higher long-context rate, which we can't price for agentic extraction's calls, and a
  retried AWS Lambda attempt is billed without being metered.
- **Coverage.** The benchmarks cover Pipeline mode, not BDA mode. Most extraction results
  come from [RealKIE-FCC-Verified](../idp-fundamentals/benchmark-datasets.md#realkie-fcc-verified)
  invoices, real documents with checked ground truth but one kind of document. Handwritten
  forms and packets with field ground truth are not covered yet.
- **Later models and releases.** A result holds for the release and models it names.

## Terms

- **Weighted accuracy:** the share of fields right, weighted by each field's importance,
  scored with Stickler against ground truth. See Stickler's
  [Weighted Overall Score](https://awslabs.github.io/stickler/Guides/Evaluation/bulk-evaluation/#weighted-overall-score).
- **Line-item score:** the share of a list's rows extracted right: matched rows over the
  larger of the true and returned row counts. See Stickler's
  [List Score](https://awslabs.github.io/stickler/Advanced/hungarian-matching/#step-4-list-score).
- **Sections exactly right:** the share of documents in a packet split at the right pages
  and given the right class. See Stickler's
  [Classical Metrics](https://awslabs.github.io/stickler/Guides/Document_Packet_Splitting/#classical-metrics).
- **Grouping score:** a packet score from 0 to 1 for how pages are grouped and ordered, so a
  nearly right section still earns credit. See Stickler's
  [Packet Score](https://awslabs.github.io/stickler/Guides/Document_Packet_Splitting/#proposed-metrics-packet-score).
- **AUROC:** how well confidence separates right values from wrong ones; 50% is a coin flip,
  100% is perfect. See Stickler's [AUROC Calculation](https://awslabs.github.io/stickler/Advanced/confidence-metrics/#auroc-calculation).
- **ECARB (error capture at review budget):** if people review the 30% of values with the
  lowest confidence, how many times as many wrong values they find as a random 30% review
  would. 1× is no better than random; at 30% the most possible is about 3.3×. See Stickler's
  [Error Capture at Review Budget](https://awslabs.github.io/stickler/Advanced/confidence-metrics/#error-capture-at-review-budget).
- **Errors caught reviewing 30% of values:** the same review, as the share of wrong values
  found. See [Error Capture at Review Budget](https://awslabs.github.io/stickler/Advanced/confidence-metrics/#error-capture-at-review-budget).
- **Cost per error prevented:** the extra cost per 1,000 documents divided by the errors it
  removes, against the cheapest option. See [Marginal Cost of Error](marginal-cost-of-error.md).
- **Baseline setup:** the configuration most benchmarks change one setting from; see
  [Baseline setup](#baseline-setup).

## Benchmark status

<!-- BEGIN GENERATED FAQ -->

### Planned

We haven't run these yet.

- How much do results vary between identical runs? (TR-01)

<!-- END GENERATED FAQ -->
