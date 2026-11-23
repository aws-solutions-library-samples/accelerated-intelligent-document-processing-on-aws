// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit page's model for a production review item: the shared editor over the item's
// sections, edits autosaved as drafts (and Save), then Process Changes (finish the
// review; the finalize run applies the corrections and re-runs evaluation and
// summarization, reusing the document's OCR, classification and extraction) or Skip
// (give the item back to its queue; the draft is kept). Cancel leaves without
// finishing. Only the assignee edits. Any Annotator listed on the item's queue can open
// it read-only, and Claim an unassigned item to work it. An Admin edits an unassigned
// item directly: the first draft save, or Process Changes, assigns it to them. An item
// assigned to someone else is view-only for an Admin until they Take over.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useLocation, useSearchParams } from 'react-router-dom';
import { defineMessages, FormattedMessage, useIntl, type IntlShape, type MessageTag, type NoMessageValues } from 'react-intl';
import { Box, Button } from '@cloudscape-design/components';

import useAppContext from '../../contexts/app';
import { useOptionalReviewWork } from '../../hooks/use-review-work';
import useUserRole from '../../hooks/use-user-role';
import { describeApiError } from '../../hooks/utils/graphql-error';
import { documentEditHref, parseEditOrigin, productionQueuePath } from '../../routes/constants';
import type { ReviewItem, ReviewItemDetail } from '../../graphql/generated/operation-types';
import { baselineCopyBlockedReason, copyDocumentToBaseline } from '../common/copy-to-baseline';
import { saveDocumentGroupingOrThrow } from '../common/document-grouping';
import { sharedLabelMessages } from '../common/labels';
import { QueueItemState, reviewItemStateView } from '../common/review-status';
import { editActiveHref, editTrail } from '../review-queues/breadcrumbs';
import { claimItem, documentName, releaseItem, reviewErrorCode, takeOverItem } from '../review-queues/review-api';
import ReviewTaskContext from '../review-queues/ReviewTaskContext';
import { productionWorkItemId } from '../review-queues/work-item-ids';
import { documentEditStatusMessages } from './document-edit-status';
import { EditPageError, editPageErrorText, editScreenSharedMessages, failedActionMessages } from './edit-messages';
import type { EditModel, EditNavigation } from './edit-model';
import { itemFailedModel, notAMemberModel } from './edit-mode-states';
import { AUTOSAVE_MS, useProductionItemEdit } from './production-item-edit-adapter';
import { useLeavePath } from './use-edit-exit';
import type { RawDocument } from './use-edit-mode';
import type { EditorSession } from './use-editor-session';

type Name = { readonly name: string };
type Assignee = { readonly assignee: string };
type ProcessChanges = { readonly processChanges: string };
type State = { readonly state: string };

const messages = defineMessages<{
  reviewNotComplete: NoMessageValues;
  claimToRegroup: NoMessageValues;
  holdingNotice: NoMessageValues;
  finalizeFailedNotice: NoMessageValues;
  takeOverAction: NoMessageValues;
  saveGroupingAction: NoMessageValues;
  assignedElsewhere: NoMessageValues;
  latestEditsNotSaved: NoMessageValues;
  staleDraft: NoMessageValues;
  loading: NoMessageValues;
  lockedHolding: NoMessageValues;
  lockedFinalizing: NoMessageValues;
  lockedComplete: NoMessageValues;
  lockedRejected: NoMessageValues;
  lockedFinalizeFailed: NoMessageValues;
  lockedWithdrawn: NoMessageValues;
  lockedOther: State;
  notAssignedToYou: NoMessageValues;
  finishedFinalizing: NoMessageValues;
  finishedComplete: NoMessageValues;
  finishedRejected: NoMessageValues;
  finishedWithdrawn: NoMessageValues;
  finishedOther: State;
  assignedJustNow: Assignee;
  assignedJustNowNoAssignee: NoMessageValues;
  assignedTakeOver: Assignee;
  viewOnlyAssigned: Assignee;
  viewOnlyClaim: NoMessageValues;
  viewOnly: NoMessageValues;
  takenOver: Name & Assignee;
  takenOverNoAssignee: Name;
  notAssignedYet: ProcessChanges;
  savingLatest: NoMessageValues;
  primaryHint: NoMessageValues;
  takeOver: NoMessageValues;
  noSectionsToReview: NoMessageValues;
  regroupConsequence: ProcessChanges & { readonly b: MessageTag };
  skipHeader: NoMessageValues;
  skipBody: NoMessageValues;
  processing: Name;
  baselineCopying: Name;
}>({
  reviewNotComplete: {
    id: 'editScreen.productionItem.reviewNotComplete',
    defaultMessage: 'Available once the review is complete',
    description:
      "Why Summary and Use As Evaluation Baseline are unavailable on a review queue item's edit page: they are about the reviewed document.",
  },
  claimToRegroup: {
    id: 'editScreen.productionItem.claimToRegroup',
    defaultMessage: 'Claim this item first to change its page grouping',
    description:
      "Why Edit Page Grouping is unavailable to an Annotator on an unassigned review queue item's edit page: only the item's holder may change its page grouping.",
  },
  holdingNotice: {
    id: 'editScreen.productionItem.holdingNotice',
    defaultMessage: 'This item is being prepared for review. It can be edited once it reaches its queue.',
    description: "Read-only notice at the top of a review queue item's edit page while the item is being prepared for review.",
  },
  finalizeFailedNotice: {
    id: 'editScreen.productionItem.finalizeFailedNotice',
    defaultMessage:
      "Applying this review's corrections failed, so it can no longer be edited here. An Admin can see it in Review Management.",
    description:
      "Read-only notice at the top of a review queue item's edit page when applying the review's corrections failed. Review Management is a page of the app.",
  },
  takeOverAction: {
    id: 'editScreen.productionItem.action.takeOver',
    defaultMessage: 'take over the item',
    description:
      'Infinitive verb phrase placed inside the shared error sentence ("Failed to {action}: {error}", or the permission sentence) when Take over failed on a review queue item\'s edit page.',
  },
  saveGroupingAction: {
    id: 'editScreen.productionItem.action.saveGrouping',
    defaultMessage: 'save the page grouping',
    description:
      'Infinitive verb phrase placed inside the shared error sentence ("Failed to {action}: {error}", or the permission sentence) when Edit Page Grouping could not save the page grouping of a review queue item\'s document.',
  },
  assignedElsewhere: {
    id: 'editScreen.productionItem.assignedElsewhere',
    defaultMessage: 'This item was just assigned to someone else',
    description:
      'Why the section editor of a review queue item\'s edit page could not save an Admin\'s edit, shown after "Failed to save:": another user was assigned the item first.',
  },
  latestEditsNotSaved: {
    id: 'editScreen.productionItem.latestEditsNotSaved',
    defaultMessage: 'Your latest edits could not be saved',
    description:
      "Why Process Changes failed on a review queue item's edit page, shown as the error inside the shared error sentence: the open section's latest edits could not be saved first.",
  },
  staleDraft: {
    id: 'editScreen.productionItem.staleDraft',
    defaultMessage: 'Someone saved a newer draft of this section. It has been reloaded; reapply your last change.',
    description:
      "Error on a review queue item's edit page when a save found a newer draft of the open section, which the page then reloaded.",
  },
  loading: {
    id: 'editScreen.productionItem.loading',
    defaultMessage: 'Loading the review item…',
    description: 'Shown beside a spinner while the edit page reads a review queue item.',
  },
  lockedHolding: {
    id: 'editScreen.productionItem.locked.holding',
    defaultMessage: 'This item is preparing',
    description:
      "Why Process Changes and Edit Page Grouping are unavailable on a review queue item's edit page: the item is being prepared for review.",
  },
  lockedFinalizing: {
    id: 'editScreen.productionItem.locked.finalizing',
    defaultMessage: 'This item is finalizing',
    description:
      "Why Process Changes and Edit Page Grouping are unavailable on a review queue item's edit page: the review was submitted and its corrections are being applied.",
  },
  lockedComplete: {
    id: 'editScreen.productionItem.locked.complete',
    defaultMessage: 'This item is complete',
    description:
      "Why Process Changes and Edit Page Grouping are unavailable on a review queue item's edit page: the item's review is complete.",
  },
  lockedRejected: {
    id: 'editScreen.productionItem.locked.rejected',
    defaultMessage: 'This item is rejected',
    description: "Why Process Changes and Edit Page Grouping are unavailable on a review queue item's edit page: the item was rejected.",
  },
  lockedFinalizeFailed: {
    id: 'editScreen.productionItem.locked.finalizeFailed',
    defaultMessage: 'This item is finalize failed',
    description:
      "Why Process Changes and Edit Page Grouping are unavailable on a review queue item's edit page: applying the review's corrections failed. The English uses the state's name, Finalize failed.",
  },
  lockedWithdrawn: {
    id: 'editScreen.productionItem.locked.withdrawn',
    defaultMessage: 'This item is withdrawn',
    description:
      "Why Process Changes and Edit Page Grouping are unavailable on a review queue item's edit page: the item was withdrawn from review.",
  },
  lockedOther: {
    id: 'editScreen.productionItem.locked.other',
    defaultMessage: 'This item is {state}',
    description:
      "Why Process Changes and Edit Page Grouping are unavailable on a review queue item's edit page, for a state the page does not know. {state} is that state as stored, in lower case.",
  },
  notAssignedToYou: {
    id: 'editScreen.productionItem.notAssignedToYou',
    defaultMessage: 'This item is not assigned to you',
    description:
      "Why Process Changes and Edit Page Grouping are unavailable on a review queue item's edit page: the item is not assigned to the user.",
  },
  finishedFinalizing: {
    id: 'editScreen.productionItem.finished.finalizing',
    defaultMessage: 'This review is finished: it was submitted and its corrections are being applied.',
    description: "Read-only notice at the top of a review queue item's edit page while the submitted review's corrections are applied.",
  },
  finishedComplete: {
    id: 'editScreen.productionItem.finished.complete',
    defaultMessage: 'This review is finished: the item is complete and can no longer be edited.',
    description: "Read-only notice at the top of a review queue item's edit page once the item's review is complete.",
  },
  finishedRejected: {
    id: 'editScreen.productionItem.finished.rejected',
    defaultMessage: 'This review is finished: the item is rejected and can no longer be edited.',
    description: "Read-only notice at the top of a review queue item's edit page once the item was rejected.",
  },
  finishedWithdrawn: {
    id: 'editScreen.productionItem.finished.withdrawn',
    defaultMessage: 'This review is finished: the item is withdrawn and can no longer be edited.',
    description: "Read-only notice at the top of a review queue item's edit page once the item was withdrawn from review.",
  },
  finishedOther: {
    id: 'editScreen.productionItem.finished.other',
    defaultMessage: 'This review is finished: the item is {state} and can no longer be edited.',
    description:
      "Read-only notice at the top of a review queue item's edit page, for a finished state the page does not know. {state} is that state as stored, in lower case.",
  },
  assignedJustNow: {
    id: 'editScreen.productionItem.assignedJustNow',
    defaultMessage: 'Assigned to {assignee} just now. Take over to edit it and process its changes.',
    description:
      "Read-only notice at the top of a review queue item's edit page for an Admin whose first edit found the item just assigned to another user. {assignee} is that user's e-mail address; Take over is the button beside the notice.",
  },
  assignedJustNowNoAssignee: {
    id: 'editScreen.productionItem.assignedJustNowNoAssignee',
    defaultMessage: 'Assigned to another reviewer just now. Take over to edit it and process its changes.',
    description:
      "Read-only notice at the top of a review queue item's edit page for an Admin whose first edit found the item just assigned to another user, when that user is not known. Take over is the button beside the notice.",
  },
  assignedTakeOver: {
    id: 'editScreen.productionItem.assignedTakeOver',
    defaultMessage: 'Assigned to {assignee}. Take over to edit it and process its changes.',
    description:
      "Read-only notice at the top of a review queue item's edit page for an Admin when the item is assigned to another user. {assignee} is that user's e-mail address; Take over is the button beside the notice.",
  },
  viewOnlyAssigned: {
    id: 'editScreen.productionItem.viewOnlyAssigned',
    defaultMessage: 'View only. This item is assigned to {assignee}.',
    description:
      "Read-only notice at the top of a review queue item's edit page when the item is assigned to another user. {assignee} is that user's e-mail address.",
  },
  viewOnlyClaim: {
    id: 'editScreen.productionItem.viewOnlyClaim',
    defaultMessage: 'View only. Claim this item to work on it; it is then assigned to you and shows in Home › Tasks.',
    description:
      "Read-only notice at the top of an unassigned review queue item's edit page, beside its Claim button. Home › Tasks is the page that lists the user's work, named by its breadcrumb trail.",
  },
  viewOnly: {
    id: 'editScreen.productionItem.viewOnly',
    defaultMessage: 'View only.',
    description: "Read-only notice at the top of a review queue item's edit page when the user may not edit the item.",
  },
  takenOver: {
    id: 'editScreen.productionItem.takenOver',
    defaultMessage: '{name} is assigned to you. {assignee} was told.',
    description:
      "Notice after Take over on a review queue item's edit page assigned the item to the Admin. {name} is the file name of the item's document; {assignee} is the former assignee's e-mail address.",
  },
  takenOverNoAssignee: {
    id: 'editScreen.productionItem.takenOverNoAssignee',
    defaultMessage: '{name} is assigned to you. The former assignee was told.',
    description:
      "Notice after Take over on a review queue item's edit page assigned the item to the Admin, when the former assignee is not known. {name} is the file name of the item's document.",
  },
  notAssignedYet: {
    id: 'editScreen.productionItem.notAssignedYet',
    defaultMessage: 'Not assigned yet. Editing it, or choosing {processChanges}, assigns it to you.',
    description:
      "Notice at the top of an unassigned review queue item's edit page for an Admin. {processChanges} is the name of the page's action, Process Changes.",
  },
  savingLatest: {
    id: 'editScreen.productionItem.savingLatest',
    defaultMessage: 'Saving your latest edits…',
    description:
      "Why an action of a review queue item's edit page, such as Save, Skip or Process Changes, is unavailable while the latest edits are being saved.",
  },
  primaryHint: {
    id: 'editScreen.productionItem.primaryHint',
    defaultMessage:
      'Apply your corrections and finish the review: evaluation and summarization re-run; OCR, classification and extraction are not repeated.',
    description: "Hint of Process Changes in the action bar of a review queue item's edit page: what it does.",
  },
  takeOver: {
    id: 'editScreen.productionItem.takeOver',
    defaultMessage: 'Take over',
    description:
      "Button beside the read-only notice of a review queue item's edit page, for an Admin: it assigns the item, which another user holds, to the Admin.",
  },
  noSectionsToReview: {
    id: 'editScreen.productionItem.noSectionsToReview',
    defaultMessage: 'This item has no sections to review.',
    description: "Shown in the section editor's place on a review queue item's edit page when the item has no sections under review.",
  },
  regroupConsequence: {
    id: 'editScreen.productionItem.regroupConsequence',
    defaultMessage:
      "Moving pages rewrites this document's <b>section grouping</b>. The extracted values and your draft edits are <b>kept</b> with their sections, and the document finishes with this grouping when you choose <b>{processChanges}</b>. A section you remove takes its draft edits with it.",
    description:
      "What saving a new page grouping does, in Edit Page Grouping opened from a review queue item's edit page. <b>…</b> marks bold text; {processChanges} is the edit page's action, Process Changes.",
  },
  skipHeader: {
    id: 'editScreen.productionItem.skipHeader',
    defaultMessage: 'Skip this item',
    description: "Title of the dialog that Skip opens on a review queue item's edit page, which asks why the item is skipped.",
  },
  skipBody: {
    id: 'editScreen.productionItem.skipBody',
    defaultMessage: 'It goes back to its queue, unassigned and not finished. Your saved edits are kept for whoever picks it up next.',
    description: "Text of the dialog that Skip opens on a review queue item's edit page: what skipping the item does.",
  },
  processing: {
    id: 'editScreen.productionItem.processing',
    defaultMessage: 'Processing the changes to {name}: your corrections are being applied.',
    description:
      "Notice shown as a review queue item's edit page closes after Process Changes finished the review. {name} is the file name of the item's document.",
  },
  baselineCopying: {
    id: 'editScreen.productionItem.baselineCopying',
    defaultMessage: 'Copying {name} to the evaluation baseline.',
    description:
      "Notice after Use As Evaluation Baseline on a review queue item's edit page started copying the document to the evaluation baseline. {name} is the file name of the item's document.",
  },
});

/** Its English: the item's test compares with it. */
export const CLAIM_TO_REGROUP_REASON = String(messages.claimToRegroup.defaultMessage);

/** Renders a `<b>...</b>` element value. */
const bold = (chunks: React.ReactNode[]): React.JSX.Element => <b>{chunks}</b>;

/** Why an item that is neither open nor claimed can't be worked: one whole sentence per state. */
const lockedReasonOf = (state: string, intl: IntlShape): string => {
  switch (state) {
    case QueueItemState.HOLDING:
      return intl.formatMessage(messages.lockedHolding);
    case QueueItemState.FINALIZING:
      return intl.formatMessage(messages.lockedFinalizing);
    case QueueItemState.COMPLETE:
      return intl.formatMessage(messages.lockedComplete);
    case QueueItemState.REJECTED:
      return intl.formatMessage(messages.lockedRejected);
    case QueueItemState.FINALIZE_FAILED:
      return intl.formatMessage(messages.lockedFinalizeFailed);
    case QueueItemState.WITHDRAWN:
      return intl.formatMessage(messages.lockedWithdrawn);
    default:
      return intl.formatMessage(messages.lockedOther, { state: state.toLowerCase() });
  }
};

/** The read-only notice of an item that is neither open nor claimed, each saying what happens next. */
const finishedNoticeOf = (state: string, intl: IntlShape): string => {
  switch (state) {
    case QueueItemState.HOLDING:
      return intl.formatMessage(messages.holdingNotice);
    case QueueItemState.FINALIZE_FAILED:
      return intl.formatMessage(messages.finalizeFailedNotice);
    case QueueItemState.FINALIZING:
      return intl.formatMessage(messages.finishedFinalizing);
    case QueueItemState.COMPLETE:
      return intl.formatMessage(messages.finishedComplete);
    case QueueItemState.REJECTED:
      return intl.formatMessage(messages.finishedRejected);
    case QueueItemState.WITHDRAWN:
      return intl.formatMessage(messages.finishedWithdrawn);
    default:
      return intl.formatMessage(messages.finishedOther, { state: state.toLowerCase() });
  }
};

/** An Annotator was assigned the item between the Admin opening it and editing it. */
class AssignedElsewhere extends EditPageError {
  constructor() {
    super({ descriptor: messages.assignedElsewhere });
  }
}

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
  const { detail, loadError, reload, notice, clearNotice } = adapter;
  const { user, setErrorMessage, setSuccessMessage } = useAppContext();
  const { isAdmin, isQueueWorker, canWrite } = useUserRole();
  const reviewWork = useOptionalReviewWork();
  const leavePath = useLeavePath();
  const [searchParams] = useSearchParams();
  const location = useLocation();
  const focusField = searchParams.get('field');
  const initialSectionId = searchParams.get('section');
  const [takingOver, setTakingOver] = useState(false);
  /** Who was assigned the item just as this Admin tried to edit or finish it (`email` null: not known). */
  const [assignedJustNow, setAssignedJustNow] = useState<{ email: string | null } | null>(null);
  // The latest item read, and the one claim in flight: concurrent saves share it.
  const itemRef = useRef<ReviewItem | null>(null);
  const assigningRef = useRef<Promise<boolean> | null>(null);

  useEffect(() => {
    itemRef.current = detail?.item ?? null;
  }, [detail]);

  // A save's notice (a newer draft was found and reloaded), worded here with `intl` and then
  // cleared, so the saving callback never words it.
  useEffect(() => {
    if (!notice) return;
    setErrorMessage(intl.formatMessage(messages.staleDraft));
    clearNotice();
  }, [notice, clearNotice, intl, setErrorMessage]);

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
          setAssignedJustNow({ email: fresh?.item.ownerEmail || null });
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
      if (!(await ensureAssigned())) throw new AssignedElsewhere();
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
    // The edit page's own states for an item it can't open, under this item's trail.
    const { state } = notAssigned ? notAMemberModel(intl) : itemFailedModel(loadError, () => void reload(), intl);
    return { navigation, state };
  }
  if (!detail) return { navigation, state: { kind: 'loading', text: intl.formatMessage(messages.loading) } };

  const { item } = detail;
  const name = documentName(item.objectKey);
  const stateView = reviewItemStateView(item.state, intl);
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

  const savingLatest = intl.formatMessage(messages.savingLatest);
  const processChanges = intl.formatMessage(sharedLabelMessages.processChanges);

  let lockedReason: string | undefined;
  if (terminal) lockedReason = lockedReasonOf(item.state, intl);
  else if (!canEdit)
    lockedReason = assignee
      ? intl.formatMessage(editScreenSharedMessages.assignedTo, { assignee })
      : intl.formatMessage(messages.notAssignedToYou);

  // Why the fields can't be edited, shown first: with Take over for an Admin on someone
  // else's item, and Claim (from the bar's claim) for an Annotator on an unassigned one.
  let readOnlyReason: string | undefined;
  if (terminal) readOnlyReason = finishedNoticeOf(item.state, intl);
  else if (canTakeOver) {
    const justNowBy = assignee ?? assignedJustNow?.email;
    if (!assignedJustNow) readOnlyReason = intl.formatMessage(messages.assignedTakeOver, { assignee: String(assignee) });
    else if (justNowBy != null) readOnlyReason = intl.formatMessage(messages.assignedJustNow, { assignee: justNowBy });
    else readOnlyReason = intl.formatMessage(messages.assignedJustNowNoAssignee);
  } else if (!canEdit)
    readOnlyReason = assignee
      ? intl.formatMessage(messages.viewOnlyAssigned, { assignee })
      : intl.formatMessage(canClaim ? messages.viewOnlyClaim : messages.viewOnly);

  const queuePath = productionQueuePath(item.queue);
  const workItemId = productionWorkItemId(objectKey);

  const handleTakeOver = async () => {
    setTakingOver(true);
    try {
      await takeOverItem(item);
      setAssignedJustNow(null);
      setSuccessMessage(
        assignee != null
          ? intl.formatMessage(messages.takenOver, { name, assignee })
          : intl.formatMessage(messages.takenOverNoAssignee, { name }),
      );
      void reviewWork?.refresh();
    } catch (err) {
      setErrorMessage(describeApiError(err, messages.takeOverAction, intl));
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
          unavailableReason: intl.formatMessage(finished ? documentEditStatusMessages.noSummary : messages.reviewNotComplete),
        },
        offerReport: true,
      },
      // One alert for the screen's notices, so the space stays given over to the review.
      notices: [
        adminOnOpen && {
          id: 'unassigned',
          type: 'info',
          content: intl.formatMessage(messages.notAssignedYet, { processChanges }),
        },
      ],
      // A reason's chip, or Next alert, moves to its field.
      context: <ReviewTaskContext detail={detail} onReasonClick={(r) => session.focusField(r.sectionId, r.field ?? '')} />,
      bar: {
        primaryDisabledReason: lockedReason ?? (saving ? savingLatest : undefined),
        primaryHint: intl.formatMessage(messages.primaryHint),
        // Skip gives the item back to its queue: only its holder can.
        showSkip: assignedToMe,
        skipDisabledReason: dirty ? savingLatest : undefined,
        showCancel: canEdit,
        cancelDisabledReason: saving ? savingLatest : undefined,
        showSave: canEdit,
        saveDisabledReason: !dirty ? intl.formatMessage(editScreenSharedMessages.noChangesToSave) : saving ? savingLatest : undefined,
        showRefresh: true,
        refreshDisabledReason: saving ? savingLatest : undefined,
        showRegroup: true,
        regroupDisabledReason:
          // The item's holder regroups, Annotator or Admin, as the server allows.
          (!terminal && canClaim ? intl.formatMessage(messages.claimToRegroup) : lockedReason) ?? (dirty ? savingLatest : undefined),
        // copyToBaseline is Admin and Author (canWrite), as on the document page.
        showBaseline: canWrite,
        baselineDisabledReason: finished
          ? baselineCopyBlockedReason(adapter.evaluationStatus, intl)
          : intl.formatMessage(messages.reviewNotComplete),
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
            <FormattedMessage {...messages.takeOver} />
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
        emptyText: intl.formatMessage(messages.noSectionsToReview),
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
        consequence: <FormattedMessage {...messages.regroupConsequence} values={{ b: bold, processChanges }} />,
        // Regrouping is part of review: like a first save, it assigns an unassigned item to
        // the Admin opening it, since only the item's holder may regroup.
        beforeOpen: async () => {
          if (!(await ensureAssigned())) throw new AssignedElsewhere();
          return true;
        },
        save: async (next) => {
          const message = await saveDocumentGroupingOrThrow(objectKey, next, (err) =>
            describeApiError(err, messages.saveGroupingAction, intl),
          );
          await Promise.all([reload(), adapter.refreshDocument()]);
          return message;
        },
      },
      history: { reviewActivity: detail.history },
      skip: {
        header: intl.formatMessage(messages.skipHeader),
        reasons: detail.skipReasons,
        body: (
          <Box>
            <FormattedMessage {...messages.skipBody} />
          </Box>
        ),
        run: async (reason, note) => {
          await releaseItem(item, reason, note);
          return { to: leavePath(queuePath), message: intl.formatMessage(editScreenSharedMessages.returnedToQueue, { name }), workItemId };
        },
      },
      process: {
        run: async () => {
          if (!(await ensureAssigned())) throw new AssignedElsewhere();
          // The latest edits are saved as a draft first, so the finalize run applies them.
          const editor = session.controllerRef.current;
          if (editor?.isDirty() && !(await editor.save())) throw new EditPageError({ descriptor: messages.latestEditsNotSaved });
          await adapter.complete();
          return {
            to: leavePath(queuePath),
            message: intl.formatMessage(messages.processing, { name }),
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
        return intl.formatMessage(editScreenSharedMessages.claimed, { name });
      },
      baseline: async () => {
        await copyDocumentToBaseline(objectKey);
        void adapter.refreshDocument();
        return intl.formatMessage(messages.baselineCopying, { name });
      },
      refresh: adapter.refresh,
      cancelTo: leavePath(queuePath),
      failures: {
        surface: 'flash',
        // Losing the item to an Annotator is shown in the notices, not as an error. The
        // page's own error is worded here; any other is passed on as describeApiError reads it.
        describe: (action, err) =>
          err instanceof AssignedElsewhere
            ? null
            : describeApiError(err instanceof EditPageError ? editPageErrorText(err, intl) : err, failedActionMessages[action], intl),
        after: () => void reload(),
      },
    },
  };
};
