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
 * Reduce a section's issues to a single Cloudscape StatusIndicator type +
 * label, worst-severity-wins:
 *   error   -> "error"   ("Failed" when the stage raised, else "Incomplete")
 *   warning -> "warning" ("Degraded")
 *   info    -> "info"    ("Auto-recovered")
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
  return { type: 'info', label: 'Auto-recovered', count: issues.length };
};

/** True when any section in the list carries at least one processing issue. */
export const documentHasProcessingIssues = (sections: SectionWithIssues[] | null | undefined): boolean =>
  Array.isArray(sections) && sections.some((s) => (s.ProcessingIssues?.length ?? 0) > 0);

/** Total processing-issue count across a document's sections. */
export const getDocumentProcessingIssueCount = (sections: SectionWithIssues[] | null | undefined): number =>
  Array.isArray(sections) ? sections.reduce((total, s) => total + (s.ProcessingIssues?.length ?? 0), 0) : 0;
