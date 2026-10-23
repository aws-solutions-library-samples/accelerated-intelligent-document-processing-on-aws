---
title: "Customizing Classification"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Customizing Classification

Document classification is a key component of the GenAIIDP solution that categorizes each document or page into predefined classes. This guide explains how to customize classification to best suit your document processing needs.

## How the classification settings are organised

Classification does two jobs: it **splits** the file into documents, and it
**classifies** each one. **Strategy** decides whether those are one model call or
two, and every other setting sits inside the call that uses it, so a block the
selected strategy does not run is not shown in the configuration editor and has
no effect.

```yaml
classification:
  strategy: single_pass          # "One stage" | two_stage ("Two stage")
  fallback_class: unclassified   # assigned when no class can be decided

  one_stage:                     # strategy: single_pass
    granularity: page            # page | packet — how many calls are made
    page:   {contextPagesCount, system_prompt, task_prompt}
    packet: {window_pages, overlap_pages, system_prompt, task_prompt}
    llm:                         # the model settings of this call
      model: us.amazon.nova-2-lite-v1:0
      model_lambda_hook_arn      # only with model: LambdaHook
      temperature / top_p / top_k / max_tokens / reasoning_effort
      modality: {text, images}   # what this call is sent
      image: {target_width, target_height}
      validation: {enforceValidClasses, maxValidationRetries}
      confidence: {mode, top_k_candidates, ...}

  two_stage:                     # strategy: two_stage
    segmentation:                # where each document starts — no class list
      method: llm
      llm:
        granularity, page, packet, boundary_hints
        model settings           # no validation or confidence: it returns no class
    section_classification:      # one class per section, one call per section
      method: llm
      pages: first_k             # first | first_k | all
      k: 2
      llm:
        system_prompt, task_prompt
        model settings + validation + confidence

  file_shape:                    # Advanced — what you already know about a file
    sectionSplitting: llm_determined   # detect boundaries | disabled | page
    maxPagesForClassification: ALL     # only with sectionSplitting: disabled
```

The model settings are the same block in every call: a model, its parameters,
**modality** (send page text, send page images) and the image size. The two
calls that assign a class — one stage, and two-stage section classification —
add **validation** and **class confidence**. Segmentation has neither, because it
returns page ranges and never a class.

Two of those settings are substituted rather than used as written when they
cannot be. An empty `model` is not "no model" — every call needs a model id to
send — so it falls back to the default classification model, which is the same
`us.amazon.nova-2-lite-v1:0` the defaults put on all three calls, with a line in
the log. And a `reasoning_effort` outside the levels listed in the editor is
replaced with `medium`, also with a line in the log, because the value is
forwarded to the provider as written and a misspelled one either fails the
request or is silently ignored by a model family that has no effort control.

| Setting | Values | What it decides |
|---|---|---|
| `strategy` | `single_pass` (default) \| `two_stage` | **Whether splitting and classifying are one call or two.** `two_stage` finds the boundaries with no class vocabulary in the prompt, then classifies each section with one call. |
| `granularity` (per page-reading call) | `page` (default) \| `packet` | **How many calls are made.** `page`: one call per page, each returning that page's verdict, with the boundaries pieced together from those verdicts. `packet`: one call per document (or per window), with the model seeing the whole packet and returning the segments directly. One stage and segmentation each have their own. |
| `modality.text` / `modality.images` (per call) | `true` \| `false` \| empty | **What each call sees.** Empty means "infer from this call's task prompt placeholders". |
| `file_shape.sectionSplitting` | `llm_determined` (default) \| `disabled` \| `page` | **Whether boundaries are detected at all.** `disabled` treats the file as one document and `page` treats every page as one, so neither needs a boundary from the model. See [Section Splitting Strategies](#section-splitting-strategies). |
| `file_shape.maxPagesForClassification` | `ALL` (default) \| a positive number of pages | **How much of a one-document file is classified.** Only with `sectionSplitting: disabled`: the first N pages are classified and the whole file takes their majority class. See [Page limit configuration](#page-limit-configuration) below, which carries the spelling hazards. |

The settings combine freely. Two stage with segmentation at `packet` granularity
and page images on is one boundary call over the whole packet with its images,
followed by one section classification call per document found.

A stored configuration written for an earlier release, with every setting
directly under `classification:`, is converted to this layout automatically when
it is read; nothing needs editing.
[Migrating from v0.6.12 to v0.6.13](migration-v0612-to-v0613.md) lists what each
old setting becomes and the cases where the conversion is not a pure move.

### What each setting costs

- **`granularity: packet` saves the repeated prefix and the per-page output.**
  A page-level call re-sends the instructions, the class list, every class
  description and every few-shot example on every page, and asks for roughly
  150–350 output tokens of reasoning each time. A packet call pays all of that
  once.
- **It does *not* reduce image tokens.** A page's image costs the same whether
  it arrives in its own call or alongside nineteen others. So
  **`packet` + `modality.images: false` is the large cost lever**, and `packet`
  with images on is mainly an *accuracy* change — it is the only way the model
  ever sees the whole packet when deciding where documents start.
- **Images are usually the largest single input cost per page**, roughly
  1.1K–4.8K tokens depending on the model and the image size, against a few
  hundred output tokens. Each call's `image.target_width` / `target_height` and
  `modality.images: false` are the two levers on that.
- **`contextPagesCount` is the most expensive setting per unit of change.**
  Each call carries up to 2k+1 pages: ×3 at k=1 and ×5 at k=2, in both text and
  image tokens.
- **`strategy: two_stage` drops the class vocabulary from every boundary call
  and classifies once per document instead of once per page.** A clear win on
  long documents with few sections; roughly a wash on single-page files, where it
  adds a second call to save a prefix that was only paid once. Each stage can use
  its own model, so boundaries can run on a cheap model and classification on a
  stronger one.
- **A declared file shape skips the boundary work.** Under
  `sectionSplitting: disabled` or `page`, two stage makes no segmentation calls:
  the segments are known, so it goes straight to section classification.
- **The model choice outweighs all of it.** On the benchmark suite Nova 2 Lite,
  Claude Haiku 4.5 and Claude Sonnet 5 reached the same classification accuracy
  at $0.007, $0.047 and $0.189 per document respectively — see
  [`benchmarking/config-guidance.md`](./benchmarking/config-guidance.md) §5.3.
  With Sonnet 5 as the classifier, classification reached 67% of total document
  cost on a 9-page benchmark row.

### Combinations refused when you save

- `modality.text: false` **and** `modality.images: false` on any call — nothing
  left to classify.
- `ocr.backend: none` with `modality.images: false` on a call the selected
  strategy makes — no OCR text is produced and no images are sent, so
  classification would be working from the literal string
  `[No text content for page N]`. A packet call in that state would go out
  anyway and return a confident class derived from nothing; it fails instead,
  with a message naming both remedies.
- `packet.overlap_pages` greater than or equal to `packet.window_pages` — each
  window would cover no new pages, so a long packet would cost one call per page
  while claiming to be the cost-saving granularity.
- A model ID that is not a known Bedrock model, or a `max_tokens` above the
  model's output limit, on a call the selected strategy makes. Each is named by
  its path, for example `classification.two_stage.section_classification.llm.model`.

## Classification Methods Across Patterns

The solution supports multiple classification approaches that vary by pattern:

### Pattern 1: BDA-Based Classification

- Classification is performed by the BDA (Bedrock Data Automation) project configuration
- Uses BDA blueprints to define classification rules
- Not configurable inside the GenAIIDP solution itself
- Configuration happens at the BDA project level

### Pipeline Mode: Bedrock LLM-Based Classification

In Pipeline mode, one-stage classification works at one of two granularities:

#### Page granularity with sequence segmentation (default)

- Classifies each page independently using both text and image data
- **Uses sequence segmentation with BIO-like tagging for document boundary detection**
- **Each page receives both a document type and a boundary indicator ("start" or "continue")**
- **Automatically segments multi-document packets where multiple documents may be combined**
- Works exceptionally well for complex document packets containing multiple documents of the same or different types
- Supports optional few-shot examples to improve classification accuracy
- Selected with `classification.one_stage.granularity: page`, the default
- See the [few-shot-examples.md](./few-shot-examples.md) documentation for details on configuring examples

##### Sequence Segmentation Approach

Page granularity implements a sophisticated sequence segmentation approach similar to BIO (Begin-Inside-Outside) tagging commonly used in NLP. This enables accurate segmentation of multi-document packets where a single file may contain multiple distinct documents.

**How It Works:**

Each page receives two pieces of information during classification:
1. **Document Type**: The classification label (e.g., "invoice", "letter", "financial_statement")
2. **Document Boundary**: A boundary indicator that signals document transitions:
   - `"start"`: Indicates the beginning of a new document (similar to "Begin" in BIO)
   - `"continue"`: Indicates continuation of the current document (similar to "Inside" in BIO)

**Benefits of Sequence Segmentation:**

- **Multi-Document Packet Support**: Accurately segments packets containing multiple documents
- **Type-Aware Boundaries**: Detects when a new document of the same type begins
- **Automatic Section Creation**: Pages are grouped into sections based on both type and boundaries
- **Improved Accuracy**: Context-aware classification that considers document flow
- **No Manual Splitting Required**: Eliminates the need to manually separate documents before processing

**Example Segmentation:**

Consider a packet with 6 pages containing two invoices and one letter:

```
Page 1: type="invoice", boundary="start"      → Section 1 (Invoice #1)
Page 2: type="invoice", boundary="continue"   → Section 1 (Invoice #1)
Page 3: type="letter", boundary="start"       → Section 2 (Letter)
Page 4: type="letter", boundary="continue"    → Section 2 (Letter)
Page 5: type="invoice", boundary="start"      → Section 3 (Invoice #2)
Page 6: type="invoice", boundary="continue"   → Section 3 (Invoice #2)
```

The system automatically creates three sections, properly separating the two invoices despite them having the same document type.

##### Page Context for Classification

Page granularity supports including surrounding pages as context to improve classification accuracy. This is particularly useful when a single page doesn't contain enough information to determine its document type or boundary status.

**Configuration:**

```yaml
classification:
  one_stage:
    granularity: page
    page:
      contextPagesCount: 1  # Include 1 page before and 1 page after as context
      # contextPagesCount: 0  # Default: no additional context
      # contextPagesCount: 2  # Include 2 pages before and 2 pages after
```

Two-stage segmentation at page granularity has the same setting at
`classification.two_stage.segmentation.llm.page.contextPagesCount`.

**How It Works:**

When `contextPagesCount` is set to a value greater than 0, the classification prompt includes surrounding pages as additional context:

- **`contextPagesCount: 1`**: Includes 1 page before and 1 page after the target page
- **`contextPagesCount: 2`**: Includes 2 pages before and 2 pages after the target page
- **Edge handling**: At document boundaries, only available pages are included (e.g., first page has no "before" pages)

**Enhanced Prompt Structure:**

The system replaces the standard `{DOCUMENT_TEXT}` and `{DOCUMENT_IMAGE}` placeholders with context-aware versions that clearly separate context pages from the page being classified:

**Text Context Structure:**
```xml
For context, here is the OCR text for the page(s) immediately prior to the page you should classify:
<context-pages-before>
[OCR text from all context pages before - combined if multiple pages]
</context-pages-before>

Here is the OCR text for the page to classify:
<current-page>
[OCR text for the page being classified]
</current-page>

For context, here is the OCR text for the page(s) immediately after the page you should classify:
<context-pages-after>
[OCR text from all context pages after - combined if multiple pages]
</context-pages-after>
```

**Image Context Structure:**
```
For context, here are the image(s) for the page(s) immediately prior to the page you should classify:
[Image 1 - context page before]
[Image 2 - context page before (if contextPagesCount >= 2)]

Here is the image for the page to classify:
[Image - current page being classified]

For context, here are the image(s) for the page(s) immediately after the page you should classify:
[Image 1 - context page after]
[Image 2 - context page after (if contextPagesCount >= 2)]
```

**Note:** Context pages are combined within their respective sections (before or after). The structure uses descriptive text labels and XML tags (`<context-pages-before>`, `<current-page>`, `<context-pages-after>`) to clearly indicate which content is for context versus which content should be classified.

**Benefits:**

- **Improved Boundary Detection**: Context helps the LLM identify document transitions
- **Better Classification Accuracy**: Surrounding pages provide additional clues
- **Handles Ambiguous Pages**: Pages that look similar can be distinguished by context
- **Flexible Configuration**: Adjust context size based on document complexity

**Use Cases:**

- Documents where headers/footers span multiple pages
- Multi-page forms where individual pages look similar
- Document packages with varying page layouts
- Cases where LLM boundary detection has been unreliable

**Considerations:**

- Increases token usage proportionally to the number of context pages
- May increase latency due to larger prompts
- Works best when surrounding pages provide meaningful classification hints

**Configuration for Boundary Detection:**

The boundary detection is automatically included in the classification results. No special configuration is needed - the system will populate the `document_boundary` field in the metadata for each page:

```json
{
  "page_id": "1",
  "classification": {
    "doc_type": "invoice",
    "confidence": 0.95,
    "metadata": {
      "document_boundary": "start",  // New document begins
      "classification_reason": "Invoice number and remittance block present"
    }
  }
}
```

`confidence` and `classification_reason` appear only when the model returned
them — see [Classification Confidence](#classification-confidence) below.

The value is also **persisted** on the page record and exposed on the API, so an
unexpected split or merge can be inspected after the fact instead of re-derived
from Lambda logs.

###### How the model is asked to decide it

Each page is classified in an **independent** LLM call that sees only that page
(plus any neighbours from `contextPagesCount`) and is never told its position in
the packet. So "does this continue the previous document?" is a question the model
often cannot answer from what it was given — and the prompt used to ask exactly
that. Failing runs said so in their own reasoning: *"Since no prior page 1 has been
established in this sequence, this is treated as the start of the document."* The
result was intermittent over-splitting of multi-page documents
([#653](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/653)).

The one-stage page prompt, `classification.one_stage.page.task_prompt`, carries a `<boundary-detection-rules>` block that
asks a question a single page **can** answer — is this page a *first* page? — from
its own evidence, in priority order: pagination (`Page 2 of 2` ⇒ `continue`), then
the presence of an opening identity block, then **table continuation** — a page of
table rows whose column headings repeat at the top is a continuation page, because
repeated column headings are not a document title — then other continuation
evidence. The table-continuation rule was added for
[#726](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/726):
on a single-class config the classifier called the second or third page of a
three-page bank statement `start` in about half of the runs, because the reprinted
`Date | Description | Amount` heading row looked like a heading. Measured with the
real OCR and classification services (Nova 2 Lite, `temperature 0`) on the
benchmark fixtures: the unpaginated 3-page statement went from **8/15** to
**15/15** correct section counts, with the two-documents (5/5) and paginated (5/5)
fixtures unchanged. Confirmed end to end on a deployed stack with a same-stack A/B
(`boundaryab` suite, the frozen v0.6.7 prompt as the control, 5 repeats): the
unpaginated statement scored **1/5** under the v0.6.7 prompt and **5/5** under the
new rules, the two-documents and paginated fixtures 5/5 under both, and the
running-header fixture 0/5 under both. Note that an upgrade delivers the new rules to `Config#default`
(and to a `CustomConfigPath` config, which is re-resolved from S3) but **not** to a
configuration profile you saved yourself — a saved profile is an independent snapshot
that keeps whatever page task prompt it was saved with. Re-apply the block or
reset the prompt to the default in such a profile. Two clauses in the block are a matched
pair and must not be removed independently:

| Clause | Prevents |
|---|---|
| *no preceding page shown* — absence of page 1 in the input says nothing about the document | **over-splitting** (every page reported as `start`) |
| *consecutive documents of the same type* — a genuine first page is `start` even when the previous page has the same class | **over-merging** (back-to-back copies of one form collapsing into one section) |

The block sits **before `<<CACHEPOINT>>`**, so it is part of the cacheable prefix
and is not re-billed per page.

> `contextPagesCount: 1` is not a substitute. It fixes the simple two-page case but
> biases the model toward `continue`, which then merges genuinely separate
> back-to-back documents — trading over-splitting for over-merging. Measured on a
> 4-page packet holding two copies of one form, `contextPagesCount: 1` **on its own
> scores 0/5** — it merges all four pages. It remains the right answer for one case
> the rules cannot cover: **pages scanned out of order**, where a page has no way to
> know it is physically second.

###### Measured effect

On **DocSplit-Poly-Seq** — 500 packets, 7,330 pages, 2,027 sections, 5,000
packet-runs — split accuracy on multi-section packets, rules vs. the prior prompt:

| Model | Δ split accuracy | |
|---|---|---|
| Qwen3-VL | +0.117 | p<0.05 |
| Claude Opus 5 | +0.040 | p<0.05 |
| Amazon Nova 2 Lite (default) | +0.030 | p<0.05 |
| Claude Sonnet 5 | +0.013 | p<0.05 |
| gpt-5.6-sol | +0.004 | not significant |

Paired bootstrap + Wilcoxon; **no model regresses**. Under-split rate is 0.000 in
all ten cells, so the over-merge guard holds. Page-level *class* accuracy moves at
most 0.015 — the rules affect boundaries only, not classification. On #653's
reported two-page form, Sonnet 5 goes from 6/24 to 10/10.

Two limits worth knowing before you rely on this:

- **It leans on pagination markers.** Corpora whose scans mostly lack them (for
  example RVL-CDIP) see much less benefit — the header-block and continuation
  heuristics are softer signals than `Page 2 of 3`.
- **`llm_determined` over-splits 1.5×–2.3× on real-world packets regardless of
  prompt.** That is a separate and larger problem than the one these rules fix; the
  rules narrow the gap, they do not close it. If exact section counts matter to you,
  measure on your own documents.

###### Known failure mode: repeating running headers

The rules are wrong on one common document shape, and the fix is a per-class
setting rather than a change to the rules
([#750](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/750)).

A long table — a fund/distribution schedule, a brokerage or transaction
statement — typically reprints its **title, logo and column headers on every
page** and paginates with a **bare page number** (`7`), not `Page 7 of 17`. That
combination defeats the priority order above:

- rules 1–2 never fire, because a lone `7` matches none of the pagination
  patterns the model is given;
- rule 3 (opening header block ⇒ `start`) is evaluated **before** rules 4–5
  (table / other continuation evidence ⇒ `continue`), and the repeated running
  header satisfies it — especially when the reprinted column headers themselves
  carry a date (`NAV as of 10/31/2024`), which rule 3 lists as page-1 evidence.
  Rule 4 (added for #726) only clears a page whose repeated content is the table's
  **column headings**; a reprinted **title and account block** still reads as an
  opening block, so a synthetic 3-page statement with that running header and no
  pagination scores 0/5 before and after the #726 change, on Nova 2 Lite and on
  Claude Haiku 4.5 alike;
- `contextPagesCount: 0` (the default) means the continuation rules have no preceding
  page to compare against — rule 4 judges the page's own table rows and rule 5's
  *"running balance or subtotal carried forward"* is a within-page clue.

The model says so in its own reasoning: *"The page number '15' at the top right
indicates this is part of a multi-page document, **but the presence of the full
title** and structured data table **confirms this is the start** of the
document's content."*

It is **silent**: each fragment is still 100% complete and the document still
reaches `COMPLETED`. Only the section count changes — so a downstream consumer
that reads one section sees a truncated list and blames extraction.

**The supported fix is the `PRECEDENCE` clause: put the boundary rule in the
class description.** A document type's own boundary instructions override the
generic rules, which is exactly what this shape needs:

```yaml
classes:
  - x-aws-idp-document-type: AnnualDistributions
    description: >-
      Annual taxable distributions schedule.
      BOUNDARY: this is ONE continuous multi-page document - a single table that
      runs to the disclosures page. Every page reprints the title, logo and table
      column headers and carries only a bare page number, so only the page
      numbered 1 is "start"; every other page is "continue".
```

Name `"start"` and `"continue"` explicitly — those are the values
`document_boundary` takes, and a paraphrase leaves the mapping to the model.

**Why this is not fixed in the shared prompt.** Every generic lever was measured
against the three boundary fixtures in `benchmarks/matrices/doc_matrix.yaml`
(Nova 2 Lite, `temperature: 0`, 10 runs per cell, scored against exact ground
truth). Each one buys the running-header case at the cost of a case #653 already
balances:

| prompt / setting | 16-page running-header table (want 1) | `small_narrow` — 3-page statement, no pagination (want 1) | `paginated_3pg` (want 1) | `twodocs_2x20` — two forms back to back (want 2) |
|---|---|---|---|---|
| v0.6.7 rules (#653, before the #726 table-continuation rule) | **0/10** | 7/10 | 10/10 | 10/10 |
| + *"a running header is not an opening block"* | — | 4/10 ↓ | 10/10 | 10/10 |
| + *"a bare page number > 1 is decisive"* | 10/10 | 2/10 ↓ | 10/10 | 10/10 |
| + both | 10/10 | 0/10 ↓ | 10/10 | 10/10 |
| `contextPagesCount: 1`, rules unchanged | 10/10 | 10/10 | 10/10 | **5/10 ↓** |
| **class-description `BOUNDARY:` sentence** | **10/10** | unaffected | unaffected | unaffected |

Re-measured for #726 (5 runs per cell, real services): `contextPagesCount: 1` fixes
the running-header statement (4/5) and the unpaginated one (5/5) but scores **0/5** on
`twodocs_2x20` with the shipped rules and with the #726 rules alike — the
over-merge trade is not a prompt-wording artefact.

`contextPagesCount: 1` is the right lever if your corpus contains no back-to-back
copies of the same form — it fixes both over-split directions with the shipped
prompt, at the cost of the over-merge direction and roughly 3× the image tokens
per classification call.

**`granularity: packet` is the other lever, and on this shape it works.** A
3-page statement with the identity header reprinted on every page returned 3
sections at page granularity and **1** — correct — at packet granularity, under
`single_pass` and `two_stage` alike. That is the expected direction: the rules
above fail because a page judged alone has nothing to compare its header
against, and a packet call sees every page at once. The cost is the mirror-image
error, measured on the same pair of fixtures: at packet granularity two 3-page
statements filed back to back merged into one section where page granularity got
them right. `boundary_hints: class_names` was also measured on the over-split
case and left it at 3 sections. See [Neither granularity dominates, and the
failure modes are
opposite](#neither-granularity-dominates-and-the-failure-modes-are-opposite) for
the pair. So on a corpus of long single documents, packet granularity is a
genuine fix for this shape; on a mixed corpus the class-description `BOUNDARY:`
sentence remains the only lever measured not to cost anything elsewhere.

Two notes on reading that table next to the *Measured effect* numbers above:

- It is **Nova 2 Lite**, the shipped default and the model on which the rules have
  the most headroom to move. `small_narrow` at 7/10 for the shipped rules is
  consistent with the independent 0/5 → 3/5 on the same unpaginated shape. The
  Sonnet-5 factorial in
  `benchmarks/results/v0.6.7/boundary-factorial/FINDINGS.md` scores 1.00 on all
  three fixtures for **both** prompts — Sonnet 5's true effect is +0.013, which
  three synthetic documents at n=5 cannot resolve, so that null says nothing about
  this failure mode either way.
- The running-header column is **not** one of the synthetic fixtures. A synthetic
  reproduction was attempted (running header + as-of date + bare page number, at 3
  and 12 pages) and scored 10/10 correct, i.e. it does **not** reproduce; the
  trigger needs more of the real document than the generator emits. The 0/10 is
  measured on `samples/Nuveen.pdf` itself, and CI Step 8 is the standing guard.

⚠️ **A stored or preset `classification.one_stage.page.task_prompt` overrides the
default, so it does not get these rules.** If you customized the prompt, re-apply the block or
reset to the default. The presets shipped in `config_library/` are kept in sync by
`scripts/tests/test_classification_prompt_copies_in_sync.py`.

##### Enforcing a Valid Class Vocabulary (Validation + Retry)

With a fixed set of classes, a language model can occasionally return a label
that is **not** in your configured list (for example predicting `receipt` when
only `invoice`, `w2`, and `check` are valid). Smaller / cheaper models are
especially prone to this. Every call that assigns a class — one stage at page or
packet granularity, and two-stage section classification — includes a
deterministic validation + retry guardrail to prevent out-of-vocabulary
classifications:

1. After the model returns a class, it is validated against the configured
   class vocabulary.
2. If the class is **not** valid, the model is **re-prompted** — the original
   request content is re-sent with an appended correction message that lists
   the allowed classes. (This matters: on a model that honors it, classification
   runs at `temperature=0`, so re-sending an identical request would return the
   identical invalid answer. The correction changes the input and steers the
   model back to the allowed set.)

   > **`temperature=0` is not universal.** Claude Opus 4.7/4.8, Opus 5, Opus 5.5
   > and Sonnet 5 **reject** `temperature`/`top_p`/`top_k` (HTTP 400), so
   > `idp_common` strips them before the request (`_CLAUDE_4_7_BASE_NAMES` in
   > `idp_common/bedrock/client.py`). On those models classification **samples**,
   > and identical inputs can return different answers run to run. Assuming the
   > parameter applies everywhere is what makes [#653](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/653)
   > — intermittent page-boundary misdetection — look like a model defect rather
   > than a prompt one.
3. The retry repeats up to `maxValidationRetries` times.
4. If all retries are exhausted, the page is assigned `fallback_class`
   (default `unclassified`) and a warning-severity
   `classification_invalid_class_fallback` processing issue is recorded on the
   section that holds the page, naming the class the model returned. The document
   continues processing — there is no hard failure, but the stored class is not the
   model's answer and the section says so. See [Pages classification could not
   classify](#pages-classification-could-not-classify).

```yaml
classification:
  fallback_class: unclassified        # Class used when retries are exhausted (default: unclassified)
  one_stage:
    llm:
      validation:
        enforceValidClasses: true     # Validate + retry on invalid class (default: true)
        maxValidationRetries: 2       # Re-prompt up to N times (default: 2)
  two_stage:
    section_classification:
      llm:
        validation:                   # the same block, for two-stage section classification
          enforceValidClasses: true
          maxValidationRetries: 2
```

| Setting | Default | Description |
|---------|---------|-------------|
| `validation.enforceValidClasses` | `true` | When `true`, validate the predicted class and re-prompt on out-of-vocabulary results. When `false`, an invalid class is logged and used as-is. |
| `validation.maxValidationRetries` | `2` | Maximum number of re-prompts. `0` disables retries (a single invalid prediction goes straight to the fallback). |
| `fallback_class` | `unclassified` | Class assigned when retries are exhausted. Set to one of your defined classes, or the built-in `unclassified`. One setting for the whole classification step; extraction reads it too. |

> **Catch-all class:** if you want an explicit "other"/"unknown" bucket, define
> it as one of your classes — the model will then be able to select it
> legitimately, and it counts as a valid prediction.
>
> **Packet granularity:** a reply that names an invalid class for any segment
> re-prompts the whole window, and a segment still invalid after the retries is
> given the fallback class.

A runnable demonstration (forcing an out-of-vocabulary prediction and showing
the retry correct it) is available in
`notebooks/misc/classification-valid-class-enforcement.ipynb`.

#### Packet granularity: one call per packet

- Analyzes entire document packets to identify logical boundaries
- Identifies distinct document segments within multi-page documents
- Determines document type for each segment
- Better suited for multi-document packets where context spans multiple pages
- Selected with `classification.one_stage.granularity: packet`

The packet task prompt lives in
`lib/idp_common_pkg/idp_common/config/system_defaults/base-classification.yaml`
under `classification.one_stage.packet.task_prompt`, and the configuration panel
exposes it under *Packet settings* once the one-stage granularity is `packet`. It
instructs the model to:

1. Read through the entire document package to understand its contents
2. Identify page ranges that form complete, distinct documents
3. Match each document segment to one of the defined document types
4. Record the start and end pages for each identified segment

⚠️ **Write a packet prompt under `one_stage.packet.task_prompt`, not
`one_stage.page.task_prompt`.** The two are asked for different output shapes:
the page prompt returns one class and a boundary verdict, the packet prompt a
`segments` array, and each granularity reads only its own.

Example configuration:

```yaml
classification:
  one_stage:
    granularity: packet
    llm:
      model: us.amazon.nova-pro-v1:0
    packet:
      task_prompt: >-
        <task-description>
        You are a document classification system. Your task is to analyze a document package 
        containing multiple pages and identify distinct document segments, classifying each 
        segment according to the predefined document types provided below.
        </task-description>

        <document-types>
        {CLASS_NAMES_AND_DESCRIPTIONS}
        </document-types>

        <document-boundary-rules>
        Rules for determining document boundaries:
        - Content continuity: Pages with continuing paragraphs, numbered sections, or ongoing narratives belong to the same document
        - Visual consistency: Similar layouts, headers, footers, and styling indicate pages belong together
        - Logical structure: Documents typically have clear beginning, middle, and end sections
        - New document indicators: Title pages, cover sheets, or significantly different subject matter signal a new document
        </document-boundary-rules>

        <<CACHEPOINT>>

        <document-text>
        {DOCUMENT_TEXT}
        </document-text>
```

##### Packet granularity: images, few-shot examples and windowing

**Page images.** Every page's image is attached, each labelled with its ordinal
in the call so the model can say which image it is looking at — layout is most
of what tells you where a document starts. Subject to the call's
`modality.images`.

**Few-shot examples.** `{FEW_SHOT_EXAMPLES}` is filled from each class's
`x-aws-idp-examples`, the same way the page-level path fills it.

> ⚠️ **In a two-stage *segmentation* prompt this placeholder renders empty, at
> either granularity.** Sending no examples is part of what makes the boundary
> call cheap, so a segmentation template that references `{FEW_SHOT_EXAMPLES}`
> gets nothing rather than an error. See
> [which placeholders a stage is allowed to render](../lib/idp_common_pkg/idp_common/classification/README.md#which-placeholders-a-stage-is-allowed-to-render).

**Windowing.** A packet larger than one window is split into overlapping windows
whose results are merged:

```yaml
classification:
  one_stage:
    granularity: packet
    packet:
      window_pages: "20"    # 0 = never window: a packet too large for the model fails
      overlap_pages: "2"
```

Two-stage segmentation at packet granularity has the same window settings at
`classification.two_stage.segmentation.llm.packet`.

The default of 20 is where Bedrock starts downscaling: above 20 image blocks in
one request every image is shrunk to 2,000px, so a wider window with images on
silently halves its own page resolution.

The overlap exists so the window *after* a junction can see the pages before its
own first page — a boundary landing on a window's first page would otherwise
have to be judged with no preceding context, which is precisely the weakness
packet granularity exists to fix. The overlapping pages' own verdicts are taken
from the **earlier** window, which is the one that had their preceding context.
Windows are merged by projecting each window's segments onto per-page verdicts
and re-running the same fold page-level classification uses, so a windowed packet
and a page-level document cannot disagree about how verdicts become segments.
A single window (the common case) uses the model's segments directly.

One window failing does not discard the others, so a transient throttle on a long
packet costs that window's boundaries rather than the whole document's.

**How to pick the two numbers.**

- Keep `window_pages` at **20 or below** whenever images are on, so a request
  stays under the 20-image-block threshold above which Bedrock downscales every
  image. Raising it to get fewer calls costs page resolution on every call.
- Keep it **no smaller than your longest single document**. A document that
  straddles two windows has its boundary judged twice from two partial views,
  which is the main way windowing loses accuracy. If your longest document is
  longer than 20 pages and images matter, prefer page granularity over a wide
  window.
- `0` means never window: one call for the whole packet, which fails outright
  above the model's context window rather than degrading. Use it when you need
  the old single-call behaviour and your packets are bounded.
- `overlap_pages` must be **strictly smaller** than `window_pages` or the save is
  refused. It should cover the longest run of continuation pages you expect
  before a boundary cue appears — 2 is enough when first pages are clearly
  marked, more when a document opens with a cover sheet or a table that runs on.
  Each additional window costs `overlap_pages` extra pages of input, so the cost
  is roughly `overlap / (window - overlap)` on top of the un-overlapped page
  count.

The number of calls over N pages is 1 when `window_pages` is 0 or N fits in one
window, and otherwise `ceil((N - window_pages) / (window_pages - overlap_pages))
+ 1`.

##### Limitations of packet-level classification

Despite its strengths in handling full-document context, this granularity has several limitations:

**Context & Model Constraints:**: 
- Long documents can exceed the context window of smaller models. Set the call's `packet.window_pages` to split them; with `window_pages: 0` the request fails outright.
- Lengthy inputs may dilute the model’s focus, leading to inaccurate or inconsistent classifications.
- Requires high-context models. A 1-million-token input window is reached two different ways, and only one of them is a `:1m` id. The extended-context Claude variants — `us.anthropic.claude-sonnet-4-6:1m`, `us.anthropic.claude-sonnet-5:1m` and the `:1m` Opus variants — **opt into** it; `us.anthropic.claude-sonnet-5-5` and `us.anthropic.claude-haiku-5-5` have it **by default** and have no `:1m` id at all, so looking for one rules out two models that already qualify. Every other base Claude 4.x/5 model carries 200K and the Nova family ~300K. Smaller models are not suitable for this method. `config_library/model_config_limits.yaml` is the authority for which id gets which window.
- For more details on supported models and their context limits, refer to the [Amazon Bedrock Supported Models documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/models-supported.html).

**Scalability Challenges**: Not ideal for very large or visually complex document sets. In such cases, page granularity is more appropriate.

### Pattern 3: UDOP-Based Classification

- Classification is performed by a pre-trained UDOP (Unified Document Processing) model
- Model is deployed on Amazon SageMaker
- Performs multi-modal page-level classification (classifies each page based on OCR data and page image)
- Not configurable inside the GenAIIDP solution

## Section Splitting Strategies

`classification.file_shape.sectionSplitting` controls how classified pages are grouped into document sections. It applies to both strategies and both granularities, and the configuration editor shows it under *Advanced — known file shape* with the labels *Detect boundaries (default)*, *One document* and *One per page*. It has three values:

### Available Strategies

#### 1. `disabled` - No Splitting (Entire Document = One Section)

**Behavior:**
- All pages are assigned to a single section
- Uses a **page-weighted vote across the segments that were detected** to determine the document class: each segment casts as many votes as it has pages, and the class with the most votes wins
- Excludes segments whose class is not one of the configured classes, so unclassifiable and blank pages do not affect the result
- If there's a tie, uses the class of the earliest segment carrying a tied class, for determinism
- Ignores the detected boundaries when building sections — it still reads them to weight the vote
- With a **single-class configuration**, sends no page to the model: every page is given that class with confidence `1.0`. Under `llm_determined`, a multi-page file in such a configuration still goes to the model for its section boundaries: page by page at page granularity, and in one call for the whole file at packet granularity
- Under two stage, makes **no segmentation call**: the whole file is one segment, given its class by one section classification call

**Use Cases:**
- Documents known to be single-type with no internal divisions
- Simplified processing where granular section splitting isn't needed
- When you want to force all pages to be treated as one cohesive document
- **Documents with occasional blank or unclassifiable pages** (these won't affect the final classification)

**Configuration Example:**
```yaml
classification:
  file_shape:
    sectionSplitting: disabled
```

**Result:**
- Document with 10 pages → 1 section containing all 10 pages
- All pages assigned the most common (voted) class

**Voting Behavior:**

The `disabled` strategy votes on the document classification, which provides
robust handling of edge cases. The same rule runs at **both** granularities and
under both strategies — what differs is only how the segments it counts were
produced:

1. **Config-Driven Voting**: Only segments whose class matches a document type defined in your configuration are eligible to vote. This automatically excludes:
   - Blank pages (`unclassifiable_blank_page`, `blank`, etc.)
   - Error states (`error (backoff/retry)`, `unclassified`)
   - LLM hallucinations or typos that don't match any defined class

2. **Pages, Not Segments, Are The Weight**: each eligible segment contributes one vote per page it covers, so a 5-page segment outweighs a 1-page one. The class with the most page-votes becomes the document classification.

3. **Tie-Breaking**: If two classes have the same weight, the class of the earliest segment carrying a tied class is used for determinism.

4. **Fallback**: If no segment carries a configured class, the first segment's class is used, with a warning in the log.

⚠️ **One disagreeing page leaves the section unscored.** A page whose own
verdict was a class other than the winner has its confidence cleared, and a
section's confidence is the aggregate over all its pages, which is `None` as soon
as any page is unscored. A page that produced no verdict of its own inherits its
segment's class *and* confidence, so silence does not count as disagreement —
see [Not scored vs. scored 1.0](#not-scored-vs-scored-10).

**Example:**
```
6-page document with classifications:
- Page 1: DRILLING_PLAN_GEOLOGIC
- Page 2: DRILLING_PLAN_GEOLOGIC  
- Page 3: DRILLING_PLAN_GEOLOGIC
- Page 4: DRILLING_PLAN_GEOLOGIC
- Page 5: DRILLING_PLAN_GEOLOGIC
- Page 6: unclassifiable_blank_page (excluded from voting)

Voting result: DRILLING_PLAN_GEOLOGIC (5 votes)
→ Entire document classified as DRILLING_PLAN_GEOLOGIC
```

**GitHub Issue Reference:**
This voting behavior addresses [Issue #167](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/167) where documents with blank last pages were incorrectly classified as the blank page type.

#### 2. `page` - Per-Page Splitting (Each Page = Own Section)

**Behavior:**
- Every page becomes an independent section
- Each page keeps its individually classified document type
- **Prevents automatic joining of same-type documents**
- Under two stage, makes **no segmentation call**: each page is its own segment, given its class by its own section classification call

**Use Cases:**
- **Critical for long documents with multiple same-type forms** (e.g., multiple W-2 forms, multiple invoices)
- When LLM boundary detection is unreliable or fails frequently
- Government form processing where each form must be processed independently
- Scenarios where deterministic splitting is required

**Configuration Example:**
```yaml
classification:
  file_shape:
    sectionSplitting: page
```

**Result:**
- Document with 10 pages → 10 sections (one per page)
- Each page maintains its individual classification

**GitHub Issue Reference:**
This strategy directly addresses [Issue #146](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/146) where long documents with multiple same-type forms were being incorrectly joined together.

#### 3. `llm_determined` - LLM Boundary Detection (Default)

**Behavior:**
- Uses "Start"/"Continue" boundary indicators from LLM responses
- Automatically groups related pages into logical sections
- Implements BIO-like tagging for sophisticated document segmentation

**Use Cases:**
- Complex multi-document packets requiring intelligent boundary detection
- When LLM boundary detection works reliably
- Default behavior that works well for most use cases

**Configuration Example:**
```yaml
classification:
  file_shape:
    sectionSplitting: llm_determined  # This is the default
```

**Result:**
- Document with 10 pages → Variable number of sections based on LLM boundary detection
- Pages grouped according to document boundaries and type changes

### Strategy Comparison Table

| Strategy | Sections Created | Boundary Detection | Same-Type Handling | Deterministic | Performance |
|----------|-----------------|-------------------|-------------------|---------------|-------------|
| `disabled` | 1 section always | None — asserted | All joined | Yes | Fastest |
| `page` | N sections (N pages) | None — asserted | Never joined | Yes | Fast |
| `llm_determined` | Variable | LLM boundaries | May join | No | Standard |

### What decides the choice

⚠️ **`disabled` and `page` are assertions, not preferences.** They do not ask
the model to lean one way; they replace boundary detection with a claim about the
file, and nothing downstream can recover from the claim being wrong. A packet of
eight documents declared `disabled` returns one section, with one extraction over
eight documents' worth of pages. That is why these two sit under `file_shape` —
*what you already know about this file* — rather than beside the strategy.

So the choice is really a question about your input, not about accuracy:

- **You know every file holds exactly one document** → `disabled`. This is the
  common case for a single-document intake pipeline, and it is the only value
  that enables `maxPagesForClassification`.
- **You know every file is a run of one-page documents**, or a packet holds
  same-type forms back to back and you would rather over-split than merge them →
  `page`. This is the deterministic answer to the multiple-W-2 problem below.
- **You do not know, or files vary** → `llm_determined`, the default. Pay for the
  boundary decision.

Three further properties worth knowing:

- **`disabled` and `page` are the only values that let classification skip the
  model entirely.** With a single-class configuration, or a document-name regex
  match that already fixes the class, there is nothing left to decide and no
  call is made. Under `llm_determined` the model is still called in both of those
  cases, on purpose: the class may be known but the boundaries are not, and
  silently skipping the call would collapse every multi-document packet into one
  section.
- **`file_shape` applies identically under both strategies.** Under `two_stage`
  a declared shape skips the segmentation call outright, leaving one section
  classification call per asserted section.
- **`two_stage` with `sectionSplitting: page` is strictly dominated.** It makes
  one classification call per page with no boundary pass, which on a measured
  3-page file cost the same requests and essentially the same input tokens as
  `single_pass` at page granularity (3 calls, 1,545 against 1,527 input tokens) —
  the same price for a configuration that cannot return a per-page boundary
  verdict. If you want one section per page, `single_pass` is the simpler
  spelling.

### Configuration Placement

`sectionSplitting` sits under `file_shape`, beside the page limit that only
applies to a one-document file:

```yaml
classification:
  file_shape:
    sectionSplitting: disabled         # Options: llm_determined (default), disabled, page
    maxPagesForClassification: "3"     # Only with disabled: classify the first 3 pages
```

Both are assertions about the file, so both are checked when you save.
`sectionSplitting` accepts only those three values — an unrecognised one is
**refused**, naming the setting, rather than quietly becoming `llm_determined`,
which is the expensive value and changes where sections start. Clearing the field
takes the default silently, as clearing any other setting does.

⚠️ The same check runs on **deploy**, not only on save. A `CustomConfigPath`
file carrying an unrecognised `sectionSplitting` fails the stack operation that
applies it — which on an update means a rollback, so correct the value before
deploying rather than after. The same is true of every classification setting
that takes a fixed set of values, not just this one. No shipped preset or
configuration can carry a bad one: the editor renders them as picklists, and
every value in `config_library/` is on-list.

`maxPagesForClassification` accepts `ALL` or a positive number of pages;
anything else — a word, a negative number, an unquoted YAML `true` — is reported
and the file is classified in full, because that is what the pipeline does with a
limit it cannot read, and a stored value saying otherwise only looks like a
setting that is in force.

### Interaction with the strategies

**One stage, page granularity:**
- `disabled`: All pages in one section, with the class most pages were given (see Voting Behavior above)
- `page`: Each page's individual classification preserved in separate sections
- `llm_determined`: Pages grouped by class + boundary verdicts

**One stage, packet granularity:**
- `disabled`: All pages in one section, with the class most pages were given
- `page`: Each page gets its own section, with the class of the segment it fell in
- `llm_determined`: The model's segments used as sections (default behavior)

**Two stage:**
- `disabled`: No segmentation call; one section classification call over the file
- `page`: No segmentation call; one section classification call per page
- `llm_determined`: Segmentation finds the segments, then one section classification call each

### Real-World Example: Multiple W-2 Forms

Consider a 6-page document containing three W-2 forms (2 pages each):

**With `sectionSplitting: llm_determined` (may work or may fail):**
```
Result depends on LLM boundary detection accuracy
Best case: 3 sections (one per W-2)
Worst case: 1 section (all W-2s incorrectly joined)
```

**With `sectionSplitting: page` (deterministic solution):**
```
Page 1 → Section 1 (W-2)
Page 2 → Section 2 (W-2)
Page 3 → Section 3 (W-2)
Page 4 → Section 4 (W-2)
Page 5 → Section 5 (W-2)
Page 6 → Section 6 (W-2)

Result: 6 independent sections
Each W-2 page processed separately
No risk of incorrect joining
```

**With `sectionSplitting: disabled` (simplest case):**
```
All 6 pages → Section 1 (W-2)

Result: Single section
Entire document treated as one unit
```

## Pages Classification Could Not Classify

A page can come out of classification without a usable class. When that happens
the page is labelled `unclassified` (or `fallback_class`, which defaults to
the same), and because no class of that name exists in configuration it has no
extraction schema — so extraction skips the model for its section and the section
completes holding no data. A document in that state still completes normally, and
that is the right behaviour: one unreadable page should not discard the rest of a
packet.

What it must not do is complete without saying so. Classification records a
processing issue on the section that holds the page, which is where the Sections
panel renders it and where `ProcessingIssueCount` on the document counts it. The
severity says how much attention the case deserves:

> **Scope: one stage at page granularity only.** These issues are recorded from
> the page-level path. Packet granularity and two stage make one decision per
> segment rather than per page and record none of them; at packet granularity a
> page the model places in no segment is appended to no section at all, so it
> produces neither data nor a record. Extending this to the segment-level paths is
> tracked as follow-up work.

| Issue code | Severity | What happened | What to do |
|---|---|---|---|
| `classification_failed` | error | A classification attempt errored, exhausted its retries, or the page's required OCR artifacts were absent, so the page has no class and nothing was extracted from it. **Reached on the SageMaker/UDOP backend.** On the Bedrock backend an exception propagates instead and fails the document, where the error appears in the execution history rather than as an issue | Check the ClassificationFunction log group for the page, and endpoint health, model access and quota |
| `classification_page_no_content` | warning | The page had neither usable OCR text nor a loadable page image, so there was nothing to classify. Note this needs *both* to be absent — a blank page normally still has an image, so it does not land here | Check the OCR step for those pages unless they are genuinely empty |
| `classification_invalid_class_fallback` | warning | The model returned a class outside the configured vocabulary after every retry, so `fallback_class` was assigned. The stored class is not the model's answer, and extraction ran against the fallback's schema | Add the class the model kept choosing if it is legitimate, or sharpen the class descriptions it confused. See [Enforcing a Valid Class Vocabulary](#enforcing-a-valid-class-vocabulary-validation--retry) |

One issue per section per cause, listing the page IDs, so a long run of
unclassifiable pages produces one row rather than one per page.

**Why two of them are warnings and not errors.** An error-severity issue puts a red
indicator on the section in the UI, and an indicator that appears on most documents
is one nobody reads — the same reasoning that keeps the confidence alarm usable in
[Monitoring](./monitoring.md#confidence-assessment-degraded). The fallback case is
where a page the model cannot place ends up, so it is the one you are most likely to
see; a class was assigned, it is just not the model's.

⚠️ The document list's **Processing Issues** badge is severity-blind — it renders
`ProcessingIssueCount`, which counts warnings and errors alike — so a deployment
whose documents routinely contain pages the classifier cannot place will show the
badge on them. That is the intended reading, since those pages produced no extracted
data, but if it is unwanted the lever is to define a catch-all class the model can
legitimately choose (see **Catch-all class** under [Enforcing a Valid Class
Vocabulary](#enforcing-a-valid-class-vocabulary-validation--retry)), which removes
the condition rather than hiding it. The badge clears when a later run of the
document records no issues; a "has processing issues" **filter** is stickier, because
the index attribute behind it is only ever set and never cleared.

**Why no CloudWatch metric.** Every fleet-level alarm in this solution is paired
with a threshold parameter a deployer tunes, and none of these three has an
established base rate to set one from; an unalarmed metric would only add cost.
The issues reach the Sections panel, the document's `ProcessingIssueCount` and the
processing-issues index, which is what a document-level query reads.

**Downstream.** The confidence pass does **not** also report an `unclassified`
section as unscored. It has no confidence to be missing that this page's issues do
not already explain, and a `root_cause` written from the Assessment step would
point the operator at Extraction, where nothing is wrong. A section whose class is
a *named* class missing from configuration is a different case and **is** reported
from both Extraction and Assessment — see
[Monitoring](./monitoring.md#confidence-assessment-degraded).

## Excluding Static Pages (e.g. Instructions, Legal Boilerplate)

Many forms packages bundle several pages of **static** content alongside
the pages that actually carry dynamic, applicant-specific data. Think of
the first four pages of a DS-11 U.S. Passport Application (WARNING
notice, fee instructions, FEDERAL TAX LAW disclosure, ACTS OR
CONDITIONS affidavit) — they are identical across every applicant and
carry no fields to extract.

You can mark a class as *excluded* so that downstream stages
(extraction, assessment, summarization, rule validation, evaluation)
will **skip** sections classified as that class, avoiding wasted LLM
calls, tokens, and noise in accuracy metrics.

https://github.com/user-attachments/assets/3c5106ee-ffaf-48d0-ac57-6de78a221474

### How classification decides a section is "excluded"

The **primary mechanism is the LLM classifier** using each class's
`description` field. Mark the class as excluded via
`x-aws-idp-exclude-from-processing: true`; the multimodal page-level
classifier then picks the class by description like any other class,
and the `excluded` flag propagates onto the resulting `Section`. This
is the canonical, robust path — tolerant of form revisions, OCR quirks,
wording differences, and visual-only pages.

The optional `x-aws-idp-document-page-content-regex` extension is just
a **tokens-saving fast-path** for pages whose OCR text reliably matches
a known stable boilerplate phrase; if the regex misses, the LLM still
classifies the page correctly via the description. Regex alone is not
a substitute for a well-written class `description`.

### Configuring through the UI

In the Web UI **Configuration Editor → Document Schema**, select a
document-type class and use the **"Exclude from Processing"** checkbox
and the **"Exclusion Reason"** text input (appears when the checkbox is
enabled). Changes round-trip through the standard configuration save
flow, no YAML editing required.

### Class-level extensions

Add two JSON Schema extensions to the class:

```yaml
- $schema: https://json-schema.org/draft/2020-12/schema
  $id: PassportApplicationInstructions
  type: object
  x-aws-idp-document-type: PassportApplicationInstructions
  description: >-
    Static informational pages of a DS-11 passport application —
    WARNING notice, fee instructions, FEDERAL TAX LAW, ACTS OR
    CONDITIONS affidavit. No applicant-specific data.

  # The new, feature-defining flag.
  x-aws-idp-exclude-from-processing: true

  # Optional human-readable reason, surfaced in UI badges and the
  # evaluation report.
  x-aws-idp-exclusion-reason: instructions

  # Optional: use the existing page-content regex fast path so pages
  # containing these anchor phrases are classified instantly without
  # an LLM call.
  x-aws-idp-document-page-content-regex: "(?is)(WARNING:\\s*False statements|FEDERAL TAX LAW\\s*Section 6039E|ACTS OR CONDITIONS)"

  # Excluded classes have no extractable fields.
  properties: {}
```

### What happens at each stage

| Stage | Behavior on an excluded section |
|-------|---------------------------------|
| **Classification** | Section is classified normally (regex fast path or LLM). The `excluded` and `exclusion_reason` flags are copied from the class config onto the `Section` object. |
| **Extraction** | `process_document_section` short-circuits and writes a small stub `result.json` with `{"status": "skipped_excluded_class", "excluded": true, ...}`. Zero LLM calls. |
| **Assessment** (confidence) | Returns without writing anything (no extraction results exist to assess). No processing issue and no `AssessmentConfidenceUnavailable` metric: an excluded section never had extraction run, so it has no confidence to be missing — unlike a section that *should* have been scored and was not, which is [reported](./monitoring.md#confidence-assessment-degraded). |
| **Summarization** | Writes a small `summary.json` stub. No LLM call. |
| **Rule Validation** | Skips the section in `validate_document_async`. |
| **Evaluation** | Filters excluded sections out of precision/recall/F1. They still appear in the markdown report under an **Excluded Sections (Not Evaluated)** table so nothing is silently dropped. |
| **Reporting database** | Excluded sections are skipped when writing the per-section parquet rows (their stub JSON has no attributes to aggregate). |
| **UI** | Sections panel renders excluded sections with a grey `Skipped: <reason>` badge next to the class name. |

### Backwards compatibility

* If the `x-aws-idp-exclude-from-processing` extension is absent (the
  existing case), everything behaves as before.
* Legacy snake_case keys `exclude_from_processing` and
  `exclusion_reason` are accepted too for hand-authored configs.
* `Section.to_dict()` only emits the flags when they are set, so
  documents persisted before the feature still round-trip cleanly.

### End-to-end example

A working sample config for the DS-11 passport application form is
available at `config_library/unified/ds11-passport-application/`
(together with a fixture PDF `samples/DS11-USPassportApplication.pdf`).
A single-file Jupyter notebook walking through the full pipeline —
OCR → classification (LLM + optional regex) → extraction → assessment
→ summarization, with side-by-side real vs. stub output inspection —
ships at
`notebooks/usecase-specific-examples/ds11-passport-application/demo.ipynb`.

## Two-stage classification: segmentation, then section classification

`strategy: two_stage` (*Two stage* in the editor) splits the two jobs into two
calls. One stage stays the default.

```
packet ──► segmentation ──────────► segments {start_page, end_page}
                                        │
                                        ▼
           section classification ─► segments {start_page, end_page, class, confidence}
                                        │
                                        ▼
                     section forming, HITL, extraction (unchanged)
```

A one-stage call does two different jobs at once, and pays for it in two ways.
**Every boundary decision carries the whole class vocabulary** — the class list,
every description and every few-shot example — although finding a boundary does
not depend on any of it; at page granularity that prefix is paid once per page.
And **classifying scales with pages rather than documents**: a 40-page statement
is classified forty times to produce one answer.

```yaml
classification:
  strategy: two_stage
  two_stage:
    segmentation:
      method: llm
      llm:
        granularity: packet        # page | packet — this call's own
        boundary_hints: none       # or class_names (names only, no descriptions)
        model: us.amazon.nova-2-lite-v1:0
        modality: {text: "", images: ""}
        packet: {window_pages: "20", overlap_pages: "2"}
    section_classification:
      method: llm
      pages: first_k               # first | first_k | all
      k: "2"
      llm:
        model: us.anthropic.claude-haiku-4-5-20251001-v1:0
        validation: {enforceValidClasses: true, maxValidationRetries: "2"}
        confidence: {mode: topk}
```

Segmentation has its own granularity, page and packet settings, prompts and
model settings — the same call shape as one stage, asked a smaller question. At
packet granularity it is one call (or one per window) asking for page ranges; at
page granularity it is one call per page asking only `start` or `continue`, which
still makes N calls but drops the prefix from each. It has no validation or
class confidence, because it returns no class.

> ⚠️ **Writing a custom segmentation prompt: the vocabulary placeholders render
> empty there.** `{CLASS_NAMES_AND_DESCRIPTIONS}`,
> `{CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS}` and `{FEW_SHOT_EXAMPLES}` are
> all substituted with nothing in a segmentation prompt, at either granularity
> and whatever `contextPagesCount` is set to — that absence *is* the dropped
> prefix, so referencing one gets you silence rather than an error.
> `boundary_hints: class_names` is the supported way to put the class **names**
> into this call; it renders a short `{BOUNDARY_HINTS}` block, which in turn is
> empty in every call other than segmentation, because it tells the model to
> report no document type. See
> [which placeholders a stage is allowed to render](../lib/idp_common_pkg/idp_common/classification/README.md#which-placeholders-a-stage-is-allowed-to-render).

Section classification is **one call per segment**, over the pages `pages`
selects. `first_k` is the default because a document's identity is almost always
on its opening pages, while `all` makes classifying a long segment cost what
page-level classification cost. It has its own prompts, modality, image size,
validation and class confidence.

#### Setting `boundary_hints`

`boundary_hints` decides whether the segmentation prompt is told your class
names. Leave it at **`none`** by default: withholding the vocabulary from the
boundary pass is the saving two stage exists for, and a boundary is a question
about where a document ends rather than about what kind it is.

Reach for **`class_names`** when `boundary_accuracy` is the metric that is
failing — not `label_accuracy` — **and** your class names are the kind of string
that appears on a first page: a form number (`form_1040`, `w2`), a document
title, an institution's own name for a statement. Those give the model a literal
token to look for at the top of a page. If your class names are internal labels
(`type_a`, `customer_doc_3`), `class_names` sends tokens that match nothing and
costs the saving for no signal. On the over-split fixture [measured
above](#neither-granularity-dominates-and-the-failure-modes-are-opposite),
`class_names` changed nothing, which is the expected result when the ambiguity
is about extent rather than identity. `class_names` sends names only, never
descriptions or examples.

#### Setting `pages` and `k`

`pages` decides how much of each section the classifying call sees, and it is
the only two-stage setting whose cost grows with section length:

- **`first_k` with `k: 2`** is the default and covers the opening identity block
  of nearly every form — a cover page plus the first content page.
- **`first`** is the cheapest. Use it when every class is identifiable from its
  very first page, which is usually true of forms and statements.
- **`all`** makes a long section cost roughly what page-level classification
  would have cost, which removes most of two stage's advantage. It is the right
  answer only when a class is genuinely not identifiable from its opening pages.
- **Raise `k` above 2** only for classes whose distinguishing content sits past
  the cover page — a bundle whose first page is a generic transmittal sheet, or
  two classes that share an opening page and differ later.

Cost is roughly linear in the pages sent. Measured on one 6-page section (Nova 2
Lite, section classification input tokens only): `first` sent 1 page for 1,317
input tokens, `first_k` with `k: 2` sent 2 pages for 2,777, and `all` sent 6 for
6,599.

⚠️ `pages` and `k` are **Stage B** settings and cannot change the boundaries —
if your sections are wrong, this is not the axis to tune.

Four things worth knowing:

- **Each stage names its own model.** That is most of the point: boundary
  detection may want a capable multimodal model looking at a whole packet, while
  classifying a clean segment is often a much easier problem a cheaper model
  answers just as well. A custom or fine-tuned classification model
  (`CustomClassificationModelARN`, or *Create configuration version* on a
  fine-tuning job) is written to section classification.
- **The halves are metered separately**, as `Segmentation/...` and
  `SectionClassification/...` in the cost table and the reporting database,
  while one stage keeps `Classification/...`. That split is what makes "is this
  actually cheaper on my documents?" answerable from the cost table rather than
  from arithmetic.
- **A document-name regex match skips section classification entirely.** The
  name already asserts the class, so a name-matched 40-page packet costs its
  boundary calls and no classification calls at all.
- **A declared file shape skips segmentation entirely.** Under
  `file_shape.sectionSplitting: disabled` the whole file is one segment, and
  under `page` every page is one, so no boundary call is made.

A segment whose section classification call fails keeps **no class** and is
reported in the document's errors. Its boundaries are still good, and an
invented class would propagate into extraction as though it had been decided.

`segmentation.method` and `section_classification.method` ship with one value,
`llm`. A boundary-detection model, embeddings-based classification and external
classifier endpoints (SageMaker, a Bedrock custom model, or a Lambda adapter in
front of anything else) are described in
[issue #1329](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1329);
each needs measurement on real documents before its defaults can be set
honestly. Their settings blocks (`segmentation.boundary_model`,
`section_classification.embeddings`, `decision_model` and `low_confidence`) are
declared so adding a method later needs no configuration migration, and are
ignored until their method exists.

### Telling a wrong boundary from a wrong label

`split_accuracy_without_order` requires a predicted section to match on **both**
its page set and its class, so a wrong boundary and a wrong label are reported
as the same failure — and those two call for different fixes. The evaluation
report now also carries the two halves:

| Metric | Means |
|---|---|
| `boundary_accuracy` | Ground-truth sections whose exact page set a predicted section matched, ignoring class |
| `label_accuracy` | Of those, the fraction whose class also matched |

The decomposition is exact: `split_accuracy_without_order =
boundary_accuracy × label_accuracy`. `label_accuracy` is reported as *not
scored* rather than `0.0` when no section's boundaries matched — "we never got
far enough to judge the labels" is a different fact from "every label was
wrong", and that is the pair of readings the split exists to keep apart. Both
are computed for one stage too.

## Choosing granularity, modality and strategy

Granularity and modality are per call: one stage has one of each, and two stage
sets them separately on segmentation and on section classification (which has
modality only — it is always one call per section).

### Use `granularity: packet` when

- **Boundaries depend on seeing the packet**, not on each page in isolation —
  continuing narratives, numbered sections, a cover sheet whose role is only
  clear from what follows it.
- **Classification cost is a large share of the bill.** Packet granularity pays
  the prefix and the output once instead of per page, which matters most on long
  packets with few sections.
- The packet fits the window you set, or you are happy to window it. Set
  the call's `packet.window_pages` rather than relying on a model's context size.

### Use `granularity: page` when

- **Document packets contain multiple documents of the same type** and the
  boundary cue is a per-page detail (a restarting page number, a repeated
  header) that a per-page verdict catches reliably.
- **Processing speed matters**: pages are classified in parallel, so a long
  packet's wall-clock cost is roughly one call rather than N.
- You want a per-page confidence score. Packet granularity scores per *segment*,
  which is a coarser signal for HITL routing.

### Modality

- **Both on** (the default) is the right starting point: layout and text each
  carry boundary and class evidence the other does not.
- **`images: false`** is the lever for image-dominated cost, and the one to
  reach for first — it is usually a far larger saving than anything else here.
  Measure the accuracy cost on your own documents; on clean text documents it is
  often nil.
- **`text: false`** suits scans whose OCR is poor enough to mislead, and
  requires a real OCR backend only if you still want the page-content regex
  shortcut (which reads the text even when it is not sent).

### Neither granularity dominates, and the failure modes are opposite

The hardest boundary case is a **reprinted identity block**: a multi-page
document whose header, account number and address appear again on every page.
Read one page at a time, every page looks like a first page. Read the packet at
once, two adjacent documents of the same type look like one. Those are the same
ambiguity seen from the two available vantage points, and the two granularities
fail on opposite sides of it.

Measured on a matched pair of generated bank-statement fixtures, Nova 2 Lite,
expected section counts from exact ground truth:

| Fixture | Shape | `granularity: page` | `granularity: packet` |
|---|---|---|---|
| one 3-page statement, identity header reprinted on every page | expect **1** section | **3** — wrong | **1** — right |
| two 3-page statements, header on each first page | expect **2** sections | **2** — right | **1** — wrong |

Page granularity split a single document into one section per page; packet
granularity merged two adjacent documents into one. Both results held under
`strategy: single_pass` and `strategy: two_stage`, so this is a property of the
granularity rather than of the strategy. **Choose by which error your corpus can
afford.** A corpus of long single documents is safer at packet granularity; a
corpus of back-to-back same-type forms is safer at page granularity.

Two levers were measured on the over-split case and neither solves it:

- **`boundary_hints: class_names`** left it at 3 sections. Putting the class
  names into the boundary prompt does not help when the ambiguity is about where
  one document ends, not about what kind it is.
- **`contextPagesCount: 1`** improved it from 3 sections to 2 — a real
  improvement, not a fix — and on the *other* fixture the same setting moved a
  correct 3-section answer to 2. That is the over-merge trade this page warns
  about, measured in both directions on one pair of documents: showing a page its
  neighbours makes continuation pages easier to recognise and makes two
  back-to-back documents of the same type easier to confuse.

If neither error is acceptable, assert the shape instead of detecting it:
`file_shape.sectionSplitting: disabled` gives a file known to hold one document
exactly one section, and `page` gives a file of one-page documents exactly one
section each. Both are assertions, so they are only right when you know the
shape — see [Section Splitting Strategies](#section-splitting-strategies).

### Strategy

- **One stage (`single_pass`)** for single-page files and short packets, where
  two stage adds a call to save a prefix that was only paid once.
- **Two stage (`two_stage`)** for long documents with few sections, when you
  want a different model for classifying than for boundary detection, or when you
  need to know whether your section errors are boundary errors or label errors.

#### Why the prefix saving is smaller than it looks

The argument for two stage above is that a one-stage boundary decision carries
the class vocabulary it does not need. That is true, and it is **not** worth the
list price of those tokens, because the prefix is cached. The default
classification model, `us.amazon.nova-2-lite-v1:0`, is in
`CACHEPOINT_SUPPORTED_MODELS` in
`lib/idp_common_pkg/idp_common/bedrock/client.py`, the shipped one-stage prompts
put the class list, the descriptions and the few-shot examples **before**
`<<CACHEPOINT>>`, and `config_library/pricing.yaml` prices that model's
`cacheReadInputTokens` at a quarter of its `inputTokens` with
`cacheWriteInputTokens` at zero. So from the second page of a run onward the
prefix one stage "wastes" is billed at 25% of list and was free to write. Two
stage can therefore recover at most three quarters of the prefix on a cache-hit
page — and it spends a whole extra request per section to do it.

Which way that nets out depends on how many pages each section has, so measure
it rather than reasoning about it. ⚠️ **The figures below are one regime, and it
is the regime least favourable to two stage** — a 3-page file holding three
1-page documents, a 2-class vocabulary with short descriptions, Nova 2 Lite,
classification Bedrock tokens only. One page per document means the per-section
second call is a whole extra request against a prefix that was small to begin
with. Do not read it as a general result:

| Cell | Bedrock requests | Fresh input tokens |
|---|---|---|
| one stage, `granularity: page` | 3 | 1,527 |
| two stage, `granularity: page` | 6 | 2,988 |
| one stage, `granularity: packet` | 1 | 1,650 |
| two stage, `granularity: packet` | 4 | 3,258 |

At each granularity two stage cost about twice the fresh input tokens and twice
the requests. Section counts were correct in all four cells, so this is a cost
difference and not an accuracy trade. (Cache-read volumes are omitted
deliberately: they depend on what else had recently run against the same prefix,
so single runs are not comparable on that column.)

The durable reasons to pick two stage are not the prefix:

- **One classification call per *document* instead of per page.** This is the one
  that scales, and it is the opposite of the regime measured above: on a 40-page
  statement, one stage makes 40 classification calls to produce one answer and
  two stage makes one boundary pass plus one classification call. The longer your
  sections, the better two stage looks.
- **A different model per stage.** Boundary detection and classification are
  different problems and the cheapest model that is good at one is often not the
  other. One stage has a single model field for both.
- **The `boundary_accuracy` / `label_accuracy` decomposition**, which tells you
  which half to fix. See [Telling a wrong boundary from a wrong
  label](#telling-a-wrong-boundary-from-a-wrong-label).

Measure on your own corpus. The two stages are metered separately
(`Segmentation/` and `SectionClassification/`, against one stage's
`Classification/`), so the cost table answers the question directly.

### Comparison Table

| | `page` | `packet` |
|---|---|---|
| Calls per document | N (one per page) | 1, or ⌈(N − `overlap_pages`) / (`window_pages` − `overlap_pages`)⌉ |
| Context when deciding a boundary | One page, plus `contextPagesCount` neighbours | The whole packet (or window) |
| Prefix cost | Paid per page | Paid once per call |
| Image tokens | Same | Same |
| Confidence unit | Per page | Per segment |
| Parallelism | Pages run concurrently | Windows run in sequence |
| Large packets | No practical limit | Windowed; `window_pages: 0` is limited by context |
| Multi-document packets of one type | Excellent (per-page `start` verdict) | Good, and improving with a packet view |

## Customizing Classification in Pipeline Mode

### Configuration Settings

#### Page Limit Configuration

For a file you know is one document, classify only its first pages:

```yaml
classification:
  file_shape:
    sectionSplitting: disabled          # one document per file
    maxPagesForClassification: "3"      # Default "ALL": classify every page
```

When set to a number, only the first N pages are classified, and their majority
class is applied to every page in the file as one section. The limit applies only
with `sectionSplitting: disabled`; with boundary detection on it is ignored, with
a warning in the classification log, because a limit would collapse only files
longer than N while sectioning shorter ones normally.

⚠️ **Quote the value, or write it as a bare number.** `maxPagesForClassification:
true` is not a page count: an unquoted YAML boolean is reported and the file is
classified in full. `ALL` and `0` both mean "every page".

### Prompt Components

You can customize classification behavior through the prompts of each call. The
examples below are written for the one-stage page call
(`classification.one_stage.page`); the packet call, segmentation and section
classification have the same two prompts in their own blocks.

### System Prompts

Define overall model behavior and constraints:

```yaml
classification:
  one_stage:
    page:
      system_prompt: |
        You are an expert document classifier specializing in financial and business documents.
        Your task is to analyze document images and classify them into predefined categories.
        Focus on visual layout, textual content, and common patterns found in each document type.
        When in doubt, analyze the most prominent features like headers, logos, and form fields.
```

### Task Prompts

Specify classification instructions and formatting. A page prompt must ask for
the boundary verdict as well as the class, because `document_boundary` is the
signal `file_shape.sectionSplitting: llm_determined` — the default — groups pages
into sections with. A prompt that returns only a class name leaves every page
looking like the start of a new document:

```yaml
classification:
  one_stage:
    page:
      task_prompt: |
        Analyze the following document page and classify it into one of these categories:
        {CLASS_NAMES_AND_DESCRIPTIONS}

        Also say whether this page is the FIRST page of a document ("start") or a
        later page of a document that began earlier ("continue").

        <output-format> {
          "classification_reason": "the evidence for this class and this boundary verdict",
          "class": "exact_document_type_from_list",
          "document_boundary": "start or continue"
        } </output-format>

        <<CACHEPOINT>>

        <document-ocr-data> {DOCUMENT_TEXT} </document-ocr-data>
        <document-image> {DOCUMENT_IMAGE} </document-image>
```

⚠️ **Three things a hand-written classification prompt has to carry, none of
which produces an error if it is missing.**

- **The output shape the active call's parser reads.** A page call is parsed for
  `class` and `document_boundary`; a packet call is parsed for a `segments` array
  of `ordinal_start_page`/`ordinal_end_page`/`type`. Asking for the wrong one
  fails the parse rather than the validation. The shipped prompts in
  `lib/idp_common_pkg/idp_common/config/system_defaults/base-classification.yaml`
  are the reference for both shapes, and the two-stage segmentation prompts there
  drop `class`/`type` entirely because that call assigns no class.
- **Both `{DOCUMENT_TEXT}` and `{DOCUMENT_IMAGE}`, unless you mean to drop one.**
  An unset `modality.text` or `modality.images` is **inferred from this call's
  task prompt**, so deleting `{DOCUMENT_IMAGE}` turns images off for the call
  silently. That is the documented way to make a call text-only — it is only a
  problem when it was not intended.
- **The boundary rules themselves, if you are relying on boundary detection.**
  The shipped prompt's `<boundary-detection-rules>` block is what makes a
  repeated table heading read as a continuation page rather than a new document;
  a short prompt without it will split long tables.

The snippets further down this page are **fragments** that illustrate one setting
at a time — placeholder position, few-shot splicing, image sizing — and are not
complete prompts. Start from the shipped prompt and change what you need.

### Class Descriptions

Classes live in the top-level `classes:` list, not under `classification:`. Each
entry is a JSON Schema object whose `x-aws-idp-document-type` is the class name
the model must return and whose `description` is what
`{CLASS_NAMES_AND_DESCRIPTIONS}` renders into the prompt. Write a description
detailed enough to separate the class from its neighbours — it is the only thing
distinguishing them in the prompt:

```yaml
classes:
  - $schema: "https://json-schema.org/draft/2020-12/schema"
    $id: invoice
    x-aws-idp-document-type: invoice
    type: object
    description: "A commercial document issued by a seller to a buyer, related to a sale transaction and indicating the products, quantities, and agreed prices for products or services."
    properties: {}
  - $schema: "https://json-schema.org/draft/2020-12/schema"
    $id: receipt
    x-aws-idp-document-type: receipt
    type: object
    description: "A document acknowledging that something of value has been received, often as proof of payment."
    properties: {}
  - $schema: "https://json-schema.org/draft/2020-12/schema"
    $id: bank_statement
    x-aws-idp-document-type: bank_statement
    type: object
    description: "A document issued by a bank showing transactions and balances for a specific account over a defined period."
    properties: {}
```

The `properties` of each class are the extraction schema for documents of that
class; [Few Shot Example Configuration](#few-shot-example-configuration) below
shows a fully populated entry.

## Using CachePoint for Classification

The solution integrates with Amazon Bedrock CachePoint for improved performance:

- Caches frequently used prompts and responses
- Reduces latency for similar classification requests
- Optimizes costs through response reuse
- Automatic cache management and expiration

CachePoint is particularly beneficial with few-shot examples, as these can add significant token count to prompts. The `<<CACHEPOINT>>` delimiter in prompt templates separates:

- **Static portion** (before CACHEPOINT): Class definitions, few-shot examples, instructions
- **Dynamic portion** (after CACHEPOINT): The specific document being processed

This approach allows the static portion to be cached and reused across multiple document processing requests, while only the dynamic portion varies per document, significantly reducing costs and improving performance.

Example task prompt with CachePoint for few-shot examples:

```yaml
classification:
  one_stage:
    page:
      task_prompt: |
        Classify this document into exactly one of these categories:
    
        {CLASS_NAMES_AND_DESCRIPTIONS}
    
        <few_shot_examples>
        {FEW_SHOT_EXAMPLES}
        </few_shot_examples>
    
        <<CACHEPOINT>>
    
        <document_content>
        {DOCUMENT_TEXT}
        </document_content>
```

## Document Classes

### Standard Document Classes

The solution includes standard document classes based on the RVL-CDIP dataset:

- `letter`: Formal written correspondence
- `form`: Structured documents with fields
- `email`: Digital messages with headers
- `handwritten`: Documents with handwritten content
- `advertisement`: Marketing materials
- `scientific_report`: Research documents
- `scientific_publication`: Academic papers
- `specification`: Technical specifications
- `file_folder`: Organizational documents
- `news_article`: Journalistic content
- `budget`: Financial planning documents
- `invoice`: Commercial billing documents
- `presentation`: Slide-based documents
- `questionnaire`: Survey forms
- `resume`: Employment documents
- `memo`: Internal communications

### Custom Document Classes

You can define custom document classes through the Web UI configuration:

1. Navigate to the Configuration section
2. Select the Document Classes tab
3. Click "Add New Class"
4. Provide:
   - Class name (machine-readable identifier)
   - Display name (human-readable name)
   - Detailed description (to guide the classification model)
5. Save changes

## Image Placement with {DOCUMENT_IMAGE} Placeholder

Pattern 2 supports precise control over where document images are positioned within your classification prompts using the `{DOCUMENT_IMAGE}` placeholder. This feature allows you to specify exactly where images should appear in your prompt template, rather than having them automatically appended at the end.

### How {DOCUMENT_IMAGE} Works

**Without Placeholder (Default Behavior):**
```yaml
classification:
  one_stage:
    page:
      task_prompt: |
        Analyze this document:
    
        {DOCUMENT_TEXT}
    
        Classify it as one of: {CLASS_NAMES_AND_DESCRIPTIONS}
```
Images are automatically appended after the text content.

**With Placeholder (Controlled Placement):**
```yaml
classification:
  one_stage:
    page:
      task_prompt: |
        Analyze this document:
    
        {DOCUMENT_IMAGE}
    
        Text content: {DOCUMENT_TEXT}
    
        Classify it as one of: {CLASS_NAMES_AND_DESCRIPTIONS}
```
Images are inserted exactly where `{DOCUMENT_IMAGE}` appears in the prompt.

### Usage Examples

**Image Before Text Analysis:**
```yaml
task_prompt: |
  Look at this document image first:
  
  {DOCUMENT_IMAGE}
  
  Now read the extracted text:
  {DOCUMENT_TEXT}
  
  Based on both the visual layout and text content, classify this document as one of:
  {CLASS_NAMES_AND_DESCRIPTIONS}
```

**Image in the Middle for Context:**
```yaml
task_prompt: |
  You are classifying business documents. Here are the possible types:
  {CLASS_NAMES_AND_DESCRIPTIONS}
  
  Examine this document image:
  {DOCUMENT_IMAGE}
  
  Additional text content extracted from the document:
  {DOCUMENT_TEXT}
  
  Classification:
```

### Integration with Few-Shot Examples

The `{DOCUMENT_IMAGE}` placeholder works seamlessly with few-shot examples:

```yaml
classification:
  one_stage:
    page:
      task_prompt: |
        Here are examples of each document type:
        {FEW_SHOT_EXAMPLES}
    
        Now classify this new document:
        {DOCUMENT_IMAGE}
    
        Text: {DOCUMENT_TEXT}
    
        Classification: {CLASS_NAMES_AND_DESCRIPTIONS}
```

### Benefits

- **🎯 Contextual Placement**: Position images where they provide maximum context
- **📱 Better Multimodal Understanding**: Help models correlate visual and textual information
- **🔄 Flexible Prompt Design**: Create prompts that flow naturally between different content types
- **⚡ Improved Performance**: Strategic image placement can improve classification accuracy
- **🔒 Backward Compatible**: Existing prompts without the placeholder continue to work unchanged

### Multi-Page Documents

For documents with multiple pages, the system provides comprehensive image support:

- **No cap on page count**: a document may have any number of pages. What *is*
  bounded is how many images one request carries: above 20 image blocks in a
  single Bedrock request every image in it is downscaled to 2,000 px, which is
  why `packet.window_pages` ships at 20. A single image also fails outright
  above 3.75 MiB stored or 8,000 px on a side
- **Info Logging**: System logs image counts for monitoring and debugging purposes
- **Automatic Pagination**: Images are processed in page order for all pages

## Optional `{CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS}` Placeholder

Pattern 2 classification prompts support an **optional** placeholder that
expands to each class's name, description, **and the names of the schema
attributes (extraction fields)** declared for that class. This gives the
classifier richer disambiguation signal — particularly useful when two
document types have similar names or descriptions but very different
extraction schemas (e.g. `appraisal_report` vs `inspection_report`).

The placeholder is fully **opt-in**:

- The default classification prompts in `config_library/` continue to use
  only `{CLASS_NAMES_AND_DESCRIPTIONS}`. **Token usage and cost are
  unchanged** for users who don't reference the new placeholder.
- Power users with schema-rich domains (lending, healthcare, insurance)
  can drop `{CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS}` into a custom
  `task_prompt` to give the model the extra signal.

> ⚠️ **In a two-stage *segmentation* prompt this placeholder renders empty**,
> as `{CLASS_NAMES_AND_DESCRIPTIONS}` and `{FEW_SHOT_EXAMPLES}` do: it carries
> the class names, their descriptions *and* their attributes, which is more
> vocabulary than the plain listing rather than less, and a boundary call that
> carries none of it is the saving two-stage exists for. A segmentation
> template referencing it gets nothing rather than an error;
> `boundary_hints: class_names` is the supported way to put class **names**
> into that call. See
> [which placeholders a stage is allowed to render](../lib/idp_common_pkg/idp_common/classification/README.md#which-placeholders-a-stage-is-allowed-to-render).

### Rendered output (page-level classification)

At page granularity the placeholder renders as one XML block per class:

```xml
<class name="appraisal_report">
  <description>Real estate valuation report</description>
  <attributes>property_address, appraised_value, effective_date, appraiser_name, comparable_sales.address, comparable_sales.sale_price</attributes>
</class>
<class name="inspection_report">
  <description>Property condition inspection report</description>
  <attributes>property_address, inspection_date, inspector_name, findings</attributes>
</class>
```

### Rendered output (packet granularity)

At packet granularity the same placeholder renders as a markdown table — matching the format of the
existing `{CLASS_NAMES_AND_DESCRIPTIONS}` table:

```markdown
| type | description | attributes |
| --- | --- | --- |
| appraisal_report | Real estate valuation report | property_address, appraised_value, effective_date, ... |
| inspection_report | Property condition inspection report | property_address, inspection_date, inspector_name, findings |
```

### Example: Custom prompt using the new placeholder

```yaml
classification:
  one_stage:
    page:
      task_prompt: |
        Classify the following document page into one of these document types.

        Use the schema attribute names listed for each class as a strong
        disambiguation signal — if the page text mentions field names that
        match a class's attributes, prefer that class.

        {CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS}

        Document text:
        <document-text>
        {DOCUMENT_TEXT}
        </document-text>

        Respond with JSON: {"class": "...", "document_boundary": "start|continue"}
```

### Schema-walking rules

- Flat scalar properties surface by their property name
  (e.g. `appraised_value`).
- Nested `object` properties are flattened to dotted paths
  (e.g. `borrower.address.zip`).
- Arrays of objects are unwrapped — each item-property is rendered as
  `parent.child` (no `[]` indexing).
- Arrays of scalars (or arrays without item properties) surface by their
  parent name only.
- Subschemas declared as a local `$ref` into the class's `$defs` — which
  is what the Web UI's schema editor emits for every group and list-item
  shape — are dereferenced first, so a `$ref` group renders exactly like
  an equivalent inline group (`Signatures.Signature-of-taxpayer1`, not a
  bare `Signatures`). `$ref` chains are followed; a `$ref` that cannot be
  resolved (dangling or remote) surfaces by its property name only rather
  than failing the classification. A **recursive** definition is walked
  normally and only stops where it re-enters itself — a `Node` with a
  `child: {"$ref": "#/$defs/Node"}` member yields `root.label`, `root.child`
  and `root.kids`, with `child`/`kids` as leaves.
- Very deeply nested schemas are truncated. Because a `$defs` definition may
  be referenced from many sibling branches, dereferencing can expand a small
  schema into a very large attribute list, so the walk stops at 500 names and
  logs a warning. Prefer flatter schemas if you hit it.
- Classes that have no JSON Schema render
  `<attributes>(no schema)</attributes>` so the absence is obvious for
  debugging.

### Soft cap and token-cost guardrails

To prevent pathologically large schemas from bloating the classification
prompt, the rendered attribute list per class is **soft-capped at 50
field names** (`ClassificationService.MAX_ATTRIBUTES_PER_CLASS`). When a
class exceeds the cap, the rendered list is truncated and a
`...(+N more)` suffix is appended; a `WARNING` log line is emitted for
visibility.

If you have a class with hundreds of attributes, prefer:

1. Writing a richer class `description` that captures distinguishing
   characteristics, **or**
2. Adding [few-shot classification examples](#setting-up-few-shot-examples-in-pattern-2)
   for that class.

### Mixing with the legacy placeholder

You can use both placeholders in the same prompt — they're independent.
For example you could keep the compact `{CLASS_NAMES_AND_DESCRIPTIONS}`
list for an overview block and add `{CLASS_AND_ATTRIBUTE_NAMES_AND_DESCRIPTIONS}`
in a "for ambiguous cases, consult the schema fields" sub-section.

## Setting Up Few Shot Examples in Pattern 2

Classification supports few-shot example prompting at either granularity and under either strategy, which can significantly improve classification accuracy by providing concrete document examples. The `rvl-cdip-with-few-shot-examples` preset in `config_library/unified/` is a worked example; examples are defined on a class, so no preset is required to use them.

### Benefits of Few-Shot Examples

- **🎯 Improved Accuracy**: Models understand document patterns better through concrete examples
- **📏 Consistent Output**: Examples establish exact structure and formatting standards
- **🚫 Reduced Hallucination**: Examples reduce likelihood of made-up classifications
- **🔧 Domain Adaptation**: Examples help models understand domain-specific terminology
- **💰 Cost Effectiveness with Caching**: Using prompt caching with few-shot examples significantly reduces costs

### Few Shot Example Configuration

In Pattern 2, few-shot examples are configured within document class definitions using JSON Schema format:

```yaml
classes:
  - $schema: "https://json-schema.org/draft/2020-12/schema"
    $id: Letter
    x-aws-idp-document-type: Letter
    type: object
    description: "A formal written correspondence..."
    properties:
      SenderName:
        type: string
        description: "The name of the person who wrote the letter..."
    x-aws-idp-examples:
      - x-aws-idp-class-prompt: "This is an example of the class 'Letter'"
        name: "Letter1"
        x-aws-idp-image-path: "config_library/unified/your_config/example-images/letter1.jpg"
      - x-aws-idp-class-prompt: "This is an example of the class 'Letter'"
        name: "Letter2"
        x-aws-idp-image-path: "config_library/unified/your_config/example-images/letter2.png"
```

### Example Image Path Support

The `x-aws-idp-image-path` field supports multiple formats:

- **Single Image File**: `"config_library/unified/examples/letter1.jpg"`
- **Local Directory with Multiple Images**: `"config_library/unified/examples/letters/"`
- **S3 Prefix with Multiple Images**: `"s3://my-config-bucket/examples/letter/"`
- **Direct S3 Image URI**: `"s3://my-config-bucket/examples/letter1.jpg"`

For comprehensive details on configuring few-shot examples, including multimodal vs. text-only approaches, example management, and advanced features, refer to the [few-shot-examples.md](./few-shot-examples.md) documentation.

## Image Processing Configuration

The classification service supports configurable image dimensions for optimal performance and quality:

### Image dimensions default to the original resolution

An empty string, or an absent dimension, sends the image OCR produced at its own
resolution, which is what gives the best classification accuracy. That is the
shipped setting on all three calls:

```yaml
classification:
  one_stage:
    llm:
      model: us.amazon.nova-2-lite-v1:0
      # Image processing settings - preserves original resolution
      image:
        target_width: ""     # Empty string = no resizing (recommended)
        target_height: ""    # Empty string = no resizing (recommended)
```

A classification call's `image` block honours **only** `target_width` and
`target_height`. `dpi` and `preprocessing` are accepted by the same model, so
writing them produces no error and no warning, and they do nothing here —
rasterization DPI and binarization are OCR-stage decisions and belong under
`ocr.image`. Under `strategy: two_stage` the same block exists on each stage at
`classification.two_stage.segmentation.llm.image` and
`classification.two_stage.section_classification.llm.image`, and the segmentation
one is the one worth shrinking first: a boundary decision needs layout, not
legible body text.

### Custom Image Dimensions

Configure specific dimensions when performance optimization is needed:

```yaml
# For high-accuracy classification with controlled dimensions
classification:
  one_stage:
    llm:
      image:
        target_width: "1200"   # Resize to 1200 pixels wide
        target_height: "1600"  # Resize to 1600 pixels tall

# For fast processing with lower resolution
classification:
  one_stage:
    llm:
      image:
        target_width: "600"    # Smaller for faster processing
        target_height: "800"   # Maintains reasonable quality
```

### Image Resizing Features

- **Original Resolution Preservation**: Empty strings preserve full document resolution for maximum accuracy
- **Aspect Ratio Preservation**: Images are resized proportionally without distortion when dimensions are specified
- **Smart Scaling**: Only downsizes images when necessary (scale factor < 1.0)
- **High-Quality Resampling**: Better visual quality after resizing
- **Performance Optimization**: Configurable dimensions allow balancing accuracy vs. speed

### Configuration Benefits

- **Maximum Classification Accuracy**: Empty strings preserve full document resolution for best results
- **Service-Specific Tuning**: Each service can use optimal image dimensions
- **Runtime Configuration**: No code changes needed to adjust image processing
- **Backward Compatibility**: Existing numeric values continue to work as before
- **Memory Optimization**: Configurable dimensions allow resource optimization
- **Better Resource Utilization**: Choose between accuracy (original resolution) and performance (smaller dimensions)

### Fixed dimensions

Empty strings preserve the original image resolution. To resize every page image
a call sends to a fixed size, set both dimensions on that call:

```yaml
classification:
  one_stage:
    llm:
      image:
        target_width: "951"
        target_height: "1268"
```

### Best Practices for Classification

1. **Use Empty Strings for High Accuracy**: For critical document classification, use empty strings to preserve original resolution
2. **Consider Document Types**: Complex layouts benefit from higher resolution, simple text documents may work well with smaller dimensions
3. **Test Performance Impact**: Higher resolution images provide better accuracy but consume more resources
4. **Monitor Processing Time**: Balance classification accuracy with processing speed based on your requirements

## JSON and YAML Output Support

The classification service supports both JSON and YAML output formats from LLM responses, with automatic format detection and parsing:

### Automatic Format Detection

The system automatically detects whether the LLM response is in JSON or YAML format:

```yaml
# JSON response (traditional)
classification:
  one_stage:
    page:
      task_prompt: |
        Classify this document and respond with JSON:
        {"class": "invoice", "confidence": 0.95}

# YAML response (more token-efficient)
classification:
  one_stage:
    page:
      task_prompt: |
        Classify this document and respond with YAML:
        class: invoice
        confidence: 0.95
```

### Token Efficiency Benefits

YAML format provides significant token savings:

- **10-30% fewer tokens** than equivalent JSON
- No quotes required around keys
- More compact syntax for nested structures
- Natural support for multiline content

### Example Prompt Configurations

**JSON-focused prompt:**
```yaml
classification:
  one_stage:
    page:
      system_prompt: |
        You are a document classifier. Respond only with JSON format.
      task_prompt: |
        Classify this document and return a JSON object with the class name and confidence score.
```

**YAML-focused prompt:**
```yaml
classification:
  one_stage:
    page:
      system_prompt: |
        You are a document classifier. Respond only with YAML format.
      task_prompt: |
        Classify this document and return YAML with the class name and confidence score.
```

### Backward Compatibility

- All existing JSON-based prompts continue to work unchanged
- The system automatically detects and parses both formats
- No configuration changes required for existing deployments
- Intelligent fallback between formats if parsing fails

### Implementation Details

The classification service uses the new `extract_structured_data_from_text()` function which:

- Automatically detects JSON vs YAML format
- Provides robust parsing with multiple extraction strategies
- Handles malformed content gracefully
- Returns both parsed data and detected format for logging

## Classification Confidence

Classification can report **how confident it is in the class it chose**, per page
and per section. This is separate from the per-field confidence that extraction
produces (see [extraction-and-confidence.md](./extraction-and-confidence.md));
this one is about the *class*, not the extracted values.

> **Both `confidence` and `classification_reason` are read and persisted.** A
> prompt that asks for either — including a hand-written one — has the value
> stored on the page or section and surfaced in the Web UI, the document JSON and
> the reporting database. Releases before
> [#673](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/673)
> discarded them, so a prompt carrying them starts producing scores on upgrade.

### Where a score comes from

There is no separate confidence inference for classification: the value comes out
of the same response as the class. Each class-assigning call's `confidence.mode`
(`classification.one_stage.llm.confidence.mode`, and the same under
`two_stage.section_classification.llm`) composes the
instruction into the prompt for you — it is **on by default** — and a custom
prompt that asks for `confidence` or `candidates` itself is honoured either way,
because the response parser is not gated on the setting.

Editable in the Web UI under **Configuration → Classification → Class
confidence**, or in YAML:

```yaml
classification:
  one_stage:
    llm:
      confidence:
        mode: topk           # topk (default) | verbalized | off
        top_k_candidates: 3  # topk only
```

| Mode | What the model is asked for | Notes |
|------|-----------------------------|-------|
| `topk` (default) | ranked candidate classes with probabilities | Best calibrated, and it answers "what else could this have been?" |
| `verbalized` | one self-reported 0-1 number | Cheapest and the most overconfident. |
| `off` | nothing extra | Costs nothing at all. |

`topk` is the default for a measured reason. Asking for a single number gets
~0.95 on everything; making the model enumerate and rank alternatives forces it
to distribute probability mass instead (Tian et al., *Just Ask for Calibration*,
EMNLP 2023 — the same reasoning behind extraction's `G1/P1` confidence path). It
produces exactly the "80 % W-2, 15 % 1099" shape, and the runner-ups are stored
and shown in the UI.

**What it costs:** measured over 298 pages of a 13-class corpus, `topk` added
**+17 % to the classification step** — which is only ~3 % of total document cost
on the default model, so **~0.5 % of the bill**, inside normal run-to-run
variance. It changed classification accuracy by nothing consistent (+0.013 on
Nova 2 Lite, −0.007 on Haiku 4.5, opposite signs, single runs). Page-level
classification is one inference *per page*, so if you process very large packets
and want none of this, `off` costs exactly nothing. See the
[classification-confidence benchmark](./benchmarking/studies/classification-confidence.md).

A page is scored on the probability of the class **actually stored**, not simply
the highest probability in the list — those differ when the model's `class`
disagrees with its own ranking, and the top probability would then describe a
class the page was not given. Candidate classes outside your configured
vocabulary are dropped (they cannot be stored, so they cannot be reported), and
probabilities are rescaled only if they sum to **more** than 1.0; mass left
unassigned below 1.0 legitimately means "possibly some other class".

The instruction block is spliced in before the document content so it stays
inside the prompt-cache prefix, and it is skipped for a prompt that already
carries a `<class-confidence>` block. Both blocks are editable config
(`confidence.task_prompt_topk` / `task_prompt_verbalized`), like every other
prompt here. At one-stage packet granularity the per-segment variants,
`task_prompt_topk_packet` / `task_prompt_verbalized_packet`, are spliced instead,
because that call returns a segment list rather than one object per page.

Writing it into your own prompt works too:

```yaml
classification:
  one_stage:
    page:
      task_prompt: |
        ...
        <output-format>
        {
          "classification_reason": "Evidence that led to this classification",
          "class": "exact_document_type_from_list",
          "confidence": <probability between 0.0 and 1.0>,
          "document_boundary": "start or continue"
        }
        </output-format>
```

The parse is tolerant: `0.95`, `"0.95"`, `95`, and `"95%"` all read as 0.95. A
value that is unparseable or out of range is ignored (the page is simply
unscored) rather than failing the classification.

⚠️ **Confidence multiplies output tokens per PAGE.** Page-level classification
runs one inference per page, so anything added to its output format is paid for
on every page of every document — unlike extraction's confidence, which is per
section. Reasoning text in particular is not free.

### Not scored vs. scored 1.0

An **absent** confidence means NOT SCORED, and it is stored as null, not as a
number. Nothing invents a value:

| Situation | Confidence |
|-----------|-----------|
| Model asked for a confidence and returned one | the model's value |
| Prompt asks for no confidence (`mode: off`, or a custom prompt that asks for none) | *not scored* |
| **BDA mode**, blueprint matched | BDA's **matched-blueprint confidence** — the blueprint is what determines the class, so this needs no prompt change and costs nothing extra |
| **BDA mode**, no blueprint match | *not scored* |
| Class came from a document-name regex, a single-class configuration, or a page-content regex | `1.0` — deterministic assertion, no model involved |
| Class was corrected by a human in the Web UI | `1.0` — the operator's assertion |
| Model returned an invalid class and the fallback was applied | *not scored* (the class is no longer the model's) |
| Pages beyond `maxPagesForClassification` | *not scored* (the class is extrapolated) |
| Under `sectionSplitting: disabled`, a page whose own verdict was not the winning class | *not scored* (the class it carries is the vote's, not its own) |
| SageMaker/UDOP backend | *not scored* (the endpoint returns no score) |
| Page classification errored | `0.0` |

⚠️ **The `disabled` row is the one that catches people out, because it makes a
whole section unscored from a single disagreeing page.** Under
`sectionSplitting: disabled` every page takes the voted class, and a page that
predicted something else keeps no confidence — combined with the minimum rule
below, one disagreement leaves the entire file's section unscored. A page that
produced no verdict of its own is different: it inherits its segment's class
*and* its confidence, so silence does not count as disagreement.

This distinction matters because the aggregated value reaches the reporting lake
as `section_confidence`: a fabricated `1.0` there would be indistinguishable
from a genuinely confident classification and would silently corrupt any
analysis of it.

### Section confidence is the weakest page

A section's confidence is the **minimum** across its pages, and *not scored* if
any of its pages is unscored. A mean would hide the one page the classifier was
unsure about — which is the page a reviewer needs — and a section's class cannot
be more certain than its least certain page.

### Where it shows up

- **Web UI** — a **Class conf.** column in the **Document Pages** table on the
  document detail page, beside `Class/Type` rather than inside it. It **sorts**,
  so you can put the least-confident pages first — which is how you find the ones
  worth a second look. The percentage is a link: click it for **"Why this
  class?"**, the model's own reasoning plus the ranked alternatives it considered
  with their probabilities. A page the model scored but did not explain shows the
  number as plain text; an unscored page shows `—`, never `0%`.

  The **Document Sections** table deliberately does not show a section's class
  confidence. It is an aggregate (the minimum across the section's pages) of
  numbers already listed per page in the table directly below, and two extra
  columns squeezed that table until its own headers wrapped. `Section.Confidence`
  is still on the API and still lands in the reporting lake as
  `section_confidence`. The **Low-conf. fields** column there is a different
  measurement entirely — per-extracted-field confidence, not the class.

  The number is deliberately shown the same way at every value. There is no
  configured classification confidence threshold (unlike extraction fields), so a
  green/amber/red band would assert a pass/fail the system has not defined — and
  on a classifier that answers ~0.95 for most pages it would paint a coarse flag
  as a calibrated traffic light.
- **API** — `Page.ClassConfidence`, `Page.ClassReason`, `Page.ClassCandidates`
  and `Section.Confidence` on `getDocument`, `getDocumentVersion` and the
  document subscription. Named `ClassConfidence` on a page because
  `TextConfidenceUri` there is *OCR* confidence — a different measurement.
- **Reporting / analytics** — the existing `section_confidence` column, which
  now carries a real value when one exists and null when it does not.
- **Document JSON** — `pages.<id>.confidence`, `pages.<id>.classification_reason`
  and `sections[].confidence`, each omitted when absent.

### Calibration: measure it before you act on it

A single self-reported confidence is usually poorly calibrated — models asked
"how sure are you?" answer ~0.95 almost everywhere, which is worse than no score
because it invites automated escalation on noise. Smaller models are the most
overconfident, and the default classification model is a small one. That is why
the shipped mode is `topk` rather than `verbalized`: ranking alternatives against
each other is what makes the number carry information, where a single
self-reported figure mostly does not. `off` is for a deployment that will not act
on the score — it is the only setting that saves the output tokens.

**This is measured, not asserted.** On 298 pages of a deliberately confusable
13-class corpus, in `topk` mode ([full study](./benchmarking/studies/classification-confidence.md)):

| | Nova 2 Lite (default) | Claude Haiku 4.5 |
|---|---|---|
| Page classification accuracy | 0.846 | 0.852 |
| Calibration separation | +0.044 | **+0.207** |
| Mean confidence when **wrong** | 0.903 | 0.741 |
| Distinct confidence values emitted | **6** (90 % of pages at exactly 0.95) | 11 |
| Errors caught / pages reviewed at the best threshold | 43 % / 8 % | **73 % / 11 %** |
| Classification cost per page | $0.00090 | $0.00573 |

Equally accurate; not equally *informative*. Nova 2 Lite answered 0.95 for 90 % of
pages including half its own errors, so its score is a coarse triage flag at best.
Haiku 4.5's is actionable — and costs 6.4× more per page. Decide that trade
deliberately; do not assume the number is useful because it exists.

**You can measure this directly, and you should before wiring it to anything
automatic.** With ground truth available, the evaluation report's per-page
classification details now record the classifier's own confidence next to whether
the class was correct:

```json
{"page_index": 5, "ground_truth_class": "W2", "predicted_class": "Receipt",
 "correct": false, "predicted_confidence": 0.48}
```

The number that matters is the **separation**: mean confidence on correctly
classified pages minus mean confidence on incorrectly classified ones. A healthy
signal separates them clearly; near zero means the model is equally confident
when it is right and when it is wrong, and the score should not be used to route
work no matter how reasonable the individual values look. The benchmark harness
reports this as `class_calibration_separation` alongside `class_accuracy` and
`n_class_scored_pages`, and treats a drop of 0.03 as a regression.

Two cheap signals are already reliable and cost nothing extra:

- a page whose class came back **invalid** and needed a re-prompt (or fell back
  to `fallback_class`) — the classifier demonstrably struggled;
- a page classified as `unclassified` or a blank-page class.

## Regex-Based Classification for Performance Optimization

Pattern 2 now supports optional regex-based classification that can provide significant performance improvements and cost savings by bypassing LLM calls when document patterns are recognized.

### Document Name Regex (All Pages Same Class)

When you want all pages of a document to be classified as the same class, you can use document name regex to instantly classify entire documents based on their filename or ID:

```yaml
classes:
  - $schema: "https://json-schema.org/draft/2020-12/schema"
    $id: Payslip
    x-aws-idp-document-type: Payslip
    type: object
    description: "Employee wage statement showing earnings and deductions"
    x-aws-idp-document-name-regex: "(?i).*(payslip|paystub|salary|wage).*"
    properties:
      EmployeeName:
        type: string
        description: "Name of the employee"
```

**Benefits:**
- **Instant Classification**: Entire document classified without any LLM calls
- **Massive Performance Gains**: ~100-1000x faster than LLM classification
- **Zero Token Usage**: Complete elimination of API costs for matched documents
- **Deterministic Results**: Consistent classification for known patterns

**When document ID matches the pattern:**
- All pages are immediately classified as the matching class
- Single section is created containing all pages
- No backend service calls are made
- Info logging confirms regex match

### Page Content Regex (Multi-Modal Page-Level Classification)

For multi-class configurations using page-level classification, you can use page content regex to classify individual pages based on text patterns:

```yaml
classification:
  one_stage:
    granularity: page

classes:
  - $schema: "https://json-schema.org/draft/2020-12/schema"
    $id: Invoice
    x-aws-idp-document-type: Invoice
    type: object
    description: "Business invoice document"
    x-aws-idp-document-page-content-regex: "(?i)(invoice\\s+number|bill\\s+to|amount\\s+due)"
    properties:
      InvoiceNumber:
        type: string
        description: "Invoice number"
  - $schema: "https://json-schema.org/draft/2020-12/schema"
    $id: Payslip
    x-aws-idp-document-type: Payslip
    type: object
    description: "Employee wage statement"
    x-aws-idp-document-page-content-regex: "(?i)(gross\\s+pay|net\\s+pay|employee\\s+id)"
    properties:
      EmployeeName:
        type: string
        description: "Employee name"
  - $schema: "https://json-schema.org/draft/2020-12/schema"
    $id: Other
    x-aws-idp-document-type: Other
    type: object
    description: "Documents that don't match specific patterns"
    # No regex - will always use LLM
    properties: {}
```

**Benefits:**
- **Selective Performance Gains**: Pages matching patterns are classified instantly
- **Mixed Processing**: Some pages use regex, others fall back to LLM
- **Cost Optimization**: Reduced token usage proportional to regex matches
- **Maintained Accuracy**: LLM fallback ensures all pages are properly classified

**How it works:**
- Each page's text content is checked against all class regex patterns
- First matching pattern wins and classifies the page instantly
- Pages with no matches use standard LLM classification
- Results are seamlessly integrated into document sections

### Regex Pattern Best Practices

1. **Case-Insensitive Matching**: Always use `(?i)` flag
   ```regex
   (?i).*(invoice|bill).*  # Matches any case variation
   ```

2. **Flexible Whitespace**: Use `\\s+` for varying spaces/tabs
   ```regex
   (?i)(gross\\s+pay|net\\s+pay)  # Handles "gross pay", "gross  pay"
   ```

3. **Multiple Alternatives**: Use `|` for different terms
   ```regex
   (?i).*(payslip|paystub|salary|wage).*  # Any of these terms
   ```

4. **Balanced Specificity**: Specific enough to avoid false matches
   ```regex
   # Good: Specific to W2 forms
   (?i)(form\\s+w-?2|wage\\s+and\\s+tax|employer\\s+identification)
   
   # Too broad: Could match many documents
   (?i)(form|wage|tax)
   ```

### Performance Analysis

Use `notebooks/examples/step2_classification_with_regex.ipynb` to:
- Test regex patterns against your documents
- Compare processing speeds (regex vs LLM)
- Analyze cost savings through token usage reduction
- Validate classification accuracy
- Debug pattern matching behavior

### Error Handling

The regex system includes robust error handling:
- **Invalid Patterns**: Compilation errors are logged, system falls back to LLM
- **Runtime Failures**: Pattern matching errors default to LLM classification  
- **Graceful Degradation**: Service continues working with invalid regex
- **Comprehensive Logging**: Detailed logs for debugging pattern issues

### Configuration Examples

**Common Document Types:**
```yaml
classes:
  # W2 Tax Forms
  - $schema: "https://json-schema.org/draft/2020-12/schema"
    $id: W2
    x-aws-idp-document-type: W2
    type: object
    description: "W2 Tax Form"
    x-aws-idp-document-page-content-regex: "(?i)(form\\s+w-?2|wage\\s+and\\s+tax|social\\s+security)"
    properties: {}
    
  # Bank Statements  
  - $schema: "https://json-schema.org/draft/2020-12/schema"
    $id: Bank-Statement
    x-aws-idp-document-type: Bank-Statement
    type: object
    description: "Bank Statement"
    x-aws-idp-document-page-content-regex: "(?i)(account\\s+number|statement\\s+period|beginning\\s+balance)"
    properties: {}
    
  # Driver Licenses
  - $schema: "https://json-schema.org/draft/2020-12/schema"
    $id: US-drivers-licenses
    x-aws-idp-document-type: US-drivers-licenses
    type: object
    description: "US Driver's License"
    x-aws-idp-document-page-content-regex: "(?i)(driver\\s+license|state\\s+id|date\\s+of\\s+birth)"
    properties: {}
    
  # Invoices
  - $schema: "https://json-schema.org/draft/2020-12/schema"
    $id: Invoice
    x-aws-idp-document-type: Invoice
    type: object
    description: "Invoice"
    x-aws-idp-document-page-content-regex: "(?i)(invoice\\s+number|bill\\s+to|remit\\s+payment)"
    properties: {}
```

## Best Practices for Classification

1. **Provide Clear Class Descriptions**: Include distinctive features and common elements
2. **Use Few Shot Examples**: Include 2-3 diverse examples per class
3. **Choose the Right Granularity**: Use page granularity with sequence segmentation for multi-document packets, packet granularity for context-dependent documents
4. **Balance Class Coverage**: Ensure all expected document types have classes
5. **Monitor and Refine**: Use the evaluation framework to track classification accuracy
6. **Consider Visual Elements**: Describe visual layout and design patterns in class descriptions
7. **Test with Real Documents**: Validate classification against actual document samples
8. **Optimize Image Dimensions**: Configure appropriate image sizes based on document complexity and processing requirements
9. **Balance Quality vs Performance**: Higher resolution images provide better accuracy but consume more resources
10. **Consider Output Format**: Use YAML prompts for token efficiency, especially with complex nested responses
11. **Leverage Format Flexibility**: Take advantage of automatic format detection to optimize prompts for different use cases
12. **Understand Boundary Indicators**: Review the `document_boundary` metadata to understand how documents are being segmented
13. **Handle Multi-Document Packets**: Use sequence segmentation when processing files containing multiple documents of the same type
14. **Test Segmentation Logic**: Verify that documents are correctly separated by reviewing section boundaries in the results
15. **Consider Document Flow**: Ensure your document classes account for typical document structures (headers, body, footers)
16. **Leverage BIO-like Tagging**: Take advantage of the automatic boundary detection to eliminate manual document splitting
17. **Use Regex for Known Patterns**: Add regex patterns for document types with predictable content or naming conventions
18. **Test Regex Thoroughly**: Validate regex patterns against diverse document samples before production use
19. **Balance Regex Specificity**: Make patterns specific enough to avoid false matches but flexible enough to catch variations
20. **Monitor Regex Performance**: Track how often regex patterns match vs fall back to LLM classification
