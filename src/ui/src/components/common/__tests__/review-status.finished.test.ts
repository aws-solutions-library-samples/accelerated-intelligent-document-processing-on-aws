// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Which stored HITLStatus values mean the review has finished.
 *
 * The inputs are the declared vocabularies themselves rather than a hand-written
 * list, so a status added to either is classified here without an edit. A review
 * queue finishes as `complete` or `rejected` (written by the review-queue tracker);
 * the legacy flow finishes as completed or skipped.
 */

import { describe, expect, it } from 'vitest';

import mapDocumentsAttributes from '../map-document-attributes';
import { LegacyHitlStatus, ReviewStatus, isHitlReviewFinished, normalizeHitlStatus } from '../review-status';

const FINISHED_KINDS = new Set(['complete', 'rejected', 'skipped']);
const declared = [...Object.values(ReviewStatus), ...Object.values(LegacyHitlStatus)];

describe('isHitlReviewFinished', () => {
  it('reads every declared status by its normalised kind', () => {
    expect(declared.length).toBeGreaterThan(0);
    for (const status of declared) {
      expect([status, isHitlReviewFinished(status)]).toEqual([status, FINISHED_KINDS.has(normalizeHitlStatus(status).kind)]);
    }
  });

  it('treats both queue terminal statuses as finished, and an open queue review as not', () => {
    expect(isHitlReviewFinished(ReviewStatus.COMPLETE)).toBe(true);
    expect(isHitlReviewFinished(ReviewStatus.REJECTED)).toBe(true);
    expect(isHitlReviewFinished(ReviewStatus.IN_REVIEW)).toBe(false);
  });

  it('treats the earlier A2I flow’s COMPLETED as finished, and its open spellings as not', () => {
    expect(isHitlReviewFinished('COMPLETED')).toBe(true);
    for (const open of ['IN_PROGRESS', 'TRIGGERED', 'FAILED']) {
      expect([open, isHitlReviewFinished(open)]).toEqual([open, false]);
    }
  });

  it('reads an absent status as not finished', () => {
    for (const absent of [undefined, null, '', 'N/A']) {
      expect(isHitlReviewFinished(absent)).toBe(false);
    }
  });
});

describe('the document list mapping', () => {
  const hitlCompleted = (HITLStatus: string) =>
    (mapDocumentsAttributes([{ ObjectKey: 'd.pdf', HITLStatus } as never])[0] as Record<string, unknown>).hitlCompleted;

  it('marks a rejected queue review completed, like a completed one', () => {
    expect(hitlCompleted(ReviewStatus.REJECTED)).toBe(true);
    expect(hitlCompleted(ReviewStatus.COMPLETE)).toBe(true);
    expect(hitlCompleted(ReviewStatus.IN_REVIEW)).toBe(false);
  });

  it('keeps the legacy finished spellings completed and the open ones not', () => {
    for (const status of declared) {
      expect([status, hitlCompleted(status)]).toEqual([status, isHitlReviewFinished(status)]);
    }
    expect(hitlCompleted('COMPLETED')).toBe(true);
  });
});
