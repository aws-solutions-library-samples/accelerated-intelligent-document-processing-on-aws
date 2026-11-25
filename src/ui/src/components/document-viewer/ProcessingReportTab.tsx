// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import {
  Container,
  Header,
  SpaceBetween,
  Box,
  Alert,
  ColumnLayout,
  StatusIndicator,
  ExpandableSection,
} from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type MessageTag, type NoMessageValues } from 'react-intl';

import { describePromptCache } from '../common/promptCacheModel';
import type { PromptCacheSummary } from '../common/promptCacheModel';
import { issueCodeLabel } from '../common/processing-issues-utils';
import { useFormatters } from '../../i18n/format';

interface Violation {
  field: string;
  message: string;
  possible_cause: string;
}

interface SchemaAnalysis {
  large_array_fields?: string[];
  max_min_items?: number;
  recommendation_strength?: string;
  recommendation_reason?: string;
}

interface OcrAnalysis {
  tables_detected?: number;
  estimated_row_count?: number;
  recommendation_strength?: string;
  recommendation_reason?: string;
  tool_usage_recommended?: boolean;
}

interface ToolUsageDecision {
  expected?: boolean;
  actual?: boolean;
  mismatch?: boolean;
  explanation?: string;
  tool_enabled?: boolean;
  ocr_had_markdown_tables?: boolean;
}

interface CompletenessCheck {
  schema_constraints_met?: boolean;
  violations?: Violation[];
  summary?: string;
}

interface TableParsingStats {
  tables_parsed?: number;
  rows_parsed?: number;
  rows_mapped?: number;
  invocation_count?: number;
  parse_success_rate?: number;
  avg_confidence?: number;
  confidence_available?: boolean;
  warnings?: string[];
}

interface ValidationError {
  path?: string;
  validator?: string;
  message?: string;
}

interface ValidationInfo {
  valid?: boolean;
  error_count?: number;
  failed_fields?: string[];
  errors?: ValidationError[];
  check_formats?: boolean;
  fail_action?: string;
  escalated?: boolean;
  initial_error_count?: number;
  initial_failed_fields?: string[];
  escalation_model?: string;
  escalation_scope?: string;
  escalation_fields?: string[];
  resolved_by_escalation?: boolean;
  escalation_kept?: boolean;
  escalation_decision?: string;
}

interface PopulationCheck {
  fields_defined?: number;
  fields_populated?: number;
  population_ratio?: number;
  threshold?: number;
  below_threshold?: boolean;
  empty_fields?: string[];
}

interface SizingPlan {
  model_id?: string;
  context_buffer?: number;
  max_input_tokens?: number;
  max_output_tokens?: number;
  shard_token_budget?: number;
  max_pages_per_shard?: number;
  list_batch_size?: number;
  prompt_overhead_tokens?: number;
  overrides?: Record<string, unknown>;
}

interface AssessmentBatchSplitStats {
  batch_count?: number;
  concurrent_batches?: number;
  derived_batch_size?: number;
  configured_batch_size?: number;
  escalation_model?: string;
  truncated_calls?: number;
  splits?: number;
  rows_recovered_by_retry?: number;
  rows_recovered_by_escalation?: number;
  unrecoverable_rows?: number;
}

interface FlowStage {
  key?: string;
  label?: string;
  detail?: string;
  status?: string; // ok | info | warning | skipped
  fanout?: number;
  model?: string;
}

interface FlowRecovery {
  truncated_calls?: number;
  splits?: number;
  rows_recovered_by_retry?: number;
  rows_recovered_by_escalation?: number;
  escalation_model?: string;
  unrecoverable_rows?: number;
  deadline_reached?: boolean;
}

interface ProcessingFlow {
  stages?: FlowStage[];
  recovery?: FlowRecovery | null;
}

interface ProcessingMetadata {
  extraction_method?: string;
  extraction_time_seconds?: number;
  parsing_succeeded?: boolean;
  extraction_model?: string;
  extraction_model_overridden?: boolean;
  schema_analysis?: SchemaAnalysis;
  ocr_analysis?: OcrAnalysis;
  tool_usage_decision?: ToolUsageDecision;
  completeness_check?: CompletenessCheck;
  table_parsing_tool_used?: boolean;
  table_parsing_stats?: TableParsingStats;
  validation?: ValidationInfo;
  // Scalar cells the extraction agent left null because it could not read them
  // (#782). Recorded regardless of validation.enabled; a nulled LIST is not
  // counted here — that is a defect, surfaced as extraction_incomplete.
  abstained_fields?: { count?: number; paths?: string[] };
  population_check?: PopulationCheck;
  sizing_plan?: SizingPlan;
  assessment_batch_split_stats?: AssessmentBatchSplitStats;
  processing_flow?: ProcessingFlow;
  // Per-section prompt-cache efficiency (#780): caching / write-only /
  // never-cached / disabled / no-cache-data, with the token counts behind it.
  prompt_cache?: PromptCacheSummary;
}

interface ProcessingIssue {
  stage?: string;
  severity?: string; // "error" | "warning" | "info"
  code?: string;
  message?: string;
  // GraphQL delivers camelCase (rootCause); the section result.json metadata
  // delivers snake_case (root_cause). Accept both.
  rootCause?: string;
  root_cause?: string;
}

interface ProcessingReportTabProps {
  metadata?: ProcessingMetadata;
  processingReport?: string;
  inferenceResult?: Record<string, unknown>;
  // Structured self-healing issues for this section (from the backend
  // ProcessingIssue spine), surfaced at the top of the report.
  processingIssues?: ProcessingIssue[];
}

const messages = defineMessages<{
  notAvailable: NoMessageValues;
  valueNotAvailable: NoMessageValues;
  valid: NoMessageValues;
  outcomeTitle: NoMessageValues;
  agentic: NoMessageValues;
  agenticTimed: { readonly seconds: string };
  standard: NoMessageValues;
  standardTimed: { readonly seconds: string };
  allClear: NoMessageValues;
  completedToReview: { readonly count: number };
  failed: NoMessageValues;
  dataExtracted: NoMessageValues;
  rows: { readonly rows: number };
  fields: { readonly fields: number };
  rowsAndFields: { readonly rows: number; readonly fields: number };
  schemaValidation: NoMessageValues;
  validationIssues: { readonly count: number };
  notEnabled: NoMessageValues;
  model: NoMessageValues;
  modelPerClass: { readonly model: string };
  method: NoMessageValues;
  methodAgentic: NoMessageValues;
  methodStandard: NoMessageValues;
  methodAgenticWithParser: NoMessageValues;
  lists: { readonly lists: string };
}>({
  notAvailable: {
    id: 'documents.processingReport.notAvailable',
    defaultMessage: 'Processing report not available',
    description: "Shown in place of the edit page's Processing Report when the open section's result records no processing metadata.",
  },
  valueNotAvailable: {
    id: 'documents.processingReport.valueNotAvailable',
    defaultMessage: 'N/A',
    description:
      "Shown in the edit page's Processing Report in place of a value the section's report does not record: a percentage, the extraction model, the action taken when schema validation fails, or the table parser's success rate.",
  },
  valid: {
    id: 'documents.processingReport.valid',
    defaultMessage: 'Valid',
    description:
      "Result of schema validation beside a success icon in the edit page's Processing Report, in the Schema validation cell of the Extraction Outcome panel and in the Result cell under Schema Validation & Escalation: the extracted data satisfied the class's schema.",
  },
  outcomeTitle: {
    id: 'documents.processingReport.outcome.title',
    defaultMessage: 'Extraction Outcome',
    description: "Heading of the first panel of the edit page's Processing Report, which sums up how the open section's extraction went.",
  },
  agentic: {
    id: 'documents.processingReport.outcome.agentic',
    defaultMessage: 'Agentic extraction',
    description:
      "Line under the Extraction Outcome heading of the edit page's Processing Report when the section was extracted by the agentic method, without an extraction time.",
  },
  agenticTimed: {
    id: 'documents.processingReport.outcome.agenticTimed',
    defaultMessage: 'Agentic extraction · {seconds}s',
    description:
      "Line under the Extraction Outcome heading of the edit page's Processing Report when the section was extracted by the agentic method. {seconds} is how long the extraction took, in seconds with one decimal, such as 12.3; s is the symbol for seconds.",
  },
  standard: {
    id: 'documents.processingReport.outcome.standard',
    defaultMessage: 'Standard extraction',
    description:
      "Line under the Extraction Outcome heading of the edit page's Processing Report when the section was extracted by the standard, non-agentic method, without an extraction time.",
  },
  standardTimed: {
    id: 'documents.processingReport.outcome.standardTimed',
    defaultMessage: 'Standard extraction · {seconds}s',
    description:
      "Line under the Extraction Outcome heading of the edit page's Processing Report when the section was extracted by the standard, non-agentic method. {seconds} is how long the extraction took, in seconds with one decimal, such as 12.3; s is the symbol for seconds.",
  },
  allClear: {
    id: 'documents.processingReport.outcome.allClear',
    defaultMessage: 'Completed — no issues detected',
    description:
      "Status of the extraction beside a success icon in the Extraction Outcome panel of the edit page's Processing Report: it succeeded, and the report lists no item to review and no processing issue.",
  },
  completedToReview: {
    id: 'documents.processingReport.outcome.completedToReview',
    defaultMessage: 'Completed with {count, plural, one {# item} other {# items}} to review',
    description:
      "Status of the extraction beside a warning icon in the Extraction Outcome panel of the edit page's Processing Report: it succeeded, and the report lists items to review or processing issues. {count} is the number of items to review it lists; it is 0 when only processing issues are listed.",
  },
  failed: {
    id: 'documents.processingReport.outcome.failed',
    defaultMessage: 'Failed',
    description:
      "Status of the extraction beside an error icon in the Extraction Outcome panel of the edit page's Processing Report: the section's result records that parsing the model's output did not succeed.",
  },
  dataExtracted: {
    id: 'documents.processingReport.outcome.dataExtracted',
    defaultMessage: 'Data extracted',
    description:
      "Label of the cell of the Extraction Outcome panel, in the edit page's Processing Report, that counts the data the extraction produced.",
  },
  rows: {
    id: 'documents.processingReport.outcome.rows',
    defaultMessage: '{rows, plural, one {# row} other {# rows}}',
    description:
      "Value of the Data extracted cell in the edit page's Processing Report when the extracted data has list rows and no other field with a value. {rows} is the number of rows across all list fields.",
  },
  fields: {
    id: 'documents.processingReport.outcome.fields',
    defaultMessage: '{fields, plural, one {# field} other {# fields}}',
    description:
      "Value of the Data extracted cell in the edit page's Processing Report when the extracted data has no list rows. {fields} is the number of fields with a value, list fields aside.",
  },
  rowsAndFields: {
    id: 'documents.processingReport.outcome.rowsAndFields',
    defaultMessage: '{rows, plural, one {# row} other {# rows}} + {fields, plural, one {# field} other {# fields}}',
    description:
      "Value of the Data extracted cell in the edit page's Processing Report when the extracted data has list rows and other fields with a value. {rows} is the number of rows across all list fields; {fields} is the number of the other fields with a value.",
  },
  schemaValidation: {
    id: 'documents.processingReport.outcome.schemaValidation',
    defaultMessage: 'Schema validation',
    description:
      "Label of the cell of the Extraction Outcome panel, in the edit page's Processing Report, that shows whether the extracted data satisfied the class's schema.",
  },
  validationIssues: {
    id: 'documents.processingReport.outcome.validationIssues',
    defaultMessage: '{count, plural, one {# issue} other {# issues}}',
    description:
      "Value of the Schema validation cell beside a warning icon, in the Extraction Outcome panel of the edit page's Processing Report, when the extracted data did not satisfy the schema. {count} is the number of validation errors.",
  },
  notEnabled: {
    id: 'documents.processingReport.outcome.notEnabled',
    defaultMessage: 'Not enabled',
    description:
      "Value of the Schema validation cell in the Extraction Outcome panel of the edit page's Processing Report when the section's report records no schema validation.",
  },
  model: {
    id: 'documents.processingReport.outcome.model',
    defaultMessage: 'Model',
    description:
      "Label of the cell of the Extraction Outcome panel, in the edit page's Processing Report, that names the model the section was extracted with.",
  },
  modelPerClass: {
    id: 'documents.processingReport.outcome.modelPerClass',
    defaultMessage: '{model} (per-class)',
    description:
      "Value of the Model cell in the edit page's Processing Report when the class's own model override chose the model, not the global extraction model. {model} is the model id, or N/A when the report names none.",
  },
  method: {
    id: 'documents.processingReport.outcome.method',
    defaultMessage: 'Method',
    description:
      "Label of the cell of the Extraction Outcome panel, in the edit page's Processing Report, that names the extraction method.",
  },
  methodAgentic: {
    id: 'documents.processingReport.outcome.methodAgentic',
    defaultMessage: 'Agentic',
    description: "Value of the Method cell in the edit page's Processing Report: the agentic extraction method, without the table parser.",
  },
  methodStandard: {
    id: 'documents.processingReport.outcome.methodStandard',
    defaultMessage: 'Standard',
    description: "Value of the Method cell in the edit page's Processing Report: the standard, non-agentic extraction method.",
  },
  methodAgenticWithParser: {
    id: 'documents.processingReport.outcome.methodAgenticWithParser',
    defaultMessage: 'Agentic + table parser',
    description:
      "Value of the Method cell in the edit page's Processing Report: the agentic extraction method, which also used the deterministic table parser.",
  },
  lists: {
    id: 'documents.processingReport.outcome.lists',
    defaultMessage: 'Lists: {lists}',
    description:
      "Line at the foot of the Extraction Outcome panel of the edit page's Processing Report. {lists} names each list field of the extracted data with its row count in brackets, separated by commas, such as holdings (12), fees (3).",
  },
});

/** The kinds of item to review the report can list, each at most once: a row's key never depends on the language. */
type ReviewItemKind = 'extractionFailed' | 'requiredBlank' | 'schemaInvalid' | 'possibleMissing' | 'completenessShortfall';

const reviewMessages = defineMessages<{
  header: { readonly count: number };
  item: { readonly label: string; readonly detail: string; readonly strong: MessageTag };
  extractionFailed: NoMessageValues;
  extractionFailedDetail: NoMessageValues;
  requiredBlank: NoMessageValues;
  requiredBlankDetail: { readonly count: number };
  requiredBlankDetailSample: { readonly count: number; readonly sample: string };
  requiredBlankDetailSampleMore: { readonly count: number; readonly sample: string };
  schemaInvalid: NoMessageValues;
  schemaInvalidDetail: { readonly count: number };
  schemaInvalidDetailFields: { readonly count: number; readonly fields: string };
  schemaInvalidDetailResolved: { readonly count: number };
  schemaInvalidDetailFieldsResolved: { readonly count: number; readonly fields: string };
  schemaInvalidDetailStillInvalid: { readonly count: number };
  schemaInvalidDetailFieldsStillInvalid: { readonly count: number; readonly fields: string };
  possibleMissing: NoMessageValues;
  possibleMissingDetail: { readonly populated: string; readonly defined: string; readonly ratio: string; readonly threshold: string };
  completenessShortfall: NoMessageValues;
  completenessShortfallDetail: NoMessageValues;
  noneHeader: NoMessageValues;
  noneBody: NoMessageValues;
}>({
  header: {
    id: 'documents.processingReport.review.header',
    defaultMessage: '{count, plural, one {# item} other {# items}} to review',
    description:
      "Heading of the alert in the edit page's Processing Report that lists what to review in the open section's extraction. {count} is the number of items it lists.",
  },
  item: {
    id: 'documents.processingReport.review.item',
    defaultMessage: '<strong>{label}:</strong> {detail}',
    description:
      "One item of the alert in the edit page's Processing Report that lists what to review. {label} is the item's name, such as Schema validation failed, shown in bold; {detail} explains it. <strong> makes the name bold.",
  },
  extractionFailed: {
    id: 'documents.processingReport.review.extractionFailed',
    defaultMessage: 'Extraction failed',
    description:
      "Name of an item to review in the edit page's Processing Report, listed when the section's result records that parsing the model's output did not succeed.",
  },
  extractionFailedDetail: {
    id: 'documents.processingReport.review.extractionFailedDetail',
    defaultMessage: 'The model output could not be parsed into the expected structure.',
    description: "Explanation of the Extraction failed item to review in the edit page's Processing Report.",
  },
  requiredBlank: {
    id: 'documents.processingReport.review.requiredBlank',
    defaultMessage: 'Required values left blank',
    description:
      "Name of an item to review in the edit page's Processing Report: the extraction agent left values blank because it could not read them.",
  },
  requiredBlankDetail: {
    id: 'documents.processingReport.review.requiredBlankDetail',
    defaultMessage:
      '{count, plural, one {# required value} other {# required values}} came back null — the extraction agent leaves a cell null rather than guessing when it cannot read it.',
    description:
      "Explanation of the Required values left blank item to review in the edit page's Processing Report when the report names none of the values. {count} is the number of values left blank.",
  },
  requiredBlankDetailSample: {
    id: 'documents.processingReport.review.requiredBlankDetailSample',
    defaultMessage:
      '{count, plural, one {# required value} other {# required values}} came back null — the extraction agent leaves a cell null rather than guessing when it cannot read it: {sample}.',
    description:
      "Explanation of the Required values left blank item to review in the edit page's Processing Report when the report names five of the values or fewer. {count} is the number of values left blank; {sample} lists the paths it names, separated by commas.",
  },
  requiredBlankDetailSampleMore: {
    id: 'documents.processingReport.review.requiredBlankDetailSampleMore',
    defaultMessage:
      '{count, plural, one {# required value} other {# required values}} came back null — the extraction agent leaves a cell null rather than guessing when it cannot read it: {sample}, ….',
    description:
      "Explanation of the Required values left blank item to review in the edit page's Processing Report when more than five are named. {count} is the number of values left blank; {sample} lists the paths of the first five, separated by commas, and the ellipsis stands for the rest.",
  },
  schemaInvalid: {
    id: 'documents.processingReport.review.schemaInvalid',
    defaultMessage: 'Schema validation failed',
    description:
      "Name of an item to review in the edit page's Processing Report: fields of the extracted data did not satisfy the class's schema.",
  },
  schemaInvalidDetail: {
    id: 'documents.processingReport.review.schemaInvalidDetail',
    defaultMessage: '{count, plural, one {# field} other {# fields}} did not satisfy the schema.',
    description:
      "Explanation of the Schema validation failed item to review in the edit page's Processing Report when the report names no field and no escalation ran. {count} is the number of validation errors.",
  },
  schemaInvalidDetailFields: {
    id: 'documents.processingReport.review.schemaInvalidDetailFields',
    defaultMessage: '{count, plural, one {# field} other {# fields}} did not satisfy the schema: {fields}.',
    description:
      "Explanation of the Schema validation failed item to review in the edit page's Processing Report when no escalation ran. {count} is the number of validation errors; {fields} names the fields that failed, separated by commas.",
  },
  schemaInvalidDetailResolved: {
    id: 'documents.processingReport.review.schemaInvalidDetailResolved',
    defaultMessage: '{count, plural, one {# field} other {# fields}} did not satisfy the schema (resolved by escalation).',
    description:
      "Explanation of the Schema validation failed item to review in the edit page's Processing Report when the report names no field and escalation, a re-extraction with another model, resolved the errors. {count} is the number of validation errors.",
  },
  schemaInvalidDetailFieldsResolved: {
    id: 'documents.processingReport.review.schemaInvalidDetailFieldsResolved',
    defaultMessage: '{count, plural, one {# field} other {# fields}} did not satisfy the schema: {fields} (resolved by escalation).',
    description:
      "Explanation of the Schema validation failed item to review in the edit page's Processing Report when escalation, a re-extraction with another model, resolved the errors. {count} is the number of validation errors; {fields} names the fields that failed, separated by commas.",
  },
  schemaInvalidDetailStillInvalid: {
    id: 'documents.processingReport.review.schemaInvalidDetailStillInvalid',
    defaultMessage: '{count, plural, one {# field} other {# fields}} did not satisfy the schema (escalation attempted, still invalid).',
    description:
      "Explanation of the Schema validation failed item to review in the edit page's Processing Report when the report names no field and escalation, a re-extraction with another model, left the data invalid. {count} is the number of validation errors.",
  },
  schemaInvalidDetailFieldsStillInvalid: {
    id: 'documents.processingReport.review.schemaInvalidDetailFieldsStillInvalid',
    defaultMessage:
      '{count, plural, one {# field} other {# fields}} did not satisfy the schema: {fields} (escalation attempted, still invalid).',
    description:
      "Explanation of the Schema validation failed item to review in the edit page's Processing Report when escalation, a re-extraction with another model, left the data invalid. {count} is the number of validation errors; {fields} names the fields that failed, separated by commas.",
  },
  possibleMissing: {
    id: 'documents.processingReport.review.possibleMissing',
    defaultMessage: 'Possible missing data',
    description:
      "Name of an item to review in the edit page's Processing Report: fewer of the schema's fields got a value than the configured minimum share.",
  },
  possibleMissingDetail: {
    id: 'documents.processingReport.review.possibleMissingDetail',
    defaultMessage:
      'Only {populated}/{defined} schema fields were populated ({ratio}, below the {threshold} threshold) — review for silent extraction loss.',
    description:
      "Explanation of the Possible missing data item to review in the edit page's Processing Report. {populated} is the number of the schema's fields that got a value and {defined} the number the schema defines; {ratio} is the populated share and {threshold} the configured minimum share, each as a percentage, or N/A.",
  },
  completenessShortfall: {
    id: 'documents.processingReport.review.completenessShortfall',
    defaultMessage: 'Completeness shortfall',
    description:
      "Name of an item to review in the edit page's Processing Report: list fields of the extracted data have fewer items than the schema requires.",
  },
  completenessShortfallDetail: {
    id: 'documents.processingReport.review.completenessShortfallDetail',
    defaultMessage: 'Some array fields have fewer items than the schema requires.',
    description:
      "Explanation of the Completeness shortfall item to review in the edit page's Processing Report when the report carries no summary of its own.",
  },
  noneHeader: {
    id: 'documents.processingReport.review.noneHeader',
    defaultMessage: 'No issues detected',
    description: "Heading of the success alert in the edit page's Processing Report when its checks list no item to review.",
  },
  noneBody: {
    id: 'documents.processingReport.review.noneBody',
    defaultMessage: 'Extraction completed and passed all enabled checks.',
    description: "Text of the success alert in the edit page's Processing Report when its checks list no item to review.",
  },
});

const pathMessages = defineMessages<{
  title: NoMessageValues;
  recoveryLabel: NoMessageValues;
  recoveryTruncated: { readonly calls: number; readonly splits: number };
  recovered: { readonly rows: number; readonly strong: MessageTag };
  recoveredByRetry: { readonly rows: number; readonly retried: number; readonly strong: MessageTag };
  recoveredByEscalation: { readonly rows: number; readonly escalated: number; readonly model: string; readonly strong: MessageTag };
  recoveredByBoth: {
    readonly rows: number;
    readonly retried: number;
    readonly escalated: number;
    readonly model: string;
    readonly strong: MessageTag;
  };
  recoveryStrongerModel: NoMessageValues;
  recoveryUnscored: { readonly rows: number };
  recoveryAllScored: NoMessageValues;
  recoveryDeadline: NoMessageValues;
  modelWindow: NoMessageValues;
  modelWindowValue: { readonly input: string; readonly output: string };
  contextBuffer: NoMessageValues;
  contextBufferValue: { readonly share: number };
  shardBudget: NoMessageValues;
  shardBudgetValue: { readonly budget: string; readonly pages: number | string };
  shardBudgetValueWithOverhead: { readonly budget: string; readonly pages: number | string; readonly overhead: string };
  confidenceBatch: NoMessageValues;
  confidenceBatchValue: { readonly size: number | string };
  batchesConcurrent: {
    readonly batches: number;
    readonly concurrency: number;
    readonly batchSize: string;
    readonly escalation: string;
    readonly strong: MessageTag;
  };
  batchesSequential: { readonly batches: number; readonly batchSize: string; readonly escalation: string; readonly strong: MessageTag };
  batchSize: { readonly derived: number; readonly configured: number | string };
  batchSizeUnset: NoMessageValues;
  batchEscalation: { readonly model: string };
  overrides: { readonly overrides: string };
}>({
  title: {
    id: 'documents.processingReport.path.title',
    defaultMessage: 'Processing Path',
    description:
      "Heading of the panel of the edit page's Processing Report that shows how the section was processed: its processing stages, how it was sized, split and batched, and its prompt-cache use.",
  },
  recoveryLabel: {
    id: 'documents.processingReport.path.recoveryLabel',
    defaultMessage: '⚠ Confidence auto-recovery:',
    description:
      "Bold label that starts the note, in the Processing Path panel of the edit page's Processing Report, on how confidence assessment recovered scores it first lacked. ⚠ is a warning sign.",
  },
  recoveryTruncated: {
    id: 'documents.processingReport.path.recoveryTruncated',
    defaultMessage:
      "{calls, plural, one {# confidence call} other {# confidence calls}} truncated at the model's output limit (batches split {splits}×).",
    description:
      "Sentence of the confidence auto-recovery note in the Processing Path panel of the edit page's Processing Report. {calls} is the number of confidence assessment calls whose output the model cut off at its output limit; {splits} is how many times a batch was halved and assessed again, and × means times.",
  },
  recovered: {
    id: 'documents.processingReport.path.recovered',
    defaultMessage: 'Recovered <strong>{rows}</strong> {rows, plural, one {row} other {rows}}.',
    description:
      "Sentence of the confidence auto-recovery note in the Processing Path panel of the edit page's Processing Report when no row was recovered, by a retry or by escalation. {rows} is that number, 0, shown in bold; <strong> makes it bold.",
  },
  recoveredByRetry: {
    id: 'documents.processingReport.path.recoveredByRetry',
    defaultMessage: 'Recovered <strong>{rows}</strong> {rows, plural, one {row} other {rows}} — {retried} by same-model retry.',
    description:
      "Sentence of the confidence auto-recovery note in the Processing Path panel of the edit page's Processing Report. {rows} is the number of rows whose confidence scores were recovered, shown in bold; {retried} is how many of them a retry with the same model recovered. <strong> makes the number bold.",
  },
  recoveredByEscalation: {
    id: 'documents.processingReport.path.recoveredByEscalation',
    defaultMessage: 'Recovered <strong>{rows}</strong> {rows, plural, one {row} other {rows}}, {escalated} by escalation to {model}.',
    description:
      "Sentence of the confidence auto-recovery note in the Processing Path panel of the edit page's Processing Report. {rows} is the number of rows whose confidence scores were recovered, shown in bold; {escalated} is how many of them were recovered by escalating to a stronger model, and {model} is that model's id, or the phrase a stronger model. <strong> makes the number bold.",
  },
  recoveredByBoth: {
    id: 'documents.processingReport.path.recoveredByBoth',
    defaultMessage:
      'Recovered <strong>{rows}</strong> {rows, plural, one {row} other {rows}} — {retried} by same-model retry, {escalated} by escalation to {model}.',
    description:
      "Sentence of the confidence auto-recovery note in the Processing Path panel of the edit page's Processing Report. {rows} is the number of rows whose confidence scores were recovered, shown in bold; {retried} is how many of them a retry with the same model recovered; {escalated} is how many were recovered by escalating to a stronger model, and {model} is that model's id, or the phrase a stronger model. <strong> makes the number bold.",
  },
  recoveryStrongerModel: {
    id: 'documents.processingReport.path.recoveryStrongerModel',
    defaultMessage: 'a stronger model',
    description:
      "Named in the confidence auto-recovery note of the edit page's Processing Report, in place of a model id, when the report does not name the model the rows were escalated to.",
  },
  recoveryUnscored: {
    id: 'documents.processingReport.path.recoveryUnscored',
    defaultMessage: '{rows, plural, one {# row} other {# rows}} remained unscored',
    description:
      "Status beside an error icon in the confidence auto-recovery note of the edit page's Processing Report. {rows} is the number of rows that still had no confidence score after recovery.",
  },
  recoveryAllScored: {
    id: 'documents.processingReport.path.recoveryAllScored',
    defaultMessage: 'All rows scored',
    description:
      "Status beside a success icon in the confidence auto-recovery note of the edit page's Processing Report: every row has a confidence score.",
  },
  recoveryDeadline: {
    id: 'documents.processingReport.path.recoveryDeadline',
    defaultMessage: 'Stopped early on the Lambda wall-clock guard.',
    description:
      "Sentence that ends the confidence auto-recovery note of the edit page's Processing Report when recovery stopped early to stay within the Lambda function's time budget.",
  },
  modelWindow: {
    id: 'documents.processingReport.path.modelWindow',
    defaultMessage: 'Model window',
    description:
      "Label of the cell of the Processing Path panel, in the edit page's Processing Report, with the input and output token limits of the model the section was sized for.",
  },
  modelWindowValue: {
    id: 'documents.processingReport.path.modelWindowValue',
    defaultMessage: 'in {input}K / out {output}K',
    description:
      "Value of the Model window cell in the edit page's Processing Report. {input} is the model's input token limit and {output} its output token limit, each in thousands of tokens; K stands for thousand.",
  },
  contextBuffer: {
    id: 'documents.processingReport.path.contextBuffer',
    defaultMessage: 'Context buffer',
    description:
      "Label of the cell of the Processing Path panel, in the edit page's Processing Report, with the share of the model's input window that sizing keeps free.",
  },
  contextBufferValue: {
    id: 'documents.processingReport.path.contextBufferValue',
    defaultMessage: '{share, number, ::percent} kept free',
    description: "Value of the Context buffer cell in the edit page's Processing Report. {share} is that share, as a percentage.",
  },
  shardBudget: {
    id: 'documents.processingReport.path.shardBudget',
    defaultMessage: 'Shard budget (auto)',
    description:
      "Label of the cell of the Processing Path panel, in the edit page's Processing Report, with the token budget and page limit of each shard the section's pages are split into for extraction; auto stands for automatic sizing.",
  },
  shardBudgetValue: {
    id: 'documents.processingReport.path.shardBudgetValue',
    defaultMessage: '~{budget} tok · {pages} pg/shard',
    description:
      "Value of the Shard budget cell in the edit page's Processing Report. {budget} is a shard's token budget; ~ means about, and tok stands for tokens. {pages} is the most pages a shard holds; pg/shard stands for pages per shard.",
  },
  shardBudgetValueWithOverhead: {
    id: 'documents.processingReport.path.shardBudgetValueWithOverhead',
    defaultMessage: '~{budget} tok · {pages} pg/shard · after ~{overhead} tok of prompt overhead',
    description:
      "Value of the Shard budget cell in the edit page's Processing Report when the report records the prompt's own size. {budget} is a shard's token budget; ~ means about, and tok stands for tokens. {pages} is the most pages a shard holds; pg/shard stands for pages per shard. {overhead} is the prompt's own size in tokens, about.",
  },
  confidenceBatch: {
    id: 'documents.processingReport.path.confidenceBatch',
    defaultMessage: 'Confidence batch (auto)',
    description:
      "Label of the cell of the Processing Path panel, in the edit page's Processing Report, with the number of list rows per confidence assessment batch; auto stands for automatic sizing.",
  },
  confidenceBatchValue: {
    id: 'documents.processingReport.path.confidenceBatchValue',
    defaultMessage: '{size} rows/batch',
    description: "Value of the Confidence batch cell in the edit page's Processing Report. {size} is the number of list rows per batch.",
  },
  batchesConcurrent: {
    id: 'documents.processingReport.path.batchesConcurrent',
    defaultMessage:
      'Confidence assessment ran in <strong>{batches}</strong> {batches, plural, one {batch} other {batches}} — {concurrency}-way concurrent after a cache-warming call{batchSize}{escalation}.',
    description:
      "Sentence in the Processing Path panel of the edit page's Processing Report when confidence assessment ran batches at the same time. {batches} is the number of batches, shown in bold; {concurrency} is how many ran at once, after a first call that filled the prompt cache. {batchSize} and {escalation} are clauses of this sentence, each empty or starting with a semicolon. <strong> makes the number bold.",
  },
  batchesSequential: {
    id: 'documents.processingReport.path.batchesSequential',
    defaultMessage:
      'Confidence assessment ran in <strong>{batches}</strong> {batches, plural, one {batch} other {batches}} — sequential{batchSize}{escalation}.',
    description:
      "Sentence in the Processing Path panel of the edit page's Processing Report when confidence assessment ran its batches one after another. {batches} is the number of batches, shown in bold. {batchSize} and {escalation} are clauses of this sentence, each empty or starting with a semicolon. <strong> makes the number bold.",
  },
  batchSize: {
    id: 'documents.processingReport.path.batchSize',
    defaultMessage: '; token-aware batch size {derived} (configured {configured})',
    description:
      "Clause of the confidence assessment sentence in the Processing Path panel of the edit page's Processing Report: the batch size sized from the model's token limits. {derived} is that size; {configured} is the batch size the configuration sets, or n/a.",
  },
  batchSizeUnset: {
    id: 'documents.processingReport.path.batchSizeUnset',
    defaultMessage: 'n/a',
    description:
      "Shown in the batch size clause of the confidence assessment sentence, in the edit page's Processing Report, in place of the configured batch size when the report records none.",
  },
  batchEscalation: {
    id: 'documents.processingReport.path.batchEscalation',
    defaultMessage: '; escalated to {model}',
    description:
      "Clause of the confidence assessment sentence in the Processing Path panel of the edit page's Processing Report: rows were escalated to another model. {model} is that model's id.",
  },
  overrides: {
    id: 'documents.processingReport.path.overrides',
    defaultMessage: 'Manual size overrides in effect: {overrides}',
    description:
      "Line in the Processing Path panel of the edit page's Processing Report when the configuration sets sizes by hand. {overrides} is those settings as JSON, shown as written.",
  },
});

const issueMessages = defineMessages<{
  header: { readonly count: number };
  fallback: NoMessageValues;
  rootCause: { readonly rootCause: string };
}>({
  header: {
    id: 'documents.processingReport.issues.header',
    defaultMessage: '{count, plural, one {# processing issue} other {# processing issues}}',
    description:
      "Heading of the alert in the edit page's Processing Report that lists the open section's processing issues, each with its code and the backend's message. {count} is the number of issues.",
  },
  fallback: {
    id: 'documents.processingReport.issues.fallback',
    defaultMessage: 'issue',
    description:
      "Shown in the processing issues of the edit page's Processing Report in place of an issue's code when the issue has neither a code nor a processing stage.",
  },
  rootCause: {
    id: 'documents.processingReport.issues.rootCause',
    defaultMessage: 'Root cause: {rootCause}',
    description:
      "Line under a processing issue's message in the edit page's Processing Report. {rootCause} is the cause the backend recorded, in English.",
  },
});

const detailMessages = defineMessages<{
  title: NoMessageValues;
  validationTitle: NoMessageValues;
  result: NoMessageValues;
  violationCount: { readonly count: number };
  onFailure: NoMessageValues;
  formatChecks: NoMessageValues;
  formatChecksOn: NoMessageValues;
  formatChecksOff: NoMessageValues;
  escalated: {
    readonly model: string;
    readonly scope: string;
    readonly outcome: string;
    readonly errors: string;
    readonly strong: MessageTag;
  };
  escalationStrongerModel: NoMessageValues;
  scopeFields: { readonly fields: string };
  scopeNoFields: NoMessageValues;
  scopeFullSection: NoMessageValues;
  outcomeRejected: NoMessageValues;
  outcomeResolved: NoMessageValues;
  outcomeStillInvalid: NoMessageValues;
  errorCounts: { readonly before: string; readonly after: number };
  violations: { readonly count: number };
  rootPath: NoMessageValues;
  populationTitle: NoMessageValues;
  populationSummary: { readonly populated: number | string; readonly defined: number; readonly ratio: string };
  populationBelow: { readonly threshold: string };
  populationOk: NoMessageValues;
  emptyFields: { readonly count: number };
  tablesTitle: NoMessageValues;
  parserUsed: NoMessageValues;
  parserNotUsed: NoMessageValues;
  parserNotNeeded: NoMessageValues;
  ocrRows: NoMessageValues;
  rowsParsed: NoMessageValues;
  parseSuccessRate: NoMessageValues;
  avgOcrConfidence: NoMessageValues;
  estimateNote: { readonly estimate: string; readonly rows: number };
  parserWarnings: { readonly count: number };
  shortfallsTitle: NoMessageValues;
  rawReport: NoMessageValues;
}>({
  title: {
    id: 'documents.processingReport.details.title',
    defaultMessage: 'Details & diagnostics',
    description:
      "Heading of the expandable part of the edit page's Processing Report that holds the details of schema validation, field population, table extraction and completeness, and the raw report.",
  },
  validationTitle: {
    id: 'documents.processingReport.details.validationTitle',
    defaultMessage: 'Schema Validation & Escalation',
    description:
      "Heading of the schema validation details under Details & diagnostics in the edit page's Processing Report. Escalation is the re-extraction of the section, or of its failing fields, with another model.",
  },
  result: {
    id: 'documents.processingReport.details.result',
    defaultMessage: 'Result',
    description:
      "Label of the cell with the schema validation result, under Schema Validation & Escalation in the edit page's Processing Report.",
  },
  violationCount: {
    id: 'documents.processingReport.details.violationCount',
    defaultMessage: '{count, plural, one {# violation} other {# violations}}',
    description:
      "Value of the Result cell beside a warning icon, under Schema Validation & Escalation in the edit page's Processing Report. {count} is the number of validation errors.",
  },
  onFailure: {
    id: 'documents.processingReport.details.onFailure',
    defaultMessage: 'On failure',
    description:
      "Label of the cell, under Schema Validation & Escalation in the edit page's Processing Report, with what the configuration does when schema validation fails; the value is the setting as configured.",
  },
  formatChecks: {
    id: 'documents.processingReport.details.formatChecks',
    defaultMessage: 'Format checks',
    description:
      "Label of the cell, under Schema Validation & Escalation in the edit page's Processing Report, that says whether schema validation also checked value formats.",
  },
  formatChecksOn: {
    id: 'documents.processingReport.details.formatChecksOn',
    defaultMessage: 'On',
    description: "Value of the Format checks cell in the edit page's Processing Report: schema validation checked value formats.",
  },
  formatChecksOff: {
    id: 'documents.processingReport.details.formatChecksOff',
    defaultMessage: 'Off',
    description: "Value of the Format checks cell in the edit page's Processing Report: schema validation did not check value formats.",
  },
  escalated: {
    id: 'documents.processingReport.details.escalated',
    defaultMessage: 'Escalated to <strong>{model}</strong> ({scope}) — {outcome}{errors}.',
    description:
      "Sentence under Schema Validation & Escalation in the edit page's Processing Report when the section was re-extracted with another model. {model} is that model's id, or the phrase stronger model, shown in bold; {scope} says what was re-extracted; {outcome} says how it ended; {errors} is empty or a clause, starting with a semicolon, with the error counts before and after. <strong> makes the model bold. The backend's own explanation, in English, may follow the sentence.",
  },
  escalationStrongerModel: {
    id: 'documents.processingReport.details.escalationStrongerModel',
    defaultMessage: 'stronger model',
    description:
      "Shown in bold in the escalation sentence of the edit page's Processing Report, in place of a model id, when the report does not name the model the section was re-extracted with.",
  },
  scopeFields: {
    id: 'documents.processingReport.details.scopeFields',
    defaultMessage: 'fields: {fields}',
    description:
      "What escalation re-extracted, in brackets in the escalation sentence of the edit page's Processing Report: some of the section's fields. {fields} names them, separated by commas.",
  },
  scopeNoFields: {
    id: 'documents.processingReport.details.scopeNoFields',
    defaultMessage: 'fields: none',
    description:
      "What escalation re-extracted, in brackets in the escalation sentence of the edit page's Processing Report: some of the section's fields, of which the report names none.",
  },
  scopeFullSection: {
    id: 'documents.processingReport.details.scopeFullSection',
    defaultMessage: 'full section',
    description:
      "What escalation re-extracted, in brackets in the escalation sentence of the edit page's Processing Report: the whole section.",
  },
  outcomeRejected: {
    id: 'documents.processingReport.details.outcomeRejected',
    defaultMessage: 'rejected, original kept',
    description:
      "How escalation ended, in the escalation sentence of the edit page's Processing Report: its result was rejected and the original extraction kept.",
  },
  outcomeResolved: {
    id: 'documents.processingReport.details.outcomeResolved',
    defaultMessage: 'resolved',
    description:
      "How escalation ended, in the escalation sentence of the edit page's Processing Report: it resolved the validation errors.",
  },
  outcomeStillInvalid: {
    id: 'documents.processingReport.details.outcomeStillInvalid',
    defaultMessage: 'still invalid',
    description:
      "How escalation ended, in the escalation sentence of the edit page's Processing Report: the data still did not satisfy the schema.",
  },
  errorCounts: {
    id: 'documents.processingReport.details.errorCounts',
    defaultMessage: '; errors {before} → {after}',
    description:
      "Clause at the end of the escalation sentence in the edit page's Processing Report. {before} is the number of validation errors before escalation and {after} the number after it.",
  },
  violations: {
    id: 'documents.processingReport.details.violations',
    defaultMessage: 'Violations ({count})',
    description:
      "Heading of the expandable list of schema validation errors, each a field path and the validator's message, in the edit page's Processing Report. {count} is the number of errors.",
  },
  rootPath: {
    id: 'documents.processingReport.details.rootPath',
    defaultMessage: '(root)',
    description:
      "Shown in bold in the list of schema validation errors of the edit page's Processing Report, in place of a field path, when an error has no path: it concerns the extracted data as a whole.",
  },
  populationTitle: {
    id: 'documents.processingReport.details.populationTitle',
    defaultMessage: 'Field Population (completeness heuristic)',
    description:
      "Heading of the field population details under Details & diagnostics in the edit page's Processing Report: how many of the schema's fields got a value, as an estimate of completeness.",
  },
  populationSummary: {
    id: 'documents.processingReport.details.populationSummary',
    defaultMessage: '{populated}/{defined} fields populated ({ratio}) ·',
    description:
      "Line of the field population details in the edit page's Processing Report, followed by the population status. {populated} is the number of the schema's fields that got a value and {defined} the number the schema defines; {ratio} is the populated share as a percentage, or N/A.",
  },
  populationBelow: {
    id: 'documents.processingReport.details.populationBelow',
    defaultMessage: 'below {threshold} threshold',
    description:
      "Population status beside a warning icon, after the field population line of the edit page's Processing Report: the populated share is below the configured minimum. {threshold} is that minimum as a percentage, or N/A.",
  },
  populationOk: {
    id: 'documents.processingReport.details.populationOk',
    defaultMessage: 'OK',
    description:
      "Population status beside a success icon, after the field population line of the edit page's Processing Report: the populated share is not below the configured minimum.",
  },
  emptyFields: {
    id: 'documents.processingReport.details.emptyFields',
    defaultMessage: 'Empty fields ({count})',
    description:
      "Heading of the expandable list of the schema's fields that got no value, in the field population details of the edit page's Processing Report. {count} is the number listed.",
  },
  tablesTitle: {
    id: 'documents.processingReport.details.tablesTitle',
    defaultMessage: 'Table Extraction',
    description:
      "Heading of the table extraction details under Details & diagnostics in the edit page's Processing Report, shown for a section extracted by the agentic method.",
  },
  parserUsed: {
    id: 'documents.processingReport.details.parserUsed',
    defaultMessage:
      'The deterministic table parser was used (parses tables from OCR text instead of having the model regenerate every row).',
    description: "Table extraction note in the edit page's Processing Report when the deterministic table parser was used for the section.",
  },
  parserNotUsed: {
    id: 'documents.processingReport.details.parserNotUsed',
    defaultMessage: 'Large tables were detected but the deterministic table parser was not used for this section.',
    description:
      "Table extraction note, shown as a warning, in the edit page's Processing Report when the OCR text had large tables, the table parser was not used, and the report gives no reason.",
  },
  parserNotNeeded: {
    id: 'documents.processingReport.details.parserNotNeeded',
    defaultMessage: 'No large tables detected in the OCR text; the deterministic table parser was not needed.',
    description: "Table extraction note in the edit page's Processing Report when the OCR text had no large tables.",
  },
  ocrRows: {
    id: 'documents.processingReport.details.ocrRows',
    defaultMessage: 'Table rows seen in OCR (estimate)',
    description:
      "Label of the cell, in the table extraction details of the edit page's Processing Report, with the number of table rows estimated from the OCR text before extraction.",
  },
  rowsParsed: {
    id: 'documents.processingReport.details.rowsParsed',
    defaultMessage: 'Rows parsed by tool',
    description:
      "Label of the cell, in the table extraction details of the edit page's Processing Report, with the number of rows the deterministic table parser parsed.",
  },
  parseSuccessRate: {
    id: 'documents.processingReport.details.parseSuccessRate',
    defaultMessage: 'Parse success rate',
    description:
      "Label of the cell, in the table extraction details of the edit page's Processing Report, with the deterministic table parser's success rate, as a percentage.",
  },
  avgOcrConfidence: {
    id: 'documents.processingReport.details.avgOcrConfidence',
    defaultMessage: 'Avg OCR confidence',
    description:
      "Label of the cell, in the table extraction details of the edit page's Processing Report, with the average OCR confidence of the parsed tables, as a percentage.",
  },
  estimateNote: {
    id: 'documents.processingReport.details.estimateNote',
    defaultMessage:
      'The OCR estimate (~{estimate}) is a pre-extraction guess and may differ from the {rows, plural, one {# row} other {# rows}} actually extracted (the authoritative count, shown in Document Data).',
    description:
      "Note in the table extraction details of the edit page's Processing Report. {estimate} is the number of table rows estimated from the OCR text before extraction; {rows} is the number of list rows the extraction produced. Document Data names the root of the section's extracted data on the same page.",
  },
  parserWarnings: {
    id: 'documents.processingReport.details.parserWarnings',
    defaultMessage: 'Parser warnings ({count})',
    description:
      "Heading of the expandable list of the deterministic table parser's warnings, shown in English as the parser writes them, in the edit page's Processing Report. {count} is the number of warnings.",
  },
  shortfallsTitle: {
    id: 'documents.processingReport.details.shortfallsTitle',
    defaultMessage: 'Completeness Shortfalls',
    description:
      "Heading of the list, under Details & diagnostics in the edit page's Processing Report, of the list fields with fewer items than the schema requires, each with the backend's message in English.",
  },
  rawReport: {
    id: 'documents.processingReport.details.rawReport',
    defaultMessage: 'Raw processing report (text)',
    description:
      "Heading of the expandable part of Details & diagnostics in the edit page's Processing Report that shows the processing report as the backend wrote it, in English.",
  },
});

/** Renders a `<strong>...</strong>` element value. */
const strong = (chunks: React.ReactNode[]): React.JSX.Element => <strong>{chunks}</strong>;

// Count the items the extraction actually produced: total list rows across all
// array fields (e.g. holdings_positions) plus populated scalar fields. This is
// the authoritative result, distinct from the pre-flight OCR row estimate.
function summarizeResult(inferenceResult?: Record<string, unknown>): { listRows: number; scalarFields: number; listFields: string[] } {
  let listRows = 0;
  let scalarFields = 0;
  const listFields: string[] = [];
  if (inferenceResult && typeof inferenceResult === 'object') {
    Object.entries(inferenceResult).forEach(([key, value]) => {
      if (Array.isArray(value)) {
        listRows += value.length;
        listFields.push(`${key} (${value.length})`);
      } else if (value !== null && value !== undefined && value !== '') {
        scalarFields += 1;
      }
    });
  }
  return { listRows, scalarFields, listFields };
}

/**
 * Data-driven process-flow visual for the Processing Path section. Renders the
 * backend's `processing_flow.stages` left-to-right (works for both simple and
 * advanced), colors each stage by status, and stacks fanned-out stages (sharded
 * extract / concurrent confidence) to convey parallelism. Uses recorded counts
 * only (no fabricated per-inference timings).
 */
// Per-status flow-stage styling. Uses Cloudscape CSS custom properties (with hex
// fallbacks, the established pattern in this app) so the boxes adapt to dark /
// high-contrast modes, and pairs each status with a text MARK so status is never
// conveyed by color alone (accessibility). `ok` is neutral (no mark).
const STAGE_TONE: Record<string, { border: string; bg: string; mark: string }> = {
  ok: {
    border: 'var(--color-border-divider-default, #b6bec9)',
    bg: 'var(--color-background-container-content, #ffffff)',
    mark: '',
  },
  info: {
    border: 'var(--color-border-status-info, #0972d3)',
    bg: 'var(--color-background-status-info, #f0f8ff)',
    mark: '● ',
  },
  warning: {
    border: 'var(--color-border-status-warning, #f89256)',
    bg: 'var(--color-background-status-warning, #fff7f0)',
    mark: '⚠ ',
  },
  skipped: {
    border: 'var(--color-border-divider-secondary, #d5dbdb)',
    bg: 'var(--color-background-container-content, #fbfbfb)',
    mark: '– ',
  },
};

/** Every stage box's height: room for its label and a detail line. */
const STAGE_HEIGHT = 44;

const StageBox: React.FC<{ label: string; sub?: string; status?: string; fanout?: number }> = ({ label, sub, status = 'ok', fanout }) => {
  const tone = STAGE_TONE[status] || STAGE_TONE.ok;
  // A boolean, not `fanout && …`: the backend records `fanout: 0` for a single pass,
  // and `{0 && …}` renders the number, a stray "0" above the stage.
  const parallel = (fanout ?? 0) > 1;
  return (
    <div style={{ position: 'relative', flex: '0 0 auto' }}>
      {/* Stacked "shadow" cards convey fan-out (sharded extract / concurrent confidence). */}
      {parallel && (
        <>
          <div
            style={{
              position: 'absolute',
              top: 4,
              left: 4,
              right: -4,
              bottom: -4,
              border: `1px solid ${tone.border}`,
              borderRadius: 8,
              background: tone.bg,
              opacity: 0.5,
            }}
          />
          <div
            style={{
              position: 'absolute',
              top: 2,
              left: 2,
              right: -2,
              bottom: -2,
              border: `1px solid ${tone.border}`,
              borderRadius: 8,
              background: tone.bg,
              opacity: 0.75,
            }}
          />
        </>
      )}
      <div
        style={{
          position: 'relative',
          border: `1px solid ${tone.border}`,
          borderRadius: 8,
          padding: '6px 10px',
          background: tone.bg,
          minWidth: 96,
          // One height for every stage, with or without a detail line, so the boxes and
          // the arrows between them share one centre line.
          minHeight: STAGE_HEIGHT,
          boxSizing: 'border-box',
          display: 'flex',
          flexDirection: 'column',
          justifyContent: 'center',
          textAlign: 'center',
          whiteSpace: 'nowrap',
          opacity: status === 'skipped' ? 0.6 : 1,
        }}
      >
        <div style={{ fontSize: 12, fontWeight: 600 }}>
          {tone.mark}
          {label}
          {parallel ? ` ×${fanout}` : ''}
        </div>
        {sub && <div style={{ fontSize: 11, color: '#5f6b7a' }}>{sub}</div>}
      </div>
    </div>
  );
};

const Arrow: React.FC = () => (
  <div aria-hidden style={{ flex: '0 0 auto', color: '#5f6b7a', lineHeight: 1 }}>
    →
  </div>
);

// Data-driven flow: renders whatever stages the backend recorded (works for both
// simple and advanced), coloring each by status and stacking fanned-out stages. It is
// one row, never wrapped: a wrapped flow strands an arrow at the end of a line and
// reads as two flows. A flow wider than its panel scrolls sideways instead.
const ProcessFlow: React.FC<{ stages: FlowStage[] }> = ({ stages }) => (
  <div
    data-testid="process-flow"
    style={{ display: 'flex', flexWrap: 'nowrap', alignItems: 'center', gap: 8, overflowX: 'auto', padding: '4px 4px 8px' }}
  >
    {stages.map((st, i) => (
      <React.Fragment key={st.key || i}>
        {i > 0 && <Arrow />}
        <StageBox label={st.label || '?'} sub={st.detail} status={st.status} fanout={st.fanout} />
      </React.Fragment>
    ))}
  </div>
);

const ProcessingReportTab: React.FC<ProcessingReportTabProps> = ({ metadata, processingReport, inferenceResult, processingIssues }) => {
  const intl = useIntl();
  const { formatFixed, formatNumber, formatPercent } = useFormatters();
  if (!metadata) {
    return (
      <Box padding="l" textAlign="center" color="text-status-inactive">
        <FormattedMessage {...messages.notAvailable} />
      </Box>
    );
  }

  const notAvailable = intl.formatMessage(messages.valueNotAvailable);
  // A share as a whole percentage, or N/A. Rounded first, as the percentage always was: a percent
  // format alone would round the ratio's decimal form (0.145 would read 15%, not 14%).
  const pct = (n: number | undefined): string =>
    n === undefined || n === null || Number.isNaN(n) ? notAvailable : intl.formatNumber(Math.round(n * 100) / 100, { style: 'percent' });

  const isAgentic = (metadata.extraction_method || '').toLowerCase() === 'agentic';
  const succeeded = metadata.parsing_succeeded !== false;
  const validation = metadata.validation;
  const populationCheck = metadata.population_check;
  const completenessCheck = metadata.completeness_check || {};
  const stats = metadata.table_parsing_stats;
  const tableToolUsed = metadata.table_parsing_tool_used === true;

  const { listRows, scalarFields, listFields } = summarizeResult(inferenceResult);

  // Item 3: how the document was sized/split/batched (model-aware auto-sizing).
  const sizing = metadata.sizing_plan;
  const batchStats = metadata.assessment_batch_split_stats;
  const promptCache = metadata.prompt_cache;
  const promptCacheText = promptCache ? describePromptCache(promptCache, {}, intl) : null;
  // Systematic flow (both simple and advanced) + explicit auto-recovery detail.
  const flow = metadata.processing_flow;
  const flowStages = flow?.stages || [];
  const recovery = flow?.recovery;

  // ---- Build the list of issues to surface up top (plain language) ----
  // Each keeps its kind, which keys its row: the label is in the UI's language.
  const issues: { kind: ReviewItemKind; label: string; detail: string }[] = [];
  if (!succeeded) {
    issues.push({
      kind: 'extractionFailed',
      label: intl.formatMessage(reviewMessages.extractionFailed),
      detail: intl.formatMessage(reviewMessages.extractionFailedDetail),
    });
  }
  const abstained = metadata.abstained_fields;
  if (abstained && (abstained.count || 0) > 0) {
    const count = abstained.count || 0;
    const sample = (abstained.paths || []).slice(0, 5).join(', ');
    issues.push({
      kind: 'requiredBlank',
      label: intl.formatMessage(reviewMessages.requiredBlank),
      detail: !sample
        ? intl.formatMessage(reviewMessages.requiredBlankDetail, { count })
        : (abstained.paths || []).length > 5
          ? intl.formatMessage(reviewMessages.requiredBlankDetailSampleMore, { count, sample })
          : intl.formatMessage(reviewMessages.requiredBlankDetailSample, { count, sample }),
    });
  }
  if (validation && validation.valid === false) {
    const count = validation.error_count || 0;
    const fields = (validation.failed_fields || []).join(', ');
    let detail: string;
    if (!validation.escalated) {
      detail = fields
        ? intl.formatMessage(reviewMessages.schemaInvalidDetailFields, { count, fields })
        : intl.formatMessage(reviewMessages.schemaInvalidDetail, { count });
    } else if (validation.resolved_by_escalation) {
      detail = fields
        ? intl.formatMessage(reviewMessages.schemaInvalidDetailFieldsResolved, { count, fields })
        : intl.formatMessage(reviewMessages.schemaInvalidDetailResolved, { count });
    } else {
      detail = fields
        ? intl.formatMessage(reviewMessages.schemaInvalidDetailFieldsStillInvalid, { count, fields })
        : intl.formatMessage(reviewMessages.schemaInvalidDetailStillInvalid, { count });
    }
    issues.push({ kind: 'schemaInvalid', label: intl.formatMessage(reviewMessages.schemaInvalid), detail });
  }
  if (populationCheck?.below_threshold) {
    issues.push({
      kind: 'possibleMissing',
      label: intl.formatMessage(reviewMessages.possibleMissing),
      detail: intl.formatMessage(reviewMessages.possibleMissingDetail, {
        populated: String(populationCheck.fields_populated),
        defined: String(populationCheck.fields_defined),
        ratio: pct(populationCheck.population_ratio),
        threshold: pct(populationCheck.threshold),
      }),
    });
  }
  if (completenessCheck.schema_constraints_met === false && (completenessCheck.violations || []).length > 0) {
    issues.push({
      kind: 'completenessShortfall',
      label: intl.formatMessage(reviewMessages.completenessShortfall),
      detail: completenessCheck.summary || intl.formatMessage(reviewMessages.completenessShortfallDetail),
    });
  }

  // Structured self-healing issues (backend ProcessingIssue spine). Worst
  // severity drives the block's Alert type.
  const structuredIssues = processingIssues || [];
  const hasStructuredError = structuredIssues.some((i) => (i.severity || 'info').toLowerCase() === 'error');
  const hasStructuredWarning = structuredIssues.some((i) => (i.severity || 'info').toLowerCase() === 'warning');
  const structuredAlertType: 'error' | 'warning' | 'info' = hasStructuredError ? 'error' : hasStructuredWarning ? 'warning' : 'info';

  // Overall verdict
  const allClear = issues.length === 0 && structuredIssues.length === 0 && succeeded;

  // The recovery note's sentence on the rows it recovered: one whole message for each mix of
  // same-model retry and escalation.
  let recoveredRows: React.ReactNode = null;
  if (recovery) {
    const retried = recovery.rows_recovered_by_retry || 0;
    const escalated = recovery.rows_recovered_by_escalation || 0;
    const rows = retried + escalated;
    const model = recovery.escalation_model || intl.formatMessage(pathMessages.recoveryStrongerModel);
    if (retried > 0 && escalated > 0) {
      recoveredRows = <FormattedMessage {...pathMessages.recoveredByBoth} values={{ rows, retried, escalated, model, strong }} />;
    } else if (retried > 0) {
      recoveredRows = <FormattedMessage {...pathMessages.recoveredByRetry} values={{ rows, retried, strong }} />;
    } else if (escalated > 0) {
      recoveredRows = <FormattedMessage {...pathMessages.recoveredByEscalation} values={{ rows, escalated, model, strong }} />;
    } else {
      recoveredRows = <FormattedMessage {...pathMessages.recovered} values={{ rows, strong }} />;
    }
  }
  // The optional clauses of the confidence assessment sentence, each empty or whole.
  const batchSizeClause = batchStats?.derived_batch_size
    ? intl.formatMessage(pathMessages.batchSize, {
        derived: batchStats.derived_batch_size,
        configured: batchStats.configured_batch_size ?? intl.formatMessage(pathMessages.batchSizeUnset),
      })
    : '';
  const batchEscalationClause = batchStats?.escalation_model
    ? intl.formatMessage(pathMessages.batchEscalation, { model: batchStats.escalation_model })
    : '';

  return (
    <SpaceBetween size="l">
      {/* ---- Outcome (top-line verdict) ---- */}
      <Container
        header={
          <Header
            variant="h2"
            description={
              metadata.extraction_time_seconds
                ? intl.formatMessage(isAgentic ? messages.agenticTimed : messages.standardTimed, {
                    seconds: formatFixed(metadata.extraction_time_seconds, 1),
                  })
                : intl.formatMessage(isAgentic ? messages.agentic : messages.standard)
            }
          >
            <FormattedMessage {...messages.outcomeTitle} />
          </Header>
        }
      >
        <SpaceBetween size="m">
          <Box>
            <StatusIndicator type={allClear ? 'success' : succeeded ? 'warning' : 'error'}>
              {allClear ? (
                <FormattedMessage {...messages.allClear} />
              ) : succeeded ? (
                <FormattedMessage {...messages.completedToReview} values={{ count: issues.length }} />
              ) : (
                <FormattedMessage {...messages.failed} />
              )}
            </StatusIndicator>
          </Box>

          <ColumnLayout columns={4} variant="text-grid">
            {inferenceResult && (
              <div>
                <Box variant="awsui-key-label">
                  <FormattedMessage {...messages.dataExtracted} />
                </Box>
                <Box>
                  {listRows > 0 && scalarFields > 0 ? (
                    <FormattedMessage {...messages.rowsAndFields} values={{ rows: listRows, fields: scalarFields }} />
                  ) : listRows > 0 ? (
                    <FormattedMessage {...messages.rows} values={{ rows: listRows }} />
                  ) : (
                    <FormattedMessage {...messages.fields} values={{ fields: scalarFields }} />
                  )}
                </Box>
              </div>
            )}
            <div>
              <Box variant="awsui-key-label">
                <FormattedMessage {...messages.schemaValidation} />
              </Box>
              <Box>
                {validation ? (
                  <StatusIndicator type={validation.valid ? 'success' : 'warning'}>
                    {validation.valid ? (
                      <FormattedMessage {...messages.valid} />
                    ) : (
                      <FormattedMessage {...messages.validationIssues} values={{ count: validation.error_count || 0 }} />
                    )}
                  </StatusIndicator>
                ) : (
                  <Box color="text-status-inactive">
                    <FormattedMessage {...messages.notEnabled} />
                  </Box>
                )}
              </Box>
            </div>
            <div>
              <Box variant="awsui-key-label">
                <FormattedMessage {...messages.model} />
              </Box>
              <Box>
                {metadata.extraction_model_overridden ? (
                  <FormattedMessage {...messages.modelPerClass} values={{ model: metadata.extraction_model || notAvailable }} />
                ) : (
                  metadata.extraction_model || notAvailable
                )}
              </Box>
            </div>
            <div>
              <Box variant="awsui-key-label">
                <FormattedMessage {...messages.method} />
              </Box>
              <Box>
                {!isAgentic ? (
                  <FormattedMessage {...messages.methodStandard} />
                ) : tableToolUsed ? (
                  <FormattedMessage {...messages.methodAgenticWithParser} />
                ) : (
                  <FormattedMessage {...messages.methodAgentic} />
                )}
              </Box>
            </div>
          </ColumnLayout>

          {listFields.length > 0 && (
            <Box fontSize="body-s" color="text-status-inactive">
              <FormattedMessage {...messages.lists} values={{ lists: listFields.join(', ') }} />
            </Box>
          )}
        </SpaceBetween>
      </Container>

      {/* ---- Processing path: how the doc was sized / split / batched ---- */}
      {(sizing || batchStats || promptCache || flowStages.length > 0) && (
        <Container
          header={
            <Header variant="h2">
              <FormattedMessage {...pathMessages.title} />
            </Header>
          }
        >
          <SpaceBetween size="m">
            {/* Systematic flow graph (rendered for BOTH simple and advanced):
                OCR → Classify → Extract(→shards) → [Table tool] → [Escalation] →
                Confidence(→batches) → Geometry — each stage colored by status. */}
            {flowStages.length > 0 && <ProcessFlow stages={flowStages} />}
            {/* Explicit "what failed and was recovered by retry" callout. */}
            {recovery && (
              <Box fontSize="body-s" padding="xs" color="text-body-secondary" variant="p">
                <strong>
                  <FormattedMessage {...pathMessages.recoveryLabel} />
                </strong>{' '}
                {(recovery.truncated_calls || 0) > 0 && (
                  <>
                    <FormattedMessage
                      {...pathMessages.recoveryTruncated}
                      values={{ calls: recovery.truncated_calls || 0, splits: recovery.splits || 0 }}
                    />{' '}
                  </>
                )}
                {recoveredRows}{' '}
                {(recovery.unrecoverable_rows || 0) > 0 ? (
                  <StatusIndicator type="error">
                    <FormattedMessage {...pathMessages.recoveryUnscored} values={{ rows: recovery.unrecoverable_rows || 0 }} />
                  </StatusIndicator>
                ) : (
                  <StatusIndicator type="success">
                    <FormattedMessage {...pathMessages.recoveryAllScored} />
                  </StatusIndicator>
                )}
                {recovery.deadline_reached && (
                  <>
                    {' '}
                    <FormattedMessage {...pathMessages.recoveryDeadline} />
                  </>
                )}
              </Box>
            )}
            {sizing && (
              <ColumnLayout columns={4} variant="text-grid">
                <div>
                  <Box variant="awsui-key-label">
                    <FormattedMessage {...pathMessages.modelWindow} />
                  </Box>
                  <Box>
                    <FormattedMessage
                      {...pathMessages.modelWindowValue}
                      values={{
                        input: formatNumber((sizing.max_input_tokens || 0) / 1000),
                        output: formatNumber((sizing.max_output_tokens || 0) / 1000),
                      }}
                    />
                  </Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">
                    <FormattedMessage {...pathMessages.contextBuffer} />
                  </Box>
                  <Box>
                    {/* Rounded first, as the percentage always was. */}
                    <FormattedMessage
                      {...pathMessages.contextBufferValue}
                      values={{ share: Math.round((sizing.context_buffer || 0) * 100) / 100 }}
                    />
                  </Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">
                    <FormattedMessage {...pathMessages.shardBudget} />
                  </Box>
                  <Box>
                    {sizing.prompt_overhead_tokens ? (
                      <FormattedMessage
                        {...pathMessages.shardBudgetValueWithOverhead}
                        values={{
                          budget: formatNumber(sizing.shard_token_budget || 0),
                          pages: sizing.max_pages_per_shard ?? '',
                          overhead: formatNumber(sizing.prompt_overhead_tokens),
                        }}
                      />
                    ) : (
                      <FormattedMessage
                        {...pathMessages.shardBudgetValue}
                        values={{ budget: formatNumber(sizing.shard_token_budget || 0), pages: sizing.max_pages_per_shard ?? '' }}
                      />
                    )}
                  </Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">
                    <FormattedMessage {...pathMessages.confidenceBatch} />
                  </Box>
                  <Box>
                    <FormattedMessage {...pathMessages.confidenceBatchValue} values={{ size: sizing.list_batch_size ?? '' }} />
                  </Box>
                </div>
              </ColumnLayout>
            )}
            {batchStats && batchStats.batch_count ? (
              <Box fontSize="body-s" color="text-body-secondary">
                {batchStats.concurrent_batches && batchStats.concurrent_batches > 1 ? (
                  <FormattedMessage
                    {...pathMessages.batchesConcurrent}
                    values={{
                      batches: batchStats.batch_count,
                      concurrency: batchStats.concurrent_batches,
                      batchSize: batchSizeClause,
                      escalation: batchEscalationClause,
                      strong,
                    }}
                  />
                ) : (
                  <FormattedMessage
                    {...pathMessages.batchesSequential}
                    values={{ batches: batchStats.batch_count, batchSize: batchSizeClause, escalation: batchEscalationClause, strong }}
                  />
                )}
              </Box>
            ) : null}
            {promptCacheText && (
              <Box fontSize="body-s">
                <StatusIndicator type={promptCacheText.indicator}>{promptCacheText.headline}</StatusIndicator>
                <Box fontSize="body-s" color="text-body-secondary">
                  {promptCacheText.detail}
                </Box>
              </Box>
            )}
            {sizing?.overrides && Object.keys(sizing.overrides).length > 0 && (
              <Box fontSize="body-s" color="text-status-inactive">
                <FormattedMessage {...pathMessages.overrides} values={{ overrides: JSON.stringify(sizing.overrides) }} />
              </Box>
            )}
          </SpaceBetween>
        </Container>
      )}

      {/* ---- Structured self-healing issues (ProcessingIssue spine) ---- */}
      {structuredIssues.length > 0 && (
        <Alert type={structuredAlertType} header={intl.formatMessage(issueMessages.header, { count: structuredIssues.length })}>
          <SpaceBetween size="xs">
            {structuredIssues.map((iss, idx) => {
              const rootCause = iss.rootCause || iss.root_cause;
              return (
                <Box key={`${iss.code ?? 'issue'}-${iss.message?.slice(0, 24) ?? idx}`}>
                  <StatusIndicator
                    type={
                      (iss.severity || 'info').toLowerCase() === 'error'
                        ? 'error'
                        : (iss.severity || 'info').toLowerCase() === 'warning'
                          ? 'warning'
                          : 'info'
                    }
                  >
                    {iss.code ? issueCodeLabel(iss.code, intl) : iss.stage || intl.formatMessage(issueMessages.fallback)}
                  </StatusIndicator>{' '}
                  {iss.message}
                  {rootCause && (
                    <Box fontSize="body-s" color="text-body-secondary">
                      <FormattedMessage {...issueMessages.rootCause} values={{ rootCause }} />
                    </Box>
                  )}
                </Box>
              );
            })}
          </SpaceBetween>
        </Alert>
      )}

      {/* ---- Issues / all-clear ---- */}
      {issues.length > 0 ? (
        <Alert type={succeeded ? 'warning' : 'error'} header={intl.formatMessage(reviewMessages.header, { count: issues.length })}>
          <SpaceBetween size="xs">
            {issues.map((iss) => (
              <Box key={iss.kind}>
                <FormattedMessage {...reviewMessages.item} values={{ label: iss.label, detail: iss.detail, strong }} />
              </Box>
            ))}
          </SpaceBetween>
        </Alert>
      ) : (
        <Alert type="success" header={intl.formatMessage(reviewMessages.noneHeader)}>
          <FormattedMessage {...reviewMessages.noneBody} />
        </Alert>
      )}

      {/* ---- Details (collapsed by default) ---- */}
      <ExpandableSection variant="container" headerText={intl.formatMessage(detailMessages.title)} defaultExpanded={issues.length > 0}>
        <SpaceBetween size="l">
          {/* Schema validation detail */}
          {validation && (
            <div>
              <Box variant="awsui-key-label">
                <FormattedMessage {...detailMessages.validationTitle} />
              </Box>
              <ColumnLayout columns={3} variant="text-grid">
                <div>
                  <Box variant="awsui-key-label">
                    <FormattedMessage {...detailMessages.result} />
                  </Box>
                  <Box>
                    <StatusIndicator type={validation.valid ? 'success' : 'warning'}>
                      {validation.valid ? (
                        <FormattedMessage {...messages.valid} />
                      ) : (
                        <FormattedMessage {...detailMessages.violationCount} values={{ count: validation.error_count || 0 }} />
                      )}
                    </StatusIndicator>
                  </Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">
                    <FormattedMessage {...detailMessages.onFailure} />
                  </Box>
                  <Box>{validation.fail_action || notAvailable}</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">
                    <FormattedMessage {...detailMessages.formatChecks} />
                  </Box>
                  <Box>
                    {validation.check_formats ? (
                      <FormattedMessage {...detailMessages.formatChecksOn} />
                    ) : (
                      <FormattedMessage {...detailMessages.formatChecksOff} />
                    )}
                  </Box>
                </div>
              </ColumnLayout>
              {validation.escalated && (
                <Box padding={{ top: 'xs' }} fontSize="body-s">
                  <FormattedMessage
                    {...detailMessages.escalated}
                    values={{
                      model: validation.escalation_model || intl.formatMessage(detailMessages.escalationStrongerModel),
                      scope:
                        validation.escalation_scope === 'field-subset'
                          ? (validation.escalation_fields || []).join(', ')
                            ? intl.formatMessage(detailMessages.scopeFields, { fields: (validation.escalation_fields || []).join(', ') })
                            : intl.formatMessage(detailMessages.scopeNoFields)
                          : intl.formatMessage(detailMessages.scopeFullSection),
                      outcome:
                        validation.escalation_kept === false
                          ? intl.formatMessage(detailMessages.outcomeRejected)
                          : validation.resolved_by_escalation
                            ? intl.formatMessage(detailMessages.outcomeResolved)
                            : intl.formatMessage(detailMessages.outcomeStillInvalid),
                      errors:
                        validation.initial_error_count !== undefined
                          ? intl.formatMessage(detailMessages.errorCounts, {
                              before: String(validation.initial_error_count),
                              after: validation.error_count || 0,
                            })
                          : '',
                      strong,
                    }}
                  />
                  {validation.escalation_decision ? ` ${validation.escalation_decision}.` : ''}
                </Box>
              )}
              {!validation.valid && validation.errors && validation.errors.length > 0 && (
                <Box padding={{ top: 'xs' }}>
                  <ExpandableSection
                    headerText={intl.formatMessage(detailMessages.violations, {
                      count: validation.error_count || validation.errors.length,
                    })}
                  >
                    <SpaceBetween size="xxs">
                      {validation.errors.map((e) => (
                        <Box key={`verr-${e.path}-${e.validator}-${e.message}`} fontSize="body-s">
                          <strong>{e.path || intl.formatMessage(detailMessages.rootPath)}</strong>: {e.message}
                        </Box>
                      ))}
                    </SpaceBetween>
                  </ExpandableSection>
                </Box>
              )}
            </div>
          )}

          {/* Field population */}
          {populationCheck && populationCheck.fields_defined !== undefined && (
            <div>
              <Box variant="awsui-key-label">
                <FormattedMessage {...detailMessages.populationTitle} />
              </Box>
              <Box>
                <FormattedMessage
                  {...detailMessages.populationSummary}
                  values={{
                    populated: populationCheck.fields_populated ?? '',
                    defined: populationCheck.fields_defined,
                    ratio: pct(populationCheck.population_ratio),
                  }}
                />{' '}
                <StatusIndicator type={populationCheck.below_threshold ? 'warning' : 'success'}>
                  {populationCheck.below_threshold ? (
                    <FormattedMessage {...detailMessages.populationBelow} values={{ threshold: pct(populationCheck.threshold) }} />
                  ) : (
                    <FormattedMessage {...detailMessages.populationOk} />
                  )}
                </StatusIndicator>
              </Box>
              {populationCheck.empty_fields && populationCheck.empty_fields.length > 0 && (
                <Box padding={{ top: 'xs' }}>
                  <ExpandableSection
                    headerText={intl.formatMessage(detailMessages.emptyFields, { count: populationCheck.empty_fields.length })}
                  >
                    <Box fontSize="body-s">{populationCheck.empty_fields.join(', ')}</Box>
                  </ExpandableSection>
                </Box>
              )}
            </div>
          )}

          {/* Table parsing */}
          {isAgentic && (metadata.ocr_analysis || stats) && (
            <div>
              <Box variant="awsui-key-label">
                <FormattedMessage {...detailMessages.tablesTitle} />
              </Box>
              {(() => {
                const decision = metadata.tool_usage_decision;
                const recommended =
                  metadata.ocr_analysis?.tool_usage_recommended || metadata.ocr_analysis?.recommendation_strength === 'MANDATORY';
                // Prefer the backend's explicit, reasoned explanation when the tool
                // was recommended but not used — it states WHY (disabled / no
                // Markdown tables in OCR / agent declined) rather than just "not used".
                let text: string;
                let warn = false;
                if (tableToolUsed) {
                  text = intl.formatMessage(detailMessages.parserUsed);
                } else if (recommended && decision?.explanation) {
                  text = decision.explanation;
                  // Only a genuine "agent declined an available tool" is worth a warning tint.
                  warn = decision.tool_enabled !== false && decision.ocr_had_markdown_tables !== false;
                } else if (recommended) {
                  text = intl.formatMessage(detailMessages.parserNotUsed);
                  warn = true;
                } else {
                  text = intl.formatMessage(detailMessages.parserNotNeeded);
                }
                return (
                  <Box fontSize="body-s" padding={{ bottom: 'xs' }} color={warn ? 'text-status-warning' : 'text-body-secondary'}>
                    {warn ? '⚠ ' : ''}
                    {text}
                  </Box>
                );
              })()}
              <ColumnLayout columns={3} variant="text-grid">
                {metadata.ocr_analysis?.estimated_row_count !== undefined && (
                  <div>
                    <Box variant="awsui-key-label">
                      <FormattedMessage {...detailMessages.ocrRows} />
                    </Box>
                    <Box>~{formatNumber(metadata.ocr_analysis.estimated_row_count)}</Box>
                  </div>
                )}
                {stats && (
                  <>
                    <div>
                      <Box variant="awsui-key-label">
                        <FormattedMessage {...detailMessages.rowsParsed} />
                      </Box>
                      <Box>{formatNumber(stats.rows_parsed || 0)}</Box>
                    </div>
                    <div>
                      <Box variant="awsui-key-label">
                        <FormattedMessage {...detailMessages.parseSuccessRate} />
                      </Box>
                      <Box>{stats.parse_success_rate !== undefined ? pct(stats.parse_success_rate) : notAvailable}</Box>
                    </div>
                    {stats.confidence_available && stats.avg_confidence !== undefined && (
                      <div>
                        <Box variant="awsui-key-label">
                          <FormattedMessage {...detailMessages.avgOcrConfidence} />
                        </Box>
                        <Box>{formatPercent(stats.avg_confidence / 100, { fractionDigits: 1 })}</Box>
                      </div>
                    )}
                  </>
                )}
              </ColumnLayout>
              {inferenceResult && listRows > 0 && metadata.ocr_analysis?.estimated_row_count !== undefined && (
                <Box padding={{ top: 'xs' }} fontSize="body-s" color="text-status-inactive">
                  <FormattedMessage
                    {...detailMessages.estimateNote}
                    values={{ estimate: formatNumber(metadata.ocr_analysis.estimated_row_count), rows: listRows }}
                  />
                </Box>
              )}
              {stats?.warnings && stats.warnings.length > 0 && (
                <Box padding={{ top: 'xs' }}>
                  <ExpandableSection headerText={intl.formatMessage(detailMessages.parserWarnings, { count: stats.warnings.length })}>
                    <SpaceBetween size="xxs">
                      {stats.warnings.map((w) => (
                        <Box key={`warn-${w}`} fontSize="body-s">
                          • {w}
                        </Box>
                      ))}
                    </SpaceBetween>
                  </ExpandableSection>
                </Box>
              )}
            </div>
          )}

          {/* Completeness violations (minItems shortfalls) */}
          {completenessCheck.violations && completenessCheck.violations.length > 0 && (
            <div>
              <Box variant="awsui-key-label">
                <FormattedMessage {...detailMessages.shortfallsTitle} />
              </Box>
              <SpaceBetween size="xxs">
                {completenessCheck.violations.map((v) => (
                  <Box key={`cv-${v.field}`} fontSize="body-s">
                    <strong>{v.field}</strong>: {v.message}
                  </Box>
                ))}
              </SpaceBetween>
            </div>
          )}

          {/* Raw processing report text, if present */}
          {processingReport && (
            <ExpandableSection headerText={intl.formatMessage(detailMessages.rawReport)}>
              <Box padding="s" variant="code">
                <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{processingReport}</pre>
              </Box>
            </ExpandableSection>
          )}
        </SpaceBetween>
      </ExpandableSection>
    </SpaceBetween>
  );
};

export default ProcessingReportTab;
