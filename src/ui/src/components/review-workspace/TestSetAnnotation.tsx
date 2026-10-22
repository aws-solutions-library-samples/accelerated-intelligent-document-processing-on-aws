// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The unified editor for a Test Studio document: the ground-truth editor (the
// test-set adapter over the shared SectionEditor), Complete and Skip. Saving a
// section marks it reviewed, so there is no Save as Draft here: switching sections
// saves the open one, and Complete saves it and confirms the sections not saved
// yet, so "the labels are correct" and "I corrected them" end the same way.
// Skip gives the document back to its queue, unassigned and not reviewed, for another
// Annotator to claim. The assignee works the document; an Admin or Author may too. An
// Annotator listed on the set's queue can open any document read-only, and Claim an
// unassigned one to work it.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  Alert,
  Badge,
  Box,
  Button,
  ContentLayout,
  Header,
  Link,
  Popover,
  SegmentedControl,
  SpaceBetween,
  Spinner,
  StatusIndicator,
} from '@cloudscape-design/components';

import useAppContext from '../../contexts/app';
import useSettingsContext from '../../contexts/settings';
import { useOptionalReviewWork } from '../../hooks/use-review-work';
import useUserRole from '../../hooks/use-user-role';
import { describeApiError } from '../../hooks/utils/graphql-error';
import { MY_QUEUES_PATH, testSetAnnotateHref, testSetQueuePath } from '../../routes/constants';
import FileViewer from '../document-viewer/FileViewer';
import ReviewPresence from '../review-queues/ReviewPresence';
import { claimTestSetDocument } from '../review-queues/reviewer-assignment';
import GroundTruthVisualEditor from '../test-studio/GroundTruthVisualEditor';
import {
  DRAFT_PROMPT_BODY,
  DRAFT_PROMPT_HEADER,
  annotationEditFlags,
  draftBadgeHover,
  draftOpenedMessage,
  groundTruthAlertSuffix,
  mayChangeClassFor,
} from '../test-studio/annotation-edit-flags';
import AnnotationFrame from './AnnotationFrame';
import EditorNotices from './EditorNotices';
import EditorHistoryModal from './EditorHistoryModal';
import ProcessingReportModal from './ProcessingReportModal';
import type { SectionEditorController } from './SectionEditor';
import ReasonModal from '../common/ReasonModal';
import type { AnnotationAction } from './AnnotationFrame';
import { NOT_ASSIGNED_MESSAGE, useTestSetAnnotation } from './test-set-adapter';
import useAnnotationExit from './use-annotation-exit';

type DocView = 'ground-truth' | 'source';

/** The item's test set, for the breadcrumbs. */
export interface ItemTestSet {
  id: string;
  name: string | null;
}

interface TestSetAnnotationProps {
  testSetId: string;
  objectKey: string;
  /** Told the test set's name once its queue has loaded. */
  onTestSet?: (testSet: ItemTestSet) => void;
}

const TestSetAnnotation = ({ testSetId, objectKey, onTestSet }: TestSetAnnotationProps): React.JSX.Element => {
  const adapter = useTestSetAnnotation(testSetId, objectKey);
  const { doc, draft, queue, loading, loadError, reload } = adapter;
  const { setErrorMessage, setSuccessMessage } = useAppContext();
  const { settings } = useSettingsContext();
  const { isAdmin, isAuthor, isReviewer, isAnnotator, canAnnotate, loading: roleLoading } = useUserRole();
  const exit = useAnnotationExit();
  const reviewWork = useOptionalReviewWork();
  const [searchParams] = useSearchParams();
  const focusField = searchParams.get('field');
  // ?view=source (the queue's View action) opens on the source document.
  const [docView, setDocView] = useState<DocView>(searchParams.get('view') === 'source' ? 'source' : 'ground-truth');
  const [busy, setBusy] = useState<AnnotationAction | null>(null);
  const [askSkipReason, setAskSkipReason] = useState(false);
  const [isOpeningDraft, setIsOpeningDraft] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [sectionData, setSectionData] = useState<Record<string, unknown> | null>(null);
  const [showHistory, setShowHistory] = useState(false);
  const [showReport, setShowReport] = useState(false);
  const [regroupOpen, setRegroupOpen] = useState(false);
  const editor = useRef<SectionEditorController | null>(null);
  // Sections saved on this screen: they already count as reviewed. A ref, so Process
  // Changes sees a save it has just made.
  const savedSections = useRef<Set<string>>(new Set());
  // While Complete runs, a save it makes doesn't exit on its own.
  const processing = useRef(false);

  const queueName = queue?.name ?? null;
  const queueLoaded = !!queue;
  useEffect(() => {
    if (queueLoaded) onTestSet?.({ id: testSetId, name: queueName });
  }, [queueLoaded, queueName, testSetId, onTestSet]);

  const testSetBucket = (settings as Record<string, unknown>).TestSetBucket as string | undefined;
  const queuePath = testSetQueuePath(testSetId);
  const workItemId = `t:${testSetId}:${objectKey}`;

  const buildFieldLink = useCallback(
    (fieldPath: string) => `${window.location.origin}/${testSetAnnotateHref(testSetId, objectKey)}?field=${encodeURIComponent(fieldPath)}`,
    [testSetId, objectKey],
  );

  if (!roleLoading && !canAnnotate) {
    return (
      <ContentLayout header={<Header variant="h1">Annotate</Header>}>
        <Alert type="error" header="Not available for your account">
          Ground-truth annotation requires an Annotator, Author or Admin role.
        </Alert>
      </ContentLayout>
    );
  }
  if (loadError && !doc) {
    const notAssigned = loadError === NOT_ASSIGNED_MESSAGE;
    return (
      <Alert
        type={notAssigned ? 'warning' : 'error'}
        header={notAssigned ? "You can't open this document" : "Couldn't open this document"}
        action={!notAssigned && <Button onClick={() => void reload()}>Retry</Button>}
      >
        {notAssigned ? (
          <>
            Only the Annotators listed on this test set&apos;s queue can open its documents. The queues that list you are in{' '}
            <Link href={`#${MY_QUEUES_PATH}`}>My Work › Queues</Link>.
          </>
        ) : (
          loadError
        )}
      </Alert>
    );
  }
  if (!queue) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" /> Loading the document…
      </Box>
    );
  }
  if (!doc) {
    return (
      <Alert type="error" header="Document not found">
        This document isn&apos;t in the test set any more. <Link href={`#${queuePath}`}>Back to the test set</Link>
      </Alert>
    );
  }

  const roles = { isAdmin, isAuthor, isReviewer, isAnnotator, canAnnotate };
  const editFlags = annotationEditFlags(roles, doc, draft);
  const mayWork = doc.claimedByMe || isAdmin || isAuthor;
  const sections = doc.sections ?? [];

  // Complete needs no edit and no Start annotating first: it opens the version
  // transition itself. Only the role half of the editor's read-only state applies.
  const roleCannotSave = editFlags.saveViaReview ? !editFlags.canSaveViaReview : !editFlags.canSaveDirectToBaseline;
  // A completed document still opens, so its labels can be read and corrected, but
  // its task is over: completing or skipping it again would end nothing. Skip now
  // returns a document to its queue; `skipped` marks one skipped before that, which
  // still counts as complete.
  const isComplete = doc.reviewed || !!doc.skipped;
  // An Annotator (not an Admin, who assigns from the queue) claims a document nobody holds.
  const canClaim = isAnnotator && !isAdmin && !doc.assignedTo && !isComplete;
  let skipReason: string | undefined;
  if (isComplete) skipReason = doc.reviewed ? 'This document is already reviewed' : 'This document was already skipped';
  else if (!mayWork) skipReason = doc.assignedTo ? `Assigned to ${doc.assignedTo}` : 'This document is not assigned to you';
  else if (loading) skipReason = 'Loading…';
  // Complete saves the open section itself, so unsaved edits don't block it.
  let completeReason: string | undefined = skipReason;
  if (!skipReason && dirty) skipReason = 'Save or discard your changes first';
  if (!completeReason) {
    if (!doc.reviewObjectKey && !doc.labelSource) completeReason = 'This document has no labels yet; Skip completes it';
    else if (roleCannotSave) completeReason = 'Your role cannot save these labels';
    else if (sections.length === 0) completeReason = 'This document has no sections; Skip completes it';
  }

  const handleOpenDraft = async () => {
    setIsOpeningDraft(true);
    try {
      const opened = await adapter.openDraft();
      if (opened) setSuccessMessage(draftOpenedMessage(opened));
    } catch (err) {
      setErrorMessage(describeApiError(err, 'start annotating this set'));
    } finally {
      setIsOpeningDraft(false);
    }
  };

  /** Claim: the document is assigned to the caller; reload so the screen becomes editable. */
  const handleClaim = async () => {
    setBusy('claim');
    reviewWork?.beginMutation();
    try {
      await claimTestSetDocument(testSetId, objectKey);
      setSuccessMessage(`Claimed ${objectKey}. It is in My Work › Tasks.`);
    } catch (err) {
      setErrorMessage(describeApiError(err, 'claim this document'));
    } finally {
      setBusy(null);
    }
    await Promise.all([reload(), reviewWork?.refresh()]);
  };

  const handleAction = async (action: AnnotationAction) => {
    if (action === 'claim') {
      await handleClaim();
      return;
    }
    if (action === 'skip') {
      // The reason is asked for first; confirmSkip does the skip.
      setAskSkipReason(true);
      return;
    }
    if (action === 'discard') {
      editor.current?.discard();
      return;
    }
    if (action === 'regroup') {
      setRegroupOpen(true);
      return;
    }
    if (action !== 'process') return;
    setBusy(action);
    processing.current = true;
    try {
      // The open section's edits are saved first (which marks it reviewed).
      if (editor.current?.isDirty() && !(await editor.current.save())) return;
      // Idempotent server-side: the version transition this completion belongs to.
      if (!draft) await adapter.openDraft();
      const remaining = sections.filter((s) => !savedSections.current.has(s.sectionId)).map((s) => s.sectionId);
      if (remaining.length > 0) await adapter.confirmSections(remaining);
      exit(queuePath, `${objectKey} is complete and marked reviewed.`, workItemId);
    } catch (err) {
      setErrorMessage(describeApiError(err, 'complete this document'));
      void reload();
    } finally {
      processing.current = false;
      setBusy(null);
    }
  };

  const confirmSkip = async (reason: string, note: string | undefined) => {
    setBusy('skip');
    try {
      await adapter.skip(reason, note);
      setAskSkipReason(false);
      exit(queuePath, `${objectKey} returned to the queue.`, workItemId);
    } catch (err) {
      setAskSkipReason(false);
      setErrorMessage(describeApiError(err, 'skip this document'));
      void reload();
    } finally {
      setBusy(null);
    }
  };

  const handleSaved = (baselineKey: string) => {
    const sectionId = sections.find((s) => s.baselineKey === baselineKey)?.sectionId;
    if (sectionId) savedSections.current.add(sectionId);
    // Complete finishes the document itself.
    if (processing.current) return;
    // Saving the last section reviews the document, which ends its assignment.
    if (sections.every((s) => savedSections.current.has(s.sectionId))) {
      exit(queuePath, `Saved. ${objectKey} is marked reviewed.`, workItemId);
      return;
    }
    setSuccessMessage(`Section ${sectionId ?? ''} saved. Check the other sections, then choose Complete.`);
  };

  const editorReadOnly = editFlags.isReadOnly || !mayWork;
  const mayChangeClass = mayChangeClassFor(editFlags.canChangeClass && mayWork, editorReadOnly);

  // One alert for the screen's notices, so the space stays given over to annotation.
  const notices = (
    <EditorNotices
      notices={[
        !testSetBucket && { id: 'bucket', type: 'error', content: 'TestSetBucket is not configured in settings.' },
        !mayWork && {
          id: 'view-only',
          type: 'info',
          content: doc.assignedTo
            ? `View only. This document is assigned to ${doc.assignedTo}.`
            : canClaim
              ? 'View only. Claim this document to work on it; it is then assigned to you and shows in My Work › Tasks.'
              : 'View only. It is not assigned to you.',
        },
        !draft && {
          id: 'draft',
          type: 'info',
          content: (
            <>
              <b>{DRAFT_PROMPT_HEADER}.</b> {DRAFT_PROMPT_BODY}
            </>
          ),
          action: (
            <Button variant="primary" loading={isOpeningDraft} disabled={!canAnnotate} onClick={() => void handleOpenDraft()}>
              Start annotating
            </Button>
          ),
        },
        !doc.reviewObjectKey &&
          !doc.labelSource && {
            id: 'no-labels',
            type: 'warning',
            content: 'Not ready to annotate: this document has no labels yet. Generate draft labels for the set first.',
          },
        !doc.reviewObjectKey &&
          !!doc.labelSource && {
            id: 'authored',
            type: 'info',
            content: `Authored ground truth: check its labels, correct them if needed, then choose Complete.${groundTruthAlertSuffix(editFlags.canSaveDirectToBaseline, draft)}`,
          },
      ]}
    />
  );

  return (
    <AnnotationFrame
      title={objectKey}
      subtitle={
        <SpaceBetween size="xxxs">
          <span title={doc.inputKey}>Document: {doc.inputKey}</span>
          <span title={doc.objectKey}>Ground Truth: {doc.objectKey}</span>
        </SpaceBetween>
      }
      badges={
        <>
          <Badge color="grey">Test Studio</Badge>
          <StatusIndicator type={doc.reviewed ? 'success' : doc.skipped ? 'stopped' : 'pending'}>
            {doc.reviewed ? 'Reviewed' : doc.skipped ? 'Skipped' : 'To review'}
          </StatusIndicator>
        </>
      }
      meta={
        <SpaceBetween direction="horizontal" size="l" alignItems="center">
          {draft && (
            <Popover dismissButton={false} position="bottom" size="medium" triggerType="custom" content={draftBadgeHover(draft)}>
              <Badge color="blue">
                v{draft.baseVersion} &rarr; v{draft.draftVersion}
              </Badge>
            </Popover>
          )}
          <ReviewPresence assignee={doc.assignedTo} isMe={doc.claimedByMe} since={doc.assignedAt} />
        </SpaceBetween>
      }
      actions={{
        processDisabledReason: completeReason ?? (saving ? 'Saving your changes…' : undefined),
        processHint: 'Mark the labels reviewed: saved and edited sections as corrected, the rest as correct',
        showSkip: true,
        skipDisabledReason: skipReason,
        showDiscard: !editorReadOnly,
        discardDisabledReason: !dirty ? 'No changes to discard' : saving ? 'Saving your changes…' : undefined,
        showRegroup: true,
        regroupDisabledReason: !mayChangeClass || !testSetId ? 'Your role cannot change this document’s page grouping' : undefined,
        showClaim: canClaim,
        // Edit the ground truth, or view the source document.
        leading: testSetBucket ? (
          <SegmentedControl
            label="Edit or view the document"
            selectedId={docView}
            onChange={({ detail }) => setDocView(detail.selectedId as DocView)}
            options={[
              { id: 'ground-truth', text: 'Edit' },
              { id: 'source', text: 'View' },
            ]}
          />
        ) : undefined,
        onShowHistory: () => setShowHistory(true),
        onShowReport: () => setShowReport(true),
        busy,
        onAction: (action) => void handleAction(action),
      }}
      notices={notices}
      dialogs={
        // Mounted only while asked, so their buttons are not on the screen otherwise.
        <>
          {askSkipReason && (
            <ReasonModal
              visible
              header="Skip this document"
              reasons={queue.skipReasons ?? []}
              confirmLabel="Skip"
              loading={busy === 'skip'}
              onDismiss={() => setAskSkipReason(false)}
              onConfirm={(reason, note) => void confirmSkip(reason, note)}
            >
              <Box>It goes back to its queue, unassigned and not reviewed, for another Annotator to claim.</Box>
            </ReasonModal>
          )}
          {showHistory && <EditorHistoryModal visible onDismiss={() => setShowHistory(false)} sectionData={sectionData} />}
          {showReport && <ProcessingReportModal visible onDismiss={() => setShowReport(false)} sectionData={sectionData} />}
        </>
      }
    >
      {testSetBucket && (
        <SpaceBetween size="s">
          {docView === 'source' && <FileViewer objectKey={doc.inputKey} bucket={testSetBucket} />}
          {docView === 'ground-truth' && (
            <GroundTruthVisualEditor
              key={doc.objectKey}
              bucket={testSetBucket}
              inputKey={doc.inputKey}
              objectKey={doc.objectKey}
              sections={sections}
              /* Read-only until the version transition is open, by this document's save
                 path, and for anyone the document is not assigned to (bar Admin/Author). */
              isReadOnly={editorReadOnly}
              canChangeClass={editFlags.canChangeClass && mayWork}
              onSave={adapter.saveSection}
              onSaved={handleSaved}
              onDirtyChange={setDirty}
              onSavingChange={setSaving}
              onDataChange={setSectionData}
              controllerRef={editor}
              actionsPlacement="external"
              showHistoryTab={false}
              hideHeader
              saveOnSectionSwitch
              regroupPlacement="external"
              regroupOpen={regroupOpen}
              onRegroupOpenChange={setRegroupOpen}
              saveButtonText="Save labels"
              testSetId={testSetId}
              onReextracted={() => void reload()}
              focusFieldPath={focusField}
              buildFieldLink={buildFieldLink}
            />
          )}
        </SpaceBetween>
      )}
    </AnnotationFrame>
  );
};

export default TestSetAnnotation;
