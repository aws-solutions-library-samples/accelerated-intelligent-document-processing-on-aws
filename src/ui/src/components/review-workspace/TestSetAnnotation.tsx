// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The annotation screen for a Test Studio document: the ground-truth editor (the
// test-set adapter over the shared SectionEditor), Complete and Skip. Saving a section
// marks it reviewed; Complete confirms the sections not saved yet, so "the labels
// are correct" and "I corrected them" end the same way, and needs no edit first.
// Skip completes the task without reviewing the labels. The assignee works the
// document; an Admin or Author may too. Nothing here claims or releases.
import React, { useCallback, useState } from 'react';
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
import useUserRole from '../../hooks/use-user-role';
import { describeApiError } from '../../hooks/utils/graphql-error';
import { MY_TASKS_PATH, testSetAnnotateHref, testSetItemPath, testSetQueuePath } from '../../routes/constants';
import FileViewer from '../document-viewer/FileViewer';
import FavoriteButton from '../review-queues/FavoriteButton';
import ReviewPresence from '../review-queues/ReviewPresence';
import GroundTruthVisualEditor from '../test-studio/GroundTruthVisualEditor';
import {
  DRAFT_PROMPT_BODY,
  DRAFT_PROMPT_HEADER,
  annotationEditFlags,
  draftBadgeHover,
  draftOpenedMessage,
  groundTruthAlertSuffix,
} from '../test-studio/annotation-edit-flags';
import AnnotationFrame from './AnnotationFrame';
import type { AnnotationAction } from './AnnotationFrame';
import { NOT_ASSIGNED_MESSAGE, useTestSetAnnotation } from './test-set-adapter';
import useAnnotationExit from './use-annotation-exit';

type DocView = 'ground-truth' | 'source';

interface TestSetAnnotationProps {
  testSetId: string;
  objectKey: string;
}

const TestSetAnnotation = ({ testSetId, objectKey }: TestSetAnnotationProps): React.JSX.Element => {
  const adapter = useTestSetAnnotation(testSetId, objectKey);
  const { doc, draft, queue, loading, loadError, reload } = adapter;
  const { setErrorMessage, setSuccessMessage } = useAppContext();
  const { settings } = useSettingsContext();
  const { isAdmin, isAuthor, isReviewer, isAnnotator, canAnnotate, loading: roleLoading } = useUserRole();
  const exit = useAnnotationExit();
  const [searchParams] = useSearchParams();
  const focusField = searchParams.get('field');
  // ?view=source (the queue's View action) opens on the source document.
  const [docView, setDocView] = useState<DocView>(searchParams.get('view') === 'source' ? 'source' : 'ground-truth');
  const [busy, setBusy] = useState<AnnotationAction | null>(null);
  const [isOpeningDraft, setIsOpeningDraft] = useState(false);
  const [dirty, setDirty] = useState(false);
  // Sections saved on this screen: they already count as reviewed.
  const [savedSections, setSavedSections] = useState<ReadonlySet<string>>(new Set());

  const testSetBucket = (settings as Record<string, unknown>).TestSetBucket as string | undefined;
  const queuePath = testSetQueuePath(testSetId);

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
        header={notAssigned ? 'This document is not assigned to you' : "Couldn't open this document"}
        action={!notAssigned && <Button onClick={() => void reload()}>Retry</Button>}
      >
        {notAssigned ? (
          <>
            An Admin assigns Test Studio documents to Annotators. Your assigned documents are in{' '}
            <Link href={`#${MY_TASKS_PATH}`}>My Work › Tasks</Link>.
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
  const remaining = sections.filter((s) => !savedSections.has(s.sectionId)).map((s) => s.sectionId);

  // Complete needs no edit and no Start annotating first: it opens the version
  // transition itself. Only the role half of the editor's read-only state applies.
  const roleCannotSave = editFlags.saveViaReview ? !editFlags.canSaveViaReview : !editFlags.canSaveDirectToBaseline;
  let skipReason: string | undefined;
  if (!mayWork) skipReason = doc.assignedTo ? `Assigned to ${doc.assignedTo}` : 'This document is not assigned to you';
  else if (dirty) skipReason = 'Save or discard your changes first';
  else if (loading) skipReason = 'Loading…';
  let completeReason: string | undefined = skipReason;
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

  const handleAction = async (action: AnnotationAction) => {
    setBusy(action);
    try {
      if (action === 'skip') {
        await adapter.skip();
        exit(queuePath, `${objectKey} is complete (skipped).`);
        return;
      }
      // Idempotent server-side: the version transition this completion belongs to.
      if (!draft) await adapter.openDraft();
      await adapter.confirmSections(remaining);
      exit(queuePath, `${objectKey} is complete and marked reviewed.`);
    } catch (err) {
      setErrorMessage(describeApiError(err, action === 'skip' ? 'skip this document' : 'complete this document'));
      void reload();
    } finally {
      setBusy(null);
    }
  };

  const handleSaved = (baselineKey: string) => {
    const sectionId = sections.find((s) => s.baselineKey === baselineKey)?.sectionId;
    const next = new Set(savedSections);
    if (sectionId) next.add(sectionId);
    setSavedSections(next);
    // Saving the last section reviews the document, which ends its assignment.
    if (sections.every((s) => next.has(s.sectionId))) {
      exit(queuePath, `Saved. ${objectKey} is marked reviewed.`);
      return;
    }
    setSuccessMessage(`Section ${sectionId ?? ''} saved. Check the other sections, then choose Complete.`);
  };

  const notices = (
    <>
      {!testSetBucket && <Alert type="error">TestSetBucket is not configured in settings.</Alert>}
      {!mayWork && (
        <Alert type="info">
          View only. {doc.assignedTo ? `This document is assigned to ${doc.assignedTo}.` : 'It is not assigned to you.'}
        </Alert>
      )}
      {mayWork && !doc.claimedByMe && (
        <Alert type="info">
          {doc.assignedTo
            ? `Assigned to ${doc.assignedTo}. You can still correct and complete it.`
            : 'Not assigned to an Annotator. You can still correct and complete it, or assign it from the test set queue.'}
        </Alert>
      )}
      {!draft && (
        <Alert
          type="info"
          header={DRAFT_PROMPT_HEADER}
          action={
            <Button variant="primary" loading={isOpeningDraft} disabled={!canAnnotate} onClick={() => void handleOpenDraft()}>
              Start annotating
            </Button>
          }
        >
          {DRAFT_PROMPT_BODY}
        </Alert>
      )}
      {!doc.reviewObjectKey && !doc.labelSource && (
        <Alert type="warning" header="Not ready to annotate">
          This document has no labels yet. Generate draft labels for the set first, then it can be reviewed.
        </Alert>
      )}
      {!doc.reviewObjectKey && doc.labelSource && (
        <Alert type="info" header="Authored ground truth">
          This document carries authored ground truth rather than a machine draft. Check its labels, correct them if needed, then Complete.
          {groundTruthAlertSuffix(editFlags.canSaveDirectToBaseline, draft)}
        </Alert>
      )}
    </>
  );

  return (
    <AnnotationFrame
      title={objectKey}
      objectKey={doc.inputKey}
      badges={
        <>
          <Badge color="grey">Test Studio</Badge>
          <StatusIndicator type={doc.reviewed ? 'success' : 'pending'}>{doc.reviewed ? 'Reviewed' : 'To review'}</StatusIndicator>
          <FavoriteButton
            favorite={{
              id: `test-set-item:${testSetId}/${objectKey}`,
              kind: 'test-set-item',
              name: objectKey,
              href: `#${testSetItemPath(testSetId, objectKey)}`,
              context: `Test set ${testSetId}`,
            }}
          />
        </>
      }
      meta={
        <SpaceBetween direction="horizontal" size="l" alignItems="center">
          <Box variant="small" color="text-body-secondary">
            In test set <Link href={`#${queuePath}`}>{testSetId}</Link>
          </Box>
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
        completeDisabledReason: completeReason,
        completeHint: 'Mark the labels reviewed: saved sections as corrected, the rest as correct',
        showReject: false,
        showSkip: true,
        skipDisabledReason: skipReason,
        busy,
        onAction: (action) => void handleAction(action),
      }}
      notices={notices}
    >
      {testSetBucket && (
        <SpaceBetween size="s">
          <SegmentedControl
            selectedId={docView}
            onChange={({ detail }) => setDocView(detail.selectedId as DocView)}
            options={[
              { id: 'ground-truth', text: 'Annotate' },
              { id: 'source', text: 'View source document' },
            ]}
          />
          {docView === 'source' && <FileViewer objectKey={doc.inputKey} bucket={testSetBucket} presignVia="server" />}
          {docView === 'ground-truth' && (
            <GroundTruthVisualEditor
              key={doc.objectKey}
              bucket={testSetBucket}
              inputKey={doc.inputKey}
              objectKey={doc.objectKey}
              sections={sections}
              /* Read-only until the version transition is open, by this document's save
                 path, and for anyone the document is not assigned to (bar Admin/Author). */
              isReadOnly={editFlags.isReadOnly || !mayWork}
              canChangeClass={editFlags.canChangeClass && mayWork}
              onSave={adapter.saveSection}
              onSaved={handleSaved}
              onDirtyChange={setDirty}
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
