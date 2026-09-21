// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * How the document list resolves its processing-issue count, which the backend now
 * depends on.
 *
 * The stored `ProcessingIssueCount` is preferred whenever it is merely **non-null**
 * — not "non-zero" — so a stored `0` wins over a count derived from the sections.
 * That is the right precedence (the stored value is computed from the whole
 * document, and a list page may not carry `Sections` at all), and it is exactly why
 * the backend must not write a `0` it cannot substantiate: an end-of-execution
 * status write that re-derived the count from a Document carrying no sections
 * stamped a green 0 over a section-level issue written moments earlier.
 *
 * `ConfidenceAlertCount` beside it uses the *other* rule — non-null **and** > 0 —
 * so the two are not interchangeable and a change to one must not be copied to the
 * other by assumption. Both are pinned here.
 */

import { describe, expect, it } from 'vitest';

import mapDocumentsAttributes from '../map-document-attributes';

const sectionWithIssues = {
  Id: '1',
  Class: 'bank-statement',
  ProcessingIssues: [{ stage: 'extraction', severity: 'error', code: 'extraction_failed', message: 'failed' }],
};

const mapOne = (item: Record<string, unknown>) => mapDocumentsAttributes([item as never])[0] as Record<string, unknown>;

describe('processing-issue count precedence', () => {
  it('prefers a stored count over the sections', () => {
    const row = mapOne({ ObjectKey: 'd.pdf', ProcessingIssueCount: 7, Sections: [sectionWithIssues] });
    expect(row.processingIssueCount).toBe(7);
  });

  it('prefers a stored ZERO over the sections, which is why the backend must not write an unsubstantiated 0', () => {
    const row = mapOne({ ObjectKey: 'd.pdf', ProcessingIssueCount: 0, Sections: [sectionWithIssues] });
    expect(row.processingIssueCount).toBe(0);
  });

  it('derives from the sections when no count is stored', () => {
    const row = mapOne({ ObjectKey: 'd.pdf', Sections: [sectionWithIssues] });
    expect(row.processingIssueCount).toBe(1);
  });

  it('reports zero when neither a count nor sections are present', () => {
    expect(mapOne({ ObjectKey: 'd.pdf' }).processingIssueCount).toBe(0);
  });
});

describe('confidence-alert count uses the other rule', () => {
  it('falls back to the sections when the stored value is zero', () => {
    const row = mapOne({
      ObjectKey: 'd.pdf',
      ConfidenceAlertCount: 0,
      Sections: [{ Id: '1', Class: 'x', ConfidenceThresholdAlerts: [{ attributeName: 'a' }] }],
    });
    expect(row.confidenceAlertCount).toBe(1);
  });

  it('prefers a stored non-zero value', () => {
    const row = mapOne({ ObjectKey: 'd.pdf', ConfidenceAlertCount: 4, Sections: [] });
    expect(row.confidenceAlertCount).toBe(4);
  });
});
