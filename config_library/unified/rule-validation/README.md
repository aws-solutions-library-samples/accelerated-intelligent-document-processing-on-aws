Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Rule Validation Configuration

This directory contains the rule validation configuration for the GenAI IDP Accelerator. This configuration is specifically designed for processing healthcare prior authorization (PA) packets with automated rule validation against medical coding guidelines such as NCCI (National Correct Coding Initiative) rules.

## Processing Mode

**Default Mode**: Pipeline (use_bda: false) — Uses Amazon Textract for OCR, then Bedrock LLM for classification, extraction, and rule validation. Set use_bda: true for BDA mode.

## Validation Level

**Level**: 2 - Comprehensive Testing

- **Testing Evidence**: This configuration has been tested with respiratory surgery prior authorization packets containing multiple document sections including administrative forms, medical history, operative reports, procedure logs, and claims evidence.
- **Known Limitations**: Performance may vary with non-standard PA packet formats, heavily redacted medical documents, or documents with poor image quality that affect OCR accuracy. Rule validation accuracy depends on the completeness and clarity of clinical documentation.

## Overview

The rule validation configuration is designed to handle comprehensive healthcare prior authorization document packages typically encountered in:

- **Prior Authorization Processing**: Medical procedure approval workflows
- **Medical Coding Compliance**: NCCI rule validation and modifier verification
- **Claims Review**: Supporting documentation assessment
- **Medical Necessity Evaluation**: Clinical justification review
- **Regulatory Compliance**: Healthcare coding guideline adherence

It includes specialized settings for document classification, detailed medical information extraction, automated rule validation against coding guidelines, and comprehensive summarization using Amazon Bedrock models optimized for healthcare document processing.

## Key Components

### Document Classes

The configuration defines 5 specialized prior authorization document classes, each with comprehensive attributes for detailed medical data extraction:

- **PA-Administrative**: Administrative and demographic information including patient identification, insurance/payer details, encounter information, provider credentials, facility details, and billing summary with CPT codes, modifiers, and global periods (29 properties)
- **PA-Medical-History**: Patient medical history including demographics, current and past medical conditions, medications, allergies, surgical history, and relevant health background (23 properties)
- **PA-Operative-Report**: Surgical operative report with detailed procedure information, pre-operative diagnosis, post-operative diagnosis, procedure descriptions, findings, complications, and surgeon details (28 properties)
- **PA-Procedure-Log**: Detailed operative procedure log with timestamps, procedure steps, findings, equipment used, and clinical observations (22 properties)
- **PA-Claims-Evidence**: Claims evidence and supporting documentation including letters of medical necessity, emergency criteria, conversion documentation, and modifier justification (12 properties)

### Classification Settings

This preset defines no `classification` section of its own, so every value below
comes from the system defaults in
`lib/idp_common_pkg/idp_common/config/system_defaults/`.

- **Strategy**: Combined (`classification.strategy: combined`) — a single call
  returns both the section boundaries and the classes
- **Granularity**: page (`classification.combined.granularity`) — one classification
  per page rather than per packet
- **Model**: `us.amazon.nova-2-lite-v1:0`
- **Temperature**: 0 (deterministic outputs)
- **Top-k**: 5
- **Modality**: `modality.text` and `modality.images` are both empty, so each is
  inferred from the placeholders the call's task prompt uses
- **OCR Backend**: Amazon Textract with LAYOUT, TABLES, and SIGNATURES features

The classification component analyzes document content and structure to accurately identify each section's PA document class and establish the section boundaries within each PA packet.

### Extraction Settings

- **Model**: `us.anthropic.claude-sonnet-5` (from the system defaults — this preset
  pins no extraction model)
- **Mode**: `simple` (`extraction.mode`); agentic extraction is off
- **Temperature**: 0 (deterministic outputs)
- **Top-k**: 5

There is no `extraction.max_tokens` setting. Extraction always requests the model's
maximum output, resolved per model from `config_library/model_config_limits.yaml`.

The extraction component performs comprehensive attribute extraction tailored to each PA document type, capturing critical medical and administrative information including:
- Patient demographics and insurance details
- CPT procedure codes with modifiers
- Global period indicators (000, 010, 090)
- Provider and facility information
- Clinical diagnoses and findings
- Medical necessity justification

### Rule Validation Settings

- **Model**: `us.anthropic.claude-sonnet-4-6` for both `fact_extraction` and
  `rule_validation_orchestrator` (from the system defaults — this preset sets only
  `rule_validation.enabled`)
- **Temperature**: 0 (deterministic outputs)
- **Top-k**: 20
- **Semaphore**: 5 concurrent API calls
- **Chunking**: `max_chunk_size` 8000 with 10% overlap
- **Recommendation Options**: Configurable (default: Pass, Fail, Information Not Found)

The rule validation component evaluates extracted information against predefined medical coding rules including:
- **Global Period Rules**: Validates E&M services with procedures having 000, 010, or 090 day global periods
- **Same Day Service Rules**: Verifies same-provider/same-date requirements and modifier usage
- **Postoperative Rules**: Checks postoperative E&M service compliance
- **Bundling Rules**: Validates procedure bundling and unbundling scenarios
- **Diagnostic/Surgical Rules**: Ensures proper coding of diagnostic vs surgical procedures
- **Component Service Rules**: Validates component vs comprehensive service coding
- **Separate Procedure Rules**: Checks separate procedure designation compliance

### Rule Classes

The configuration defines 7 rule types with specific validation criteria:

1. **global_periods**: Minor surgery (000/010 days) and major surgery (090 days) global period rules
2. **same_day_service_rules**: Same provider, same date service validation with NCCI PTP edits
3. **postoperative_rules**: Postoperative E&M service compliance during global periods
4. **bundling**: Procedure bundling and modifier requirements (e.g., emergency intubation with modifier 59)
5. **diagnostic_surgical**: Diagnostic vs surgical procedure coding rules
6. **component_service**: Component service vs comprehensive service validation
7. **separate_procedure**: Separate procedure designation and reporting rules

### Confidence Assessment Settings

Confidence assessment is configured under `extraction.confidence`, and this preset
takes the system defaults for all of it:

- **Mode**: `separate` — confidence is scored in its own call after extraction
- **Model**: Amazon Nova Lite (`us.amazon.nova-lite-v1:0`)
- **List batch size**: 25 (`list_batch_size`) — oversized list fields are scored in
  batches of this many rows
- **Escalation**: enabled, up to 2 rounds, escalating to
  `us.anthropic.claude-sonnet-5:1m`
- **Human review threshold**: `hitl.confidence_threshold`, default `0.8`

Confidence assessment ensures high accuracy for medical data extraction, critical for
rule validation and compliance decisions.

### Summarization Settings

- **Model**: `us.anthropic.claude-sonnet-5:1m` (from the system defaults — this preset
  pins no summarization model)
- **Temperature**: 0 (deterministic outputs)
- **Top-k**: 5

The summarization component creates:
- Consolidated rule validation summaries with pass/fail/info-not-found counts per rule type (via orchestrator)
- Document summaries with citations and hover functionality
- Supporting page references for each rule evaluation

## Rule Validation Workflow

The rule validation process follows these detailed steps:

1. **Document Classification**: Identifies PA document sections (Administrative, Medical History, Operative Report, etc.) and groups pages into sections

2. **Information Extraction**: Extracts relevant medical and administrative data from each classified section

3. **Section-Level Rule Evaluation**: For each document section:
   - Evaluates each rule within each rule type against the section's content
   - Performs intelligent chunking if content exceeds token limits
   - Generates intermediate results for each rule evaluation
   - Stores section-level responses with supporting page references

4. **Rule Type Consolidation**: For each rule type (e.g., global_periods, same_day_service_rules):
   - Aggregates all rule evaluations across all document sections
   - Consolidates multiple responses for the same rule from different sections
   - Uses LLM to generate a single consolidated recommendation per rule
   - Produces consolidated responses with comprehensive reasoning and page citations

5. **Rule Validation Orchestration**: 
   - Collects all consolidated rule type responses
   - Generates overall statistics with dynamic recommendation counts
   - Creates rule summary by rule type with total rules and recommendation breakdowns
   - Produces supporting pages list across all evaluations
   - Saves consolidated summary in JSON and Markdown formats

6. **Document Summarization**: Generates comprehensive document summary with citations and hover functionality (separate from rule validation)

## Customizable Recommendation Options

The rule validation component supports user-customizable recommendation options:

- **Default Options**: Pass, Fail, Information Not Found
- **Custom Options**: Users can define custom recommendation values via UI (e.g., Approved, Rejected, Needs Review)
- **Dynamic Counting**: Statistics automatically adapt to custom recommendation options
- **Prompt Integration**: Custom options are injected into LLM prompts for consistent evaluation

## Test Set

This configuration is designed to work with respiratory surgery prior authorization packets:

- **Sample Document**: `samples/rule-validation/respiratory_pa_packet.pdf`
- **5 Document Sections**: Administrative, Medical History, Operative Report, Procedure Log, Claims Evidence
- **Multiple CPT Codes**: With modifiers and global period indicators
- **Rule Validation**: Tests against 7 rule types with multiple validation criteria

## How to Use

To use this rule validation configuration:

1. **Direct Use**: Deploy the GenAI IDP Accelerator with this configuration for healthcare PA packet processing with automated rule validation.

2. **As a Template**: Copy this configuration to a new directory and modify it for your specific medical coding rules:
   ```bash
   cp -r config_library/unified/rule-validation config_library/unified/your_use_case_name
   ```

3. **For Testing**: Use this configuration as a baseline for comparing the performance of customized rule validation configurations.

## Common Customization Scenarios

### Adding New Document Classes

To add a new class of PA document section:

1. Add a new entry to the `classes` array in `config.yaml`:
   ```yaml
   - $schema: https://json-schema.org/draft/2020-12/schema
     $id: YourDocumentType
     title: Your Document Type
     description: Description of your document section
     type: object
     x-aws-idp-document-type: YourDocumentType
     properties:
       your_property:
         type: string
         description: Description of the property
   ```

2. Test the configuration with sample documents containing the new section.

### Adding New Policy Types

To add a new policy type for rule validation:

1. Add a new entry to the `policy_classes` array in `config.yaml`:
   ```yaml
   - $schema: https://json-schema.org/draft/2020-12/schema
     x-aws-idp-policy-type: your_policy_type
     type: object
     rule_properties:
       your_rule:
         type: string
         description: Description of the validation rule
     $id: your_policy_type
   ```

2. The rule will automatically be evaluated during the rule validation step.

### Customizing Recommendation Options

To customize recommendation options:

1. Update the `recommendation_options` field in the `rule_validation` section of `config.yaml`:
   ```yaml
   rule_validation:
     recommendation_options: |-
       Approved: The requirement is fully satisfied.
       Rejected: The requirement is not met.
       Pending: Additional information needed.
   ```

2. The system will automatically use these options in prompts and statistics.

### Modifying Prompts

To adjust the behavior of rule validation:

1. Modify the `system_prompt` or `task_prompt` in the `rule_validation` section.
2. Keep the placeholders (e.g., `{recommendation_options}`, `{DOCUMENT_TEXT}`) intact.
3. Ensure `{recommendation_options}` is placed AFTER `<<CACHEPOINT>>` for proper caching.
4. Test the modified prompts with representative PA packets.

### Changing Models

To use a different model for rule validation:

1. Update the `model` field in the `rule_validation` or `rule_validation_orchestrator` section.
2. Adjust temperature, top_k, top_p, and max_tokens as needed for the new model.
3. Test the configuration with the new model to ensure compatibility.

## Analytics and Reporting

Rule validation results are automatically stored in AWS Glue tables for analytics and reporting:

### Athena Tables

Two tables are created in the ReportingDatabase:

1. **rule_validation_summary**: Document-level summary with pass/fail counts
   - Columns: document_id, input_key, validation_date, overall_status, total_policy_types, total_rules, pass_count, fail_count, information_not_found_count
   - Partitioned by date (YYYY-MM-DD format)

2. **rule_validation_details**: Individual rule results with recommendations
   - Columns: document_id, policy_type, rule, recommendation, reasoning, supporting_pages, validation_date
   - Partitioned by date (YYYY-MM-DD format)

### Agent Companion Chat

The Agent Companion Chat feature can query rule validation data using natural language:
- "How many documents failed rule validation?"
- "What are the most common rule failures?"
- "Show me rule validation trends over time"

See [Agent Companion Chat documentation](../../../docs/agent-companion-chat.md) for more details.

## Workflow Optimization

### Automatic Skipping

When no rules are configured (`policy_classes: []`), the rule validation workflow is automatically skipped:
- No Lambda invocations for rule validation
- No orchestration processing
- Reduces processing time and costs
- Workflow continues directly to next step

This optimization is useful when:
- Testing extraction without rule validation
- Processing documents that don't require rule validation
- Temporarily disabling rule validation

## Performance Considerations

The rule validation configuration is optimized for:

- **Accuracy**: Using temperature 0 for deterministic rule evaluation
- **Compliance**: Strict validation against medical coding guidelines
- **Traceability**: Supporting page references for each rule evaluation
- **Scalability**: Parallel processing with configurable semaphore limits

For specialized use cases, consider adjusting the configuration to focus on the specific rule types and document sections relevant to your needs.

## Pricing Configuration

The configuration includes pricing information for cost tracking:

- **Textract**: $0.0015 per page (first million), $0.0006 per page (over million)
- **Claude 3.5 Sonnet**: $0.000003 per input token, $0.000015 per output token
- **Amazon Nova Models**: Varies by model (Lite, Pro, etc.)

Pricing is used for metering and cost estimation in the reporting database.

## Contributors

- GenAI IDP Accelerator Team
