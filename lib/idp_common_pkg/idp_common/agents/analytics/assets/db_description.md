# Athena Table Information

## Overview 
The solution creates several predefined tables in the Glue Data Catalog:

1. Document Evaluations Table (document_evaluations)
   * Contains document-level evaluation metrics
   * Columns include: document_id, input_key, evaluation_date, accuracy, precision, recall, f1_score, false_alarm_rate, false_discovery_rate, execution_time, config_version
   * Partitioned by date (YYYY-MM-DD format)

2. Section Evaluations Table (section_evaluations)
   * Contains section-level evaluation metrics
   * Columns include: document_id, section_id, section_type, accuracy, precision, recall, f1_score, false_alarm_rate, false_discovery_rate, evaluation_date, config_version
   * Partitioned by date (YYYY-MM-DD format)

3. Attribute Evaluations Table (attribute_evaluations)
   * Contains attribute-level evaluation metrics
   * Columns include: document_id, section_id, section_type, attribute_name, expected, actual, matched, score, reason, evaluation_method, confidence, confidence_threshold, evaluation_date, config_version
   * Partitioned by date (YYYY-MM-DD format)

4. Metering Table (metering)
   * Captures detailed usage metrics for document processing operations
   * Columns include: document_id, context, service_api, unit, value, number_of_pages, timestamp, config_version
   * Partitioned by date (YYYY-MM-DD format)

5. Rule Validation Summary Table (rule_validation_summary)
   * Contains document-level rule validation results
   * Columns include: document_id, input_key, validation_date, overall_status, total_policy_types, total_rules, pass_count, fail_count, information_not_found_count
   * Partitioned by date (YYYY-MM-DD format)

6. Rule Validation Details Table (rule_validation_details)
   * Contains individual rule validation results
   * Columns include: document_id, policy_type, rule, recommendation, reasoning, supporting_pages, validation_date
   * Partitioned by date (YYYY-MM-DD format)

### Dynamic Document Section Tables

In addition to the predefined tables, the solution also creates dynamic tables for document sections:

* Tables are automatically created by an AWS Glue Crawler based on the section classification
* Each section type gets its own table (e.g., document_sections_invoice, document_sections_receipt)
* Common columns include: section_id, document_id, section_classification, section_confidence, timestamp, config_version
* Additional columns are dynamically inferred from the JSON extraction results
* Tables are partitioned by date (YYYY-MM-DD format)


## Evaluation Tables

The evaluation tables store metrics and results from comparing extracted document data against baseline (ground truth) data. These tables provide insights into the accuracy and performance of the document processing system.

### Document Evaluations

The `document_evaluations` table contains document-level evaluation metrics:

| Column | Type | Description |
|--------|------|-------------|
| document_id | string | Unique identifier for the document |
| input_key | string | S3 key of the input document |
| evaluation_date | timestamp | When the evaluation was performed |
| accuracy | double | Overall accuracy score (0-1) |
| precision | double | Precision score (0-1) |
| recall | double | Recall score (0-1) |
| f1_score | double | F1 score (0-1) |
| false_alarm_rate | double | False alarm rate (0-1) |
| false_discovery_rate | double | False discovery rate (0-1) |
| execution_time | double | Time taken to evaluate (seconds) |

This table is partitioned by date (YYYY-MM-DD format).

### Section Evaluations

The `section_evaluations` table contains section-level evaluation metrics:

| Column | Type | Description |
|--------|------|-------------|
| document_id | string | Unique identifier for the document |
| section_id | string | Identifier for the section |
| section_type | string | Type/class of the section |
| accuracy | double | Section accuracy score (0-1) |
| precision | double | Section precision score (0-1) |
| recall | double | Section recall score (0-1) |
| f1_score | double | Section F1 score (0-1) |
| false_alarm_rate | double | Section false alarm rate (0-1) |
| false_discovery_rate | double | Section false discovery rate (0-1) |
| evaluation_date | timestamp | When the evaluation was performed |

This table is partitioned by date (YYYY-MM-DD format).

### Attribute Evaluations

The `attribute_evaluations` table contains attribute-level evaluation metrics:

| Column | Type | Description |
|--------|------|-------------|
| document_id | string | Unique identifier for the document |
| section_id | string | Identifier for the section |
| section_type | string | Type/class of the section |
| attribute_name | string | Name of the attribute |
| expected | string | Expected (ground truth) value |
| actual | string | Actual extracted value |
| matched | boolean | Whether the values matched |
| score | double | Match score (0-1) |
| reason | string | Explanation for the match result |
| evaluation_method | string | Method used for comparison |
| confidence | string | Confidence score from extraction |
| confidence_threshold | string | Confidence threshold used |
| evaluation_date | timestamp | When the evaluation was performed |

This table is partitioned by date (YYYY-MM-DD format).

## Metering Table

The `metering` table captures detailed usage metrics for each document processing operation:

| Column | Type | Description |
|--------|------|-------------|
| document_id | string | Unique identifier for the document |
| context | string | Processing context (OCR, Classification, Extraction, etc.) |
| service_api | string | Specific API or model used (e.g., textract/analyze_document, bedrock/claude-3) |
| unit | string | Unit of measurement (pages, inputTokens, outputTokens, etc.) |
| value | double | Quantity of the unit consumed |
| number_of_pages | int | Number of pages in the document |
| timestamp | timestamp | When the operation was performed |

This table is partitioned by date (YYYY-MM-DD format).

The metering table is particularly valuable for:
- Cost analysis and allocation
- Usage pattern identification
- Resource optimization
- Performance benchmarking across different document types and sizes

## Document Sections Tables

The document sections tables store the actual extracted data from document sections in a structured format suitable for analytics. These tables are automatically discovered by AWS Glue Crawler and are organized by section type (classification).

### Dynamic Section Tables

Document sections are stored in dynamically created tables based on the section classification. Each section type gets its own table (e.g., `document_sections_invoice`, `document_sections_receipt`, `document_sections_bank_statement`, etc.) with the following characteristics:

**Common Metadata Columns:**
| Column | Type | Description |
|--------|------|-------------|
| section_id | string | Unique identifier for the section |
| document_id | string | Unique identifier for the document |
| section_classification | string | Type/class of the section |
| section_confidence | double | Confidence in the section's classification. NULL when not scored (the default); was a hardcoded 1.0 before v0.7 |
| timestamp | timestamp | When the document was processed |

**Dynamic Data Columns:**
The remaining columns are dynamically inferred from the JSON extraction results and vary by section type. Common patterns include:
- Nested JSON objects are flattened using dot notation (e.g., `customer.name`, `customer.address.street`)
- Arrays are converted to JSON strings
- Primitive values (strings, numbers, booleans) are preserved as their native types

**Partitioning:**
Each section type table is partitioned by date (YYYY-MM-DD format) for efficient querying.

## Sample Athena SQL Queries

Here are some example queries to get you started:

**Overall accuracy by document type:**
```sql
SELECT 
  section_type, 
  AVG(accuracy) as avg_accuracy, 
  COUNT(*) as document_count
FROM 
  section_evaluations
GROUP BY 
  section_type
ORDER BY 
  avg_accuracy DESC;
```

**Token usage by model:**
```sql
SELECT 
  service_api, 
  SUM(CASE WHEN unit = 'inputTokens' THEN value ELSE 0 END) as total_input_tokens,
  SUM(CASE WHEN unit = 'outputTokens' THEN value ELSE 0 END) as total_output_tokens,
  SUM(CASE WHEN unit = 'totalTokens' THEN value ELSE 0 END) as total_tokens,
  COUNT(DISTINCT document_id) as document_count
FROM 
  metering
GROUP BY 
  service_api
ORDER BY 
  total_tokens DESC;
```

