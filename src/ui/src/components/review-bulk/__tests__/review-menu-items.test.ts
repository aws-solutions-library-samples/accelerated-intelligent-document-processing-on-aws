// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/** The Document List's Review menu: counts, reasons, roles and the Test Studio view. */

import type { ButtonDropdownProps } from '@cloudscape-design/components';
import { describe, expect, it } from 'vitest';

import {
  NO_SENDABLE_SELECTION_REASON,
  NO_SKIPPABLE_SELECTION_REASON,
  REVIEW_QUEUE_OFF_REASON,
  TEST_STUDIO_REVIEW_MENU_REASON,
} from '../../document-actions/action-reasons';
import type { MappedDocument } from '../../document-list/documents-table-config';
import { buildReviewMenuItems, isSkippableQueueItem } from '../review-menu-items';

const doc = (objectKey: string, over: Partial<MappedDocument> = {}): MappedDocument =>
  ({
    objectKey,
    objectStatus: 'COMPLETED',
    hitlTriggered: false,
    hitlCompleted: false,
    hitlStatus: null,
    hitlReviewOwner: '',
    ...over,
  }) as MappedDocument;

const ALL = { claim: true, release: true, skip: true, send: true };
const flat = (items: ButtonDropdownProps.Items) =>
  items.flatMap((i) => ('items' in i ? (i.items as ButtonDropdownProps.Item[]) : [i as ButtonDropdownProps.Item]));
const item = (items: ButtonDropdownProps.Items, id: string) => flat(items).find((i) => i.id === id);

const SELECTION = [
  doc('legacy.pdf', { hitlTriggered: true, hitlStatus: 'Review Pending' }),
  doc('queued.pdf', { hitlStatus: 'in_review' }),
  doc('held.pdf', { hitlTriggered: true, hitlStatus: 'InProgress', hitlReviewOwner: 'carol' }),
  doc('done.pdf', { hitlTriggered: true, hitlStatus: 'Review Skipped', hitlCompleted: true }),
  doc('running.pdf', { objectStatus: 'RUNNING' }),
];

describe('buildReviewMenuItems', () => {
  it('counts only the rows each bulk action applies to', () => {
    const menu = buildReviewMenuItems({
      selected: SELECTION,
      currentUsername: 'rev',
      isAdmin: false,
      isTestView: false,
      defaultQueueEnabled: true,
      show: ALL,
    });
    expect(menu.skippable).toEqual(['legacy.pdf', 'queued.pdf']);
    expect(menu.sendable).toEqual(['done.pdf']);
    expect(item(menu.items, 'skip-selected')?.text).toBe('Skip All Reviews (2 selected)');
    expect(item(menu.items, 'send-selected')?.text).toBe('Send to review (1 selected)');
  });

  it('lets an Admin count another reviewer’s legacy review', () => {
    const menu = buildReviewMenuItems({ selected: SELECTION, isAdmin: true, isTestView: false, defaultQueueEnabled: true, show: ALL });
    expect(menu.skippable).toContain('held.pdf');
  });

  it('disables with reasons', () => {
    const empty = buildReviewMenuItems({ selected: [], isAdmin: false, isTestView: false, defaultQueueEnabled: false, show: ALL });
    expect(item(empty.items, 'skip-selected')?.disabledReason).toBe(NO_SKIPPABLE_SELECTION_REASON);
    expect(item(empty.items, 'send-selected')?.disabledReason).toBe(REVIEW_QUEUE_OFF_REASON);
    const noSend = buildReviewMenuItems({ selected: [], isAdmin: false, isTestView: false, defaultQueueEnabled: true, show: ALL });
    expect(item(noSend.items, 'send-selected')?.disabledReason).toBe(NO_SENDABLE_SELECTION_REASON);
    expect(item(noSend.items, 'skip-profile')?.disabled).toBeFalsy();
  });

  it('offers nothing a role may not use, and disables the menu in Test Studio', () => {
    const none = buildReviewMenuItems({
      selected: [],
      isAdmin: false,
      isTestView: false,
      defaultQueueEnabled: true,
      show: { claim: false, release: false, skip: false, send: false },
    });
    expect(none.visible).toBe(false);
    const test = buildReviewMenuItems({ selected: [], isAdmin: true, isTestView: true, defaultQueueEnabled: true, show: ALL });
    expect(test.disabledReason).toBe(TEST_STUDIO_REVIEW_MENU_REASON);
  });
});

describe('isSkippableQueueItem', () => {
  it('counts OPEN items and own claims for a Reviewer, any claim for an Admin', () => {
    expect(isSkippableQueueItem({ state: 'OPEN' }, false)).toBe(true);
    expect(isSkippableQueueItem({ state: 'CLAIMED', isMine: true }, false)).toBe(true);
    expect(isSkippableQueueItem({ state: 'CLAIMED', isMine: false }, false)).toBe(false);
    expect(isSkippableQueueItem({ state: 'CLAIMED', isMine: false }, true)).toBe(true);
    expect(isSkippableQueueItem({ state: 'FINALIZING' }, true)).toBe(false);
  });
});
