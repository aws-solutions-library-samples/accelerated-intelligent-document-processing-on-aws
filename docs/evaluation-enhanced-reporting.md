---
title: "Enhanced Evaluation Reporting"
---

# Enhanced Evaluation Reporting

This document describes the enhanced evaluation reporting features surfaced
by the evaluation module (running on `stickler-eval==1.0.0`).

## Overview

The evaluation module surfaces stickler-eval's fine-grain field comparison
feature (introduced upstream in [GitHub Issue #48](https://github.com/awslabs/stickler/issues/48) / [PR #51](https://github.com/awslabs/stickler/pull/51)) to provide:

1. **Detailed nested object match information** alongside aggregate scores
2. **Interactive controls** to filter and explore evaluation results
3. **Field-by-field comparison details** for arrays and complex objects

## Key Features

### 1. Nested Field Comparison Details

For complex attributes (nested objects, arrays), the evaluation now captures detailed field-by-field comparison information:

```json
{
  "name": "LineItems",
  "score": 0.88,  // Aggregate score
  "matched": false,
  "field_comparison_details": [
    {
      "expected_key": "LineItems[0].Description",
      "expected_value": "Service A",
      "actual_key": "LineItems[0].Description", 
      "actual_value": "Service A",
      "match": true,
      "score": 1.0,
      "weighted_score": 2.0
    },
    {
      "expected_key": "LineItems[1].Description",
      "expected_value": "Service B",
      "actual_key": "LineItems[1].Description",
      "actual_value": "Service C",
      "match": false,
      "score": 0.75,
      "weighted_score": 1.5
    }
    // ... more comparisons
  ]
}
```

### 2. Interactive Markdown Reports

The markdown reports now include interactive HTML controls:

#### 🔍 Show Only Unmatched
Filter the attribute table to show only rows where matches failed, providing a compact view highlighting problematic fields.

```html
<button onclick="toggleUnmatchedOnly()">🔍 Show Only Unmatched</button>
```

#### ➕➖ Expand/Collapse All Details
Expand or collapse all nested field comparison details at once.

```html
<button onclick="expandAllDetails()">➕ Expand All Details</button>
<button onclick="collapseAllDetails()">➖ Collapse All Details</button>
```

#### 📋 Expandable Nested Details
Each attribute with nested comparisons has an expandable section:

```html
<details>
  <summary>🔍 View 6 Nested Field Comparisons</summary>
  <!-- Detailed comparison table -->
</details>
```

### 3. Aggregate Score Annotations

Aggregate scores for complex objects are clearly marked:

- **Visual indicator**: `<span class="aggregate-score">0.88</span>`
- **Text annotation**: `(aggregate)` appears next to the score
- **Color coding**: Blue styling distinguishes aggregate from simple field scores

## Report Structure

### JSON Report

The JSON report (`results.json`) includes:

```json
{
  "document_id": "doc-123",
  "overall_metrics": { ... },
  "section_results": [
    {
      "section_id": "section-001",
      "document_class": "Invoice",
      "metrics": { ... },
      "attributes": [
        {
          "name": "AttributeName",
          "expected": "...",
          "actual": "...",
          "matched": true,
          "score": 0.95,
          "field_comparison_details": [  // NEW in v0.1.4
            { /* detailed comparison */ }
          ]
        }
      ]
    }
  ]
}
```

### Markdown Report

The markdown report (`report.md`) includes:

1. **Interactive Controls** - Filter and navigation buttons
2. **Summary Section** - High-level metrics with visual indicators
3. **Section Details** - Per-section metrics and attributes
4. **Attribute Table** - Enhanced with:
   - Row classes for filtering (`matched-row`, `unmatched-row`)
   - Aggregate score annotations
   - Expandable nested details for complex fields
5. **Evaluation Methods** - Documentation of comparison methods

## Usage Example

```python
from idp_common.evaluation.service import EvaluationService

# Initialize service
eval_service = EvaluationService(region="us-east-1", config=config)

# Evaluate document (field_comparisons automatically enabled)
result_doc = eval_service.evaluate_document(
    actual_document=actual_doc,
    expected_document=expected_doc,
    store_results=True  # Generates both JSON and Markdown
)

# Access detailed comparisons programmatically
for section in result_doc.evaluation_result.section_results:
    for attr in section.attributes:
        if attr.field_comparison_details:
            print(f"Attribute: {attr.name}")
            print(f"Aggregate Score: {attr.score}")
            print(f"Nested Comparisons: {len(attr.field_comparison_details)}")
            
            for detail in attr.field_comparison_details:
                if not detail['match']:
                    print(f"  Mismatch: {detail['expected_key']}")
                    print(f"    Expected: {detail['expected_value']}")
                    print(f"    Actual: {detail['actual_value']}")
                    print(f"    Score: {detail['score']}")
```

## Viewing Interactive Reports

### GitHub
GitHub's markdown renderer supports HTML, so the interactive controls will work when viewing the report in:
- Pull requests
- Issue comments
- Repository files

### VS Code
Install a markdown extension that supports HTML:
- **Markdown Preview Enhanced** (recommended)
- **Markdown All in One**

### Web Browser
Open the `.md` file directly in a browser:
```bash
open test_evaluation_report.md
```

### Jupyter Notebooks
Use `IPython.display.Markdown`:
```python
from IPython.display import Markdown, display

with open('evaluation/report.md', 'r') as f:
    display(Markdown(f.read()))
```

## Configuration

No additional configuration required! The enhancement automatically activates when using stickler-eval v0.1.4+.

The feature is enabled in `lib/idp_common_pkg/idp_common/evaluation/service.py`:

```python
# Compare using Stickler with field_comparisons enabled
stickler_result = expected_instance.compare_with(
    actual_instance,
    document_field_comparisons=True,  # Enables detailed comparison
)
```

## Benefits

### 1. Better Debugging
- Quickly identify which specific nested fields are causing mismatches
- See exact values that differ within complex objects
- Understand Hungarian matching results for arrays

### 2. Compact Problem View
- Filter to show only unmatched rows
- Focus attention on fields requiring investigation
- Reduce cognitive load when reviewing large reports

### 3. Complete Context
- Aggregate scores provide high-level overview
- Nested details provide granular diagnostics
- Both perspectives available in single report

### 4. Production Ready
- JSON structure fully captures all comparison data
- Can be consumed by analytics tools
- Markdown provides human-readable interface

## Technical Details

### Data Model Changes

**AttributeEvaluationResult** now includes:
```python
@dataclass
class AttributeEvaluationResult:
    # ... existing fields ...
    field_comparison_details: Optional[List[Dict[str, Any]]] = None
```

### Field Comparison Structure

Each comparison in `field_comparison_details`:
```python
{
    "expected_key": "path.to.field",      # Dot/bracket notation
    "expected_value": "actual value",
    "actual_key": "path.to.field",
    "actual_value": "actual value",
    "match": true,                        # Boolean match result
    "score": 0.95,                        # Similarity score (0.0-1.0)
    "weighted_score": 1.9,                # score * field_weight
    "reason": "explanation"               # Human-readable reason
}
```

### Grouping Logic

Field comparisons are grouped by root field name:
- `LineItems[0].Description` → grouped under `LineItems`
- `Address.City` → grouped under `Address`
- Simple fields have no grouping (single comparison or none)

## Backward Compatibility

The enhancement is fully backward compatible:

- ✅ Existing API unchanged
- ✅ JSON reports remain consumable by old code (new field is optional)
- ✅ Markdown reports viewable in any viewer (controls degrade gracefully)
- ✅ No configuration changes required

## Examples

Working examples of enhanced-reporting shapes live in the golden-fixture
regression suite at
`lib/idp_common_pkg/tests/unit/evaluation/test_golden_fixture_stickler.py`
and its fixtures under `.../fixtures/` (in particular
`nested_invoice.input.json`, which exercises arrays with nested field
comparison details). Run:

```bash
cd lib/idp_common_pkg && python -m pytest tests/unit/evaluation/test_golden_fixture_stickler.py -v
```

## Future Enhancements

Potential future improvements:
- Export to CSV with nested details flattened
- Comparison history tracking across runs
- Threshold recommendations based on field mismatch patterns
- Visual diff viewer for nested structures