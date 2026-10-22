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

  it.each([
    ['rule_validation_failed', 'rule_validation'],
    ['rule_validation_not_consolidated', 'rule_validation'],
    ['section_processing_failed', 'postprocessing'],
  ])('labels the document-level failure code %s "Failed"', (code, stage) => {
    // The three codes `idp_common.document_failure` writes (#1064). They mean the
    // stage raised, exactly as `extraction_failed` does, so they must not read as
    // the milder "Incomplete". `scripts/tests/test_failure_code_ui_parity.py` is
    // what keeps the backend's set and the UI's in step.
    const status = getSectionIssueStatus({ ProcessingIssues: [{ severity: 'error', code, stage, message: code }] });
    expect(status.type).toBe('error');
    expect(status.label).toBe('Failed');
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
      label: 'Info',
      count: 1,
    });
  });

  // An `info` note usually reports something MISSING, not something repaired, so
  // the label must not claim a recovery that did not happen. Only
  // `assessment_recovered_with_retries` reports one.
  it.each([
    ['extraction_sparse', 'possible silent extraction loss'],
    ['extraction_off_schema_fields', 'fields dropped before assessment'],
    ['assessment_geometry_incomplete', 'scored fields with no bounding box'],
  ])('labels the non-recovery info note %s "Info", not "Auto-recovered"', (code) => {
    const status = getSectionIssueStatus({ ProcessingIssues: [issue('info', code)] });
    expect(status.type).toBe('info');
    expect(status.label).toBe('Info');
  });

  it('labels a genuine recovery "Auto-recovered"', () => {
    const status = getSectionIssueStatus({
      ProcessingIssues: [issue('info', 'assessment_recovered_with_retries')],
    });
    expect(status).toEqual({ type: 'info', label: 'Auto-recovered', count: 1 });
  });

  it('matches a recovery code case-insensitively', () => {
    const status = getSectionIssueStatus({
      ProcessingIssues: [issue('info', 'ASSESSMENT_RECOVERED_WITH_RETRIES')],
    });
    expect(status.label).toBe('Auto-recovered');
  });

  it('does not claim recovery when a non-recovery note sits alongside one', () => {
    // Something was repaired AND something is still worth reading. "Auto-recovered"
    // would be true of one issue and misleading about the section, so the neutral
    // label wins; the repair stays visible in the message and Processing Report.
    const status = getSectionIssueStatus({
      ProcessingIssues: [issue('info', 'assessment_recovered_with_retries'), issue('info', 'extraction_sparse')],
    });
    expect(status).toEqual({ type: 'info', label: 'Info', count: 2 });
  });

  it('keeps a warning outranking any info note', () => {
    const status = getSectionIssueStatus({
      ProcessingIssues: [issue('info', 'assessment_recovered_with_retries'), issue('warning', 'assessment_fields_unscored')],
    });
    expect(status.label).toBe('Degraded');
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
