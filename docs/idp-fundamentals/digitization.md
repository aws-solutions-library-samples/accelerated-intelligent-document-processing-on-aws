---
title: "Digitization (OCR)"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Digitization (OCR)

## Overview

Before any model can find a field on a page, something has to read the page. Digitization
turns each page of each file into **page text**: the words on the page, in reading order,
with tables kept as rows and columns. Optical character recognition (OCR) is the usual name
for the job. These pages call the service or model that does the job the **reader**.

Some readers also return where each line and word sits on the page, and how sure the reader
is of each one. The accelerator keeps that position and confidence data next to the page
text, and later steps use it to draw boxes around extracted values and to judge how far to
trust them.

Here is a scanned invoice page for broadcast advertising, and the page text a reader returns
for it:

```markdown
# INVOICE
Invoice No: 10482        Invoice Date: 03/14/2026
Advertiser: Harbor Credit Union

| Description              | Spots | Amount   |
|--------------------------|-------|----------|
| Morning News, 30 seconds | 4     | 1,200.00 |
| Evening News, 30 seconds | 2     | 900.00   |

Total Due: 2,100.00
```

Extraction then reads `Invoice No: 10482` from the page text and fills the `invoice_number`
column of your invoice table. The two rows of the table become two line items. Every later
step works from this output, so a value the reader misses cannot reach your database.

```mermaid
flowchart LR
    F["Invoice page<br/>scanned image"] --> R{{"Reader"}}
    R --> T["Page text<br/>words in reading order,<br/>tables as Markdown"]
    R --> P["Positions<br/>where each line and<br/>word sits"]
    R --> C["Confidence<br/>how sure the reader is<br/>of each line"]
    T --> N["Classification,<br/>extraction and<br/>confidence assessment"]
    P --> N
    C --> N
    F -. "page image" .-> N

    classDef file fill:#f1f5f9,stroke:#475569,color:#0f172a,stroke-width:2px
    classDef app fill:#dbeafe,stroke:#2563eb,color:#1e3a8a,stroke-width:2px
    classDef stubA fill:#dcfce7,stroke:#16a34a,color:#14532d,stroke-width:2px
    classDef stubB fill:#ecfccb,stroke:#65a30d,color:#365314,stroke-width:2px
    classDef bank fill:#ffedd5,stroke:#ea580c,color:#7c2d12,stroke-width:2px
    class F file
    class R app
    class T stubA
    class P,C stubB
    class N bank
```

**The accelerator adds no OCR of its own.** It renders your pages, hands them to the reader
you choose, and passes the result and the page image to every later step. Your choice is
which reader to use and how to prepare the pages for it.

## Page reading

Digitization runs in three steps for every file: the accelerator renders each page as an
image, a reader turns the image into page text, and the accelerator stores both for the
steps that follow.

```mermaid
flowchart LR
    I["Your file, rendered<br/>as page images<br/>at 300 DPI"] --> B{"OCR backend"}
    B --> A["Managed service<br/>Amazon Textract or<br/>Bedrock Data Automation"]
    B --> V["Vision-language model<br/>on Amazon Bedrock"]
    B --> H["Specialized OCR model<br/>through a Lambda hook"]
    A --> O["Page text, with positions<br/>and confidence when<br/>the reader returns them"]
    V --> O
    H --> O
    O --> L["Classification, extraction<br/>and confidence assessment"]
    I -. "stored page image" .-> L

    classDef file fill:#f1f5f9,stroke:#475569,color:#0f172a,stroke-width:2px
    classDef app fill:#dbeafe,stroke:#2563eb,color:#1e3a8a,stroke-width:2px
    classDef stubA fill:#dcfce7,stroke:#16a34a,color:#14532d,stroke-width:2px
    classDef stubB fill:#ecfccb,stroke:#65a30d,color:#365314,stroke-width:2px
    classDef bank fill:#ffedd5,stroke:#ea580c,color:#7c2d12,stroke-width:2px
    class I file
    class B,A app
    class V stubA
    class H stubB
    class O,L bank
```

### Step 1: page images

The accelerator renders each page of a PDF at **300 DPI** by default, which keeps small print,
box numbers and hand-filled values legible. A US Letter page becomes a 2550 × 3300 pixel
image. An out-of-memory ceiling of 2600 × 3600 pixels shrinks only pages larger than Letter
or A4, such as Legal, and the accelerator never enlarges an image. An image file, such as a
phone photo or a scanned PNG, keeps its own resolution.

Word, Excel, CSV and plain-text files already hold their text, so the accelerator reads that
text directly and renders page images from it for the later steps.

Two settings change how the accelerator prepares each page:

- **Resolution.** In the configuration editor, the *Dpi* field under *OCR* > *Image
  Processing Settings* (`ocr.image.dpi`). *Target Width* and *Target Height*
  (`ocr.image.target_width`, `ocr.image.target_height`) set the ceiling. See
  [OCR Image Sizing](../ocr-image-sizing-guide.md#defaults).
- **Preprocessing.** *Preprocessing* (`ocr.image.preprocessing`) applies adaptive
  binarization: the accelerator turns the page into clean black text on a white background
  before the reader sees it, which helps with shadows, uneven lighting and faint scans. The
  stored page image stays as it was. See
  [Image Preprocessing Configuration](../pattern-2.md#image-preprocessing-configuration).

### Step 2: page text

The accelerator sends each page to the reader you choose, one call per page, with up to 20
pages in flight at once (`ocr.max_workers`). In the editor the choice is **OCR backend**
(`ocr.backend`). Three kinds of reader are available.

**A managed OCR service.**

- **Amazon Textract** (*Amazon Textract (default)*, `textract`) returns every line and word
  with its position and a confidence score, and marks handwriting. You choose its analysis
  features in **Textract features** (`ocr.features`):
  - `LAYOUT` puts the text in reading order, with headings and paragraphs.
  - `TABLES` returns each table cell by cell, which the accelerator writes as a Markdown
    table.
  - `FORMS` returns key-value pairs, such as a label and the value printed beside it.
  - `SIGNATURES` reports each region that holds a signature, with its position and a
    detection confidence.

  With no features selected, Textract returns plain lines of text.
- **Amazon Bedrock Data Automation** (*Bedrock Data Automation*, `bda`) returns Markdown with
  tables and layout, plus every line and word with its position and confidence, from one
  call per page. The stack creates the project the backend needs in most commercial AWS
  Regions; the backend is not available in AWS GovCloud (US) or China. See [When to choose BDA vs Textract for OCR](../configuration.md#when-to-choose-bda-vs-textract-for-ocr).

**A vision-language model on Amazon Bedrock** (*Bedrock LLM*, `bedrock`). A model that reads
images, such as Amazon Nova 2 Lite (the default in **Model**, `ocr.model_id`), receives the
page image with an instruction to transcribe it, and writes the page text as Markdown. You
can edit the instruction in **Task prompt** (`ocr.task_prompt`) and **System prompt**
(`ocr.system_prompt`), for example to ask for tables as Markdown pipe tables. A
vision-language model returns no positions and no confidence. See
[Bedrock OCR](../pattern-2.md#bedrock-ocr-feature).

**A specialized OCR model through a Lambda hook.** Set **OCR backend** to *Bedrock LLM*,
choose *LambdaHook* in **Model**, and enter your function in **Model Lambda hook ARN**
(`ocr.model_lambda_hook_arn`). The accelerator then sends each page image to your AWS Lambda
function, which can call any OCR model, hosted by you or by a third party. Ready-made samples
call [Chandra](../lambda-hook-inference.md#chandra-ocr-integration),
[Mistral OCR](../lambda-hook-inference.md#mistral-ocr-integration) and
[Cohere Parse](../lambda-hook-inference.md#cohere-parse-integration). A hook can return
positions and confidence alongside the text, in Textract's format, and the accelerator then
uses them as it uses Textract's. See
[structured OCR output](../lambda-hook-inference.md#optional-structured-ocr-output-confidence--geometry).

A fourth setting, *None — images only* (`none`), skips the reader. Classification and
extraction then work from the page images alone.

What each reader returns:

| Reader | Page text | Tables | Positions | Confidence |
|---|---|---|---|---|
| Amazon Textract | Lines in reading order | Markdown tables, with `TABLES` | Every line and word | Every line and word |
| Bedrock Data Automation | Markdown in reading order | Markdown tables | Every line and word | Every line and word |
| Vision-language model | Markdown, as the model writes it | Markdown, when the prompt asks for it | None | None |
| Mistral OCR hook | Markdown | Markdown tables | One box per paragraph | Every line and word |
| Cohere Parse hook | Markdown | Converted to Markdown tables | Tables and figures only | None |
| Chandra hook | Markdown | Markdown tables | None | None |

For the full comparison, see the
[cross-backend matrix](../../lib/idp_common_pkg/idp_common/ocr/README.md#cross-backend-matrix).

### Step 3: output

For every page, the accelerator stores the page image, the page text, a compact table of
each line with its confidence, and the positions of every line and word. Each later step
reads the part it needs:

- **Classification** reads the page text and the page image. In the classification settings
  these are **Send page text** and **Send page images**.
- **Extraction** reads the page text and the page image of every page in the section. The
  extraction model can check a value in the text against the image, and read checkboxes and
  signatures from the image.
- **Confidence assessment** gives the **scoring model** (the model that assigns each
  extracted value a confidence score) the reader's line-by-line confidence alongside the page.
  See [Confidence Assessment](../extraction-and-confidence.md#3-confidence-assessment).
- **Bounding boxes** come from the reader's positions. By default the accelerator matches
  each extracted value to the OCR line that holds it and draws a box around that line. See
  [OCR grounding](../extraction-and-confidence.md#ocr_only-default--ocr-grounding-via-pagedatajson).
- **Table parsing** in agentic extraction reads Markdown tables from the page text directly,
  without asking a model to copy every row. See
  [Deterministic table parsing](../extraction-and-confidence.md#deterministic-table-parsing).

Classification and extraction can each send a smaller copy of the page image to their
model, through their own image settings. The stored image at full resolution stays
untouched.

### Configurations

| Configuration | OCR backend | Settings | Returns positions and confidence |
|---|---|---|---|
| **Textract with tables (default)** | *Amazon Textract (default)* | `TABLES`, `LAYOUT`, `SIGNATURES`, 300 DPI | Yes |
| Textract, layout only | *Amazon Textract (default)* | `LAYOUT` | Yes |
| Bedrock Data Automation | *Bedrock Data Automation* | None to choose | Yes |
| Vision-language model | *Bedrock LLM* | A model in **Model**, and your **Task prompt** | No |
| Specialized OCR model | *Bedrock LLM* | *LambdaHook* and your function's ARN | Depends on the model |
| Images only | *None — images only* | None | No |

These settings apply in pipeline mode. In BDA mode (`use_bda: true`), Bedrock Data Automation
reads the pages as part of its own processing, and the editor hides the *OCR* section. See
[Pattern 1](../pattern-1.md).

For the configuration we recommend, and the measurements behind it, see
[Digitization benchmarks](../benchmark-guidance/digitization.md).

## Token use

**Textract and Bedrock Data Automation do not use tokens.** Each makes one call per page.
Textract charges per page by the features you select. Bedrock Data Automation charges one
rate per page, whatever the page holds. A specialized OCR model behind a Lambda hook is
billed however its provider bills it, often per page.

**A vision-language model uses tokens on every page.** Three things decide how many:

- **The page image.** The input is the instruction and the page image. How many tokens an
  image costs depends on the model and the image size, and the model scales large images down before counting, so
  a higher resolution adds few tokens.
- **How much the page says.** The output is the transcription, so a dense page of small print
  or a long table costs far more output tokens than a check.
- **The model.** Models count image tokens differently, and larger models charge more for
  each token.

**The reader also changes the tokens every later step uses,** because each step reads the
page text the reader wrote:

- **Longer page text** means more input tokens for classification and extraction.
- **`TABLES`** adds Markdown table markup to the page text.
- **The reader's confidence.** Textract and Bedrock Data Automation add each line's
  confidence to the scoring model's prompt. A reader with no confidence makes that prompt
  shorter, and the scoring model judges each value without the reader's confidence.

See [What is the cheapest configuration that keeps accuracy?](../benchmark-guidance/token-use.md#what-is-the-cheapest-configuration-that-keeps-accuracy)

## Measuring accuracy

Few teams have a correct transcription of their own pages, and you do not need one. Measure
a reader in two stages.

**1. Shortlist readers with public OCR benchmarks.** The
[OmniAI OCR benchmark](https://github.com/getomni-ai/benchmark),
[OmniDocBench](https://github.com/opendatalab/OmniDocBench) and
[olmOCR-Bench](https://huggingface.co/datasets/allenai/olmOCR-bench) score readers directly
against reference transcriptions or checks of each page. Each covers different kinds of
document, so pick the benchmarks whose pages look most like yours, and shortlist the readers
that score well there.

**2. Compare the shortlist by what reaches your database.** Run your test set once with each
reader and keep every other setting the same. Ground truth here is the ground truth you
already use for classification and extraction: the right document class of each section and
the right value of each field. These numbers show how well digitization is working:

| Number | Question it answers |
|---|---|
| **Extraction accuracy** | What share of field values match the ground truth? Stickler weights each field by the weight you give it, averages over the sections of a file, and reports the result as the file's *weighted overall score*. |
| **Paired difference** | For the same files, how much higher or lower is extraction accuracy with reader B than with reader A? Subtract file by file, then average. The standard error (SE) is the standard deviation of the differences divided by the square root of the number of files. |
| **Page accuracy** | What share of pages received the right document class? A reader that garbles a heading can change the document class. |
| **Errors the reader caused** | Of the field values extraction got wrong, what share are missing or misread in the page text? Open the page text of each wrong value to tell a reading error from an extraction error. |

Compare readers file by file rather than by their averages. How hard a file is varies far
more than how well two readers read it, so pairing removes most of the noise. A difference
smaller than about twice its SE can be chance.

If you do have reference transcriptions for some pages, you can also measure the **character
error rate**: the number of characters to insert, delete or change to turn the page text into
the reference, divided by the length of the reference. A lower rate is better, but extraction
accuracy is still the number that tells you what reaches your database.

See [Evaluation](../evaluation.md#field-weighting-for-business-criticality) for how Stickler
weights fields, and [Measure with ground truth](index.md#4-measure-with-ground-truth) to
build the test set.

Build a test set of real files with their correct field values,
run it in [Test Studio](../test-studio.md) once per reader, and compare extraction accuracy
file by file. The
[Auto Optimizer](../extensions/auto-optimizer.md) can run comparisons of pipeline
configurations against your test set for you.

## Common mistakes

Once you know which wrong values the reader caused, look at how the page text is wrong.
Each reader, and each setting, tends to make its own kind of mistake:

| Configuration | Typical mistake | Example | What to change |
|---|---|---|---|
| **Textract, low resolution** | A value is missing from the page text | A page number or a hand-filled box number is absent below about 200 DPI | Keep `ocr.image.dpi` at 300. Scan image files at 300 DPI or more |
| **Textract, layout only** | A table comes back as lines of text, not a Markdown table, so agentic extraction's table tool and the row-count checks cannot read it | A long statement table | Add `TABLES`, or use Bedrock Data Automation |
| **Vision-language model** | A character changes, and the value looks right | A fixed-width ID such as `SEQ00042` comes back as `SEQ000042` | Use Textract for pages full of IDs and account numbers, or tighten the **Task prompt** |
| **Vision-language model, Chandra and Cohere Parse hooks** | No reader confidence for the scoring model | Confidence comes from the scoring model alone | Use Textract, Bedrock Data Automation or the Mistral OCR hook when confidence drives review |
| **Any reader, on photos** | Characters misread in shadows or faint areas | A phone photo of a receipt with a dark corner | Turn on *Preprocessing* (the Bedrock Data Automation backend does its own image processing and skips it) |
| **Textract with `SIGNATURES`** | A stray mark is reported as a signature | A smudge in an empty signature box | Remove `SIGNATURES` if no field asks about signatures. See [The SIGNATURES feature](../configuration.md#the-signatures-feature) |

## Improving accuracy

When your test set shows wrong values that the reader caused, try these changes, and score each one against your test set.

**Look at the page text before you change anything.** On any processed document, open
[View Page Text](../web-ui.md#view-page-text) for the page that holds a wrong value. The OCR
lines view shows each line with its confidence, and draws its box on the page image when the
reader returns positions. If the value is right in the page text, the cause is in
classification or extraction, not in digitization.

**Check the resolution when a value is missing from the page text.** The accelerator renders
PDFs at 300 DPI, but an image file keeps the resolution it was scanned or photographed at. See
[When to change the defaults](../ocr-image-sizing-guide.md#when-to-change-the-defaults).

**Add `TABLES` when tables come back wrong.** If rows run together or cells land in the
wrong column, add `TABLES` in **Textract features**. Agentic extraction's table tool and the
row-count checks read the Markdown tables `TABLES` produces; with `LAYOUT` alone they have
no tables to read. See
[Textract features](../configuration.md#textract-features--the-tables-costaccuracy-trade-off).

**Turn on preprocessing for photos and poor scans.** *Preprocessing* cleans up shadows,
uneven lighting and low contrast before the reader sees the page. See
[OCR image processing](../pattern-2.md#ocr-image-processing-features).

**Send the page image to the extraction model.** The default extraction prompts send the
page image with the page text, so the extraction model can check a misread character
against the image and see checkboxes the text does not show. If you write your own
extraction prompt, keep the `{DOCUMENT_IMAGE}` placeholder in it. See
[What is the impact of document modality?](../benchmark-guidance/extraction.md#what-is-the-impact-of-document-modality)

**Tell a vision-language model exactly what to write.** The **Task prompt** decides what the
page text looks like. Ask for tables as Markdown pipe tables, so table parsing can read them,
and ask the model to copy numbers and IDs character for character.

**Correct a page by hand.** An Admin can fix the page text of one page with
[Edit Page Text](../web-ui.md#edit-page-text) and choose **Save and Reprocess**. The
accelerator saves the text and extracts the sections on that page again.

## Configuration reference

- **Choose a reader and its features:**
  [OCR Backend Configuration](../configuration.md#ocr-backend-configuration-pipeline-mode),
  including [when to choose BDA or Textract](../configuration.md#when-to-choose-bda-vs-textract-for-ocr)
  and [Textract features](../configuration.md#textract-features--the-tables-costaccuracy-trade-off).
- **Use a vision-language model on Bedrock:** [Bedrock OCR](../pattern-2.md#bedrock-ocr-feature).
- **Bring your own OCR model:** [Lambda Hook Inference](../lambda-hook-inference.md), with
  ready-made [sample functions](../lambda-hook-inference.md#sample-lambda-functions) for
  Chandra, Mistral OCR and Cohere Parse.
- **Set resolution, image size and preprocessing:**
  [OCR Image Sizing](../ocr-image-sizing-guide.md) and
  [OCR Configuration](../pattern-2.md#ocr-configuration).
- **Pick a reader for your language:**
  [Languages](../languages.md#ocr-backend-considerations-for-language-support).
- **Deploy in AWS GovCloud (US):** use Textract, because the Bedrock Data Automation
  backend is not available there. See
  [Bedrock Data Automation as the OCR backend](../govcloud-deployment.md#bedrock-data-automation-as-the-ocr-backend).
- **See and correct the page text:** [View Page Text](../web-ui.md#view-page-text) and
  [Edit Page Text](../web-ui.md#edit-page-text) in the Web UI.
- **See how the scoring model uses the reader's confidence:**
  [Confidence Assessment](confidence-assessment.md), and
  [Confidence Assessment in the accelerator](../extraction-and-confidence.md#3-confidence-assessment).
