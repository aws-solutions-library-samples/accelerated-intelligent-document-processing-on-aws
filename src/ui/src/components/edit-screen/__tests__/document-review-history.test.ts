// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * History › Review Activity for a legacy review: the records complete_section_review and
 * legacy Skip All Reviews append to HITLReviewHistory (`reviewedAt`, `reviewedBy`,
 * `reviewedByEmail`) are listed, not dropped, with the skip's reason, note and options.
 */
import { describe, expect, it } from 'vitest';
import { createIntl } from 'react-intl';

import { documentReviewHistory } from '../direct-edit-model';
import { eventLabel } from '../../review-queues/ReviewHistoryModal';

const intl = createIntl({ locale: 'en' });

describe('documentReviewHistory', () => {
  it("lists a finished section from complete_section_review's record", () => {
    const rows = documentReviewHistory(
      [{ sectionId: '2', reviewedBy: 'user-1', reviewedByEmail: 'r@example.com', reviewedAt: '2026-11-01T10:00:00Z' }],
      intl,
    );
    expect(rows).toEqual([
      { at: '2026-11-01T10:00:00Z', event: 'section_reviewed', actor: 'r@example.com', details: JSON.stringify({ note: 'Section 2' }) },
    ]);
    expect(eventLabel(rows[0].event)).toBe('Section review completed');
  });

  it("lists Skip All Reviews from legacy_skip's record, with its reason, note, job and resubmit choice", () => {
    const rows = documentReviewHistory(
      [
        {
          sectionId: 'ALL_SKIPPED',
          reviewedBy: 'user-1',
          reviewedByEmail: '',
          reviewedAt: '2026-11-02T10:00:00Z',
          action: 'skip_all',
          reason: 'Duplicate document',
          note: 'Sent twice',
          source: 'bulk',
          jobId: 'job-7',
          resubmitted: false,
        },
      ],
      intl,
    );
    expect(rows).toHaveLength(1);
    expect(rows[0].event).toBe('skip_all');
    expect(eventLabel('skip_all')).toBe('Skipped (Skip All Reviews)');
    expect(rows[0].actor).toBe('user-1');
    expect(JSON.parse(rows[0].details as string)).toEqual({
      reason: 'Duplicate document',
      note: 'Sent twice · Not resubmitted for downstream processing · Job job-7',
    });
  });

  it('still reads records written with timestamp, user and action', () => {
    expect(documentReviewHistory([{ action: 'skip_all', timestamp: '2026-11-03T00:00:00Z', user: 'u@example.com' }], intl)).toEqual([
      { at: '2026-11-03T00:00:00Z', event: 'skip_all', actor: 'u@example.com', details: null },
    ]);
  });

  it('drops a record with no time, or with neither an action nor a section', () => {
    expect(documentReviewHistory([{ action: 'skip_all' }, { reviewedAt: '2026-11-03T00:00:00Z' }], intl)).toEqual([]);
  });
});
