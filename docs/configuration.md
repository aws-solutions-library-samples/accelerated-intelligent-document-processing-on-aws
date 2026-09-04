---
title: "Configuration and Customization"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Configuration and Customization

The GenAIIDP solution provides multiple configuration approaches to customize document processing behavior to suit your specific needs.

> **📝 Note:** Starting with version 0.3.21, document class definitions use **JSON Schema** format instead of the legacy custom format. See [json-schema-migration.md](json-schema-migration.md) for migration details and format comparison. Legacy configurations are automatically migrated on first use.

## Pattern Configuration via Web UI

The web interface allows real-time configuration updates without stack redeployment:

- **Document Classes**: Define and modify document categories and their descriptions (using JSON Schema format). Choose from **35+ pre-built standard classes** (Invoice, Receipt, W-2, Bank Statement, etc.) or create custom classes from scratch.
- **Extraction Attributes**: Configure fields to extract for each document class (defined as JSON Schema properties)
- **Few Shot Examples**: Upload and configure example documents to improve accuracy (supported in Pattern 2)
- **Model Selection**: Choose between available Bedrock models for classification and extraction
  > **💡 Cost Attribution Tip:** You can replace standard model IDs with [Bedrock Application Inference Profile](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-create.html) ARNs to enable cost-allocation tagging (e.g., for MAP migration tracking). This is a configuration-only change — no code modifications required. See [Cost Attribution with Application Inference Profiles](./cost-calculator.md#cost-attribution-with-bedrock-application-inference-profiles) for step-by-step instructions.
  > **🤖 OpenAI GPT-5.x:** `openai.gpt-5.4`, `openai.gpt-5.5`, and the GPT-5.6 family (`openai.gpt-5.6-sol` / `-terra` / `-luna`) are selectable for OCR, classification, extraction, assessment, summarization, evaluation, and chat (US regions only), and are tuned via a `reasoning_effort` field instead of temperature/top_p. They are **not** supported for agentic extraction or Discovery. See [OpenAI GPT-5.x Models](./openai-models.md) for the full support matrix and caveats.
  > **🤖 xAI Grok:** `us.xai.grok-4.6` (US regions) and `global.xai.grok-4.6` (EU/APAC too, and cheaper) are selectable for OCR, classification, extraction — **including agentic extraction** — assessment, summarization, evaluation, chat, and the rule-validation/agent paths. Grok runs on the standard Converse API, is tuned via `reasoning_effort` (`none`/`low`/`medium`/`high`/`xhigh`) instead of temperature/top_p, and is **not** supported for Discovery or Policy Discovery. Flex/Priority tiers and prompt caching do **not** work despite being advertised. See [xAI Grok Models](./grok-models.md) for the full support matrix and caveats.
- **Prompt Engineering**: Customize system and task prompts for optimal results
- **OCR Features**: Configure Textract features (TABLES, FORMS, SIGNATURES, LAYOUT) for enhanced data capture
- **Evaluation Methods**: Set evaluation methods and thresholds for each attribute
- **Summarization**: Configure model, prompts, parameters, and enable/disable document summarization via the `enabled` property

### Configuration Versions

The solution supports **multiple named configuration versions**, enabling you to maintain independent configuration snapshots for A/B testing, environment separation, and iterative prompt tuning — all without redeploying the stack. Each version stores a complete, self-contained configuration. The active version determines which configuration is used for new document processing.

Key capabilities:
- **Create, edit, and delete** configuration versions with unique names and descriptions
- **Activate** any version to make it the default for new processing
- **Compare** versions side-by-side to see differences (exportable as CSV/JSON)
- **Track** which version was used for each processed document and test run
- **Select** a specific version when uploading documents, running tests, or using the CLI

#### Managed Configuration Versions

The stack automatically deploys **managed configuration versions** for each pre-deployed test set (`fake-w2`, `docsplit`, `ocr-benchmark`, `realkie-fcc-verified`). These are marked with `managed: true` and have the following behavior:

- **Overwritten on stack updates** — always reflect the latest defaults shipped with the solution
- **Save disabled** — the Save button is disabled and an info banner explains the config is stack-managed
- **Delete disabled** — managed versions cannot be deleted in the UI or via the API
- **Editable copies** — use **Create profile** in the Configuration Profiles table to create a custom, editable copy
- **Not importable** — managed configs are stored separately (`config_library/managed_config/`) and do not appear in the configuration import browser
- **Test Studio integration** — when a test set is selected, the matching managed config version is auto-selected

For comprehensive documentation, see [configuration-profiles.md](configuration-profiles.md).

### Configuration Management Features

- **Save Changes**: Save your current configuration changes. The button is **enabled only when you have unsaved changes** (comparing your edits against the last saved configuration); when it's disabled on a stack-managed version, hovering it explains why. After a successful save, a confirmation banner **and** a brief success toast (top-right notification area) are shown. Less-frequent actions (Export, Save as default, Restore default (All), Save current edits as new profile, and BDA sync) are grouped under an **Actions** menu next to Save changes. To create a new profile from an existing one's *saved* state, use **Create profile** in the Configuration Profiles table instead.
- **Unsaved Changes Indicator**: Individual fields with unsaved edits display an orange dot next to the field label, and an info banner with a "Discard changes" button appears when the configuration form has unsaved edits (shown on all versions, including the stack-managed `default`).
- **Browser Navigation Guard**: The browser warns before leaving the page when unsaved configuration changes exist (both on browser close/refresh and SPA navigation).
- **Save as Default**: Save your current version's configuration as the new default baseline. This replaces the existing default configuration. **Warning**: Default configurations may be overwritten during solution upgrades - export your configuration first for backup.
- **Restore Default (All)**: Reset the current version's configuration back to the default values, replacing all customizations.
- **Refresh**: Reload the configuration from the server. Use this to sync your view with the latest saved configuration, discard unsaved local changes, or verify your configuration after external updates.
- **Export Configuration**: Download your current configuration to local files in JSON or YAML format with customizable filenames. Use this to backup configurations before upgrades or share configurations between environments.
- **Import Configuration**: Upload configuration files from your local machine OR import from the Configuration Library:
  - **From Local File**: Upload configuration files from your computer in JSON or YAML format with automatic format detection and validation
  - **From Configuration Library**: Browse and import pre-configured document processing workflows from the solution's built-in configuration library
    - **Pattern-Filtered**: Only shows configurations compatible with your currently deployed pattern (Pattern 1, 2, or 3)
    - **Dual Format Support**: Automatically detects and imports both `config.yaml` and `config.json` formats
    - **README Preview**: View markdown-formatted documentation before importing to understand configuration purpose and features
    - **Format Indicators**: Visual badges show file format (YAML/JSON) and README availability
    - **Library Contents**: Includes sample configurations like lending-package-sample, bank-statement-sample, rvl-cdip, criteria-validation, and more
  - **Important**: Importing a configuration **replaces** your existing custom configuration entirely. Any prior customizations not included in the imported file will be reset to defaults. Export your current configuration first if you want to preserve it.

Configuration changes are validated and applied immediately, with rollback capability if issues arise. See [web-ui.md](web-ui.md) for details on using the administration interface.

### Configuration Management via CLI

The IDP CLI provides command-line tools for configuration management:

- **`idp-cli config-create`**: Generate configuration templates from system defaults
- **`idp-cli config-validate`**: Validate configuration files against schemas
- **`idp-cli config-download`**: Download configuration from deployed stacks
- **`idp-cli config-upload`**: Upload configuration to deployed stacks

See [idp-cli.md](idp-cli.md#config-create) for complete command documentation.

## Custom Configuration Path

The solution now supports specifying a custom configuration file location via the `CustomConfigPath` CloudFormation parameter. This allows you to use your own configuration files stored in S3 instead of the default configuration library.

### Usage

When deploying the stack, you can specify a custom configuration file:

```yaml
CustomConfigPath: "s3://my-bucket/custom-config/config.yaml"
```

**Key Features:**
- **Override Default Configuration**: When specified, your custom configuration completely replaces the default pattern configuration
- **S3 URI Format**: Accepts standard S3 URI format (e.g., `s3://my-bucket/custom-config/config.yaml`)
- **Least-Privilege Security**: IAM permissions are conditionally granted only to the specific S3 bucket and object you specify
- **All Patterns Supported**: Works with Pattern 1 (BDA), Pattern 2 (Textract + Bedrock), and Pattern 3 (Textract + UDOP + Bedrock)

**Security Benefits:**
- Eliminates wildcard S3 permissions (`arn:aws:s3:::*/*`)
- Conditional IAM access only when CustomConfigPath is specified
- Proper S3 URI to ARN conversion for least-privilege compliance
- Passes security scans with minimal required permissions

**Configuration File Requirements:**
- Must be valid YAML format
- Only needs to include `notes`, `classes`, and any settings that differ from system defaults (see "System Defaults and Configuration Inheritance" below)
- Follow the same structure as the configuration files in the `config_library` directory

Leave the `CustomConfigPath` parameter empty (default) to use the standard configuration library included with the solution.

## System Defaults and Configuration Inheritance

The GenAI IDP Accelerator uses a **system defaults** architecture where configurations inherit from pattern-specific default files. This means user configurations only need to specify differences from the defaults, making them simpler and more maintainable.

### How It Works

1. **System defaults** are loaded first from `lib/idp_common_pkg/idp_common/config/system_defaults/`:
   - `pattern-1.yaml` - BDA mode defaults (used when `use_bda: true`)
   - `pattern-2.yaml` - Pipeline mode defaults (used when `use_bda: false`)

2. **User configurations** are merged on top, overriding only the specified values

3. **Result**: A complete configuration with user customizations applied to system defaults

### Minimal Configuration Example

A user configuration only needs:

```yaml
notes: "My document processing configuration"

classes:
  - $schema: https://json-schema.org/draft/2020-12/schema
    $id: Invoice
    type: object
    x-aws-idp-document-type: Invoice
    description: "A billing document"
    properties:
      invoice_number:
        type: string
        description: "Unique invoice identifier"
```

All other settings (OCR, classification, extraction, assessment, evaluation, summarization, discovery, agents) are inherited from the pattern's system defaults.

### The system defaults are the canonical list of every setting

This page is organized by topic and covers the settings people ask about most — it is
**not** an exhaustive key reference, and a newly added option may not be described here
yet. The authoritative enumeration is the `system_defaults/` directory itself:

| file | covers |
|---|---|
| `base.yaml` | top-level keys shared by every pattern |
| `base-ocr.yaml` | OCR backends and image handling |
| `base-classification.yaml` | classification, `sectionSplitting`, `contextPagesCount` |
| `base-extraction.yaml` | extraction, `agentic.*`, `forced_tool.*`, `multi_instance_detection.*` |
| `base-confidence.yaml` | assessment / confidence |
| `base-evaluation.yaml`, `base-summarization.yaml`, `base-discovery.yaml`, `base-agents.yaml`, `base-chat.yaml`, `base-geometry.yaml`, `base-rule-validation.yaml`, `base-rule-discovery.yaml`, `base-classes.yaml`, `base-notes.yaml` | the remaining stages |
| `pattern-1.yaml` / `pattern-2.yaml` | which of the above compose for BDA vs Pipeline mode |

Every key in those files carries its shipped default and an inline comment explaining
what it does, when to change it, and — where it has been measured — the evidence. Reading
them is the reliable way to find out what is tunable; they are also what the **Web UI
shows** for `Config#default`, so what you read there is what the UI presents.

For **measured** guidance on which settings are worth changing — including the ones added
most recently — see the [Configuration Guidance paper](./benchmarking/config-guidance.md).
Its §7 covers the current release's options with the A/B numbers behind each
recommendation, and states plainly where a setting was measured to buy nothing.

### Override Example

To override specific settings while keeping others at defaults:

```yaml
notes: "Configuration with custom classification method"

# Override just the classification method
classification:
  classificationMethod: textbasedHolisticClassification

# Override confidence to use integrated mode
extraction:
  confidence:
    mode: integrated

classes:
  # ... your document classes
```

### Benefits

- **Simpler configs** - Only specify what makes your use case unique
- **Maintainable** - System default updates automatically apply to all configs
- **Focused** - Easy to see what customizations are active
- **Version-safe** - Defaults evolve with the solution while custom overrides remain stable

### Configuration Library

The `config_library/` directory contains example configurations demonstrating this inheritance pattern. Each config contains:
- `notes:` - Description of the configuration
- `classes:` - Document class definitions (JSON Schema format)
- **Overrides** - Only settings that differ from system defaults

See the [config_library README](../config_library/README.md) for available configurations and usage examples.

### Retired and legacy Bedrock models

Bedrock retires model versions over time. A retired model is removed from the
model picklists (the enums in `patterns/unified/template.yaml` and
`template.yaml`) and from `config_library/pricing.yaml`. Model IDs in a
configuration are plain strings, not a closed enum, so **a stored configuration
that still names a retired model keeps loading** — it just fails at invoke time
with:

```
ResourceNotFoundException: This model version has reached the end of its life.
```

The failing stage's model is named in the Lambda log line and by the Error
Analyzer agent's `fetch_pipeline_configuration` tool. Repoint the stage at a
current model to fix it.

Two failure shapes to distinguish:

- **End of life** — the model is gone for everyone. It is removed from the
  picklists. `us.anthropic.claude-3-5-haiku-20241022-v1:0` is the most recent
  example.
- **Provider-legacy, account-scoped** — the model still exists but access is
  withdrawn per account after inactivity:
  `ResourceNotFoundException: Access denied. This Model is marked by provider as
  Legacy and you have not been actively using the model in the last 30 days.`
  `us.amazon.nova-premier-v1:0` is currently in this state for some accounts. It
  remains selectable because it works for accounts that have used it recently —
  if you hit this error, either pick a current model or request access again in
  the Bedrock console.

## Summarization Configuration

### Enable/Disable Summarization

Summarization can be controlled via the configuration file rather than CloudFormation stack parameters. This provides more flexibility and eliminates the need for stack redeployment when changing summarization behavior.

**Configuration-based Control (Recommended):**
```yaml
summarization:
  enabled: true  # Set to false to disable summarization
  model: us.anthropic.claude-3-7-sonnet-20250219-v1:0
  temperature: 0.0
  # ... other summarization settings
```

**Key Benefits:**
- **Runtime Control**: Enable/disable without stack redeployment
- **Cost Optimization**: Zero LLM costs when disabled (`enabled: false`)
- **Simplified Architecture**: No conditional logic in state machines
- **Backward Compatible**: Defaults to `enabled: true` when property is missing

**Behavior When Disabled:**
- Summarization lambda is still called (minimal overhead)
- Service immediately returns with logging: "Summarization is disabled in configuration"
- No LLM API calls or S3 operations are performed
- Document processing continues to completion

**Note:** Prior to v0.4.0, this feature was controlled by the `IsSummarizationEnabled` CloudFormation parameter. The configuration-based approach provides runtime control without requiring stack redeployment.

## Confidence (Assessment) Configuration

As of **config v0.6**, per-field **confidence** and **geometry** are **outputs of
extraction**, configured under `extraction.confidence.*` and
`extraction.geometry.*` — there is no top-level `assessment.{model, geometry_mode,
...}` block anymore. Human-in-the-loop review is configured under the top-level
`hitl.*` block. See [Extraction & Confidence](./extraction-and-confidence.md) for
the full reference.

### Enable/Disable Confidence

Confidence is controlled via the configuration file rather than CloudFormation
stack parameters. This provides runtime control without stack redeployment.

**Configuration-based Control (Recommended):**
```yaml
extraction:
  confidence:
    enabled: true             # false disables confidence entirely (zero LLM cost)
    mode: separate            # off | separate (default) | integrated
    model: us.amazon.nova-lite-v1:0
    temperature: 0.0
    list_batch_size: 25       # rows per assessment batch for large lists
    # ... other confidence settings
```

**Confidence modes** (`extraction.confidence.mode`):
- **`separate`** *(default)* — on the Simple path, confidence runs as the standalone Assessment step; on the Advanced (agentic) path it runs inside each extraction shard and the standalone step auto-skips.
- **`integrated`** — a single extraction inference returns values **and** inline confidence together (works on **both** the simple and agentic paths); the standalone Assessment step auto-skips.
- **`off`** — no confidence scoring (equivalent to `enabled: false`); zero LLM cost.

**Behavior When Disabled** (`enabled: false` or `mode: off`):
- Assessment lambda is still called (minimal overhead)
- Service immediately returns with logging: "Assessment is disabled via configuration"
- No LLM API calls or S3 operations are performed
- Document processing continues to completion

**Note:** Prior to v0.4.0, this feature was controlled by the `IsAssessmentEnabled` CloudFormation parameter. The configuration-based approach provides runtime control without requiring stack redeployment.

### Large lists (`list_batch_size`)

For complex documents with large lists (bank statements with hundreds of
transactions, line-item tables), the standalone Assessment step **batches large
lists automatically**: it slices the largest list field into
`extraction.confidence.list_batch_size` chunks (default **25**), scores each chunk
sequentially, then reconciles so every list cell gets its own confidence and
bounding box. A bounded missing-row retry re-scores any dropped rows so coverage
reaches 100%. Lower `list_batch_size` if a chunk under-enumerates; raise it to cut
inference count.

```yaml
extraction:
  confidence:
    enabled: true
    mode: separate
    list_batch_size: 25       # rows per assessment batch for large lists
```

> **Granular assessment is retired.** The former "granular assessment" service
> (parallel thread-pool fan-out with DynamoDB caching, formerly `assessment.granular`
> / `extraction.confidence.granular` with `max_workers` / `simple_batch_size` / etc.)
> has been **retired and deleted**. Large-list batching is its full replacement and
> `list_batch_size` is the one knob. Any leftover `granular.*` keys still validate
> but are ignored — no config edit required.

**For large documents**, prefer **Advanced (agentic) extraction** — it shards both
extraction and confidence assessment and yields the best-calibrated confidence.

### Classification confidence (`classification.confidence.*`)

Separate from the extraction confidence above, and **on by default** — so an
existing deployment that never set it starts paying for it on upgrade.

```yaml
classification:
  confidence:
    mode: topk          # topk (default) | off
    top_k_candidates: 3 # alternative classes returned per page
```

Each page's classification returns its top-K candidate classes with
probabilities, giving a per-page confidence you can threshold on and a visible
reason when a page is ambiguous.

⚠️ **It costs output tokens on every page.** Unlike extraction confidence, which
is per *section*, this scales with page count. Measured at roughly **+17% of the
classification step**, which is a small share of a typical bill because
classification is cheap relative to extraction — but on a page-heavy corpus it is
not free. Set `mode: off` to return to the previous behaviour.

See [Classification](./classification.md) for the full reference, including how the
score reaches the UI and what a `None` confidence means.

### v0.5 → v0.6 config migration

- **Confidence and geometry moved under `extraction.*`** in v0.6: the former top-level `assessment.*` confidence settings are now `extraction.confidence.*`, and `assessment.geometry_mode` / `assessment.ground_geometry_in_ocr` are now `extraction.geometry.mode`. HITL moved to the top-level `hitl.*` block.
- **Migrate-on-read handles old configs automatically** — pre-v0.6 configurations are migrated transparently when loaded; **no manual edit is required**.
- **Granular assessment is retired** and its config keys are a **no-op** (they validate but are ignored).
- **`list_batch_size`** is the knob for large lists; for large documents, Advanced (agentic) extraction is recommended.

See [Granular Assessment Retirement](./migration-granular-retirement.md) for details.

For detailed information, see [Extraction & Confidence](extraction-and-confidence.md).

## Stack Parameters

Key parameters that can be configured during CloudFormation deployment:

### General Parameters
- `AdminEmail`: Administrator email for web UI access
- `AllowedSignUpEmailDomain`: Optional domain(s) allowed for web UI user signup
- `MaxConcurrentWorkflows`: Control concurrent document processing (default: 100)
- `DataRetentionInDays`: Set retention period for documents and tracking records (default: 365 days)
- `ErrorThreshold`: Number of workflow errors that trigger alerts (default: 1)
- `ExecutionTimeThresholdMs`: Maximum acceptable execution time before alerting (default: 300000 ms)
- `QueueStalledAgeThresholdSeconds`: How long the oldest queued document may wait *with no queue progress at all* before `DocumentQueueStalledAlarm` fires (default: 1800 s). Not a backlog limit — see [Monitoring](./monitoring.md#documentqueuestalledalarm--why-it-is-not-a-queue-depth-alarm)
- `LogLevel`: Set logging level (DEBUG, INFO, WARN, ERROR). At `INFO` or `DEBUG`, access logging is also enabled on the web UI's REST API stage (request metadata only — no request/response bodies), capturing requests that fail before reaching a Lambda (e.g. authorizer 401/403s, WAF blocks)
- `WAFAllowedIPv4Ranges`: IP restrictions for web UI access (default: allow all)
- `CloudFrontPriceClass`: Set CloudFront price class for UI distribution (CloudFront hosting only)
- `CloudFrontAllowedGeos`: Optional geographic restrictions for UI access (CloudFront hosting only)
- `WebUIHosting`: Select hosting mode — `CloudFront` (default) or `APIGateway` for VPC-based hosting (see [API Gateway Hosting](./apigateway-hosting.md))
- `CustomConfigPath`: Optional S3 URI to a custom configuration file that overrides pattern presets. Leave blank to use selected pattern configuration. Example: s3://my-bucket/custom-config/config.yaml

### Integration and Tracing Parameters
- `EnableXRayTracing`: Enable X-Ray tracing for Lambda functions and Step Functions (default: true). Provides distributed tracing capabilities for debugging and performance analysis.
- `EnableMCP`: Enable Model Context Protocol (MCP) integration for external application access via AWS Bedrock AgentCore Gateway (default: true). See [mcp-server.md](mcp-server.md) for details.
- `EnableECRImageScanning`: Enable automatic vulnerability scanning for Lambda container images in ECR for Patterns 1-3 (default: false). Recommended for production deployments but may impact deployment reliability. See [troubleshooting.md](troubleshooting.md) for guidance.

### Pattern Selection
- `IDPPattern`: Select processing pattern:
  - Unified: Supports both BDA and Pipeline processing modes via `use_bda` flag

### Pattern-Specific Parameters
- **Configuration Preset**: `ConfigurationPreset` — Select from available presets (lending-package-sample, bank-statement-sample, etc.)
- **Custom Model ARNs**: Optional custom fine-tuned classification/extraction model ARNs

> **Note**: The processing mode (BDA vs Pipeline) is controlled by the `use_bda` flag in the configuration, not by deployment parameters. See the [architecture docs](./architecture.md) for details.

- **Pattern 3 (Textract + UDOP + Bedrock)**

### Optional Features
- `EvaluationBaselineBucketName`: Optional existing bucket for ground truth data
- `DocumentKnowledgeBase`: Enable document knowledge base functionality
- `KnowledgeBaseModelId`: Bedrock model for knowledge base queries
- `PostProcessingLambdaHookFunctionArn`: Optional Lambda ARN for custom post-processing (see [post-processing-lambda-hook.md](post-processing-lambda-hook.md) for detailed implementation guidance)
- `BedrockGuardrailId`: Optional Bedrock Guardrail ID to apply
- `BedrockGuardrailVersion`: Version of Bedrock Guardrail to use

For details on processing modes, see [architecture.md](architecture.md). For legacy pattern-specific references, see [pattern-1.md](pattern-1.md) (BDA) and [pattern-2.md](pattern-2.md) (Pipeline).

## High Volume Processing

### Request Service Quota Limits

For high-volume document processing, consider requesting increases for these service quotas:

- **Lambda Concurrent Executions**: Default 1,000 per region
- **Step Functions Executions**: Default 25,000 per second (Standard workflow)
- **Bedrock Model Invocations**: Varies by model and region
  - Claude models: Typically 5-20 requests per minute by default
  - Titan models: 15-30 requests per minute by default
- **SQS Message Rate**: Default 300 per second for FIFO queues
- **TextractLimitPage API**: 15 transactions per second by default
- **DynamoDB Read/Write Capacity**: Uses on-demand capacity by default

Use the AWS Service Quotas console to request increases before deploying for production workloads. See [monitoring.md](monitoring.md) for details on monitoring your resource usage and quotas.

### Cost Estimation

The solution provides built-in cost estimation capabilities:

- Real-time cost tracking for Bedrock model usage
- Per-document processing cost breakdown
- Historical cost analysis and trends
- Budget alerts and threshold monitoring

See [COST_CALCULATOR.md](../COST_CALCULATOR.md) for detailed cost analysis across different processing volumes.

## Bedrock Guardrail Integration

The solution supports Amazon Bedrock Guardrails for content safety and compliance across all patterns:

### How Guardrails Work

Guardrails provide:
- **Content Filtering**: Block harmful, inappropriate, or sensitive content
- **Topic Restrictions**: Prevent processing of specific topic areas
- **Data Protection**: Redact or block personally identifiable information (PII)
- **Automated Reasoning Checks**: Enable formal verification of model outputs against defined policies using [Automated Reasoning](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-automated-reasoning.html), ensuring factual consistency and logical correctness
- **Custom Filters**: Define organization-specific content policies

### Configuring Guardrails

Guardrails are configured with two CloudFormation parameters:
- `BedrockGuardrailId`: The ID (not name) of an existing Bedrock Guardrail
- `BedrockGuardrailVersion`: The version of the guardrail to use (e.g., "DRAFT" or "1")

This applies guardrails to all Bedrock model interactions, including:
- Document extraction (all patterns)
- Document summarization (all patterns) 
- Document classification (Pattern 2 only)
- Knowledge base queries (if enabled)

### Best Practices

1. **Test Thoroughly**: Validate guardrail behavior with representative documents
2. **Monitor Impact**: Track processing latency and accuracy changes
3. **Regular Updates**: Review and update guardrail policies as requirements evolve
4. **Compliance Alignment**: Ensure guardrails align with organizational compliance requirements

For more information on creating and managing Guardrails, see the [Amazon Bedrock documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails.html).

## Concurrency and Throttling Management

The solution implements sophisticated concurrency control and throttling management:

### Throttling and Retry (Bedrock, Textract, SageMaker)

- **Exponential Backoff**: Automatic retry with increasing delays
- **Jitter Addition**: Random delay variation to prevent thundering herd
- **Circuit Breaker**: Temporary halt on repeated failures
- **Rate Limiting**: Configurable request rate controls

The solution tracks metrics for throttling events and successful retries, viewable in the CloudWatch dashboard.

### Step Functions Retry Configuration

The Step Functions state machine includes comprehensive retry policies for API failures:

```json
{
  "Retry": [
    {
      "ErrorEquals": ["Lambda.ServiceException", "Lambda.AWSLambdaException"],
      "IntervalSeconds": 2,
      "MaxAttempts": 6,
      "BackoffRate": 2
    },
    {
      "ErrorEquals": ["States.TaskFailed"],
      "IntervalSeconds": 1,
      "MaxAttempts": 3,
      "BackoffRate": 2
    }
  ]
}
```

### Concurrency Control

- **Workflow Limits**: Maximum concurrent Step Function executions, controlled by `MaxConcurrentWorkflows` parameter
- **Lambda Concurrency**: Per-function concurrent execution limits
- **Queue Management**: SQS visibility timeout (30 seconds) and message batching
- **Dynamic Scaling**: Automatic adjustment based on queue depth and in-flight workflows

## Document Status Tracking

The solution provides multiple ways to track document processing status:

### Using the Web UI

The web UI dashboard provides a real-time view of document processing status, including:
- Document status (queued, processing, completed, failed)
- Processing time
- Classification results
- Extraction results
- Error details (if applicable)

See [web-ui.md](web-ui.md) for details on using the dashboard.

### Using the Lookup Script

Use the included script to check document processing status via CLI:

```bash
bash scripts/lookup_file_status.sh <DOCUMENT_KEY> <STACK_NAME>
```

### Response Format

Status lookup returns comprehensive information:

```json
{
  "document_key": "example.pdf",
  "status": "COMPLETED",
  "workflow_arn": "arn:aws:states:...",
  "start_time": "2024-01-01T12:00:00Z",
  "end_time": "2024-01-01T12:05:30Z",
  "processing_time_seconds": 330,
  "pages_processed": 15,
  "document_class": "BankStatement",
  "attributes_found": 12,
  "output_location": "s3://output-bucket/results/example.json",
  "error_details": null
}
```

## Evaluation Extensions in JSON Schema

Document class schemas support evaluation-specific extensions for fine-grained control over accuracy assessment. These extensions work with the [Stickler](https://github.com/awslabs/stickler)-based evaluation framework to provide flexible, business-aligned evaluation capabilities.

### Available Extensions

- `x-aws-idp-evaluation-method`: Comparison method (EXACT, FUZZY, LEVENSHTEIN, NUMERIC_EXACT, SEMANTIC, DATE, LLM, HUNGARIAN)
- `x-aws-idp-evaluation-method-config`: Optional comparator config (used by DATE: `dayfirst`, `tolerance`, `range_mode`)
- `x-aws-idp-evaluation-threshold`: Minimum score to consider a match (0.0-1.0)
- `x-aws-idp-evaluation-weight`: Field importance for weighted scoring (default: 1.0, higher values = more important)

### Example Configuration

```yaml
classes:
  - $schema: "https://json-schema.org/draft/2020-12/schema"
    x-aws-idp-document-type: "Invoice"
    x-aws-idp-evaluation-match-threshold: 0.8  # Document-level threshold
    properties:
      invoice_number:
        type: string
        x-aws-idp-evaluation-method: EXACT
        x-aws-idp-evaluation-weight: 2.0  # Critical field - double weight
      invoice_date:
        type: string
        x-aws-idp-evaluation-method: FUZZY
        x-aws-idp-evaluation-threshold: 0.9
        x-aws-idp-evaluation-weight: 1.5  # Important field
      vendor_name:
        type: string
        x-aws-idp-evaluation-method: FUZZY
        x-aws-idp-evaluation-threshold: 0.85
        x-aws-idp-evaluation-weight: 1.0  # Normal weight (default)
      vendor_notes:
        type: string
        x-aws-idp-evaluation-method: SEMANTIC
        x-aws-idp-evaluation-threshold: 0.7
        x-aws-idp-evaluation-weight: 0.5  # Less critical - half weight
```

### Stickler Backend Integration

The evaluation framework uses [Stickler](https://github.com/awslabs/stickler) as its evaluation engine. The `SticklerConfigMapper` automatically translates these IDP extensions to Stickler's native format, providing:

- **Field-level weighting** for business-critical attributes
- **Optimal list matching** using the Hungarian algorithm
- **Extensible comparator system** with exact, fuzzy, numeric, semantic, and LLM-based comparison
- **Native JSON Schema support** with $ref resolution

### Benefits

1. **Business Alignment**: Weight critical fields higher to ensure evaluation scores reflect business priorities
2. **Flexible Comparison**: Choose the right evaluation method for each field type
3. **Tunable Thresholds**: Set field-specific thresholds for matching sensitivity
4. **Dynamic Schema Generation**: Auto-generates evaluation schema from baseline data when configuration is missing (for development/prototyping)

For detailed evaluation capabilities and best practices, see [evaluation.md](evaluation.md).

## Section Splitting Strategies

Pattern-2 and Pattern-3 support configurable strategies for how classified pages are grouped into document sections. This is controlled by the `sectionSplitting` configuration field:

### Available Strategies

- **`disabled`**: Treats the entire document as a single section with the first detected class. Simplest approach for single-document processing.
  
- **`page`**: Creates one section per page, preventing automatic joining of same-type documents. Useful for deterministic processing of documents containing multiple forms of the same type (e.g., multiple W-2s, multiple invoices in one packet).
  
- **`llm_determined`** (default): Uses LLM boundary detection with "Start"/"Continue" indicators to intelligently segment multi-document packets. Best for complex scenarios where document boundaries are not obvious.

### Configuration Example

```yaml
classification:
  sectionSplitting: page  # or "disabled", "llm_determined"
```

### Use Cases

- **Single Document Processing**: Use `disabled` for simplicity
- **Multiple Same-Type Forms**: Use `page` for deterministic splitting (resolves Issue #146)
- **Complex Multi-Document Packets**: Use `llm_determined` for intelligent boundary detection

For more details on classification methods and section splitting, see [classification.md](classification.md).

### Page Limit Configuration

Control how many pages are used during document classification to optimize performance and costs:

```yaml
classification:
  maxPagesForClassification: "ALL"  # or "1", "2", "3", etc.
```

**Behavior:**
- **"ALL"** (default): Uses all pages for classification
- **Numeric value**: Classifies only the first N pages, then applies that classification to the entire document

**Important:** When using a numeric limit, the classification result from the first N pages is applied to ALL pages, effectively forcing a single class/section for the entire document.

**Use Cases:**
- Performance optimization for large documents
- Cost reduction for documents with consistent patterns
- Simplified processing for homogeneous document types

## Prompt Preview

The Configuration page includes a **Prompt Preview** tab that lets you see the actual prompts sent to the LLM for each processing step (Classification, Extraction, Assessment, Summarization) with your configuration values filled in. This is useful for optimizing document class schemas and prompt templates — you can see exactly how your class names, descriptions, and JSON Schema attributes appear in the prompt that the LLM receives. See [web-ui.md](web-ui.md#prompt-preview) for details.

## Prompt Optimization

### Bedrock Prompt Caching

The solution supports Bedrock prompt caching to reduce costs and improve performance by caching static portions of prompts. This feature is available across all patterns for classification, extraction, assessment, and summarization.

#### How It Works

Insert a `<<CACHEPOINT>>` delimiter in your prompt to separate static (cacheable) content from dynamic content:

```yaml
extraction:
  task_prompt: |
    You are an expert document analyst. Follow these rules:
    - Extract exact values from the document
    - Preserve formatting as it appears
    
    <<CACHEPOINT>>
    
    Document to process:
    {DOCUMENT_TEXT}
```

Everything **before** the `<<CACHEPOINT>>` delimiter is cached and reused across similar requests, while content after it remains dynamic. This can significantly reduce token costs and improve response times.

#### Best Practices

1. **Place Static Content First**: Instructions, rules, schemas, and examples should come before the cachepoint
2. **Dynamic Content Last**: Document text, images, and variable data should come after the cachepoint
3. **Cache Hit Optimization**: Keep static content consistent across requests for maximum cache utilization

#### Benefits

- **Cost Savings**: Cached tokens cost significantly less than regular input tokens
- **Performance**: Reduced processing time for cached content
- **Token Efficiency**: Particularly beneficial for long system prompts or few-shot examples

For pricing details on cached tokens, see [cost-calculator.md](cost-calculator.md).

## Regex-Based Classification (Pattern-2)

Pattern-2 supports optional regex patterns in document class definitions for performance optimization and deterministic classification when patterns are known.

### Configuration

Add regex patterns to your class definitions:

```yaml
classes:
  - name: W2 Tax Form
    description: IRS Form W-2 Wage and Tax Statement
    document_name_regex: "^w2_.*\\.pdf$"  # Matches filenames starting with "w2_"
    document_page_content_regex: "Form W-2.*Wage and Tax Statement"
    
  - name: Invoice
    description: Commercial invoice
    document_name_regex: "^invoice_\\d{6}\\.pdf$"  # Matches invoice_123456.pdf
    document_page_content_regex: "^INVOICE\\s+#\\d+"
```

### Classification Logic

1. **Document Name Matching**: If `document_name_regex` matches the document filename, all pages are classified as that type without LLM processing
2. **Page Content Matching**: During multimodal page-level classification, if `document_page_content_regex` matches page text, that page is classified without LLM processing
3. **Fallback**: If no regex matches, standard LLM classification is used

### Benefits

- **Performance**: Significant speed improvements by bypassing LLM calls for known patterns
- **Cost Savings**: Reduced token consumption for documents matching regex patterns
- **Deterministic**: Consistent classification results for known document patterns
- **Backward Compatible**: Seamless fallback to LLM classification when patterns don't match

### Monitoring

The system logs INFO-level messages when regex patterns match, providing visibility into optimization effectiveness.

For examples and demonstrations, see the `step2_classification_with_regex.ipynb` notebook.

## OCR Backend Configuration (Pattern-2 and Pattern-3)

Patterns 2 and 3 support multiple OCR backend engines for flexible document processing:

### Available Backends

- **Textract** (default): AWS Textract with advanced feature support (TABLES, FORMS, SIGNATURES, LAYOUT). Cheapest for raw text (~$1.50/1K pages); TABLES +$15/1K, FORMS +$50/1K.
- **BDA**: Amazon Bedrock Data Automation "standard output" used as a pure OCR engine — reading-order markdown with **tables and layout** plus word-level confidence/bounding boxes in one call, flat **$10/1K pages**. Auto-enables the agentic extraction table tool. Best for table-heavy documents and predictable pricing without composing Textract features. (Distinct from the whole-pipeline BDA mode `use_bda`, which also does classification/extraction.)
- **Bedrock**: LLM-based OCR using Claude/Nova models with customizable prompts for better handling of complex documents
- **None**: Image-only processing without OCR (useful for pure visual analysis)

### Configuration Example

```yaml
ocr:
  backend: textract  # or "bda", "bedrock", "none"

  # Textract features. DEFAULT: TABLES + LAYOUT + SIGNATURES (see trade-off below).
  features:
    - name: TABLES
    - name: LAYOUT
    - name: SIGNATURES

  # For BDA backend (optional): use a specific standard-output SYNC project
  # instead of the per-stack <stackname>_OCR_StdOutput project the stack
  # provisions (delivered via the BDA_OCR_PROJECT_ARN env var).
  #
  # NOTE: backend "bda" is COMMERCIAL-PARTITION ONLY. GovCloud and China offer
  # Bedrock Data Automation but reject the SYNC document-modality project this
  # backend needs, so the stack does not create it there and the ARN is empty.
  # See docs/govcloud-deployment.md#bedrock-data-automation-as-the-ocr-backend.
  bda_project_arn: null

  # For Bedrock backend:
  bedrock_model: us.anthropic.claude-3-5-sonnet-20241022-v2:0
  system_prompt: "You are an OCR expert..."
  task_prompt: "Extract all text from this document..."
```

### When to choose BDA vs Textract for OCR

- **BDA** — you want table-aware OCR (bank/brokerage statements, invoices) with a
  single flat per-page price and no feature tuning. One call returns markdown
  tables, layout, word confidence, and bounding boxes; per-page processing scales
  past BDA's ~10-page synchronous limit automatically.
- **Textract** — you need only raw text (much cheaper: ~$0.0015/page), or you
  already tune specific Textract features. For table-heavy docs, `TABLES` on
  Textract (~$0.015/page, with `LAYOUT` and `SIGNATURES` free alongside it) costs
  somewhat more per page than BDA ($0.01/page) up to 1M pages/month and the same
  above it (Textract's `TABLES` tier drops to $0.010); benchmark accuracy on your
  corpus rather than choosing on price alone.

### Textract features & the TABLES cost/accuracy trade-off

`ocr.features` selects which Amazon Textract analysis features run. The default is
**`TABLES` + `LAYOUT` + `SIGNATURES`** — of which only `TABLES` is billed (see
below).

- **`TABLES` is on by default because tables are common and the accuracy gain is
  large.** It makes Textract emit structured Table/Cell blocks (with per-cell text,
  confidence, and geometry) that linearize into clean Markdown pipe-tables for the
  agentic table parser — yielding more complete extraction *and* more accurate
  confidence/geometry on table-heavy documents. In validation on a 24-page
  brokerage statement, `TABLES` extracted **all 1,440 rows (every page)** while
  `LAYOUT`-only silently dropped ~5 pages (~300 rows) where the plain-text
  linearization mis-segmented the table.
- **Cost trade-off** (`TABLES` ≈ **$0.015/page**, with `LAYOUT` and `SIGNATURES`
  free alongside it, vs `LAYOUT`-only ≈ **$0.004/page** — ~3.75× on the Textract
  line item):
  - **Documents *with* tables:** the extra OCR cost is typically *more*
    cost-effective and scalable end-to-end — cleaner cell structure means fewer LLM
    extraction retries, fewer confidence truncations/re-batches, and less downstream
    correction than fighting a mis-linearized plain-text table.
  - **Documents *without* tables:** `TABLES` adds cost with no benefit. If your
    corpus is table-free (forms, prose, single-value docs), **remove the `TABLES`
    entry** to fall back to cheaper `LAYOUT`-only OCR.

Set `ocr.features` per configuration to match the documents each stack processes.

### The `SIGNATURES` feature

`SIGNATURES` is **on by default**, because signature presence is a common
extraction target (loan packages, tax forms, claims, consents) and it adds **no
Textract charge** in the default combination. The
[Textract pricing page](https://aws.amazon.com/textract/pricing/) states it
directly: *"Signatures feature is included free of cost with any combination of
Forms, Tables, Queries, and Layout"* — AWS emits no usage type at all for a feature
that is free in combination. Used **alone**, without any of those features,
`SIGNATURES` is billed at ~$0.0035/page.

With `SIGNATURES` enabled, Textract reports each region it believes contains a
signature, as a **detection confidence plus a bounding box** — not text. Those
detections reach the rest of the pipeline in three places:

- **Page text** (used by the extraction prompt and the Web UI markdown view) —
  the linearizer inserts an inline `[SIGNATURE]` token per detection, and an
  `OCR signature detections` block is appended listing each one's normalized
  position and confidence.
- **`textConfidence.json`** (used by the confidence/assessment prompt) — the same
  block, appended after the per-line confidence table.
- **`pageData.json`** — a `signatures` array, which the Web UI page viewer lists
  under **Signature detections** with a clickable bounding box and a
  colour-coded confidence.

**Read the confidence.** A detection is not proof of a signature: Textract will
flag a stray pen mark, smudge or scanning artifact, typically at *low*
confidence (single- or low-double-digit). The appended block reports each
detection's confidence band, its page position in left/right + upper/lower terms,
— because a bare `left=0.572` is not usable evidence in practice: on the form in
[#634](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/634)
both the extraction and the confidence model read that as *"the first (left)
signature box"* when the detection was in the right-hand cell.

#### Extracting a reliable signed/unsigned boolean

Measured on that form — an unsigned Form 4549 whose left signature cell holds only
a faint smudge, with a date filled in beside it (Claude Sonnet 4.5, temperature 0,
page images attached, repeated runs). Prose alone does not do it: descriptions
saying "smudges are not signatures" still returned `true`, as did few-shot examples
alone, and as did the detections block alone.

**What worked, 9/9 and then confirmed end-to-end on a live stack, is a description
that turns the detection confidence into an explicit decision rule.** Give the
boolean field a description along these lines:

> Does a handwritten signature (a person's name or initials) exist in the LEFT
> "Signature of taxpayer" box near the bottom of page 2? Decide with this rule, in
> order: (1) Look at the `--- OCR signature detections ---` block in the document
> text. It lists EVERY region the OCR engine flagged as a possible signature, each
> with a detection confidence from 0-100. (2) Answer false unless that block
> contains a detected region with confidence of 50 or higher that falls in the LEFT
> signature box (left half of the page). A confidence below 50 means a faint or
> ambiguous mark — a stray pen mark, smudge, speck or scanning artifact — and MUST
> be treated as NOT a signature. (3) If the block lists no region for that box,
> answer false. (4) A date in the adjacent Date column is NOT evidence of a
> signature; handwriting elsewhere is NOT evidence. (5) Only answer true when a
> qualifying detection (confidence >= 50) is present AND you can see a handwritten
> name or initials there. (6) IGNORE any inline `[SIGNATURE]` marker in the text:
> it is placed by reading order, so its position next to a field is NOT evidence
> that that field is signed — use ONLY the detections block. (7) A faint mark you
> can see does NOT override rules 2-3. When in doubt, answer false.

Rules 6 and 7 are the ones that made it deterministic. Without 6 the model latches
onto the inline token's accidental adjacency; without 7 it overrides the OCR
evidence with its own read of the smudge.

Two supporting measures, if you have them:

- **Keep `SIGNATURES` enabled** so the block exists at all — the rule above depends
  on it.
- **A few-shot example of the negative case** (the unsigned document *with* a date
  present) also works, and needs `{FEW_SHOT_EXAMPLES}` in the extraction
  `task_prompt` — present in the shipped prompts; a custom prompt must add it. See
  [few-shot-examples.md](few-shot-examples.md). Note that with examples but *no*
  detections block the false positive did not disappear, it **moved to the other
  taxpayer's field**: the model could tell a mark existed but not which cell owned
  it.

**If your corpus has no signature fields, remove the `SIGNATURES` entry.** Pages
with a detection (including false positives on stray ink) add a few prompt tokens
and an extra signal the model may over-read, for no benefit when nothing asks about
signatures. Removing it costs nothing, since the feature was free to begin with.

### Bedrock OCR Benefits

- Better handling of complex layouts and tables
- Customizable extraction logic through prompts
- Layout preservation capabilities
- Support for documents with challenging formatting

For more details on OCR configuration and feature selection, see the pattern-specific documentation.

## Custom Prompt Lambda (Pattern-2 and Pattern-3)

Patterns 2 and 3 support injection of custom business logic into the extraction process through a Lambda function.

### Configuration

Add the Lambda ARN to your extraction configuration:

```yaml
extraction:
  custom_prompt_lambda_arn: arn:aws:lambda:us-west-2:123456789012:function:GENAIIDP-MyCustomLogic
```

### Lambda Interface

Your Lambda receives:
- All template placeholders (DOCUMENT_TEXT, DOCUMENT_CLASS, ATTRIBUTE_NAMES_AND_DESCRIPTIONS, DOCUMENT_IMAGE)
- Complete document context
- Configuration parameters

The Lambda should return modified prompt content or additional context.

### Use Cases

- Document type-specific processing rules
- Integration with external systems for customer configurations
- Conditional processing based on document content
- Regulatory compliance and industry-specific requirements

### Requirements

- Lambda function name must start with `GENAIIDP-` prefix for IAM permissions
- Function must handle JSON serialization for image URIs
- Implement comprehensive error handling (fail-fast behavior)

### Demo Resources

See `notebooks/examples/demo-lambda/` for:
- Interactive demonstration notebook (`step3_extraction_with_custom_lambda.ipynb`)
- SAM deployment template for example Lambda
- Complete documentation and examples

For more details, see [Extraction & Confidence](extraction-and-confidence.md).

### Tiered Models (Validation + Escalation)

Extraction supports a **cost-tiered** strategy: extract with a fast/cheap model, then automatically re-extract only the fields that fail schema validation with a stronger model. This is configured under `extraction.validation`:

```yaml
extraction:
  model: us.amazon.nova-pro-v1:0          # fast/cheap primary extractor
  validation:
    enabled: true                          # on by default since v0.7
    fail_action: escalate                  # default is 'warn' (free); escalate costs money
    escalation_model: us.anthropic.claude-opus-4-8   # stronger tier, used only on failure
```

> **Moved in v0.7.** This block was `extraction.agentic.validation`. It now lives at
> `extraction.validation` because Simple extraction runs the same validate-and-retry
> path, so the setting is no longer agentic-only. Stored configurations are migrated
> automatically on read — no action is required.

When validation fails, only the failing top-level fields are re-extracted with `escalation_model` (a per-class `x-aws-idp-extraction-escalation-model` override takes precedence) and merged back — typically a small fraction of documents, so the stronger model's cost is incurred only where it's needed. See [Schema validation and model escalation](extraction-and-confidence.md#schema-validation-and-model-escalation) for the full feature, including the deterministic table-parsing tool, the completeness heuristic, and sharding for large documents.

> Validation and escalation are editable in the Web UI under **Configuration → Extraction → Schema Validation & Escalation**; the Advanced-mode options (table parsing, sharding) are under **Advanced extraction settings**. Sub-options are progressively revealed as you enable each feature.

> **Deprecated:** the older `extraction.agentic.review_agent` / `review_agent_model` fields are no-ops retained only for backward compatibility — use `extraction.validation` + `escalation_model` above instead.

## Cost Tracking and Optimization

The solution includes built-in cost tracking capabilities:

- **Per-document cost metrics**: Track token usage and API calls per document
- **Real-time dashboards**: Monitor costs in the CloudWatch dashboard
- **Cost estimation**: Configuration includes pricing estimates for each component

For detailed cost analysis and optimization strategies, see [cost-calculator.md](cost-calculator.md).

## Image Processing Configuration

The solution supports configurable image dimensions across all processing services (OCR, classification, extraction, and assessment) to optimize performance and accuracy for different document types.

### New Default Behavior (Preserves Original Resolution)

**Important Change**: As of the latest version, empty strings or unspecified image dimensions now preserve the original document resolution instead of resizing to default dimensions.

```yaml
# Preserves original image resolution (recommended for high-accuracy processing)
classification:
  image:
    target_width: ""     # Empty string = no resizing
    target_height: ""    # Empty string = no resizing

extraction:
  image:
    target_width: ""     # Preserves original resolution
    target_height: ""    # Preserves original resolution

assessment:
  image:
    target_width: ""     # No resizing applied
    target_height: ""    # No resizing applied
```

### Custom Image Dimensions

You can still specify exact dimensions when needed for performance optimization:

```yaml
# Custom dimensions for specific requirements
classification:
  image:
    target_width: "1200"   # Resize to 1200 pixels wide
    target_height: "1600"  # Resize to 1600 pixels tall

# Performance-optimized dimensions
extraction:
  image:
    target_width: "800"    # Smaller for faster processing
    target_height: "1000"  # Maintains good quality
```

### Image Resizing Features

- **Aspect Ratio Preservation**: Images are resized proportionally without distortion
- **Smart Scaling**: Only downsizes images when necessary (scale factor < 1.0)
- **High-Quality Resampling**: Better visual quality after resizing
- **Original Format Preservation**: Maintains PNG, JPEG, and other formats when possible

### Configuration Benefits

- **High-Resolution Processing**: Empty strings preserve full document resolution for maximum OCR accuracy
- **Service-Specific Tuning**: Each service can use optimal image dimensions
- **Runtime Configuration**: No code changes needed to adjust image processing
- **Backward Compatibility**: Existing numeric values continue to work as before
- **Memory Optimization**: Configurable dimensions allow resource optimization

### Best Practices

1. **Use Empty Strings for High Accuracy**: For critical documents requiring maximum OCR accuracy, use empty strings to preserve original resolution
2. **Specify Dimensions for Performance**: For high-volume processing, consider smaller dimensions to improve speed
3. **Test Different Settings**: Evaluate the trade-off between accuracy and performance for your specific document types
4. **Monitor Resource Usage**: Higher resolution images consume more memory and processing time

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

## Additional Configuration Resources

The solution provides additional configuration options through:

- Configuration files in the `config_library` directory
- Pattern-specific settings in each pattern's subdirectory
- Environment variables for Lambda functions
- CloudWatch alarms and notification settings

See the [README.md](../README.md) for a high-level overview of the solution architecture and components.