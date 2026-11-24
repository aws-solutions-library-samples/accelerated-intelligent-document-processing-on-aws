Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Rule Validation Configuration

This directory contains the rule validation configuration for the GenAI IDP Accelerator. This configuration is designed for the review of a bank statement: the statement is classified and extracted, and policy rule validation checks it against the policy rules of a bank statement review policy.

## Processing Mode

**Default Mode**: Pipeline (use_bda: false) — Uses Amazon Textract for OCR, then Bedrock LLM for classification, extraction, and rule validation. Set use_bda: true for BDA mode.

## Validation Level

**Level**: 1 - Basic Testing

- **Testing Evidence**: The configuration loads and passes the config library tests (`make test-config-library`). `scripts/tests/test_generate_bank_statement_review_policy.py` checks that its policy classes hold the policy rules of the policy document word for word, and that every policy class applies to the sample statement by its file name. No live run on the bank statement sample has been recorded.
- **Known Limitations**: Policy rule verdicts come from a model and can differ between runs. The policy applies to a document by its file name, so a bank statement whose name does not say what it is gets no policy rule validation. Performance may vary with statement layouts other than the sample's, and with documents of poor image quality that affects OCR accuracy. Rule validation accuracy depends on the completeness and clarity of what the statement prints.

## Overview

The rule validation configuration is designed to review a bank statement before it is accepted as evidence of an account and its activity:

- **Statement Identity**: The institution, the account holder and the account numbers are printed
- **Statement Period**: The statement is dated, and its balances and transactions fall within the period it covers
- **Balance Reconciliation**: The printed amounts add up to the printed balances
- **Transaction Review**: The transactions are in date order and listed once, and a large withdrawal names its payee

It includes settings for document classification, extraction of the statement's values, automated policy rule validation against the review policy, and summarization using Amazon Bedrock models.

The policy rules are those of `samples/rule-validation/bank-statement-review-policy.pdf`, which is also the example policy document of [Policy Discovery](../../../docs/policy-discovery.md).

## Key Components

### Document Classes

The `classes` section holds the document class of a bank statement:

- **Bank-Statement**: A periodic financial document issued by banks detailing account activity, balances, and transactions over a specific time period. It is the `Bank-Statement` class of the [lending-package-sample](../lending-package-sample/) preset, unchanged. Its attributes are the bank name, the account holder's name and address, the account number and type, the statement start and end dates, the account summary rows (description and amount) and the transaction rows (date, description, deposits, withdrawals and balance).

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

The classification component analyzes document content and structure to identify each section's document class and establish the section boundaries within the document.

### Extraction Settings

- **Model**: `us.anthropic.claude-sonnet-5` (from the system defaults — this preset
  pins no extraction model)
- **Mode**: `simple` (`extraction.mode`); agentic extraction is off
- **Temperature**: 0 (deterministic outputs)
- **Top-k**: 5

There is no `extraction.max_tokens` setting. Extraction always requests the model's
maximum output, resolved per model from `config_library/model_config_limits.yaml`.

The extraction component extracts the attributes of the `Bank-Statement` class, including:
- Bank name
- Account holder name and address
- Account number and account type
- Statement start and end dates
- Account summary rows
- Transaction details (date, description, deposits, withdrawals and balance)

### Rule Validation Settings

- **Model**: `us.anthropic.claude-sonnet-4-6` for both `fact_extraction` and
  `rule_validation_orchestrator` (from the system defaults — this preset sets only
  `rule_validation.enabled`)
- **Temperature**: 0 (deterministic outputs)
- **Top-k**: 20
- **Semaphore**: 5 concurrent API calls
- **Chunking**: `max_chunk_size` 8000 with 10% overlap
- **Recommendation Options**: Configurable (default: Pass, Fail, Information Not Found)

The rule validation component evaluates the statement against the policy rules of the bank statement review policy, by policy type:
- **Statement Identity** (`statement_identity`): Checks that the statement names the institution, the account holder and an account number for every account
- **Statement Period** (`statement_period`): Checks the statement date, the order of the balance dates and the dates of the transactions
- **Balance Reconciliation** (`balance_reconciliation`): Checks that the printed amounts add up to the new balance, and that a summary total is the sum of its accounts
- **Transaction Review** (`transaction_review`): Checks that the transactions are in date order and listed once, and that a large withdrawal names its payee

### Policy Classes

The `policy_classes` section holds one policy class for each section of the policy document, and one policy rule for each policy rule the policy prints:

| Policy type | Policy rule | What the statement must show |
|---|---|---|
| `statement_identity` | `institution_named` | The financial institution that issued it |
| `statement_identity` | `account_holder_named` | The full name and the mailing address of the account holder |
| `statement_identity` | `account_number_shown` | An account number for every account it summarises |
| `statement_period` | `statement_date_shown` | The date it was issued or the period it covers |
| `statement_period` | `balance_dates_in_order` | A new balance dated the same as or later than the previous balance of the account |
| `statement_period` | `transactions_within_period` | Every transaction dated within the period the statement covers and no later than the statement date |
| `balance_reconciliation` | `new_balance_reconciles` | For each account, a new balance that equals the previous balance, minus checks and other withdrawals, plus deposits and credits |
| `balance_reconciliation` | `summary_total_matches_accounts` | A total for a group of accounts that equals the sum of the balances of the accounts in that group |
| `transaction_review` | `transactions_in_date_order` | Transactions listed in date order, oldest first, within each account |
| `transaction_review` | `no_duplicate_transactions` | Each transaction listed once |
| `transaction_review` | `large_withdrawals_identified` | A description that identifies the payee for every withdrawal of 10,000.00 or more |

Each policy rule's description in `config.yaml` is the text the policy prints: the requirement, then the case in which the policy rule fails. A policy rule's stable id is its policy type and its key, for example `statement_period.transactions_within_period`.

Every policy class carries `x-aws-idp-document-name-regex: "(?i).*bank[_ -]?statement.*"`, so the policy applies to a document whose file name says it is a bank statement, such as `bank-statement-multipage.pdf` or `Bank_Statement_March.pdf`.

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

Confidence assessment gives every extracted value of the statement a confidence score.

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

1. **Document Classification**: Identifies the bank statement and groups its pages into sections

2. **Information Extraction**: Extracts the statement's values from each classified section

3. **Section-Level Rule Evaluation**: For each document section:
   - Evaluates each rule within each rule type against the section's content
   - Performs intelligent chunking if content exceeds token limits
   - Generates intermediate results for each rule evaluation
   - Stores section-level responses with supporting page references

4. **Rule Type Consolidation**: For each rule type (e.g., statement_period, transaction_review):
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

This configuration is designed to work with the bank statement sample of this repository:

- **Sample Document**: `samples/bank-statement-multipage.pdf`, a synthetic consolidated statement of "Example Inc. Credit Union" for one account holder, with the statement date 04/04/2022. Page 1 holds the summary of accounts and the checking account summary, and pages 2 to 5 list transactions.
- **Policy Document**: `samples/rule-validation/bank-statement-review-policy.pdf`, the "Bank Statement Review Policy" of the fictitious AnyCompany Financial Services. `scripts/generate_bank_statement_review_policy.py` writes it, and the `policy_classes` of this configuration hold its policy rules.
- **Rule Validation**: The sample's file name matches the document name pattern of every policy class, so every policy rule is evaluated on it.

The statement is meant to show failed policy rules beside passed ones. These policy rules have something to find in it:

| Policy rule | What the statement prints | What the policy rule tests |
|---|---|---|
| `statement_period.balance_dates_in_order` | The checking account summary gives a previous balance "as of 04/04/2022" and a new balance "as of 06/17/2020" | The new balance is dated the same as or later than the previous balance |
| `statement_period.transactions_within_period` | The statement date is 04/04/2022, and the transactions are dated throughout 2020 | Every transaction is dated within the period the statement covers and no later than the statement date |
| `transaction_review.transactions_in_date_order` | Pages 2 to 5 list the transactions out of date order | Transactions are listed oldest first |
| `transaction_review.no_duplicate_transactions` | Pages 4 and 5 repeat transactions that pages 2 and 3 list | A transaction is listed once |

A verdict comes from a model, and no live run on this sample has been recorded: the table says what each policy rule reads, and not the verdict it gets.

## How to Use

To use this rule validation configuration:

1. **Direct Use**: Deploy the GenAI IDP Accelerator with this configuration for bank statement review with automated rule validation.

2. **As a Template**: Copy this configuration to a new directory and modify it for the policy rules of your own policy:
   ```bash
   cp -r config_library/unified/rule-validation config_library/unified/your_use_case_name
   ```

3. **For Testing**: Use this configuration as a baseline for comparing the performance of customized rule validation configurations.

## Common Customization Scenarios

### Adding New Document Classes

To add a new document class:

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
     x-aws-idp-document-name-regex: "(?i).*bank[_ -]?statement.*"
     type: object
     rule_properties:
       your_rule:
         type: string
         description: Description of the validation rule
     $id: your_policy_type
   ```

2. The policy rule will automatically be evaluated during the rule validation step, on every document whose file name matches the pattern. With more than one policy class, a policy class that has no pattern is not evaluated.

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
4. Test the modified prompts with representative bank statements.

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
   - Columns: document_id, policy_type, rule, recommendation, reasoning, supporting_pages, validation_date, rule_id, verdict
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
- **Compliance**: Strict validation against the requirements of the review policy
- **Traceability**: Supporting page references for each rule evaluation
- **Scalability**: Parallel processing with configurable semaphore limits

For specialized use cases, consider adjusting the configuration to focus on the specific rule types and document sections relevant to your needs.

## Contributors

- GenAI IDP Accelerator Team
