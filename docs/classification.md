---
title: "Customizing Classification"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Customizing Classification

Document classification is a key component of the GenAIIDP solution that categorizes each document or page into predefined classes. This guide explains how to customize classification to best suit your document processing needs.

## Classification Methods Across Patterns

The solution supports multiple classification approaches that vary by pattern:

### Pattern 1: BDA-Based Classification

- Classification is performed by the BDA (Bedrock Data Automation) project configuration
- Uses BDA blueprints to define classification rules
- Not configurable inside the GenAIIDP solution itself
- Configuration happens at the BDA project level

### Pattern 2: Bedrock LLM-Based Classification

Pattern 2 offers two main classification approaches, configured through different templates:

#### MultiModal Page-Level Classification with Sequence Segmentation (default)

- Classifies each page independently using both text and image data
- **Uses sequence segmentation with BIO-like tagging for document boundary detection**
- **Each page receives both a document type and a boundary indicator ("start" or "continue")**
- **Automatically segments multi-document packets where multiple documents may be combined**
- Works exceptionally well for complex document packets containing multiple documents of the same or different types
- Supports optional few-shot examples to improve classification accuracy
- Deployed when you select 'few_shot_example_with_multimodal_page_classification' during stack deployment
- See the [few-shot-examples.md](./few-shot-examples.md) documentation for details on configuring examples

##### Sequence Segmentation Approach

The multimodal page-level classification implements a sophisticated sequence segmentation approach similar to BIO (Begin-Inside-Outside) tagging commonly used in NLP. This enables accurate segmentation of multi-document packets where a single file may contain multiple distinct documents.

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

The multimodal page-level classification supports including surrounding pages as context to improve classification accuracy. This is particularly useful when a single page doesn't contain enough information to determine its document type or boundary status.

**Configuration:**

```yaml
classification:
  classificationMethod: multimodalPageLevelClassification
  contextPagesCount: 1  # Include 1 page before and 1 page after as context
  # contextPagesCount: 0  # Default: no additional context (current behavior)
  # contextPagesCount: 2  # Include 2 pages before and 2 pages after
```

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

`classification.task_prompt` now carries a `<boundary-detection-rules>` block that
asks a question a single page **can** answer — is this page a *first* page? — from
its own evidence, in priority order: pagination (`Page 2 of 2` ⇒ `continue`), then
the presence of an opening identity block, then continuation evidence. Two clauses
in it are a matched pair and must not be removed independently:

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
- rule 3 (opening header block ⇒ `start`) is evaluated **before** rule 4
  (continuation evidence ⇒ `continue`), and the repeated running header satisfies
  it — especially when the reprinted column headers themselves carry a date
  (`NAV as of 10/31/2024`), which rule 3 lists as page-1 evidence;
- `contextPagesCount: 0` (the default) means rule 4's *"a table continuing from a
  previous page"* has no preceding page to compare against.

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
| shipped rules | **0/10** | 7/10 | 10/10 | 10/10 |
| + *"a running header is not an opening block"* | — | 4/10 ↓ | 10/10 | 10/10 |
| + *"a bare page number > 1 is decisive"* | 10/10 | 2/10 ↓ | 10/10 | 10/10 |
| + both | 10/10 | 0/10 ↓ | 10/10 | 10/10 |
| `contextPagesCount: 1`, rules unchanged | 10/10 | 10/10 | 10/10 | **5/10 ↓** |
| **class-description `BOUNDARY:` sentence** | **10/10** | unaffected | unaffected | unaffected |

`contextPagesCount: 1` is the right lever if your corpus contains no back-to-back
copies of the same form — it fixes both over-split directions with the shipped
prompt, at the cost of the over-merge direction and roughly 3× the image tokens
per classification call.

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

⚠️ **A stored or preset `classification.task_prompt` overrides the default, so it
does not get these rules.** If you customized the prompt, re-apply the block or
reset to the default. The presets shipped in `config_library/` are kept in sync by
`scripts/tests/test_classification_prompt_copies_in_sync.py`.

##### Enforcing a Valid Class Vocabulary (Validation + Retry)

With a fixed set of classes, a language model can occasionally return a label
that is **not** in your configured list (for example predicting `receipt` when
only `invoice`, `w2`, and `check` are valid). Smaller / cheaper models are
especially prone to this. `multimodalPageLevelClassification` includes a
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

   > **`temperature=0` is not universal.** Claude Opus 4.7/4.8, Opus 5 and
   > Sonnet 5 **reject** `temperature`/`top_p`/`top_k` (HTTP 400), so
   > `idp_common` strips them before the request (`_CLAUDE_4_7_BASE_NAMES` in
   > `idp_common/bedrock/client.py`). On those models classification **samples**,
   > and identical inputs can return different answers run to run. This
   > documentation previously stated the parameter applied everywhere, and that
   > assumption is what made [#653](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/653)
   > — intermittent page-boundary misdetection — look like a model defect rather
   > than a prompt one.
3. The retry repeats up to `maxValidationRetries` times.
4. If all retries are exhausted, the page is assigned `invalidClassFallback`
   (default `unclassified`) and flagged with a `validation_error` entry in its
   classification metadata. The document continues processing — there is no
   hard failure.

```yaml
classification:
  classificationMethod: multimodalPageLevelClassification
  enforceValidClasses: true       # Validate + retry on invalid class (default: true)
  maxValidationRetries: 2         # Re-prompt up to N times (default: 2)
  invalidClassFallback: unclassified  # Class used when retries are exhausted (default: unclassified)
```

| Setting | Default | Description |
|---------|---------|-------------|
| `enforceValidClasses` | `true` | When `true`, validate the predicted class and re-prompt on out-of-vocabulary results. When `false`, an invalid class is logged and used as-is (legacy behavior). |
| `maxValidationRetries` | `2` | Maximum number of re-prompts. `0` disables retries (a single invalid prediction goes straight to the fallback). |
| `invalidClassFallback` | `unclassified` | Class assigned when retries are exhausted. Set to one of your defined classes, or the built-in `unclassified`. |

> **Behavior change on upgrade:** enforcement is **on by default**. An
> out-of-vocabulary prediction that previously passed through unchanged is now
> corrected or coerced to the fallback class. Set `enforceValidClasses: false`
> to restore the prior "warn and use as-is" behavior.
>
> **Catch-all class:** if you want an explicit "other"/"unknown" bucket, define
> it as one of your classes — the model will then be able to select it
> legitimately, and it counts as a valid prediction.
>
> **Scope:** this loop applies to `multimodalPageLevelClassification`.
> Text-based holistic classification has similar needs but is not covered yet.

A runnable demonstration (forcing an out-of-vocabulary prediction and showing
the retry correct it) is available in
`notebooks/misc/classification-valid-class-enforcement.ipynb`.

#### Text-Based Holistic Classification

- Analyzes entire document packets to identify logical boundaries
- Identifies distinct document segments within multi-page documents
- Determines document type for each segment
- Better suited for multi-document packets where context spans multiple pages
- Deployed when you select the default pipeline mode configuration during stack deployment or update

The default configuration in `config_library/unified/default/config.yaml` implements this approach with a task prompt that instructs the model to:

1. Read through the entire document package to understand its contents
2. Identify page ranges that form complete, distinct documents
3. Match each document segment to one of the defined document types
4. Record the start and end pages for each identified segment

Example configuration:

```yaml
classification:
  classificationMethod: textbasedHolisticClassification
  model: us.amazon.nova-pro-v1:0
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

##### Limitations of Text-Based Holistic Classification

Despite its strengths in handling full-document context, this method has several limitations:

**Context & Model Constraints:**: 
- Long documents can exceed the context window of smaller models, resulting in request failure.
- Lengthy inputs may dilute the model’s focus, leading to inaccurate or inconsistent classifications.
- Requires high-context models such as Amazon Nova Premier, which supports up to 1 million tokens. Smaller models are not suitable for this method.
- For more details on supported models and their context limits, refer to the [Amazon Bedrock Supported Models documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/models-supported.html).

**Scalability Challenges**: Not ideal for very large or visually complex document sets. In such cases, the Multi-Modal Page-Level Classification method is more appropriate.

### Pattern 3: UDOP-Based Classification

- Classification is performed by a pre-trained UDOP (Unified Document Processing) model
- Model is deployed on Amazon SageMaker
- Performs multi-modal page-level classification (classifies each page based on OCR data and page image)
- Not configurable inside the GenAIIDP solution

## Section Splitting Strategies

The `sectionSplitting` configuration controls how classified pages are grouped into document sections. This setting works with both classification methods and provides three strategies:

### Available Strategies

#### 1. `disabled` - No Splitting (Entire Document = One Section)

**Behavior:**
- All pages are assigned to a single section
- Uses **majority voting** to determine the document class (most common classification wins)
- Excludes unclassifiable/blank pages from voting to prevent them from affecting the result
- If there's a tie, uses the first page's classification for determinism
- Ignores any page-level classification boundaries

**Use Cases:**
- Documents known to be single-type with no internal divisions
- Simplified processing where granular section splitting isn't needed
- When you want to force all pages to be treated as one cohesive document
- **Documents with occasional blank or unclassifiable pages** (these won't affect the final classification)

**Configuration Example:**
```yaml
classification:
  sectionSplitting: disabled
  classificationMethod: multimodalPageLevelClassification
```

**Result:**
- Document with 10 pages → 1 section containing all 10 pages
- All pages assigned the most common (voted) class

**Voting Behavior:**

The `disabled` strategy uses majority voting to determine the document classification, which provides robust handling of edge cases:

1. **Config-Driven Voting**: Only pages whose classification matches a valid document type defined in your configuration are eligible to vote. This automatically excludes:
   - Blank pages (`unclassifiable_blank_page`, `blank`, etc.)
   - Error states (`error (backoff/retry)`, `unclassified`)
   - LLM hallucinations or typos that don't match any defined class

2. **Majority Wins**: The classification that appears most frequently among votable pages becomes the document classification.

3. **Tie-Breaking**: If multiple classifications have the same count, the classification from the earliest page (by page number) is used for determinism.

4. **Fallback**: If no pages have valid classifications (all are unclassifiable types), the first page's classification is used.

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

**Use Cases:**
- **Critical for long documents with multiple same-type forms** (e.g., multiple W-2 forms, multiple invoices)
- When LLM boundary detection is unreliable or fails frequently
- Government form processing where each form must be processed independently
- Scenarios where deterministic splitting is required

**Configuration Example:**
```yaml
classification:
  sectionSplitting: page
  classificationMethod: multimodalPageLevelClassification
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
  sectionSplitting: llm_determined  # This is the default
  classificationMethod: multimodalPageLevelClassification
```

**Result:**
- Document with 10 pages → Variable number of sections based on LLM boundary detection
- Pages grouped according to document boundaries and type changes

### Strategy Comparison Table

| Strategy | Sections Created | Boundary Detection | Same-Type Handling | Deterministic | Performance |
|----------|-----------------|-------------------|-------------------|---------------|-------------|
| `disabled` | 1 section always | None | All joined | Yes | Fastest |
| `page` | N sections (N pages) | Per-page | Never joined | Yes | Fast |
| `llm_determined` | Variable | LLM boundaries | May join | No | Standard |

### Configuration Placement

The `sectionSplitting` setting is placed in the classification configuration section:

```yaml
classification:
  model: us.amazon.nova-pro-v1:0
  classificationMethod: multimodalPageLevelClassification
  sectionSplitting: page  # Options: disabled, page, llm_determined
  maxPagesForClassification: "ALL"
  temperature: "0.0"
  # ... other classification settings
```

### Interaction with Classification Methods

The `sectionSplitting` setting works with both classification methods:

**With `multimodalPageLevelClassification`:**
- `disabled`: First page's class applies to all pages in one section
- `page`: Each page's individual classification preserved in separate sections
- `llm_determined`: Pages grouped by class + boundary metadata

**With `textbasedHolisticClassification`:**
- `disabled`: First segment's class applies to all pages in one section
- `page`: Each page gets its own section with the class assigned by holistic method
- `llm_determined`: LLM-determined segments used as sections (default behavior)

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
| **Assessment** (confidence) | Returns without writing anything (no extraction results exist to assess). |
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

## Choosing Between Classification Methods

When deciding between Text-Based Holistic Classification and MultiModal Page-Level Classification with Sequence Segmentation, consider these factors:

### Use Text-Based Holistic Classification When:
- Documents have clear logical boundaries based on content
- Text context spans multiple pages and requires understanding the full document
- You have access to high-context models (e.g., Amazon Nova Premier)
- Document packets are relatively small (within model context limits)
- Visual elements are less important than textual continuity

### Use MultiModal Page-Level Classification with Sequence Segmentation When:
- **Document packets contain multiple documents of the same type** (e.g., multiple invoices)
- **Visual layout and image content are important for classification**
- **You need to process very large document packets** that might exceed context limits
- **Documents have clear visual boundaries** (headers, footers, different layouts)
- **You want to leverage both text and image information** for better accuracy
- **Processing speed is important** (parallel page processing is possible)

### Comparison Table

| Feature | Text-Based Holistic | MultiModal Page-Level with Sequence Segmentation |
|---------|-------------------|--------------------------------------------------|
| Context Awareness | Full document context | Page-level with boundary detection |
| Multi-document Packets | Good | Excellent (handles same-type documents) |
| Visual Processing | Text only | Text + Images |
| Model Requirements | High-context models | Standard models |
| Processing Speed | Sequential | Can be parallelized |
| Boundary Detection | Content-based | BIO-like tagging |
| Large Documents | Limited by context | No practical limit |

## Customizing Classification in Pattern 2

### Configuration Settings

#### Page Limit Configuration

Control how many pages are used for classification:

```yaml
classification:
  maxPagesForClassification: "ALL"  # Default: use all pages
  # Or: "1", "2", "3", etc. - use only first N pages
```

**Important**: When set to a number (e.g., `"3"`), only the first N pages are classified, but the result is applied to ALL pages in the document. This forces the entire document to be assigned a single class with one section.

### Prompt Components

In Pattern 2, you can customize classification behavior through various prompt components:

### System Prompts

Define overall model behavior and constraints:

```yaml
system_prompt: |
  You are an expert document classifier specializing in financial and business documents.
  Your task is to analyze document images and classify them into predefined categories.
  Focus on visual layout, textual content, and common patterns found in each document type.
  When in doubt, analyze the most prominent features like headers, logos, and form fields.
```

### Task Prompts

Specify classification instructions and formatting:

```yaml
task_prompt: |
  Analyze the following document page and classify it into one of these categories: 
  {{document_classes}}
  
  Return ONLY the document class name without additional explanations.
  If the document doesn't fit any of the provided classes, classify it as "other".
```

### Class Descriptions

Provide detailed descriptions for each document category:

```yaml
document_classes:
  invoice:
    description: "A commercial document issued by a seller to a buyer, related to a sale transaction and indicating the products, quantities, and agreed prices for products or services."
  receipt:
    description: "A document acknowledging that something of value has been received, often as proof of payment."
  bank_statement:
    description: "A document issued by a bank showing transactions and balances for a specific account over a defined period."
```

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
  task_prompt: |
    Analyze this document:
    
    {DOCUMENT_TEXT}
    
    Classify it as one of: {CLASS_NAMES_AND_DESCRIPTIONS}
```
Images are automatically appended after the text content.

**With Placeholder (Controlled Placement):**
```yaml
classification:
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

- **No Image Limits**: All document pages are processed following Bedrock API removal of image count restrictions
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

### Rendered output (page-level classification)

In the page-level (`multimodalPageLevelClassification`) path, the
placeholder renders as one XML block per class:

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

### Rendered output (holistic packet classification)

In the holistic (`textbasedHolisticClassification`) path the same
placeholder renders as a markdown table — matching the format of the
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

Pattern 2's multimodal page-level classification supports few-shot example prompting, which can significantly improve classification accuracy by providing concrete document examples. This feature is available when you select the 'few_shot_example_with_multimodal_page_classification' configuration.

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

### New Default Behavior (Preserves Original Resolution)

**Important Change**: Empty strings or unspecified image dimensions now preserve the original document resolution for maximum classification accuracy:

```yaml
classification:
  model: us.amazon.nova-pro-v1:0
  # Image processing settings - preserves original resolution
  image:
    target_width: ""     # Empty string = no resizing (recommended)
    target_height: ""    # Empty string = no resizing (recommended)
```

### Custom Image Dimensions

Configure specific dimensions when performance optimization is needed:

```yaml
# For high-accuracy classification with controlled dimensions
classification:
  image:
    target_width: "1200"   # Resize to 1200 pixels wide
    target_height: "1600"  # Resize to 1600 pixels tall

# For fast processing with lower resolution
classification:
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

### Migration from Previous Versions

**Previous Behavior**: Empty strings defaulted to 951x1268 pixel resizing
**New Behavior**: Empty strings preserve original image resolution

If you were relying on the previous default resizing behavior, explicitly set dimensions:

```yaml
# To maintain previous default behavior
classification:
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
  task_prompt: |
    Classify this document and respond with JSON:
    {"class": "invoice", "confidence": 0.95}

# YAML response (more token-efficient)
classification:
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
  system_prompt: |
    You are a document classifier. Respond only with JSON format.
  task_prompt: |
    Classify this document and return a JSON object with the class name and confidence score.
```

**YAML-focused prompt:**
```yaml
classification:
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

> **Two of the keys in the examples above — `confidence` and
> `classification_reason` — used to be parsed by nothing.** A prompt could ask
> for them, the model would answer, and the values were discarded. They are now
> read and persisted ([#673](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/673)).

### Where a score comes from

There is no separate confidence inference for classification: the value comes out
of the same response as the class. `classification.confidence.mode` composes the
instruction into the prompt for you — it is **on by default** — and a custom
prompt that asks for `confidence` or `candidates` itself is honoured either way,
because the response parser is not gated on the setting.

Editable in the Web UI under **Configuration → Classification → Class
confidence**, or in YAML:

```yaml
classification:
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
[classification-confidence benchmark](./benchmarking/classification-confidence.md).

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
(`classification.confidence.task_prompt_topk` /
`task_prompt_verbalized`), like every other prompt here.

Writing it into your own prompt works too, and is the only option for
`textbasedHolisticClassification` (whose response is a segment list, not one
object per page — the setting logs a warning and composes nothing there, but a
per-segment `confidence` key is parsed if your prompt asks for one):

```yaml
classification:
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
| SageMaker/UDOP backend | *not scored* (the endpoint returns no score) |
| Page classification errored | `0.0` |

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
`topk` is recommended over `verbalized`, and why the whole block is off by
default.

**This is measured, not asserted.** On 298 pages of a deliberately confusable
13-class corpus, in `topk` mode ([full study](./benchmarking/classification-confidence.md)):

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
  to `invalidClassFallback`) — the classifier demonstrably struggled;
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
  classificationMethod: multimodalPageLevelClassification

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
3. **Choose the Right Method**: Use page-level with sequence segmentation for multi-document packets, holistic for context-dependent documents
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
