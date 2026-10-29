// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { describe, expect, it, vi } from 'vitest';
import { claimSelectedReviews, type ClaimedReview } from '../claim-selected-reviews';
import type { MappedDocument } from '../documents-table-config';

const row = (objectKey: string, overrides: Partial<MappedDocument> = {}): MappedDocument =>
  ({ objectKey, hitlTriggered: true, hitlStatus: 'Review Pending', hitlCompleted: false, ...overrides }) as MappedDocument;

const stored: ClaimedReview = { HITLReviewOwner: 'rev', HITLReviewOwnerEmail: 'rev@example.com', HITLStatus: 'InProgress' };

describe('claimSelectedReviews', () => {
  it('sends a claim only for the selected documents Start Review can claim', async () => {
    const claim = vi.fn().mockResolvedValue(stored);
    const result = await claimSelectedReviews(
      [
        row('owed.pdf'),
        row('never-raised.pdf', { hitlTriggered: false, hitlStatus: 'N/A' }),
        row('finished.pdf', { hitlCompleted: true, hitlStatus: 'Completed' }),
        row('owned.pdf', { hitlReviewOwner: 'bob', hitlStatus: 'InProgress' }),
        row('queued.pdf', { hitlStatus: 'in_review' }),
      ],
      claim,
    );
    expect(claim.mock.calls).toEqual([['owed.pdf']]);
    expect(result.claimed).toEqual([{ objectKey: 'owed.pdf', review: stored }]);
    expect(result.failures).toEqual([]);
  });

  it("reports a refused claim with the server's message and does not open the document", async () => {
    const claim = vi.fn().mockRejectedValue({
      errors: [{ message: 'NO_REVIEW_PENDING: this document has no review waiting, so there is nothing to start' }],
    });
    const result = await claimSelectedReviews([row('owed.pdf')], claim);
    expect(result.failures).toEqual(['owed.pdf: NO_REVIEW_PENDING: this document has no review waiting, so there is nothing to start']);
    expect(result.claimed).toEqual([]);
    expect(result.navigateTo).toBeNull();
  });

  it('opens the one claimable document once it is claimed, and none for several', async () => {
    const claim = vi.fn().mockResolvedValue(stored);
    const single = await claimSelectedReviews([row('owed.pdf'), row('never.pdf', { hitlTriggered: false })], claim);
    expect(single.navigateTo).toBe('owed.pdf');
    const several = await claimSelectedReviews([row('a.pdf'), row('b.pdf')], claim);
    expect(several.navigateTo).toBeNull();
    expect(several.claimed.map((c) => c.objectKey)).toEqual(['a.pdf', 'b.pdf']);
  });

  it('counts an empty claim result as a refusal', async () => {
    const result = await claimSelectedReviews([row('owed.pdf')], vi.fn().mockResolvedValue(null));
    expect(result.failures).toEqual(['owed.pdf: claimReview returned no document']);
    expect(result.navigateTo).toBeNull();
  });
});
