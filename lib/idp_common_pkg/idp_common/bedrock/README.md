Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Bedrock Integration

The GenAIIC IDP Accelerator includes a robust client for Amazon Bedrock that provides resilient model invocation with built-in retry handling, metrics collection, and helpful utilities. This integration supports all document processing patterns that utilize Bedrock models.

## Using the Bedrock Client

### Simple Function Approach

For quick, straightforward use cases, you can use the function-style interface:

```python
from idp_common.bedrock import invoke_model

# Basic model invocation
response = invoke_model(
    model_id="anthropic.claude-3-haiku-20240307-v1:0",
    system_prompt="You are a helpful assistant.",
    content=[{"text": "What are the main features of AWS Bedrock?"}],
    temperature=0.0,
    top_k=5
)

# Process the response
output_text = response["response"]["output"]["message"]["content"][0]["text"]
print(output_text)
```

### Class-Based Interface

For more control and advanced features, use the BedrockClient class directly:

```python
from idp_common.bedrock.client import BedrockClient

# Create a custom client
client = BedrockClient(
    region="us-east-1",
    max_retries=5,
    initial_backoff=1.5,
    max_backoff=300,
    metrics_enabled=True
)

# Invoke a model
response = client.invoke_model(
    model_id="anthropic.claude-3-sonnet-20240229-v1:0",
    system_prompt="You are a helpful assistant.",
    content=[{"text": "How does document processing work?"}],
    temperature=0.0
)

# Extract text using the helper method
output_text = client.extract_text_from_response(response)
print(output_text)
```

## Working with Embeddings

Generate text embeddings for semantic search or document comparison:

```python
from idp_common.bedrock.client import BedrockClient

client = BedrockClient()
embedding = client.generate_embedding(
    text="This document contains information about loan applications.",
    model_id="amazon.titan-embed-text-v1"
)

# Use embedding for vector search, clustering, etc.
```

## Prompt Caching with CachePoint

Prompt caching is a powerful feature in Amazon Bedrock that significantly reduces response latency for workloads with repetitive contexts. The Bedrock client provides built-in support for this via the `<<CACHEPOINT>>` tag.

### Supported Models

CachePoint functionality is only available for specific Bedrock model IDs.
`CACHEPOINT_SUPPORTED_MODELS` in `client.py` is the authoritative list; it
currently covers the Claude Haiku 4.5 / Sonnet 4.x-5 / Opus 4.x-5 families
(including the `:1m` variants) and the Nova models — for example:

- `us.anthropic.claude-haiku-4-5-20251001-v1:0`
- `us.anthropic.claude-sonnet-5`
- `us.amazon.nova-lite-v1:0`
- `us.amazon.nova-pro-v1:0`

OpenAI GPT-5.x models are deliberately absent: they are served via the
bedrock-mantle Responses API and handle caching in `openai_responses.py`.

xAI Grok models are also deliberately absent, for a different reason: explicit
`cachePoint` blocks raise `AccessDeniedException`. Grok's model card advertises
*implicit* caching (no request change needed), but that was not observed to
engage — four back-to-back identical 20,033-token prompts each reported
`cacheReadInputTokens = 0`. Do not plan cost around a caching discount for Grok.

When using unsupported models, the client will automatically remove `<<CACHEPOINT>>` tags from the content while preserving all text, and log a warning.

### Using CachePoint in Your Prompts

To implement prompt caching, insert the `<<CACHEPOINT>>` tag in your text content to indicate where caching boundaries should occur:

```python
from idp_common.bedrock.client import BedrockClient

client = BedrockClient()

# Content with cachepoint tags
content = [
    {
        "text": """This is static context that doesn't change between requests. 
        It could include model instructions, few shot examples, etc.
        <<CACHEPOINT>>
        This is dynamic content that changes with each request.
        """
    }
]

response = client.invoke_model(
    model_id="anthropic.claude-3-sonnet-20240229-v1:0",
    system_prompt="You are a helpful assistant.",
    content=content,
    temperature=0.0
)
```

### How CachePoint Works

When the `invoke_model` method processes your content:

1. It detects any text elements containing the `<<CACHEPOINT>>` tag
2. The text is split at each tag location
3. The client inserts a `{"cachePoint": {"type": "default"}}` element between the split text parts
4. The resulting message structure enables Bedrock to cache the preceding content

### Multiple CachePoints and Mixed Content

You can use multiple cachepoints in a single prompt and combine them with other content types:

```python
content = [
    {"text": "Static instructions for document processing<<CACHEPOINT>>"},
    {"image": {"url": "s3://bucket/document.png"}},
    {"text": "Static analysis guidelines<<CACHEPOINT>>Dynamic query about the document"}
]
```

### Benefits of Prompt Caching

- **Faster Response Times**: Avoid reprocessing the same context repeatedly
- **Reduced TTFT**: Time-To-First-Token is significantly lower for subsequent requests
- **Cost Efficiency**: Potentially lower token usage by avoiding redundant processing

> **NOTE**: To effectively use Prompt Caching, there is a [minimum number of tokens](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html#prompt-caching-models) for the cache.

### Debugging CachePoint Processing

The Bedrock client includes detailed debug logging for cachepoint processing:

```python
import logging
logging.getLogger('idp_common.bedrock.client').setLevel(logging.DEBUG)

# Now invoke_model calls will log detailed cachepoint processing information
# including word counts and split points in the content
```

### Example CachePoint Processing
See notebook [Bedrock Client Prompt Cache Testing Notebook](../../../../notebooks/bedrock_client_cachepoint_test.ipynb)

## OpenAI GPT-5.x Models (bedrock-mantle Responses API)

OpenAI's frontier models — **GPT-5.4** (`openai.gpt-5.4`), **GPT-5.5**
(`openai.gpt-5.5`), and the **GPT-5.6** family (`openai.gpt-5.6-sol`,
`openai.gpt-5.6-terra`, `openai.gpt-5.6-luna`) — are **not** served on the
Converse API like every other model. They are available only on the
**`bedrock-mantle` endpoint via the OpenAI Responses API**. The client hides
this difference: when a model ID starts with `openai.gpt-5`, `invoke_model`
transparently routes the request to a SigV4-signed HTTP call against
`bedrock-mantle` (implemented in [`openai_responses.py`](openai_responses.py))
and returns the **same `{"response", "metering"}` structure** every caller
already expects — so no caller code changes.

```python
from idp_common.bedrock import invoke_model

# Routed automatically to the bedrock-mantle Responses API.
response = invoke_model(
    model_id="openai.gpt-5.4",
    system_prompt="You are a helpful assistant.",
    content=[{"text": "Summarize this document."}],
    max_tokens=4000,
    reasoning_effort="medium",   # OpenAI-only: minimal | low | medium | high
)
text = response["response"]["output"]["message"]["content"][0]["text"]
```

### Behavior and limitations

- **Inference params**: these are reasoning models — `temperature`, `top_p`, and
  `top_k` are ignored. Control output via `reasoning_effort`
  (`minimal`/`low`/`medium`/`high`, default `medium`). The default can be set
  globally with the `BEDROCK_MANTLE_REASONING_EFFORT` env var.
- **Input modalities**: text and images only. Converse `document` blocks (e.g.
  whole-PDF input) are **not** supported and are dropped — callers needing PDF
  ingestion (Discovery) must use a Claude/Nova model.
- **Prompt caching**: differs by generation. GPT-5.4/5.5 cache **automatically**
  (any prefix > ~1,024 tokens, populated server-side and reused on repeat calls,
  no cache-write charge; `<<CACHEPOINT>>` markers are stripped). GPT-5.6
  caches **explicitly** — a `<<CACHEPOINT>>` marker (or `cachePoint` block) is
  translated into `prompt_cache_options`/`prompt_cache_breakpoint` with a
  deterministic `prompt_cache_key` derived from the cached prefix. Cache reads
  and (5.6-only) cache writes are metered via `cacheReadInputTokens` /
  `cacheWriteInputTokens`. Note: this is separate from `CACHEPOINT_SUPPORTED_MODELS`,
  which governs the Converse path only.
- **Service tiers**: standard only (no `:priority`/`:flex`).
- **Regions**: US only. GPT-5.4 in `us-east-1`, `us-east-2`, `us-west-2`,
  `us-gov-west-1`; GPT-5.5 in `us-east-1`, `us-east-2`; GPT-5.6 Sol in
  `us-east-1`, `us-east-2`; GPT-5.6 Terra/Luna add `us-west-2`. No EU/global;
  GovCloud is GPT-5.4 only. When the configured region lacks the model, the
  request is routed to a known-available region (override with
  `BEDROCK_MANTLE_REGION`).
- **Agentic extraction / Discovery**: not supported (the Strands path and
  Discovery's PDF document blocks are incompatible); these are rejected by
  `idp-cli config-validate` and guarded at runtime.

### Schema-enforced output (`output_schema`, opt-in)

This is the **only** surface in the accelerator with a native constrained-decoding
format. On Converse and InvokeModel, `strict` tool use and `output_config.format`
are both rejected by `bedrock-runtime` (`Extra inputs are not permitted`), so
schema enforcement there is best-effort `toolConfig` at most. The Responses API
instead accepts:

```
text.format = {"type": "json_schema", "name": ..., "schema": {...}, "strict": true}
```

Note the field is **`text.format`** (the Responses API shape), *not* the Chat
Completions `response_format`.

```python
from idp_common.bedrock.openai_responses import invoke_responses_api

result = invoke_responses_api(
    client=default_client,
    model_id="openai.gpt-5.4",
    system_prompt="Extract the invoice fields.",
    content=[{"text": ocr_text}],
    max_tokens=4000,
    max_retries=3,
    context="Extraction",
    output_schema=class_schema,        # opt-in; None = unchanged behaviour
    output_schema_name="Invoice",
)
```

The return value is the same `{"response": ..., "metering": ...}` structure as
always — including the disjoint `inputTokens` accounting — so no downstream
consumer changes.

**Opt-in means opt-in.** With `output_schema=None` (the default) no `text` key is
added to the request body and every code path, including the error paths, is
byte-identical to what it was before the parameter existed.

**Schema normalization.** `to_strict_json_schema()` is applied automatically. It
is pure, non-mutating and idempotent, and it enforces the three rules strict mode
requires: `additionalProperties: false` on every object, every declared property
listed in `required`, and originally-optional properties widened to a nullable
union (`"type": ["string", "null"]`) so requiring them does not change semantics.
It also strips `x-*` vendor extensions (`x-aws-idp-*`, `x-aws-stickler-*`), which
are unknown keywords to the endpoint's schema validator. Two caveats: a property
that is a bare `$ref` cannot be widened and stays genuinely required; and the
exact keyword allow-list the endpoint enforces is undocumented, so other keywords
pass through verbatim rather than being silently dropped.

**Failure modes are raised, never swallowed.** All three are `RuntimeError`
subclasses, so existing `except RuntimeError` handlers still catch them:

| Exception | Trigger | Intended handling |
|---|---|---|
| `OutputSchemaRejectedError` | terminal 4xx while a schema was attached | catch and retry with `output_schema=None` (prompt-based schema); the endpoint's error text is preserved verbatim |
| `OutputRefusalError` | a `refusal` content block (note `status` can still be `completed`) | a refusal is not an empty extraction — surface it; `.refusal` and `.metering` are attached |
| `IncompleteOutputError` | `status == "incomplete"` (usually `max_output_tokens`) | partial text is invalid JSON under constrained decoding; raise `max_tokens` or shard the input; `.reason` and `.metering` are attached |

The refusal/incomplete errors carry `.metering` because the request was billed;
record it before re-raising if you account for cost.

> **Not yet live-verified.** `text.format` + `strict: true` is the documented
> OpenAI Responses API contract, and the AWS Responses API documentation defers to
> it for request-field details while listing the endpoint's behaviour differences
> (structured output is not among them). It has **not** been exercised against a
> live GPT-5.x deployment from this repo. Until it is, do not treat this as a hard
> guarantee — keep validating the parsed result against the schema downstream.
> `invoke_model()` on `BedrockClient` does not forward `output_schema` yet either;
> call `invoke_responses_api` directly, or plumb it through the client first.

### Streaming

For interactive use (e.g. Chat-with-Document), `stream_responses_api` yields
incremental text deltas, then a final `{"metering": ..., "text": ...}` dict:

```python
from idp_common.bedrock import stream_responses_api
from idp_common.bedrock.client import default_client

for item in stream_responses_api(
    client=default_client,
    model_id="openai.gpt-5.4",
    system_prompt="Answer concisely.",
    content=[{"text": "What is the total due?"}],
    max_tokens=2000,
    context="ChatWithDocument",
    reasoning_effort="low",
):
    if isinstance(item, str):
        print(item, end="")        # incremental token delta
    else:
        metering = item["metering"]  # final usage record
```

### Detecting these models

```python
from idp_common.bedrock import is_openai_responses_model

is_openai_responses_model("openai.gpt-5.4")          # True
is_openai_responses_model("us.anthropic.claude-...")  # False
```

For the full support matrix and IAM/env-var details, see the
[OpenAI GPT-5.x Models](../../../../docs/openai-models.md) guide.

## Helper Methods

The BedrockClient provides useful utilities for common tasks:

### Prompt Formatting

```python
from idp_common.bedrock.client import BedrockClient

client = BedrockClient()

template = """
Please analyze this {DOCUMENT_TYPE}:

<document>
{CONTENT}
</document>

Extract the following fields: {FIELDS}
"""

substitutions = {
    "DOCUMENT_TYPE": "invoice",
    "CONTENT": "Invoice #12345\nDate: 2023-05-15\nAmount: $1,250.00",
    "FIELDS": "invoice_number, date, amount"
}

formatted_prompt = client.format_prompt(template, substitutions)
```

### Response Text Extraction

```python
# Extract text from a complex response structure
text = client.extract_text_from_response(response)
```

Both extraction helpers scan **every** content block rather than indexing
`content[0]`. Reasoning models (Claude Sonnet 5 / 4.6+, and any model with
extended thinking on) emit one or more `reasoningContent` blocks *before* the
answer block, so `content[0]` is frequently not the answer.

## Tool Use (`toolConfig` / `toolChoice`)

`invoke_model` accepts two optional parameters that put a JSON Schema on the
wire as a Converse tool, which is the strongest output-shape enforcement
available on `bedrock-runtime`:

```python
TOOL_CONFIG = {                      # build this ONCE per document class
    "tools": [
        {
            "toolSpec": {
                "name": "extract_fields",
                "description": "Return the extracted fields.",
                "inputSchema": {"json": class_json_schema},
            }
        }
    ]
}

response = client.invoke_model(
    model_id="us.anthropic.claude-sonnet-5",
    system_prompt="You extract fields from documents.",
    content=[{"text": document_text}],
    tool_config=TOOL_CONFIG,
    tool_choice={"tool": {"name": "extract_fields"}},   # force the tool
)

data = client.extract_tool_use_from_response(response)   # dict, or None
if data is None:
    # A model can accept a toolConfig and still answer in prose.
    data = json.loads(extract_json_from_text(client.extract_text_from_response(response)))
```

When both parameters are omitted (the default) the request is identical to one
made before tool support existed — no `toolConfig` key is sent.

### The `tool_choice` contract

`toolChoice` is a *member of* `toolConfig` in the Converse API. It is accepted as
a separate parameter here purely as a convenience:

- `tool_config` is passed through **verbatim**, so you can also just embed
  `"toolChoice"` inside it and omit the parameter.
- When both are given, `tool_choice` is merged into a **shallow copy** of
  `tool_config` (your dict is never mutated). If `tool_config` already carried a
  different `toolChoice`, the parameter wins and a warning is logged.
- `tool_choice` **without** `tool_config` raises `ValueError` — Converse rejects
  a `toolChoice` with no tools.

Valid modes: `{"auto": {}}`, `{"any": {}}`, `{"tool": {"name": "..."}}`. All
three are accepted by every currently selectable Claude and Nova family.

### Property names: sanitize before you send (#709)

Bedrock enforces `^[a-zA-Z0-9_.-]{1,64}$` on `toolSpec.inputSchema` property
keys. Document classes are authored for humans, so `"Account Number"` and
`"Purchase Date and Time"` are normal — **4 of the 152 shipped preset classes
contain names Bedrock would reject** (`bank-statement-sample`,
`lending-package-sample`, `lending-package-sample-govcloud`,
`rvl-cdip-package-sample`).

`invoke_model` **refuses** such a schema with a `ValueError` naming the offending
paths. It deliberately does not sanitize for you: renaming here would return a
response keyed by names you never asked for, with no map to reverse it — silently
renaming every field of every extraction. Do both halves yourself:

```python
from idp_common.bedrock.tool_schema import restore_names, sanitize_tool_schema

clean, name_map = sanitize_tool_schema(class_schema)   # "Account Number" -> "Account_Number"
response = client.invoke_model(..., tool_config=tool_config_for(clean))
fields = restore_names(model_output, name_map)          # and back again
```

`sanitize_tool_schema` leaves an already-valid name **byte-identical** (so a
working schema is unaffected and `name_map.is_empty()` makes `restore_names` a
no-op), resolves collisions with a deterministic numeric suffix rather than
merging two fields into one, and truncates to 64 characters.

> **It sanitizes recursively, and that is deliberate.** Bedrock's own check is
> **top level only** — a bad key inside an object property, inside `array.items`,
> or inside a `$defs` entry is accepted *today*. Relying on that would break the
> moment a class is wrapped in a list, and the day AWS makes the check recursive.
> The `ValueError` reports nested offenders for the same reason.

### Capability gate

Every model that reaches the Converse API supports tool use, so there is no
per-family allow-list (unlike `CACHEPOINT_SUPPORTED_MODELS`). Two routes in
`invoke_model` bypass Converse and therefore **cannot** carry a `toolConfig`:

| Route | Why |
|---|---|
| `model_id="LambdaHook"` | posts a Converse-shaped payload to a customer-owned Lambda, which need not implement tool use |
| OpenAI GPT-5.x (`openai.gpt-5.*`) | served by the bedrock-mantle Responses API, which has its own tools schema |

xAI Grok reaches Converse, so it **does** support `toolConfig` — verified live
with all three `toolChoice` modes (`auto`/`any`/`tool`), each emitting a
`toolUse` block. This is the reason Grok is allowed for agentic extraction while
GPT-5.x is not.

A separate gate, `supports_document_blocks()` /
`document_blocks_unsupported_reason()`, covers whole-PDF `document` content
blocks. Both GPT-5.x (text + image only on the Responses API) and xAI Grok
(*"This model doesn't support documents"*) fail it, which is what excludes them
from Discovery. Use it the same way as the tool-config gate.

**Inference-profile ARNs.** `is_grok_model()`, `strips_sampling_params()`,
`is_claude_4_7_model()` and `document_blocks_unsupported_reason()` resolve
inference-profile ARNs (via `resolve_model_id_from_arn`) before matching, so a
config that names
`arn:aws:bedrock:...:inference-profile/us.anthropic.claude-sonnet-5` — the form
recommended for cost-allocation tagging, and the only form GovCloud accepts — is
gated the same as the bare ID. **Opaque
`application-inference-profile/<uuid>` ARNs are the exception**: the underlying
foundation model cannot be determined without a `GetInferenceProfile` call, so
these gates return the permissive answer for them. A Grok application inference
profile will therefore bypass the sampling-param strip and the document-block
refusal and fail at the Bedrock call instead. (`_is_model_cachepoint_supported`
does make the control-plane call, if you need a model for how to resolve one.)

Passing `tool_config`/`tool_choice` for either route raises `ValueError` — the
schema is never silently dropped. Branch beforehand if you need a text fallback:

```python
from idp_common.bedrock.client import supports_tool_config, tool_config_unsupported_reason

if supports_tool_config(model_id):
    ...  # tool-based path
else:
    logger.info("No tool use: %s", tool_config_unsupported_reason(model_id))
    ...  # prompt-based schema path
```

### Caveats before you add a caller

- **There is no strict / structured-output mode on `bedrock-runtime`.**
  `toolSpec.strict`, `output_config.format` and `response_format` are all
  rejected (`Extra inputs are not permitted`) on **both** Converse and
  InvokeModel. A forced `toolChoice` is best-effort constrained decoding, not a
  grammar guarantee — always validate the returned object.
- **Property names must be sanitized by the caller.** Converse rejects
  *top-level* `inputSchema.json.properties` keys that don't match
  `^[a-zA-Z0-9_.-]{1,64}$` (`Property keys should match pattern ...`). Names like
  `"Account Number"` appear in several shipped class schemas, so a schema taken
  straight from configuration can fail. `invoke_model` does **not** sanitize;
  rename recursively (even though only the top level is enforced today) and map
  the response keys back.
- **Keep the `toolConfig` deterministic per class.** Tools render *before*
  `system` in the prompt-cache prefix, so changing the tool schema invalidates
  the entire cached prefix — system prompt included. A stable per-class
  `toolConfig` caches normally (measured: +~400 cached input tokens); a
  per-request one destroys cache hits silently.
- A tool-using response has `stopReason == "tool_use"`; a prose answer has
  `end_turn`. Existing truncation checks elsewhere in the library only look for
  `max_tokens`, so they are unaffected.

## Guardrail Support

This module includes built-in support for Amazon Bedrock Guardrails, which help enforce content safety, security, and policy compliance across all Bedrock model interactions. Since the integration passes the Guardrail ID and version directly to Bedrock API calls, all Guardrail policy types are supported — including content filters, topic restrictions, PII detection, and [Automated Reasoning Checks](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-automated-reasoning.html) for formal verification of model outputs.

### Configuring Guardrails

Guardrails are configured via environment variables:

- `GUARDRAIL_ID_AND_VERSION`: Contains the Guardrail ID and Version in format `id:version`

When properly configured, the `get_guardrail_config()` function will automatically include guardrail parameters in Bedrock API calls.

### How Guardrail Integration Works

1. The `invoke_model` function checks if `GUARDRAIL_ID_AND_VERSION` is set
2. If configured, guardrail parameters are parsed from the environment variable
3. Appropriate guardrail parameters are added to Bedrock API calls:
   - For `converse` API: Uses `guardrailIdentifier`, `guardrailVersion`, and `trace`
4. Debug logs show when guardrails are being applied

### Example with Guardrails

```python
import os
from idp_common.bedrock import invoke_model

# Set the environment variable (typically configured in Lambda environment)
os.environ["GUARDRAIL_ID_AND_VERSION"] = "your-guardrail-id:Draft"

# Call invoke_model normally - guardrails are applied automatically
response = invoke_model(
    model_id="anthropic.claude-3-sonnet-20240229-v1:0",
    system_prompt="You are a helpful assistant.",
    content=[{"text": "Tell me about security best practices."}],
    temperature=0.0
)
```

## Generation Parameter Configuration

The Bedrock client supports key generation parameters that control the output behavior of foundation models. Understanding these parameters is crucial for optimizing model performance for different document processing tasks.

### Understanding Generation Parameters

#### Temperature

Temperature controls the randomness of model outputs by scaling the probability distribution of next tokens:

- **Low temperature (0.0-0.3)**: More deterministic, focused outputs ideal for factual extraction
- **Medium temperature (0.4-0.7)**: Balanced outputs with some creativity
- **High temperature (0.8-1.0)**: More diverse and creative outputs

#### Top-p (Nucleus Sampling)

Top-p (nucleus sampling) computes the cumulative distribution over all token options in decreasing probability order and cuts it off once it reaches the specified probability threshold:

- Lower values (0.1-0.5): More focused on high-probability tokens
- Higher values (0.6-1.0): Includes more diversity in possible tokens

**Important**: Anthropic recommends adjusting either temperature OR top-p, but not both simultaneously, as this can lead to unpredictable generation behavior.

#### Top-k

Top-k limits the selection to only the k highest probability tokens before temperature/top-p logic runs:

- Lower values (5-20): Narrows token selection for more predictable outputs
- Higher values (50-200): Allows for more diverse language

### Parameter Implementation by Model Family

Different Bedrock models implement these parameters with varying defaults, naming conventions, and parameter placements:

- **Claude models**:
  - Default values: temperature=1.0, top_p=0.999, top_k=250 (wide open)
  - Parameters use snake_case: `temperature`, `top_p`, `top_k`
  - Implementation: `top_k` is placed in `additionalModelRequestFields`
  - **Reasoning effort** (Sonnet 5, Sonnet 4.6, Opus 4.5–4.8, Opus 5, Fable 5 — see
    `is_claude_effort_model()`): `reasoning_effort` (`low`/`medium`/`high`/
    `xhigh`/`max`) maps to `additionalModelRequestFields.output_config.effort`.
    Ignored for Sonnet 4.5 / Haiku 4.5 (they 400 on it). `budget_tokens` is
    rejected — use effort. Verified live: effort changes output-token spend.
  - **max_tokens**: an OPTIONAL cap. It is `Optional[int]` in every service
    config (default `None`; an empty string in stored config also parses to
    `None`), and each service passes the value straight through. When `None`
    the client resolves the model maximum from `get_model_max_output_tokens()`
    — so leaving it unset (the default everywhere) means "use the model's max
    output". Set a positive value only to cap output below the model max. OCR
    passes `None` (no config knob); extraction/confidence have no `max_tokens`
    field at all and always request the model maximum. The limits
    come from the DynamoDB Configuration Table when `CONFIGURATION_TABLE_NAME`
    is set (seeded from `config_library/model_config_limits.yaml` at deploy and
    editable in the web UI under "View / Edit Model Limits"; cached ~60s per
    container), falling back to the on-disk `config_library/` YAML offline.
    Omitting `maxTokens` would let Bedrock apply a small truncating default.
    If a request still exceeds the model cap, the client parses the true limit
    from the `ValidationException` and retries once.

- **Nova models**:
  - Default values: temperature=0.7, topP=0.9, topK≈50 (moderately constrained)
  - Parameters use camelCase: `temperature`, `topP`, `topK`
  - Implementation: `topK` is placed in `additionalModelRequestFields.inferenceConfig`

- **OpenAI GPT-5.x models** (`openai.gpt-5.*`):
  - Reasoning models — `temperature`/`top_p`/`top_k` are **not used** (ignored)
  - Controlled instead via `reasoning_effort` (`minimal`/`low`/`medium`/`high`)
  - Served on the `bedrock-mantle` Responses API, not Converse — see the
    [OpenAI GPT-5.x Models](#openai-gpt-5x-models-bedrock-mantle-responses-api)
    section above

- **xAI Grok models** (`us.xai.grok-4.6`, `global.xai.grok-4.6`):
  - Reasoning model — `temperature` and `topP` are **hard-rejected** with a 400
    naming the field, so `strips_sampling_params()` omits the whole sampling
    group (`top_k` was never forwarded for non-Claude families). This is a
    stronger contract than GPT-5.x, where the params are merely ignored.
  - **Reasoning effort**: always on, defaults to `low`. `reasoning_effort` maps
    to `additionalModelRequestFields.reasoning.effort` and accepts
    `none`/`low`/`medium`/`high`/`xhigh` — **not** `max` (Grok 400s on it), and
    **not** Claude's `output_config.effort` carrier (silently ignored here).
  - **max_tokens** rides in `inferenceConfig.maxTokens`, the Converse-standard
    field and the default for every non-Claude family. Claude's
    `additionalModelRequestFields.max_tokens` is *accepted but silently ignored*
    by Grok, so a wrong carrier fails open (uncapped output) rather than loudly.
  - Served on Converse, so `toolConfig` works with all three `toolChoice` modes
    — this is why Grok supports **agentic extraction** and GPT-5.x does not.
  - Cannot accept `document` content blocks (`supports_document_blocks()` is
    False), which excludes it from Discovery. See
    [xAI Grok Models](../../../../docs/grok-models.md).
  - Not in `CACHEPOINT_SUPPORTED_MODELS`: explicit `cachePoint` blocks raise
    `AccessDeniedException`, and the advertised implicit caching was not
    observed to engage. `<<CACHEPOINT>>` markers are stripped from the text.

**Common implementation details**:
- Temperature is always included in the main `inferenceConfig`, except for the
  sampling-param-stripped models (Claude 4.7+, xAI Grok)
- top_p is added to `inferenceConfig` as "topP"
- `maxTokens` goes in `inferenceConfig` for every family EXCEPT Claude, which
  uses `additionalModelRequestFields.max_tokens`

### Task-Specific Best Practices

For document understanding tasks, we recommend the following parameter settings:

1. **Key Information Extraction**:
   - Temperature: 0.0 (deterministic)
   - Top-p: 0.1 (focused on highest probability tokens)
   - Top-k: 5 (restrict to most likely tokens)
   - Rationale: Maximizes precision and consistency for structured data extraction

2. **Classification**:
   - Temperature: 0.0 (deterministic)
   - Top-p: 0.1 (focused)
   - Top-k: 5 (restricted)
   - Rationale: Ensures consistent classification decisions with minimum variance

3. **Summarization**:
   - Temperature: 0.0 (deterministic)
   - Top-p: 0.1 (focused but allows some flexibility)
   - Top-k: 5 (moderately restricted)
   - Rationale: Balances factual accuracy with coherent narrative flow

Remember: As Anthropic recommends, adjust either temperature OR top-p, but not both simultaneously. For document processing tasks that require high accuracy and consistency, we've found that using a temperature of 0.0 with a low top-p value (0.1) provides the most reliable results.

## Resilience Features

The BedrockClient automatically handles common failure scenarios:

- Exponential backoff with jitter for rate limits and transient errors
- Intelligent classification of retryable vs. non-retryable errors
- Detailed logging with appropriate content sanitization
- Metrics collection for request counts, latencies, and token usage

## Configuration Options

When creating a BedrockClient instance, you can customize:

- `region`: AWS region for Bedrock (default: AWS_REGION env var or us-west-2)
- `max_retries`: Maximum retry attempts for throttled requests (default: 7)
- `initial_backoff`: Starting backoff time in seconds (default: 2)
- `max_backoff`: Maximum backoff time in seconds (default: 300)
- `metrics_enabled`: Whether to publish CloudWatch metrics (default: True)

This integration provides the foundation for reliable, scalable document processing with Amazon Bedrock models throughout the accelerator.
