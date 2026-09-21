// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The Sections panel's Status column has to separate a section that FAILED from
 * one that was flagged and kept.
 *
 * Both are error severity, so severity alone cannot tell them apart. The
 * distinction is the one an operator acts on first: a failed section has no
 * trustworthy result, while a flagged one produced a result that was kept
 * deliberately. `extraction_failed` is the code that carries it, and it is
 * written alongside a detection issue rather than replacing it — so a section
 * failed by `extraction.row_shortfall_action: fail` arrives with both
 * `extraction_rows_below_ocr_estimate` and `extraction_failed`, and rendering it
 * as merely "Incomplete" would collapse exactly the distinction the two issues
 * exist to draw.
 */

import { describe, expect, it } from 'vitest';

import { documentHasProcessingIssues, getDocumentProcessingIssueCount, getSectionIssueStatus } from '../processing-issues-utils';

const issue = (severity: string, code: string) => ({ severity, code, stage: 'extraction', message: code });

describe('getSectionIssueStatus', () => {
  it('reports OK for a section with no issues', () => {
    expect(getSectionIssueStatus({ ProcessingIssues: [] })).toEqual({ type: 'success', label: 'OK', count: 0 });
    expect(getSectionIssueStatus(null)).toEqual({ type: 'success', label: 'OK', count: 0 });
    expect(getSectionIssueStatus({})).toEqual({ type: 'success', label: 'OK', count: 0 });
  });

  it('labels a raised extraction failure "Failed"', () => {
    const status = getSectionIssueStatus({ ProcessingIssues: [issue('error', 'extraction_failed')] });
    expect(status.type).toBe('error');
    expect(status.label).toBe('Failed');
    expect(status.count).toBe(1);
  });

  it('labels an error-severity detection that was NOT a failure "Incomplete"', () => {
    const status = getSectionIssueStatus({
      ProcessingIssues: [issue('error', 'extraction_rows_below_ocr_estimate')],
    });
    expect(status.type).toBe('error');
    expect(status.label).toBe('Incomplete');
  });

  it('prefers "Failed" when a failure and a detection arrive together', () => {
    // The shape a section failed by row_shortfall_action: fail actually has.
    const status = getSectionIssueStatus({
      ProcessingIssues: [issue('error', 'extraction_rows_below_ocr_estimate'), issue('error', 'extraction_failed')],
    });
    expect(status.label).toBe('Failed');
    expect(status.count).toBe(2);
  });

  it('is not confused by issue order', () => {
    const status = getSectionIssueStatus({
      ProcessingIssues: [issue('error', 'extraction_failed'), issue('error', 'extraction_rows_below_ocr_estimate')],
    });
    expect(status.label).toBe('Failed');
  });

  it('matches the code case-insensitively', () => {
    expect(getSectionIssueStatus({ ProcessingIssues: [issue('error', 'EXTRACTION_FAILED')] }).label).toBe('Failed');
  });

  it('keeps the warning and info rungs unchanged', () => {
    expect(getSectionIssueStatus({ ProcessingIssues: [issue('warning', 'extraction_incomplete')] })).toEqual({
      type: 'warning',
      label: 'Degraded',
      count: 1,
    });
    expect(getSectionIssueStatus({ ProcessingIssues: [issue('info', 'extraction_sparse')] })).toEqual({
      type: 'info',
      label: 'Auto-recovered',
      count: 1,
    });
  });

  it('lets the worst severity win, and a failure outranks a warning', () => {
    const status = getSectionIssueStatus({
      ProcessingIssues: [issue('warning', 'extraction_incomplete'), issue('error', 'extraction_failed')],
    });
    expect(status.type).toBe('error');
    expect(status.label).toBe('Failed');
  });

  it('treats a missing severity as info rather than an error', () => {
    const status = getSectionIssueStatus({ ProcessingIssues: [{ code: 'something', stage: 'ocr', message: 'x' }] });
    expect(status.type).toBe('info');
  });
});

describe('document-level aggregation', () => {
  it('counts issues across sections and reports whether any exist', () => {
    const sections = [
      { ProcessingIssues: [issue('error', 'extraction_failed')] },
      { ProcessingIssues: [] },
      { ProcessingIssues: [issue('warning', 'extraction_incomplete'), issue('info', 'extraction_sparse')] },
    ];
    expect(getDocumentProcessingIssueCount(sections)).toBe(3);
    expect(documentHasProcessingIssues(sections)).toBe(true);
    expect(documentHasProcessingIssues([{ ProcessingIssues: [] }])).toBe(false);
    expect(documentHasProcessingIssues(null)).toBe(false);
    expect(getDocumentProcessingIssueCount(null)).toBe(0);
  });
});
