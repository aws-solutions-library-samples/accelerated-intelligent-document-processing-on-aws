// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit screen's direct edit of any processed document, in review or not: the
// shared SectionEditor over its sections, with the evaluation baseline where the
// document has one. Save writes the edits without reprocessing; Reprocess saves them,
// confirms, then reprocesses the document (evaluation and summarization); Cancel
// leaves for the document's page. Edit Page Grouping saves a new grouping.
//
// A document in a review queue opens read-only: it is edited from its queue item,
// and finalizing that item would overwrite edits made here. So does a document still
// processing. Save needs the Admin or Author role, or a Reviewer holding the
// document's legacy review; Reprocess needs the Admin or Reviewer role. The
// server enforces both.
import React, { useCallback, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Alert, Box, Button, Link, Spinner } from '@cloudscape-design/components';

import useAppContext from '../../contexts/app';
import useSettingsContext from '../../contexts/settings';
import useConfiguration from '../../hooks/use-configuration';
import usePageThumbnails from '../../hooks/use-page-thumbnails';
import useUserRole from '../../hooks/use-user-role';
import { DOCUMENTS_PATH, reviewItemHref } from '../../routes/constants';
import { getErrorMessage } from '../../utils/errorUtils';
import { getConfigClassOptions } from '../common/config-class-options';
import { saveDocumentGrouping } from '../common/document-grouping';
import PageGroupingEditor from '../common/PageGroupingEditor';
import ProcessChangesConfirmModal from '../common/ProcessChangesConfirmModal';
import { inReviewQueueEditReason } from '../common/review-status';
import type { GroupedSection } from '../common/section-grouping';
import { documentName } from '../review-queues/review-api';
import EditScreen, { EditDocumentEmpty } from './EditScreen';
import EditorNotices from './EditorNotices';
import type { EditAction } from './EditScreen';
import { useDocumentEdit } from './document-edit-adapter';
import type { DirectEditDocument } from './document-edit-adapter';
import { PROCESSING_REASON, isEditableStatus } from './document-edit-status';
import EditorHistoryModal from './EditorHistoryModal';
import FileViewer from '../document-viewer/FileViewer';
import SectionEditor from './SectionEditor';
import type { SectionEditorController } from './SectionEditor';
import useEditCancel from './use-edit-cancel';
import useEditView from './use-edit-view';
import useSectionEvaluation from './use-section-evaluation';

export const PROCESS_ROLE_REASON = 'Reprocess needs the Admin or Reviewer role.';
export const EDIT_ROLE_REASON = "Editing needs the Admin or Author role, or a Reviewer who holds this document's review.";

/** The document's HITL review events, in the shape History's Review Activity lists. */
export const documentReviewHistory = (
  events: DirectEditDocument['hitlReviewHistory'],
): { at: string; event: string; actor: string | null; details: string | null }[] =>
  (events ?? [])
    .filter((e) => !!e.timestamp && !!e.action)
    .map((e) => {
      const reason = typeof e.reason === 'string' ? e.reason : undefined;
      const note = [typeof e.note === 'string' ? e.note : undefined, e.sectionId ? `Section ${e.sectionId}` : undefined]
        .filter(Boolean)
        .join(' · ');
      return {
        at: e.timestamp as string,
        event: e.action as string,
        actor: e.user ?? null,
        details: reason || note ? JSON.stringify({ reason, note: note || undefined }) : null,
      };
    });

interface DocumentEditProps {
  objectKey: string;
  /** The section to open first (from `?section=`). */
  initialSectionId?: string | null;
}

const DocumentEdit = ({ objectKey, initialSectionId = null }: DocumentEditProps): React.JSX.Element => {
  const adapter = useDocumentEdit(objectKey);
  const { document: doc, loading, loadError, reload } = adapter;
  const navigate = useNavigate();
  const { user, setSuccessMessage } = useAppContext();
  const { settings } = useSettingsContext();
  const { isAdmin, isReviewer, canWrite, canWorkReviewQueues } = useUserRole();
  const { mergedConfig } = useConfiguration(doc?.configVersion || 'default');
  const editor = useRef<SectionEditorController | null>(null);
  const [view, setView] = useEditView();
  // The document's page: where Cancel and Reprocess leave to.
  const documentPath = `${DOCUMENTS_PATH}/${encodeURIComponent(objectKey)}`;
  const { cancel, dialog: cancelDialog } = useEditCancel(editor, documentPath);
  const [busy, setBusy] = useState<EditAction | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [sectionData, setSectionData] = useState<Record<string, unknown> | null>(null);
  const [selectedSectionId, setSelectedSectionId] = useState<string | null>(null);
  const [showHistory, setShowHistory] = useState(false);
  const [confirmProcess, setConfirmProcess] = useState(false);
  const [regroupOpen, setRegroupOpen] = useState(false);
  const [isSavingGrouping, setIsSavingGrouping] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const selectedSection = adapter.sections.find((s) => s.sectionId === selectedSectionId) ?? null;
  const evaluation = useSectionEvaluation({
    enabled: !!doc,
    objectKey,
    outputUri: selectedSection?.resultUri || null,
    evaluationStatus: doc?.evaluationStatus,
    reviewerOnly: adapter.reviewerOnly,
  });
  const { urls: thumbnailUrls } = usePageThumbnails(doc?.pages, { enabled: regroupOpen });

  const boardPages = useMemo(
    () => (doc?.pages ?? []).map((page) => ({ id: Number(page.Id), imageUri: thumbnailUrls[String(page.Id)] ?? null })),
    [doc?.pages, thumbnailUrls],
  );
  const boardSections: GroupedSection[] = useMemo(
    () =>
      (doc?.sections ?? []).map((s) => ({
        sectionId: String(s.Id),
        documentClass: s.Class ?? null,
        pageIds: (s.PageIds ?? []).map((id) => Number(id)),
      })),
    [doc?.sections],
  );

  const onSave = useCallback(
    (sectionId: string, data: Record<string, unknown>) => adapter.saveSection(sectionId, data, evaluation),
    [adapter.saveSection, evaluation],
  );

  if (loading && !doc) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" /> Loading document…
      </Box>
    );
  }
  if (!doc) {
    const notFound = loadError === 'NOT_FOUND';
    return (
      <Alert
        type={notFound ? 'warning' : 'error'}
        header={notFound ? 'Document not found' : "Couldn't open this document"}
        action={!notFound && <Button onClick={() => void reload()}>Retry</Button>}
      >
        {notFound ? 'This document no longer exists. ' : `${loadError ?? 'The document could not be loaded.'} `}
        <Link href={`#${DOCUMENTS_PATH}`}>Back to Documents</Link>
      </Alert>
    );
  }

  const name = documentName(objectKey);
  const status = doc.objectStatus ?? '';
  const loginId = (user?.signInDetails?.loginId ?? '').toLowerCase();
  const holdsReview =
    isReviewer &&
    ((!!doc.hitlReviewOwner && doc.hitlReviewOwner === user?.username) ||
      (!!doc.hitlReviewOwnerEmail && doc.hitlReviewOwnerEmail.toLowerCase() === loginId));
  const inReview = adapter.inReview;
  const inReviewReason = inReviewQueueEditReason(canWorkReviewQueues);

  let readOnlyReason: string | undefined;
  if (inReview) readOnlyReason = inReviewReason;
  else if (!isEditableStatus(status)) readOnlyReason = PROCESSING_REASON;
  else if (!canWrite && !holdsReview) readOnlyReason = EDIT_ROLE_REASON;
  const readOnly = readOnlyReason !== undefined;
  // In review, the editing actions stay on the bar, disabled with the in-review reason.
  const showEditing = !readOnly || inReview;

  let processReason: string | undefined = readOnlyReason;
  if (!processReason && !(isAdmin || isReviewer)) processReason = PROCESS_ROLE_REASON;
  if (!processReason && saving) processReason = 'Saving your changes…';

  const bdaMode = String((settings as Record<string, unknown>)?.IDPPattern ?? '')
    .toLowerCase()
    .includes('pattern1');

  const handleAction = async (action: EditAction) => {
    setError(null);
    if (action === 'cancel') {
      cancel();
      return;
    }
    if (action === 'regroup') {
      setRegroupOpen(true);
      return;
    }
    if (action === 'save') {
      setBusy('save');
      try {
        if (await editor.current?.save()) setSuccessMessage(`Saved your changes to ${name}.`);
      } finally {
        setBusy(null);
      }
      return;
    }
    if (action !== 'reprocess') return;
    // The edits are saved first, so the reprocess evaluates them.
    if (editor.current?.isDirty()) {
      setBusy('save');
      const ok = await editor.current.save().finally(() => setBusy(null));
      if (!ok) return;
    }
    setConfirmProcess(true);
  };

  const runProcessChanges = async () => {
    setBusy('reprocess');
    try {
      await adapter.processChanges();
      setConfirmProcess(false);
      setSuccessMessage(`${name} was submitted for reprocessing.`);
      navigate(documentPath);
    } catch (err) {
      setConfirmProcess(false);
      setError(`Could not reprocess the document: ${getErrorMessage(err)}`);
    } finally {
      setBusy(null);
    }
  };

  const handleSaveGrouping = async (next: GroupedSection[]) => {
    setIsSavingGrouping(true);
    setError(null);
    try {
      const result = await saveDocumentGrouping(objectKey, next);
      if (!result.success) {
        setError(result.message ?? 'The page grouping could not be saved.');
        return;
      }
      setRegroupOpen(false);
      if (result.message) setSuccessMessage(result.message);
      await reload();
    } catch (err) {
      setError(`Could not save the page grouping: ${getErrorMessage(err)}`);
    } finally {
      setIsSavingGrouping(false);
    }
  };

  // One alert for the screen's notices; an error keeps its own.
  const notices = (
    <EditorNotices
      notices={[
        !!error && { id: 'error', type: 'error', content: error, onDismiss: () => setError(null) },
        inReview && {
          id: 'in-review',
          type: 'info',
          content: 'This document is in a review queue. Edits are made in the queue.',
          action: canWorkReviewQueues ? (
            <Button href={reviewItemHref(objectKey)} iconName="arrow-right">
              Review in queue
            </Button>
          ) : undefined,
        },
        !inReview &&
          readOnlyReason === PROCESSING_REASON && {
            id: 'processing',
            type: 'info',
            content: 'View only. This document is still processing; its results can be edited once processing finishes.',
          },
        !inReview && readOnlyReason === EDIT_ROLE_REASON && { id: 'role', type: 'info', content: `View only. ${EDIT_ROLE_REASON}` },
      ]}
    />
  );

  return (
    <EditScreen
      title={name}
      status={status}
      objectKey={objectKey}
      actions={{
        showSave: showEditing,
        saveDisabledReason: readOnlyReason ?? (!dirty ? 'No changes to save' : saving ? 'Saving your changes…' : undefined),
        showRegroup: showEditing,
        regroupDisabledReason: readOnlyReason ?? (boardSections.length === 0 ? 'This document has no sections' : undefined),
        showCancel: !readOnly,
        cancelDisabledReason: saving ? 'Saving your changes…' : undefined,
        primaryDisabledReason: processReason,
        primaryHint: 'Save your changes and reprocess the document (evaluation and summary)',
        onShowHistory: () => setShowHistory(true),
        view,
        onViewChange: setView,
        busy,
        onAction: (action) => void handleAction(action),
      }}
      notices={notices}
      dialogs={
        <>
          {showHistory && (
            <EditorHistoryModal
              visible
              onDismiss={() => setShowHistory(false)}
              sectionData={sectionData}
              baselineData={evaluation.baseline}
              reviewHistory={documentReviewHistory(doc.hitlReviewHistory)}
            />
          )}
          {cancelDialog}
          <ProcessChangesConfirmModal
            visible={confirmProcess}
            structuralChanges={false}
            bdaMode={bdaMode}
            busy={busy === 'reprocess'}
            onConfirm={() => void runProcessChanges()}
            onCancel={() => setConfirmProcess(false)}
          />
          {regroupOpen && (
            <PageGroupingEditor
              presentation="modal"
              pages={boardPages}
              sections={boardSections}
              classOptions={getConfigClassOptions(mergedConfig)}
              consequence={
                <>
                  Moving pages rewrites this document&apos;s <b>section grouping</b>. The extracted field values are <b>kept</b> and the
                  document is <b>not</b> reprocessed, so they may no longer match their pages. Use <b>Reprocess</b> afterwards if you would
                  rather the pipeline redo them.
                </>
              }
              saveLabel="Save page grouping"
              isSaving={isSavingGrouping}
              onSave={handleSaveGrouping}
              onCancel={() => setRegroupOpen(false)}
            />
          )}
        </>
      }
    >
      {adapter.sections.length === 0 ? (
        <EditDocumentEmpty body={view === 'source' ? <FileViewer objectKey={objectKey} /> : undefined}>
          This document has no sections to edit.
        </EditDocumentEmpty>
      ) : (
        <SectionEditor
          key={objectKey}
          objectKey={objectKey}
          sections={adapter.sections}
          pages={adapter.pages}
          isReadOnly={readOnly}
          loadSection={adapter.loadSection}
          onSave={onSave}
          onDirtyChange={setDirty}
          onSavingChange={setSaving}
          onDataChange={setSectionData}
          onSelectedSectionChange={setSelectedSectionId}
          controllerRef={editor}
          actionsPlacement="external"
          initialSectionId={initialSectionId}
          mergedConfig={mergedConfig ?? undefined}
          evaluation={evaluation}
          showHistoryTab={false}
          variant="widget"
          /* The editor stays mounted behind the source view, so unsaved edits survive the switch. */
          body={view === 'source' ? <FileViewer objectKey={objectKey} /> : undefined}
        />
      )}
    </EditScreen>
  );
};

export default DocumentEdit;
