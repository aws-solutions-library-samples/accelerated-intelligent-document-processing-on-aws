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
import { useSearchParams } from 'react-router-dom';
import { Box, Button, Link } from '@cloudscape-design/components';

import useAppContext from '../../contexts/app';
import { useOptionalReviewWork } from '../../hooks/use-review-work';
import useUserRole from '../../hooks/use-user-role';
import { describeApiError } from '../../hooks/utils/graphql-error';
import { MY_QUEUES_PATH, MY_TASKS_PATH, REVIEW_QUEUES_PATH, documentEditHref, productionQueuePath } from '../../routes/constants';
import type { ReviewItem, ReviewItemDetail } from '../../graphql/generated/operation-types';
import { baselineCopyBlockedReason, copyDocumentToBaseline } from '../common/copy-to-baseline';
import { saveDocumentGroupingOrThrow } from '../common/document-grouping';
import { QueueItemState, reviewItemStateView } from '../common/review-status';
import { claimItem, documentName, releaseItem, reviewErrorCode, takeOverItem } from '../review-queues/review-api';
import ReviewTaskContext from '../review-queues/ReviewTaskContext';
import { productionWorkItemId } from '../review-queues/work-item-ids';
import type { EditAction } from './EditScreen';
import type { EditModel, EditNavigation } from './edit-model';
import { AUTOSAVE_MS, useProductionItemEdit } from './production-item-edit-adapter';
import { useLeavePath } from './use-edit-exit';
import type { RawDocument } from './use-edit-mode';
import type { EditorSession } from './use-editor-session';

/** Why Summary and Use As Evaluation Baseline wait: they are about the reviewed document. */
const REVIEW_NOT_COMPLETE = 'Available once the review is complete';

/** On a Production item only an Admin holding it may regroup: an Annotator regroups only test-set documents. */
export const QUEUE_REGROUP_ROLE_REASON = 'Edit Page Class needs the Admin role on a review queue item.';

/** Notices for the in-review states that are not open for editing, each saying what happens next. */
export const HOLDING_NOTICE = 'This item is being prepared for review. It can be edited once it reaches its queue.';
export const FINALIZE_FAILED_NOTICE =
  "Applying this review's corrections failed, so it can no longer be edited here. An Admin can see it in Queue Management.";

/** What a failed action was trying to do, in the error it reports. */
const FAILED_ACTION: Record<EditAction, string> = {
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
  const adapter = useProductionItemEdit(objectKey, seed);
  const { detail, loadError, reload } = adapter;
  const { user, setErrorMessage, setSuccessMessage } = useAppContext();
  const { isAdmin, isAnnotator, canWrite } = useUserRole();
  const reviewWork = useOptionalReviewWork();
  const leavePath = useLeavePath();
  const [searchParams] = useSearchParams();
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
  const navigation: EditNavigation = {
    // Home only until the item, and so its queue, is known.
    root: queueCrumb,
    crumbs: queueCrumb ? [{ text: documentName(objectKey), href: '#' }] : [],
    // An Admin works items from Queue Management; an Annotator from Tasks.
    activeHref: isAdmin ? `#${REVIEW_QUEUES_PATH}` : `#${MY_TASKS_PATH}`,
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
  const canClaim = isAnnotator && !isAdmin && item.state === QueueItemState.OPEN;
  // Summary and Use as Evaluation Baseline are about the finished document: only once its
  // review is complete and the corrections have been applied.
  const finished = item.state === QueueItemState.COMPLETE;
  const { dirty, saving } = session;

  let lockedReason: string | undefined;
  if (terminal) lockedReason = `This item is ${stateView.label.toLowerCase()}`;
  else if (!canEdit) lockedReason = assignee ? `Assigned to ${assignee}` : 'This item is not assigned to you';

  const queuePath = productionQueuePath(item.queue);
  const workItemId = productionWorkItemId(objectKey);

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
        offerReport: true,
      },
      // One alert for the screen's notices, so the space stays given over to the review.
      notices: [
        terminal && {
          id: 'finished',
          type: 'info',
          content:
            item.state === QueueItemState.HOLDING
              ? HOLDING_NOTICE
              : item.state === QueueItemState.FINALIZE_FAILED
                ? FINALIZE_FAILED_NOTICE
                : item.state === 'FINALIZING'
                  ? 'This review is finished: it was submitted and its corrections are being applied.'
                  : `This review is finished: the item is ${stateView.label.toLowerCase()} and can no longer be edited.`,
        },
        adminOnOpen && {
          id: 'unassigned',
          type: 'info',
          content: 'Not assigned yet. Editing it, or choosing Process Changes, assigns it to you.',
        },
        canTakeOver && {
          id: 'take-over',
          type: assignedJustNow ? 'warning' : 'info',
          content: assignedJustNow
            ? `Assigned to ${assignee ?? assignedJustNow} just now. Take over to edit it and process its changes.`
            : `Assigned to ${assignee}. Take over to edit it and process its changes.`,
          action: (
            <Button onClick={() => void handleTakeOver()} loading={takingOver} disabled={session.busy !== null}>
              Take over
            </Button>
          ),
        },
        !terminal &&
          !canEdit &&
          !isAdmin && {
            id: 'view-only',
            type: 'info',
            content: assignee
              ? `View only. This item is assigned to ${assignee}.`
              : canClaim
                ? 'View only. Claim this item to work on it; it is then assigned to you and shows in Home › Tasks.'
                : 'View only.',
          },
      ],
      context: <ReviewTaskContext detail={detail} />,
      bar: {
        primaryDisabledReason: lockedReason ?? (saving ? 'Saving your latest edits…' : undefined),
        primaryHint:
          'Apply your corrections and finish the review: evaluation and summarization re-run; OCR, classification and extraction are not repeated.',
        // Skip gives the item back to its queue: only its holder can.
        showSkip: assignedToMe,
        skipDisabledReason: dirty ? 'Saving your latest edits…' : undefined,
        showCancel: canEdit,
        cancelDisabledReason: saving ? 'Saving your latest edits…' : undefined,
        showSave: canEdit,
        saveDisabledReason: !dirty ? 'No changes to save' : saving ? 'Saving your latest edits…' : undefined,
        showRefresh: true,
        refreshDisabledReason: saving ? 'Saving your latest edits…' : undefined,
        showRegroup: true,
        regroupDisabledReason:
          lockedReason ??
          (canEdit && !isAdmin ? QUEUE_REGROUP_ROLE_REASON : undefined) ??
          (dirty ? 'Saving your latest edits…' : undefined),
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
        consequence: (
          <>
            Moving pages rewrites this document&apos;s <b>section grouping</b>. The extracted values and your draft edits are <b>kept</b>{' '}
            with their sections, and the document finishes with this grouping when you choose <b>Process Changes</b>. A section you remove
            takes its draft edits with it.
          </>
        ),
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
      process: {
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
        describe: (action, err) => (err instanceof AssignedElsewhere ? null : describeApiError(err, FAILED_ACTION[action])),
        after: () => void reload(),
      },
    },
  };
};
