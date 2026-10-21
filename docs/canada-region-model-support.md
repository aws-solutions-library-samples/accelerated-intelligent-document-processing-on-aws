---
title: "Canada (Central) Region Model Support"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Canada (Central) Region Model Support

## Overview

The accelerator's region-aware model handling covers Canada. When the stack is
deployed in a `ca-*` region, the `UpdateConfiguration` custom resource
(`src/lambda/update_configuration/index.py`) resolves the stack region to its
own region type, `ca`, and applies the two deploy-time steps it already applies
for US and EU stacks: it limits the model picklists to models that can be called
from ca-central-1, and it swaps the shipped defaults that cannot. `us-gov-*`
regions keep resolving to the US type.

The handling has been validated in `ca-central-1` (live checks, 2026-10).
`ca-west-1` receives the same handling but has not been validated. The EU
counterpart, with its full US-to-EU mapping table, is
[EU Region Model Support and Mapping](./eu-region-model-support.md).

## What a Canada stack offers

Every `model` / `model_id` picklist in the configuration schema is filtered by
id prefix when the stack is created or updated:

| Ids | In a Canada stack | Why |
|-----|-------------------|-----|
| `ca.*` | kept | Canada geo inference profile |
| `us.*`, `global.*`, unprefixed ids, `LambdaHook` | kept, except the deny list below | callable from ca-central-1 |
| `eu.*` | dropped | not callable from ca-central-1 |
| `openai.gpt-5.4`, `openai.gpt-5.5`, `openai.gpt-5.6-sol` / `-terra` / `-luna` | dropped | bedrock-mantle ids with no Canada availability; the client would route them to `us-east-1` |

The deny list (`CA_UNAVAILABLE_MODELS`) holds the shipped ids that have no
inference profile and no in-region hosting in ca-central-1 (verified 2026-10-11):

- `us.amazon.nova-lite-v1:0` — swapped to `ca.amazon.nova-lite-v1:0`, see below
- `us.amazon.nova-pro-v1:0` and `global.amazon.nova-pro-v1:0`
- `us.cohere.embed-v4:0` — swapped to `global.cohere.embed-v4:0`, see below
- `us.meta.llama4-maverick-17b-instruct-v1:0`, `us.meta.llama4-scout-17b-instruct-v1:0`
- `qwen.qwen3-vl-235b-a22b`, `google.gemma-3-27b-it`, `nvidia.nemotron-nano-12b-v2`

`us.xai.grok-4.6` and `us.openai.gpt-6-astra` have profiles in ca-central-1 and
stay in the picklists, as do the `:priority` and `:flex` service-tier variants of
`us.amazon.nova-2-lite-v1:0` and `global.amazon.nova-2-lite-v1:0`, which Bedrock
accepts from ca-central-1 (verified 2026-10-12).

## Default model changes

Three shipped defaults name models that cannot be called from ca-central-1. On
stack create and update, the custom resource rewrites them in the ConfigSchema,
the shipped default configuration and the managed configurations through a
one-way Canada table (`CA_MODEL_MAPPINGS`). Configuration versions you saved
yourself are not rewritten, the same as in EU regions.

| Setting | Shipped default | In a Canada stack | Why |
|---------|-----------------|-------------------|-----|
| Confidence assessment (`extraction.confidence.model`) | `us.amazon.nova-lite-v1:0` | `ca.amazon.nova-lite-v1:0` | Same model through its Canada geo profile; processes in Canada |
| Discovery, with and without ground truth (`model_id`) | `us.amazon.nova-pro-v1:0` | `us.amazon.nova-2-lite-v1:0` | No Nova Pro profile in ca-central-1. Nova 2 Lite is the catalog's Nova default for OCR, classification and evaluation, and is multimodal and cheaper |
| Multi-document discovery embedding (`multi_document.embedding_model_id`) | `us.cohere.embed-v4:0` | `global.cohere.embed-v4:0` | Only the global profile exists in Canada. The swap is ca-central-1-only in effect: this profile does not exist in ca-west-1 either, so a ca-west-1 stack moves from one id it cannot call to another |

The two text-generation ids swapped in above are in the Canada picklists.
`multi_document.embedding_model_id` has no picklist — it is a free-text field
with a `default:` and no `enum` — so its value is only ever set by this swap or
by hand.

`ca.amazon.nova-lite-v1:0` is new to the catalog with this support, and it is
the only `ca.` profile AWS offers. It is in every model dropdown where its EU
twin is, in the `KnowledgeBaseModelId` AllowedValues, priced with Canada
(Central) rates ($0.064 input, $0.256 output and $0.016 cache read per 1M
tokens; cache writes are billed at the input rate, like the other Nova rows),
and prompt caching is enabled for it (`CACHEPOINT_SUPPORTED_MODELS`). The `ca.`
prefix is recognised by the shared region-prefix list, so the client resolves
it to the same base model as the `us.` and `eu.` Nova Lite rows.

## Where requests are processed

The stack calls the ca-central-1 Bedrock endpoint for everything, and all data
at rest stays in ca-central-1. Cross-Region inference, however, may process a
request outside Canada; which Regions depends on the profile prefix:

| Profile | Requests processed in |
|---------|-----------------------|
| `ca.*` | `ca-central-1`, `ca-west-1` |
| `us.*` | US Regions |
| `global.*` | any commercial AWS Region |
| BDA `na.data-automation-v1` | `ca-central-1`, `ca-west-1`, `us-east-1`, `us-east-2`, `us-west-2` |

`ca.amazon.nova-lite-v1:0` is therefore the only model in the catalog whose
requests stay in Canada. See
[Cross-Region inference](https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html)
and
[Cross Region support required for Bedrock Data Automation](https://docs.aws.amazon.com/bedrock/latest/userguide/bda-cris.html)
in the Amazon Bedrock User Guide.

## Bedrock Data Automation

Canada stacks use the `na.data-automation-v1` cross-Region profile — AWS names
the Canada (Central) profile `na.`, and `ca.data-automation-v1` is not a valid
profile — so BDA mode (`use_bda`, which calls the asynchronous API) works.

The BDA **OCR backend** (`ocr.backend: bda`) does not. It needs a stack-scoped
SYNC document project, and BDA's synchronous API rejects that project in
ca-central-1 with a `ValidationException`. The `BDAOCRProjectFunction` custom
resource treats the refusal as "unsupported here": it returns success with an
empty project ARN and logs **one ERROR line** (`Bedrock Data Automation
rejected the stack-scoped OCR project as unsupported in this region ...;
returning empty ProjectArn`). That line is expected, and the stack deploys. The
OCR service raises a clear error only if `ocr.backend: bda` is selected; use
the Textract (default) or Bedrock OCR backend instead (see
[When to choose BDA vs Textract for OCR](./configuration.md#when-to-choose-bda-vs-textract-for-ocr)).
An asynchronous OCR path is a possible follow-up.

## Knowledge Base model

`KnowledgeBaseModelId` is a stack parameter, so the configuration step does not
swap it. Its default, `us.amazon.nova-pro-v1:0`, has no inference profile in
ca-central-1, so the root template swaps it instead: a stack in a Canada region
that leaves the parameter at its default gives the Knowledge Base
`ca.amazon.nova-lite-v1:0`, which keeps Knowledge Base answers in Canada (the
stack parameter itself still shows the default). An explicit choice is passed
through unchanged, so it must be one ca-central-1 can call, such as
`us.amazon.nova-2-lite-v1:0`, `us.anthropic.claude-sonnet-4-5-20250929-v1:0` or
`global.anthropic.claude-sonnet-4-5-20250929-v1:0`. The
Knowledge Base embedding model, `amazon.titan-embed-text-v2:0`, is supported
in ca-central-1. See [Document Knowledge Base Query](./knowledge-base.md#configuration).

## Known limitations

- **Capacity planner.** `calculate_capacity` has no Service Quotas keys for
  `ca.` (or `eu.`) profiles, so capacity estimates do not cover
  `ca.amazon.nova-lite-v1:0`. This is a pre-existing gap shared with EU; see
  [Capacity Planning](./capacity-planning.md).
- **GPT-5.x ids typed into a custom configuration.** The picklists hide them,
  but an `openai.gpt-5.*` id written into a configuration by hand still routes
  to `us-east-1` through bedrock-mantle, with only a warning logged (same as
  EU). Avoid them in Canada.
- **The Canada swaps are one-way.** A configuration that holds a `ca.` id keeps
  it when carried into a US or EU stack, and an `eu.` id stored in a Canada
  stack is not rewritten. The picklists hide both, but a stored value stays
  until someone edits it.
- **Embedding calls are not costed.** Multi-document discovery's embedding
  model (`global.cohere.embed-v4:0` in Canada, `us.cohere.embed-v4:0`
  elsewhere) emits no metering entry — `generate_embedding` returns a bare
  vector — and neither id has a row in `config_library/pricing.yaml`, so
  embedding usage does not appear in cost reporting. Pre-existing and not
  specific to Canada. The coverage gates record this per id rather than as a
  blanket rule for embeddings: `_COVERAGE_EXEMPT` in
  `lib/idp_common_pkg/tests/unit/reporting/test_pricing_lookup.py` and
  `EMBEDDING_MODELS` in `scripts/tests/test_model_surface_consistency.py` name
  the Knowledge Base embedding models, and `NON_SELECTABLE_DEFAULTS` names
  `us.cohere.embed-v4:0`. `global.cohere.embed-v4:0` is in none of them, and
  needs no entry only because it reaches a stack through the swap table rather
  than through any surface those gates walk — so a shipped preset or code
  default naming it would fail them with no entry to point at.
- **ca-west-1 is handled, but the model lists are derived from ca-central-1.**
  A `ca-west-1` stack resolves to the same `ca` region type and gets the same
  picklists, swaps and Knowledge Base default, which is validated for
  ca-central-1 only. Checked live on 2026-10-13: of the 47 ids the Canada
  picklists offer, four cannot be called from ca-west-1 —
  `global.xai.grok-4.6`, `us.anthropic.claude-haiku-4-5-20251001-v1:0`,
  `us.anthropic.claude-opus-4-5-20251101-v1:0` and
  `us.anthropic.claude-sonnet-4-5-20250929-v1:0` — and
  `global.cohere.embed-v4:0`, the multi-document discovery embedding default,
  has no ca-west-1 profile. `ca.amazon.nova-lite-v1:0`, which the Knowledge
  Base condition selects, is callable there. Verify with the command in
  [Verifying model availability](#verifying-model-availability) against your
  own Region before relying on a model.
- **Claude Sonnet 5.5 is offered in Canada and its ca-west-1 status is open.**
  `us.anthropic.claude-sonnet-5-5` and `global.anthropic.claude-sonnet-5-5` are
  both ACTIVE in ca-central-1 and the bare `anthropic.claude-sonnet-5-5` is hosted
  in-Region there (checked 2026-10-14), which is why the model carries no
  `CA_UNAVAILABLE_MODELS` entry. Neither that nor its two Service Quotas codes
  (`L-94A31E46`, `L-31AB82D0`) has been resolved from ca-west-1. The quota codes
  matter more than the usual picklist question there: `CalculateCapacity` re-raises
  any non-throttling error from `get_service_quota`, so a code that does not
  resolve fails the **whole** Capacity Planning response rather than one row. Run
  the availability command below plus `aws service-quotas get-service-quota
  --service-code bedrock --quota-code L-94A31E46` from a ca-west-1 stack before
  relying on either.

## Deploying to ca-central-1

ca-central-1 is one of the four Regions each release is published to, so it has
a Launch Stack button in the [Deployment Guide](./deployment.md) and a plain
`idp-cli deploy --region ca-central-1` resolves the published template without
your having to name one. ⚠️ **0.6.11 and every release before it have no
ca-central-1 artifacts** — Canada joined the published set in 0.6.12, so both
routes work only from that release on, and until it is published the Launch
Stack button answers 404. To deploy an earlier release, or unreleased code,
supply the template yourself in one of two forms:

```bash
# Build, publish to a bucket in your account and deploy in one step
idp-cli deploy --stack-name my-idp --region ca-central-1 --from-code . \
    --admin-email your.email@example.com --wait

# Or publish first, then deploy the published template
idp-cli publish --source-dir . --region ca-central-1
idp-cli deploy --stack-name my-idp --region ca-central-1 \
    --template-url <template URL printed by publish> \
    --admin-email your.email@example.com --wait
```

The general build-from-source procedure, including the prerequisites, is in the
[Deployment Guide](./deployment.md#option-3-build-and-deploy-from-source-code).

## Verifying model availability

The authoritative list of what ca-central-1 can call is the set of active
system-defined inference profiles:

```bash
aws bedrock list-inference-profiles --region ca-central-1 --type-equals SYSTEM_DEFINED \
  --query "inferenceProfileSummaries[?status=='ACTIVE'].inferenceProfileId" --output text \
  | tr '\t' '\n' | sort
```

A prefixed id that is not in this list cannot be called from a Canada stack.
The deny list above was built from this command (plus
`aws bedrock list-foundation-models --region ca-central-1` for the unprefixed
ids) on 2026-10-11; run them again before relying on a model this page does not
mention.
