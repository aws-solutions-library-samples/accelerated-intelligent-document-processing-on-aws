---
title: "EU Region Model Support and Mapping"
---

# EU Region Model Support and Mapping

## Overview

The GenAI-IDP accelerator supports deployment in EU regions with automatic model mapping between US and EU model endpoints. This document outlines the available models, mapping behavior, and fallback mechanisms.

## Complete Model Mappings

The following table shows all US to EU model mappings currently configured in the system:

| US Model | EU Model | Notes |
|----------|----------|-------|
| `us.amazon.nova-lite-v1:0` | `eu.amazon.nova-lite-v1:0` | Direct mapping |
| `us.amazon.nova-pro-v1:0` | `eu.amazon.nova-pro-v1:0` | Direct mapping |
| `us.amazon.nova-premier-v1:0` | `eu.anthropic.claude-sonnet-4-5-20250929-v1:0` | **Fallback mapping** |
| `us.amazon.nova-2-lite-v1:0` | `eu.amazon.nova-2-lite-v1:0` | Direct mapping |
| `us.anthropic.claude-3-haiku-20240307-v1:0` | `eu.anthropic.claude-3-haiku-20240307-v1:0` | Direct mapping |
| `us.anthropic.claude-haiku-4-5-20251001-v1:0` | `eu.anthropic.claude-haiku-4-5-20251001-v1:0` | Direct mapping |
| `us.anthropic.claude-3-5-sonnet-20241022-v2:0` | `eu.anthropic.claude-3-5-sonnet-20241022-v2:0` | Direct mapping |
| `us.anthropic.claude-3-7-sonnet-20250219-v1:0` | `eu.anthropic.claude-3-7-sonnet-20250219-v1:0` | Direct mapping |
| `us.anthropic.claude-sonnet-4-20250514-v1:0` | `eu.anthropic.claude-sonnet-4-20250514-v1:0` | Direct mapping |
| `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | `eu.anthropic.claude-sonnet-4-5-20250929-v1:0` | Direct mapping |
| `us.anthropic.claude-sonnet-4-6` | `eu.anthropic.claude-sonnet-4-6` | Direct mapping |
| `us.anthropic.claude-sonnet-4-6:1m` | `eu.anthropic.claude-sonnet-4-6:1m` | Direct mapping |
| `us.anthropic.claude-opus-4-20250514-v1:0` | `eu.anthropic.claude-sonnet-4-5-20250929-v1:0` | **Fallback mapping** |
| `us.anthropic.claude-opus-4-1-20250805-v1:0` | `eu.anthropic.claude-sonnet-4-5-20250929-v1:0` | **Fallback mapping** |
| `us.anthropic.claude-opus-4-5-20251101-v1:0` | `eu.anthropic.claude-opus-4-5-20251101-v1:0` | Direct mapping |
| `us.anthropic.claude-opus-4-6-v1` | `eu.anthropic.claude-opus-4-6-v1` | Direct mapping |
| `us.anthropic.claude-opus-4-6-v1:1m` | `eu.anthropic.claude-opus-4-6-v1:1m` | Direct mapping |
| `us.anthropic.claude-opus-4-7` | `eu.anthropic.claude-opus-4-7` | Direct mapping |
| `us.anthropic.claude-opus-4-7:1m` | `eu.anthropic.claude-opus-4-7:1m` | Direct mapping |
| `us.anthropic.claude-opus-4-8` | `eu.anthropic.claude-opus-4-8` | Direct mapping |
| `us.anthropic.claude-opus-4-8:1m` | `eu.anthropic.claude-opus-4-8:1m` | Direct mapping |
| `us.anthropic.claude-opus-5` | `eu.anthropic.claude-opus-5` | Direct mapping |
| `us.anthropic.claude-opus-5:1m` | `eu.anthropic.claude-opus-5:1m` | Direct mapping |
| `us.meta.llama4-maverick-17b-instruct-v1:0` | `eu.anthropic.claude-sonnet-4-5-20250929-v1:0` | **Fallback mapping** |
| `us.meta.llama4-scout-17b-instruct-v1:0` | `eu.anthropic.claude-sonnet-4-5-20250929-v1:0` | **Fallback mapping** |

### Mapping Types

- **Direct Mapping**: US model has exact EU equivalent
- **Fallback Mapping**: US model not available in EU, mapped to best available EU alternative

### US-Only Models (not offered in EU deployments)

The OpenAI frontier models are served via the `bedrock-mantle` endpoint and are
only available in US (and us-gov) regions — there is no EU availability and no
cross-region/geo inference. Rather than mapping them to a Claude fallback, the
`UpdateConfiguration` Lambda **removes** them from the model dropdowns when the
stack is deployed in an EU region, so they are never offered where they cannot
be called.

| Model | Availability |
|-------|--------------|
| `openai.gpt-5.4` | `us-east-1`, `us-east-2`, `us-west-2`, `us-gov-west-1` |
| `openai.gpt-5.5` | `us-east-1`, `us-east-2` |
| `openai.gpt-5.6-sol` | `us-east-1`, `us-east-2` |
| `openai.gpt-5.6-terra` | `us-east-1`, `us-east-2`, `us-west-2` |
| `openai.gpt-5.6-luna` | `us-east-1`, `us-east-2`, `us-west-2` |

See [OpenAI GPT-5.x model support](#openai-gpt-5x-models-bedrock-mantle) below.

### xAI Grok in EU regions

Grok 4.6 has **no `eu.` inference profile**. It is reachable in EU regions only
through the **global** cross-region profile:

| Model | EU availability |
|-------|-----------------|
| `us.xai.grok-4.6` | **None** — `us.`-prefixed IDs return *"The provided model identifier is invalid"* in `eu-west-1` / `eu-central-1` (verified live) |
| `global.xai.grok-4.6` | ✅ All EU regions (also APAC, Canada, and more) |

No special-case filtering was needed: the existing region filter already drops
`us.`-prefixed IDs outside US regions and keeps `global.`-prefixed ones
everywhere, so EU deployments see exactly `global.xai.grok-4.6` in the model
dropdowns. The global profile is also the cheaper of the two
($2.00/$6.00 vs $2.20/$6.60 per 1M). See [xAI Grok Models](grok-models.md).

### Available EU Models

Based on the mappings above, the following EU models are supported:

#### Amazon Nova Models
- `eu.amazon.nova-lite-v1:0`
- `eu.amazon.nova-pro-v1:0`

#### Anthropic Claude Models
- `eu.anthropic.claude-3-haiku-20240307-v1:0`
- `eu.anthropic.claude-haiku-4-5-20251001-v1:0`
- `eu.anthropic.claude-3-5-sonnet-20241022-v2:0`
- `eu.anthropic.claude-3-7-sonnet-20250219-v1:0`
- `eu.anthropic.claude-sonnet-4-20250514-v1:0`
- `eu.anthropic.claude-sonnet-4-5-20250929-v1:0`

## Model Mapping Behavior

### Automatic Region Detection
The system automatically detects the deployment region and applies appropriate model mappings:

- **EU Regions**: Any region starting with `eu-` (e.g., `eu-west-1`, `eu-central-1`)
- **US Regions**: Any region starting with `us-` (e.g., `us-east-1`, `us-west-2`)

### Mapping Logic

1. **US to EU Mapping**: When deployed in EU regions, US model IDs are automatically mapped to their EU equivalents
2. **EU to US Mapping**: When deployed in US regions, EU model IDs are mapped back to US equivalents
3. **Fallback Behavior**: If no mapping exists, the original model ID is returned unchanged

### Configuration Processing

The UpdateConfiguration lambda processes default configurations as follows:

1. **Model ID Detection**: Identifies strings containing `us.` or `eu.` prefixes
2. **Region-Based Swapping**: Swaps model IDs based on deployment region
3. **Logging**: Logs all model swaps for debugging purposes

## Important Limitations

### Fallback Mappings

**⚠️ Critical**: Some US models are not directly available in EU regions and use fallback mappings:

- **Nova Premier**: `us.amazon.nova-premier-v1:0` → `eu.anthropic.claude-sonnet-4-5-20250929-v1:0`
- **Claude Opus Models**: Both Opus variants → `eu.anthropic.claude-sonnet-4-5-20250929-v1:0`

### Implications of Fallback Mappings

1. **Performance Changes**: Fallback models may have different speed/cost characteristics
2. **Behavior Differences**: Different models may produce varying outputs for the same input
3. **Cost Impact**: Fallback models may have different pricing structures

### Third-Party Models (US-Only)

The following third-party models are available in US regions only and have no EU cross-region inference profiles. In EU deployments, the Meta Llama 4 models fall back to Claude Sonnet 4.5, while the single-region models (Gemma, Nemotron) are not available and should not be selected:

- **Meta Llama 4 Maverick 17B**: `us.meta.llama4-maverick-17b-instruct-v1:0` → `eu.anthropic.claude-sonnet-4-5-20250929-v1:0` (fallback)
- **Meta Llama 4 Scout 17B**: `us.meta.llama4-scout-17b-instruct-v1:0` → `eu.anthropic.claude-sonnet-4-5-20250929-v1:0` (fallback)
- **Google Gemma 3 27B IT**: `google.gemma-3-27b-it` — single-region only, no EU availability
- **NVIDIA Nemotron Nano 12B v2**: `nvidia.nemotron-nano-12b-v2` — single-region only, no EU availability
- **Qwen3 VL 235B A22B**: `qwen.qwen3-vl-235b-a22b` — single-region only, no EU availability

### Missing Direct EU Equivalents

The following US models do not have direct EU equivalents:
- Nova Premier
- Claude 3.5 Haiku (specific version)
- Claude Opus 4 variants
- Meta Llama 4 Maverick/Scout (US cross-region only)
- Google Gemma 3, NVIDIA Nemotron, Qwen (single-region US only)

## Best Practices

### For EU Deployments

1. **Verify Model Availability**: Ensure all configured models have EU mappings before deployment
2. **Test Thoroughly**: Test document processing with actual EU model endpoints
3. **Monitor Logs**: Check UpdateConfiguration lambda logs for model swap confirmations

### Recommended EU Models

- **Fastest**: `eu.amazon.nova-lite-v1:0`
- **Balanced**: `eu.amazon.nova-pro-v1:0`
- **Best Quality**: `eu.anthropic.claude-sonnet-4-5-20250929-v1:0`

## Troubleshooting

### Common Issues

1. **Model Not Found Errors**: Check if the model has an EU mapping
2. **Access Denied**: Verify Bedrock model access is enabled for EU models
3. **Unexpected US Model Usage**: Check logs for missing mappings

## Region Availability

This feature supports all AWS regions but is specifically designed for:
- **EU Regions**: `eu-west-1`, `eu-central-1`, `eu-north-1`, etc.
- **US Regions**: `us-east-1`, `us-west-2`, etc.

Other regions (Asia-Pacific, etc.) will use the original model IDs without mapping.

## OpenAI GPT-5.x Models (bedrock-mantle)

> **See [OpenAI GPT-5.x Models](openai-models.md) for the authoritative,
> full support matrix and caveats.** This section summarizes how they differ
> and why they are US-only (and therefore hidden in EU-region deployments).

OpenAI's frontier models (`openai.gpt-5.4`, `openai.gpt-5.5`, and the GPT-5.6
family `openai.gpt-5.6-sol` / `-terra` / `-luna`) are integrated differently
from every other supported model.

### How they differ

| Aspect | Claude / Nova (Converse) | OpenAI GPT-5.x (Responses) |
|--------|--------------------------|----------------------------|
| API | `bedrock-runtime` Converse | `bedrock-mantle` OpenAI Responses API (REST) |
| Endpoint | boto3 `converse()` | `https://bedrock-mantle.{region}.api.aws/openai/v1/responses` |
| Auth | IAM (boto3) | IAM via **SigV4-signed HTTP** (signing name `bedrock-mantle`) |
| Inference params | temperature / top_p / top_k | reasoning models — sampling params omitted; `reasoning.effort` |
| Prompt caching | cachePoint supported | 5.4/5.5 automatic; 5.6 explicit breakpoints (`<<CACHEPOINT>>` → `prompt_cache_breakpoint`) |
| Agentic extraction | supported | **not supported** (rejected by config-validate; raises at runtime) |
| Service tiers | priority / flex / standard | **standard only** |
| Region prefix / `:1m` | yes | none |

The accelerator's `idp_common.bedrock.BedrockClient.invoke_model` detects these
model IDs and routes them to `idp_common/bedrock/openai_responses.py`, which
translates the same inputs to/from the Converse-shaped contract — so all
services (OCR, classification, extraction, assessment, summarization,
evaluation) use them with no special handling. Chat-with-Document also supports
them: the chat processor streams GPT-5.x responses via the Responses API's SSE
stream (`stream_responses_api` in `openai_responses.py`), publishing incremental
token deltas to the UI exactly like the Converse streaming path.

### Regional availability

| Model | In-Region availability | Context window |
|-------|------------------------|----------------|
| `openai.gpt-5.5` | `us-east-1`, `us-east-2` | 272K |
| `openai.gpt-5.4` | `us-east-1`, `us-east-2`, `us-west-2`, `us-gov-west-1` | 272K |
| `openai.gpt-5.6-sol` | `us-east-1`, `us-east-2` | 272K |
| `openai.gpt-5.6-terra` | `us-east-1`, `us-east-2`, `us-west-2` | 272K |
| `openai.gpt-5.6-luna` | `us-east-1`, `us-east-2`, `us-west-2` | 272K |

There is **no EU availability and no geo/global cross-region inference**. When
the IDP stack is deployed in a region where the model is not available, the
request is routed to a known-available `bedrock-mantle` region (a warning is
logged about cross-region data movement). EU-region deployments hide these
models from the dropdowns entirely.

### Reasoning effort

GPT-5.x are reasoning models — they reject `temperature`/`top_p`/`top_k` and are
instead tuned via **reasoning effort**. Each model-selectable service (OCR,
classification, extraction, assessment, summarization, evaluation) exposes a
`reasoning_effort` config field with allowed values `minimal`, `low`, `medium`,
`high` (default `medium`). It is an **OpenAI-only** parameter — ignored by
Anthropic/Nova and other model families. Set it per service in the config (or
the UI editor); for example, in `extraction`:

```yaml
extraction:
  model: "openai.gpt-5.4"
  reasoning_effort: "high"
```

### Configuration / environment variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `BEDROCK_MANTLE_REGION` | Pin the bedrock-mantle region for all GPT-5.x calls | derived from stack region with per-model fallback |
| `BEDROCK_MANTLE_SIGNING_NAME` | SigV4 signing service name | `bedrock-mantle` |
| `BEDROCK_MANTLE_REASONING_EFFORT` | Global fallback reasoning effort when a service config omits `reasoning_effort` | `medium` |

### IAM

Lambda execution roles that perform generation are granted
`bedrock-mantle:CreateInference` (plus `GetProject` / `ListProjects` /
`ListTagsForResources`), equivalent to the AWS-managed
`AmazonBedrockMantleInferenceAccess` policy. When using cross-account Bedrock
via a hub-account role, that role must also grant these `bedrock-mantle`
actions (see [Cross-Account Bedrock](cross-account-bedrock.md)).
