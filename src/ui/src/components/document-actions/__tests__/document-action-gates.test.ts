// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * When an offered document page action applies. What has to hold: each action is
 * disabled with the reason that applies first, in the order past version, review queue,
 * still processing, configuration loading, BDA mode, then its own availability; Summary
 * on a past version ignores what the live document is doing; Edit Page Class is refused
 * for a legacy review in progress, and only it is; a Reviewer-only user may not change a
 * section's class once the review is finished, while it is processing, or on an
 * unclaimed review, but may while holding a review in progress (the guarantees the
 * Sections panel's reviewerFinished suite pinned); and the same user may not reset a page
 * class in those states (the Pages panel's).
 */

import { describe, expect, it } from 'vitest';

import { inReviewQueueEditReason } from '../../common/review-status';
import {
  BDA_REGROUP_REASON,
  IN_REVIEW_SHORT_REASON,
  NO_SECTIONS_REASON,
  NO_SUMMARY_REASON,
  PROCESSING_REASON,
} from '../../edit-screen/document-edit-status';
import {
  BASELINE_COPYING_REASON,
  BDA_REPROCESS_REASON,
  CONFIG_LOADING_REASON,
  HISTORICAL_REASON,
  LEGACY_REVIEW_REGROUP_REASON,
  NOT_ABORTABLE_REASON,
  NO_EVALUATION_REPORT_REASON,
  NO_EXECUTION_REASON,
  NO_PAGES_REASON,
  NO_PAGE_TEXT_REASON,
  NO_PENDING_REVIEW_REASON,
  NO_RULE_VALIDATION_REASON,
  REVIEWER_FINISHED_REASON,
  REVIEWER_UNCLAIMED_REASON,
  REVIEW_OWNER_REASON,
  RULE_VALIDATION_HISTORICAL_REASON,
} from '../action-reasons';
import { documentActionGates } from '../document-action-gates';
import type { ActionRoles, DocumentActionId, GateInput, GatedDocument } from '../document-action-gates';

const ADMIN: ActionRoles = {
  isAdmin: true,
  isAuthor: false,
  isReviewer: false,
  isAnnotator: false,
  isViewer: false,
  isReviewerOnly: false,
  canWorkReviewQueues: true,
};
const REVIEWER: ActionRoles = { ...ADMIN, isAdmin: false, isReviewer: true, isReviewerOnly: true, canWorkReviewQueues: false };

const DOC: GatedDocument = {
  objectKey: 'in/a.pdf',
  objectStatus: 'COMPLETED',
  hitlStatus: null,
  hitlTriggered: false,
  evaluationStatus: 'COMPLETED',
  summaryReportUri: 's3://o/summary.md',
  evaluationReportUri: 's3://o/eval.md',
  ruleValidationResultUri: 's3://o/rules.json',
  executionArn: 'arn:aws:states:us-east-1:000000000000:execution:sm:x',
  sectionCount: 2,
  pageCount: 3,
  pagesWithText: 3,
};

interface Situation {
  doc?: Partial<GatedDocument>;
  displayed?: GateInput['displayed'];
  isHistorical?: boolean;
  holdsReview?: boolean;
  bdaMode?: boolean;
  configLoading?: boolean;
  baselineRequestInFlight?: boolean;
  roles?: ActionRoles;
}

const gatesFor = ({ doc = {}, displayed, roles = ADMIN, ...rest }: Situation = {}) => {
  const d = { ...DOC, ...doc };
  return documentActionGates({
    roles,
    doc: d,
    displayed: displayed ?? d,
    isHistorical: false,
    holdsReview: false,
    bdaMode: false,
    configLoading: false,
    baselineRequestInFlight: false,
    ...rest,
  });
};
const reason = (action: DocumentActionId, s: Situation = {}) => gatesFor(s)[action].disabledReason;

const H = { isHistorical: true };
const Q = { doc: { hitlStatus: 'in_review' } };
const P = { doc: { objectStatus: 'RUNNING' } };
const C = { configLoading: true };
const B = { bdaMode: true };
const QUEUE = inReviewQueueEditReason(true);

describe('the situational reasons, first match wins', () => {
  it.each([
    ['regroup', [H, Q, P, C]],
    ['reextract', [H, Q, P, C, B]],
    ['page-text', [H, Q, P, C, B]],
    ['reset-class', [H, Q, P, C, B]],
    ['baseline', [H, Q, P]],
  ] as [DocumentActionId, Situation[]][])('%s: each of its reasons applies on its own', (action, situations) => {
    const expected = [HISTORICAL_REASON, QUEUE, PROCESSING_REASON, CONFIG_LOADING_REASON, BDA_REPROCESS_REASON];
    situations.forEach((s, i) => expect(reason(action, s), JSON.stringify(s)).toBe(expected[i]));
    expect(reason(action)).toBeUndefined();
  });

  it.each(['regroup', 'reextract', 'page-text', 'reset-class', 'baseline'] as DocumentActionId[])(
    '%s: a past version wins over a queue, which wins over processing',
    (action) => {
      expect(reason(action, { isHistorical: true, doc: { hitlStatus: 'in_review', objectStatus: 'RUNNING' } })).toBe(HISTORICAL_REASON);
      expect(reason(action, { doc: { hitlStatus: 'in_review', objectStatus: 'RUNNING' } })).toBe(QUEUE);
    },
  );

  it('the reprocessing dialogs: processing wins over configuration, which wins over BDA mode', () => {
    for (const action of ['reextract', 'page-text', 'reset-class'] as DocumentActionId[]) {
      expect(reason(action, { doc: { objectStatus: 'RUNNING' }, configLoading: true, bdaMode: true })).toBe(PROCESSING_REASON);
      expect(reason(action, { configLoading: true, bdaMode: true })).toBe(CONFIG_LOADING_REASON);
    }
  });

  it('refuses Edit Page Class in BDA mode with its own reason, after configuration loading', () => {
    expect(reason('regroup', B)).toBe(BDA_REGROUP_REASON);
    expect(reason('regroup', { configLoading: true, bdaMode: true })).toBe(CONFIG_LOADING_REASON);
  });

  it('names the queue reason for a caller who does not work queues', () => {
    expect(reason('reextract', { ...Q, roles: REVIEWER })).toBe(inReviewQueueEditReason(false));
  });

  it('gives the board the configuration and BDA reasons for Save and Re-extract', () => {
    expect(gatesFor().reextractBlockedReason).toBeUndefined();
    expect(gatesFor(C).reextractBlockedReason).toBe(CONFIG_LOADING_REASON);
    expect(gatesFor(B).reextractBlockedReason).toBe(BDA_REPROCESS_REASON);
  });
});

describe('each action’s own availability', () => {
  it.each([
    ['summary', { doc: { summaryReportUri: null } }, NO_SUMMARY_REASON],
    ['summary', Q, IN_REVIEW_SHORT_REASON],
    ['summary', P, PROCESSING_REASON],
    ['evaluation', { doc: { evaluationReportUri: null } }, NO_EVALUATION_REPORT_REASON],
    ['rule-validation', { doc: { ruleValidationResultUri: null } }, NO_RULE_VALIDATION_REASON],
    ['rule-validation', H, RULE_VALIDATION_HISTORICAL_REASON],
    ['flow', { doc: { executionArn: null } }, NO_EXECUTION_REASON],
    ['regroup', { doc: { sectionCount: 0 } }, NO_SECTIONS_REASON],
    ['reextract', { doc: { sectionCount: 0 } }, NO_SECTIONS_REASON],
    ['page-text', { doc: { pagesWithText: 0 } }, NO_PAGE_TEXT_REASON],
    ['reset-class', { doc: { pageCount: 0 } }, NO_PAGES_REASON],
    ['baseline', { doc: { evaluationStatus: 'BASELINE_COPYING' } }, BASELINE_COPYING_REASON],
    ['baseline', { baselineRequestInFlight: true }, BASELINE_COPYING_REASON],
    ['skip-all', {}, NO_PENDING_REVIEW_REASON],
    ['skip-all', { doc: { hitlStatus: 'in_review', hitlTriggered: true } }, QUEUE],
    ['skip-all', { isHistorical: true, doc: { hitlStatus: 'PendingReview', hitlTriggered: true } }, HISTORICAL_REASON],
    ['abort', {}, NOT_ABORTABLE_REASON],
    ['reprocess', H, HISTORICAL_REASON],
    ['delete', H, HISTORICAL_REASON],
    ['refresh', H, HISTORICAL_REASON],
    ['edit', H, HISTORICAL_REASON],
    ['edit', P, PROCESSING_REASON],
  ] as [DocumentActionId, Situation, string][])('%s is disabled with its reason (%#)', (action, situation, expected) => {
    expect(reason(action, situation)).toBe(expected);
  });

  it('Skip All Reviews is available for a legacy review pending', () => {
    expect(reason('skip-all', { doc: { hitlStatus: 'PendingReview', hitlTriggered: true } })).toBeUndefined();
  });

  it('Abort is available while the document processes, and Edit into a queue whatever the status', () => {
    expect(reason('abort', P)).toBeUndefined();
    expect(reason('edit', { doc: { hitlStatus: 'in_review', objectStatus: 'RUNNING' } })).toBeUndefined();
  });

  it('Summary on a past version ignores the live document’s queue and processing state', () => {
    const displayed = { summaryReportUri: 's3://o/v1/summary.md' };
    expect(reason('summary', { isHistorical: true, displayed, doc: { hitlStatus: 'in_review', objectStatus: 'RUNNING' } })).toBeUndefined();
    expect(reason('summary', { isHistorical: true, displayed: { summaryReportUri: '' } })).toBe(NO_SUMMARY_REASON);
  });

  it('refuses Edit Page Class only, for a legacy review in progress', () => {
    const legacy = { doc: { objectStatus: 'HITL_IN_PROGRESS', hitlStatus: 'InProgress', hitlTriggered: true, hitlReviewOwner: 'r' } };
    expect(reason('regroup', legacy)).toBe(LEGACY_REVIEW_REGROUP_REASON);
    for (const action of ['reextract', 'page-text', 'reset-class', 'baseline'] as DocumentActionId[]) {
      expect(reason(action, legacy), action).toBeUndefined();
    }
  });

  it('Start Review is offered only for a legacy review pending and unclaimed', () => {
    expect(gatesFor({ roles: REVIEWER, doc: { hitlStatus: 'PendingReview', hitlTriggered: true } })['start-review'].shown).toBe(true);
    expect(
      gatesFor({ roles: REVIEWER, doc: { hitlStatus: 'PendingReview', hitlTriggered: true, hitlReviewOwner: 'r' } })['start-review'].shown,
    ).toBe(false);
    expect(gatesFor({ roles: REVIEWER, doc: { hitlStatus: 'in_review', hitlTriggered: true } })['start-review'].shown).toBe(false);
    expect(gatesFor({ roles: REVIEWER, doc: { hitlStatus: 'Completed', hitlTriggered: true } })['start-review'].shown).toBe(false);
  });
});

describe('the Reviewer-only class lock', () => {
  const held = { hitlTriggered: true, hitlReviewOwner: 'r' };
  it.each(['complete', 'rejected', 'Review Completed', 'COMPLETED', 'Skipped'])('locks the class once the review is %s', (hitlStatus) => {
    expect(gatesFor({ roles: REVIEWER, doc: { ...held, hitlStatus } }).canChangeClass).toBe(false);
    expect(reason('reset-class', { roles: REVIEWER, doc: { ...held, hitlStatus } })).toBe(REVIEWER_FINISHED_REASON);
  });

  it('leaves it open to the holder of a review in progress', () => {
    expect(gatesFor({ roles: REVIEWER, doc: { ...held, hitlStatus: 'InProgress' } }).canChangeClass).toBe(true);
    expect(reason('reset-class', { roles: REVIEWER, holdsReview: true, doc: { ...held, hitlStatus: 'InProgress' } })).toBeUndefined();
  });

  it('locks an unclaimed review, and a document still processing', () => {
    const unclaimed = { roles: REVIEWER, doc: { hitlTriggered: true, hitlStatus: 'PendingReview' } };
    expect(gatesFor(unclaimed).canChangeClass).toBe(false);
    expect(reason('reset-class', unclaimed)).toBe(REVIEWER_UNCLAIMED_REASON);
    expect(gatesFor({ roles: REVIEWER, doc: { ...held, objectStatus: 'RUNNING' } }).canChangeClass).toBe(false);
  });

  it('does not lock an Admin', () => {
    expect(gatesFor({ doc: { ...held, hitlStatus: 'complete' } }).canChangeClass).toBe(true);
    expect(reason('reset-class', { doc: { hitlTriggered: true, hitlStatus: 'complete' } })).toBeUndefined();
  });
});

describe('the processChanges actions for a caller who is not an Admin (the server’s open-review owner rule)', () => {
  const AUTHOR_REVIEWER: ActionRoles = { ...REVIEWER, isAuthor: true, isReviewerOnly: false };
  const open = { hitlTriggered: true, hitlStatus: 'InProgress', hitlReviewOwner: 'r' };
  const PROCESS_ACTIONS = ['reextract', 'page-text', 'reset-class'] as DocumentActionId[];

  it.each([REVIEWER, AUTHOR_REVIEWER])('are open to the holder of the open review (%#)', (roles) => {
    for (const action of PROCESS_ACTIONS) expect(reason(action, { roles, holdsReview: true, doc: open }), action).toBeUndefined();
    expect(gatesFor({ roles, holdsReview: true, doc: open }).reextractBlockedReason).toBeUndefined();
  });

  it.each([
    ['a finished review the caller holds', { ...open, hitlStatus: 'Review Completed' }, true, REVIEWER_FINISHED_REASON],
    ['an unclaimed pending review', { hitlTriggered: true, hitlStatus: 'PendingReview' }, false, REVIEWER_UNCLAIMED_REASON],
    ['an open review someone else holds', open, false, REVIEW_OWNER_REASON],
    ['a document with no review', {}, false, REVIEW_OWNER_REASON],
  ] as [string, Partial<GatedDocument>, boolean, string][])(
    'are refused on %s, for a Reviewer and an Author',
    (_label, doc, holdsReview, expected) => {
      for (const roles of [REVIEWER, AUTHOR_REVIEWER]) {
        for (const action of PROCESS_ACTIONS) expect(reason(action, { roles, holdsReview, doc }), action).toBe(expected);
        expect(gatesFor({ roles, holdsReview, doc }).reextractBlockedReason).toBe(expected);
      }
    },
  );

  it('does not apply to an Admin', () => {
    for (const action of PROCESS_ACTIONS) expect(reason(action, { doc: { ...open, hitlReviewOwner: 'someone' } }), action).toBeUndefined();
  });

  it('comes after BDA mode, and does not touch Edit Page Class, which is not processChanges', () => {
    expect(reason('reextract', { roles: REVIEWER, ...B })).toBe(BDA_REPROCESS_REASON);
    expect(reason('regroup', { roles: REVIEWER })).toBeUndefined();
  });
});
