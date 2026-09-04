---
title: "JSON Schema Migration Guide"
---

# JSON Schema Migration Guide

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

## Overview

Starting with version 0.3.21, the GenAI IDP solution uses **JSON Schema** format for document class definitions instead of the legacy custom format. This provides:

- ✅ **Industry standard** format with broad tooling support
- ✅ **Better validation** using standard JSON Schema validators
- ✅ **Improved documentation** through self-describing schemas
- ✅ **Backward compatibility** - automatic migration of legacy configurations


https://github.com/user-attachments/assets/ee817858-8285-4087-9b25-2c7c5bea65df


## Format Comparison

### Legacy Format (Pre-0.3.21)

```yaml
classes:
  - name: Payslip
    description: An employee wage statement
    attributes:
      - name: YTDNetPay
        description: Year-to-date net pay amount
        attributeType: simple
        evaluation_method: NUMERIC_EXACT
      
      - name: CompanyAddress
        description: Complete business address
        attributeType: group
        evaluation_method: LLM
        groupAttributes:
          - name: Street
            description: Street address
          - name: City
            description: City name
      
      - name: Deductions
        description: List of deductions
        attributeType: list
        listItemTemplate:
          itemDescription: A single deduction
          itemAttributes:
            - name: Type
              description: Deduction type
            - name: Amount
              description: Deduction amount
```

### JSON Schema Format (0.3.21+)

```yaml
classes:
  - $schema: "https://json-schema.org/draft/2020-12/schema"
    $id: Payslip
    x-aws-idp-document-type: Payslip
    type: object
    description: An employee wage statement
    properties:
      YTDNetPay:
        type: string
        description: Year-to-date net pay amount
        x-aws-idp-evaluation-method: NUMERIC_EXACT
      
      CompanyAddress:
        type: object
        description: Complete business address
        x-aws-idp-evaluation-method: LLM
        properties:
          Street:
            type: string
            description: Street address
          City:
            type: string
            description: City name
      
      Deductions:
        type: array
        description: List of deductions
        x-aws-idp-list-item-description: A single deduction
        items:
          type: object
          properties:
            Type:
              type: string
              description: Deduction type
            Amount:
              type: string
              description: Deduction amount
```

## Migration Mapping

### Field Name Mapping

| Legacy Field | JSON Schema Field | Notes |
|-------------|-------------------|-------|
| `name` | `$id` and `x-aws-idp-document-type` | Document class name |
| `description` | `description` | Same field name |
| `attributes` | `properties` | List → Object |
| `attributeType: simple` | `type: string` | Simple values are strings |
| `attributeType: group` | `type: object` with `properties` | Nested object |
| `attributeType: list` | `type: array` with `items` | Array of items |
| `groupAttributes` | `properties` (nested) | Object properties |
| `listItemTemplate` | `items` | Array item schema |
| `itemAttributes` | `items.properties` | Properties of array items |
| `itemDescription` | `x-aws-idp-list-item-description` | AWS IDP extension |
| `evaluation_method` | `x-aws-idp-evaluation-method` | AWS IDP extension |
| `confidence_threshold` | `x-aws-idp-confidence-threshold` | AWS IDP extension |
| `prompt_override` | `x-aws-idp-prompt-override` | AWS IDP extension |

### Type Mapping

| Legacy Type | JSON Schema Type |
|------------|------------------|
| `attributeType: simple` | `type: string` |
| `attributeType: group` | `type: object` |
| `attributeType: list` | `type: array` |

## Automatic Migration

The solution automatically migrates legacy configurations to JSON Schema format:

### When Migration Happens

1. **First read after upgrade** - When configuration is loaded from DynamoDB
2. **Automatic persistence** - Migrated format is saved back to DynamoDB
3. **One-time process** - Subsequent reads use JSON Schema format directly

### Migration Behavior

- ✅ **Non-destructive** - Legacy data is preserved during migration
- ✅ **Idempotent** - Won't re-migrate already migrated data
- ✅ **Transparent** - Happens automatically without user intervention
- ✅ **Logged** - Migration activity logged to CloudWatch

### Migration Logs

Check Lambda logs to verify migration:

```bash
aws logs tail /aws/lambda/<STACK>-ConfigurationResolverFunction-<ID> \
  --region <REGION> --follow
```

Look for:
```
Migrating 6 legacy classes to JSON Schema format
Successfully migrated classes to JSON Schema format
```

## AWS IDP Extensions

JSON Schema is extended with custom AWS IDP fields:

### Document-Level Extensions

- `x-aws-idp-document-type` - Marks a schema as a document type (value is the document class name)
- `x-aws-idp-multi-instance` - `true` replaces the class's *effective* schema with a
  list of that class (`{"instances": [ … ]}`), so a section holding several
  documents of the class extracts every one of them. Mutually exclusive with
  `x-aws-idp-instance-array`. ⚠️ Changes the shape of `inference_result`, so this
  class's evaluation baselines must be migrated — see
  [extraction-and-confidence.md](extraction-and-confidence.md).
- `x-aws-idp-instance-array` - For a class whose schema is *already* a packet of
  records: names the top-level array-of-objects property holding one record per
  document, so the section reports that array's length as its instance count. No
  schema change and no downstream impact.

### Attribute-Level Extensions

- `x-aws-idp-evaluation-method` - Evaluation method for attribute comparison
  - Valid values: `EXACT`, `NUMERIC_EXACT`, `FUZZY`, `LEVENSHTEIN`, `SEMANTIC`, `DATE`, `LLM`, `HUNGARIAN`
- `x-aws-idp-evaluation-method-config` - Optional comparator config passed through to the underlying comparator (used by `DATE`: `dayfirst`, `tolerance`, `range_mode`)
- `x-aws-idp-confidence-threshold` - Confidence threshold (0.0 to 1.0)
- `x-aws-idp-prompt-override` - Custom prompt for attribute extraction

### List-Specific Extensions

- `x-aws-idp-list-item-description` - Description for array items
- `x-aws-idp-original-name` - Preserved original attribute name from legacy format

### Few-Shot Example Extensions

- `x-aws-idp-class-prompt` - Classification prompt for example
- `x-aws-idp-attributes-prompt` - Extraction prompt for example  
- `x-aws-idp-image-path` - Path to example image

## Creating New Configurations

### Using the Web UI (Recommended)

The web UI provides two ways to create/edit document schemas:

1. **Schema Builder** - Visual editor with drag-and-drop interface
   - Navigate to Configuration → Document Schema tab
   - Click "Schema Builder" view
   - Click "Add Class" to choose between:
     - **Custom Class** — define your own class with custom fields
     - **Standard Class** — import from 35+ pre-built document types (Invoice, Receipt, W-2, Bank Statement, Payslip, Driver License, Passport, tax forms, insurance cards, certificates, and more) derived from AWS BDA standard blueprints. Imported classes are fully editable.
   - Add/edit document types and properties visually. A newly-created class shows an **Add first attribute** button; the **Add Attribute** dialog has an **Add another** option that saves the field and immediately starts a new one, so you can add several attributes without reopening the dialog.
   - **Show Preview** opens a **Diagram** tab (a visual entity-relationship map of the classes and their `$ref`/array references), alongside **JSON Schema** and **Statistics** tabs.
   - **Export** offers **Export all** (every document type) or **Export "&lt;class&gt;"** (the selected document type plus only the shared classes it references) as downloaded JSON. See the [Schema Builder Guide](web-ui.md#document-schema-builder).

2. **JSON View** - Direct JSON editing with validation
   - Navigate to Configuration → JSON View
   - Edit the `classes` array directly
   - Validation happens in real-time

### Manual YAML Configuration

When creating configurations manually, use JSON Schema format:

```yaml
classes:
  - $schema: "https://json-schema.org/draft/2020-12/schema"
    $id: MyDocument
    x-aws-idp-document-type: MyDocument
    type: object
    description: Document description here
    properties:
      FieldName:
        type: string
        description: Field description
        x-aws-idp-evaluation-method: EXACT
```

### Configuration Templates

Find JSON Schema templates in:
- `config_library/unified/` - Pattern 2 examples (Bedrock)

## Validation Methods

The solution supports these evaluation methods:

### Standard Methods

- `EXACT` - Exact string match
- `NUMERIC_EXACT` - Exact numeric match (handles different number formats)
- `FUZZY` - Fuzzy string matching (Levenshtein distance)
- `SEMANTIC` - Semantic similarity using embeddings

### Advanced Methods

- `LLM` - LLM-based evaluation for complex/contextual comparisons
  - Useful for address blocks, multi-field groups
  - Higher cost but more flexible
  - Requires evaluation configuration with LLM model

## Best Practices

### 1. Use Descriptive Field Names

**Good:**
```yaml
properties:
  InvoiceDate:
    type: string
    description: Date when invoice was issued
```

**Avoid:**
```yaml
properties:
  Date:  # Too generic
    type: string
```

### 2. Leverage Standard JSON Schema Features

```yaml
properties:
  TotalAmount:
    type: string  # Store as string for exact extraction
    description: Total invoice amount including taxes
    x-aws-idp-evaluation-method: NUMERIC_EXACT
    
  Email:
    type: string
    format: email  # Standard JSON Schema format
    description: Customer email address
    
  Status:
    type: string
    enum: [PAID, PENDING, OVERDUE]  # Constrain values
    description: Payment status
```

### 3. Structure Complex Objects Properly

For nested data like addresses:

```yaml
properties:
  ShippingAddress:
    type: object
    description: Complete shipping address
    x-aws-idp-evaluation-method: LLM  # Use LLM for complex structures
    properties:
      Street:
        type: string
      City:
        type: string
      State:
        type: string
      ZipCode:
        type: string
```

### 4. Use Arrays for Repeating Data

For line items, deductions, etc.:

```yaml
properties:
  LineItems:
    type: array
    description: Invoice line items
    x-aws-idp-list-item-description: A single line item
    items:
      type: object
      properties:
        Description:
          type: string
        Quantity:
          type: string
        UnitPrice:
          type: string
        Total:
          type: string
          x-aws-idp-evaluation-method: NUMERIC_EXACT
```

## Troubleshooting

### UI Shows Legacy Format

**Symptoms:**
- UI displays `attributes` array instead of `properties` object
- Configuration tab is blank

**Solution:**
1. Refresh browser cache (hard refresh: Ctrl+Shift+R or Cmd+Shift+R)
2. Check Lambda logs for migration errors
3. Verify Lambda has latest code with migration support

### Validation Errors for LLM Method

**Symptoms:**
- Error: "Invalid evaluation_method 'LLM'"

**Solution:**
- Ensure using version 0.3.21 or later
- Check `lib/idp_common_pkg/idp_common/config/schema_constants.py` includes `EVALUATION_METHOD_LLM`

### Migration Not Running

**Symptoms:**
- Legacy format still in DynamoDB after upgrade
- No migration logs in CloudWatch

**Solution:**
1. Verify Lambda has `requirements.txt` with `./lib/idp_common_pkg`
2. Check Lambda includes `ConfigurationManager` code
3. Trigger migration manually via UI configuration load

## Additional Resources

- [Configuration Best Practices](idp-configuration-best-practices.md)
- [Web UI Documentation](web-ui.md)
- [JSON Schema Specification](https://json-schema.org/draft/2020-12/schema)
- [Schema Builder Guide](web-ui.md#document-schema-builder)
