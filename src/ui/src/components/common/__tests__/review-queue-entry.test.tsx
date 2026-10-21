// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * A document in a review queue has one way in: its queue item.
 *
 * `isInReviewQueue` is the one predicate the document page, the panels and the list
 * use to decide that, and the In Review status is what links there from the list.
 * A finished queue document is editable again (a fresh, rerouted run), so only
 * `in_review` counts; legacy statuses never do.
 */

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import { inReviewQueueEditReason, isInReviewQueue, lastSkipAllReason } from '../review-status';
import { renderHitlStatus } from '../hitl-status-renderer';

describe('isInReviewQueue', () => {
  it('is true only while a queue review is open', () => {
    expect(isInReviewQueue('in_review')).toBe(true);
    expect(isInReviewQueue('complete')).toBe(false);
    expect(isInReviewQueue('rejected')).toBe(false);
  });

  it('never counts a legacy status', () => {
    for (const legacy of ['InProgress', 'Review In Progress', 'PendingReview', 'Review Pending', 'Completed']) {
      expect(isInReviewQueue(legacy)).toBe(false);
    }
    expect(isInReviewQueue(undefined)).toBe(false);
  });

  it('tells a caller without queue access who reviews it', () => {
    expect(inReviewQueueEditReason(true)).toMatch(/Choose Review/);
    expect(inReviewQueueEditReason(false)).toMatch(/Admin or Annotator/);
  });
});

describe('renderHitlStatus In Review link', () => {
  const queued = { hitlStatus: 'in_review', hitlTriggered: true };

  it('links In Review to the queue item when given an href', () => {
    render(renderHitlStatus(queued, '#/review-queues/items/doc.pdf'));
    const link = screen.getByRole('link', { name: /In Review/ });
    expect(link).toHaveAttribute('href', '#/review-queues/items/doc.pdf');
  });

  it('is plain status text without one', () => {
    render(renderHitlStatus(queued));
    expect(screen.getByText('In Review')).toBeInTheDocument();
    expect(screen.queryByRole('link')).toBeNull();
  });

  it('names finished queue reviews like the legacy ones', () => {
    render(renderHitlStatus({ hitlStatus: 'complete', hitlTriggered: true }));
    expect(screen.getByText('Review Completed')).toBeInTheDocument();
    render(renderHitlStatus({ hitlStatus: 'rejected', hitlTriggered: true }));
    expect(screen.getByText('Review Rejected')).toBeInTheDocument();
  });

  it('never links a legacy status', () => {
    render(renderHitlStatus({ hitlStatus: 'Review Pending', hitlTriggered: true }, '#/x'));
    expect(screen.queryByRole('link')).toBeNull();
  });
});

describe('lastSkipAllReason', () => {
  it('reads the latest Skip All Reviews reason and note', () => {
    const history = [
      { sectionId: '1', reviewedBy: 'rev' },
      { sectionId: 'ALL_SKIPPED', action: 'skip_all', reason: 'Duplicate document' },
      { sectionId: 'ALL_SKIPPED', action: 'skip_all', reason: 'Other (see note)', note: 'customer withdrew' },
    ];
    expect(lastSkipAllReason(history)).toEqual({ reason: 'Other (see note)', note: 'customer withdrew' });
  });

  it('is null for a skip recorded without a reason, or no history', () => {
    expect(lastSkipAllReason([{ action: 'skip_all' }])).toBeNull();
    expect(lastSkipAllReason(undefined)).toBeNull();
    expect(lastSkipAllReason('not a list')).toBeNull();
  });
});
