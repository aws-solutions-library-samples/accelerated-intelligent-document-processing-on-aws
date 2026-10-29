// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { getErrorMessage } from '../../utils/errorUtils';
import { isClaimableForReview, type MappedDocument } from './documents-table-config';

/** What `claimReview` returns that the document list shows. */
export interface ClaimedReview {
  HITLReviewOwner: string;
  HITLReviewOwnerEmail: string;
  HITLStatus: string;
}

export interface ClaimSelectionResult {
  /** The documents claimed, with what the server stored. */
  claimed: { objectKey: string; review: ClaimedReview }[];
  /** One line per refused claim: the document and the server's message. */
  failures: string[];
  /** The document to open: the one claimable document, if it was claimed. */
  navigateTo: string | null;
}

/**
 * Start Review from the document list. Claims only the selected documents Start
 * Review can claim (`isClaimableForReview`): the server refuses the rest
 * (`NO_REVIEW_PENDING`, `REVIEW_FINISHED`, `QUEUE_MANAGED`). A refused claim is
 * reported with the server's message rather than only logged.
 */
export const claimSelectedReviews = async (
  selected: readonly MappedDocument[],
  claim: (objectKey: string) => Promise<ClaimedReview>,
): Promise<ClaimSelectionResult> => {
  const claimable = selected.filter(isClaimableForReview);
  const claimed: ClaimSelectionResult['claimed'] = [];
  const failures: string[] = [];
  for (const item of claimable) {
    try {
      const review = await claim(item.objectKey);
      if (!review) throw new Error('claimReview returned no document');
      claimed.push({ objectKey: item.objectKey, review });
    } catch (e) {
      failures.push(`${item.objectKey}: ${getErrorMessage(e)}`);
    }
  }
  const navigateTo = claimable.length === 1 && failures.length === 0 ? claimable[0].objectKey : null;
  return { claimed, failures, navigateTo };
};
