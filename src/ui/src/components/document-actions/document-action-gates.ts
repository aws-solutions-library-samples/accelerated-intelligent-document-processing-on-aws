// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Which of the document page's actions a user is offered, and which of those apply now.
// Pure: the page's bar, menus, dialogs and table rows all read it.
//
// An action the user's role can never perform is not offered (`shown: false`). Its
// groups mirror the server's: ACTION_OPERATIONS names each action's API operations, and
// document-action-gates.rbac.test.ts derives the groups from
// scripts/api_rbac_expectations.yaml and compares them with ACTION_GROUPS, so the two
// cannot drift apart silently. Hiding is not security; the server enforces each policy.
//
// An action that is offered but does not apply now is disabled with its reason. The
// reasons are checked in one order and the first that applies wins: a past version is
// being viewed (H), the document is in a review queue (Q), it is still processing (P),
// its configuration is loading (C), it is in BDA mode (B), then the action's own
// availability. The actions that call processChanges are, for a caller who is not an
// Admin, also refused unless the caller holds the document's open review (O), as the
// server refuses them.
import type { IntlShape, NoMessageValues, TypedMessageDescriptor } from 'react-intl';

import { baselineCopyBlockedReason } from '../common/copy-to-baseline';
import { ABORTABLE_STATUSES } from '../common/document-statuses';
import { inReviewQueueEditReason, isHitlReviewFinished, isInReviewQueue, normalizeHitlStatus } from '../common/review-status';
import { documentEditStatusMessages, isEditableStatus } from '../edit-screen/document-edit-status';
import { actionReasonMessages } from './action-reasons';

export type DocumentActionId =
  | 'summary'
  | 'evaluation'
  | 'rule-validation'
  | 'cost'
  | 'flow'
  | 'regroup'
  | 'reextract'
  | 'page-text'
  | 'reset-class'
  | 'baseline'
  | 'skip-all'
  | 'send-to-review'
  | 'reprocess'
  | 'abort'
  | 'delete'
  | 'troubleshoot'
  | 'refresh'
  | 'start-review'
  | 'edit'
  | 'download';

export type AppGroup = 'Admin' | 'Author' | 'Reviewer' | 'Annotator' | 'Viewer';
export const ALL_GROUPS: readonly AppGroup[] = ['Admin', 'Author', 'Reviewer', 'Annotator', 'Viewer'];

type GatedAction = Exclude<DocumentActionId, 'edit'>;

/** The API operations each action calls; its users must be admitted by every one. */
export const ACTION_OPERATIONS: Record<GatedAction, { operations: string[] }> = {
  summary: { operations: ['getFileContents'] },
  evaluation: { operations: ['getFileContents'] },
  'rule-validation': { operations: ['getFileContents'] },
  // The metering comes with the document; pricing it reads the stack's pricing.
  cost: { operations: ['getPricing'] },
  flow: { operations: ['getStepFunctionExecution'] },
  regroup: { operations: ['updateDocumentSections'] },
  reextract: { operations: ['processChanges'] },
  // The page text is uploaded, then the document reprocessed: only a role both admit.
  'page-text': { operations: ['uploadDocument', 'processChanges'] },
  'reset-class': { operations: ['processChanges'] },
  baseline: { operations: ['copyToBaseline'] },
  'skip-all': { operations: ['skipAllSectionsReview'] },
  'send-to-review': { operations: ['sendToReview'] },
  reprocess: { operations: ['reprocessDocument'] },
  abort: { operations: ['abortWorkflow'] },
  delete: { operations: ['deleteDocument'] },
  troubleshoot: { operations: ['listAvailableAgents', 'submitAgentQuery', 'getAgentJobStatus'] },
  refresh: { operations: ['getDocument'] },
  'start-review': { operations: ['claimReview'] },
  download: { operations: ['getFileContents'] },
};

const ADMIN_REVIEWER: readonly AppGroup[] = ['Admin', 'Reviewer'];
const ADMIN_AUTHOR: readonly AppGroup[] = ['Admin', 'Author'];

/**
 * The groups offered each action. A group the server admits only for some objects
 * (`object_scoped_roles`, an Annotator's test sets) is not offered it here.
 */
export const ACTION_GROUPS: Record<GatedAction, readonly AppGroup[]> = {
  summary: ALL_GROUPS,
  evaluation: ALL_GROUPS,
  'rule-validation': ALL_GROUPS,
  cost: ['Admin', 'Author', 'Viewer'],
  flow: ALL_GROUPS,
  regroup: ADMIN_REVIEWER,
  reextract: ADMIN_REVIEWER,
  'page-text': ['Admin'],
  'reset-class': ADMIN_REVIEWER,
  baseline: ADMIN_AUTHOR,
  'skip-all': ADMIN_REVIEWER,
  'send-to-review': ADMIN_REVIEWER,
  reprocess: ADMIN_AUTHOR,
  abort: ADMIN_AUTHOR,
  delete: ADMIN_AUTHOR,
  troubleshoot: ['Admin', 'Author', 'Viewer'],
  refresh: ALL_GROUPS,
  'start-review': ADMIN_REVIEWER,
  download: ALL_GROUPS,
};

/** Edit outside a review queue: the direct edit, which decides what is editable for each role. */
export const EDIT_GROUPS: readonly AppGroup[] = ['Admin', 'Author', 'Reviewer'];

export interface ActionRoles {
  isAdmin: boolean;
  isAuthor: boolean;
  isReviewer: boolean;
  isAnnotator: boolean;
  isViewer: boolean;
  isReviewerOnly: boolean;
  canWorkReviewQueues: boolean;
}

export interface GatedDocument {
  objectKey: string;
  objectStatus?: string | null;
  hitlStatus?: string | null;
  hitlTriggered?: boolean | null;
  hitlReviewOwner?: string | null;
  hitlReviewOwnerEmail?: string | null;
  evaluationStatus?: string | null;
  summaryReportUri?: string | null;
  evaluationReportUri?: string | null;
  ruleValidationResultUri?: string | null;
  /** The document's metering has entries, so it has a cost to estimate. */
  hasCost?: boolean;
  executionArn?: string | null;
  sectionCount: number;
  pageCount: number;
  pagesWithText: number;
}

export interface GateInput {
  roles: ActionRoles;
  /** The live document. */
  doc: GatedDocument;
  /** What the page shows: the live document, or a past version's reports. */
  displayed: Pick<GatedDocument, 'summaryReportUri' | 'evaluationReportUri' | 'ruleValidationResultUri' | 'hasCost'>;
  isHistorical: boolean;
  /** The caller holds the document's review (its owner, by username or email). */
  holdsReview: boolean;
  bdaMode: boolean;
  configLoading: boolean;
  baselineRequestInFlight: boolean;
  /** Whether the Default review queue is on (``null``/absent = not known yet: the server decides). */
  defaultQueueEnabled?: boolean | null;
}

export interface ActionGate {
  shown: boolean;
  disabledReason?: string;
}

export type DocumentActionGates = Record<DocumentActionId, ActionGate> & {
  /** Edit opens the document's queue item. */
  editInQueue: boolean;
  /** Whether Edit Page Grouping may change a section's class (a Reviewer-only user's lock). */
  canChangeClass: boolean;
  /** Why Save and Re-extract can't run now (configuration, BDA mode, the review owner rule), for the board. */
  reextractBlockedReason?: string;
};

const GROUP_FLAG: Record<AppGroup, keyof ActionRoles> = {
  Admin: 'isAdmin',
  Author: 'isAuthor',
  Reviewer: 'isReviewer',
  Annotator: 'isAnnotator',
  Viewer: 'isViewer',
};

const inGroups = (roles: ActionRoles, groups: readonly AppGroup[]): boolean => groups.some((g) => roles[GROUP_FLAG[g]]);

/** Whether ``roles`` are offered ``action`` at all (the list and queue menus use it too). */
export const isOfferedTo = (roles: ActionRoles, action: GatedAction): boolean => inGroups(roles, ACTION_GROUPS[action]);

/** The first reason that applies, in order. */
const first = (...checks: [boolean, string | undefined][]): string | undefined => checks.find(([applies]) => applies)?.[1];

/**
 * Every action's gate for one document. With `intl`, every reason is in the UI's language;
 * without it, the English source text the exported reason constants hold.
 */
export const documentActionGates = (input: GateInput, intl?: IntlShape): DocumentActionGates => {
  const { roles, doc, displayed, isHistorical, holdsReview, bdaMode, configLoading, baselineRequestInFlight, defaultQueueEnabled } = input;
  // A reason in the UI's language; without `intl`, its English source text, the exported constant's value.
  const reason = (message: TypedMessageDescriptor<NoMessageValues>): string =>
    intl ? intl.formatMessage(message) : String(message.defaultMessage);
  const H: [boolean, string] = [isHistorical, reason(actionReasonMessages.historical)];
  const inQueue = isInReviewQueue(doc.hitlStatus);
  const Q: [boolean, string] = [inQueue, inReviewQueueEditReason(roles.canWorkReviewQueues, intl)];
  const processing = !isEditableStatus(doc.objectStatus);
  const P: [boolean, string] = [processing, reason(documentEditStatusMessages.processing)];
  const C: [boolean, string] = [configLoading, reason(actionReasonMessages.configLoading)];
  const B: [boolean, string] = [bdaMode, reason(actionReasonMessages.bdaReprocess)];

  const hitl = normalizeHitlStatus(doc.hitlStatus);
  const hasOwner = !!(doc.hitlReviewOwner || doc.hitlReviewOwnerEmail);
  const finished = isHitlReviewFinished(doc.hitlStatus);
  // A legacy review waiting for a reviewer (not one managed by a review queue).
  const pendingReview = !!doc.hitlTriggered && !hitl.queueManaged && !finished;
  // processChanges for a caller who is not an Admin: refused on a finished review, and
  // unless the caller holds the document's open review (pending or in progress).
  const nonAdmin = !roles.isAdmin;
  const reviewOpen = ['pending', 'in_progress'].includes(hitl.kind);
  const O: [boolean, string][] = [
    [nonAdmin && finished, reason(actionReasonMessages.reviewerFinished)],
    [nonAdmin && reviewOpen && !hasOwner, reason(actionReasonMessages.reviewerUnclaimed)],
    [nonAdmin && !(reviewOpen && holdsReview), reason(actionReasonMessages.reviewOwner)],
  ];

  const gate = (action: GatedAction, ...checks: [boolean, string | undefined][]): ActionGate => ({
    shown: inGroups(roles, ACTION_GROUPS[action]),
    disabledReason: first(...checks),
  });

  const editInQueue = inQueue && roles.canWorkReviewQueues;

  return {
    // Reports. Summary is about the document as shown: a past version's summary is
    // available whatever the live document is doing.
    summary: gate(
      'summary',
      [!isHistorical && inQueue, reason(documentEditStatusMessages.inReviewShort)],
      [!isHistorical && processing, reason(documentEditStatusMessages.processing)],
      [!displayed.summaryReportUri, reason(documentEditStatusMessages.noSummary)],
    ),
    evaluation: gate('evaluation', [!displayed.evaluationReportUri, reason(actionReasonMessages.noEvaluationReport)]),
    'rule-validation': gate(
      'rule-validation',
      [isHistorical, reason(actionReasonMessages.ruleValidationHistorical)],
      [!displayed.ruleValidationResultUri, reason(actionReasonMessages.noRuleValidation)],
    ),
    // About the document as shown: a past version's cost is its own.
    cost: gate('cost', [!displayed.hasCost, reason(actionReasonMessages.noCost)]),
    flow: gate('flow', [!doc.executionArn, reason(actionReasonMessages.noExecution)]),
    // More Actions.
    regroup: gate(
      'regroup',
      H,
      Q,
      P,
      C,
      // A BDA reprocess keeps the sections a document already has.
      [bdaMode, reason(documentEditStatusMessages.bdaRegroup)],
      // The server refuses to regroup a legacy review in progress.
      [doc.objectStatus === 'HITL_IN_PROGRESS' && !hitl.queueManaged, reason(actionReasonMessages.legacyReviewRegroup)],
      [doc.sectionCount === 0, reason(documentEditStatusMessages.noSections)],
    ),
    reextract: gate('reextract', H, Q, P, C, B, ...O, [doc.sectionCount === 0, reason(documentEditStatusMessages.noSections)]),
    'page-text': gate('page-text', H, Q, P, C, B, ...O, [doc.pagesWithText === 0, reason(actionReasonMessages.noPageText)]),
    'reset-class': gate('reset-class', H, Q, P, C, B, ...O, [doc.pageCount === 0, reason(actionReasonMessages.noPages)]),
    baseline: gate(
      'baseline',
      H,
      Q,
      P,
      [baselineRequestInFlight, reason(actionReasonMessages.baselineCopying)],
      [!!baselineCopyBlockedReason(doc.evaluationStatus), baselineCopyBlockedReason(doc.evaluationStatus, intl)],
    ),
    'skip-all': gate(
      'skip-all',
      H,
      [hitl.queueManaged && hitl.kind === 'in_review', inReviewQueueEditReason(roles.canWorkReviewQueues, intl)],
      [!pendingReview, reason(actionReasonMessages.noPendingReview)],
    ),
    'send-to-review': gate(
      'send-to-review',
      H,
      [defaultQueueEnabled === false, reason(actionReasonMessages.reviewQueueOff)],
      [doc.objectStatus !== 'COMPLETED', reason(actionReasonMessages.notFinished)],
      [(hitl.queueManaged && hitl.kind === 'in_review') || pendingReview, reason(actionReasonMessages.alreadyInReview)],
    ),
    reprocess: gate('reprocess', H),
    abort: gate('abort', H, [!ABORTABLE_STATUSES.includes(String(doc.objectStatus ?? '')), reason(actionReasonMessages.notAbortable)]),
    delete: gate('delete', H),
    // Buttons.
    download: gate('download'),
    troubleshoot: gate('troubleshoot'),
    refresh: gate('refresh', H),
    'start-review': {
      shown: inGroups(roles, ACTION_GROUPS['start-review']) && pendingReview && !hasOwner,
      disabledReason: first(H),
    },
    edit: {
      shown: editInQueue || inGroups(roles, EDIT_GROUPS),
      disabledReason: first(H, [!editInQueue && processing, reason(documentEditStatusMessages.processing)]),
    },
    editInQueue,
    canChangeClass: !(roles.isReviewerOnly && ((!!doc.hitlTriggered && !hasOwner) || processing || finished)),
    reextractBlockedReason: first(C, B, ...O),
  };
};
