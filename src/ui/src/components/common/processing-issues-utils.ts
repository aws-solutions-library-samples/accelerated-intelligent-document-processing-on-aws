// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Helpers for the structured self-healing "processing issues" surfaced on each
 * section (see backend `ProcessingIssue`). Mirrors the conventions in
 * confidence-alerts-utils.ts and hitl-status-renderer.tsx.
 */

import type { StatusIndicatorProps } from '@cloudscape-design/components';

import type { ProcessingIssue } from '../../types/documents';

// `ProcessingIssue` is derived from the generated GraphQL type (see
// types/documents.ts) and re-exported here so existing importers keep using the
// nearby path.
export type { ProcessingIssue };

export interface SectionWithIssues {
  ProcessingIssues?: ProcessingIssue[] | null;
}

/**
 * Codes that mean the stage RAISED, as opposed to flagging a result the pipeline
 * still accepted. Both are error severity, so severity alone cannot tell them
 * apart, and the difference is the one an operator acts on first: a failed
 * section has no trustworthy result, while a flagged one does and was kept.
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
 * Severity alone cannot carry this. Four backend codes are `info`, and only this
 * one reports a repair: the other three — `extraction_sparse`
 * ("possible silent extraction loss"), `extraction_off_schema_fields` (fields
 * dropped before assessment) and `assessment_geometry_incomplete` (scored fields
 * with no bounding box) — all report something *missing*. Labelling those
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

/**
 * Reduce a section's issues to a single Cloudscape StatusIndicator type +
 * label, worst-severity-wins:
 *   error   -> "error"   ("Failed" when the stage raised, else "Incomplete")
 *   warning -> "warning" ("Degraded")
 *   info    -> "info"    ("Auto-recovered" only when every note is a recovery,
 *                         else "Info" — most `info` notes report something
 *                         missing, not something repaired)
 *   none    -> "success" ("OK")
 */
export const getSectionIssueStatus = (
  section: SectionWithIssues | null | undefined,
): { type: StatusIndicatorProps.Type; label: string; count: number } => {
  const issues = section?.ProcessingIssues || [];
  if (!Array.isArray(issues) || issues.length === 0) {
    return { type: 'success', label: 'OK', count: 0 };
  }
  const severities = new Set(issues.map((i) => (i.severity || 'info').toLowerCase()));
  if (severities.has('error')) {
    const failed = issues.some((i) => FAILURE_CODES.has((i.code || '').toLowerCase()));
    return { type: 'error', label: failed ? 'Failed' : 'Incomplete', count: issues.length };
  }
  if (severities.has('warning')) {
    return { type: 'warning', label: 'Degraded', count: issues.length };
  }
  // "Auto-recovered" only when EVERY note is a recovery — not when merely one is.
  // A section carrying both a repair and a sparse-extraction note has something
  // still worth reading, so the neutral label is the honest summary and the
  // recovery stays visible in the message and the Processing Report tab.
  const allRecovered = issues.every((i) => RECOVERY_CODES.has((i.code || '').toLowerCase()));
  return { type: 'info', label: allRecovered ? 'Auto-recovered' : 'Info', count: issues.length };
};

/** True when any section in the list carries at least one processing issue. */
export const documentHasProcessingIssues = (sections: SectionWithIssues[] | null | undefined): boolean =>
  Array.isArray(sections) && sections.some((s) => (s.ProcessingIssues?.length ?? 0) > 0);

/** Total processing-issue count across a document's sections. */
export const getDocumentProcessingIssueCount = (sections: SectionWithIssues[] | null | undefined): number =>
  Array.isArray(sections) ? sections.reduce((total, s) => total + (s.ProcessingIssues?.length ?? 0), 0) : 0;
