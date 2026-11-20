Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Decision models

A decision model picks between options or rates on a scale. It returns a probability
for each option rather than generated text, so there is no output to parse, and the
confidence comes from a distribution instead of being a number the model writes out.

This package is the client the pipeline uses to call one. User-facing
documentation: [docs/decision-models.md](../../../../docs/decision-models.md). It is used in place of an
LLM call wherever the question is closed:

| Call site | Configuration | Question |
|---|---|---|
| Section classification | `classification.split_then_classify.section_classification.method: decision_model` | one `choice` per section over the configured classes |
| Extraction confidence | `extraction.confidence.method: decision_model` | one `noul` per extracted field, per page |

## The contract: the System One API

`wire.py` holds the request and response types, written from the specification of
the `POST /v1/systemone` request the [Strands Decider](https://github.com/strands-labs/strands-decider)
serves. It does not import that package. The wire contract is the stable part across
the decider's releases, and it is the only thing this repository depends on.

```python
from idp_common.decision_model import (
    ChoiceQuestion, NoulQuestion, SystemOneRequest, client_from_config,
)

request = SystemOneRequest(
    state="<the section's text>",           # or structured data
    images=[...],                           # optional: base64, at most 4
    questions={
        "doc_class": ChoiceQuestion(
            instructions="Which type of document is this?",
            criteria={"invoice": "A bill for goods or services", "letter": "..."},
        ),
        "legible": NoulQuestion(instructions="Is the page legible?"),
    },
)
result = client_from_config(cfg.decision_model).decide(request, "SectionClassification")
result.response.answers["doc_class"].choice          # "invoice"
result.response.answers["doc_class"].probabilities   # {"invoice": 0.91, ...}
result.response.answers["legible"].noul              # P(true)
result.metering   # {"SectionClassification/decision_model/<fn>": {...}}
```

| Type | Criteria | Answer |
|---|---|---|
| `noul` | optional `{"true": ..., "false": ...}` | `noul`: P(true) |
| `choice` | option → description, 2 to 255 options | `choice`, `probabilities`, `confidence` |
| `score` | ordered level labels, 2 to 10, lowest first | `score` (expected level index), `legend`, `probabilities`, `confidence` |

⚠️ A `choice` answer's `confidence` is the normalised max-probability,
`(N·p_max − 1)/(N − 1)`. It is **not** the chosen option's probability, which is
`probabilities[choice]`. A caller that wants "how likely is this answer right" reads
the probability, not `confidence`.

The state is encoded once per request, and every question reads that encoding, so
asking many questions about one state costs little more than asking one. Batch the
questions about one page into one request.

## Fitting the window: `max_input_tokens`

The state, the request's images and the longest question must fit the model's
window together: `CONTEXT_WINDOW_TOKENS`, 4,096 in the decider's v1 release. The
decider refuses a longer request and never shortens the state, so both call sites
cut the state before sending, against their `decision_model.max_input_tokens`
setting (#2032). `budget.py` holds the pieces:

| Name | What it is |
|---|---|
| `estimate_tokens(text)` | An **estimate** from characters, since this package has no tokenizer: one token per digit or non-ASCII character, one per two other characters. It errs high. |
| `PROMPT_RESERVE_TOKENS` | 512, left in the window for the decider's own prompt |
| `DEFAULT_MAX_INPUT_TOKENS` | the window less that reserve, 3584: the setting's default and the largest value it takes: `idp_common.config.models` caps a larger one, with a warning (#2111) |
| `IMAGE_TOKENS` | 256 counted per image (196 for a square page at 448 px) |
| `state_budget(max_input_tokens, question_tokens, images)` | the text a request can carry beside its question and images |
| `LINE_BREAK_BACKOFF_SHARE` | 0.1: a cut ends at a line break only when the break is in the last tenth of the prefix that fits; otherwise it falls mid-line |
| `MIN_EVIDENCE_SHARE` | 0.5: the least share of the budget a cut must keep besides its header lines |
| `truncate_to_tokens(text, budget)` | `(kept, characters dropped)`: the text cut from the end, at a line break within `LINE_BREAK_BACKOFF_SHARE` of the cut and mid-line otherwise; unchanged when it fits |
| `fit_text(text, budget, what, headers=())` | what both call sites send: the text cut by `truncate_to_tokens`, each cut logged at info naming `what`. `headers` are lines that are not evidence (section classification passes its `Page N:` lines). A cut whose kept text, less every header and any header it ends partway through, is blank or under `MIN_EVIDENCE_SHARE` of the budget raises `DecisionModelRefused` (logged at warning) instead of being sent. Text that fits is returned unchanged |
| `require_room(max_input_tokens, question_tokens, images, what)` | raises `DecisionModelRefused` (logged at warning) when the question and images alone are over `max_input_tokens`. Both call sites run it before building a request, in every `input` mode, so an `images` request that would overflow is refused locally rather than sent |

**Text beyond the limit is cut and never read by the decision model**, so a value
or class cue that appears only in the cut part is not seen. Extraction confidence
cuts each page's text on its own
([assessment README](../assessment/README.md#confidence-by-a-decision-model-assessmentdecision_model_confidencepy));
section classification cuts the section's joined text from the end, so its first
page stays whole when it fits (`classification/decision_model_labeler.py`). Both
go through `fit_text`, and both check the question and images with `require_room`
first.

### Checking the constants against a deployed decider

`CONTEXT_WINDOW_TOKENS`, `PROMPT_RESERVE_TOKENS` and `IMAGE_TOKENS` describe the
remote model, and the unit suites cannot check them: their fake decision-model
Lambdas (`tests/unit/decision_model/window_contract.py`) refuse any request over
the window by the same estimate, so they check that callers cut to the budget, not
that the budget is right. `make stacktest-decision-window`
(`scripts/sdlc/decision_model_window_check.py`) asks a deployed Strands Decider
instead. It measures what the decider adds around a state and what a page image
costs, sends one request just under the window and one just over it, then sends
the largest request each call site builds, with its own code, at the largest
`max_pages` and the `max_input_tokens` cap. It fails when a request is refused for
its size, when the decider's `usage.input_tokens` exceeds the estimate plus
`PROMPT_RESERVE_TOKENS`, or when the window, the prompt or an image's cost no
longer matches its constant. Run it after changing any of them, the estimate, a
call site's request, or the decider's pinned model. It is live and on demand, in
neither CI; [run-stack-tests](../../../../.claude/skills/run-stack-tests.md) has
the options.

What three runs measured, in us-east-1 in November 2026, against the extension's
pinned model (the table and serial figures are from the third, the first to send
those shapes):

- The window is 4,096 tokens and holds the state and the **longest** question: a
  state that fits beside one question was answered with two. The refusal is a
  `ValueError` naming the state and question token counts, which the client raises
  as `DecisionModelRefused`.
- `usage.input_tokens` counts the state once and **every** question, so a
  request with several questions can report more than 4,096 tokens and still fit.
  Compare only a one-question request's usage with the window.
- The decider added 77 tokens around a state for a one-line `noul` question: 68
  for the question, most of it the decider's per-question template, and 9 once per
  request. The decider's window counts a square page image at 448 px as 196 tokens
  (its refusal message names the image's share), and the image added 199 to a
  request's usage; a portrait page added 157 to usage. Both are under `IMAGE_TOKENS`.
- **Extraction confidence overflows the window on dense table and serial text.**
  At the default cap, a confidence request over one page of a markdown statement
  table (`| 06/11/2026 | 39,595.05 | 49,865.35 |` rows) was refused at 4,159 and
  4,170 tokens in `text` mode, and over a page of 17-character alphanumeric
  serials at 4,293 and 4,304 in `text` mode and 4,189 and 4,200 in `both`. The
  same table in `both` mode fit with 14 tokens to spare, and a table of figures
  with 102. The estimate counts ASCII punctuation and letters at half a token
  each; on these shapes the decider's tokenizer spends about 1.2 tokens for each
  estimated one (measured offline with the model's tokenizer, and consistent with
  the state counts in the refusal messages), and `PROMPT_RESERVE_TOKENS` does not absorb the difference.
  Section classification fit on every shape (at most 3,659 tokens). Its question,
  every class description counted as JSON, is estimated higher than the decider
  counts it, which is inferred to be what leaves it the margin.
  The estimate erred high on prose and CJK, by up to about 2,500 tokens.

## Backends

`backend: lambda` (or empty) invokes a Lambda function directly, with the request as
the event and the response as the result. That is the direct-invocation contract of
the Strands Decider's Lambda deployment. `sagemaker` and `bedrock_custom` are declared
on `ExternalModelConfig` and refused until they are implemented.

The `decision-model-decider` Feature Platform extension
(`feature-platform/decision-model-decider/`) deploys a function that serves this
contract, `GENAIIDP-decider`, and its stack's `DeciderEndpoint` output is the `live`
alias ARN to configure. The client accepts an alias- or version-qualified ARN and
meters on the bare function name.

The function name must start with `GENAIIDP-`, the same convention `LambdaHook` uses.
The pipeline's existing invoke permission is scoped to that prefix, so no new IAM is
needed. `endpoint` is required: a configuration that selects a decision
model without one is refused when it is loaded.

## Failures

| Exception | Meaning | Retried |
|---|---|---|
| `DecisionModelRefused` | the model refused the request: over its context window or request limit, too many images, malformed | never: the same request gets the same answer |
| `DecisionModelUnavailable` | the call did not complete: throttling past the retries, a timeout, a bad response | throttling and a crashed execution environment are retried, with bounded backoff that stays inside the calling Lambda's deadline; a read timeout is not, because the invocation may still be running and billing. When throttling, a capacity refusal or a connection that never opened outlasts the retries, the error carries `retry_later=True` and keeps that failure as its `__cause__`: the request never ran, so a caller may retry its whole step. A read timeout, a closed connection, a crashed environment, a malformed response and a Lambda error outside the retried codes (including a bare-status reply such as `"500"` or `"429"`) are never `retry_later`. `utils.transient_errors.is_transient_error` takes `retry_later` as final, so no caller second-guesses it from the cause (#2112) |

Neither is ever converted into a call to another model. A configuration that selects
a decision model uses only the decision model. What a caller does with the error is
its own decision, and each call site documents its own.

`decide_many` sends several requests, at most `max_concurrency` at once, and raises
the first failure once every request has finished. `decide_each` sends them the
same way and puts each failed request's `DecisionModelError` in that request's slot
instead, so a caller can say which request failed.

The decider's Lambda engine **refuses** a prompt over its window, and the error names
the token counts. It never truncates the state, so a decision is never made from part
of the input without saying so.

## Metering

Each request adds `requests: 1` and `gb_seconds` under
`{context}/decision_model/{function-name}`. `gb_seconds` is measured at the caller
times `function_memory_mb`, so it slightly over-states Lambda's billed duration.
`config_library/pricing.yaml` prices `decision_model/GENAIIDP-decider`; a function
under another name records a NULL cost until it has an entry.

## Images

`encode_image(bytes)` scales a page image so its longer side is at most 448 px, the
size the decider reads at, and base64-encodes it. Sending a larger one only spends
payload: a Lambda request is capped at 6 MB.
