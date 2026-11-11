# Evaluation Report

<!-- eval-report-v2 -->

## Summary

**Document Extraction:**
- **Match Rate**: 🟠 1/2 attributes matched [██████████░░░░░░░░░░] 50%
- **Precision**: 0.50 | **Recall**: 0.50 | **F1 Score**: 🟠 0.50
- **Weighted Overall Score**: 🟠 0.5000 (Stickler's field-weighted aggregate)

## Overall Metrics

### Document Extraction Metrics
| Metric | Value | Rating |
| ------ | :----: | :----: |
| precision | 0.5000 | 🟠 Fair |
| recall | 0.5000 | 🟠 Fair |
| f1_score | 0.5000 | 🟠 Fair |
| accuracy | 0.5000 | 🟠 Fair |
| false_alarm_rate | 0.0000 | 🟢 Excellent |
| false_discovery_rate | 0.5000 | 🟠 Fair |
| weighted_overall_score | 0.5000 | 🟠 Fair |


## Extraction Attribute Evaluation

### Section: 1 (Payslip)
#### Metrics
| Metric | Value | Rating |
| ------ | :----: | :----: |
| precision | 0.5000 | 🟠 Fair |
| recall | 0.5000 | 🟠 Fair |
| f1_score | 0.5000 | 🟠 Fair |
| accuracy | 0.5000 | 🟠 Fair |
| false_alarm_rate | 0.0000 | 🟢 Excellent |
| false_discovery_rate | 0.5000 | 🟠 Fair |
| weighted_overall_score | 0.5000 | 🟠 Fair |


#### Attributes
<table style="width: 100%; border-collapse: collapse;">
<thead><tr style="background: #f0f0f0;"><th style="padding: 8px; border: 1px solid #ddd; text-align: center;">Status</th><th style="padding: 8px; border: 1px solid #ddd;">Attribute</th><th style="padding: 8px; border: 1px solid #ddd;">Expected</th><th style="padding: 8px; border: 1px solid #ddd;">Actual</th><th style="padding: 8px; border: 1px solid #ddd; text-align: center;">Confidence</th><th style="padding: 8px; border: 1px solid #ddd; text-align: center;">Conf. Threshold</th><th style="padding: 8px; border: 1px solid #ddd; text-align: center;">Score</th><th style="padding: 8px; border: 1px solid #ddd; text-align: center;">Weight</th><th style="padding: 8px; border: 1px solid #ddd;">Method</th><th style="padding: 8px; border: 1px solid #ddd;">Reason</th></tr></thead><tbody>
<tr class="matched-row">
<td style="padding: 8px; border: 1px solid #ddd; text-align: center;">✅</td>
<td style="padding: 8px; border: 1px solid #ddd;"><strong>EmployeeName</strong></td>
<td style="padding: 8px; border: 1px solid #ddd;">Jane Doe</td>
<td style="padding: 8px; border: 1px solid #ddd;">Jane Doe</td>
<td style="padding: 8px; border: 1px solid #ddd; text-align: center;">0.97</td>
<td style="padding: 8px; border: 1px solid #ddd; text-align: center;">0.90</td>
<td style="padding: 8px; border: 1px solid #ddd; text-align: center;">1.00</td>
<td style="padding: 8px; border: 1px solid #ddd; text-align: center;">1.00</td>
<td style="padding: 8px; border: 1px solid #ddd;">EXACT</td>
<td style="padding: 8px; border: 1px solid #ddd;">exact</td>
</tr>
<tr class="unmatched-row">
<td style="padding: 8px; border: 1px solid #ddd; text-align: center;">❌</td>
<td style="padding: 8px; border: 1px solid #ddd;"><strong>NetPay</strong></td>
<td style="padding: 8px; border: 1px solid #ddd;">1,200.00</td>
<td style="padding: 8px; border: 1px solid #ddd;">1,000.00</td>
<td style="padding: 8px; border: 1px solid #ddd; text-align: center;">N/A</td>
<td style="padding: 8px; border: 1px solid #ddd; text-align: center;">N/A</td>
<td style="padding: 8px; border: 1px solid #ddd; text-align: center;">0.00</td>
<td style="padding: 8px; border: 1px solid #ddd; text-align: center;">1.00</td>
<td style="padding: 8px; border: 1px solid #ddd;">NUMERIC_EXACT</td>
<td style="padding: 8px; border: 1px solid #ddd;">value differs</td>
</tr>
</tbody></table>

Execution time: 0.00 seconds

## Evaluation Methods Used

This evaluation uses Stickler-based comparison with the following methods:

### Field-Level Comparison Methods

1. **EXACT** - Exact string match (case-sensitive)
   - Use for: IDs, codes, exact text requiring character-for-character match

2. **NUMERIC_EXACT** - Numeric comparison with configurable tolerance
   - Tolerance specified via `x-aws-idp-evaluation-threshold`
   - Use for: Monetary amounts, percentages, numeric values

3. **FUZZY** - Fuzzy string matching using similarity metrics
   - Threshold specified via `x-aws-idp-evaluation-threshold` (0.0-1.0)
   - Use for: Names, addresses, text with minor variations

4. **LEVENSHTEIN** - Levenshtein distance-based string comparison
   - Configurable threshold for acceptable edit distance
   - Use for: Similar to FUZZY but using specific edit distance algorithm

5. **SEMANTIC** - Semantic similarity using embedding models
   - Threshold specified via `x-aws-idp-evaluation-threshold`
   - Use for: Text where meaning matters more than exact wording

6. **DATE** - Format-insensitive date / date-range comparison
   - Compares dates by resolved value, not surface form (e.g. `2024-01-05` == `January 5, 2024`)
   - Optional per-field config via `x-aws-idp-evaluation-method-config`: `dayfirst`, `range_mode`, `tolerance`
   - Use for: Dates in mixed formats, date ranges, birthdates

7. **LLM** - Advanced semantic evaluation using **AWS Bedrock LLMs**
   - Configured via `evaluation.llm_method` section:
     - `model`: Bedrock model ID (e.g., Claude Haiku, Sonnet)
     - `task_prompt`: Custom prompt template with context placeholders
     - `system_prompt`: System instructions for the LLM
     - `temperature`, `top_k`, `top_p`, `max_tokens`: LLM generation parameters
   - Provides contextual evaluation with reasoning
   - Use for: Complex nested objects, structured data, semantic understanding

### Array-Level Matching

8. **HUNGARIAN** - Bipartite graph matching for arrays of structured objects
   - Finds optimal 1:1 mapping between expected and actual lists
   - Each matched item pair is then compared using field-level methods
   - Configured with `x-aws-idp-evaluation-method: "HUNGARIAN"` on array properties

9. **LLM for Arrays** - Semantic evaluation of entire list structures
   - Evaluates whether lists semantically match as a whole
   - Configured with `x-aws-idp-evaluation-method: "LLM"` on array properties

### Field Weighting

Fields can be assigned importance weights using `x-aws-idp-evaluation-weight` in the schema:
- **Default weight**: 1.0 (standard importance)
- **Higher weights** (e.g., 2.0, 3.0): Critical fields that matter more for overall quality
- **Lower weights** (e.g., 0.5): Less important optional fields
- **Impact**: Used in Stickler's weighted_overall_score calculation
- **Display**: Shown in the Weight column of attribute results

**Example field weights:**
- Account Number (weight: 3.0) - Critical identifier
- Phone Number (weight: 1.0) - Standard field
- Notes (weight: 0.5) - Optional supplementary info

The **Weighted Overall Score** aggregates individual field scores weighted by importance:
- **Formula**: `Σ(weight_i × score_i) / Σ(weight_i)`
- **Section-level**: Weighted score for that specific section
- **Document-level**: Average of all section weighted scores

---

**Note**: Each attribute specifies its evaluation method via `x-aws-idp-evaluation-method` in the schema.