---
title: "Rule Validation"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Rule Validation

## Overview

Rule Validation automatically checks if your documents meet specific business rules and compliance requirements. It uses AI to evaluate documents against predefined criteria, making it useful for any industry that needs to validate documents against policies or regulations.

**Common Uses:**
- Healthcare: Checking prior authorizations, validating medical coding rules
- Financial Services: Reviewing bank statements, verifying loan applications, checking compliance
- Legal: Reviewing contract clauses, ensuring regulatory compliance
- Insurance: Validating claims, checking policy compliance
- Manufacturing: Quality control checks, specification compliance

The bank statement example we provide shows what's possible, but you can customize this for any industry.

> **See it as a packaged vertical:** the
> [Health Insurance Review sample feature](extensions/sample-health-insurance-review.md) wraps this
> rule-validation capability in an installable Feature Platform extension — it
> ships a prior-auth config preset, a `postRuleValidation` hook that turns rule
> results into a deterministic claim status, and a UI that visualizes both the
> per-rule results and a live Rules Discovery flow.

https://github.com/user-attachments/assets/bac617ec-edb0-4719-827f-175571a4b9f5

## Getting Started

### How to Enable Rule Validation

Policy rule validation runs in **both processing modes**, Pipeline mode and BDA mode: it is a step of the shared part of the workflow, after extraction and before Review Routing. In either mode it runs when **Enabled** is on (`rule_validation.enabled`) and the profile has at least one policy class.

In BDA mode, check that the profile holds the step's settings (`rule_validation.fact_extraction` with a model, and `rule_validation.rule_validation_orchestrator`). A profile that was switched to BDA mode in the web UI keeps the ones it had. A configuration supplied to the stack with `use_bda: true` is completed from the BDA system defaults, which hold no `rule_validation` section, so it has to carry those settings itself; without `rule_validation.fact_extraction` the step fails for a document that matches a policy class. The settings are listed under [Configuration](#configuration).

You can enable it in two ways:

**Option 1: During Stack Deployment**
- When deploying the CloudFormation stack, select the **rule-validation** configuration preset
- In the configuration dropdown, select **rule-validation**
- The stack deploys with rule validation enabled

**Option 2: Import After Deployment**
- Open the IDP-ACC Web UI
- Navigate to Configuration → Import
- Select **rule-validation** from the Config Library
- In **Processing Settings → Policy Rule Validation (Optional)**, check that **Enabled** is on

### Two-Step Process for Any Industry

Rule validation works for any industry using this two-step process:

#### Step 1: Extract Rules from Your Policy Documents (Optional)

**What you need:**
- A policy document containing your rules (PDF format)
  - Examples: compliance manuals, regulatory guidelines, coding policies, underwriting rules

**How to do it:** Use **Policy Discovery** — see [docs/policy-discovery.md](./policy-discovery.md) for the full walkthrough. In short:

1. In the Web UI, open the **Discovery** tab and switch to **Policy Discovery** mode.
2. Upload your policy PDF. Policy Discovery parses it into structured rules and writes them to the active configuration profile's `policy_classes` field.
3. Review the extracted rules in **Configuration → Policy Schema**, then save the profile.

**Skip this step if:** You already have your rules in a structured format — paste them directly into the Policy Schema editor.

#### Step 2: Validate Your Documents Against Rules

**What you need:**
- Rules to validate against (from Step 1 or your own structured rules)
- Documents to validate (PDF format)
  - Examples: applications, claims, authorization requests, contracts

**How to do it:**
1. **Enable Rule Validation**
   - Option A: Deploy stack with the **rule-validation** configuration preset
   - Option B: Import **rule-validation** from Config Library in the UI

2. **Configure Document Schema**
   - Go to Configuration → **Document Schema** tab
   - Define your document sections (e.g., Applicant Info, Financial Data, Supporting Docs)
   - Specify what attributes to extract from each section
   - These extracted attributes provide context to the AI for better validation

3. **Configure the Policy Schema**
   - Go to Configuration → **Policy Schema** tab
   - Paste the rules from Step 1 (or your own rules)
   - Organize into rule types
   - Add detailed descriptions for each rule

4. **Upload and Process Documents**
   - Upload documents to validate
   - System automatically processes and validates against your rules
   - View results showing Pass/Fail for each rule with detailed reasoning

### Quick Start with the Bank Statement Example

Want to see it in action first? We provide a complete bank statement example with sample documents and pre-configured rules. See the [Bank Statement Example](#bank-statement-review-example) section below for step-by-step instructions using:
- **Sample bank statement**: `samples/bank-statement-multipage.pdf`
  - Synthetic multi-page consolidated bank statement
  - Contains multiple parts: a summary of accounts, a checking account summary, and a list of transactions
- **Sample review policy**: `samples/rule-validation/bank-statement-review-policy.pdf`
  - "Bank Statement Review Policy" of the fictitious AnyCompany Financial Services, written by `scripts/generate_bank_statement_review_policy.py`
  - Contains the policy rules a bank statement must meet before it is accepted
- Pre-configured Policy Discovery and rule validation configs

## Key Features

- **Multi-Level Validation Workflow**: Policy classification, section-level evaluation, rule type consolidation, and orchestrated summary generation
- **Dual Validation Engines**: Choose LLM (semantic) or [Z3 (symbolic/deterministic)](./rule-validation-z3.md) per rule via `x-aws-idp-validation-engine`
- **Policy Classification**: Automatically determines which policy types apply to a document before validation
- **Asynchronous Processing**: Concurrent evaluation of multiple rules with built-in rate limiting
- **Intelligent Chunking**: Page-aware text chunking that preserves page boundaries and context
  - `chunks_created: 0` = No chunking (section processed as whole)
  - `chunks_created: 2+` = Section split into multiple chunks with 10% overlap
- **Customizable Recommendations**: User-defined recommendation options (e.g., Pass/Fail, Compliant/Non-Compliant)
- **Dynamic Statistics**: Automatic generation of recommendation counts based on actual results
- **Comprehensive Tracking**: Token usage, timing metrics, supporting page references, and chunking metadata
- **Dual Output Formats**: JSON for programmatic access and Markdown for human review
- **Robust Error Handling**: Graceful degradation with fallback responses

## Architecture

### Rule Validation Workflow

1. **Section-Level Evaluation**: Each document section is evaluated against all configured rule types
   - Rules are processed concurrently with semaphore-based rate limiting
   - Page-aware chunking handles large sections while preserving context
   - Results stored in S3 for each section

2. **Rule Type Consolidation**: Multiple section responses are consolidated per rule
   - LLM analyzes all evidence across sections
   - Generates single consolidated recommendation per rule
   - Aggregates supporting page references

3. **Orchestrated Summary Generation**: Final summary with statistics and reports
   - Dynamic recommendation counts (e.g., {"Pass": 10, "Fail": 2})
   - Overall statistics by rule type
   - JSON and Markdown output formats

### State Machine Integration

Policy rule validation is a step of the processing workflow, after extraction and
assessment and before Review Routing. The steps from policy rule validation on are
the same in Pipeline mode and BDA mode:

```
OCR → Classification → Extraction → Assessment → Policy Rule Validation → Review Routing → Summarization → Evaluation
```

The step has three states:
1. **Policy classification** (`PolicyClassificationStep`): determines which policy types apply to the document
2. **Validation by section** (the `ValidatePolicyRulesBySection` Map state, running `PolicyRuleValidationStep`): processes sections in parallel for matched policy types only
3. **Consolidation** (`PolicyRuleConsolidationStep`): consolidates the results and gives every policy rule its verdict

In the web UI the step is called **Policy Rule Validation**. Its policy rules are
edited on the **Policy Schema** tab, its settings are the **Policy Rule Validation
(Optional)** section of **Processing Settings** (before **Review Routing
(Optional)**, as in the pipeline), and its report is the **Policy Rule Validation
Summary**.

### When the step runs

- On a document's first run, when `rule_validation.enabled` is on and the profile
  has at least one policy.
- Again on the run after a **completed review**, on the corrected values (see
  [Human review](#human-review)).
- On a reprocess that starts at policy rule validation or at an earlier step.
  **Reprocess** can also start at a later step, or skip policy rule validation; the
  document then keeps the result it has.
- After **Process Changes** on a document whose field values were edited, which starts
  at this step.

## Human review

Policy rule validation runs **before** [Review Routing](./review-routing.md), so a
routing rule can send a document to a review queue because of a policy rule's verdict
(`policy_rule_verdict`, `failed_policy_rule_count`). A document that is held is
reviewed with its policy rule validation results available, and its review record
lists the policy rules it failed, with the explanation given for each, whichever
routing rule placed it (see
[Failed policy rules](./review-routing.md#failed-policy-rules)). The reviewer reads
them in the queue item's **Review Reason** panel, and opens the whole report with
**Reports › Policy Rule Validation Summary** on the edit page.

A routing rule that reads a policy rule result has nothing to read while policy rule
validation does not run for the profile. Saving a profile in that state is accepted
with a warning that names each such routing rule (see
[Warnings](./review-routing.md#warnings)).

When the review is completed, policy rule validation runs again on the corrected
values, and the summary is replaced. A document that is reviewed therefore runs the
step twice. Reviewers correct extracted values; the verdicts themselves are not
edited.

### No-Policy-Match Behavior

When a document doesn't match any configured policy class (e.g., the document name and page content don't match any `policy_classes` patterns), rule validation is skipped gracefully:

- A formatted Markdown summary is saved to S3 and displayed in the Web UI
- The message includes the document name and guidance on configuring policy classes
- The Step Functions workflow bypasses the rule validation Map state — no errors are raised

## Configuration

### Three-Step Rule Validation Approach

Rule validation uses a three-step approach to improve accuracy and handle large documents effectively:

1. **Policy Classification**: Determines which policy types apply to the document
2. **Fact Extraction**: Extracts relevant facts from document sections for matched policies
3. **Orchestrator**: Consolidates facts and makes final compliance decisions

This separation provides several benefits:
- Only relevant policy types are processed, reducing cost and time
- Large documents can be processed in chunks without losing context
- Fact-finding is separated from decision-making for clearer reasoning
- Multiple pieces of evidence are synthesized into accurate compliance determinations

### Basic Configuration

Configure policy rule validation in the `rule_validation` section of the
configuration. In the Web UI it is the **Policy Rule Validation (Optional)** section
of **Configuration → Processing Settings**:

```yaml
rule_validation:
  enabled: true
  semaphore: 5  # Max concurrent API calls
  max_chunk_size: 8000  # Characters per chunk
  overlap_percentage: 10  # Chunk overlap for context
  
  recommendation_options: |
    Pass: The requirement criteria are fully met.
    Fail: The requirement is partially met or requires additional information.
    Information Not Found: No relevant data exists in the user history.
  
  # Step 1: Fact Extraction Configuration
  fact_extraction:
    model: us.anthropic.claude-sonnet-4-6
    temperature: 0.0
    top_k: 20
    top_p: 0
    max_tokens: ""  # empty uses the selected model's maximum output
    system_prompt: |
      You are a specialized fact extraction assistant...
    task_prompt: |
      Extract relevant facts from the document text for the given rule.
      Document Text: {DOCUMENT_TEXT}
      Policy Type: {policy_type}
      Rule: {rule}
  
  # Step 2: Orchestrator Configuration
  rule_validation_orchestrator:
    model: us.anthropic.claude-sonnet-4-6
    temperature: 0.0
    top_k: 20
    top_p: 0
    max_tokens: ""  # empty uses the selected model's maximum output
    system_prompt: |
      You are a compliance decision orchestrator...
    task_prompt: |
      Based on the extracted evidence, determine compliance.
      Extracted Evidence: {extracted_evidence}
      Policy Class: {policy_class}
      Rule: {rule}
```

### Configuration Parameters

**Common Parameters** (top level):
- `enabled`: Turns rule validation on or off
- `semaphore`: Maximum number of concurrent Bedrock calls **per document**, in both
  the section-level fact-extraction step and the consolidation step (default: 5).
  It is a per-invocation bound, and the account-level call rate is this value times
  the number of documents in flight — so with `MaxConcurrentWorkflows` at its
  default of 100, a `semaphore` of 5 permits up to 500 concurrent calls from the
  consolidation step alone. That multiplier is the reason to be careful raising it.

  ⚠️ **The consolidation step now honours it and previously did not**, so a
  deployment that left the default at 5 will see that step issue fewer concurrent
  calls and take longer. What bounded it before was the event loop's default thread
  pool — `os.cpu_count() + 4` threads, capped at 32 — rather than this setting, so
  the width it actually ran at depended on the container rather than on the
  configuration. For the deployed 4,096 MB `RuleValidationOrchestrationFunction`
  that is roughly 6 to 7, so a 14-rule document previously ran in about two waves
  and now runs in three (`ceil(14 / 5)`). Raising the value is how to take that
  throughput back deliberately, bearing the multiplier above in mind. The
  end-to-end figures available are an **upper bound** on the change rather than the
  deployed one: measured against Claude Sonnet 4.5 on a 16-CPU machine, where the
  default pool is 20 threads wide and all 14 rules therefore ran at once,
  consolidation took 16.9 s at `semaphore: 5` against 5.7 s unbounded.
- `max_chunk_size`: Maximum **tokens** per chunk (default: 8000). Multiplied by
  `token_size`, the assumed characters per token (default: 4), to get the character
  budget a chunk is measured against — 32,000 characters with the defaults
- `overlap_percentage`: How much of the previous chunk is repeated at the start of the
  next, to keep a fact that spans the boundary readable (0-100, default: 10).
  ⚠️ **It does not govern every chunk boundary.** Chunking is page-aware, and when the
  previous chunk held **more than one** complete page the whole of its last page is
  repeated regardless of this setting — which is the usual case for a multi-page
  document. The percentage applies where the previous chunk held a **single** page,
  i.e. on documents whose pages are large relative to `max_chunk_size`, and to the
  character-based fallback. In both of those, `0` repeats nothing. Values above 50 are
  reduced to 50 by the character fallback, which logs that it did, because a smaller
  stride multiplies the number of model calls rather than improving context
- `recommendation_options`: Custom recommendation categories for your use case

**Fact Extraction Parameters**:
- `model`: The LLM model to use for extracting facts
- `temperature`: Controls randomness in responses (0.0 = fully deterministic)
- `system_prompt`: Defines the role and behavior of the fact extraction assistant
- `task_prompt`: Instructions for extracting facts, with these placeholders:
  - `{DOCUMENT_TEXT}`: The actual document content
  - `{policy_type}`: The category of policy class being evaluated
  - `{rule}`: The specific rule text

**Orchestrator Parameters**:
- `model`: The LLM model to use for making compliance decisions
- `temperature`: Controls randomness in responses (0.0 = fully deterministic)
- `system_prompt`: Defines the role and behavior of the compliance orchestrator
- `task_prompt`: Instructions for making decisions, with these placeholders:
  - `{extracted_evidence}`: Facts gathered from all chunks and sections
  - `{policy_class}`: The category of policy class being evaluated
  - `{rule}`: The specific rule text

### Policy Classes

Define policy types and specific rules to evaluate. These policy classes are from the **rule-validation** preset:

```yaml
policy_classes:
  - $schema: https://json-schema.org/draft/2020-12/schema
    x-aws-idp-policy-type: statement_period
    x-aws-idp-document-name-regex: "(?i).*bank[_ -]?statement.*"
    type: object
    rule_properties:
      statement_date_shown:
        type: string
        description: >-
          The statement must show the date it was issued or the period
          it covers. The rule fails when the statement prints neither.
      balance_dates_in_order:
        type: string
        description: >-
          The date printed for the new balance of an account must be
          the same as or later than the date printed for its previous
          balance. The rule fails when the new balance is dated before
          the previous balance.
      transactions_within_period:
        type: string
        description: >-
          Every transaction listed on the statement must be dated
          within the period the statement covers and no later than the
          statement date. The rule fails when a transaction is dated
          outside that period.
    $id: statement_period

  - $schema: https://json-schema.org/draft/2020-12/schema
    x-aws-idp-policy-type: balance_reconciliation
    x-aws-idp-document-name-regex: "(?i).*bank[_ -]?statement.*"
    type: object
    rule_properties:
      new_balance_reconciles:
        type: string
        description: >-
          For each account, the new balance must equal the previous
          balance, minus checks and other withdrawals, plus deposits
          and credits, using the amounts printed in the account
          summary. The rule fails when the printed amounts do not add
          up to the printed new balance.
      summary_total_matches_accounts:
        type: string
        description: >-
          The total printed for a group of accounts in the summary of
          accounts must equal the sum of the balances of the accounts
          in that group. The rule fails when the total differs from
          that sum.
    $id: balance_reconciliation
```

Each key of `rule_properties` is one policy rule, and `x-aws-idp-document-name-regex` is the pattern that says which documents the policy class applies to (see [Required: Add a Document Matching Regex](./policy-discovery.md#required-add-a-document-matching-regex)).

### Document Classes

Specify which document types should be validated:

```yaml
classes:
  - $schema: https://json-schema.org/draft/2020-12/schema
    $id: Bank-Statement
    type: object
    x-aws-idp-document-type: Bank-Statement
    description: >-
      A periodic financial document issued by banks detailing account activity,
      balances, and transactions over a specific time period.
    properties:
      bank_name:
        type: string
      account_holder_name:
        type: string
      account_number:
        type: string
```

## Customizing Recommendation Options

The default recommendation options are Pass/Fail/Information Not Found, but you can customize these for your specific use case:

### Compliance Review Example

```yaml
rule_validation:
  recommendation_options: |
    Compliant: Fully meets regulatory requirements.
    Non-Compliant: Does not meet requirements.
    Requires Review: Manual review needed.
    Not Applicable: Rule does not apply to this case.
```

### Financial Audit Example

```yaml
rule_validation:
  recommendation_options: |
    Approved: All criteria satisfied.
    Rejected: Criteria not met.
    Pending: Additional documentation required.

policy_classes:
  - policy_type: "loan_eligibility"
    questions:
      - "Applicant must have minimum credit score of 650..."
      - "Debt-to-income ratio must not exceed 43%..."
  
  - policy_type: "documentation_requirements"
    questions:
      - "Two years of tax returns must be provided..."
      - "Proof of employment must be current within 30 days..."
```

The statistics in the final summary will automatically use your custom options:

```json
{
  "recommendation_counts": {
    "Compliant": 15,
    "Non-Compliant": 3,
    "Requires Review": 2
  }
}
```

## Rule Configuration Best Practices

### Writing Effective Rules

1. **Be Specific**: Include clear criteria and conditions
   ```yaml
   rule_properties:
     balance_dates_in_order:
       type: string
       description: "The date printed for the new balance of an account must be the same as or later than the date printed for its previous balance..."
   ```

2. **Provide Context**: Include relevant definitions and examples
   ```yaml
   rule_properties:
     large_withdrawals_identified:
       type: string
       description: "Every withdrawal of 10,000.00 or more must have a description that identifies the payee. The rule fails when such a withdrawal has no description, or a description that names no payee."
   ```

3. **Structure Complex Rules**: Use JSON format for rules with multiple components
   ```yaml
   rule_properties:
     new_balance_reconciles:
       type: string
       description: |
         {
           "rule_text": "For each account, the new balance must equal the previous balance, minus checks and other withdrawals, plus deposits and credits...",
           "amounts_subtracted": ["checks", "other withdrawals"],
           "amounts_added": ["deposits", "credits"],
           "fails_when": ["the printed amounts do not add up to the printed new balance"]
         }
   ```

### Organizing Policy Types

Group related rules into logical policy types:

```yaml
policy_classes:
  - x-aws-idp-policy-type: statement_identity
    type: object
    rule_properties:
      institution_named:
        type: string
        description: "The statement must name the financial institution that issued it..."
      account_holder_named:
        type: string
        description: "The statement must show the full name and the mailing address of the account holder..."
    $id: statement_identity

  - x-aws-idp-policy-type: transaction_review
    type: object
    rule_properties:
      transactions_in_date_order:
        type: string
        description: "Transactions must be listed in date order, oldest first, within each account..."
      no_duplicate_transactions:
        type: string
        description: "A transaction must be listed once..."
    $id: transaction_review
```

## Output Formats

### JSON Output

Located at `s3://{bucket}/{document_id}/rule_validation/consolidated/consolidated_summary.json`:

```json
{
  "document_id": "doc_123",
  "overall_status": "COMPLETE",
  "total_policy_types": 4,
  "overall_statistics": {
    "total_rules": 11,
    "recommendation_counts": {
      "Pass": 8,
      "Fail": 2,
      "Information Not Found": 1
    }
  },
  "rule_summary": {
    "statement_identity": {
      "status": "COMPLETE",
      "total_rules": 3,
      "Pass": 3,
      "Fail": 0
    }
  },
  "supporting_pages": ["1", "2", "3", "5"]
}
```

Each policy type's rules are under `rule_details`:

```json
"rule_details": {
  "statement_period": {
    "verdict_counts": {"pass": 2, "fail": 1, "not_found": 0, "unknown": 0},
    "rules": [
      {
        "rule_id": "statement_period.balance_dates_in_order",
        "z3_rule_id": null,
        "rule": "The date printed for the new balance of an account must be the same as or later than the date printed for its previous balance. The rule fails when the new balance is dated before the previous balance.",
        "recommendation": "Fail",
        "verdict": "fail",
        "supporting_pages": ["1"],
        "reasoning": "…"
      }
    ]
  }
}
```

- **`rule_id`** is `policy_type.rule_key`: the policy type and the rule's key in the
  Policy Schema. It is stable across runs and is what a
  [routing rule](./review-routing.md#policy-rule-verdicts-in-routing-rules) names. The
  Markdown report shows it beside the rule text. A rule that has an
  `x-aws-idp-rule-id` (see [Z3 Symbolic Rule Validation Engine](./rule-validation-z3.md))
  also carries it, as `z3_rule_id`.
- **`verdict`** is the recommendation as one of four fixed values: `pass`, `fail`,
  `not_found` (for **Information Not Found**) or `unknown` (any other label, and a rule
  that got no recommendation). `recommendation` keeps the label as configured.
  `overall_statistics` and each policy type also carry `verdict_counts`. With
  [customized recommendation options](#customizing-recommendation-options), the counts
  by label work as before and every custom label reads as `unknown` in `verdict`.

Two things to know if you read this file programmatically.

**`supporting_pages` is always a list of strings.** Page references reach the summary
from two engines and from model output, so they arrive as strings and as numbers; the
document-level list canonicalises them to strings, drops duplicates, and orders
numeric references by value followed by anything non-numeric by codepoint (so `'Zebra'`
precedes `'apple'`). The per-rule lists under `rule_details` are **not** canonicalised
— they hold exactly what each rule's response returned, which is the record of the
evidence cited, and their order is whichever engine produced them.

**`overall_status` is `"ERROR"` with an `error` field when consolidation did not
complete.** The statistics alongside it are real and consistent with each other — a
rule is counted once it has been read, and `pass_percentage` is computed over the rules
counted — but they cover only what was reached before the failure, so read the counts
as a floor on what was evaluated rather than as the document's total. The Markdown
report states this above the statistics table. A document whose per-section validation
failed is a different case and is reported per section — see
[Where a failed rule validation shows up](#where-a-failed-rule-validation-shows-up).

### Markdown Output

Located at `s3://{bucket}/{document_id}/rule_validation/consolidated/consolidated_summary.md`:

```markdown
# Policy Rule Validation Summary: doc_123

**Document ID:** doc_123
**Status:** COMPLETE
**Total Rule Types:** 4

## Overall Statistics

**Total Rules:** 11
**Pass:** 8
**Fail:** 2
**Information Not Found:** 1

## Rule Type: statement_identity

**Total Rules:** 3
**Pass:** 3

### Rules

| Rule | Recommendation | Reasoning | Supporting Pages |
|------|----------------|-----------|------------------|
| The statement must name the financial institution that issued it... | Pass | Evidence found on page 1... | 1, 3 |
```

## Performance Optimization

### Rate Limiting

Control concurrent API calls to prevent throttling:

```yaml
rule_validation:
  semaphore: 5  # Adjust based on your API limits
```

### Prompt Caching

Place static content before the `<<CACHEPOINT>>` marker and dynamic content after:

```yaml
task_prompt: |
  You are a bank statement reviewer...
  
  <<CACHEPOINT>>
  
  {recommendation_options}
  
  <user_history>
  {DOCUMENT_TEXT}
  </user_history>
```

This caches the static instructions ahead of the dynamic document content. Whether that lowers cost depends on the prefix clearing the model's minimum and being read again within five minutes; see the [prompt caching study](./benchmarking/studies/prompt-caching.md).

### Chunking Configuration

Adjust chunking parameters for your document sizes:

```yaml
rule_validation:
  max_chunk_size: 8000  # Increase for longer documents
  overlap_percentage: 10  # Increase for more context preservation
```

## Integration with Extraction

Rule validation works seamlessly with extraction results:

1. **Extraction Phase**: Extracts structured data from documents
2. **Rule Validation Phase**: Validates extracted data against business rules
3. **Combined Output**: Both extraction and validation results available in document object

Access both results:

```python
# Extraction results
extraction_uri = section.extraction_result_uri
extraction_data = s3.get_json_content(extraction_uri)

# Rule validation results
validation_uri = document.rule_validation_result.output_uri
validation_data = s3.get_json_content(validation_uri)
```

## Monitoring and Debugging

### CloudWatch Metrics

Monitor rule validation performance:
- Execution duration per section
- Token usage and costs
- Error rates and types

### Logs

Check CloudWatch logs for:
- `rule-validation-policy-classification-function`: Policy classification logs
- `rule-validation-function`: Section-level evaluation logs
- `rule-validation-orchestration-function`: Orchestration logs

### Where a failed rule validation shows up

When rule validation fails, the failure is recorded **on the sections it affects**,
so the document's Sections panel names them instead of leaving the explanation only
in the Step Functions cause and the CloudWatch log. The Status column reads
**Failed**, and hovering the status shows the issue with the technical cause behind
it.

Three codes are written, all at error severity, because they describe different
situations:

| Code | Written by | Meaning |
|---|---|---|
| `rule_validation_failed` | the per-section rule validation step | This section's own rule validation did not complete, so it has no compliance verdict. The cause is whatever the rule-validation service recorded — a missing page, a solver timeout, a model error. |
| `rule_validation_not_consolidated` | the orchestration step | Every section was validated, but the step that turns those results into the document's single compliance decision failed. No section has a verdict, so every section carries this issue. |
| `section_processing_failed` | the collate step | That step found this section's earlier processing had failed. |

⚠️ **Look on the document's own page, not the document list.** The Sections panel
always shows these, because it reads the section's issues directly. The document
list's **Processing Issues** badge is only refreshed by the two steps that write the
whole document — the orchestration and collate steps — so a `rule_validation_failed`
recorded by the per-section step can leave that badge showing its previous value,
commonly zero. The per-section write updates one section atomically and cannot know
the document-wide total: it does not read the stored item, and ten sections are being
written concurrently. `extraction_failed` reaches the badge the same way.

The exception is unchanged by this: the Step Functions cause still carries the same
message it always did. What changed is that the document's own record now carries it
too.

**A transient failure is not marked, and it is retried.** A throttle, a read
timeout, a dropped connection or a model that is not ready is retried by the state
machine — eight attempts at 2.5× backoff from ten seconds, about 2.8 hours of
backoff, on both the per-section and orchestration steps — so flagging the sections
would show them failed for as long as that ladder runs and then clear itself.

The two decisions come from the same place, which is what makes the suppression
trustworthy. Step Functions decides whether to retry from the Python exception's
**class name**, while transience is a property of the error **code** and the
exception's cause chain — a question no retry list can ask. So rule validation
classifies the failure once, in `idp_common.utils.transient_errors`, and re-raises
every transient cause under one class name that all three rule-validation states
list. A record is withheld exactly when a retry is genuinely coming.

If a ladder exhausts every attempt, the document fails with the explanation in the
Step Functions cause and the sections are **not** flagged. A step cannot tell its
last attempt from its first — Step Functions does not tell it which attempt it is
on — so the final one suppresses the record for the same reason the first did. The
failure is still counted by the failure alarms and the dead-letter queue; what is
missing is the per-section diagnosis. Extraction and assessment behave identically.

### A transient fault is never answered with a verdict

`Information Not Found` is a **verdict**: it is one of the configured
`recommendation_options`, it is counted in the document's rule-validation summary,
and downstream consumers act on it — the sample health-insurance review feature reads
a document made mostly of them as *insufficient documentation*. So it has to mean
"the document does not evidence this rule", and not "we could not reach the model".

Rule validation therefore separates the two everywhere it could previously conflate
them. A deterministic failure for one rule still yields `Information Not Found` with
the reason in its `reasoning` field, which is what keeps one unparseable rule from
discarding every other rule's answer. A transient failure raises instead, so the step
is retried and the rule gets a real answer. The same split applies to the Z3 engine's
value-extraction call, to the summarisation step that turns per-section facts into the
document's decision, and to the consolidation step — which previously returned an
empty result rather than failing, so a throttle could finish a document with **no**
verdicts, no failed status and nothing recorded anywhere.

A related fault is handled the same way, because it silently changed a result rather
than reporting a failure: a page whose text could not be read is a page whose
policy-matching regexes never ran, so a policy type evidenced only on that page went
unmatched and none of its rules were validated. It is now retried rather than absorbed.

**The cleanup that removes the previous run's per-section results is best-effort, and
consolidation does not depend on it.** Consolidation reads the explicit list of
per-section outputs *this* run produced, passed to it by the orchestration step, so an
object the cleanup failed to delete is not in that list and is not read. A surviving
object costs storage and makes the prefix confusing to read by hand; it cannot reach a
verdict. A transient failure of the cleanup is still retried, because a retry is the
right answer to a throttle on its own terms.

⚠️ **If you call the consolidation service directly, pass `section_uris`.** Omitting it
falls back to listing the prefix, which is the right behaviour for re-consolidating an
existing prefix by hand and the wrong behaviour for a run of your own: on a reprocessed
document that prefix can still hold the previous run's verdicts for the same document,
which are plausible enough to be consolidated and acted on. See the module README for
the parameter's three values.

**A document-scope explanation has no section to attach to, and is not invented
one.** The collate step also collects free-text errors that belong to the document
as a whole rather than to any one section — page-level OCR problems, for example.
Those are not attributed to a section, because picking one would be a claim the
pipeline cannot support; they remain in the Step Functions cause and the step's log.
Per-page classification failures behind them are separately recorded on their own
sections by the classification stage.

**A very long explanation is abridged in the middle**, on the same 4 KB bound every
processing issue's technical cause carries, with the error count written at the front
so it survives the abridgement. The unabridged text is in the step's CloudWatch log.

### Common Issues

**High Token Usage**: 
- Reduce `max_chunk_size`
- Optimize rule descriptions
- Use prompt caching effectively

**Slow Processing**:
- Increase `semaphore` value — it bounds both the fact-extraction and the
  consolidation step, so raising it shortens both. Watch for Bedrock throttling
  as you do: the account-level call rate is this value times the number of
  documents in flight.
- Reduce number of rules
- Use faster model (e.g., Claude Haiku)

**Inconsistent Results**:
- Set `temperature: 0` for deterministic output
- Improve rule clarity and specificity
- Add more context in rule descriptions

## Cost Considerations

Rule validation costs depend on:
- Number of rules evaluated
- Document size and chunking
- Model selection
- Prompt caching effectiveness
- Whether the document is reviewed: a document held in a review queue runs the step
  again after its review is completed

The verdict pass makes one model call per policy rule the symbolic engine did not
decide, for every document, including a document with one section.

**Cost Optimization Tips**:
1. Use prompt caching on busy stacks: a cached prefix that is read again within five minutes is billed at a tenth of the input price on most Claude models (a twentieth on Claude Sonnet 5.5 and Opus 5.5), but one that is never read again is billed at about 25% more than uncached, and a prefix below the model's minimum is never cached ([prompt caching study](./benchmarking/studies/prompt-caching.md))
2. Choose appropriate model (Haiku for simple rules, Sonnet for complex)
3. Minimize rule redundancy
4. Optimize chunk sizes to reduce API calls

## API Reference

For detailed API documentation, see the [Rule Validation Module README](../lib/idp_common_pkg/idp_common/rule_validation/README.md).

## Examples

### Bank Statement Review Example

We provide a complete bank statement example demonstrating the validation of a bank statement against the policy rules of a review policy.

**Sample Documents:**
- **Bank Statement**: `samples/bank-statement-multipage.pdf`
  - Synthetic multi-page consolidated statement of "Example Inc. Credit Union", with the statement date 04/04/2022
  - Contains multiple parts:
    - Summary of Accounts (a checking account and a savings account, with account numbers and balances)
    - Checking Account Summary (previous balance, checks, withdrawals, deposits, new balance)
    - Transactions (date, description, amount)

- **Review Policy**: `samples/rule-validation/bank-statement-review-policy.pdf`
  - "Bank Statement Review Policy" of the fictitious AnyCompany Financial Services
  - Written by `scripts/generate_bank_statement_review_policy.py`
  - Contains the policy rules a bank statement must meet before it is accepted

**Configuration Files:**
- **Step 1 - Policy Discovery**: extracted rules are written into `policy_classes` on the active configuration profile (see [docs/policy-discovery.md](./policy-discovery.md))
- **Step 2 - Rule Validation**: `config_library/unified/rule-validation/config.yaml`

This example includes:
- Statement identity policy rules (`statement_identity`)
- Statement period policy rules (`statement_period`)
- Balance reconciliation policy rules (`balance_reconciliation`)
- Transaction review policy rules (`transaction_review`)
- The `Bank-Statement` document class

Every policy class of the preset applies to a document whose file name says it is a bank statement (`x-aws-idp-document-name-regex`), so keep the sample's file name when you upload it.

The statement is meant to show failed policy rules beside passed ones:

| Policy rule | What the statement prints | What the policy rule tests |
|---|---|---|
| `statement_period.balance_dates_in_order` | The checking account summary gives a previous balance "as of 04/04/2022" and a new balance "as of 06/17/2020" | The new balance is dated the same as or later than the previous balance |
| `statement_period.transactions_within_period` | The statement date is 04/04/2022, and the transactions are dated throughout 2020 | Every transaction is dated within the period the statement covers and no later than the statement date |
| `transaction_review.transactions_in_date_order` | Pages 2 to 5 list the transactions out of date order | Transactions are listed oldest first |
| `transaction_review.no_duplicate_transactions` | Pages 4 and 5 repeat transactions that pages 2 and 3 list | A transaction is listed once |

A verdict comes from a model and can differ between runs, so the table says what each policy rule reads, and not the verdict it gets.

**Note**: This is a reference implementation. You can replace these rules with your own domain-specific requirements.

### Testing with Notebooks

For hands-on examples, see:
`notebooks/misc/e2e-holistic-packet-classification-rule-validation.ipynb`

This notebook demonstrates:
- Loading and configuring rule validation
- Processing document sections
- Consolidating results
- Viewing output formats

## Skipping Rule Validation for Excluded Classes

If a document class is marked with
`x-aws-idp-exclude-from-processing: true` (see
[Excluding Static Pages in the Classification docs](classification.md#excluding-static-pages-eg-instructions-legal-boilerplate)),
`RuleValidationService.validate_document_async` short-circuits for any
section classified as that class:

- **No LLM call is made** inside the per-section `process_one_section`
  coroutine — it returns immediately with `(section_responses={}, 0, False)`
  meaning "no rule responses, zero chunks created, no chunking needed".
- **No output file is written** — rule validation has no per-section
  artifact of its own, and the extraction stub (`result.json`) already
  records that this section was skipped.
- Rule validation aggregation simply sees zero responses for the
  skipped section and proceeds normally with the active sections.

This keeps rule validation clean on mixed documents (e.g. a DS-11
passport application package where only the 2 form pages need rule
checks — the 4 boilerplate pages are noise and produce no rule
triggers).

## Best Practices

1. **Start Simple**: Begin with a few clear rules and expand gradually
2. **Test Thoroughly**: Validate rules against known good/bad examples
3. **Monitor Costs**: Track token usage and optimize as needed
4. **Use Caching**: Always place static content before `<<CACHEPOINT>>`
5. **Clear Recommendations**: Define unambiguous recommendation options
6. **Document Rules**: Include context and examples in rule descriptions
7. **Track Changes**: Each save cuts a profile [revision](./configuration-profiles.md#revision-history); compare revisions to track rule changes and their impact on results
8. **Regular Review**: Periodically review and update rules based on results
