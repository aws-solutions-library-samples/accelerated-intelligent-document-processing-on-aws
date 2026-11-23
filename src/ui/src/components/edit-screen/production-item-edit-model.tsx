// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit page's model for a production review item: the shared editor over the item's
// sections, edits autosaved as drafts (and Save), then Process Changes, which asks how
// the review ends: complete it (the run that follows applies the corrections and
// continues from there; a section added or changed by regrouping is extracted again
// first), or send the document back to be processed again (the corrections and any page
// regrouping are discarded). The server decides where each run starts, and the dialog
// shows what it will do. Reject ends the review with the document rejected, and
// Skip gives the item back to its queue (the draft is kept). Cancel leaves without
// finishing. The page grouping can be changed during the review, except in BDA mode,
// where one section cannot be extracted alone. Only the assignee edits. Any Annotator
// listed on the item's queue can open it read-only, and Claim an unassigned item to work
// it. An Admin edits an unassigned item directly: the first draft save, or Process
// Changes, assigns it to them. An item assigned to someone else is view-only for an Admin
// until they Take over.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useLocation, useSearchParams } from 'react-router-dom';
import { Box, Button, Link } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type MessageTag, type NoMessageValues } from 'react-intl';

import useAppContext from '../../contexts/app';
import { useOptionalReviewWork } from '../../hooks/use-review-work';
import useUserRole from '../../hooks/use-user-role';
import { describeApiError } from '../../hooks/utils/graphql-error';
import { MY_QUEUES_PATH, documentEditHref, parseEditOrigin, productionQueuePath } from '../../routes/constants';
import type { ReviewItem, ReviewItemDetail } from '../../graphql/generated/operation-types';
import { baselineCopyBlockedReason, copyDocumentToBaseline } from '../common/copy-to-baseline';
import { saveDocumentGroupingOrThrow } from '../common/document-grouping';
import { PROCESS_CHANGES_LABEL } from '../common/labels';
import { QueueItemState, reviewItemStateView } from '../common/review-status';
import { NO_RULE_VALIDATION_REASON } from '../document-actions/action-reasons';
import { editActiveHref, editTrail } from '../review-queues/breadcrumbs';
import { claimItem, documentName, rejectItem, releaseItem, reviewErrorCode, sendBackItem, takeOverItem } from '../review-queues/review-api';
import ReviewTaskContext from '../review-queues/ReviewTaskContext';
import { productionWorkItemId } from '../review-queues/work-item-ids';
import type { ProcessingMode } from '../run-plan/run-steps';
import { BDA_REGROUP_REASON } from './document-edit-status';
import type { EditAction, ReviewOutcomeAction } from './EditScreen';
import type { EditModel, EditNavigation } from './edit-model';
import { AUTOSAVE_MS, useProductionItemEdit } from './production-item-edit-adapter';
import { useLeavePath } from './use-edit-exit';
import type { RawDocument } from './use-edit-mode';
import type { EditorSession } from './use-editor-session';

const messages = defineMessages<{
  primaryHint: NoMessageValues;
  rejectHeader: NoMessageValues;
  rejectBody: NoMessageValues;
  rejectConfirm: NoMessageValues;
  noRejectReasons: NoMessageValues;
  rejected: { readonly name: string };
  sentBack: { readonly name: string };
  failedReject: NoMessageValues;
  failedSendBack: NoMessageValues;
  regroupConsequence: { readonly b: MessageTag; readonly processChanges: string };
}>({
  regroupConsequence: {
    id: 'editScreen.productionItem.regroupConsequence',
    defaultMessage:
      "Moving pages rewrites this document's <b>section grouping</b>. When you choose <b>{processChanges}</b> and complete the review, a section you added or whose pages you changed is <b>extracted again</b>, and the corrections you saved are applied afterwards. The other sections keep their extracted values and your draft edits. A section you remove takes its draft edits with it.",
    description:
      'What changing the page grouping of a document under review does, shown in the Edit Page Grouping dialog. <b> marks emphasis. {processChanges} is the label of the Process Changes button.',
  },
  primaryHint: {
    id: 'editScreen.productionItem.primaryHint',
    defaultMessage: 'Finish the review: complete it with your corrections, or send the document back to be processed again.',
    description: 'Hover text of Process Changes on a review item. Process Changes opens a dialog with those two endings.',
  },
  rejectHeader: {
    id: 'editScreen.productionItem.rejectHeader',
    defaultMessage: 'Reject this document',
    description: 'Header of the dialog that asks why a document under review is rejected.',
  },
  rejectBody: {
    id: 'editScreen.productionItem.rejectBody',
    defaultMessage:
      'The review ends and the document is rejected. Its extracted values stay as they were before the review: your corrections are not applied, and evaluation and summarization do not run.',
    description: 'What rejecting does, in the dialog that asks why a document under review is rejected.',
  },
  rejectConfirm: {
    id: 'editScreen.productionItem.rejectConfirm',
    defaultMessage: 'Reject',
    description: 'Button that confirms rejecting a document under review, in the dialog that asks for the reason.',
  },
  noRejectReasons: {
    id: 'editScreen.productionItem.noRejectReasons',
    defaultMessage: 'This queue lists no reject reasons',
    description: 'Why Reject is disabled on a review item: its queue has no reason to choose from.',
  },
  rejected: {
    id: 'editScreen.productionItem.rejected',
    defaultMessage: '{name} was rejected.',
    description: 'Success notification after a document under review is rejected. {name} is the name of the document.',
  },
  sentBack: {
    id: 'editScreen.productionItem.sentBack',
    defaultMessage: '{name} was sent back and is being processed again.',
    description: 'Success notification after a document under review is sent back for reprocessing. {name} is the name of the document.',
  },
  failedReject: {
    id: 'editScreen.productionItem.failedReject',
    defaultMessage: 'reject the document',
    description:
      'Completes the sentences "Failed to …:" and "You do not have permission to …" when rejecting a document under review fails.',
  },
  failedSendBack: {
    id: 'editScreen.productionItem.failedSendBack',
    defaultMessage: 'send the document back',
    description:
      'Completes the sentences "Failed to …:" and "You do not have permission to …" when sending a document under review back fails.',
  },
});

/** Renders a `<b>...</b>` element value: emphasis inside a sentence. */
const bold = (chunks: React.ReactNode[]): React.JSX.Element => <b>{chunks}</b>;

/** The processing mode an item records for a document processed with Bedrock Data Automation. */
const BDA_MODE: ProcessingMode = 'bda';

/** While the editor is saving, or holds edits it has not saved yet. */
const SAVING_EDITS = 'Saving your latest edits…';

/** Why Summary and Use As Evaluation Baseline wait: they are about the reviewed document. */
const REVIEW_NOT_COMPLETE = 'Available once the review is complete';

/**
 * Why an Annotator who may claim the item can't regroup it yet: only the item's holder
 * may change its page grouping (the server's rule), so the Annotator claims it first.
 */
export const CLAIM_TO_REGROUP_REASON = 'Claim this item first to change its page grouping';

/** Notices for the in-review states that are not open for editing, each saying what happens next. */
export const HOLDING_NOTICE = 'This item is being prepared for review. It can be edited once it reaches its queue.';
export const FINALIZE_FAILED_NOTICE =
  "Applying this review's corrections failed, so it can no longer be edited here. An Admin can see it in Review Management.";

/** What a failed action was trying to do, in the error it reports (Reject and send-back are messages). */
const FAILED_ACTION: Record<Exclude<EditAction, ReviewOutcomeAction>, string> = {
  claim: 'claim the item',
  skip: 'skip the item',
  baseline: 'use this document as the evaluation baseline',
  regroup: 'start editing the page grouping',
  process: 'process the changes',
  save: 'save your draft',
  cancel: 'leave the item',
  refresh: 'reload the item',
};

/** An Annotator was assigned the item between the Admin opening it and editing it. */
class AssignedElsewhere extends Error {}

/**
 * `seed`: the item and the document as the edit page read them to choose this mode, so
 * neither is read again on arrival.
 */
export const useProductionItemEditModel = (
  objectKey: string,
  session: EditorSession,
  seed?: { detail: ReviewItemDetail; document: RawDocument },
): EditModel => {
  const intl = useIntl();
  const adapter = useProductionItemEdit(objectKey, seed);
  const { detail, loadError, reload } = adapter;
  const { user, setErrorMessage, setSuccessMessage } = useAppContext();
  const { isAdmin, isQueueWorker, canWrite } = useUserRole();
  const reviewWork = useOptionalReviewWork();
  const leavePath = useLeavePath();
  const [searchParams] = useSearchParams();
  const location = useLocation();
  const focusField = searchParams.get('field');
  const initialSectionId = searchParams.get('section');
  const [takingOver, setTakingOver] = useState(false);
  /** Who was assigned the item just as this Admin tried to edit or finish it. */
  const [assignedJustNow, setAssignedJustNow] = useState<string | null>(null);
  // The latest item read, and the one claim in flight: concurrent saves share it.
  const itemRef = useRef<ReviewItem | null>(null);
  const assigningRef = useRef<Promise<boolean> | null>(null);

  useEffect(() => {
    itemRef.current = detail?.item ?? null;
  }, [detail]);

  /**
   * Before an Admin's first draft save or Process Changes on an unassigned item: claim it
   * (once, however many saves are waiting) and re-read it. Resolves true when the item
   * is the caller's; false when an Annotator was assigned it first.
   */
  const ensureAssigned = useCallback(async (): Promise<boolean> => {
    const current = itemRef.current;
    if (!current) return false;
    if (current.state === QueueItemState.CLAIMED && current.isMine) return true;
    if (!assigningRef.current) {
      if (current.state !== QueueItemState.OPEN) return false;
      assigningRef.current = (async () => {
        try {
          await claimItem(current);
          const fresh = await reload();
          itemRef.current = fresh?.item ?? { ...current, state: QueueItemState.CLAIMED, isMine: true };
          void reviewWork?.refresh();
          return true;
        } catch (err) {
          if (reviewErrorCode(err) !== 'ALREADY_CLAIMED') throw err;
          const fresh = await reload();
          if (fresh) itemRef.current = fresh.item;
          setAssignedJustNow(fresh?.item.ownerEmail || 'another reviewer');
          return false;
        } finally {
          assigningRef.current = null;
        }
      })();
    }
    return assigningRef.current;
  }, [reload, reviewWork]);

  const saveSection = useCallback(
    async (sectionId: string, data: Record<string, unknown>): Promise<void> => {
      if (!(await ensureAssigned())) throw new AssignedElsewhere('This item was just assigned to someone else');
      await adapter.saveSection(sectionId, data);
    },
    [ensureAssigned, adapter.saveSection],
  );

  const buildFieldLink = useCallback(
    (fieldPath: string) => `${window.location.origin}/${documentEditHref(objectKey, { field: fieldPath })}`,
    [objectKey],
  );

  const queueId = detail?.item.queue;
  const queueCrumb = queueId ? { text: detail?.queueName || queueId, href: `#${productionQueuePath(queueId)}` } : null;
  // The trail the user came by; without one, an Admin works items from Review Management
  // and an Annotator from Tasks.
  const origin = parseEditOrigin(location.search) ?? (isAdmin ? 'review' : 'tasks');
  const navigation: EditNavigation = {
    trail: editTrail({
      origin,
      objectKey,
      name: documentName(objectKey),
      queue: queueCrumb ? { crumb: queueCrumb, view: 'production' } : null,
    }),
    activeHref: editActiveHref(origin),
  };

  if (loadError && !detail) {
    const code = reviewErrorCode(loadError);
    const notAssigned = code === 'NOT_ASSIGNED' || code === 'NOT_A_MEMBER';
    return {
      navigation,
      state: {
        kind: 'failure',
        type: notAssigned ? 'warning' : 'error',
        header: notAssigned ? "You can't open this review item" : "Couldn't open this review item",
        body: notAssigned ? (
          <>
            Only the Annotators listed on this item&apos;s queue can open it. The queues that list you are in{' '}
            <Link href={`#${MY_QUEUES_PATH}`}>Home › Queues</Link>.
          </>
        ) : (
          describeApiError(loadError, 'load the review item')
        ),
        retry: notAssigned ? undefined : () => void reload(),
      },
    };
  }
  if (!detail) return { navigation, state: { kind: 'loading', text: 'Loading the review item…' } };

  const { item } = detail;
  const name = documentName(item.objectKey);
  const stateView = reviewItemStateView(item.state);
  const assigned = item.state === QueueItemState.CLAIMED;
  const terminal = !assigned && item.state !== QueueItemState.OPEN;
  const assignee = assigned ? item.ownerEmail : null;
  const assignedToMe = assigned && item.isMine;
  // An Admin edits an unassigned item straight away; the first save or Process Changes assigns it to them.
  const adminOnOpen = isAdmin && item.state === QueueItemState.OPEN;
  const canEdit = assignedToMe || adminOnOpen;
  // An Admin takes over an item assigned to someone else before editing it.
  const canTakeOver = isAdmin && assigned && !assignedToMe;
  // An Annotator (not an Admin, whose edits assign the item) claims an item waiting for review.
  const canClaim = isQueueWorker && !isAdmin && item.state === QueueItemState.OPEN;
  // Summary and Use as Evaluation Baseline are about the finished document: only once its
  // review is complete and the corrections have been applied.
  const finished = item.state === QueueItemState.COMPLETE;
  const { dirty, saving } = session;

  let lockedReason: string | undefined;
  if (terminal) lockedReason = `This item is ${stateView.label.toLowerCase()}`;
  else if (!canEdit) lockedReason = assignee ? `Assigned to ${assignee}` : 'This item is not assigned to you';

  // Why the fields can't be edited, shown first: with Take over for an Admin on someone
  // else's item, and Claim (from the bar's claim) for an Annotator on an unassigned one.
  let readOnlyReason: string | undefined;
  if (terminal)
    readOnlyReason =
      item.state === QueueItemState.HOLDING
        ? HOLDING_NOTICE
        : item.state === QueueItemState.FINALIZE_FAILED
          ? FINALIZE_FAILED_NOTICE
          : item.state === 'FINALIZING'
            ? 'This review is finished: it was submitted and its corrections are being applied.'
            : `This review is finished: the item is ${stateView.label.toLowerCase()} and can no longer be edited.`;
  else if (canTakeOver)
    readOnlyReason = assignedJustNow
      ? `Assigned to ${assignee ?? assignedJustNow} just now. Take over to edit it and process its changes.`
      : `Assigned to ${assignee}. Take over to edit it and process its changes.`;
  else if (!canEdit)
    readOnlyReason = assignee
      ? `View only. This item is assigned to ${assignee}.`
      : canClaim
        ? 'View only. Claim this item to work on it; it is then assigned to you and shows in Home › Tasks.'
        : 'View only.';

  const queuePath = productionQueuePath(item.queue);
  const workItemId = productionWorkItemId(objectKey);
  // The mode of the run that placed the document. A BDA run cannot extract one section
  // alone, so the page grouping stays as it is during the review (the server refuses a
  // change too).
  const bdaMode = item.processingMode === BDA_MODE;
  const failedAction = (action: EditAction): string => {
    if (action === 'reject') return intl.formatMessage(messages.failedReject);
    if (action === 'send-back') return intl.formatMessage(messages.failedSendBack);
    return FAILED_ACTION[action];
  };

  const handleTakeOver = async () => {
    setTakingOver(true);
    try {
      await takeOverItem(item);
      setAssignedJustNow(null);
      setSuccessMessage(`${name} is assigned to you. ${assignee ?? 'The former assignee'} was told.`);
      void reviewWork?.refresh();
    } catch (err) {
      setErrorMessage(describeApiError(err, 'take over the item'));
    } finally {
      setTakingOver(false);
      void reload();
    }
  };

  return {
    navigation,
    state: {
      kind: 'ready',
      header: {
        title: name,
        status: stateView.label,
        objectKey: item.objectKey,
        source: { objectKey },
        summary: {
          reportUri: finished ? adapter.summaryReportUri || undefined : undefined,
          unavailableReason: finished ? 'This document has no summary' : REVIEW_NOT_COMPLETE,
        },
        // Policy rule validation ran before the document was placed: its report is there to read during the review.
        ruleValidation: { reportUri: adapter.ruleValidationResultUri || undefined, unavailableReason: NO_RULE_VALIDATION_REASON },
        offerReport: true,
      },
      // One alert for the screen's notices, so the space stays given over to the review.
      notices: [
        adminOnOpen && {
          id: 'unassigned',
          type: 'info',
          content: `Not assigned yet. Editing it, or choosing ${PROCESS_CHANGES_LABEL}, assigns it to you.`,
        },
      ],
      // A reason's chip, or Next alert, moves to its field.
      context: <ReviewTaskContext detail={detail} onReasonClick={(r) => session.focusField(r.sectionId, r.field ?? '')} />,
      bar: {
        primaryDisabledReason: lockedReason ?? (saving ? SAVING_EDITS : undefined),
        primaryHint: intl.formatMessage(messages.primaryHint),
        // Skip gives the item back to its queue: only its holder can.
        showSkip: assignedToMe,
        skipDisabledReason: dirty ? SAVING_EDITS : undefined,
        // Reject ends the review: offered to whoever may finish it, as Process Changes is.
        showReject: canEdit,
        rejectDisabledReason:
          detail.rejectReasons.length === 0 ? intl.formatMessage(messages.noRejectReasons) : dirty || saving ? SAVING_EDITS : undefined,
        showCancel: canEdit,
        cancelDisabledReason: saving ? SAVING_EDITS : undefined,
        showSave: canEdit,
        saveDisabledReason: !dirty ? 'No changes to save' : saving ? SAVING_EDITS : undefined,
        showRefresh: true,
        refreshDisabledReason: saving ? SAVING_EDITS : undefined,
        showRegroup: true,
        regroupDisabledReason: bdaMode
          ? BDA_REGROUP_REASON
          : // The item's holder regroups, Annotator or Admin, as the server allows.
            ((!terminal && canClaim ? CLAIM_TO_REGROUP_REASON : lockedReason) ?? (dirty ? SAVING_EDITS : undefined)),
        // copyToBaseline is Admin and Author (canWrite), as on the document page.
        showBaseline: canWrite,
        baselineDisabledReason: finished ? baselineCopyBlockedReason(adapter.evaluationStatus) : REVIEW_NOT_COMPLETE,
        showClaim: canClaim,
        offerHistory: true,
      },
      editor: {
        sections: adapter.sections,
        pages: adapter.pages,
        pagesLoading: adapter.pagesLoading,
        readOnly: !canEdit,
        readOnlyReason,
        readOnlyAction: canTakeOver ? (
          <Button onClick={() => void handleTakeOver()} loading={takingOver} disabled={session.busy !== null}>
            Take over
          </Button>
        ) : undefined,
        loadSection: adapter.loadSection,
        saveSection,
        recordEditHistory: { editedBy: user?.username || 'unknown', source: 'review-queue' },
        reloadToken: adapter.reloadToken,
        autosaveMs: canEdit ? AUTOSAVE_MS : undefined,
        confirmSectionSwitch: false,
        initialSectionId,
        focusFieldPath: focusField,
        buildFieldLink,
        defaultFilter: item.reasonCount > 0 ? 'confidence-alerts' : 'none',
        emptyText: 'This item has no sections to review.',
      },
      board: {
        pages: { kind: 'thumbnails', pages: adapter.pages },
        // Every section of the document, not only those under review: a grouping covers every page.
        sections: () =>
          adapter.documentSections.map((s) => ({
            sectionId: s.sectionId,
            documentClass: s.documentClass ?? null,
            pageIds: s.pageIds.map((id) => Number(id)),
          })),
        canChangeClass: false,
        consequence: <FormattedMessage {...messages.regroupConsequence} values={{ b: bold, processChanges: PROCESS_CHANGES_LABEL }} />,
        // Regrouping is part of review: like a first save, it assigns an unassigned item to
        // the Admin opening it, since only the item's holder may regroup.
        beforeOpen: async () => {
          if (!(await ensureAssigned())) throw new AssignedElsewhere();
          return true;
        },
        save: async (next) => {
          const message = await saveDocumentGroupingOrThrow(objectKey, next, (err) => describeApiError(err, 'save the page grouping'));
          await Promise.all([reload(), adapter.refreshDocument()]);
          return message;
        },
      },
      history: { reviewActivity: detail.history },
      skip: {
        header: 'Skip this item',
        reasons: detail.skipReasons,
        body: <Box>It goes back to its queue, unassigned and not finished. Your saved edits are kept for whoever picks it up next.</Box>,
        run: async (reason, note) => {
          await releaseItem(item, reason, note);
          return { to: leavePath(queuePath), message: `${name} returned to the queue.`, workItemId };
        },
      },
      reject: {
        header: intl.formatMessage(messages.rejectHeader),
        reasons: detail.rejectReasons,
        body: <Box>{intl.formatMessage(messages.rejectBody)}</Box>,
        confirmLabel: intl.formatMessage(messages.rejectConfirm),
        run: async (reason, note) => {
          // Only a held item can be rejected: an Admin on an unassigned one is assigned it first.
          if (!(await ensureAssigned())) throw new AssignedElsewhere();
          await rejectItem(item, reason, note);
          return { to: leavePath(queuePath), message: intl.formatMessage(messages.rejected, { name }), workItemId };
        },
      },
      sendBack: {
        // The server allows the holder, and an Admin on an unassigned item without assigning it.
        // It also decides where the run starts: nothing but the note is sent.
        run: async (note) => {
          await sendBackItem(item, note);
          return { to: leavePath(queuePath), message: intl.formatMessage(messages.sentBack, { name }), workItemId };
        },
      },
      process: {
        // Asked first: complete the review, or send the document back. The dialog reads what each ending runs.
        confirm: { kind: 'review-completion', objectKey: item.objectKey, cycleId: item.cycleId },
        run: async () => {
          if (!(await ensureAssigned())) throw new AssignedElsewhere();
          // The latest edits are saved as a draft first, so the finalize run applies them.
          const editor = session.controllerRef.current;
          if (editor?.isDirty() && !(await editor.save())) throw new Error('Your latest edits could not be saved');
          await adapter.complete();
          return {
            to: leavePath(queuePath),
            message: `Processing the changes to ${name}: your corrections are being applied.`,
            workItemId,
          };
        },
      },
      claim: async () => {
        reviewWork?.beginMutation();
        try {
          await claimItem(item);
          await reload();
        } finally {
          void reviewWork?.refresh();
        }
        return `Claimed ${name}. It is in Home › Tasks.`;
      },
      baseline: async () => {
        await copyDocumentToBaseline(objectKey);
        void adapter.refreshDocument();
        return `Copying ${name} to the evaluation baseline.`;
      },
      refresh: adapter.refresh,
      cancelTo: leavePath(queuePath),
      failures: {
        surface: 'flash',
        // Losing the item to an Annotator is shown in the notices, not as an error.
        describe: (action, err) => (err instanceof AssignedElsewhere ? null : describeApiError(err, failedAction(action))),
        after: () => void reload(),
      },
    },
  };
};
