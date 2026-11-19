// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { Link, StatusIndicator } from '@cloudscape-design/components';
import { defineMessages, type IntlShape, type NoMessageValues, type TypedMessageDescriptor } from 'react-intl';

import { normalizeHitlStatus } from './review-status';

interface HitlItem {
  hitlTriggered?: boolean;
  hitlStatus?: string;
  hitlCompleted?: boolean;
}

const messages = defineMessages<{
  inReview: NoMessageValues;
  inReviewLinkAriaLabel: NoMessageValues;
  completed: NoMessageValues;
  rejected: NoMessageValues;
  notApplicable: NoMessageValues;
  failed: NoMessageValues;
  skipped: NoMessageValues;
  inProgress: NoMessageValues;
  pending: NoMessageValues;
}>({
  inReview: {
    id: 'documents.reviewStatus.inReview',
    defaultMessage: 'In Review',
    description: 'Review Status of a document that is waiting in a review queue for a human reviewer.',
  },
  inReviewLinkAriaLabel: {
    id: 'documents.reviewStatus.inReviewLinkAriaLabel',
    defaultMessage: 'In Review: open the review queue item',
    description: 'Accessible name of the In Review status when it links to the document in its review queue.',
  },
  completed: {
    id: 'documents.reviewStatus.completed',
    defaultMessage: 'Review Completed',
    description: 'Review Status of a document whose human review is finished.',
  },
  rejected: {
    id: 'documents.reviewStatus.rejected',
    defaultMessage: 'Review Rejected',
    description: 'Review Status of a document a human reviewer rejected.',
  },
  notApplicable: {
    id: 'documents.reviewStatus.notApplicable',
    defaultMessage: 'N/A',
    description: 'Review Status of a document that never needed a human review.',
  },
  failed: {
    id: 'documents.reviewStatus.failed',
    defaultMessage: 'Review Failed',
    description: 'Review Status of a document whose human review step failed.',
  },
  skipped: {
    id: 'documents.reviewStatus.skipped',
    defaultMessage: 'Review Skipped',
    description: 'Review Status of a document whose human review was skipped.',
  },
  inProgress: {
    id: 'documents.reviewStatus.inProgress',
    defaultMessage: 'Review In Progress',
    description: 'Review Status of a document a human reviewer is working on (legacy review flow).',
  },
  pending: {
    id: 'documents.reviewStatus.pending',
    defaultMessage: 'Review Pending',
    description: 'Review Status of a document that needs a human review nobody has started.',
  },
});

/** The label in the UI's language; without `intl`, the English source text. */
const labelOf = (message: TypedMessageDescriptor<NoMessageValues>, intl?: IntlShape): string =>
  intl ? intl.formatMessage(message) : String(message.defaultMessage);

/**
 * `reviewHref`, when given, links an In Review status to the document's queue item, so
 * a queued document is opened for review the same way from every place it is listed.
 */
export const renderHitlStatus = (item: HitlItem, reviewHref?: string, intl?: IntlShape): React.JSX.Element => {
  const view = normalizeHitlStatus(item.hitlStatus);

  // Queue-managed documents carry their status whether or not the legacy flag is set.
  if (view.queueManaged) {
    if (view.kind === 'in_review') {
      const status = <StatusIndicator type="in-progress">{labelOf(messages.inReview, intl)}</StatusIndicator>;
      return reviewHref ? (
        <Link href={reviewHref} ariaLabel={labelOf(messages.inReviewLinkAriaLabel, intl)}>
          {status}
        </Link>
      ) : (
        status
      );
    }
    if (view.kind === 'complete') return <StatusIndicator type="success">{labelOf(messages.completed, intl)}</StatusIndicator>;
    return <StatusIndicator type="stopped">{labelOf(messages.rejected, intl)}</StatusIndicator>;
  }

  if (!item.hitlTriggered) {
    return <StatusIndicator type="stopped">{labelOf(messages.notApplicable, intl)}</StatusIndicator>;
  }

  const status = item.hitlStatus?.toLowerCase().replace(/\s+/g, '') || '';

  if (status === 'failed' || status === 'reviewfailed') {
    return <StatusIndicator type="error">{labelOf(messages.failed, intl)}</StatusIndicator>;
  }
  if (view.kind === 'skipped') {
    return <StatusIndicator type="stopped">{labelOf(messages.skipped, intl)}</StatusIndicator>;
  }
  if (item.hitlCompleted || view.kind === 'complete') {
    return <StatusIndicator type="success">{labelOf(messages.completed, intl)}</StatusIndicator>;
  }
  if (view.kind === 'in_progress') {
    return <StatusIndicator type="in-progress">{labelOf(messages.inProgress, intl)}</StatusIndicator>;
  }

  // HITL triggered but not completed - pending review
  return <StatusIndicator type="pending">{labelOf(messages.pending, intl)}</StatusIndicator>;
};

export default renderHitlStatus;
