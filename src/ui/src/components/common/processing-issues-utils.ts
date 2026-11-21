// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Helpers for the structured self-healing "processing issues" surfaced on each
 * section (see backend `ProcessingIssue`). Mirrors the conventions in
 * confidence-alerts-utils.ts and hitl-status-renderer.tsx.
 */

import type { StatusIndicatorProps } from '@cloudscape-design/components';
import { defineMessages, type IntlShape, type NoMessageValues, type TypedMessageDescriptor } from 'react-intl';

import type { ProcessingIssue } from '../../types/documents';

// `ProcessingIssue` is derived from the generated GraphQL type (see
// types/documents.ts) and re-exported here so existing importers keep using the
// nearby path.
export type { ProcessingIssue };

export interface SectionWithIssues {
  ProcessingIssues?: ProcessingIssue[] | null;
}

/**
 * Codes that mean the stage FAILED for the section, as opposed to flagging a
 * result the pipeline still accepted. Most of them are recorded as the stage
 * raises; `extraction_failed` is also recorded without raising when Amazon
 * Bedrock's content filter withheld the extraction reply, and the section then
 * holds an empty result. Both kinds are error severity, so severity alone cannot
 * tell them apart, and the difference is the one an operator acts on first: a
 * failed section has no trustworthy result, while a flagged one does and was kept.
 *
 * This list is a literal in a different language from the backend that produces
 * the codes, so nothing about adding a code there would make it appear here. The
 * backend declares its own set (`FAILURE_CODES` in `idp_common.document_failure`
 * and `EXTRACTION_FAILED_CODE` in `idp_common.extraction.failure`) and
 * `scripts/tests/test_failure_code_ui_parity.py` fails when the two disagree in
 * either direction — a missing code would otherwise render a raised stage as the
 * milder "Incomplete" with every test green.
 */
const FAILURE_CODES = new Set([
  'extraction_failed',
  'rule_validation_failed',
  'rule_validation_not_consolidated',
  'section_processing_failed',
]);

/**
 * Codes at `info` severity that mean something was actually RECOVERED.
 *
 * Severity alone cannot carry this. Only `assessment_recovered_with_retries`
 * reports a repair; every other `info` code reports something *missing* —
 * `extraction_sparse` ("possible silent extraction loss"),
 * `extraction_off_schema_fields` (fields dropped before assessment),
 * `extraction_row_count_unverified` (no OCR evidence to check a row count
 * against) and `assessment_geometry_incomplete` (scored fields with no bounding
 * box). The gate owns the enumeration. Labelling those
 * "Auto-recovered" tells an operator the opposite of what happened, and the two
 * extraction codes are the ones most worth looking at, so the mislabel lands
 * where it costs most. `extraction_sparse` is also by far the most common issue
 * in practice, which makes the wrong reading the usual one.
 *
 * Enumerating what IS recovery rather than what is not keeps this set at one
 * entry and makes a new backend `info` code default to the neutral label instead
 * of silently claiming a repair. `scripts/tests/test_info_code_ui_parity.py`
 * fails if the backend grows an `info` code that no side classifies.
 */
const RECOVERY_CODES = new Set(['assessment_recovered_with_retries']);

/** The section statuses of the Document Sections table's Status column. */
const sectionStatusMessages = defineMessages<{
  ok: NoMessageValues;
  failed: NoMessageValues;
  incomplete: NoMessageValues;
  degraded: NoMessageValues;
  autoRecovered: NoMessageValues;
  info: NoMessageValues;
}>({
  ok: {
    id: 'documents.sectionStatus.ok',
    defaultMessage: 'OK',
    description: 'Status of a section in the Status column of the Document Sections table: the section has no processing issue.',
  },
  failed: {
    id: 'documents.sectionStatus.failed',
    defaultMessage: 'Failed',
    description:
      'Status of a section in the Status column of the Document Sections table: a processing stage failed for the section, so it has no trustworthy result.',
  },
  incomplete: {
    id: 'documents.sectionStatus.incomplete',
    defaultMessage: 'Incomplete',
    description:
      "Status of a section in the Status column of the Document Sections table: an error-severity processing issue flags the section's result, which was kept.",
  },
  degraded: {
    id: 'documents.sectionStatus.degraded',
    defaultMessage: 'Degraded',
    description:
      "Status of a section in the Status column of the Document Sections table: the section's most severe processing issue is a warning.",
  },
  autoRecovered: {
    id: 'documents.sectionStatus.autoRecovered',
    defaultMessage: 'Auto-recovered',
    description:
      'Status of a section in the Status column of the Document Sections table: every processing issue of the section is informational and reports something recovered.',
  },
  info: {
    id: 'documents.sectionStatus.info',
    defaultMessage: 'Info',
    description:
      'Status of a section in the Status column of the Document Sections table: its processing issues are informational, and at least one reports something other than a recovery.',
  },
});

/** The label in the UI's language; without `intl`, the English source text. */
const labelOf = (message: TypedMessageDescriptor<NoMessageValues>, intl?: IntlShape): string =>
  intl ? intl.formatMessage(message) : String(message.defaultMessage);

/**
 * Reduce a section's issues to a single Cloudscape StatusIndicator type +
 * label, worst-severity-wins:
 *   error   -> "error"   ("Failed" when the stage raised, else "Incomplete")
 *   warning -> "warning" ("Degraded")
 *   info    -> "info"    ("Auto-recovered" only when every note is a recovery,
 *                         else "Info" — most `info` notes report something
 *                         missing, not something repaired)
 *   none    -> "success" ("OK")
 * The label is in the UI's language with `intl`, and English without it.
 */
export const getSectionIssueStatus = (
  section: SectionWithIssues | null | undefined,
  intl?: IntlShape,
): { type: StatusIndicatorProps.Type; label: string; count: number } => {
  const issues = section?.ProcessingIssues || [];
  if (!Array.isArray(issues) || issues.length === 0) {
    return { type: 'success', label: labelOf(sectionStatusMessages.ok, intl), count: 0 };
  }
  const severities = new Set(issues.map((i) => (i.severity || 'info').toLowerCase()));
  if (severities.has('error')) {
    const failed = issues.some((i) => FAILURE_CODES.has((i.code || '').toLowerCase()));
    return {
      type: 'error',
      label: labelOf(failed ? sectionStatusMessages.failed : sectionStatusMessages.incomplete, intl),
      count: issues.length,
    };
  }
  if (severities.has('warning')) {
    return { type: 'warning', label: labelOf(sectionStatusMessages.degraded, intl), count: issues.length };
  }
  // "Auto-recovered" only when EVERY note is a recovery — not when merely one is.
  // A section carrying both a repair and a sparse-extraction note has something
  // still worth reading, so the neutral label is the honest summary and the
  // recovery stays visible in the message and the Processing Report tab.
  const allRecovered = issues.every((i) => RECOVERY_CODES.has((i.code || '').toLowerCase()));
  return {
    type: 'info',
    label: labelOf(allRecovered ? sectionStatusMessages.autoRecovered : sectionStatusMessages.info, intl),
    count: issues.length,
  };
};

/** True when any section in the list carries at least one processing issue. */
export const documentHasProcessingIssues = (sections: SectionWithIssues[] | null | undefined): boolean =>
  Array.isArray(sections) && sections.some((s) => (s.ProcessingIssues?.length ?? 0) > 0);

/** Total processing-issue count across a document's sections. */
export const getDocumentProcessingIssueCount = (sections: SectionWithIssues[] | null | undefined): number =>
  Array.isArray(sections) ? sections.reduce((total, s) => total + (s.ProcessingIssues?.length ?? 0), 0) : 0;

// One label per code the backend raises on a section's processing issues, keyed by the code as
// stored. Its English is the code itself, so English shows the stored code; a translation may give
// a short readable label. A code raised only on the document itself has no entry, because the
// Document Details panel shows the document's own issues by their message alone. The backend keeps
// no single list of its codes, so `scripts/tests/test_issue_code_ui_parity.py` derives them from
// its `ProcessingIssue(...)` calls and fails when they and this map's keys differ, in either
// direction.
const issueCodeMessages = defineMessages<Record<string, NoMessageValues>>({
  assessment_confidence_out_of_range: {
    id: 'documents.processingIssue.code.assessmentConfidenceOutOfRange',
    defaultMessage: 'assessment_confidence_out_of_range',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by confidence assessment when the model returned confidence values outside the range 0 to 1. English is the code itself; translate it as a short readable label.",
  },
  assessment_coverage_incomplete: {
    id: 'documents.processingIssue.code.assessmentCoverageIncomplete',
    defaultMessage: 'assessment_coverage_incomplete',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by confidence assessment when some or all of a section's extracted list rows carry no confidence score. English is the code itself; translate it as a short readable label.",
  },
  assessment_deadline_reached: {
    id: 'documents.processingIssue.code.assessmentDeadlineReached',
    defaultMessage: 'assessment_deadline_reached',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by confidence assessment when its self-healing stopped early to stay within the Lambda time budget. English is the code itself; translate it as a short readable label.",
  },
  assessment_failed_confidence_unavailable: {
    id: 'documents.processingIssue.code.assessmentFailedConfidenceUnavailable',
    defaultMessage: 'assessment_failed_confidence_unavailable',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by confidence assessment when it failed for a section: the extracted values were kept but have no confidence scores. English is the code itself; translate it as a short readable label.",
  },
  assessment_fields_unscored: {
    id: 'documents.processingIssue.code.assessmentFieldsUnscored',
    defaultMessage: 'assessment_fields_unscored',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by confidence assessment when populated fields of a section carry no confidence score. English is the code itself; translate it as a short readable label.",
  },
  assessment_geometry_incomplete: {
    id: 'documents.processingIssue.code.assessmentGeometryIncomplete',
    defaultMessage: 'assessment_geometry_incomplete',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by confidence assessment when scored fields have no bounding box. English is the code itself; translate it as a short readable label.",
  },
  assessment_incomplete: {
    id: 'documents.processingIssue.code.assessmentIncomplete',
    defaultMessage: 'assessment_incomplete',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by confidence assessment when list rows could not be confidence-scored, so those rows have no confidence. English is the code itself; translate it as a short readable label.",
  },
  assessment_pages_missing: {
    id: 'documents.processingIssue.code.assessmentPagesMissing',
    defaultMessage: 'assessment_pages_missing',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by confidence assessment when some of a section's pages are not in the document, so confidence was assessed without their text or image. English is the code itself; translate it as a short readable label.",
  },
  assessment_recovered_with_retries: {
    id: 'documents.processingIssue.code.assessmentRecoveredWithRetries',
    defaultMessage: 'assessment_recovered_with_retries',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by confidence assessment when scores it first lacked were recovered by retrying: rows with smaller batches or a stronger model, fields by asking again. English is the code itself; translate it as a short readable label.",
  },
  assessment_row_too_large: {
    id: 'documents.processingIssue.code.assessmentRowTooLarge',
    defaultMessage: 'assessment_row_too_large',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by confidence assessment when a single list row is too large for the confidence model's output, so those rows have no confidence score. English is the code itself; translate it as a short readable label.",
  },
  assessment_schema_mismatch: {
    id: 'documents.processingIssue.code.assessmentSchemaMismatch',
    defaultMessage: 'assessment_schema_mismatch',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by confidence assessment when extraction returned list data for fields the class schema does not define as lists, so those rows cannot be confidence-scored. English is the code itself; translate it as a short readable label.",
  },
  assessment_skipped_confidence_unavailable: {
    id: 'documents.processingIssue.code.assessmentSkippedConfidenceUnavailable',
    defaultMessage: 'assessment_skipped_confidence_unavailable',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by confidence assessment when it did not run for a section that carried nothing to assess, so the section's values have no confidence scores. English is the code itself; translate it as a short readable label.",
  },
  assessment_truncated: {
    id: 'documents.processingIssue.code.assessmentTruncated',
    defaultMessage: 'assessment_truncated',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by confidence assessment when the confidence model truncated its output, so the truncated call scored no rows. English is the code itself; translate it as a short readable label.",
  },
  classification_confidence_missing: {
    id: 'documents.processingIssue.code.classificationConfidenceMissing',
    defaultMessage: 'classification_confidence_missing',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by classification when confidence was requested but the model's reply gave none for a section, so the section's class has no score. English is the code itself; translate it as a short readable label.",
  },
  classification_failed: {
    id: 'documents.processingIssue.code.classificationFailed',
    defaultMessage: 'classification_failed',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by classification when it produced no class for some of a section's pages. English is the code itself; translate it as a short readable label.",
  },
  classification_invalid_class_fallback: {
    id: 'documents.processingIssue.code.classificationInvalidClassFallback',
    defaultMessage: 'classification_invalid_class_fallback',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by classification when the model returned a class outside the configured classes for some pages after every retry, so the configured fallback class was assigned. English is the code itself; translate it as a short readable label.",
  },
  classification_page_assigned_to_neighbour: {
    id: 'documents.processingIssue.code.classificationPageAssignedToNeighbour',
    defaultMessage: 'classification_page_assigned_to_neighbour',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by classification when the model placed pages in no document, so they were added to the neighbouring document and given its class. English is the code itself; translate it as a short readable label.",
  },
  classification_page_no_content: {
    id: 'documents.processingIssue.code.classificationPageNoContent',
    defaultMessage: 'classification_page_no_content',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by classification when pages had nothing to classify: no usable OCR text and no page image. English is the code itself; translate it as a short readable label.",
  },
  classification_page_order_not_applied: {
    id: 'documents.processingIssue.code.classificationPageOrderNotApplied',
    defaultMessage: 'classification_page_order_not_applied',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by classification when pages were classified in scan order instead of the order of their printed page numbers. English is the code itself; translate it as a short readable label.",
  },
  extraction_class_not_configured: {
    id: 'documents.processingIssue.code.extractionClassNotConfigured',
    defaultMessage: 'extraction_class_not_configured',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by extraction when a section's class does not exist in the configuration the document was processed under, so nothing was extracted from the section. English is the code itself; translate it as a short readable label.",
  },
  extraction_failed: {
    id: 'documents.processingIssue.code.extractionFailed',
    defaultMessage: 'extraction_failed',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by extraction when extraction failed for a section, including when a content filter withheld the model's reply. English is the code itself; translate it as a short readable label.",
  },
  extraction_incomplete: {
    id: 'documents.processingIssue.code.extractionIncomplete',
    defaultMessage: 'extraction_incomplete',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by extraction when it returned no rows for list fields. English is the code itself; translate it as a short readable label.",
  },
  extraction_list_field_not_a_list: {
    id: 'documents.processingIssue.code.extractionListFieldNotAList',
    defaultMessage: 'extraction_list_field_not_a_list',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by extraction when list fields came back as something other than a list, so none of their rows is available. English is the code itself; translate it as a short readable label.",
  },
  extraction_list_truncated: {
    id: 'documents.processingIssue.code.extractionListTruncated',
    defaultMessage: 'extraction_list_truncated',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by extraction when list fields returned fewer rows than the schema's minItems requires, which very likely means the extraction was truncated. English is the code itself; translate it as a short readable label.",
  },
  extraction_multi_instance_detected: {
    id: 'documents.processingIssue.code.extractionMultiInstanceDetected',
    defaultMessage: 'extraction_multi_instance_detected',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by extraction when a section contains several separate documents of its class: all were extracted, but only the first is shown as the section's result. English is the code itself; translate it as a short readable label.",
  },
  extraction_multi_instance_suspected: {
    id: 'documents.processingIssue.code.extractionMultiInstanceSuspected',
    defaultMessage: 'extraction_multi_instance_suspected',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by extraction when a section's pages appear to contain more records of its class than extraction returned, so some records are not in the result. English is the code itself; translate it as a short readable label.",
  },
  extraction_off_schema_fields: {
    id: 'documents.processingIssue.code.extractionOffSchemaFields',
    defaultMessage: 'extraction_off_schema_fields',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by extraction when it returned fields the class schema does not define, which were dropped before assessment. English is the code itself; translate it as a short readable label.",
  },
  extraction_output_truncated: {
    id: 'documents.processingIssue.code.extractionOutputTruncated',
    defaultMessage: 'extraction_output_truncated',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by extraction when the model's response was cut off before it finished, so the result holds only the part that was emitted. English is the code itself; translate it as a short readable label.",
  },
  extraction_partial_row_shortfall: {
    id: 'documents.processingIssue.code.extractionPartialRowShortfall',
    defaultMessage: 'extraction_partial_row_shortfall',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by extraction when a list field holds fewer rows than the section's OCR tables suggest, but at least half of them: a shortfall to confirm rather than a certain loss. English is the code itself; translate it as a short readable label.",
  },
  extraction_row_count_unverified: {
    id: 'documents.processingIssue.code.extractionRowCountUnverified',
    defaultMessage: 'extraction_row_count_unverified',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by extraction when the section's OCR text holds too few matching table rows to check a list field's row count, so the count is unverified. English is the code itself; translate it as a short readable label.",
  },
  extraction_rows_below_ocr_estimate: {
    id: 'documents.processingIssue.code.extractionRowsBelowOcrEstimate',
    defaultMessage: 'extraction_rows_below_ocr_estimate',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by extraction when a list field holds fewer than half the rows the section's OCR tables suggest, so the list is likely truncated. English is the code itself; translate it as a short readable label.",
  },
  extraction_sparse: {
    id: 'documents.processingIssue.code.extractionSparse',
    defaultMessage: 'extraction_sparse',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by extraction when the share of the schema's fields that were populated is below a threshold: a possible silent extraction loss. English is the code itself; translate it as a short readable label.",
  },
  extraction_validation_failed: {
    id: 'documents.processingIssue.code.extractionValidationFailed',
    defaultMessage: 'extraction_validation_failed',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by extraction when schema violations remain in a section's extracted data. English is the code itself; translate it as a short readable label.",
  },
  rule_validation_failed: {
    id: 'documents.processingIssue.code.ruleValidationFailed',
    defaultMessage: 'rule_validation_failed',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by rule validation when it did not complete for a section, so the section has no compliance verdict. English is the code itself; translate it as a short readable label.",
  },
  rule_validation_not_consolidated: {
    id: 'documents.processingIssue.code.ruleValidationNotConsolidated',
    defaultMessage: 'rule_validation_not_consolidated',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by rule validation when a section was validated but combining the document's results into one compliance decision failed, so no verdict was recorded for it. English is the code itself; translate it as a short readable label.",
  },
  section_processing_failed: {
    id: 'documents.processingIssue.code.sectionProcessingFailed',
    defaultMessage: 'section_processing_failed',
    description:
      "Processing issue code, shown above the issue's message in the Processing issues popover of the Document Sections table. Raised by the collate step when it finds that a section's processing failed, so the section's extracted data should not be treated as complete. English is the code itself; translate it as a short readable label.",
  },
});

/**
 * A processing issue's code as its label: the map's message for a code it holds, looked up as
 * stored, and the code as it is for one it does not. In the UI's language with `intl`, English
 * without.
 */
export const issueCodeLabel = (code: string, intl?: IntlShape): string =>
  Object.hasOwn(issueCodeMessages, code) ? labelOf(issueCodeMessages[code], intl) : code;
