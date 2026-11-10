Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Decision models

A decision model picks between options or rates on a scale. It returns a probability
for each option rather than generated text, so there is no output to parse, and the
confidence comes from a distribution instead of being a number the model writes out.

This package is the client the pipeline uses to call one. It is used in place of an
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

## Backends

`backend: lambda` (or empty) invokes a Lambda function directly, with the request as
the event and the response as the result. That is the direct-invocation contract of
the Strands Decider's Lambda deployment. `sagemaker` and `bedrock_custom` are declared
on `ExternalModelConfig` and refused until they are implemented.

The function name must start with `GENAIIDP-`, the same convention `LambdaHook` uses.
The pipeline's existing invoke permission is scoped to that prefix, so no new IAM is
needed. An empty `endpoint` resolves to `DECISION_MODEL_FUNCTION_ARN`, which a stack
that deploys its own decision model sets.

## Failures

| Exception | Meaning | Retried |
|---|---|---|
| `DecisionModelRefused` | the model refused the request: over its context window or request limit, too many images, malformed | never: the same request gets the same answer |
| `DecisionModelUnavailable` | the call did not complete: throttling past the retries, a timeout, a bad response | throttling and a crashed execution environment are retried, with bounded backoff that stays inside the calling Lambda's deadline; a read timeout is not, because the invocation may still be running and billing |

Neither is ever converted into a call to another model. A configuration that selects
a decision model uses only the decision model. What a caller does with the error is
its own decision, and each call site documents its own.

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
