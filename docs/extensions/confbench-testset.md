---
title: "Test Set - ConfBench"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Test Set - ConfBench

The **Test Set - ConfBench** extension is an installable [Feature Platform](../feature-platform.md)
extension that deploys the [amazon/ConfBench](https://huggingface.co/datasets/amazon/ConfBench)
benchmark into [Test Studio](../test-studio.md) **on demand**.

ConfBench takes the 75 verified FCC invoices from
[RealKIE-FCC-Verified](../test-studio.md#realkie-fcc-verified) and degrades each
one with up to 21 [Augraphy](https://github.com/sparkfish/augraphy) noise
pipelines, producing 1,346 (document, noise variant) pairs with identical ground
truth. That makes it purpose-built for:

- **Confidence calibration** — does reported confidence actually track accuracy
  as input quality degrades?
- **OCR robustness** — where does your OCR choice start losing text?
- **Key information extraction under noise** — which fields fail first, and how
  gracefully?

Because the ground-truth schema is byte-identical to `realkie-fcc-verified`,
accuracy on any degraded variant is **directly comparable** to the clean
baseline. That comparability is the whole point of the benchmark.

That comparability holds between runs on one configuration. The stack-managed
`realkie-fcc-verified` profile, which Test Studio preselects for the
RealKIE-FCC-Verified test set, sets `classification.sectionSplitting: disabled`,
one section per file. This extension's preset does not set that key, so its
configuration takes the host `default` profile's section splitting each time the
extension is installed or upgraded, which is `llm_determined` unless it has been
changed. To compare ConfBench with RealKIE-FCC-Verified, run both test sets on
one configuration version. Within ConfBench, Test Studio preselects this
extension's configuration for every tier, including the undegraded
`confbench-clean`.



https://github.com/user-attachments/assets/0b164e6d-c694-4127-8be9-45ec7fd41bee



## Why this is an extension, not a pre-deployed test set

The accelerator pre-deploys four benchmark datasets on every stack deployment.
ConfBench is deliberately not one of them:

| Dataset | Size |
|---|---|
| RealKIE-FCC-Verified | 0.08 GB |
| OmniAI-OCR-Benchmark | 0.39 GB |
| Fake-W2-Tax-Forms | 0.31 GB |
| DocSplit-Poly-Seq | (500 packets) |
| **ConfBench (this extension)** | **32.71 GB** |

At roughly **42x the combined size** of the other three measured sets, deploying
it unconditionally would charge every deployment for storage and transfer of a
specialist research dataset, and add a long transfer to first deploy. Shipping it
as an extension makes the cost opt-in twice — once by installing, once by
choosing how much to ingest.

## Installing

Install from **Extensions → Browse catalog** in the web UI, or with the feature
CLI:

```bash
idp-feature-cli deploy --from-code ./feature-platform/confbench-testset \
    --host-stack-name <your-stack-name>
```

**Installing downloads nothing.** It creates the ingest machinery, the feature
UI, and a configuration version — typically under a minute. The dataset moves
only when you start a job.

## Choosing what to ingest

Open **Test Set - ConfBench** in the Extensions nav. Pick a tier, or select
individual variants:

| Tier | Variants | Documents | Size | Test set id |
|---|---|---|---|---|
| Clean baseline | `original` | 75 | 0.02 GB | `confbench-clean` |
| Light noise | + 3 lightest pipelines | 253 | 0.31 GB | `confbench-light` |
| Representative spread | one per severity band (7) | 525 | 4.22 GB | `confbench-representative` |
| Full dataset | all 21 | 1,346 | 32.71 GB | `confbench` |
| Custom selection | your choice | — | shown live | `confbench-custom` |

**Representative spread** is the recommended default for calibration work: it
samples across the full severity range rather than taking the cheapest variants,
which is what you need to observe accuracy *decay* rather than just accuracy.

Every tier includes `original`, because degradation is only interpretable against
an undegraded control.

The picker shows exact per-variant sizes (measured from the published dataset,
not estimated), a running total, and an approximate monthly S3 storage cost
before you confirm. Sizes are wildly uneven — `original` is 0.02 GB while
`custom15` alone is 7.12 GB — so hand-picking is often much cheaper than a tier.

### Noise variants

| Variant | Documents | Size |
|---|---|---|
| `archetype9` | 28 | 15 MB |
| `original` | 75 | 22 MB |
| `custom23` | 24 | 105 MB |
| `archetype4` | 75 | 116 MB |
| `custom22` | 29 | 128 MB |
| `archetype10` | 75 | 155 MB |
| `archetype2` | 20 | 321 MB |
| `custom21` | 75 | 375 MB |
| `custom16` | 75 | 402 MB |
| `custom19` | 75 | 407 MB |
| `archetype7` | 75 | 500 MB |
| `custom18` | 75 | 647 MB |
| `custom13` | 75 | 978 MB |
| `custom14` | 75 | 1.07 GB |
| `default` | 46 | 1.17 GB |
| `custom12` | 75 | 1.20 GB |
| `custom20` | 75 | 2.04 GB |
| `archetype11` | 75 | 5.00 GB |
| `archetype3` | 75 | 5.00 GB |
| `custom17` | 75 | 5.94 GB |
| `custom15` | 74 | 7.12 GB |

:::note[21 variants, not 18]
The upstream dataset card describes "up to 18" degradation pipelines. The
published data actually contains **21** distinct variants. Five of them
(`archetype2`, `archetype9`, `custom22`, `custom23`, `default`) are *partial* —
they cover 20–46 of the 75 source documents rather than all 75 — which is how a
per-document reading undercounts the distinct pipelines. The picker labels
partial variants explicitly, because comparing accuracy across variants with
different document populations is not an apples-to-apples comparison.
:::

## Running a test

1. Wait for the ingest job to reach **Completed** (progress updates live; the job
   continues if you navigate away).
2. Open **Test Studio → Run Test Set** and select the ConfBench test set.
3. **Configuration version** is preselected to `confbench-testset-v<version>` —
   the Invoice extraction schema this extension installed. Override it to
   evaluate a different configuration against the same documents.

Each ConfBench test set records that configuration on its own test-set record
(the `configVersion` field), which is how Test Studio knows to preselect it. The
stack-managed benchmark sets instead rely on their config version being *named*
after the test set id; that convention can't reach extension presets, since the
Feature Platform names them `<featureId>-v<version>`.

## How the ingest works

Installing creates a Step Functions state machine. Starting a job runs:

1. **Plan** — download the dataset's parquet metadata once (76 KB), filter it to
   the selected variants, and write a shared row index to S3.
2. **Ingest** — a `Map` state over the selected variants, 4 at a time. Each
   worker streams its PDFs from the HuggingFace CDN straight into the host's Test
   Set bucket via multipart upload, and writes each document's ground-truth
   baseline. Workers shard **by bytes**, not file count, and resume from an
   offset — so a 7 GB variant splits across several passes while a 0.02 GB one
   finishes in a single call.
3. **Finalize** — count what actually landed in the bucket (rather than trusting
   counters), consolidate any per-variant failure reports, and register the test
   set as `COMPLETED`.

Nothing waits on a CloudFormation response, so a long transfer cannot fail a
stack operation. A failed job is retryable and leaves whatever already
transferred in place.

Partial failures **degrade gracefully**: if some documents fail to download the
test set is still registered with whatever succeeded, and the failure detail is
written to
`s3://<TestSetBucket>/_confbench_jobs/<jobId>/report.json`. A job that transfers
nothing at all is marked `FAILED`.

## Removing a dataset

Use **Delete test set** in the extension UI to remove an ingested set — every
document and baseline, plus its Test Studio record. This is the way to reclaim
the storage; uninstalling the extension deliberately leaves ingested data in
place (the Test Set bucket is `Retain`-policied and shared with the host).

Both installing and deleting require the **Admin** role.

## Costs

Deploying documents costs S3 storage plus one-time transfer. At S3 Standard list
price (~$0.023/GB-month, us-east-1) the full dataset is roughly **$0.75/month**;
the representative tier about **$0.10/month**; the clean baseline is
effectively free. *Processing* the documents through the pipeline — OCR plus
model inference on up to 1,346 documents — will dominate that by orders of
magnitude, so size your test runs deliberately (Test Studio's **Number of files**
field limits a run without re-ingesting).

The host Test Set bucket's own `DataRetentionInDays` lifecycle rule applies to
ingested documents like any other test data.

## Attribution

The ConfBench deployer was originally contributed as an always-on main-stack
dataset deployer in
[PR #583](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/pull/583)
by [@sujimart](https://github.com/sujimart). This extension reworks that
contribution as an opt-in, subset-able extension; the streaming multipart
transfer and ground-truth baseline format are carried over from it.

## Reference

- Dataset: [amazon/ConfBench](https://huggingface.co/datasets/amazon/ConfBench)
- Source dataset: [amazon-agi/RealKIE-FCC-Verified](https://huggingface.co/datasets/amazon-agi/RealKIE-FCC-Verified)
- Augmentation library: [Augraphy](https://github.com/sparkfish/augraphy)
- Source: [`feature-platform/confbench-testset/`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/tree/main/feature-platform/confbench-testset)
- [Test Studio](../test-studio.md) · [Feature Platform](../feature-platform.md)
- [Can You Trust the Confidence? ConfBench for Vision-Language Models on Document Extraction](https://arxiv.org/abs/2608.01792)
