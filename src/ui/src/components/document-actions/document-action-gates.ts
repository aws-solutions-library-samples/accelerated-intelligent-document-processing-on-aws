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
import { baselineCopyBlockedReason } from '../common/copy-to-baseline';
import { ABORTABLE_STATUSES } from '../common/document-statuses';
import { inReviewQueueEditReason, isHitlReviewFinished, isInReviewQueue, normalizeHitlStatus } from '../common/review-status';
import {
  BDA_REGROUP_REASON,
  IN_REVIEW_SHORT_REASON,
  NO_SECTIONS_REASON,
  NO_SUMMARY_REASON,
  PROCESSING_REASON,
  isEditableStatus,
} from '../edit-screen/document-edit-status';
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
} from './action-reasons';

export type DocumentActionId =
  | 'summary'
  | 'evaluation'
  | 'rule-validation'
  | 'flow'
  | 'regroup'
  | 'reextract'
  | 'page-text'
  | 'reset-class'
  | 'baseline'
  | 'skip-all'
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
  flow: { operations: ['getStepFunctionExecution'] },
  regroup: { operations: ['updateDocumentSections'] },
  reextract: { operations: ['processChanges'] },
  // The page text is uploaded, then the document reprocessed: only a role both admit.
  'page-text': { operations: ['uploadDocument', 'processChanges'] },
  'reset-class': { operations: ['processChanges'] },
  baseline: { operations: ['copyToBaseline'] },
  'skip-all': { operations: ['skipAllSectionsReview'] },
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
  flow: ALL_GROUPS,
  regroup: ADMIN_REVIEWER,
  reextract: ADMIN_REVIEWER,
  'page-text': ['Admin'],
  'reset-class': ADMIN_REVIEWER,
  baseline: ADMIN_AUTHOR,
  'skip-all': ADMIN_REVIEWER,
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
  displayed: Pick<GatedDocument, 'summaryReportUri' | 'evaluationReportUri' | 'ruleValidationResultUri'>;
  isHistorical: boolean;
  /** The caller holds the document's review (its owner, by username or email). */
  holdsReview: boolean;
  bdaMode: boolean;
  configLoading: boolean;
  baselineRequestInFlight: boolean;
}

export interface ActionGate {
  shown: boolean;
  disabledReason?: string;
}

export type DocumentActionGates = Record<DocumentActionId, ActionGate> & {
  /** Edit opens the document's queue item. */
  editInQueue: boolean;
  /** Whether Edit Page Class may change a section's class (a Reviewer-only user's lock). */
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

/** The first reason that applies, in order. */
const first = (...checks: [boolean, string | undefined][]): string | undefined => checks.find(([applies]) => applies)?.[1];

export const documentActionGates = (input: GateInput): DocumentActionGates => {
  const { roles, doc, displayed, isHistorical, holdsReview, bdaMode, configLoading, baselineRequestInFlight } = input;
  const H: [boolean, string] = [isHistorical, HISTORICAL_REASON];
  const inQueue = isInReviewQueue(doc.hitlStatus);
  const Q: [boolean, string] = [inQueue, inReviewQueueEditReason(roles.canWorkReviewQueues)];
  const processing = !isEditableStatus(doc.objectStatus);
  const P: [boolean, string] = [processing, PROCESSING_REASON];
  const C: [boolean, string] = [configLoading, CONFIG_LOADING_REASON];
  const B: [boolean, string] = [bdaMode, BDA_REPROCESS_REASON];

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
    [nonAdmin && finished, REVIEWER_FINISHED_REASON],
    [nonAdmin && reviewOpen && !hasOwner, REVIEWER_UNCLAIMED_REASON],
    [nonAdmin && !(reviewOpen && holdsReview), REVIEW_OWNER_REASON],
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
      [!isHistorical && inQueue, IN_REVIEW_SHORT_REASON],
      [!isHistorical && processing, PROCESSING_REASON],
      [!displayed.summaryReportUri, NO_SUMMARY_REASON],
    ),
    evaluation: gate('evaluation', [!displayed.evaluationReportUri, NO_EVALUATION_REPORT_REASON]),
    'rule-validation': gate(
      'rule-validation',
      [isHistorical, RULE_VALIDATION_HISTORICAL_REASON],
      [!displayed.ruleValidationResultUri, NO_RULE_VALIDATION_REASON],
    ),
    flow: gate('flow', [!doc.executionArn, NO_EXECUTION_REASON]),
    // More Actions.
    regroup: gate(
      'regroup',
      H,
      Q,
      P,
      C,
      // A BDA reprocess keeps the sections a document already has.
      [bdaMode, BDA_REGROUP_REASON],
      // The server refuses to regroup a legacy review in progress.
      [doc.objectStatus === 'HITL_IN_PROGRESS' && !hitl.queueManaged, LEGACY_REVIEW_REGROUP_REASON],
      [doc.sectionCount === 0, NO_SECTIONS_REASON],
    ),
    reextract: gate('reextract', H, Q, P, C, B, ...O, [doc.sectionCount === 0, NO_SECTIONS_REASON]),
    'page-text': gate('page-text', H, Q, P, C, B, ...O, [doc.pagesWithText === 0, NO_PAGE_TEXT_REASON]),
    'reset-class': gate('reset-class', H, Q, P, C, B, ...O, [doc.pageCount === 0, NO_PAGES_REASON]),
    baseline: gate(
      'baseline',
      H,
      Q,
      P,
      [baselineRequestInFlight, BASELINE_COPYING_REASON],
      [!!baselineCopyBlockedReason(doc.evaluationStatus), baselineCopyBlockedReason(doc.evaluationStatus)],
    ),
    'skip-all': gate(
      'skip-all',
      H,
      [hitl.queueManaged && hitl.kind === 'in_review', inReviewQueueEditReason(roles.canWorkReviewQueues)],
      [!pendingReview, NO_PENDING_REVIEW_REASON],
    ),
    reprocess: gate('reprocess', H),
    abort: gate('abort', H, [!ABORTABLE_STATUSES.includes(String(doc.objectStatus ?? '')), NOT_ABORTABLE_REASON]),
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
      disabledReason: first(H, [!editInQueue && processing, PROCESSING_REASON]),
    },
    editInQueue,
    canChangeClass: !(roles.isReviewerOnly && ((!!doc.hitlTriggered && !hasOwner) || processing || finished)),
    reextractBlockedReason: first(C, B, ...O),
  };
};
