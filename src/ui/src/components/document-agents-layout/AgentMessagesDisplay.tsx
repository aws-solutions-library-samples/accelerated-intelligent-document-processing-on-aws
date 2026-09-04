// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useEffect, useRef, useState, useMemo, useCallback } from 'react';
import { Container, Header, Box, Spinner, SpaceBetween, Button, Modal } from '@cloudscape-design/components';

interface AgentMessage {
  role: string;
  content: string | Array<Record<string, unknown>> | Record<string, unknown>;
  message_type?: string;
  timestamp?: string;
  sequence_number?: number;
  tool_name?: string;
  originalMessage?: AgentMessage;
  throttling_details?: {
    error_code: string;
  };
  [key: string]: unknown;
}

interface AgentMessagesDisplayProps {
  agentMessages?: string | null;
  isProcessing?: boolean;
}

const AgentMessagesDisplay = ({ agentMessages = null, isProcessing = false }: AgentMessagesDisplayProps): React.JSX.Element | null => {
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const messagesContainerRef = useRef<HTMLDivElement>(null);
  const [sqlModalVisible, setSqlModalVisible] = useState(false);
  const [currentSqlQuery, setCurrentSqlQuery] = useState('');
  const [codeModalVisible, setCodeModalVisible] = useState(false);
  const [currentPythonCode, setCurrentPythonCode] = useState('');
  const [databaseInfoModalVisible, setDatabaseInfoModalVisible] = useState(false);
  const [currentDatabaseInfo, setCurrentDatabaseInfo] = useState('');

  // Suppress ResizeObserver errors in development
  useEffect(() => {
    const handleResizeObserverError = (e: ErrorEvent) => {
      if (e.message === 'ResizeObserver loop completed with undelivered notifications.') {
        e.stopImmediatePropagation();
        return false;
      }
      return true;
    };

    window.addEventListener('error', handleResizeObserverError as unknown as EventListener);
    return () => {
      window.removeEventListener('error', handleResizeObserverError as unknown as EventListener);
    };
  }, []);

  // Extract SQL query from tool use content
  const extractSqlQuery = (originalMessage: AgentMessage | undefined): string | null => {
    if (!originalMessage || !originalMessage.content) return null;

    // Handle array content format
    if (Array.isArray(originalMessage.content)) {
      const sqlItem = (originalMessage.content as Array<Record<string, unknown>>).find(
        (item) =>
          item &&
          (item.toolUse as Record<string, unknown>) &&
          (item.toolUse as Record<string, unknown>).name === 'run_athena_query_with_config',
      );
      const toolUse = sqlItem?.toolUse as Record<string, unknown> | undefined;
      const input = toolUse?.input as Record<string, unknown> | undefined;
      return (input?.query as string) || null;
    }

    return null;
  };

  // Extract Python code from tool use content
  const extractPythonCode = (originalMessage: AgentMessage | undefined): string | null => {
    if (!originalMessage || !originalMessage.content) return null;

    // Handle array content format
    if (Array.isArray(originalMessage.content)) {
      const codeItem = (originalMessage.content as Array<Record<string, unknown>>).find(
        (item) => item && (item.toolUse as Record<string, unknown>) && (item.toolUse as Record<string, unknown>).name === 'execute_python',
      );
      const toolUse = codeItem?.toolUse as Record<string, unknown> | undefined;
      const input = toolUse?.input as Record<string, unknown> | undefined;
      return (input?.code as string) || null;
    }

    return null;
  };

  // Extract database info from tool use content (for get_database_info tool)
  const extractDatabaseInfo = (originalMessage: AgentMessage | undefined): string | null => {
    if (!originalMessage || !originalMessage.content) return null;

    // Handle array content format
    if (Array.isArray(originalMessage.content)) {
      const dbInfoItem = (originalMessage.content as Array<Record<string, unknown>>).find(
        (item) =>
          item && (item.toolUse as Record<string, unknown>) && (item.toolUse as Record<string, unknown>).name === 'get_database_info',
      );
      // For get_database_info, there typically isn't input data, but we can check for tool result
      return dbInfoItem ? 'Database schema information was retrieved by the agent' : null;
    }

    return null;
  };

  // Show SQL query modal with error handling
  const showSqlQuery = useCallback((originalMessage: AgentMessage | undefined) => {
    try {
      const sqlQuery = extractSqlQuery(originalMessage);
      if (sqlQuery) {
        setCurrentSqlQuery(sqlQuery);
        // Use setTimeout to avoid ResizeObserver issues
        setTimeout(() => {
          setSqlModalVisible(true);
        }, 0);
      }
    } catch (error) {
      console.warn('Error showing SQL query:', error);
    }
  }, []);

  // Show Python code modal with error handling
  const showPythonCode = useCallback((originalMessage: AgentMessage | undefined) => {
    try {
      const pythonCode = extractPythonCode(originalMessage);
      if (pythonCode) {
        setCurrentPythonCode(pythonCode);
        // Use setTimeout to avoid ResizeObserver issues
        setTimeout(() => {
          setCodeModalVisible(true);
        }, 0);
      }
    } catch (error) {
      console.warn('Error showing Python code:', error);
    }
  }, []);

  // Show database info modal with error handling
  const showDatabaseInfo = useCallback(async () => {
    try {
      const databaseSchemaInfo = `# Comprehensive Athena Database Schema

## Overview

This database contains three main categories of tables for document processing analytics:

1. **Metering Table**: Usage metrics, costs, and consumption data
2. **Evaluation Tables**: Accuracy assessment data (typically empty unless evaluation jobs are run)
3. **Document Sections Tables**: Extracted content from processed documents (dynamically created)

## Important Notes

- **Column Names**: Always enclose column names in double quotes in Athena queries
- **Partitioning**: All tables are partitioned by date (YYYY-MM-DD format) for efficient querying
- **Timestamps**: All date/timestamp columns refer to processing time, not document content dates
- **Case Sensitivity**: Use LOWER() functions when comparing string values as case may vary

---

## Metering Table (metering)

**Purpose**: Captures detailed usage metrics and cost information for document processing operations

**Key Usage**: Always use this table for questions about:
- Volume of documents processed
- Models used and their consumption patterns
- Units of consumption (tokens, pages) for each processing step
- Costs and spending analysis
- Processing patterns and trends

**Important**: Each document has multiple rows in this table - one for each context/service/unit combination.

### Schema:
- \`document_id\` (string): Unique identifier for the document
- \`context\` (string): Processing context (OCR, Classification, Extraction, Assessment, Summarization, Evaluation)
- \`service_api\` (string): Specific API or model used (e.g., textract/analyze_document, bedrock/claude-3-sonnet)
- \`unit\` (string): Unit of measurement (pages, inputTokens, outputTokens, totalTokens)
- \`value\` (double): Quantity of the unit consumed
- \`number_of_pages\` (int): Number of pages in the document (replicated across all rows for same document)
- \`unit_cost\` (double): Cost per unit in USD
- \`estimated_cost\` (double): Calculated total cost (value × unit_cost)
- \`timestamp\` (timestamp): When the operation was performed

**Partitioned by**: date (YYYY-MM-DD format)

### Critical Aggregation Patterns:
- **For document page counts**: Use \`MAX("number_of_pages")\` per document (NOT SUM, as this value is replicated)
- **For total pages across documents**: Use \`SUM\` of per-document MAX values:
  \`\`\`sql
  SELECT SUM(max_pages) FROM (
    SELECT "document_id", MAX("number_of_pages") as max_pages
    FROM metering
    GROUP BY "document_id"
  )
  \`\`\`
- **For costs**: Use \`SUM("estimated_cost")\` for totals, \`GROUP BY "context"\` for breakdowns
- **For token usage**: Use \`SUM("value")\` when \`"unit"\` IN ('inputTokens', 'outputTokens', 'totalTokens')

### Sample Queries:
\`\`\`sql
-- Total documents processed
SELECT COUNT(DISTINCT "document_id") FROM metering

-- Total pages processed (correct aggregation)
SELECT SUM(max_pages) FROM (
  SELECT "document_id", MAX("number_of_pages") as max_pages
  FROM metering
  GROUP BY "document_id"
)

-- Cost breakdown by processing context
SELECT "context", SUM("estimated_cost") as total_cost
FROM metering
GROUP BY "context"
ORDER BY total_cost DESC

-- Token usage by model
SELECT "service_api",
       SUM(CASE WHEN "unit" = 'inputTokens' THEN "value" ELSE 0 END) as input_tokens,
       SUM(CASE WHEN "unit" = 'outputTokens' THEN "value" ELSE 0 END) as output_tokens
FROM metering
WHERE "unit" IN ('inputTokens', 'outputTokens')
GROUP BY "service_api"
\`\`\`

### Prefer Rollup Tables for Wide Time Ranges

For questions spanning **more than 2 hours** of history, use the reporting
rollup tables — they aggregate raw \`metering\` hourly/daily so a "cost
this month" question scans a few hundred rows instead of millions.

- **\`metering_hourly\`** / **\`metering_daily\`** — pre-summed cost per
  \`(hour|day, config_version, service_api, unit)\`. Columns:
  \`sum_value\` (a **quantity** — tokens/pages/seconds; check \`unit\`)
  and \`sum_cost\` (USD). \`metering_daily\` uses \`day\` (not
  \`hour_ts\`).
- **\`metering_docs_hourly\`** / **\`metering_docs_daily\`** — doc-grain
  volume per \`(hour|day, config_version)\`. Columns: \`n_docs\`,
  \`sum_pages\`. Do NOT query these for cost or by \`service_api\` —
  those columns aren't on these tables. ⚠️ **\`n_docs\` on
  \`metering_docs_daily\` is a doc-HOURS count** (\`SUM\` of hourly
  \`n_docs\`), so a doc appearing in 3 hours of a day is counted 3
  times. For strict cross-day unique-doc counts, query raw \`metering\`
  with \`COUNT(DISTINCT document_id)\`.
- **\`control_plane_hourly\`** / **\`data_plane_lambda_hourly\`** —
  Lambda compute cost per hour, split by control plane (dashboards,
  agents, resolvers) vs data plane (OCR/Extraction/etc. pipeline).

Fall back to raw \`metering\` for **per-document drilldown** or the
**current partial hour** (before the hourly rollup fires).

---

## Evaluation Tables

**Purpose**: Store accuracy metrics from comparing extracted document data against ground truth baselines

**Key Usage**: Always use these tables for questions about accuracy for documents that have ground truth data

**Important**: These tables are typically empty unless users have run separate evaluation jobs (not run by default)

### Document Evaluations Table (document_evaluations)

**Purpose**: Document-level evaluation metrics and overall accuracy scores

#### Schema:
- \`document_id\` (string): Unique identifier for the document
- \`input_key\` (string): S3 key of the input document
- \`evaluation_date\` (timestamp): When the evaluation was performed
- \`accuracy\` (double): Overall accuracy score (0-1)
- \`precision\` (double): Precision score (0-1)
- \`recall\` (double): Recall score (0-1)
- \`f1_score\` (double): F1 score (0-1)
- \`false_alarm_rate\` (double): False alarm rate (0-1)
- \`false_discovery_rate\` (double): False discovery rate (0-1)
- \`execution_time\` (double): Time taken to evaluate (seconds)

**Partitioned by**: date (YYYY-MM-DD format)

### Section Evaluations Table (section_evaluations)

**Purpose**: Section-level evaluation metrics grouped by document type/classification

#### Schema:
- \`document_id\` (string): Unique identifier for the document
- \`section_id\` (string): Identifier for the section
- \`section_type\` (string): Type/class of the section (e.g., 'invoice', 'receipt', 'w2')
- \`accuracy\` (double): Section accuracy score (0-1)
- \`precision\` (double): Section precision score (0-1)
- \`recall\` (double): Recall score (0-1)
- \`f1_score\` (double): Section F1 score (0-1)
- \`false_alarm_rate\` (double): Section false alarm rate (0-1)
- \`false_discovery_rate\` (double): Section false discovery rate (0-1)
- \`evaluation_date\` (timestamp): When the evaluation was performed

**Partitioned by**: date (YYYY-MM-DD format)

### Attribute Evaluations Table (attribute_evaluations)

**Purpose**: Detailed attribute-level comparison results showing expected vs actual extracted values

#### Schema:
- \`document_id\` (string): Unique identifier for the document
- \`section_id\` (string): Identifier for the section
- \`section_type\` (string): Type/class of the section
- \`attribute_name\` (string): Name of the extracted attribute
- \`expected\` (string): Expected (ground truth) value
- \`actual\` (string): Actual extracted value
- \`matched\` (boolean): Whether the values matched according to evaluation method
- \`score\` (double): Match score (0-1)
- \`reason\` (string): Explanation for the match result
- \`evaluation_method\` (string): Method used for comparison (EXACT, FUZZY, SEMANTIC, etc.)
- \`confidence\` (string): Confidence score from extraction process
- \`confidence_threshold\` (string): Confidence threshold used for evaluation
- \`evaluation_date\` (timestamp): When the evaluation was performed

**Partitioned by**: date (YYYY-MM-DD format)

---

## Document Sections Tables (Configuration-Based)

**Purpose**: Store actual extracted data from document sections in structured format for analytics

**Key Usage**: Use these tables to query the actual extracted content and attributes from processed documents

**IMPORTANT**: Based on your current configuration, the following tables DEFINITELY exist. Do NOT use discovery queries (SHOW TABLES, DESCRIBE) for these - use them directly.

### Known Document Sections Tables:

- \`document_sections_payslip\`
- \`document_sections_us_drivers_licenses\`
- \`document_sections_bank_checks\`
- \`document_sections_bank_statement\`
- \`document_sections_w2\`
- \`document_sections_homeowners_insurance_application\`

### Complete Table Schemas:

Each table has the following structure:

**\`document_sections_payslip\`** (Class: "Payslip"):
- **Description**: An employee wage statement showing earnings, deductions, taxes, and net pay for a specific pay period, typically issued by employers to document compensation details including gross pay, various tax withholdings, and year-to-date totals.
- **Standard Columns**:
  - \`document_class.type\` (string): Document classification type
  - \`document_id\` (string): Unique identifier for the document
  - \`section_id\` (string): Unique identifier for the section
  - \`section_classification\` (string): Type/class of the section
  - \`section_confidence\` (string): Confidence score for the section classification
  - \`explainability_info\` (string): JSON containing explanation of extraction decisions
  - \`timestamp\` (timestamp): When the document was processed
  - \`date\` (string): Partition key in YYYY-MM-DD format
  - Various \`metadata.*\` columns (strings): Processing metadata
- **Configuration-Specific Columns**:
  - \`"inference_result.ytdnetpay"\` (string): Year-to-date net pay amount representing cumulative take-home earnings after all deductions from the beginning of the year to the current pay period.
  - \`"inference_result.companyaddress.state"\` (string): The state or province portion of the company's business address.
  - \`"inference_result.employeename.firstname"\` (string): The given name of the employee.
  - \`"inference_result.federaltaxes"\` (string): JSON list of federal tax withholdings showing different types of federal taxes deducted, with both current period and year-to-date amounts.

**\`document_sections_w2\`** (Class: "W2"):
- **Description**: An annual tax document provided by employers to employees reporting wages earned and taxes withheld during the tax year for federal and state income tax filing purposes, containing comprehensive compensation and withholding information.
- **Standard Columns**: (Same as above)
- **Configuration-Specific Columns**:
  - \`"inference_result.employer_info.employer_name"\` (string): The legal name of the employing company or organization.
  - \`"inference_result.employee_general_info.ssn"\` (string): The Social Security Number of the employee.
  - \`"inference_result.federal_wage_info.wages_tips_other_compensation"\` (string): Total wages, tips, and other compensation paid to the employee.
  - \`"inference_result.state_taxes_table"\` (string): JSON array containing state and local tax information for specific jurisdictions.

**\`document_sections_us_drivers_licenses\`** (Class: "US-drivers-licenses"):
- **Description**: An official government-issued identification document that authorizes an individual to operate motor vehicles, containing personal information, physical characteristics, address details, and driving privileges with restrictions and endorsements.
- **Standard Columns**: (Same as above)
- **Configuration-Specific Columns**:
  - \`"inference_result.state_name"\` (string): The state or jurisdiction that issued the driver's license, typically shown as a two-letter state abbreviation.
  - \`"inference_result.id_number"\` (string): The unique driver's license identification number assigned by the issuing state.
  - \`"inference_result.name_details.first_name"\` (string): The given name of the license holder.
  - \`"inference_result.name_details.last_name"\` (string): The family name or surname of the license holder.
  - \`"inference_result.personal_details.height"\` (string): The physical height of the license holder.
  - \`"inference_result.address_details.city"\` (string): The city of residence for the license holder.

**\`document_sections_bank_checks\`** (Class: "Bank-checks"):
- **Description**: A written financial instrument directing a bank to pay a specific amount of money from the account holder's account to a designated payee, containing payment details, account information, and verification elements.
- **Standard Columns**: (Same as above)
- **Configuration-Specific Columns**:
  - \`"inference_result.date"\` (string): The date when the check was written, typically handwritten or printed in the date field.
  - \`"inference_result.dollar_amount"\` (string): The numerical amount to be paid as specified on the check.
  - \`"inference_result.check_number"\` (string): The unique sequential number identifying this specific check.
  - \`"inference_result.account_holder_name"\` (string): The name of the person or entity who owns the bank account and wrote the check.
  - \`"inference_result.payee_name"\` (string): The name of the person or entity receiving the payment.
  - \`"inference_result.bank_name"\` (string): The name of the financial institution where the account is held.

**\`document_sections_bank_statement\`** (Class: "Bank-Statement"):
- **Description**: A periodic financial document issued by banks detailing account activity, balances, and transactions over a specific time period, providing account holders with a summary of their financial activity and current account status.
- **Standard Columns**: (Same as above)
- **Configuration-Specific Columns**:
  - \`"inference_result.account_holder_name"\` (string): The name of the person or entity who owns the bank account.
  - \`"inference_result.account_number"\` (string): The unique identifier for the bank account, often partially masked for security.
  - \`"inference_result.bank_name"\` (string): The name of the financial institution issuing the statement.
  - \`"inference_result.statement_start_date"\` (string): The beginning date of the statement period.
  - \`"inference_result.statement_end_date"\` (string): The ending date of the statement period.
  - \`"inference_result.transaction_details"\` (string): JSON array containing detailed listing of all transactions during the statement period.

**\`document_sections_homeowners_insurance_application\`** (Class: "Homeowners-Insurance-Application"):
- **Description**: An application form for homeowners insurance coverage containing applicant personal information, property details, coverage requirements, existing insurance history, and underwriting data necessary for evaluating risk and determining appropriate coverage terms.
- **Standard Columns**: (Same as above)
- **Configuration-Specific Columns**:
  - \`"inference_result.policy number"\` (string): The unique identifier assigned to the insurance policy for tracking and reference purposes.
  - \`"inference_result.effective date"\` (string): The date when the insurance coverage begins and becomes active.
  - \`"inference_result.expiration date"\` (string): The date when the insurance policy expires and requires renewal.
  - \`"inference_result.named insured(s) and mailing address"\` (string): The complete name and mailing address of the primary insured party.
  - \`"inference_result.primary applicant information.name"\` (string): The full name of the primary applicant.
  - \`"inference_result.co-applicant information.name"\` (string): The full name of the co-applicant if applicable.

### Column Naming Patterns:
- **Simple attributes**: \`inference_result.{attribute_name_lowercase}\` (all strings)
- **Group attributes**: \`inference_result.{group_name_lowercase}.{sub_attribute_lowercase}\` (all strings)
- **List attributes**: \`inference_result.{list_name_lowercase}\` (JSON string containing array data)

### CRITICAL: Dot-Notation Column Names
**These are SINGLE column identifiers containing dots, NOT table.column references:**
- ✅ **CORRECT**: \`"document_class.type"\` (single column name containing a dot)
- ❌ **WRONG**: \`"document_class"."type"\` (table.column syntax - this will FAIL)
- ✅ **CORRECT**: \`"inference_result.ytdnetpay"\` (single column name containing dots)
- ❌ **WRONG**: \`"inference_result"."ytdnetpay"\` (table.column syntax - this will FAIL)

### Important Querying Notes:
- **All \`inference_result.*\` columns are string type** - even numeric data is stored as strings
- **Always use double quotes** around column names: \`"inference_result.companyaddress.state"\`
- **Dot notation columns**: Names like \`document_class.type\` are SINGLE column names with dots inside quotes
- **List data is stored as JSON strings** - use JSON parsing functions to extract array elements
- **Case sensitivity**: Column names are lowercase, use LOWER() for string comparisons
- **Partitioning**: All tables partitioned by \`date\` in YYYY-MM-DD format

### Sample Queries:
\`\`\`sql
-- Query specific attributes (example for Payslip)
SELECT "document_id",
       "inference_result.ytdnetpay",
       "inference_result.employeename.firstname",
       "inference_result.companyaddress.state"
FROM document_sections_payslip
WHERE date >= '2024-01-01'

-- Parse JSON list data (example for FederalTaxes)
SELECT "document_id",
       json_extract_scalar(tax_item, '$.ItemDescription') as tax_type,
       json_extract_scalar(tax_item, '$.YTD') as ytd_amount
FROM document_sections_payslip
CROSS JOIN UNNEST(json_parse("inference_result.federaltaxes")) as t(tax_item)

-- Join with metering for cost analysis
SELECT ds."section_classification",
       COUNT(DISTINCT ds."document_id") as document_count,
       AVG(CAST(m."estimated_cost" AS double)) as avg_processing_cost
FROM document_sections_w2 ds
JOIN metering m ON ds."document_id" = m."document_id"
GROUP BY ds."section_classification"
\`\`\`

**This schema information is generated from your actual configuration and shows exactly what tables and columns exist in your deployment.**

---

## General Query Tips

### Performance Optimization:
- Use date partitioning in WHERE clauses when possible: \`WHERE date >= '2024-01-01'\`
- Use LIMIT for exploratory queries to avoid large result sets
- Consider using approximate functions like \`approx_distinct()\` for large datasets

### Common Joins:
\`\`\`sql
-- Join metering with evaluations for cost vs accuracy analysis
SELECT m."document_id", m."estimated_cost", e."accuracy"
FROM metering m
JOIN document_evaluations e ON m."document_id" = e."document_id"

-- Join document sections with metering for content analysis with costs
SELECT ds.*, m."estimated_cost"
FROM document_sections_payslip ds
JOIN metering m ON ds."document_id" = m."document_id"
\`\`\``;

      setCurrentDatabaseInfo(databaseSchemaInfo);
      // Use setTimeout to avoid ResizeObserver issues
      setTimeout(() => {
        setDatabaseInfoModalVisible(true);
      }, 0);
    } catch (error) {
      console.warn('Error showing database info:', error);
    }
  }, []);

  // Handle code modal dismiss with error handling
  const handleCodeModalDismiss = useCallback(() => {
    try {
      setCodeModalVisible(false);
      setCurrentPythonCode('');
    } catch (error) {
      console.warn('Error dismissing code modal:', error);
    }
  }, []);

  // Handle copy code to clipboard with error handling
  const handleCopyCodeToClipboard = useCallback(() => {
    try {
      if (currentPythonCode && navigator.clipboard) {
        navigator.clipboard.writeText(currentPythonCode).catch((error) => {
          console.warn('Failed to copy code to clipboard:', error);
          // Fallback for older browsers
          const textArea = document.createElement('textarea');
          textArea.value = currentPythonCode;
          document.body.appendChild(textArea);
          textArea.select();
          document.execCommand('copy');
          document.body.removeChild(textArea);
        });
      }
    } catch (error) {
      console.warn('Error copying code to clipboard:', error);
    }
  }, [currentPythonCode]);

  // Handle modal dismiss with error handling
  const handleModalDismiss = useCallback(() => {
    try {
      setSqlModalVisible(false);
      setCurrentSqlQuery('');
    } catch (error) {
      console.warn('Error dismissing modal:', error);
    }
  }, []);

  // Handle copy to clipboard with error handling
  const handleCopyToClipboard = useCallback(() => {
    try {
      if (currentSqlQuery && navigator.clipboard) {
        navigator.clipboard.writeText(currentSqlQuery).catch((error) => {
          console.warn('Failed to copy to clipboard:', error);
          // Fallback for older browsers
          const textArea = document.createElement('textarea');
          textArea.value = currentSqlQuery;
          document.body.appendChild(textArea);
          textArea.select();
          document.execCommand('copy');
          document.body.removeChild(textArea);
        });
      }
    } catch (error) {
      console.warn('Error copying to clipboard:', error);
    }
  }, [currentSqlQuery]);

  // Handle database info modal dismiss with error handling
  const handleDatabaseInfoModalDismiss = useCallback(() => {
    try {
      setDatabaseInfoModalVisible(false);
      setCurrentDatabaseInfo('');
    } catch (error) {
      console.warn('Error dismissing database info modal:', error);
    }
  }, []);

  // Handle copy database info to clipboard with error handling
  const handleCopyDatabaseInfoToClipboard = useCallback(() => {
    try {
      if (currentDatabaseInfo && navigator.clipboard) {
        navigator.clipboard.writeText(currentDatabaseInfo).catch((error) => {
          console.warn('Failed to copy database info to clipboard:', error);
          // Fallback for older browsers
          const textArea = document.createElement('textarea');
          textArea.value = currentDatabaseInfo;
          document.body.appendChild(textArea);
          textArea.select();
          document.execCommand('copy');
          document.body.removeChild(textArea);
        });
      }
    } catch (error) {
      console.warn('Error copying database info to clipboard:', error);
    }
  }, [currentDatabaseInfo]);

  // Parse and process messages using useMemo to avoid re-render loops
  const messages = useMemo(() => {
    if (!agentMessages) return [];

    try {
      const parsed = JSON.parse(agentMessages);
      const rawMessages: AgentMessage[] = Array.isArray(parsed) ? parsed : [];

      // Split assistant messages that contain both text and tool use
      const splitAssistantMessage = (message: AgentMessage): AgentMessage[] => {
        const { content } = message;

        // If content is a string, check if it contains tool use JSON
        if (typeof content === 'string') {
          // Look for tool use patterns in the string
          const toolUseRegex = /\{"toolUse":\{[^}]+\}\}/g;
          const matches = content.match(toolUseRegex);

          if (matches && matches.length > 0) {
            const splitMessages: AgentMessage[] = [];
            let remainingContent = content;

            matches.forEach((match) => {
              // Split the content at the tool use
              const parts = remainingContent.split(match);

              // Add text part if it exists and has meaningful content
              if (parts[0] && parts[0].trim()) {
                splitMessages.push({
                  ...message,
                  content: parts[0].trim(),
                });
              }

              // Parse and add tool use message
              try {
                const toolUse = JSON.parse(match);
                const toolName = ((toolUse.toolUse as Record<string, unknown>)?.name as string) || 'unknown';
                splitMessages.push({
                  ...message,
                  role: 'tool',
                  content: `Tool request initiated for tool: ${toolName}`,
                  tool_name: toolName,
                  timestamp: message.timestamp,
                  originalMessage: message, // Store original message for SQL extraction
                });
              } catch (error) {
                // If parsing fails, include the raw JSON as assistant message
                splitMessages.push({
                  ...message,
                  content: match,
                });
              }

              remainingContent = parts[1] || '';
            });

            // Add any remaining content
            if (remainingContent && remainingContent.trim()) {
              splitMessages.push({
                ...message,
                content: remainingContent.trim(),
              });
            }

            return splitMessages.length > 0 ? splitMessages : [message];
          }
        }

        // If content is an array, process each item
        if (Array.isArray(content)) {
          const splitMessages: AgentMessage[] = [];
          let textParts: string[] = [];

          (content as Array<string | Record<string, unknown>>).forEach((item) => {
            if (typeof item === 'string') {
              textParts.push(item);
            } else if (item && typeof item === 'object' && (item as Record<string, unknown>).text) {
              textParts.push((item as Record<string, unknown>).text as string);
            } else if (item && typeof item === 'object' && (item as Record<string, unknown>).toolUse) {
              // Add text content if we have any
              if (textParts.length > 0) {
                const textContent = textParts.join('\n').trim();
                if (textContent) {
                  splitMessages.push({
                    ...message,
                    content: textContent,
                  });
                }
                textParts = [];
              }

              // Add tool use message
              const toolName = (((item as Record<string, unknown>).toolUse as Record<string, unknown>)?.name as string) || 'unknown';
              splitMessages.push({
                ...message,
                role: 'tool',
                content: `${toolName}`,
                tool_name: toolName,
                timestamp: message.timestamp,
                originalMessage: message, // Store original message for SQL extraction
              });
            }
          });

          // Add any remaining text content
          if (textParts.length > 0) {
            const textContent = textParts.join('\n').trim();
            if (textContent) {
              splitMessages.push({
                ...message,
                content: textContent,
              });
            }
          }

          return splitMessages.length > 0 ? splitMessages : [message];
        }

        // For other content types, return as-is
        return [message];
      };

      // Process messages to split assistant messages that contain tool use
      const processedMessages: AgentMessage[] = [];

      rawMessages.forEach((message) => {
        // Skip messages with empty or invalid content
        if (
          !message.content ||
          (Array.isArray(message.content) && message.content.length === 0) ||
          (typeof message.content === 'string' && !message.content.trim())
        ) {
          return;
        }

        if (message.role === 'assistant' && message.content) {
          const splitMsgs = splitAssistantMessage(message);
          processedMessages.push(...splitMsgs);
        } else {
          processedMessages.push(message);
        }
      });

      return processedMessages;
    } catch (error) {
      return [];
    }
  }, [agentMessages]);

  // Auto-scroll to bottom within the messages container (not the whole page)
  useEffect(() => {
    if (messagesContainerRef.current) {
      messagesContainerRef.current.scrollTop = messagesContainerRef.current.scrollHeight;
    }
  }, [messages, isProcessing]);

  // Format timestamp for display
  const formatTimestamp = (timestamp: string | undefined): string => {
    if (!timestamp) return '';
    try {
      const date = new Date(timestamp);
      return date.toLocaleTimeString();
    } catch (error) {
      return timestamp;
    }
  };

  // Get role display name and styling
  const getRoleInfo = (role: string, messageType?: string) => {
    switch (role) {
      case 'user':
        return { display: 'User', color: '#0073bb', icon: '👤' };
      case 'assistant':
        return { display: 'Assistant', color: '#037f0c', icon: '🤖' };
      case 'tool':
        return { display: 'Tool', color: '#8b5a00', icon: '🔧' };
      case 'exception':
        if (messageType === 'throttling_exception') {
          return { display: 'Throttling', color: '#ff9900', icon: '⚠️' };
        }
        return { display: 'Exception', color: '#d13212', icon: '❌' };
      default:
        return { display: role || 'Unknown', color: '#666', icon: '❓' };
    }
  };

  // Extract text content from message content (handles both string and object formats)
  const extractTextContent = (content: string | Array<Record<string, unknown>> | Record<string, unknown> | null | undefined): string => {
    if (!content) return '<No content>';

    // If content is a string, return it directly
    if (typeof content === 'string') {
      return content;
    }

    // If content is an array, extract text from each item
    if (Array.isArray(content)) {
      const textParts: string[] = [];

      (content as Array<string | Record<string, unknown>>).forEach((item) => {
        if (typeof item === 'string') {
          textParts.push(item);
        } else if (item && typeof item === 'object' && (item as Record<string, unknown>).text) {
          textParts.push((item as Record<string, unknown>).text as string);
        } else if (item && typeof item === 'object' && !(item as Record<string, unknown>).toolUse) {
          // For other objects that aren't toolUse, stringify them
          textParts.push(JSON.stringify(item));
        }
        // Skip toolUse objects as they're handled separately
      });

      const result = textParts.join('\n').trim();
      return result || '<No text content>';
    }

    // If content is an object with a text property, extract it
    if (typeof content === 'object' && (content as Record<string, unknown>).text) {
      return (content as Record<string, unknown>).text as string;
    }

    // For any other object, stringify it
    if (typeof content === 'object') {
      return JSON.stringify(content, null, 2);
    }

    return String(content);
  };

  // Render individual message
  const renderMessage = (message: AgentMessage, index: number): React.JSX.Element => {
    const roleInfo = getRoleInfo(message.role, message.message_type);
    const timestamp = formatTimestamp(message.timestamp);
    let textContent = extractTextContent(message.content);

    // Handle throttling messages specially
    const isThrottlingMessage = message.role === 'exception' && message.message_type === 'throttling_exception';

    // For tool messages, if we have a tool_name, show it more prominently
    if (message.role === 'tool' && message.tool_name) {
      // If the content is just a generic success message, show the tool name instead
      if (textContent === "Tool completed with status 'success'." || textContent.includes('Tool completed with status')) {
        textContent = `Tool request initiated for tool: ${message.tool_name}`;
      }
    }

    // Check if this is a run_athena_query_with_config tool and has SQL query
    const isAthenaQuery = message.role === 'tool' && message.tool_name === 'run_athena_query_with_config';
    const hasSqlQuery = isAthenaQuery && message.originalMessage && extractSqlQuery(message.originalMessage);

    // Check if this is an execute_python tool and has Python code
    const isPythonExecution = message.role === 'tool' && message.tool_name === 'execute_python';
    const hasPythonCode = isPythonExecution && message.originalMessage && extractPythonCode(message.originalMessage);

    // Check if this is a get_database_info tool
    const isDatabaseInfoTool = message.role === 'tool' && message.tool_name === 'get_database_info';
    const hasDatabaseInfo = isDatabaseInfoTool && message.originalMessage && extractDatabaseInfo(message.originalMessage);

    // Create a unique key for this message
    const messageKey = `${message.role}-${message.sequence_number}-${index}-${message.timestamp}`;

    // Apply styling for throttling messages
    const messageStyle: React.CSSProperties = isThrottlingMessage
      ? {
          opacity: 0.7,
          backgroundColor: '#fff8f0',
          borderRadius: '4px',
          padding: '4px',
          margin: '2px 0',
        }
      : {};

    return (
      <Box key={messageKey} padding={{ vertical: 'xs', horizontal: 's' }}>
        <div
          style={{
            borderLeft: `3px solid ${roleInfo.color}`,
            paddingLeft: '8px',
            marginBottom: '4px',
            ...messageStyle,
          }}
        >
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              marginBottom: '2px',
              fontSize: '13px',
              fontWeight: 'bold',
              color: roleInfo.color,
            }}
          >
            <span style={{ marginRight: '4px' }}>{roleInfo.icon}</span>
            <span>{roleInfo.display}</span>
            {timestamp && (
              <span
                style={{
                  marginLeft: '8px',
                  fontSize: '11px',
                  color: '#666',
                  fontWeight: 'normal',
                }}
              >
                {timestamp}
              </span>
            )}
            {message.tool_name && (
              <span
                style={{
                  marginLeft: '6px',
                  fontSize: '11px',
                  backgroundColor: '#f0f0f0',
                  padding: '1px 4px',
                  borderRadius: '2px',
                  fontWeight: 'normal',
                }}
              >
                {message.tool_name}
              </span>
            )}
            {isThrottlingMessage && message.throttling_details && (
              <span
                style={{
                  marginLeft: '6px',
                  fontSize: '11px',
                  backgroundColor: '#fff3cd',
                  color: '#856404',
                  padding: '1px 4px',
                  borderRadius: '2px',
                  fontWeight: 'normal',
                  border: '1px solid #ffeaa7',
                }}
              >
                {message.throttling_details.error_code}
              </span>
            )}
            {hasSqlQuery && (
              <button
                type="button"
                onClick={() => showSqlQuery(message.originalMessage)}
                style={{
                  marginLeft: '8px',
                  fontSize: '11px',
                  padding: '3px 8px',
                  backgroundColor: '#0073bb',
                  color: 'white',
                  border: '1px solid #0073bb',
                  borderRadius: '4px',
                  textDecoration: 'none',
                  fontWeight: '500',
                  cursor: 'pointer',
                  fontFamily: 'inherit',
                }}
                onMouseEnter={(e) => {
                  (e.target as HTMLButtonElement).style.backgroundColor = '#005a9e';
                  (e.target as HTMLButtonElement).style.borderColor = '#005a9e';
                }}
                onMouseLeave={(e) => {
                  (e.target as HTMLButtonElement).style.backgroundColor = '#0073bb';
                  (e.target as HTMLButtonElement).style.borderColor = '#0073bb';
                }}
              >
                View SQL
              </button>
            )}
            {hasPythonCode && (
              <button
                type="button"
                onClick={() => showPythonCode(message.originalMessage)}
                style={{
                  marginLeft: '8px',
                  fontSize: '11px',
                  padding: '3px 8px',
                  backgroundColor: '#0073bb',
                  color: 'white',
                  border: '1px solid #0073bb',
                  borderRadius: '4px',
                  textDecoration: 'none',
                  fontWeight: '500',
                  cursor: 'pointer',
                  fontFamily: 'inherit',
                }}
                onMouseEnter={(e) => {
                  (e.target as HTMLButtonElement).style.backgroundColor = '#005a9e';
                  (e.target as HTMLButtonElement).style.borderColor = '#005a9e';
                }}
                onMouseLeave={(e) => {
                  (e.target as HTMLButtonElement).style.backgroundColor = '#0073bb';
                  (e.target as HTMLButtonElement).style.borderColor = '#0073bb';
                }}
              >
                View Code
              </button>
            )}
            {hasDatabaseInfo && (
              <button
                type="button"
                onClick={() => showDatabaseInfo()}
                style={{
                  marginLeft: '8px',
                  fontSize: '11px',
                  padding: '3px 8px',
                  backgroundColor: '#0073bb',
                  color: 'white',
                  border: '1px solid #0073bb',
                  borderRadius: '4px',
                  textDecoration: 'none',
                  fontWeight: '500',
                  cursor: 'pointer',
                  fontFamily: 'inherit',
                }}
                onMouseEnter={(e) => {
                  (e.target as HTMLButtonElement).style.backgroundColor = '#005a9e';
                  (e.target as HTMLButtonElement).style.borderColor = '#005a9e';
                }}
                onMouseLeave={(e) => {
                  (e.target as HTMLButtonElement).style.backgroundColor = '#0073bb';
                  (e.target as HTMLButtonElement).style.borderColor = '#0073bb';
                }}
              >
                View Info
              </button>
            )}
          </div>
          {/* Hide content for tool request messages (with tool_name) and throttling messages */}
          {/* Keep content for tool response messages */}
          {!(message.role === 'tool' && message.tool_name) && !isThrottlingMessage && (
            <div
              style={{
                fontSize: '13px',
                lineHeight: '1.3',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
              }}
            >
              {textContent}
            </div>
          )}
        </div>
      </Box>
    );
  };

  if (!messages.length && !isProcessing) {
    return null;
  }

  return (
    <>
      <Container
        header={
          <Header variant="h3" description="Real-time agent conversation">
            Agent Thought Process
          </Header>
        }
      >
        <div
          ref={messagesContainerRef}
          style={{
            backgroundColor: '#fafafa',
            border: '1px solid #e0e0e0',
            borderRadius: '4px',
            height: '300px',
            overflowY: 'auto',
            fontFamily: 'Monaco, Menlo, "Ubuntu Mono", monospace',
            padding: '4px',
          }}
        >
          <SpaceBetween size="xs" {...({ size: 'none' } as Record<string, unknown>)}>
            {messages.length > 0 ? (
              messages.map((message, index) => renderMessage(message, index))
            ) : (
              <Box textAlign="center" padding="s" color="text-body-secondary">
                <em>Waiting for agent to start...</em>
              </Box>
            )}

            {isProcessing && (
              <Box padding={{ vertical: 'xs', horizontal: 's' }} textAlign="center">
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    color: '#666',
                    fontSize: '12px',
                  }}
                >
                  <Spinner size="normal" />
                  <span style={{ marginLeft: '6px' }}>Agent is thinking...</span>
                </div>
              </Box>
            )}

            {/* Invisible element to scroll to */}
            <div ref={messagesEndRef} />
          </SpaceBetween>
        </div>
      </Container>

      {/* SQL Query Modal */}
      {sqlModalVisible && (
        <Modal
          onDismiss={handleModalDismiss}
          visible={sqlModalVisible}
          header="SQL Query"
          size="large"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="normal" onClick={handleCopyToClipboard}>
                  Copy to Clipboard
                </Button>
                <Button variant="primary" onClick={handleModalDismiss}>
                  Close
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <Box padding="s">
            <div
              style={{
                backgroundColor: '#f8f9fa',
                border: '1px solid #e1e4e8',
                borderRadius: '6px',
                padding: '16px',
                fontFamily: 'Monaco, Menlo, "Ubuntu Mono", Consolas, "Courier New", monospace',
                fontSize: '14px',
                lineHeight: '1.45',
                overflow: 'auto',
                maxHeight: '400px',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
              }}
            >
              {currentSqlQuery || 'No SQL query available'}
            </div>
          </Box>
        </Modal>
      )}

      {/* Python Code Modal */}
      {codeModalVisible && (
        <Modal
          onDismiss={handleCodeModalDismiss}
          visible={codeModalVisible}
          header="Python Code"
          size="large"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="normal" onClick={handleCopyCodeToClipboard}>
                  Copy to Clipboard
                </Button>
                <Button variant="primary" onClick={handleCodeModalDismiss}>
                  Close
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <Box padding="s">
            <div
              style={{
                backgroundColor: '#f8f9fa',
                border: '1px solid #e1e4e8',
                borderRadius: '6px',
                padding: '16px',
                fontFamily: 'Monaco, Menlo, "Ubuntu Mono", Consolas, "Courier New", monospace',
                fontSize: '14px',
                lineHeight: '1.45',
                overflow: 'auto',
                maxHeight: '400px',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
              }}
            >
              {currentPythonCode || 'No Python code available'}
            </div>
          </Box>
        </Modal>
      )}

      {/* Database Info Modal */}
      {databaseInfoModalVisible && (
        <Modal
          onDismiss={handleDatabaseInfoModalDismiss}
          visible={databaseInfoModalVisible}
          header="Database Schema Information"
          size="large"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="normal" onClick={handleCopyDatabaseInfoToClipboard}>
                  Copy to Clipboard
                </Button>
                <Button variant="primary" onClick={handleDatabaseInfoModalDismiss}>
                  Close
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <Box padding="s">
            <div
              style={{
                backgroundColor: '#f8f9fa',
                border: '1px solid #e1e4e8',
                borderRadius: '6px',
                padding: '16px',
                fontFamily: 'Monaco, Menlo, "Ubuntu Mono", Consolas, "Courier New", monospace',
                fontSize: '14px',
                lineHeight: '1.45',
                overflow: 'auto',
                maxHeight: '400px',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
              }}
            >
              {currentDatabaseInfo || 'No database information available'}
            </div>
          </Box>
        </Modal>
      )}
    </>
  );
};

export default AgentMessagesDisplay;
