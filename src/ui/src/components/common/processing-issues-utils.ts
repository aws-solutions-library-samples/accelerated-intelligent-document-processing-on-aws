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
 * Reduce a section's issues to a single Cloudscape StatusIndicator type +
 * label, worst-severity-wins:
 *   error   -> "error"   ("Failed" / "Incomplete")
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
    return { type: 'error', label: 'Incomplete', count: issues.length };
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
