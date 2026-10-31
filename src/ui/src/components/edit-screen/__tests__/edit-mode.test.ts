// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Which mode a document's edit page opens in, one case per row of the state-to-mode
 * mapping (IDP-0004 requirement, section 8). What has to hold: a document in review
 * never opens as an editable direct edit — an unreadable item is an error, a lagging
 * mirror is a read-only direct edit — and the item's state decides when it was read.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import type { ReviewItemDetail } from '../../../graphql/generated/operation-types';
import { IN_REVIEW_ITEM_STATES, decideEditMode, needsItemRead } from '../edit-mode';
import type { ItemRead } from '../edit-mode';

const STATUSES_PY = join(__dirname, '../../../../../../lib/idp_common_pkg/idp_common/review_queues/statuses.py');

const HISTORY = [{ at: '2026-10-01T00:00:00Z', event: 'completed', actor: 'ann@example.com', details: null }];
const detail = (state: string, isMine = false): ReviewItemDetail =>
  ({ item: { objectKey: 'a.pdf', state, isMine }, history: HISTORY }) as unknown as ReviewItemDetail;
const read = (state: string, isMine = false): ItemRead => ({ kind: 'read', detail: detail(state, isMine) });
const failed = (message: string, errorType?: string): ItemRead => ({ kind: 'failed', error: { errors: [{ message, errorType }] } });
const NOT_READ: ItemRead = { kind: 'not-read' };

const ADMIN = { canWorkReviewQueues: true, annotatorOnly: false };
const ANNOTATOR_ONLY = { canWorkReviewQueues: true, annotatorOnly: true };
const ANNOTATOR_AND_AUTHOR = { canWorkReviewQueues: true, annotatorOnly: false };
const OTHER = { canWorkReviewQueues: false, annotatorOnly: false };

const decide = (hitlStatus: string | null, roles: typeof ADMIN, item: ItemRead = NOT_READ) =>
  decideEditMode({ hitlStatus, ...roles, item });

describe('the edit page’s mode', () => {
  describe('no review, or a legacy status: direct, for every role', () => {
    it.each([null, 'PENDING_REVIEW', 'REVIEW_COMPLETED', 'COMPLETED', ''])('%s', (status) => {
      for (const roles of [ADMIN, ANNOTATOR_ONLY, OTHER]) expect(decide(status, roles)).toEqual({ mode: 'direct' });
    });
  });

  describe('in review: OPEN', () => {
    it('opens the queue for an Admin and an Annotator', () => {
      expect(decide('in_review', ADMIN, read('OPEN'))).toMatchObject({ mode: 'queue' });
      expect(decide('in_review', ANNOTATOR_ONLY, read('OPEN'))).toMatchObject({ mode: 'queue' });
    });

    it('opens the direct edit read-only for an Author, Reviewer or Viewer, saying where it is reviewed', () => {
      expect(decide('in_review', OTHER)).toEqual({ mode: 'direct', lock: 'in-review-elsewhere' });
    });
  });

  it.each([
    ['CLAIMED, mine', read('CLAIMED', true)],
    ["CLAIMED, someone else's", read('CLAIMED', false)],
    ['FINALIZING', read('FINALIZING')],
    ['HOLDING', read('HOLDING')],
    ['FINALIZE_FAILED', read('FINALIZE_FAILED')],
  ])('in review: %s opens the queue', (_name, item) => {
    expect(decide('in_review', ADMIN, item)).toMatchObject({ mode: 'queue' });
    expect(decide('in_review', ANNOTATOR_ONLY, item)).toMatchObject({ mode: 'queue' });
    expect(decide('in_review', OTHER, item)).toEqual({ mode: 'direct', lock: 'in-review-elsewhere' });
  });

  describe('a finished queue review: direct, with its review activity', () => {
    it.each(['complete', 'rejected'])('%s', (status) => {
      const state = status === 'complete' ? 'COMPLETE' : 'REJECTED';
      expect(decide(status, ADMIN, read(state))).toEqual({ mode: 'direct', reviewActivity: HISTORY });
      // A failed read does not matter here: direct, with no queue history.
      expect(decide(status, ADMIN, failed('Internal error'))).toEqual({ mode: 'direct' });
      expect(decide(status, OTHER)).toEqual({ mode: 'direct' });
    });
  });

  it('a withdrawn item’s cleared row: direct', () => {
    expect(decide(null, ADMIN)).toEqual({ mode: 'direct' });
  });

  describe('in review, but the item is missing or has finished (the mirror lags): direct, read-only', () => {
    it.each([
      ['COMPLETE', read('COMPLETE')],
      ['REJECTED', read('REJECTED')],
      ['WITHDRAWN', read('WITHDRAWN')],
      ['an unknown state', read('SOMETHING_NEW')],
      ['ITEM_NOT_FOUND', failed("ITEM_NOT_FOUND: the review item doesn't exist")],
      ['a 404', failed('No such item', 'ResourceNotFound')],
    ])('%s', (_name, item) => {
      expect(decide('in_review', ADMIN, item)).toEqual({ mode: 'direct', lock: 'mirror-lag' });
      expect(decide('in_review', ANNOTATOR_ONLY, item)).toEqual({ mode: 'direct', lock: 'mirror-lag' });
    });
  });

  it('the item read failed: an error, never an editable direct edit', () => {
    expect(decide('in_review', ADMIN, failed('Internal server error', 'InternalError'))).toMatchObject({ mode: 'item-failed' });
  });

  it('in review with no item read: an error (fail closed)', () => {
    expect(decide('in_review', ADMIN, NOT_READ)).toMatchObject({ mode: 'item-failed' });
  });

  describe('not a member of the item’s queue', () => {
    it.each([
      ['NOT_A_MEMBER', failed('NOT_A_MEMBER: you are not a member of this queue')],
      ['OUT_OF_SCOPE', failed("OUT_OF_SCOPE: the item's profile is outside your scope")],
      ['an authorization refusal', failed('Unauthorized', 'Unauthorized')],
    ])('%s', (_name, item) => {
      // Annotator-only: "You can't open this review item".
      expect(decide('in_review', ANNOTATOR_ONLY, item)).toEqual({ mode: 'not-a-member' });
      // An Annotator with another role: direct, read-only, saying where it is reviewed.
      expect(decide('in_review', ANNOTATOR_AND_AUTHOR, item)).toEqual({ mode: 'direct', lock: 'in-review-elsewhere' });
    });
  });

  it('reads the item only for a queue-managed document and a caller who can work queues', () => {
    expect(needsItemRead('in_review', true)).toBe(true);
    expect(needsItemRead('complete', true)).toBe(true);
    expect(needsItemRead('in_review', false)).toBe(false);
    expect(needsItemRead(null, true)).toBe(false);
    expect(needsItemRead('REVIEW_COMPLETED', true)).toBe(false);
  });

  it('counts as in review exactly the item states the server maps to IN_REVIEW', () => {
    const source = readFileSync(STATUSES_PY, 'utf-8');
    const block = /_STATE_TO_STATUS = \{([\s\S]*?)\n\}/.exec(source);
    expect(block, '_STATE_TO_STATUS not found in statuses.py').not.toBeNull();
    const server = [...block![1].matchAll(/QueueItemState\.(\w+): ReviewStatus\.IN_REVIEW/g)].map((m) => m[1]).sort();
    expect(server.length).toBeGreaterThan(0);
    expect([...IN_REVIEW_ITEM_STATES].sort()).toEqual(server);
  });
});
