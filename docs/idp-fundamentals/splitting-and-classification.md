---
title: "Splitting and Section Classification"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Splitting and Section Classification

## What section detection and classification do

Stacks of documents rarely arrive one document per file. A loan package holds an
application, pay stubs, bank statements and an ID. A claim holds a claim form, invoices and
medical records. A scanned batch holds whatever arrived that day.

This step does two jobs for every file:

- **Section detection** finds where each document in the file starts and ends. Each document
  it finds is a **section**: a run of consecutive pages.
- **Classification** assigns each section a **document class**. A document class is a kind of
  document you define, such as *Pay stub*, with a description and the fields to extract
  from it.

The document class decides which fields extraction pulls from the section, and so which table
in your database the section's record goes to. A file that always holds exactly one document
is one section, and it still gets a document class.

In the example below, section detection finds four sections in a seven-page loan package,
including two pay stubs on consecutive pages. Classification then assigns each section its
document class.

```mermaid
flowchart LR
    F["Loan package<br/>one file, 7 pages"] -- "section<br/>detection" --> S1 & S2 & S3 & S4
    subgraph S1["Section 1"]
        direction LR
        P1["Page 1"] ~~~ P2["Page 2"] -. classification .-> T1(["Loan application"])
    end
    subgraph S2["Section 2"]
        direction LR
        P3["Page 3"] -. classification .-> T2(["Pay stub"])
    end
    subgraph S3["Section 3"]
        direction LR
        P4["Page 4"] -. classification .-> T3(["Pay stub"])
    end
    subgraph S4["Section 4"]
        direction LR
        P5["Page 5"] ~~~ P6["Page 6"] ~~~ P7["Page 7"] -. classification .-> T4(["Bank statement"])
    end

    classDef file fill:#f1f5f9,stroke:#475569,color:#0f172a,stroke-width:2px
    classDef app fill:#dbeafe,stroke:#2563eb,color:#1e3a8a,stroke-width:2px
    classDef stubA fill:#dcfce7,stroke:#16a34a,color:#14532d,stroke-width:2px
    classDef stubB fill:#ecfccb,stroke:#65a30d,color:#365314,stroke-width:2px
    classDef bank fill:#ffedd5,stroke:#ea580c,color:#7c2d12,stroke-width:2px
    class F file
    class P1,P2,T1 app
    class P3,T2 stubA
    class P4,T3 stubB
    class P5,P6,P7,T4 bank
    style S1 fill:#eff6ff,stroke:#2563eb,color:#1e3a8a
    style S2 fill:#f0fdf4,stroke:#16a34a,color:#14532d
    style S3 fill:#f7fee7,stroke:#65a30d,color:#365314
    style S4 fill:#fff7ed,stroke:#ea580c,color:#7c2d12
```

Extraction then reads each section with the fields of its document class: the loan
application fields from pages 1–2, the pay stub fields from page 3, the pay stub fields again
from page 4, and the bank statement fields from pages 5–7.

## How the accelerator detects and classifies sections

A language model does both jobs. You make three choices about how it works: how much of the
file the model reads at once, whether detection and classification happen in one model call
or two, and whether the model reads the page text, the page images, or both.

### Choice 1: read the file page by page, or all at once

**Page by page.** The model reads one page per call and assigns that page a document class.
The accelerator then groups consecutive pages into sections, starting a new section where the
document class changes or where the page shows that a new document begins, such as a new
letterhead or "Page 1 of 2". The model can also be shown the neighboring pages as context,
which multiplies the tokens each call reads.

```mermaid
flowchart LR
    P1["Page 1"] -- call 1 --> L1(["Loan application"])
    P2["Page 2"] -- call 2 --> L2(["Loan application"])
    P3["Page 3"] -- call 3 --> L3(["Pay stub"])
    P4["Page 4"] -- call 4 --> L4(["Pay stub"])
    P5["Page 5"] -- call 5 --> L5(["Bank statement"])
    P6["Page 6"] -- call 6 --> L6(["Bank statement"])
    P7["Page 7"] -- call 7 --> L7(["Bank statement"])
    classDef app fill:#dbeafe,stroke:#2563eb,color:#1e3a8a,stroke-width:2px
    classDef stubA fill:#dcfce7,stroke:#16a34a,color:#14532d,stroke-width:2px
    classDef stubB fill:#ecfccb,stroke:#65a30d,color:#365314,stroke-width:2px
    classDef bank fill:#ffedd5,stroke:#ea580c,color:#7c2d12,stroke-width:2px
    classDef file fill:#f1f5f9,stroke:#475569,color:#0f172a,stroke-width:2px
    class P1,P2,L1,L2 app
    class P3,L3 stubA
    class P4,L4 stubB
    class P5,P6,P7,L5,L6,L7 bank
```

The accelerator then groups pages 1–2 into one section and pages 5–7 into another. Pages 3
and 4 both receive *Pay stub*, so they become two sections only if page 4 shows that a new
pay stub starts there, such as a new pay period.

**All at once (holistic).** The model reads the whole file in one call and returns the
sections and their document classes together. Because it sees every page, it can compare
them to decide where one document ends and the next begins. By default one call
covers up to 20 pages. For a longer file, the accelerator reads it in overlapping 20-page
windows and merges the results.

```mermaid
flowchart LR
    F["Pages 1–7<br/>one call"] --> S1(["Section 1<br/>pages 1–2<br/>Loan application"])
    F --> S2(["Section 2<br/>page 3<br/>Pay stub"])
    F --> S3(["Section 3<br/>page 4<br/>Pay stub"])
    F --> S4(["Section 4<br/>pages 5–7<br/>Bank statement"])
    classDef app fill:#dbeafe,stroke:#2563eb,color:#1e3a8a,stroke-width:2px
    classDef stubA fill:#dcfce7,stroke:#16a34a,color:#14532d,stroke-width:2px
    classDef stubB fill:#ecfccb,stroke:#65a30d,color:#365314,stroke-width:2px
    classDef bank fill:#ffedd5,stroke:#ea580c,color:#7c2d12,stroke-width:2px
    classDef file fill:#f1f5f9,stroke:#475569,color:#0f172a,stroke-width:2px
    class F file
    class S1 app
    class S2 stubA
    class S3 stubB
    class S4 bank
```


In the configuration editor this choice is **granularity**: *page* or *packet*.

### Choice 2: detect and classify in one call, or in two

**One call.** The same model call that finds the sections also assigns their document
classes. This is the default, shown in the editor as *Split and classify together (one
call)*.

**Two calls.** A first call only finds where each section starts, without the list of
document classes. A second call then classifies each section, reading its first pages. This
is *Split first, then classify each section*. Separating the jobs lets the first call focus
on boundaries, and the classification call sees a whole section rather than one page.

### Choice 3: what the model reads

The model can read each page's **text** (from OCR), its **image**, or **both**. The text
carries the words; the image carries layout, letterheads, logos and checkboxes, which are often
the clearest signs that a new document has started. The default classification prompts send
both. In the editor these are **Send page text** and **Send page images**.

### How the choices combine

| Configuration | Granularity | Strategy | Model reads |
|---|---|---|---|
| Holistic, one call, with images | *packet* | *Split and classify together* | Text and images |
| Page by page, one call per page (default) | *page* | *Split and classify together* | Text and images |
| Detect sections first, then classify | *page* or *packet* | *Split first, then classify each section* | Text and images |
| Holistic, text only (fewest tokens) | *packet* | *Split and classify together* | Text |

For every setting, see
[How the classification settings are organized](../classification.md#how-the-classification-settings-are-organised).

## The configuration to start with

**For files of up to about 40 pages, start with holistic classification in one call, with
page images.** The model sees the whole file at once, which is what it needs to place section
boundaries, and the images give it the visual cues. We have seen strong results with this
configuration, and in our benchmark it found document boundaries more
accurately than page-by-page classification (see
[the benchmark results](#benchmark-results-accuracy-and-tokens-by-configuration)).

**For complex packets, especially files over about 30 pages, test the alternatives on your
own files.** Past the 20-page window, a holistic call no longer sees the whole file at once.
Compare holistic classification with *Split first, then classify each section* and with
page-by-page classification, using a test set of your own files.

**If every file holds exactly one document, skip section detection.** Set section splitting
to *One section*. The accelerator keeps all of a file's pages in one section and gives it the
document class that most of its pages receive.

## How many tokens classification uses, and what drives the count

Classification runs on every page of every file, so at high volume it is one of the largest
consumers of tokens in the pipeline. On real invoices processed with the default
configuration, classification used about 15% of all the tokens spent on each document.
Four things decide how many tokens it uses:

- **Page by page or holistic.** A page-by-page call repeats the instructions, the list of
  document classes and their descriptions for every page, and the model writes its reasoning
  for every page. A holistic call sends the instructions once per file and returns one
  answer.
- **Page images.** Each image adds input tokens. How many depends on the classification
  model and the image size: about 240 tokens per page with the default model at the default
  size in our benchmark, and up to several thousand with other models or larger images.
- **Neighboring pages as context.** Showing the model one neighbor on each side of a page
  triples the text and images each call reads; two neighbors on each side multiply it by
  five.
- **The classification model.** Models count tokens differently, especially for images, and
  larger models charge more for each token.

Measured classification tokens per page, on 20 public packets with the default classification
model, Amazon Nova 2 Lite (release 0.6.14, November 2026):

| Configuration | Input tokens per page | Output tokens per page |
|---|---|---|
| Page by page, text and images (default) | 2,633 | 217 |
| Page by page, text only | 2,394 | 234 |
| Holistic, text and images | 1,080 | 40 |
| Holistic, text only | 844 | 41 |

Input tokens include the instructions the accelerator serves from the prompt cache, which
Amazon Bedrock bills at a reduced rate. Source:
`benchmarks/results/v0.6.14/clsvariants/summary.json`.

What this shows:

- **Holistic classification used about 60% fewer input tokens and 80% fewer output tokens
  per page** than page-by-page classification, because it sends the instructions once per
  file and answers once.
- **Page images added about 240 input tokens per page** with the default model, in either
  mode.

At very high volume, compare holistic classification with text only against the same
configuration with images, and keep images if your test set shows they improve boundary
accuracy for your files. See [What each setting costs](../classification.md#what-each-setting-costs).

## How to measure section detection and classification on your own files

Ground truth for this step is the page range and document class of every section in each
file. Four numbers show how well section detection and classification are working:

| Number | Question it answers |
|---|---|
| **Page accuracy** | What share of pages received the right document class, whatever section they landed in? |
| **Boundary accuracy** | What share of the true sections came back with exactly the right page range? The document class is not checked. |
| **Label accuracy** | Of the sections with the right page range, what share also received the right document class? |
| **Over-splits and merges** | When a page range is wrong, was one document cut into several sections, or were several documents joined into one? |

Boundary accuracy measures section detection and label accuracy measures classification, so
together they show which of the two jobs needs work. Extraction needs both: a section with the
right pages and the right document class. Page accuracy alone can look high while boundaries
are wrong, because a page can carry the right document class inside the wrong section.

Stickler reports page accuracy as *page-level accuracy*, and reports *split accuracy*, which
counts a section as correct only when its page range and its document class are both right.
See [Document split classification metrics](../evaluation.md#document-split-classification-metrics).
To build the ground truth, see [Measure with ground truth](index.md#4-measure-with-ground-truth).

## Over-splits and merges: which configuration makes which mistake

Once you measure boundary accuracy, look at how the wrong page ranges are wrong. Page-by-page and
holistic classification tend to make opposite mistakes:

| Configuration | Typical mistake | Example |
|---|---|---|
| **Page by page** | **Over-split:** one document cut into several sections | A long statement that repeats its header on every page, where each page looks like a new start |
| **Holistic** | **Merge:** several documents joined into one section | Two statements of the same class filed back to back |

If your test set shows mostly over-splits, try holistic classification, or set section
splitting to *One section* when each file holds one document. If it shows mostly merges of
same-class documents, such as a run of pay stubs, try page-by-page classification. See
[Page versus packet granularity](../classification.md#page-versus-packet-granularity-opposite-failure-modes).

## Benchmark results: accuracy and tokens by configuration

### Files that hold many documents

**Dataset:** [DocSplit-Poly-Seq](benchmark-datasets.md#docsplit-poly-seq), 20 public files
(298 pages, 13 document classes, 5–20 pages per file, so every file fits in one holistic
call). **Run:** release 0.6.14, November 2026, classification model Amazon Nova 2 Lite (the
default), one run per configuration.

| Configuration | Page accuracy | Boundary accuracy | Label accuracy | Input tokens per page |
|---|---|---|---|---|
| Page by page, text and images (default) | 0.818 | 0.553 | 0.915 | 2,633 |
| Page by page, text only | 0.797 | 0.518 | 0.864 | 2,394 |
| **Holistic, text and images** | 0.720 | **0.659** | 0.893 | 1,080 |
| Holistic, text only | 0.735 | 0.644 | 0.845 | 844 |
| Holistic, images only | 0.836 | 0.578 | 0.846 | 368 |
| Detect sections first (page by page), then classify | 0.797 | **0.694** | **0.949** | 2,647 |
| Detect sections first (holistic), then classify | 0.732 | 0.656 | 0.848 | 1,717 |

Source: `benchmarks/results/v0.6.14/clsvariants/summary.json`.

What this shows:

- **Section detection is the harder job.** Every configuration gave most correctly bounded
  sections the right document class (label accuracy 0.85–0.95), while boundary accuracy
  ranged from 0.52 to 0.69. Improving boundaries is where the gains are.
- **Holistic classification found more boundaries, with fewer tokens.** Against the default,
  holistic classification with text and images raised boundary accuracy by 0.20 (SE 0.09,
  19 files) and used 59% fewer input tokens per page. The previous release measured +0.35 for
  the same comparison.
- **It gave fewer individual pages the right document class** (page accuracy −0.07, SE 0.07).
  Extraction depends on whole sections, so boundary and label accuracy are the numbers to
  follow.
- **Detecting sections first, then classifying, scored highest on both boundary and label
  accuracy** in this run. It is the main alternative to test on complex packets.

### Files that hold one or two documents

**Dataset:** synthetic bank statements with exact ground truth. **Run:** release 0.6.14, page
by page (the default), five runs per file, unless noted.

| File | Sections detected |
|---|---|
| Two statements in one file | **Two sections, in 5 of 5 runs** |
| One 3-page statement, with or without "Page k of n" | **One section, in 5 of 5 runs** |
| One 3-page statement with its header repeated on every page | Three sections, in 5 of 5 runs (release 0.6.9). Holistic classification, or section splitting set to *One section*, keeps it as one |

On single statements, every classification model we compared produced the right number of
sections in at least 132 of 133 runs, so the default classification model is enough there.

Sources: `benchmarks/results/v0.6.14/boundary/`, `benchmarks/results/v0.6.9/boundaryab/` and
`benchmarks/results/v0.6.14/coresynth__classification-model-*/`.

**How far this carries:** 20 files, one run per configuration, with one classification
model, and the gap between configurations has varied between releases. Confirm the choice on
your own files.

## Improving accuracy once your pipeline is running

When your test set shows sections with the wrong pages or the wrong document class, these
changes help most, roughly in this order.

**Write a description for each document class that separates it from its closest
neighbor.** The classification model picks a document class by comparing each section with
the description of every document class in your configuration. Name the features that tell
two classes apart: "Quote: shows a 'valid until' date and has no payment terms" separates a
quote from an invoice, and "Quote: a price quote" does not. See
[class descriptions](../classification.md#class-descriptions).

**Add few-shot examples for document classes that look alike.** A few-shot example is a
sample page with its correct document class, which the accelerator adds to the
classification prompt. One or two examples for each pair of document classes that the model
confuses, such as invoice and quote, usually stop the confusion. See
[Few-Shot Examples](../few-shot-examples.md).

**Separate documents of the same class on consecutive pages,** such as three pay stubs. If
holistic classification joins them into one section, try page-by-page classification, which
starts a new section at cues inside the pages such as "Page 1 of 2" or a new pay period. See
[Multiple W-2 Forms](../classification.md#real-world-example-multiple-w-2-forms).

**Check the pages that matched no document class.** When the model cannot match a page to
any document class in your configuration, the accelerator labels the page `unclassified`
instead of assigning the closest class. Extraction skips the section that holds the page, and
the accelerator records a processing issue on that section, which you see on the document's
Sections panel. A recurring unclassified page usually means a document class is missing. See
[Pages Classification Could Not Classify](../classification.md#pages-classification-could-not-classify).

**Exclude pages that carry no data.** Instruction pages and legal boilerplate repeat on every
copy of a form. Define a document class for them and mark it excluded
(`x-aws-idp-exclude-from-processing: true`). The model still recognizes those pages, and
extraction, confidence assessment and rule validation skip them. See
[Excluding Static Pages](../classification.md#excluding-static-pages-eg-instructions-legal-boilerplate).

## Run the benchmarks yourself

**On your own files.** Build a test set of real files with their correct sections, run it in
[Test Studio](../test-studio.md) once per configuration, and compare page accuracy and section
accuracy. Every stack includes the DocSplit test set, so you can repeat the comparison above
in your own account.

**The benchmark suite.**

```bash
# DocSplit configurations (the test set comes with the stack)
python3 benchmarks/harness/run_classification_bench.py --stack <STACK> --testset docsplit \
  --n 20 --models nova2lite --mode topk \
  --variants page-both,page-text,packet-both,packet-text,packet-images,split-then-classify-packet,split-then-classify-page

# Synthetic one- and two-document files
python3 benchmarks/harness/gen_corpus.py --only small_narrow,twodocs_2x20,paginated_3pg,repeathdr_3pg
python3 benchmarks/harness/make_configs.py --suite boundary --class bank_statement
python3 benchmarks/harness/run_matrix.py --stack <STACK> --suite boundary
```

Recompute a row from the committed results:

```bash
jq '."nova2lite-packet-both" | {class_accuracy, boundary_accuracy, label_accuracy, classification_input_tokens_per_page}' \
  benchmarks/results/v0.6.14/clsvariants/summary.json
```

See the [benchmark suite](../benchmarking/index.md) for setup and scoring.

## Where to configure this in the accelerator

- **Granularity, strategy and what the model reads:** [Classification](../classification.md),
  including [choosing granularity, modality and strategy](../classification.md#choosing-granularity-modality-and-strategy)
  and [the split-then-classify strategy](../classification.md#the-split-then-classify-strategy).
- **One section per file:** [Section Splitting](../classification.md#section-splitting).
- **Draft your document classes from sample files,** including multi-document packages:
  [Discovery](../discovery.md#multi-section-package-discovery).
- **Score how sure the model is of each document class:**
  [classification confidence](../configuration.md#classification-confidence-classificationcombinedllmconfidence),
  and [Confidence Assessment](confidence-assessment.md#document-class-confidence) for what we
  measured.
- **Fix a document's sections by hand:**
  [re-grouping pages](../web-ui.md#re-grouping-a-processed-documents-pages) in the Web UI.
- **Use BDA mode instead:** [classification in BDA mode](../classification.md#bda-mode-use_bda-true).
