---
title: "OpenAI Models"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# OpenAI Models on Bedrock

The GenAIIDP accelerator supports OpenAI's frontier models on Amazon Bedrock.
They arrive over **two different APIs**, and almost every practical difference
between them follows from that split — so start here before reading either
section.

| | GPT-6 Astra | GPT-5.4 / 5.5 / 5.6 (Sol, Terra, Luna) |
|---|---|---|
| Model IDs | `us.openai.gpt-6-astra`, `global.openai.gpt-6-astra` | `openai.gpt-5.4`, `openai.gpt-5.5`, `openai.gpt-5.6-sol`, `-terra`, `-luna` |
| API | **Bedrock Converse** (`bedrock-runtime`) | **OpenAI Responses API** (`bedrock-mantle`) |
| Agentic extraction | ✅ supported | ❌ |
| Discovery (whole-PDF) | ❌ | ❌ |
| Regions | US geo + **global CRIS worldwide** (incl. EU, APAC) | US in-region only |
| Prompt caching | Implicit, automatic | Automatic (5.4/5.5) or explicit (5.6) |
| Context window | 1.05M | 272K |
| Reasoning effort values | `none`…`max` (no `minimal`) | `minimal`…`high` |

> **The trap:** "OpenAI model" does not imply "Responses API model". Astra is a
> plain Converse model — closer in behaviour to xAI Grok than to GPT-5.6 — and
> the accelerator's capability gates key on the **route**, not the vendor prefix.
> This is why Astra can do agentic extraction and GPT-5.6 cannot.

- [GPT-6 Astra](#gpt-6-astra-converse) — Converse path
- [GPT-5.x](#openai-gpt-5x-models-gpt-54--gpt-55--gpt-56) — bedrock-mantle path

## GPT-6 Astra (Converse)

**GPT-6 Astra** is OpenAI's most capable model on Bedrock, reached through the
ordinary Bedrock Converse API via cross-region inference profiles. Because it is
on the same path as Claude and Nova, it needs none of the mantle machinery below:
no special endpoint, no `BEDROCK_MANTLE_*` environment variables, and no
per-service code.

> **TL;DR** — Astra works for **OCR, classification, extraction (including
> agentic), assessment, summarization, evaluation, and Chat-with-Document**, in
> **every Region** via the `global.` profile. It does **not** work for
> **Discovery** or **Policy Discovery** (no PDF `document` blocks). Implicit
> prompt caching is automatic. Standard service tier only.

### At a glance

| | GPT-6 Astra |
|---|---|
| Model IDs | `us.openai.gpt-6-astra` (US geo), `global.openai.gpt-6-astra` (worldwide) |
| Context window | 1,050,000 tokens |
| Max output tokens | 128,000 |
| Endpoint / API | `bedrock-runtime` — Converse |
| In-Region availability | **None** — CRIS-only (the bare `openai.gpt-6-astra` is rejected on Converse) |
| Geo cross-region (`us.`) | `us-east-1`, `us-east-2`, `us-west-1`, `us-west-2`, `ca-central-1` |
| Global cross-region (`global.`) | Every commercial Region, including all EU and APAC Regions and `sa-east-1` |
| GovCloud | Not available |
| Service tier | Standard only (`flex` / `priority` rejected) |
| Prompt caching | **Implicit and automatic** — no `<<CACHEPOINT>>` needed (explicit blocks are rejected) |
| Input modalities | Text, image (**no** PDF `document` blocks) |
| Reasoning effort | `none`, `low`, `medium`, `high`, `xhigh`, `max` |
| Price / 1M (in / cache-read / out) | `us.` $11.00 / $1.10 / $55.00 · `global.` $10.00 / $1.00 / $50.00 |

Prefer `global.openai.gpt-6-astra` unless you have data-residency requirements
that need the US geo profile: it is available in more Regions **and** costs about
10% less.

### Long-context pricing — read this before sending large prompts

Astra bills in **two context bands**, and the band applies to the whole request:

| Input size | Input / 1M | Output / 1M |
|---|---|---|
| ≤ 272K tokens | $11.00 (`us.`) / $10.00 (`global.`) | $55.00 / $50.00 |
| > 272K tokens | **$22.00 / $20.00** | **$82.50 / $75.00** |

`config_library/model_config_limits.yaml` allows the full 1.05M window, while
`config_library/pricing.yaml` records only the **short-band** rates — the metering
schema has one price per unit per model and cannot express a usage-dependent
band. So for prompts above 272K tokens the accelerator's cost reports
**under-report** actual spend (up to 2× on input, 1.5× on output).

**You do not have to do anything unusual to cross that line** — the accelerator's
own auto-sizing derives its budgets from the declared window, so two ordinary
paths go over it by default:

| Path | Derived budget with Astra | vs the 272K boundary |
|---|---|---|
| Agentic extraction shard budget (`idp_common/bedrock/sizing.py`) | **613,400 tokens** — the largest of any model offered (Claude Opus 5 `:1m` is 578,400; Grok 90,500) | 2.3× over |
| Summarization prompt budget (`idp_common/summarization/service.py`) | **892,500 tokens** (85% of the window) before any truncation | 3.3× over |

So a large document summarized or agentically extracted with Astra will typically
bill in the long band while the cost report shows short-band rates. Two ways to
remove the discrepancy:

- **Cap the window** — set `max_input_tokens: 272000` for the
  `openai\.gpt-6-astra` pattern in `model_config_limits.yaml`. Both budgets above
  are derived from that number, so capping it keeps every request in the short band
  and cost stays exactly as reported; large documents shard as they do for other
  models. This is the recommended setting if predictable cost matters more than
  window size.
- **Reprice for the long band** — edit the two `bedrock/*.openai.gpt-6-astra`
  rows to the long-band rates, which then over-reports ordinary requests.

### Prompt caching

Astra caches **implicitly**: repeat a prompt prefix and it is reused with no
request change at all. Verified live — a repeated 2,707-token prefix billed
`inputTokens=2` with `cacheReadInputTokens=2707`, a 10× saving on the cached
portion. Because the discount lands in the standard `cacheReadInputTokens`
metering field, cost reports pick it up automatically.

Do **not** add `<<CACHEPOINT>>` markers for Astra: explicit `cachePoint` blocks
raise `AccessDeniedException`, so the accelerator strips the markers and keeps
Astra out of `CACHEPOINT_SUPPORTED_MODELS`.

### What is and is not supported

| Capability | Supported? | Notes |
|---|---|---|
| OCR (Bedrock backend) | ✅ | Image + text input |
| Classification | ✅ | Page-level and holistic |
| Extraction (standard) | ✅ | Text + page images |
| **Agentic / advanced extraction** | ✅ | Astra reaches Converse and emits `toolUse` under a forced `toolChoice`. **This is the key difference from GPT-5.x.** |
| Confidence (assessment) | ✅ | |
| Summarization, Evaluation (LLM method) | ✅ | |
| Chat-with-Document | ✅ | Streaming via ConverseStream |
| Guardrails | ✅ | Converse API only |
| Application inference profiles | ✅ | For cost-allocation tagging |
| **Discovery** (classes / ground-truth / auto-split) | ❌ | Rejects `document` blocks ("This model doesn't support the document field for user messages"). Rejected by `config-validate` and guarded at runtime. |
| **Policy / Rule Discovery** | ❌ | Same limitation. |
| PDF `document` input blocks | ❌ | Text and images only. |
| Explicit prompt caching (`<<CACHEPOINT>>`) | ❌ | Implicit caching is automatic instead — see above. |
| Service tiers (`:priority` / `:flex`) | ❌ | Standard only. |
| `temperature` / `top_p` / `top_k` | ❌ | Reasoning model — these are **rejected with a 400**, not ignored. Use `reasoning_effort`. |
| In-Region inference | ❌ | CRIS-only; name the `us.` or `global.` profile. |
| GovCloud | ❌ | Not in the model's Region list. |

### Reasoning effort

Astra accepts `none`, `low`, `medium`, `high`, `xhigh`, `max` — Claude's set plus
`none`. Note the two easy mistakes: `minimal` is a **GPT-5.x** value and is
rejected by Astra, and Astra accepts `max` where xAI Grok rejects it. Values
outside the vocabulary are dropped with a warning rather than sent.

```yaml
extraction:
  model: "global.openai.gpt-6-astra"
  reasoning_effort: "low"   # none | low | medium | high | xhigh | max
```

### `bedrock-mantle` and Astra

The model card also lists Astra on the `bedrock-mantle` Responses API, but only in
`us-west-2`, with no cross-region inference and no application inference profiles.
The accelerator deliberately does **not** use that route: Converse already
provides worldwide Regions, tool use, guardrails, cost-allocation profiles and
prompt caching, while mantle would add only server-side tool calling and explicit
cache breakpoints. Astra is therefore never routed through
`openai_responses.py` — a behaviour pinned by
`tests/unit/test_bedrock_astra.py`.

### IAM

No additional permissions are required. The generation Lambda roles already grant
`bedrock:InvokeModel*` on `foundation-model/*` plus `inference-profile/*` and
`application-inference-profile/*`, which covers Astra. The `bedrock-mantle:*`
actions those roles also hold are for GPT-5.x and are unused by Astra.

## OpenAI GPT-5.x Models (GPT-5.4 / GPT-5.5 / GPT-5.6)

The accelerator also supports OpenAI's GPT-5.x models on Bedrock:
**GPT-5.4** (`openai.gpt-5.4`), **GPT-5.5** (`openai.gpt-5.5`), and the
**GPT-5.6** family — **Sol** (`openai.gpt-5.6-sol`, flagship reasoning),
**Terra** (`openai.gpt-5.6-terra`, GPT-5.5-class quality at roughly half the
cost), and **Luna** (`openai.gpt-5.6-luna`, fastest / lowest cost).

Unlike Astra above and every other model in the accelerator, these are **not**
served on the Bedrock Converse / InvokeModel APIs. They are available only on the
**`bedrock-mantle` endpoint via the OpenAI Responses API**. The accelerator
hides this difference behind the existing `idp_common` Bedrock client: when a
model ID starting with `openai.gpt-5` is selected, `BedrockClient.invoke_model`
transparently routes the request to a SigV4-signed HTTP call against
`bedrock-mantle` (see `idp_common/bedrock/openai_responses.py`) and returns the
same response/metering shape every service already expects — so no per-service
code changes are required.

> **TL;DR** — all GPT-5.x models work for **OCR, classification, extraction,
> assessment, summarization, evaluation, and Chat-with-Document**. They do
> **not** work for **agentic extraction**, **Discovery**, or **Policy
> Discovery**, and are available in **US regions only**. GPT-5.6 adds prompt
> caching (see below). See the support matrix below.

### GPT-5.x at a glance

| | GPT-5.4 | GPT-5.5 | GPT-5.6 Sol | GPT-5.6 Terra | GPT-5.6 Luna |
|---|---|---|---|---|---|
| Model ID | `openai.gpt-5.4` | `openai.gpt-5.5` | `openai.gpt-5.6-sol` | `openai.gpt-5.6-terra` | `openai.gpt-5.6-luna` |
| Context window | 272K | 272K | 272K | 272K | 272K |
| Max output tokens (capped by accelerator) | 128,000 | 128,000 | 128,000 | 128,000 | 128,000 |
| Endpoint | `bedrock-mantle` (Responses API) | ← | ← | ← | ← |
| In-Region availability | `us-east-1`, `us-east-2`, `us-west-2`, `us-gov-west-1` | `us-east-1`, `us-east-2` | `us-east-1`, `us-east-2` | `us-east-1`, `us-east-2`, `us-west-2` | `us-east-1`, `us-east-2`, `us-west-2` |
| Geo / Global cross-region inference | Not available | ← | ← | ← | ← |
| Service tier | Standard only | ← | ← | ← | ← |
| Prompt caching | Automatic (prefix > 1,024 tokens) | Automatic | **Explicit** breakpoints | **Explicit** | **Explicit** |
| Price / 1M (in / cache-read / out) | $2.75 / $0.275 / $16.50 | $5.50 / $0.55 / $33.00 | $5.50 / $0.55 / $33.00 | $2.75 / $0.28 / $16.50 | $1.10 / $0.11 / $6.60 |

There are **no** `eu.*` or `global.*` variants and **no** `:1m` context suffix —
the model IDs carry no region prefix. GPT-5.6 Sol is **not** available in
`us-west-2` (Terra and Luna are). GovCloud (`us-gov-west-1`) offers GPT-5.4 only.

### GPT-5.x prompt caching

`GPT-5.4`/`GPT-5.5` cache **automatically** — any prompt prefix over ~1,024
tokens is eligible for reuse with **no request changes** (the cache is populated
server-side after the prefix is first seen, so hits register on repeat calls),
and there is **no separate cache-write charge**. `<<CACHEPOINT>>` markers are
simply stripped for these models. (Verified live for GPT-5.5: `cached_tokens`
began registering on a repeated >1,024-token prefix with `cache_write_tokens`
staying 0.)

`GPT-5.6` (Sol/Terra/Luna) uses **explicit** caching: place a `<<CACHEPOINT>>`
marker at the end of the static portion of your prompt and the client translates
it into the Responses API's `prompt_cache_options` / `prompt_cache_breakpoint`
fields with a deterministic `prompt_cache_key` derived from the cached prefix.
Cache reads are billed at a 90% discount; GPT-5.6 also has a (30-minute)
cache-write price (reflected in `config_library/pricing.yaml`). Both are metered
via `cacheReadInputTokens` / `cacheWriteInputTokens`.

> **Token accounting note.** The OpenAI Responses `usage.input_tokens` is the
> *total* prompt size and already **includes** the cached / cache-written
> tokens. The accelerator's metering reports `inputTokens` as the **disjoint**
> fresh (uncached) count — `input_tokens − cached − cache_write` — so a cached
> token is billed once (at the cache rate), not twice. This matches the Bedrock
> Converse convention the cost model assumes. Verified live: a warm GPT-5.6
> extraction with `input_tokens=4508` / `cached=3193` reports
> `inputTokens=1315` + `cacheReadInputTokens=3193` (which reconcile to 4508).

### What is supported (GPT-5.x)

| Capability | Supported? | Notes |
|---|---|---|
| OCR (Bedrock backend) | ✅ | Image + text input |
| Classification | ✅ | Page-level and holistic |
| Extraction (standard) | ✅ | Text + page images |
| Confidence (assessment) | ✅ | `separate` and `integrated` modes on the simple (non-agentic) path |
| Summarization | ✅ | |
| Evaluation (LLM method) | ✅ | |
| Chat-with-Document | ✅ | **Streaming** — token deltas stream to the UI via the Responses SSE stream |
| Text input | ✅ | |
| Image input | ✅ | Page images are sent as image content |
| Reasoning effort control | ✅ | New `reasoning_effort` config field (see below) |
| Guardrails | ✅ | Applied via the standard headers on the mantle endpoint |

### What is NOT supported (GPT-5.x)

| Capability | Supported? | Why / what happens |
|---|---|---|
| **Agentic extraction** (`extraction.agentic.enabled: true`) | ❌ | The agentic path uses the Strands framework over the Converse API, which GPT-5.x doesn't support. This combination is a **hard error** in `idp-cli config-validate` and **raises at runtime**. |
| **Discovery** (classes / without- & with-ground-truth / auto-split) | ❌ | Discovery ingests whole PDFs as Converse `document` blocks, which the Responses API cannot accept (text + image only). Rejected by `config-validate` and **guarded at runtime**. |
| **Policy / Rule Discovery** | ❌ | Same PDF-document-block limitation; agentic rule discovery also uses Strands. Rejected by `config-validate` and guarded at runtime. |
| PDF `document` input blocks | ❌ | The Responses API accepts text and images only. Pipelines that need whole-PDF ingestion should use a Claude or Nova model. |
| Prompt caching (`<<CACHEPOINT>>`) | ✅ (5.6) / auto (5.4/5.5) | GPT-5.6 translates `<<CACHEPOINT>>` into explicit Responses cache breakpoints; GPT-5.4/5.5 cache automatically for prefixes > 1,024 tokens (markers stripped). See [Prompt caching](#prompt-caching). |
| Service tiers (`:priority` / `:flex`) | ❌ | Standard tier only. |
| `temperature` / `top_p` / `top_k` | ❌ | These are reasoning models; sampling parameters are ignored. Use `reasoning_effort` instead. |
| EU / global cross-region inference | ❌ | US (and us-gov) in-region only; hidden in EU-region deployments. |

### GPT-5.x reasoning effort

GPT-5.x are reasoning models — they reject `temperature` / `top_p` / `top_k` and
are instead tuned with **reasoning effort**. Each model-selectable service (OCR,
classification, extraction, assessment, summarization, evaluation, and
Chat-with-Document) exposes a `reasoning_effort` config field.

`reasoning_effort` applies to **any reasoning-capable model**, not just OpenAI:

| Model family | Allowed values | Mechanism |
|---|---|---|
| OpenAI GPT-5.x | `minimal`, `low`, `medium`, `high` | Responses API `reasoning.effort` |
| OpenAI GPT-6 Astra | `none`, `low`, `medium`, `high`, `xhigh`, `max` | Converse `additionalModelRequestFields.reasoning.effort` |
| xAI Grok | `none`, `low`, `medium`, `high`, `xhigh` (**not** `max`) | Converse `additionalModelRequestFields.reasoning.effort` |
| Claude Sonnet 5 / Sonnet 4.6 / Opus 4.5–4.8 / Fable 5 | `low`, `medium`, `high`, `xhigh`, `max` | Bedrock Converse `output_config.effort` |

The vocabularies genuinely differ — `minimal` is GPT-5.x-only, `none` is not a
Claude value, and `max` is valid everywhere except Grok. The config UI offers the
union and each backend drops what its model would reject, so a value that does not
apply is logged and omitted rather than causing a 400.

It is **ignored** by models without an effort control — Amazon Nova, Claude
Sonnet 4.5, and Claude Haiku 4.5. In the config UI, the **Reasoning effort**
selector appears only when the section's selected model supports it.

**Extraction defaults to `low`.** A full effort sweep (5 extraction methods ×
{low, medium, high, xhigh} × 2 datasets × 20 docs) found higher effort adds
output-token cost with negligible extraction-accuracy gain, so `low` keeps the
Claude Sonnet 5 default affordable. Raise it per-config for genuinely
reasoning-heavy documents. Other services default to `medium`.

```yaml
extraction:
  model: "openai.gpt-5.4"
  reasoning_effort: "high"   # OpenAI: minimal | low | medium | high

# or, for a reasoning-capable Claude model:
extraction:
  model: "us.anthropic.claude-sonnet-5"
  reasoning_effort: "low"    # Claude: low | medium | high | xhigh | max
```

### GPT-5.x regional availability and routing

GPT-5.4 is available in `us-east-1`, `us-east-2`, `us-west-2`, and
`us-gov-west-1`; GPT-5.5 in `us-east-1` and `us-east-2`. For GPT-5.6, Sol is in
`us-east-1` and `us-east-2`; Terra and Luna add `us-west-2`. There is no EU
availability and no geo/global cross-region inference, and GovCloud offers
GPT-5.4 only.

If the IDP stack is deployed in a region where the selected model is not
available, the accelerator routes the `bedrock-mantle` request to a
known-available region (logging a warning about cross-region data movement). To
pin the region explicitly, set `BEDROCK_MANTLE_REGION`. EU-region deployments
**hide** these models from the configuration picklists entirely (they are not
callable there). See [EU Region Model Support](eu-region-model-support.md).

### GPT-5.x IAM

Lambda execution roles that perform generation are granted the
`bedrock-mantle:CreateInference` action (plus `GetProject` / `ListProjects` /
`ListTagsForResources`) — equivalent to the AWS-managed
`AmazonBedrockMantleInferenceAccess` policy. When routing Bedrock through a
cross-account hub role, that role must also grant these `bedrock-mantle` actions
— see [Cross-Account Bedrock](cross-account-bedrock.md).

### GPT-5.x environment variables

| Variable | Purpose | Default |
|---|---|---|
| `BEDROCK_MANTLE_REGION` | Pin the `bedrock-mantle` region for all GPT-5.x calls | Derived from the stack region with a per-model fallback |
| `BEDROCK_MANTLE_SIGNING_NAME` | SigV4 signing service name | `bedrock-mantle` |
| `BEDROCK_MANTLE_REASONING_EFFORT` | Global fallback reasoning effort when a service config omits `reasoning_effort` | `medium` |

## Pricing

Pricing for every OpenAI model is defined in `config_library/pricing.yaml` and
matches OpenAI first-party rates on Bedrock (per 1M tokens):

| Model | Input | Cache write (30m) | Cache read | Output |
|---|---|---|---|---|
| GPT-6 Astra (`us.`) | $11.00 | $13.75 | $1.10 | $55.00 |
| GPT-6 Astra (`global.`) | $10.00 | $12.50 | $1.00 | $50.00 |
| GPT-5.4 | $2.75 | — | $0.275 | $16.50 |
| GPT-5.5 | $5.50 | — | $0.55 | $33.00 |
| GPT-5.6 Sol | $5.50 | $6.88 | $0.55 | $33.00 |
| GPT-5.6 Terra | $2.75 | $3.44 | $0.28 | $16.50 |
| GPT-5.6 Luna | $1.10 | $1.38 | $0.11 | $6.60 |

The Astra rows are the **short-context (≤272K) band**; larger prompts bill at
roughly double — see [Long-context pricing](#long-context-pricing--read-this-before-sending-large-prompts).
GPT-5.4/5.5 cache automatically and have no cache-write cost. GPT-5.6 caches via
explicit breakpoints and bills a 30-minute cache-write. The GPT-5.x rows are
in-region on-demand rates. Confirm against the
[Amazon Bedrock pricing page](https://aws.amazon.com/bedrock/pricing/) if rates
change.

Astra is the most expensive model the accelerator offers — roughly 2× GPT-5.6 Sol
on input and 4× Claude Sonnet 5 — so reach for it where its capability earns the
cost, and rely on its automatic prompt caching (a 10× discount on repeated
prefixes) to keep steady-state extraction affordable.

## Choosing a model

Use **GPT-6 Astra** when you want OpenAI's strongest model and need either
**agentic extraction** or **non-US Regions** — it is the only OpenAI option that
supports both. Use `global.` for the wider Region coverage and lower price.

Use **GPT-5.4/5.5/5.6** for OCR, classification, extraction, assessment,
summarization, evaluation, or chat in US Regions where their reasoning quality
helps at a materially lower price than Astra, and inputs are text or page images.

For workloads that require **whole-PDF ingestion** (Discovery, Policy Discovery),
choose a Claude or Nova model — no OpenAI model on Bedrock accepts PDF `document`
blocks. For agentic extraction, Astra, Claude, Nova and xAI Grok all work; GPT-5.x
does not.
