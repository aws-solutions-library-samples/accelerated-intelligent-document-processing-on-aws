---
title: "Missing pages vs. blank fields"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Distinguishing MISSING pages from BLANK fields

Many forms processed by GenAI IDP are *sparsely populated* — entire sub-sections, worksheets, or supplemental pages may be legitimately omitted by the submitter. With the default extraction behavior, the system has no way to tell whether a field is empty because:

- the page was submitted but the field was unfilled (**BLANK**), or
- the page wasn't submitted at all (**MISSING**)

This guide explains the optional opt-in feature that lets you distinguish these two cases.

## Why prompt-engineering doesn't solve this

A natural first instinct is to instruct the LLM in `extraction.task_prompt` to "omit fields when the page is missing." This **does not work reliably** because the LLM sees only the OCR text it was given — it has no signal about which pages *should* exist for the document class. The fix has to live in the system, not the prompt.

## Configuration

The feature is opt-in and requires three coordinated config changes:

### 1. Declare page sub-types on the class

Use `x-aws-idp-page-types` on the document class to enumerate the named page sub-types it can include. Each entry has a `name`, optional `description`, and an `x-aws-idp-document-page-content-regex` used to detect the page type from per-page OCR text:

```yaml
classes:
  - $id: BankStatement
    x-aws-idp-document-type: BankStatement
    x-aws-idp-page-types:
      - name: AccountSummary
        description: First page with account holder + summary
        x-aws-idp-document-page-content-regex: "(?i)account summary|statement period"
      - name: TransactionsWorksheet
        description: Transactions ledger
        x-aws-idp-document-page-content-regex: "(?i)transactions? (history|detail)"
      - name: InternationalTransfers
        description: International transfers supplement (often omitted)
        x-aws-idp-document-page-content-regex: "(?i)international (transfers|wires)"
```

### 2. Annotate properties with their source page-types

Use `x-aws-idp-source-page-types` on each property to declare which page sub-types contain its source data. Properties without this annotation are never treated as MISSING:

```yaml
    properties:
      AccountHolderAddress:
        type: object
        x-aws-idp-source-page-types: ["AccountSummary"]
      Transactions:
        type: array
        x-aws-idp-source-page-types: ["TransactionsWorksheet"]
      InternationalTransfers:
        type: array
        x-aws-idp-source-page-types: ["InternationalTransfers"]
```

### 3. Turn on the missing-field handler

```yaml
extraction:
  missing_field_handling:
    enabled: true
    representation: omit  # or "null_with_metadata"
```

- `omit` — removes the key from `inference_result` entirely. Best when downstream systems treat missing keys as null and benefit from a smaller payload.
- `null_with_metadata` — keeps the key as `null` and adds the field name to a sibling `missing_fields` array in the output JSON. Best when downstream systems require a stable, predictable key set.

## Editing in the Web UI

The Document Schema editor in **View / Edit Configuration → Document Schema** has first-class widgets for both extensions:

- The **class inspector** (right pane when a class is selected) has a **"Page Types"** section with an "Add Page Type" button that opens an expandable editor for each entry's name, description, and regex.
- The **property inspector** (right pane when a property is selected) shows a **"Source Page Types"** multi-select under a "Missing-Page Handling" header. The dropdown is populated from the page-type names declared on the parent class, so a property edited here can only name a page-type that exists.

When no page types are declared on the parent class, the property-level widget is hidden so the inspector stays uncluttered.

> ⚠️ **Only the dropdown constrains the names — a hand-edited config is not
> checked, and the failure is silent data loss.** A name in
> `x-aws-idp-source-page-types` that does not match any `name` under the class's
> `x-aws-idp-page-types` is accepted by the configuration models and reported as
> valid by `idp-cli config-validate`. At run time it can never be found among the
> detected page types, so the property is treated as MISSING on **every**
> document — omitted from `inference_result`, or set to `null`, even when its
> pages were present and the model extracted the data. The same thing happens
> when the class-level `name` is misspelled, or when its
> `x-aws-idp-document-page-content-regex` never matches.
>
> So after editing these extensions by hand, process one document that definitely
> contains every page type and confirm that no property you expected is reported
> as missing. The section's `result.json` carries both halves of the comparison:
> `page_type_resolution.present_page_types` is what was actually detected, and
> each entry in `missing_fields_report` names the `expected_page_types` the
> omitted property was looking for. A name that appears in the second list and
> not the first is the mismatch. The same present/missing sets are logged by the
> Extraction Lambda. Editing through the Document Schema editor avoids the whole
> class of mistake.

## What the LLM sees

Once the resolver runs, the per-page text passed to the extraction prompt is annotated with the resolved page type. Compare:

```
--- PAGE 1 ---
Account Holder: ...

--- PAGE 2 ---
Date  | Description ...
```

with the annotated form:

```
--- PAGE 1 [AccountSummary] ---
Account Holder: ...

--- PAGE 2 [TransactionsWorksheet] ---
Date  | Description ...
```

The annotation is a soft hint — it can help the LLM scope its attention to the correct page when extracting. The actual *enforcement* of MISSING semantics happens after the LLM responds, in the post-processing pass.

## What appears in the output JSON

When the feature is enabled and the class declares page types, the section's `result.json` gains two new sibling keys to `inference_result`:

```json
{
  "document_class": {"type": "BankStatement"},
  "inference_result": {
    "AccountHolderAddress": { ... }
  },
  "page_type_resolution": {
    "declared": true,
    "present_page_types": ["AccountSummary"],
    "missing_page_types": ["TransactionsWorksheet", "InternationalTransfers"],
    "page_id_to_page_type": {"1": "AccountSummary"}
  },
  "missing_fields_report": [
    {
      "field": "Transactions",
      "reason": "page types not present",
      "expected_page_types": ["TransactionsWorksheet"]
    },
    {
      "field": "InternationalTransfers",
      "reason": "page types not present",
      "expected_page_types": ["InternationalTransfers"]
    }
  ]
}
```

When `representation: null_with_metadata` is set, an additional `missing_fields` array lists the affected field names for downstream consumers that prefer a flat list.

## Edge cases and tuning

- **Partial presence is BLANK.** If a property declares multiple source page-types and *any* of them are detected, the field is treated as BLANK (left as-is), not MISSING. The intent is conservative — only mark MISSING when *no* declared source page is present.
- **First-match-wins per page.** Page-type entries are evaluated in declaration order. Place more specific patterns before more general ones.
- **Malformed regex is skipped.** A bad regex on one page-type entry is logged as a warning and ignored; other entries continue to work.
- **Properties without `x-aws-idp-source-page-types`** are never affected by this feature, regardless of whether the feature is enabled.
- **Nested properties.** v1 supports top-level properties only. To mark nested fields as missing, declare the annotation on the containing object/array property at the top level.
- **BDA mode.** This feature is Pipeline-mode-only. BDA owns extraction end-to-end and would need separate design work to support page-level absence semantics.
- **Detection cost.** Detection is a deterministic regex pass over per-page OCR text — no extra LLM calls, no measurable runtime overhead.

## Backwards compatibility

The feature is fully additive. With `extraction.missing_field_handling.enabled: false` (the default) or with no `x-aws-idp-page-types` declared on the class, behavior is byte-for-byte identical to earlier releases.

## See also

- [`notebooks/usecase-specific-examples/multi-page-bank-statement/step3_extraction_with_missing_pages.ipynb`](../notebooks/usecase-specific-examples/multi-page-bank-statement/step3_extraction_with_missing_pages.ipynb) — runnable end-to-end demo. Loads the bank-statement sample's `step2_classification.ipynb` output, augments the schema with `x-aws-idp-page-types` extensions, and runs extraction twice (full document vs. transactions page omitted) to make the BLANK vs MISSING distinction visible in the resulting JSON.
- [`config_library/unified/bank-statement-sample/config.yaml`](../config_library/unified/bank-statement-sample/config.yaml) — annotated commented-out stanza you can uncomment in a deployed stack.
