---
title: "xAI Grok Models"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# xAI Grok Models (Grok 4.6)

The GenAIIDP accelerator supports xAI's **Grok 4.6** on Amazon Bedrock via the
cross-region inference profiles **`us.xai.grok-4.6`** (US geo) and
**`global.xai.grok-4.6`** (global).

Unlike the OpenAI GPT-5.x models — which require the `bedrock-mantle` Responses
API and a separate client path — Grok is served on the **standard Bedrock
Converse API**. That means it flows through `BedrockClient.invoke_model` and the
Strands `BedrockModel` exactly like Claude and Nova, so **agentic extraction and
tool use work**, with no per-service code changes.

> **TL;DR** — Grok 4.6 works for **OCR, classification, extraction (including
> agentic), assessment/confidence, summarization, evaluation, Chat-with-Document,
> and the rule-validation and agent paths**. It does **not** work for
> **Discovery** or **Policy Discovery** (it cannot accept PDF `document` blocks).
> Despite what the model card advertises, **Flex and Priority service tiers are
> rejected**, and **prompt caching was not observed to engage**.

## At a glance

| | Grok 4.6 (US geo) | Grok 4.6 (global) |
|---|---|---|
| Model ID | `us.xai.grok-4.6` | `global.xai.grok-4.6` |
| Endpoint / API | `bedrock-runtime` Converse | ← |
| Context window | 500K | ← |
| Max output tokens | 524,288 | ← |
| In-Region ID | **Not supported** — CRIS only | ← |
| Regions | US only (`us-east-1`, `us-east-2`, `us-west-1`, `us-west-2`) | Nearly all commercial regions, incl. EU and APAC |
| Input modalities | Text, **image** (no `document`) | ← |
| Reasoning | Always on; effort `none`/`low`/`medium`/`high`/`xhigh` | ← |
| Tool use (`toolConfig`) | Yes — all three `toolChoice` modes | ← |
| Service tier | **Standard only** | ← |
| Prompt caching | Not observed (see below) | ← |
| Price / 1M (in / cache-read / out) | $2.20 / $0.55 / $6.60 | $2.00 / $0.50 / $6.00 |

There is **no `eu.xai.grok-4.6`** and no `:1m` suffix. EU deployments therefore
receive **only** the `global.` entry in the model picklists — the accelerator's
region filter drops `us.`-prefixed IDs outside US regions automatically, and
`us.xai.grok-4.6` genuinely returns *"The provided model identifier is invalid"*
in `eu-west-1` / `eu-central-1`. The global profile is also the **cheaper** of
the two, so prefer it unless you need US data-residency routing.

## What is supported

| Feature | Grok 4.6 |
|---|---|
| OCR (Bedrock backend) | ✅ (sends page images) |
| Classification | ✅ |
| Extraction (traditional) | ✅ |
| **Extraction (agentic / `advanced`)** | ✅ — the key difference from GPT-5.x |
| Assessment / confidence | ✅ |
| Summarization | ✅ |
| Evaluation (`llm_method`) | ✅ |
| Chat with Document | ✅ |
| Rule validation (fact extraction, orchestrator) | ✅ |
| Chat-companion / error-analyzer agents | ✅ |
| Multi-document cluster analysis | ✅ |
| Response streaming | ✅ |

## What is NOT supported

| Feature | Why |
|---|---|
| **Discovery** (with / without ground truth, section auto-split) | Discovery sends whole PDFs as Converse `document` blocks. Grok rejects them: *"This model doesn't support documents."* Input modalities are text and image only. |
| **Policy / rule Discovery** | Same `document`-block requirement. |
| **Flex / Priority service tiers** | The model card advertises both, but Converse returns *"The provided service tier is not supported for this model"* on both prefixes in `us-west-2` and `us-east-1`. No tier-suffixed Grok IDs are offered. |
| **Explicit prompt caching** (`<<CACHEPOINT>>`) | `cachePoint` blocks raise `AccessDeniedException`. The accelerator strips `<<CACHEPOINT>>` markers from the prompt text for Grok, so prompts still work — they simply are not cached. |
| **`temperature` / `topP` / `top_k`** | Rejected with a 400 naming the field. The accelerator omits the whole sampling group for Grok, so your configured values are silently ignored rather than causing a failure. |
| **Structured outputs / hard grammar** | Not available on `bedrock-runtime` for any model. A forced `toolChoice` is the strongest schema enforcement (same as Claude). |
| **GovCloud** | Unverified. The model card claims geo CRIS in `us-gov-east-1`/`us-gov-west-1`, but the accelerator has a live finding that `us.`/`global.` prefixes are rejected in GovCloud regions. Treat as unsupported until verified. |
| **Count tokens** | Not supported by the model. |

Selecting Grok for a Discovery model is rejected by **config validation at save
time**, not at runtime — you get a clear error in the configuration editor rather
than a failed document two stages in. Grok is also simply absent from the four
Discovery model picklists.

## Prompt caching

Grok 4.6's model card advertises **implicit** prompt caching (no request
changes, no explicit breakpoints, no cache-write charge). In practice this was
**not observed to engage**: four back-to-back identical requests carrying a
20,033-token prompt each reported `cacheReadInputTokens = 0` in `us-west-2`.

The cache-read rate is recorded in `config_library/pricing.yaml` for
completeness, but **do not plan cost around a caching discount for Grok**. If
prompt-cache savings matter for your workload, prefer a Claude model, where the
accelerator emits explicit `cachePoint` blocks and caching is verified.

## Reasoning effort

Reasoning is **always on** for Grok 4.6 and defaults to `low`. Set
`reasoning_effort` on any service that supports it:

```yaml
extraction:
  model: us.xai.grok-4.6
  reasoning_effort: high    # none | low | medium | high | xhigh
```

Grok's effort vocabulary is **not** the same as Claude's:

| Model family | Accepted effort values | Carrier |
|---|---|---|
| xAI Grok | `none`, `low`, `medium`, `high`, `xhigh` | `additionalModelRequestFields.reasoning.effort` |
| Claude (Sonnet 5 / 4.6, Opus 4.5-4.8 / 5, Fable 5) | `low`, `medium`, `high`, `xhigh`, `max` | `additionalModelRequestFields.output_config.effort` |
| OpenAI GPT-5.x | `minimal`, `low`, `medium`, `high` | Responses `reasoning.effort` |

Grok **rejects `max`**, and `none` is Grok-only. The configuration picklist is a
superset across all families — `none`, `minimal`, `low`, `medium`, `high`,
`xhigh`, `max` — and each backend drops values its model does not accept, logging
a warning. So selecting `max` for Grok is safe but has no effect; pick `xhigh`
for maximum reasoning depth instead. Likewise `none` is available in the picklist
but is ignored by Claude and GPT-5.x.

Note that Bedrock also ignores *unrecognized* `additionalModelRequestFields`
keys silently for Grok, which is why the accelerator sends the exact carrier
above rather than reusing Claude's.

## Max output tokens

Bedrock enforces a hard cap of **524,288** output tokens for Grok 4.6, recorded
in `config_library/model_config_limits.yaml`. This is the first model whose
output cap rivals its own context window (500K), which interacts with
**model-aware auto-sizing**: the shard budget reserves room for the model's
response out of the input window, and reserving the full 524K would have left
nothing for OCR text. The auto-sizing output reserve is therefore capped at a
fraction of the usable input window — see
`_MAX_OUTPUT_RESERVE_FRACTION_OF_INPUT` in `idp_common/bedrock/sizing.py`. Grok
ends up with a ~90K-token shard budget, comfortably larger than the ~18K a
200K-context Claude model gets.

## IAM

No additional permissions are required. The accelerator's existing
`bedrock:InvokeModel` / `bedrock:InvokeModelWithResponseStream` grants on
`inference-profile/*` cover both Grok CRIS IDs. The model card mentions needing
`bedrock:InvokeModel` on `project/default`, but that applies to the Responses
API path — Converse calls succeed without it (verified live). No
`bedrock-mantle` permissions are needed, since the accelerator does not use that
endpoint for Grok.

## Choosing a model

Reach for Grok 4.6 when you want:

- **A very large context window** (500K) with a genuinely large shard budget —
  useful for long documents you would rather not split finely.
- **Agentic extraction with a non-Anthropic model.** Grok is currently the only
  non-Claude/non-Nova option that supports the Strands tool-use loop.
- **Low cost per token at frontier quality** — $2.00 / $6.00 per 1M on the global
  profile is materially cheaper than Claude Opus or GPT-5.6 Sol.

Prefer a **Claude** model when you need Discovery, verified prompt caching,
GovCloud, `temperature` control, or the `max` reasoning effort level.

## Related documentation

- [Configuration](./configuration.md) — where model IDs are set
- [Extraction and Confidence](./extraction-and-confidence.md) — agentic extraction
- [Discovery](./discovery.md) — why Grok is excluded
- [Service Tiers](./service-tiers.md) — tier support by model
- [EU Region Model Support](./eu-region-model-support.md) — EU picklist behavior
- [OpenAI GPT-5.x Models](./openai-models.md) — the other third-party family
- `lib/idp_common_pkg/idp_common/bedrock/README.md` — client-level behavior
