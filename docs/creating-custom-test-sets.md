---
title: "Creating Custom Test Sets with Ground Truth"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Creating Custom Test Sets with Ground Truth

This guide walks through the end-to-end workflow for creating a custom test set with ground truth (evaluation baseline) data from scratch. Once created, the test set can be used for:

- **Benchmarking** — Compare accuracy across different models and configurations
- **Cost optimization** — Find the cheapest model that meets your accuracy requirements
- **Prompt engineering** — Measure the impact of prompt and schema changes
- **Custom model training** — Provide labeled training data for fine-tuning (see [Custom Model Fine-Tuning](./custom-model-finetuning.md))

> **Pre-deployed test sets**: The accelerator ships with four ready-to-use benchmark datasets. If you just want to run tests against those, see [Test Studio — Pre-Deployed Test Sets](./test-studio.md#pre-deployed-test-sets). This guide is for creating your **own** test set from your own documents.


https://github.com/user-attachments/assets/d5e0d590-ce8b-4e14-b2b7-8bde31e57ec2


## Why you need one

When you use AI to extract data from documents, the first question is always: how accurate
is it? There is one honest way to answer — compare the system's answers against answers
you already know are correct. A golden dataset is that answer key: real documents where a
person has confirmed the correct value for every field you extract.

Two things it gives you that nothing else does.

**A real accuracy number.** Skimming documents and forming an impression is not a
substitute. Check a field on 20 documents and its true accuracy could sit anywhere in a
27-point range — a field that looks like 90% could really be 70%. Nobody can make a launch or
staffing decision on a range that wide. Test Studio reports the per-field margin alongside
the accuracy for exactly this reason (see [Field-Level
Metrics](./test-studio.md#field-level-metrics)).

**A way to improve.** Every change you make — a reworded prompt, a different model — fixes
some fields and breaks others at the same time. The only way to know whether a change
helped *overall* is to score before and after against the same answer key. Most of what
teams find this way turns out to be gaps in their own prompts and mistakes in their own
labels, not model problems, and none of it surfaces without the answer key.

### Why there are no shortcuts

**The answer key has to be better than the system it grades.** If a tenth of your labels
are wrong and your system is also wrong a tenth of the time, half the "errors" you chase
will be mistakes in your own answer key. Careful annotators still get roughly 1 field in
20 wrong on a first pass, and a golden set needs to be far better than that. The way to
close the gap is repetition: label, compare against model output, correct, compare again.
Golden data is the product of several passes, never one.

**Do not let the system grade itself.** Scored against its own predictions, a
configuration reports near-perfect accuracy no matter how it actually performs, so
self-grading measures nothing. This matters because of how the set gets built: to save
labour we start from model predictions and have a person correct them rather than label
from scratch (bootstrapping). It works and we recommend it, but reviewers tend to approve
whatever is already filled in, so a bad prompt can push its own mistakes into the answer
key and then score well against them.

The accelerator manages that tradeoff in two ways. Confidence-guided review puts the
fields most likely to be wrong in front of the reviewer first, so a focused review that
actually happens beats a thorough one that does not. And scoring is **refused** outright
when a run would measure a configuration against labels that same configuration drafted —
see [How much review is enough?](./test-studio.md#how-much-review-is-enough).

### How much data

Your overall accuracy score firms up quickly, within roughly the first hundred documents.
The accuracy of an individual *field* does not, because a field appearing once per
document gives you one observation per document — so a badly failing field can hide inside
a healthy-looking overall score. Fields that repeat within a document (invoice line items)
need fewer documents; fields appearing once per document drive the requirement.

| Documents | Observations per field | 95% margin |
|---|---|---|
| 20 | 20 | ±13.7 pts |
| 100 | 100 | ±6.0 pts |
| 300 | 300 | ±3.4 pts |
| 500 | 500 | ±2.6 pts |

Test Studio does not size your set for you — it tells you how much of the set you already
have still needs human review, and how much to trust that estimate. Use the table above to
decide how many documents to *collect*; use the estimator to decide how much of them to
*review*.

## Two paths

Both end with a published test set you can score against. Pick by what you already have.

### Path A — bootstrap the labels (recommended)

Start from the test set and let the pipeline draft the labels. Fewest actions, and it is
the path the confidence-guided review and effort estimator are built around.

```
┌──────────────┐   ┌──────────────┐   ┌───────────────┐   ┌──────────────┐   ┌─────────────┐
│ 1. Define    │──▶│ 2. Create a  │──▶│ 3. Generate   │──▶│ 4. Review    │──▶│ 5. Publish  │
│    fields    │   │    test set  │   │    draft      │   │    worst-    │   │    a        │
│              │   │    from docs │   │    labels     │   │    first     │   │    version  │
└──────────────┘   └──────────────┘   └───────────────┘   └──────────────┘   └─────────────┘
  What to extract   No baseline/       Runs the active     Only what the      Runs pin the
  and what each     folder needed      config over the     estimator says     ground truth
  field means                          set                 needs it           they scored
```

1. **Define your fields** in the configuration panel — the structure, each field's meaning,
   its type and expected format, and which answer is correct when a document offers two
   candidates. This is Step 1 and Step 2 below, and it is the part only your team can do.
2. **Create a test set from documents alone** — no `baseline/` folder. See [Creating Test
   Sets](./test-studio.md#creating-test-sets).
3. **Generate draft labels.** The set's documents run through the ordinary OCR →
   classification → extraction → assessment pipeline, and the results are written as
   ground-truth candidates tagged `draft-machine` with per-field confidence. See [Draft
   labeling](./test-studio.md#draft-labeling-unlabeled-documents--ground-truth).
4. **Review worst-first.** **Set up team annotation** tells you how many documents need
   human eyes for a target label accuracy, how much that will cost in time, and how much to
   trust that estimate. Several people can work one set at once, scoped to only their
   assigned sets. See [How much review is
   enough?](./test-studio.md#how-much-review-is-enough) and [Team
   annotation](./test-studio.md#team-annotation-the-scoped-queue).
5. **Publish a version** so runs pin the ground truth they scored against — see
   [Versioning test sets](./test-studio.md#versioning-test-sets).

### Path B — process first, then register (more control)

The original workflow: process documents *before* the test set exists, correct the
predictions in the document editor, and register the corrected results as ground truth.
Choose this when you want Discovery to derive the schema from your samples, or want
per-document control before anything is registered.

```
┌─────────────┐    ┌─────────────┐    ┌──────────────┐    ┌──────────────┐    ┌─────────────┐    ┌───────────────┐
│ 1. Configure │───▶│ 2. Discover │───▶│ 3. Process   │───▶│ 4. Review &  │───▶│ 5. Create   │───▶│ 6. Run Test   │
│    Models    │    │    Schema   │    │    Documents  │    │    Correct   │    │    Test Set  │    │    Executions │
└─────────────┘    └─────────────┘    └──────────────┘    └──────────────┘    └─────────────┘    └───────────────┘
  Use the best       Bootstrap          Process sample      Edit predictions    Save as eval       Compare models,
  model for high     document classes    docs with your      and fix errors      baseline &         prompts, and
  accuracy           from samples        configuration       in the UI editor    register set       configurations
```

The six steps below document Path B. Steps 1–2 apply to both paths, and Step 6 (running
and comparing test executions) is how you use the set whichever way you built it.

## Step 1: Configure for Maximum Accuracy

The goal of this initial run is to produce predictions that are as accurate as possible, minimizing the amount of manual editing you'll need to do later. Use the best available model for both classification and extraction.

1. Go to **Configuration** in the web UI
2. Create a new configuration version (or edit an existing one)
3. Set both the **classification model** and **extraction model** to a high-accuracy model (e.g., Claude Opus)
4. Save the configuration version

> **Tip**: You can always create a cheaper configuration later for production use. The expensive model is only used here to bootstrap high-quality ground truth.

For details on configuration management, see [Configuration](./configuration.md) and [Configuration Profiles](./configuration-profiles.md).

## Step 2: Discover the Document Schema

If you don't already have document classes defined for your document type, use Discovery to bootstrap the schema automatically.

1. Go to **Discovery** in the web UI
2. Select your high-accuracy configuration version
3. Upload a representative sample document
4. Run discovery — it will analyze the document and populate document classes and attributes

After discovery completes, verify the schema in your configuration under **Document Schema**. You should see the discovered document class with its attributes populated.

For details on discovery modes and options, see [Discovery](./discovery.md).

## Step 3: Process Your Sample Documents

Now process a set of sample documents that will become your test set.

1. Go to **Upload Documents** in the web UI
2. Select your high-accuracy configuration version
3. Upload your sample documents
4. Wait for all documents to finish processing

> **How many documents?** For illustration, a handful of documents is fine. For a meaningful benchmark test set, aim for a larger representative sample. For custom model training, you'll need a significant number of labeled documents — see [Custom Model Fine-Tuning](./custom-model-finetuning.md) for guidance on training data requirements.

## Step 4: Review, Edit, and Save Ground Truth

This is the most important step. You'll review each document's predictions, correct any errors, and save the corrected version as evaluation baseline (ground truth).

### Review and Edit Predictions

For each processed document:

1. Open the document from the document list
2. Click **View Data** to see the extracted information
3. Click **Edit Data** to enter edit mode
4. Review each extracted field:
   - Click on a field to highlight it in the document viewer
   - Compare the extracted value against the source document
   - Correct any errors by editing the field value directly
5. **Save** your changes — the system creates a revision history of all edits

> **Tip**: The solution generates a confidence score for each field. To save time, you could focus on reviewing lower-confidence fields first. However, for the highest quality ground truth, review all fields.

### Save as Evaluation Baseline

Once you're confident the predictions are correct for a document:

1. Click the **Use as Evaluation Baseline** button
2. The system copies the corrected predictions to the evaluation baseline bucket

Repeat this for every document you want to include in your test set.

For details on the editing interface, see [Web UI — Edit Data](./web-ui.md#edit-data). For details on the evaluation baseline concept, see [Evaluation Framework](./evaluation.md).

## Step 5: Create the Test Set

Now register a test set that references your documents and their ground truth.

1. Go to **Test Studio** → **Test Sets** tab
2. Click **Add Test Set**
3. Give the test set a name
4. Specify the input bucket path containing your processed files
5. Verify the file count matches your expectations
6. Click **Add Test Set**

For details on test set management, see [Test Studio](./test-studio.md).

### Browse and Edit the Test Set's Ground Truth

Once a test set is `COMPLETED`, click its name in the **Test Sets** table (or
select it and click **Browse Documents**) to open the test set browser at
`/test-studio/sets/<test-set-id>` — a paginated document list with first-page
thumbnails. Click a document's name to open its detail page, where you can:

- **View Source Document** — render the original PDF or image inline.
- **Edit Ground Truth** — a visual editor showing the document's page images
  beside an editable form over the baseline's `inference_result` (with a raw
  JSON editor tab). Multi-section baselines (e.g. packet-splitting sets) get a
  section selector, and the image pane follows the selected section's
  `split_document.page_indices`.

If the baseline was created via **Copy to Baseline** it carries
`explainability_info` geometry, so focusing a field highlights its bounding
box on the page image. Hand-built baselines without geometry still work —
fields simply have no boxes. Saves write the section's `result.json` back to
the test set's `baseline/` folder and append an `_editHistory` entry for
provenance. Editing requires the Admin or Author role; other roles get a
read-only view.

## Step 6: Run Test Executions and Compare

With your test set created, you can now run test executions to compare different configurations.

### Run a Baseline Test

1. Go to **Test Studio** → **Test Executions** tab
2. Select your test set
3. Choose the high-accuracy configuration version you used to create the ground truth
4. Run the test

This establishes your baseline — it should show near-perfect accuracy since the ground truth was generated from these same model predictions.

### Compare with Alternative Configurations

Create and test alternative configurations to find the best cost/accuracy balance:

1. Create a new configuration version with a cheaper model (e.g., Nova Lite)
2. Run a test execution against the same test set using the new configuration
3. Use the **comparison view** to analyze the results side-by-side

### Analyzing Results

The comparison view shows:

- **Overall accuracy** — How each configuration performed against the ground truth
- **Cost comparison** — Total processing cost for each configuration
- **Field-level metrics** — Which specific fields lost accuracy with the cheaper model

This data helps you identify:
- Whether a cheaper model meets your accuracy requirements
- Which fields need attention (e.g., improved prompts, better attribute descriptions)
- The cost/accuracy tradeoff for your specific document type

For details on evaluation metrics and reporting, see [Evaluation Framework](./evaluation.md) and [Enhanced Reporting](./evaluation-enhanced-reporting.md).

## Incrementally Growing Your Test Set

You don't have to create your entire test set in one go. As you process and review more documents over time, you can add them to an existing test set:

1. Process new documents and save their evaluation baselines (Steps 3-4 above)
2. Go to **Test Studio** → **Test Sets** tab
3. Select your existing test set and click **Add Documents** → **From Existing Files**
4. Select the **Input Bucket** and enter a file pattern matching your new documents
5. The file pattern is pre-filled from the original test set — adjust if needed
6. Optionally use the **Modified after** filter (e.g., "Last 24 hours" or a custom date/time) to easily find recently reviewed documents
7. Click **Check Files** to preview matches, then **Add Documents**

Files without matching baseline data are automatically excluded, so you can use a broad pattern — only documents you've reviewed and saved as evaluation baselines will be added. The test set's file count is updated automatically.

https://github.com/user-attachments/assets/bcd18e62-4795-44ea-9554-637062fd21d7

## Publishing a Version

Once the ground truth is in the shape you want, **publish a version** of the
test set. This freezes the current documents and labels as a numbered version
and makes it the *active reference*, so every subsequent test run records which
ground truth it scored against — which is what lets you tell later whether a
metric moved because your configuration changed or because the labels did.

1. Go to **Test Studio** → **Test Sets** tab
2. Select the test set and click **Publish version**

You don't need every document reviewed first: unreviewed fields keep their
machine labels and stay flagged as such, so a time-boxed "first pass" benchmark
is a legitimate thing to publish. Publish again whenever the set changes
materially. See [Versioning test sets](./test-studio.md#versioning-test-sets)
for the storage caveat on what a version does and does not freeze.

## Next Steps

- **Improve accuracy**: Use field-level metrics to refine your document class descriptions, attribute prompts, and few-shot examples. See [IDP Configuration Best Practices](./idp-configuration-best-practices.md) and [Few-Shot Examples](./few-shot-examples.md).
- **Train a custom model**: If your test set is large enough, use it to fine-tune a custom model. See [Custom Model Fine-Tuning](./custom-model-finetuning.md).
- **Automate with CLI/SDK**: Create and run test sets programmatically. See [IDP CLI](./idp-cli.md) and [IDP SDK](./idp-sdk.md).

## Related Documentation

- [Configuration](./configuration.md)
- [Discovery](./discovery.md)
- [Test Studio](./test-studio.md)
- [Evaluation Framework](./evaluation.md)
- [Web UI](./web-ui.md)
- [Custom Model Fine-Tuning](./custom-model-finetuning.md)
