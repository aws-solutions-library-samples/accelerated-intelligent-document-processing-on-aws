---
title: "Z3 Symbolic Rule Validation Engine"
---

<!-- Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. -->
<!-- SPDX-License-Identifier: MIT-0 -->

# Z3 Dual-Engine Rule Validation

## Overview

The Z3 engine provides **deterministic, formal** rule validation using the [Z3 theorem prover](https://github.com/Z3Prover/z3). It runs alongside the existing LLM engine — each rule can be individually routed to either engine via the `x-aws-idp-validation-engine` schema extension.

| Engine | Approach | Deterministic | Best For |
|--------|----------|---------------|----------|
| LLM (default) | Semantic reasoning over extracted facts | No | Subjective rules, complex language |
| Z3 | Formal SMT-LIB constraint solving | Yes | Mathematical rules, threshold checks, comparisons |

## Configuration

### Per-Rule Engine Selection

In your `policy_classes` config, set `x-aws-idp-validation-engine` on each rule property:

```yaml
policy_classes:
  - x-aws-idp-policy-type: invoice_validation
    rule_properties:
      total_check:
        type: string
        description: "Total must equal subtotal + tax"
        x-aws-idp-validation-engine: z3    # Formal validation
        x-aws-idp-rule-id: total_check
        x-aws-idp-rule-json:              # Generated via Config Editor button
          rule_id: total_check
          version: "1.0"
          description: "Total equals subtotal plus tax"
          natural_language_rule: "Total must equal subtotal + tax"
          parameters:
            - name: total
              type: Real
              required: true
              description: "Invoice total amount"
            - name: subtotal
              type: Real
              required: true
              description: "Invoice subtotal"
            - name: tax
              type: Real
              required: true
              description: "Tax amount"
          constraints:
            - "(= total (+ subtotal tax))"
          path_mappings: []
          metadata: {}
      signature_check:
        type: string
        description: "Document must be signed"
        x-aws-idp-validation-engine: llm   # Semantic validation
```

Valid values: `"llm"` (default) or `"z3"` (case-sensitive). If the field is absent, the rule defaults to the LLM engine.

Z3 rules require:
- `x-aws-idp-rule-id` — unique identifier for the rule
- `x-aws-idp-rule-json` — the RuleJSON object (generate via the "Generate RuleJSON" button in the Config Editor)

### Z3 Engine Settings (Optional)

Add these to the `rule_validation` section to customize Z3 behavior:

```yaml
rule_validation:
  z3_timeout_ms: 5000          # Solver timeout (1–300000 ms, default 5000)
  z3_rule_translator:          # LLM for rule → SMT-LIB translation
    model: us.anthropic.claude-sonnet-4-5-20250929-v1:0
    temperature: 0
    max_tokens: 4096
    system_prompt: "..."
    task_prompt: "..."
    few_shot_examples: [...]
  z3_value_extraction:         # LLM for parameter value extraction
    model: us.anthropic.claude-haiku-4-5-20251001-v1:0
    temperature: 0
    max_tokens: 2048
    system_prompt: "..."
    task_prompt: "..."
```

If `z3_rule_translator` and `z3_value_extraction` are both omitted, the engine uses built-in default prompts from `z3/config/translator_config.yaml`.

## How It Works

```
Rule (natural language) → [LLM Translation] → RuleJSON (SMT-LIB)
                                                    ↓
Document Data → [Path Extraction or LLM Extraction] → Parameter Values
                                                    ↓
                              [Z3 Solver] → sat (Pass) / unsat (Fail) / error (Information Not Found)
```

1. **Translation**: An LLM converts the natural-language rule into a `RuleJSON` structure containing typed parameters and SMT-LIB constraints. Translation is triggered via the "Generate RuleJSON" button in the Config Editor; the result is stored inline in the config under `x-aws-idp-rule-json`.

2. **Extraction**: In the orchestration step, an LLM call extracts typed parameter values from the collected facts (gathered per-section in the prior step).

3. **Validation**: The Z3 solver checks whether the extracted values satisfy the constraints:
   - `sat` → **Pass** (rule satisfied)
   - `unsat` → **Fail** (rule violated)
   - `error` / missing parameters → **Information Not Found**

## Strict Mode (Default)

Z3 validation enforces strict mode: if the RuleJSON is missing, the rule_id is not configured, or required parameters cannot be extracted from the document, the rule returns a hard failure or "Information Not Found" — it does NOT silently fall back to LLM-based reasoning. This ensures the configured engine always runs and misconfigurations are visible.

## UI: Schema Builder Dropdown

When editing rule properties in the Schema Builder (`isRuleSchema=true`), a "Validation Engine" dropdown appears with options:
- **Semantic (LLM)** — default
- **Symbolic (Z3)**

The dropdown only writes the field to the schema when the user explicitly interacts with it. Invalid stored values auto-correct to "llm".

## Lambda Deployment Note

The `z3-solver` package (~50 MB native shared object) is included in the `rule_validation` optional extra and is loaded **lazily** — it is only imported when a Z3 rule is actually encountered at runtime. If your config has no `x-aws-idp-validation-engine: z3` rules, the package is never loaded and has zero cold-start impact. For Lambda deployments with Z3 rules, ensure the unzipped package size stays under 250 MB or use a container-based Lambda.

## Limitations

- Z3 results include `supporting_pages` collected from the extracted facts' page citations. These indicate which pages contained the evidence used for parameter extraction.
- The SMT-LIB constraint language supports: arithmetic (`+`, `-`, `*`, `/`), comparison (`=`, `<`, `>`, `<=`, `>=`), logical (`and`, `or`, `not`, `=>`, `ite`), and type coercion for Int/Real/Bool/String.
- String equality checks are exact (case-sensitive). For fuzzy matching, use the LLM engine.

## Demo

See [`notebooks/examples/dual-engine-rule-validation.ipynb`](../notebooks/examples/dual-engine-rule-validation.ipynb) for an end-to-end comparison of both engines on the same rules.
