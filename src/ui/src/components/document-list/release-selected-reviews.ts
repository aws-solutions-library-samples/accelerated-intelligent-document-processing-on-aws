// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { getErrorMessage } from '../../utils/errorUtils';
import { isReleasableForReview, type MappedDocument } from './documents-table-config';

/** What `releaseReview` returns that the document list shows. */
export interface ReleasedReview {
  HITLStatus: string;
}

export interface ReleaseSelectionResult {
  /** The documents released, with the status the server stored. */
  released: { objectKey: string; review: ReleasedReview }[];
  /** One line per refused release: the document and the server's message. */
  failures: string[];
}

/**
 * Release Review from the document list. Releases only the selected documents the
 * Release Review button counts (`isReleasableForReview`): the server refuses the
 * rest (`NOT_CLAIMED`, `REVIEW_FINISHED`). A refused release is reported with the
 * server's message rather than only logged.
 */
export const releaseSelectedReviews = async (
  selected: readonly MappedDocument[],
  release: (objectKey: string) => Promise<ReleasedReview>,
): Promise<ReleaseSelectionResult> => {
  const released: ReleaseSelectionResult['released'] = [];
  const failures: string[] = [];
  for (const item of selected.filter(isReleasableForReview)) {
    try {
      const review = await release(item.objectKey);
      if (!review) throw new Error('releaseReview returned no document');
      released.push({ objectKey: item.objectKey, review });
    } catch (e) {
      failures.push(`${item.objectKey}: ${getErrorMessage(e)}`);
    }
  }
  return { released, failures };
};
