// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * A Reviewer-only user's document list shows only reviews still owed. Rows go through
 * the same mapping the list uses, so `hitlTriggered` and `hitlCompleted` are derived
 * from the stored attributes exactly as on screen.
 */

import { describe, expect, it } from 'vitest';

import mapDocumentsAttributes from '../../common/map-document-attributes';
import { LegacyHitlStatus, ReviewStatus } from '../../common/review-status';
import { isPendingForReviewer } from '../reviewer-pending-filter';

type Row = Parameters<typeof isPendingForReviewer>[0];
const row = (item: Record<string, unknown>): Row => mapDocumentsAttributes([{ ObjectKey: 'd.pdf', ...item } as never])[0] as Row;

describe('isPendingForReviewer', () => {
  it.each([ReviewStatus.COMPLETE, ReviewStatus.REJECTED])('hides a queue review that ended %s', (status) => {
    // HITLTriggered is the attribute that lets a row through the server-side filter.
    expect(isPendingForReviewer(row({ HITLTriggered: true, HITLStatus: status }), 'rev')).toBe(false);
  });

  it.each([
    LegacyHitlStatus.COMPLETED,
    LegacyHitlStatus.REVIEW_COMPLETED,
    LegacyHitlStatus.SKIPPED,
    LegacyHitlStatus.REVIEW_SKIPPED,
    'COMPLETED',
  ])('hides a finished legacy review (%s)', (status) => {
    expect(isPendingForReviewer(row({ HITLTriggered: true, HITLStatus: status }), 'rev')).toBe(false);
  });

  it.each([LegacyHitlStatus.PENDING_REVIEW, LegacyHitlStatus.REVIEW_PENDING, LegacyHitlStatus.IN_PROGRESS])(
    'shows an unassigned open legacy review (%s)',
    (status) => {
      expect(isPendingForReviewer(row({ HITLTriggered: true, HITLStatus: status }), 'rev')).toBe(true);
    },
  );

  it('shows an open review assigned to this reviewer and hides one assigned to another', () => {
    const claimed = { HITLTriggered: true, HITLStatus: LegacyHitlStatus.IN_PROGRESS };
    expect(isPendingForReviewer(row({ ...claimed, HITLReviewOwner: 'rev' }), 'rev')).toBe(true);
    expect(isPendingForReviewer(row({ ...claimed, HITLReviewOwner: 'other' }), 'rev')).toBe(false);
  });

  it('hides a document with no review', () => {
    expect(isPendingForReviewer(row({}), 'rev')).toBe(false);
  });
});
