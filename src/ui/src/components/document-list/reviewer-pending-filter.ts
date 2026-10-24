// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { isHitlReviewFinished } from '../common/review-status';

interface ReviewerListItem {
  hitlTriggered?: boolean;
  hitlStatus?: string;
  hitlReviewOwner?: string;
}

/**
 * Whether a Reviewer-only user's document list shows this document: a review is owed
 * on it and it is unassigned or assigned to them. The listDocuments resolver applies a
 * similar restriction server-side, but it reads only the HITLCompleted flag, which no
 * review queue writes, so a finished queue review on a row carrying HITLTriggered
 * reaches the client and is hidden here.
 */
export const isPendingForReviewer = (doc: ReviewerListItem, currentUsername: string | undefined): boolean => {
  if (!doc.hitlTriggered) return false;
  // The mapped hitlCompleted flag is this same check, so it is read once, here.
  if (isHitlReviewFinished(doc.hitlStatus)) return false;
  return !doc.hitlReviewOwner || doc.hitlReviewOwner === currentUsername;
};
