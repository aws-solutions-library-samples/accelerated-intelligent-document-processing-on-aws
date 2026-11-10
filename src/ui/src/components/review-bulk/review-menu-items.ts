// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The Document List's Review menu: Start Review, Release Review, Skip All Reviews
// (N selected), Skip All Reviews by profile…, Send to review (N selected). Each item
// is shown only to the roles offered it and disabled, with its reason, when the
// selection doesn't fit. The counts are estimates; the server decides per document.
import type { ButtonDropdownProps } from '@cloudscape-design/components';

import {
  NO_SENDABLE_SELECTION_REASON,
  NO_SKIPPABLE_SELECTION_REASON,
  REVIEW_QUEUE_OFF_REASON,
  TEST_STUDIO_REVIEW_MENU_REASON,
} from '../document-actions/action-reasons';
import {
  isClaimableForReview,
  isReleasableForReview,
  isSendableToReview,
  isSkippableForReview,
} from '../document-list/documents-table-config';
import type { MappedDocument } from '../document-list/documents-table-config';

export type ReviewMenuId = 'claim' | 'release' | 'skip-selected' | 'skip-profile' | 'send-selected';

export interface ReviewMenuInput {
  selected: readonly MappedDocument[];
  currentUsername?: string;
  isAdmin: boolean;
  isTestView: boolean;
  defaultQueueEnabled: boolean | null;
  show: { claim: boolean; release: boolean; skip: boolean; send: boolean };
}

export interface ReviewMenu {
  items: ButtonDropdownProps.Items;
  skippable: string[];
  sendable: string[];
  /** The whole menu is disabled with this reason (Test Studio view), or offered nothing. */
  disabledReason?: string;
  visible: boolean;
}

export const buildReviewMenuItems = ({
  selected,
  currentUsername,
  isAdmin,
  isTestView,
  defaultQueueEnabled,
  show,
}: ReviewMenuInput): ReviewMenu => {
  const skippable = selected.filter((d) => isSkippableForReview(d, currentUsername, isAdmin)).map((d) => d.objectKey);
  const sendable = selected.filter(isSendableToReview).map((d) => d.objectKey);
  const claimable = selected.some(isClaimableForReview);
  const releasable = selected.some(isReleasableForReview);
  const reviewItems: ButtonDropdownProps.Item[] = [];
  if (show.claim)
    reviewItems.push({
      id: 'claim',
      text: 'Start Review',
      disabled: !claimable,
      disabledReason: claimable ? undefined : 'Select documents with an unclaimed pending review',
    });
  if (show.release)
    reviewItems.push({
      id: 'release',
      text: 'Release Review',
      disabled: !releasable,
      disabledReason: releasable ? undefined : 'Select documents with a claimed review',
    });
  const bulkItems: ButtonDropdownProps.Item[] = [];
  if (show.skip) {
    bulkItems.push({
      id: 'skip-selected',
      text: `Skip All Reviews (${skippable.length} selected)`,
      disabled: skippable.length === 0,
      disabledReason: skippable.length === 0 ? NO_SKIPPABLE_SELECTION_REASON : undefined,
    });
    bulkItems.push({ id: 'skip-profile', text: 'Skip All Reviews by profile…' });
  }
  if (show.send) {
    const off = defaultQueueEnabled === false;
    bulkItems.push({
      id: 'send-selected',
      text: `Send to review (${sendable.length} selected)`,
      disabled: off || sendable.length === 0,
      disabledReason: off ? REVIEW_QUEUE_OFF_REASON : sendable.length === 0 ? NO_SENDABLE_SELECTION_REASON : undefined,
    });
  }
  const items: ButtonDropdownProps.Items = [
    ...(reviewItems.length ? [{ id: 'review', text: '', items: reviewItems } as ButtonDropdownProps.ItemGroup] : []),
    ...(bulkItems.length ? [{ id: 'bulk', text: '', items: bulkItems } as ButtonDropdownProps.ItemGroup] : []),
  ];
  const visible = reviewItems.length + bulkItems.length > 0;
  return {
    items,
    skippable,
    sendable,
    visible,
    disabledReason: isTestView ? TEST_STUDIO_REVIEW_MENU_REASON : undefined,
  };
};

/**
 * Whether bulk Skip All Reviews counts a production queue item: an OPEN item, or a
 * CLAIMED one the caller holds (an Admin, any claim). The server decides each item.
 */
export const isSkippableQueueItem = (item: { state: string; isMine?: boolean | null }, isAdmin: boolean): boolean =>
  item.state === 'OPEN' || (item.state === 'CLAIMED' && (isAdmin || !!item.isMine));
