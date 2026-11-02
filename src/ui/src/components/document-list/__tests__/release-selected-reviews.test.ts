// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { describe, expect, it, vi } from 'vitest';
import { releaseSelectedReviews, type ReleasedReview } from '../release-selected-reviews';
import type { MappedDocument } from '../documents-table-config';

const row = (objectKey: string, overrides: Partial<MappedDocument> = {}): MappedDocument =>
  ({
    objectKey,
    hitlTriggered: true,
    hitlStatus: 'InProgress',
    hitlCompleted: false,
    hitlReviewOwner: 'rev',
    ...overrides,
  }) as MappedDocument;

const stored: ReleasedReview = { HITLStatus: 'Review Pending' };

describe('releaseSelectedReviews', () => {
  it('sends a release only for the selected documents Release Review counts as releasable', async () => {
    const release = vi.fn().mockResolvedValue(stored);
    const result = await releaseSelectedReviews(
      [
        row('claimed.pdf'),
        row('unclaimed.pdf', { hitlReviewOwner: '', hitlStatus: 'Review Pending' }),
        row('never-raised.pdf', { hitlReviewOwner: '', hitlTriggered: false, hitlStatus: 'N/A' }),
        row('finished.pdf', { hitlCompleted: true, hitlStatus: 'Completed' }),
        row('queued.pdf', { hitlStatus: 'in_review' }),
      ],
      release,
    );
    expect(release.mock.calls).toEqual([['claimed.pdf']]);
    expect(result.released).toEqual([{ objectKey: 'claimed.pdf', review: stored }]);
    expect(result.failures).toEqual([]);
  });

  it("reports a refused release with the server's message", async () => {
    const release = vi.fn().mockRejectedValue({
      errors: [{ message: 'NOT_CLAIMED: nobody has claimed this review, so there is nothing to release' }],
    });
    const result = await releaseSelectedReviews([row('claimed.pdf')], release);
    expect(result.failures).toEqual(['claimed.pdf: NOT_CLAIMED: nobody has claimed this review, so there is nothing to release']);
    expect(result.released).toEqual([]);
  });

  it('keeps releasing after one refusal', async () => {
    const release = vi
      .fn()
      .mockRejectedValueOnce(new Error('REVIEW_FINISHED: this review is already finished, so there is nothing to release'))
      .mockResolvedValueOnce(stored);
    const result = await releaseSelectedReviews([row('a.pdf'), row('b.pdf')], release);
    expect(result.failures).toEqual(['a.pdf: REVIEW_FINISHED: this review is already finished, so there is nothing to release']);
    expect(result.released.map((r) => r.objectKey)).toEqual(['b.pdf']);
  });

  it('counts an empty release result as a refusal', async () => {
    const result = await releaseSelectedReviews([row('claimed.pdf')], vi.fn().mockResolvedValue(null));
    expect(result.failures).toEqual(['claimed.pdf: releaseReview returned no document']);
    expect(result.released).toEqual([]);
  });
});
