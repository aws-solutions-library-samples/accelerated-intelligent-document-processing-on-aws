---
title: "Decision Models"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Decision Models

A **decision model** picks between options or rates on a scale. It returns a
probability for every option instead of generated text. Use one in place of an LLM
call when the question has a closed set of answers:

- There is no output to parse, and no answer outside the options.
- The confidence comes from the model's distribution over the options, not from a
  number the model writes out, so it can be used to route work without asking the
  model how sure it is.
- It is a small model you run in your own account, with no labelled training data:
  it reads the class descriptions you already have.

The accelerator calls decision models over the **System One API**, the request the
[Strands Decider](https://github.com/strands-labs/strands-decider) serves:
a `state` (text, page images, or both) and a set of typed questions.

| Question type | Criteria | Answer |
|---|---|---|
| `noul` (yes / no) | optional descriptions of `true` and `false` | the probability the statement is true |
| `choice` | 2 to 255 named options, each with a description | the chosen option and every option's probability |
| `score` | 2 to 10 ordered levels, lowest first | the expected level and every level's probability |

## Where you can use one

| Step | Setting | What is asked |
|---|---|---|
| Section classification | `classification.split_then_classify.section_classification.method: decision_model` | one `choice` question per section: the options are your classes, each described by its `description` |
| Extraction confidence | `extraction.confidence.method: decision_model` | one `noul` question per extracted value, asked against each page of the section |

Section classification is the second call of the
[split-then-classify strategy](./classification.md): splitting still finds where
each document starts, with the LLM, and the decision model names each section.

## Setting it up

### 1. Deploy a decision model on Lambda

The quickest way is to install the
[Decision Model: Strands Decider](./extensions/decision-model-decider.md)
extension from the **Extensions** catalog. It builds the Strands Decider's Lambda
image in your account from pinned source and model commits, deploys it as
`GENAIIDP-decider` with images enabled, and adds an inactive configuration profile,
`decision-model-decider`, that selects it for section classification. Its stack's
`DeciderEndpoint` output is the ARN for step 2.

To deploy it yourself instead, deploy the Strands Decider's Lambda recipe in the
account and region of your stack.
Follow its [Lambda guide](https://github.com/strands-labs/strands-decider/tree/main/deploy/lambda)
and deploy it **with images** (`-c vision=true`), because section classification
works best on page images.

⚠️ **Name the function with the `GENAIIDP-` prefix.** The pipeline's Lambda
functions (classification, extraction including Advanced extraction's shard runtime,
and assessment) may invoke only functions whose name starts with `GENAIIDP-`, the same
rule [Lambda Hook Inference](./lambda-hook-inference.md) uses, so a decision model
under another name is refused with `AccessDeniedException`. Set the function's name
when you deploy it. Use the function's ARN from your own console: `<partition>` is
`aws` in commercial regions and `aws-us-gov` in GovCloud.

Pin the model to a dated revision (`-c model_revision=<YYMMDD>`) rather than `main`,
so a redeploy serves the weights you measured.

### 2. Select it in the configuration

In the web UI, open **Configuration**, set **Classification strategy** to *Split
first, then classify each section*, and under **Section classification** set the
method to **Decision model**. Or in YAML:

```yaml
classification:
  strategy: split_then_classify
  split_then_classify:
    section_classification:
      method: decision_model
      pages: first             # which pages represent a section
      decision_model:
        endpoint: arn:<partition>:lambda:<region>:<account>:function:GENAIIDP-decider
        input: images          # images (default) | text | both
        max_pages: 1           # 1 to 4: a request carries at most 4 images
        max_input_tokens: 3584 # text past this is cut and never read
        instructions: Which type of document is this?
        timeout_ms: 300000
        max_concurrency: 4
      low_confidence:
        min_confidence: 0.0    # 0 keeps every answer
        on_low_confidence: fallback_class   # or llm
```

| Setting | Default | Meaning |
|---|---|---|
| `endpoint` | required | The function ARN. The name must start with `GENAIIDP-`. A configuration that selects the decision model without one is refused when it is saved. |
| `input` | `images` | What the model reads. Page images measured best: OCR text alone loses the visual cues that tell forms, letters and memos apart. |
| `max_pages` | `1` | The most pages of a section the model reads, after `pages` selects them. The maximum is 4, the API's image limit. More pages did not raise accuracy and weakened the confidence. |
| `max_input_tokens` | `3584` | The most tokens one request may hold: the section's OCR text, its page images and the question with every class description. See [Text longer than the window](#text-longer-than-the-window). |
| `instructions` | `Which type of document is this?` | The question. The options are always your classes. |
| `timeout_ms` | `300000` | How long to wait for one answer. Without SnapStart, the first request to a new execution environment waits for the model to load: about 3 minutes for the first after a deploy with vision on, and tens of seconds after that. A timed-out call is not retried, because the invocation may still be running. |
| `max_concurrency` | `4` | The most calls in flight from one document. Each call occupies one 10 GB execution environment. |
| `low_confidence` | off | What an answer below `min_confidence` does: `fallback_class` gives the section `classification.fallback_class`; `llm` classifies it again with the LLM settings. |

**Write good class descriptions.** The model compares each section against the
descriptions, so a description is the whole definition of its class. Descriptions
also count against the model's context window and `max_input_tokens`, so long ones
leave less room for a section's text.

### 3. Or use it for extraction confidence

Confidence scoring asks whether each extracted value is right. With
`method: decision_model`, every value becomes one yes/no question ("the system says
the field *Total* is *100.00*; is that the correct value?"), each page of the section
is one request carrying all of them, and a value's confidence is its **highest
probability across the pages**. Thresholds, alerts and human review then work exactly
as with the LLM.

An **empty value** is asked the other way round: does the page show a value for the
field? Its confidence is **one minus the highest probability across the pages**, so
a value the extraction missed scores low, and a field no page shows scores high. As
with the LLM, an empty value raises no confidence alert; see
[Absent values and missing required fields](./extraction-and-confidence.md#absent-values-and-missing-required-fields).

```yaml
extraction:
  confidence:
    mode: separate             # required: integrated mode has no confidence pass
    method: decision_model
    decision_model:
      endpoint: arn:<partition>:lambda:<region>:<account>:function:GENAIIDP-decider
      input: images            # images (default) | text | both
      max_pages: 20            # 1 to 50: pages read per section, one request each
      max_questions_per_request: 100
      max_input_tokens: 3584   # page text past this is cut and never read
```

| Setting | Default | Meaning |
|---|---|---|
| `input` | `images` | What each request reads; every mode is one request per page. `images`: the page image, which measured best. `text`: that page's OCR text. `both`: the page image with that page's OCR text. Adding the OCR text or the extraction to the state measured worse. |
| `max_pages` | `20` | The most pages read per section. A value that appears only on a later page scores low and goes to review, and an empty value is checked against the pages read only. |
| `instructions` | the question above | `{FIELD}` is the field's path (`Lines[2].Amount`), `{DESCRIPTION}` its schema description, `{VALUE}` the extracted value. A value the extraction left empty is asked `empty_value_instructions` instead. |
| `empty_value_instructions` | the question above | The question for an empty value: whether the page shows a value for the field. `{FIELD}` and `{DESCRIPTION}` as above. Yes must mean a value is shown, because the empty value's confidence is one minus the highest answer. |
| `max_questions_per_request` | `100` | A page with more values is asked in several requests, inside the model's request limit. |
| `max_input_tokens` | `3584` | The most tokens one request may hold: a page's OCR text, its image and the longest question. See [Text longer than the window](#text-longer-than-the-window). |

It needs `extraction.geometry.mode: ocr_only`, the default, which locates each value
from OCR. In `llm` and `llm_grounded` the boxes come from the confidence call, and a
decision model returns none, so those combinations are refused when the configuration
is saved.

Each value's explainability entry carries `confidence_source: decision_model` and a
reason naming the page that supported it best. A failed call fails the confidence
pass, as a failed Bedrock call does; it never becomes an LLM call. The section keeps
its extracted values and gets no confidence scores, with an
`assessment_failed_confidence_unavailable` issue in its **Status** column and its
Processing Report. A refusal is logged at warning with the section and page, and the
issue's root cause names the refused pages.

## Text longer than the window

The decision model reads at most 4,096 tokens per request, and it refuses a longer
request rather than shortening it. So with `input: text` or `both`, each request's
text is cut to fit `decision_model.max_input_tokens` before it is sent:

- **Extraction confidence** sends one request per page, so each page's OCR text is
  cut on its own.
- **Section classification** joins the section's pages (up to `max_pages`) into one
  text and cuts it from the end, so the first page is kept whole when it fits.

**Text beyond the limit is cut and never read by the decision model.** A value or a
class cue that appears only in the cut part is not seen: such a value scores low and
goes to review, an empty field is not checked against that part, and a class cue
there does not count. The cut falls at a line break when there is one near the end
of the text that fits (within its last tenth); otherwise it falls mid-line, so a page
of dense text with few line breaks, such as CJK text or one long OCR line, still
keeps most of the limit's worth. Each cut is logged with the page and the number of
characters dropped.

A request whose text would be cut to nothing or almost nothing, because its question
and images already fill most of the limit, is **not sent**. That includes a section
whose cut would keep only its `Page N:` lines, or under half of the text the limit
allows besides them. A request whose question and images alone are over the limit is not
sent either, in every `input` mode including `images`, and the error says so. Such a
request fails like a refused request, so
extraction confidence degrades the section to no confidence scores and section
classification labels the section `unclassified`. Answering it with no text would
mean scoring an empty field, or picking a class, from no evidence. A limit that
the question and images fill on their own is refused when the configuration is
saved.

`max_input_tokens` counts the request's text, its images and its question (for
classification, every class description) together. The count is an estimate from
characters, because the model's tokenizer is not available to the pipeline, and it
errs high. The default, 3584, is the 4,096-token window less 512 for the model's own
prompt, and it is also the largest value used: a larger one would let dense text
overflow the window, so it is capped at 3584 with a warning in the logs. Lower it if
requests are still refused.
`input: images` sends no text, so nothing is cut, but the question and images are
still checked against the limit.

## What you get

For section classification:

- The section's class, and its **confidence: the probability the model gave that
  class**.
- Every class's probability, in the section's classification candidates.
- A classification reason naming the decision model and the probability, so the
  source of a confidence is visible in the document's details.
- Cost under `SectionClassification/decision_model/<function>` in the cost table,
  and `Assessment/decision_model/<function>` for confidence.

## Failures

A configuration that selects a decision model uses only the decision model:

- **A refusal fails its section.** The model refuses a request over its context
  window or with more images than it reads, and the error says so with the token
  counts. It never shortens the input to fit.
- **A request that never ran retries the classification step.** Throttling or a
  capacity refusal that outlasts the client's bounded retries, or a connection that
  never opened, fails the step with a transient error, which the state machine
  retries.
- **Any other call that does not complete fails its section.** That includes a
  read timeout, a dropped connection and a Lambda reply that names no error type
  (a bare HTTP 5xx or 429), because the invocation may still be running and
  billing. For decision-model confidence the same failures leave the section
  without confidence scores rather than retrying the assessment step, and only a
  request that never ran retries it.
- **Nothing falls back to the LLM on an error.** `low_confidence: llm` applies only
  to answers below `min_confidence`.

A failed section keeps its boundaries and is labelled `unclassified`, exactly as when
an LLM section call fails.

## Cost and scaling

Lambda bills each call by duration at 10 GB.

- **Section classification** makes one call per section, so a packet of N sections
  makes N calls.
- **Extraction confidence** makes, per section, `pages × ⌈values ÷
  max_questions_per_request⌉` calls, where `pages` is the section's page count up to
  `max_pages`. A 20-page section with
  fewer than 100 extracted values is 20 calls, up
  to `max_concurrency` of them at once, each a 10 GB execution environment. Raising
  `max_pages` raises the cost of every long section in proportion. With SnapStart off, a new
execution environment loads the model before it answers; with it on, the snapshot is
billed for as long as the function version exists. See the recipe's guide for both.

`config_library/pricing.yaml` prices `decision_model/GENAIIDP-decider` by request and
GB-second. A function under another name records no cost until you add an entry
under its name.

## Measuring it on your documents

The classification benchmark compares a decision model with the LLM on the same
splitting call:

```bash
python3 benchmarks/harness/run_classification_bench.py --stack <stack> \
  --testset docsplit --n 20 --models nova2lite \
  --decision-model-endpoint <GENAIIDP- function ARN> \
  --variants page-both,split-then-classify-page,split-then-classify-page-decider
```

`split-then-classify-page-decider` differs from `split-then-classify-page` only in
who names the sections. Read `label_accuracy` for the labelling half and
`class_calibration_separation` for whether the confidence separates right answers
from wrong ones.

## Developer reference

The client, the wire types and the failure contract are documented in
[`idp_common/decision_model`](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/lib/idp_common_pkg/idp_common/decision_model/README.md).
