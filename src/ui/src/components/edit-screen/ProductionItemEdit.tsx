// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit screen for a production review item: the shared SectionEditor over the
// item's sections, edits autosaved as drafts (and Save), then Process Changes (finish
// the review; the finalize run applies the corrections and re-runs evaluation and
// summarization, reusing the document's OCR, classification and extraction) or Skip (give the item back to its
// queue; the draft is kept). Cancel leaves without finishing. Only the assignee edits.
// Any Annotator listed on the item's queue can open it read-only, and Claim an
// unassigned item to work it. An Admin edits an unassigned item directly: the first
// draft save, or Process Changes, assigns it to them. An item assigned to someone else is view-only for an Admin
// until they Take over.
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Alert, Box, Button, Link, Spinner } from '@cloudscape-design/components';

import useAppContext from '../../contexts/app';
import usePageThumbnails from '../../hooks/use-page-thumbnails';
import { useOptionalReviewWork } from '../../hooks/use-review-work';
import useUserRole from '../../hooks/use-user-role';
import { describeApiError } from '../../hooks/utils/graphql-error';
import { baselineCopyBlockedReason, copyDocumentToBaseline } from '../common/copy-to-baseline';
import { MY_QUEUES_PATH, productionQueuePath, reviewItemHref } from '../../routes/constants';
import { QueueItemState, reviewItemStateView } from '../common/review-status';
import ReviewTaskContext from '../review-queues/ReviewTaskContext';
import ReasonModal from '../common/ReasonModal';
import { saveDocumentGrouping } from '../common/document-grouping';
import PageGroupingEditor from '../common/PageGroupingEditor';
import type { GroupedSection } from '../common/section-grouping';
import { claimItem, documentName, releaseItem, reviewErrorCode, takeOverItem } from '../review-queues/review-api';
import type { ReviewItem } from '../../graphql/generated/operation-types';
import EditScreen, { EditDocumentEmpty } from './EditScreen';
import EditorNotices from './EditorNotices';
import type { EditAction } from './EditScreen';
import EditorHistoryModal from './EditorHistoryModal';
import FileViewer from '../document-viewer/FileViewer';
import SectionEditor from './SectionEditor';
import type { SectionEditorController } from './SectionEditor';
import { AUTOSAVE_MS, useProductionItemEdit } from './production-item-edit-adapter';
import useEditCancel from './use-edit-cancel';
import useEditView from './use-edit-view';
import useEditExit, { useLeavePath } from './use-edit-exit';

/** The item's queue, for the breadcrumbs: its id (for the link) and the name people see. */
export interface ItemQueue {
  id: string;
  name: string;
}

/** Why Summary and Use As Evaluation Baseline wait: they are about the reviewed document. */
const REVIEW_NOT_COMPLETE = 'Available once the review is complete';

/** An Annotator was assigned the item between the Admin opening it and editing it. */
class AssignedElsewhere extends Error {}

interface ProductionItemEditProps {
  objectKey: string;
  /** Told the item's queue once it has loaded. */
  onItemQueue?: (queue: ItemQueue) => void;
}

const ProductionItemEdit = ({ objectKey, onItemQueue }: ProductionItemEditProps): React.JSX.Element => {
  const adapter = useProductionItemEdit(objectKey);
  const { detail, loadError, reload } = adapter;
  const { user, setErrorMessage, setSuccessMessage } = useAppContext();
  const { isAdmin, isAnnotator, canWrite } = useUserRole();
  const reviewWork = useOptionalReviewWork();
  const exit = useEditExit();
  const [searchParams] = useSearchParams();
  const focusField = searchParams.get('field');
  const [showHistory, setShowHistory] = useState(false);
  const [saving, setSaving] = useState(false);
  const [sectionData, setSectionData] = useState<Record<string, unknown> | null>(null);
  const editor = useRef<SectionEditorController | null>(null);
  const [view, setView] = useEditView();
  const [regroupOpen, setRegroupOpen] = useState(false);
  const [isSavingGrouping, setIsSavingGrouping] = useState(false);
  const [groupingError, setGroupingError] = useState<string | null>(null);
  const { urls: thumbnailUrls } = usePageThumbnails(adapter.pages, { enabled: regroupOpen });
  const boardPages = useMemo(
    () => adapter.pages.map((page) => ({ id: Number(page.Id), imageUri: thumbnailUrls[String(page.Id)] ?? null })),
    [adapter.pages, thumbnailUrls],
  );
  // Every section of the document, not only those under review: a grouping covers every page.
  const boardSections: GroupedSection[] = useMemo(
    () =>
      adapter.documentSections.map((s) => ({
        sectionId: s.sectionId,
        documentClass: s.documentClass ?? null,
        pageIds: s.pageIds.map((id) => Number(id)),
      })),
    [adapter.documentSections],
  );
  const [busy, setBusy] = useState<EditAction | null>(null);
  const [askSkipReason, setAskSkipReason] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [takingOver, setTakingOver] = useState(false);
  /** Who was assigned the item just as this Admin tried to edit or finish it. */
  const [assignedJustNow, setAssignedJustNow] = useState<string | null>(null);
  // The latest item read, and the one claim in flight: concurrent saves share it.
  const itemRef = useRef<ReviewItem | null>(null);
  const assigningRef = useRef<Promise<boolean> | null>(null);

  useEffect(() => {
    itemRef.current = detail?.item ?? null;
  }, [detail]);

  const queueId = detail?.item.queue;
  const queueName = detail?.queueName;
  const leavePath = useLeavePath();
  const { cancel, dialog: cancelDialog } = useEditCancel(editor, leavePath(productionQueuePath(queueId ?? '')));
  useEffect(() => {
    if (queueId) onItemQueue?.({ id: queueId, name: queueName || queueId });
  }, [queueId, queueName, onItemQueue]);

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
    (fieldPath: string) => `${window.location.origin}/${reviewItemHref(objectKey)}?field=${encodeURIComponent(fieldPath)}`,
    [objectKey],
  );

  if (loadError && !detail) {
    const code = reviewErrorCode(loadError);
    const notAssigned = code === 'NOT_ASSIGNED' || code === 'NOT_A_MEMBER';
    return (
      <Alert
        type={notAssigned ? 'warning' : 'error'}
        header={notAssigned ? "You can't open this review item" : "Couldn't open this review item"}
        action={!notAssigned && <Button onClick={() => void reload()}>Retry</Button>}
      >
        {notAssigned ? (
          <>
            Only the Annotators listed on this item&apos;s queue can open it. The queues that list you are in{' '}
            <Link href={`#${MY_QUEUES_PATH}`}>Home › Queues</Link>.
          </>
        ) : (
          describeApiError(loadError, 'load the review item')
        )}
      </Alert>
    );
  }
  if (!detail) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" /> Loading the review item…
      </Box>
    );
  }

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

  let lockedReason: string | undefined;
  if (terminal) lockedReason = `This item is ${stateView.label.toLowerCase()}`;
  else if (!canEdit) lockedReason = assignee ? `Assigned to ${assignee}` : 'This item is not assigned to you';

  const run = async (action: EditAction, fn: () => Promise<void>, failure: string): Promise<boolean> => {
    setBusy(action);
    try {
      await fn();
      return true;
    } catch (err) {
      // Losing the item to an Annotator is shown in the notices, not as an error.
      if (!(err instanceof AssignedElsewhere)) setErrorMessage(describeApiError(err, failure));
      void reload();
      return false;
    } finally {
      setBusy(null);
    }
  };

  const queuePath = productionQueuePath(item.queue);

  const handleSaveGrouping = async (next: GroupedSection[]) => {
    setIsSavingGrouping(true);
    setGroupingError(null);
    try {
      const result = await saveDocumentGrouping(objectKey, next);
      if (!result.success) {
        setGroupingError(result.message ?? 'The page grouping could not be saved.');
        return;
      }
      setRegroupOpen(false);
      if (result.message) setSuccessMessage(result.message);
      await Promise.all([reload(), adapter.refreshDocument()]);
    } catch (err) {
      setGroupingError(describeApiError(err, 'save the page grouping'));
    } finally {
      setIsSavingGrouping(false);
    }
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

  const confirmSkip = async (reason: string, note: string | undefined) => {
    const ok = await run(
      'skip',
      async () => {
        await releaseItem(item, reason, note);
      },
      'skip the item',
    );
    setAskSkipReason(false);
    if (ok) exit(queuePath, `${name} returned to the queue.`, `p:${objectKey}`);
  };

  const handleAction = async (action: EditAction) => {
    if (action === 'claim') {
      reviewWork?.beginMutation();
      const ok = await run(
        'claim',
        async () => {
          await claimItem(item);
        },
        'claim the item',
      );
      if (ok) {
        setSuccessMessage(`Claimed ${name}. It is in Home › Tasks.`);
        await reload();
      }
      void reviewWork?.refresh();
      return;
    }
    if (action === 'skip') {
      // The reason is asked for first; confirmSkip does the release.
      setAskSkipReason(true);
      return;
    }
    if (action === 'cancel') {
      cancel();
      return;
    }
    if (action === 'save') {
      setBusy('save');
      try {
        await editor.current?.save();
      } finally {
        setBusy(null);
      }
      return;
    }
    if (action === 'baseline') {
      const ok = await run(
        'baseline',
        async () => {
          await copyDocumentToBaseline(objectKey);
        },
        'use this document as the evaluation baseline',
      );
      if (ok) {
        setSuccessMessage(`Copying ${name} to the evaluation baseline.`);
        void adapter.refreshDocument();
      }
      return;
    }
    if (action === 'regroup') {
      // Regrouping is part of review: like a first save, it assigns an unassigned
      // item to the Admin opening it, since only the item's holder may regroup.
      const ok = await run(
        'regroup',
        async () => {
          if (!(await ensureAssigned())) throw new AssignedElsewhere();
        },
        'start editing the page grouping',
      );
      if (ok) {
        setGroupingError(null);
        setRegroupOpen(true);
      }
      return;
    }
    const ok = await run(
      'process',
      async () => {
        if (!(await ensureAssigned())) throw new AssignedElsewhere();
        // The latest edits are saved as a draft first, so the finalize run applies them.
        if (editor.current?.isDirty() && !(await editor.current.save())) throw new Error('Your latest edits could not be saved');
        await adapter.complete();
      },
      'process the changes',
    );
    if (ok) exit(queuePath, `Processing the changes to ${name}: your corrections are being applied.`, `p:${objectKey}`);
  };

  // One alert for the screen's notices, so the space stays given over to the review.
  const notices = (
    <EditorNotices
      notices={[
        terminal && {
          id: 'finished',
          type: 'info',
          content:
            item.state === 'FINALIZING'
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
            <Button onClick={() => void handleTakeOver()} loading={takingOver} disabled={busy !== null}>
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
      ]}
    />
  );

  return (
    <EditScreen
      title={name}
      status={stateView.label}
      objectKey={item.objectKey}
      reportSectionData={sectionData}
      summaryReportUri={finished ? adapter.summaryReportUri || undefined : undefined}
      summaryUnavailableReason={finished ? 'This document has no summary' : REVIEW_NOT_COMPLETE}
      actions={{
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
        showRegroup: true,
        // copyToBaseline is Admin and Author (canWrite), as on the document page.
        showBaseline: canWrite,
        baselineDisabledReason: finished ? baselineCopyBlockedReason(adapter.evaluationStatus) : REVIEW_NOT_COMPLETE,
        regroupDisabledReason: lockedReason ?? (dirty ? 'Saving your latest edits…' : undefined),
        showClaim: canClaim,
        onShowHistory: () => setShowHistory(true),
        view,
        onViewChange: setView,
        busy,
        onAction: (action) => void handleAction(action),
      }}
      notices={notices}
      context={<ReviewTaskContext detail={detail} />}
      dialogs={
        <>
          {/* Mounted only while open, so their buttons are not on the screen otherwise. */}
          {showHistory && (
            <EditorHistoryModal visible onDismiss={() => setShowHistory(false)} sectionData={sectionData} reviewHistory={detail.history} />
          )}
          {cancelDialog}
          {regroupOpen && (
            <PageGroupingEditor
              presentation="modal"
              title="Edit Page Class"
              pages={boardPages}
              sections={boardSections}
              canChangeClass={false}
              consequence={
                <>
                  {groupingError && <Alert type="error">{groupingError}</Alert>}
                  Moving pages rewrites this document&apos;s <b>section grouping</b>. The extracted values and your draft edits are{' '}
                  <b>kept</b> with their sections, and the document finishes with this grouping when you choose <b>Process Changes</b>. A
                  section you remove takes its draft edits with it.
                </>
              }
              saveLabel="Save page grouping"
              isSaving={isSavingGrouping}
              onSave={handleSaveGrouping}
              onCancel={() => setRegroupOpen(false)}
            />
          )}
          {askSkipReason && (
            <ReasonModal
              visible
              header="Skip this item"
              reasons={detail.skipReasons}
              confirmLabel="Skip"
              loading={busy === 'skip'}
              onDismiss={() => setAskSkipReason(false)}
              onConfirm={(reason, note) => void confirmSkip(reason, note)}
            >
              <Box>It goes back to its queue, unassigned and not finished. Your saved edits are kept for whoever picks it up next.</Box>
            </ReasonModal>
          )}
        </>
      }
    >
      {adapter.sections.length === 0 ? (
        <EditDocumentEmpty body={view === 'source' ? <FileViewer objectKey={objectKey} /> : undefined}>
          This item has no sections to review.
        </EditDocumentEmpty>
      ) : (
        <SectionEditor
          key={objectKey}
          objectKey={objectKey}
          sections={adapter.sections}
          pages={adapter.pages}
          pagesLoading={adapter.pagesLoading}
          isReadOnly={!canEdit}
          loadSection={adapter.loadSection}
          onSave={saveSection}
          onDirtyChange={setDirty}
          onSavingChange={setSaving}
          onDataChange={setSectionData}
          controllerRef={editor}
          actionsPlacement="external"
          showHistoryTab={false}
          recordEditHistory={{ editedBy: user?.username || 'unknown', source: 'review-queue' }}
          reloadToken={adapter.reloadToken}
          autosaveMs={canEdit ? AUTOSAVE_MS : undefined}
          confirmSectionSwitch={false}
          focusFieldPath={focusField}
          buildFieldLink={buildFieldLink}
          variant="widget"
          /* The editor stays mounted behind the source view, so unsaved edits survive the switch. */
          body={view === 'source' ? <FileViewer objectKey={objectKey} /> : undefined}
          defaultFilter={item.reasonCount > 0 ? 'confidence-alerts' : 'none'}
        />
      )}
    </EditScreen>
  );
};

export default ProductionItemEdit;
