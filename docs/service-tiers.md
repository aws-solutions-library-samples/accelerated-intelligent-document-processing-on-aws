---
title: "Amazon Bedrock Service Tiers"
---

# Amazon Bedrock Service Tiers

The GenAI IDP solution supports Amazon Bedrock service tiers through model ID suffixes, allowing you to optimize for performance and cost by selecting different service tiers for model inference operations.

## Overview

Amazon Bedrock offers three service tiers for on-demand inference:

| Tier | Performance | Cost | Best For |
|------|-------------|------|----------|
| **Priority** | Fastest response times | Premium pricing (~25% more) | Customer-facing workflows, real-time interactions |
| **Standard** | Consistent performance | Regular pricing | Everyday AI tasks, content generation |
| **Flex** | Variable latency | Discounted pricing | Batch processing, evaluations, non-urgent workloads |

## Model ID Suffix Format

Service tiers are specified using model ID suffixes:

**Format:** `<base-model-id>[:<service-tier>]`

**Examples:**
- `us.amazon.nova-2-lite-v1:0` → Standard tier (default, no suffix)
- `us.amazon.nova-2-lite-v1:0:flex` → Flex tier
- `us.amazon.nova-2-lite-v1:0:priority` → Priority tier
- `global.amazon.nova-2-lite-v1:0:flex` → Global model with Flex tier

## Available Models with Service Tiers

The following Nova 2 Lite models are available with service tier suffixes:

**US Region Models:**
- `us.amazon.nova-2-lite-v1:0` (Standard - default)
- `us.amazon.nova-2-lite-v1:0:flex` (Flex tier)
- `us.amazon.nova-2-lite-v1:0:priority` (Priority tier)

**Global Models:**
- `global.amazon.nova-2-lite-v1:0` (Standard - default)
- `global.amazon.nova-2-lite-v1:0:flex` (Flex tier)
- `global.amazon.nova-2-lite-v1:0:priority` (Priority tier)

> **Note:** OpenAI GPT-5.x models (`openai.gpt-5.4`, `openai.gpt-5.5`, and
> GPT-5.6 Sol/Terra/Luna) support the **Standard** tier only — the `:flex` and
> `:priority` suffixes do not apply.
> See [OpenAI GPT-5.x Models](openai-models.md).

> **Note:** xAI Grok 4.6 (`us.xai.grok-4.6`, `global.xai.grok-4.6`) also supports
> the **Standard** tier only, so no `:flex` / `:priority` Grok IDs are offered.
> This contradicts the AWS model card, which advertises Priority (1.75x) and Flex
> (0.5x): Converse in fact returns *"The provided service tier is not supported
> for this model"* for both, on both model IDs, in `us-west-2` and `us-east-1`
> (verified live 2026-09-02). See [xAI Grok Models](grok-models.md).

> **Note:** OpenAI GPT-6 Astra (`us.openai.gpt-6-astra`,
> `global.openai.gpt-6-astra`) also supports the **Standard** tier only. Here the
> model card agrees — it lists Standard alone — and Converse confirms it, returning
> *"The provided service tier is not supported for this model"* for both `flex` and
> `priority` (verified live 2026-09-10). No `:flex` / `:priority` Astra IDs are
> offered. See [OpenAI Models](openai-models.md#gpt-6-astra-converse).

## Configuration

### Using Model IDs with Service Tier Suffixes

Simply specify the model ID with the desired tier suffix in your configuration:

```yaml
classification:
  # ... other settings
  one_stage:
    llm:
      model: "us.amazon.nova-2-lite-v1:0:priority"  # Fast classification

extraction:
  model: "us.amazon.nova-2-lite-v1:0:flex"  # Cost-effective extraction
  confidence:
    model: "us.amazon.nova-2-lite-v1:0"     # Standard tier (no suffix)
  # ... other settings
```

⚠️ **The confidence model lives under `extraction.confidence`, not in a top-level
`assessment:` block.** A top-level `assessment:` key has no effect in any current
config: a v0.5-shaped config has it rewritten into `extraction.confidence` /
`extraction.geometry` / `hitl` and the block itself removed, and a config that is
already v0.6-shaped has it ignored as an unknown key on load. Either way nothing warns
you — a tier suffix written there is silently not applied, and the confidence calls run
on whatever `extraction.confidence.model` resolves to.

⚠️ **Classification has one model field per call, so a tier suffix has to be
applied to the call that runs.** The `one_stage.llm.model` above is read only
under `classification.strategy: single_pass` — the default, labelled *One stage*
in the configuration editor. Under `strategy: two_stage` it is not read at all,
and there are **two** fields to set:

```yaml
classification:
  strategy: two_stage
  two_stage:
    segmentation:
      llm:
        model: "us.amazon.nova-2-lite-v1:0:flex"      # boundaries can wait
    section_classification:
      llm:
        model: "us.amazon.nova-2-lite-v1:0:priority"  # the class gates extraction
```

The two may legitimately differ, and that pairing is the obvious mixed-tier case:
a `:priority` labeller over a `:flex` segmenter puts the latency where it affects
the rest of the pipeline and takes the discount on the pass that does not.

### How It Works

When you specify a model ID with a service tier suffix:

1. The system parses the model ID to extract the base model and tier
2. The base model ID (without suffix) is passed to the Bedrock API
3. The extracted tier is passed as the `serviceTier` parameter
4. Example: `us.amazon.nova-2-lite-v1:0:flex` becomes:
   - Model ID: `us.amazon.nova-2-lite-v1:0`
   - Service Tier: `flex`

## Web UI Configuration

### Selecting Models with Service Tiers

1. Navigate to the Configuration page
2. In each operation section (Classification, Extraction, Assessment, Summarization):
   - Find the "Model" dropdown. Classification shows **one dropdown per model
     call**: one under *One stage*, or — when **Strategy** is *Two stage*
     (`strategy: two_stage` in the JSON and YAML views) — one under
     *Segmentation* and one under *Section classification*. Set the tier on each
     call you want it on
   - Select a model with the desired tier suffix:
     - Models ending in `:flex` use Flex tier
     - Models ending in `:priority` use Priority tier
     - Models without suffix use Standard tier
3. Changes save automatically

The model dropdown will show all available models including those with tier suffixes.

## Use Cases and Recommendations

### Priority Tier
**When to use:**
- Customer-facing document processing
- Real-time classification needs
- Interactive applications
- Time-sensitive workflows

**Example configuration:**
```yaml
classification:
  # Fast classification for real-time user uploads
  one_stage:
    llm:
      model: "us.amazon.nova-2-lite-v1:0:priority"
```

### Standard Tier (Default)
**When to use:**
- General document processing
- Balanced performance and cost
- Most production workloads
- Default choice when unsure

**Example configuration:**
```yaml
extraction:
  model: "us.amazon.nova-2-lite-v1:0"
  # No suffix = standard tier
```

### Flex Tier
**When to use:**
- Batch document processing
- Evaluation and testing workflows
- Non-urgent background jobs
- Cost optimization scenarios

**Example configuration:**
```yaml
extraction:
  model: "us.amazon.nova-2-lite-v1:0:flex"
  # Cost-effective for batch processing
```

## Mixed Tier Strategy

You can use different tiers for different operations:

```yaml
classification:
  # Fast classification for user experience
  one_stage:
    llm:
      model: "us.amazon.nova-2-lite-v1:0:priority"

extraction:
  model: "us.amazon.nova-2-lite-v1:0:flex"
  # Cost-effective extraction (can tolerate latency)
  confidence:
    model: "us.amazon.nova-2-lite-v1:0"
    # Standard tier for the confidence pass
```

Under `strategy: two_stage` the classification half becomes two decisions rather
than one — see the note in [Using Model IDs with Service Tier
Suffixes](#using-model-ids-with-service-tier-suffixes) above.

## Performance Expectations

Based on AWS documentation:

- **Priority**: Up to 25% better OTPS (Output Tokens Per Second) latency vs Standard
- **Standard**: Consistent baseline performance
- **Flex**: Variable latency, suitable for batch workloads where speed is less critical

Actual performance varies by:
- Model size and complexity
- Request payload size
- Current system load
- AWS region

## Cost Implications

- **Priority**: ~25% premium over Standard pricing
- **Standard**: Baseline pricing (reference point)
- **Flex**: Discounted pricing compared to Standard

**Important:** Always refer to the [AWS Pricing Calculator](https://calculator.aws) for current pricing information.

## Monitoring

### CloudWatch Logs

Service tier information appears in CloudWatch logs:

```
Using service tier: flex
Extracted service tier 'flex' from model ID. Using base model ID: us.amazon.nova-2-lite-v1:0
```

### Metrics

⚠️ **CloudWatch metrics do not carry the service tier.** The Bedrock client publishes
its metrics (`BedrockRequestsTotal`, `InputTokens`, `BedrockRequestLatency` and the
rest) with a name, a value and a unit only, so every one of them lands with an **empty
dimension list**. There is no tier dimension to group or filter a CloudWatch graph by,
and adding one would be a code change in `idp_common.bedrock.client`.

**Use the reporting database instead — it does separate the tiers.** The tier suffix is
deliberately kept in the metering key (`metering_model_id` collapses `:1m`, which names
no separate price, but retains `:flex` and `:priority`, which do). That key becomes the
`service_api` column of the metering table, so a tiered call is recorded against
`bedrock/us.amazon.nova-2-lite-v1:0:flex` while a standard one is recorded against
`bedrock/us.amazon.nova-2-lite-v1:0` — distinct rows, each priced from its own
`config_library/pricing.yaml` entry. From there you can:

- Track token usage by tier, by grouping on `service_api`
- Attribute cost by tier, since each tier resolves its own rate card
- Compare tiers over the same workload, by querying two `service_api` values

Latency per tier is not in that table. The per-request figure is in the logs (and in
`BedrockRequestLatency`, which is published without a tier dimension), so comparing tier
latency means either reading the logs or running the two tiers as separate measured
batches.

## Troubleshooting

### Model ID Not Recognized

**Problem:** Model with tier suffix not working

**Solution:** Verify the model ID format:
- Correct: `us.amazon.nova-2-lite-v1:0:flex`
- Incorrect: `us.amazon.nova-2-lite-v1:0-flex`
- Incorrect: `us.amazon.nova-2-lite-v1:0_flex`

### Service Tier Not Applied

**Problem:** Service tier not being used

**Solution:** Check CloudWatch logs for:
- "Extracted service tier" messages
- "Using service tier" messages
- Verify model ID includes tier suffix

### Invalid Tier Suffix

**Problem:** Using unsupported tier suffix

**Solution:** Only `flex` and `priority` suffixes are supported. Standard tier is the default (no suffix needed).

## Best Practices

1. **Start with Standard**: Use standard tier (no suffix) as your baseline
2. **Optimize Selectively**: Add tier suffixes only where needed
3. **Monitor Costs**: Track spending by tier using CloudWatch metrics
4. **Test Performance**: Measure actual latency differences for your workload
5. **Use Flex for Batch**: Leverage Flex tier for non-urgent batch processing
6. **Reserve Priority**: Use Priority tier only for latency-sensitive operations

## Model Compatibility

Not all Bedrock models support all service tiers. Priority and Flex tiers are supported by:
- Amazon Nova models
- OpenAI models
- Qwen models
- DeepSeek models

If a model doesn't support the specified tier, the Bedrock API will return an error.

## Additional Resources

- [AWS Bedrock Service Tiers Documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/service-tiers-inference.html)
- [AWS Pricing Calculator](https://calculator.aws)
- [GenAI IDP Configuration Guide](./configuration.md)
