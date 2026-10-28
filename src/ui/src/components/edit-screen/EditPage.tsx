// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit page, the same for every mode: it renders one EditModel (see edit-model.ts).
// The layout and breadcrumbs, the load and error states, the bar (EditScreen), the
// shared SectionEditor, the Edit | View switch, History, Skip's reason, Cancel, the
// Process Changes confirmation, Edit Page Class and the exit once the work is done are
// all here, once. What applies, what each action does and what it says come from the
// model, so this file holds no mode-specific code (pinned by
// edit-page.no-mode-branching.test.ts).
import React, { useState } from 'react';
import { Alert, Box, Button, Spinner } from '@cloudscape-design/components';

import useAppContext from '../../contexts/app';
import ProcessChangesConfirmModal from '../common/ProcessChangesConfirmModal';
import ReasonModal from '../common/ReasonModal';
import FileViewer from '../document-viewer/FileViewer';
import ReviewQueuesLayout from '../review-queues/ReviewQueuesLayout';
import EditScreen, { EditDocumentEmpty } from './EditScreen';
import type { EditAction } from './EditScreen';
import EditorHistoryModal from './EditorHistoryModal';
import EditorNotices from './EditorNotices';
import type { EditExit, EditModel, EditReady } from './edit-model';
import SectionEditor from './SectionEditor';
import EditScreenToolsPanel from './tools-panel';
import useEditCancel from './use-edit-cancel';
import useEditExit from './use-edit-exit';
import useEditorSession from './use-editor-session';
import type { EditorSession } from './use-editor-session';
import usePageClassBoard from './use-page-class-board';

interface EditPageProps {
  model: EditModel;
  /** The mode's editor session; a page that only shows a load or error state has none. */
  session?: EditorSession;
}

const EditPage = ({ model, session: given }: EditPageProps): React.JSX.Element => {
  const ownSession = useEditorSession();
  const session = given ?? ownSession;
  const { setErrorMessage, setSuccessMessage } = useAppContext();
  const exit = useEditExit();
  const ready: EditReady | null = model.state.kind === 'ready' ? model.state : null;
  const [error, setError] = useState<string | null>(null);
  const [showHistory, setShowHistory] = useState(false);
  const [askSkipReason, setAskSkipReason] = useState(false);
  const [confirmProcess, setConfirmProcess] = useState(false);
  const { cancel, dialog: cancelDialog } = useEditCancel(session.controllerRef, ready?.cancelTo ?? '');
  const board = usePageClassBoard(ready?.board ?? null, session.selectedSectionId, session.sectionData);
  const { navigation } = model;

  const page = (content: React.ReactNode) => (
    <ReviewQueuesLayout
      root={navigation.root}
      crumbs={navigation.crumbs}
      navActiveHref={navigation.activeHref}
      tools={<EditScreenToolsPanel />}
    >
      {content}
    </ReviewQueuesLayout>
  );

  if (model.state.kind === 'loading') {
    return page(
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" /> {model.state.text}
      </Box>,
    );
  }
  if (model.state.kind === 'failure') {
    const { type, header, body, retry } = model.state;
    return page(
      <Alert type={type} header={header} action={retry && <Button onClick={retry}>Retry</Button>}>
        {body}
      </Alert>,
    );
  }

  const r = model.state;
  const controller = () => session.controllerRef.current;

  /** Report a failed action where the model says, then let it recover. */
  const failed = (action: EditAction) => (err: unknown) => {
    const message = r.failures.describe(action, err);
    if (message) {
      if (r.failures.surface === 'notice') setError(message);
      else setErrorMessage(message);
    }
    r.failures.after?.(action);
  };

  /** Run an action that may finish the work; leave once it has. */
  const runToExit = async (action: EditAction, fn: () => Promise<EditExit | null>): Promise<boolean> => {
    let done: EditExit | null = null;
    const ok = await session.run(
      action,
      async () => {
        done = await fn();
      },
      failed(action),
    );
    if (ok && done) exit(done);
    return ok;
  };

  const runProcess = async () => {
    await runToExit('process', r.process.run);
    setConfirmProcess(false);
  };

  const confirmSkip = async (reason: string, note: string | undefined) => {
    const { skip } = r;
    if (!skip) return;
    await runToExit('skip', () => skip.run(reason, note));
    setAskSkipReason(false);
  };

  const handleAction = async (action: EditAction) => {
    setError(null);
    if (action === 'cancel') {
      cancel();
      return;
    }
    if (action === 'skip') {
      // The reason is asked for first; confirmSkip does the skip.
      setAskSkipReason(true);
      return;
    }
    if (action === 'save') {
      await session.run(
        'save',
        async () => {
          if ((await controller()?.save()) && r.savedMessage) setSuccessMessage(r.savedMessage);
        },
        failed('save'),
      );
      return;
    }
    if (action === 'claim' || action === 'baseline') {
      const fn = action === 'claim' ? r.claim : r.baseline;
      if (!fn) return;
      await session.run(
        action,
        async () => {
          setSuccessMessage(await fn());
        },
        failed(action),
      );
      return;
    }
    if (action === 'regroup') {
      const { beforeOpen } = r.board ?? {};
      if (!beforeOpen) {
        board.open();
        return;
      }
      let mayOpen = false;
      const ok = await session.run(
        'regroup',
        async () => {
          mayOpen = await beforeOpen();
        },
        failed('regroup'),
      );
      if (ok && mayOpen) board.open();
      return;
    }
    // Process Changes.
    if (!r.process.confirm) {
      await runToExit('process', r.process.run);
      return;
    }
    // The edits are saved first, so processing the changes evaluates them; then it is confirmed.
    if (controller()?.isDirty()) {
      let saved = false;
      await session.run(
        'save',
        async () => {
          saved = !!(await controller()?.save());
        },
        failed('save'),
      );
      if (!saved) return;
    }
    setConfirmProcess(true);
  };

  const { header, bar, editor, history } = r;
  const sourceView = header.source && session.view === 'source' ? <FileViewer {...header.source} /> : undefined;
  const notices = (
    <EditorNotices notices={[!!error && { id: 'error', type: 'error', content: error, onDismiss: () => setError(null) }, ...r.notices]} />
  );

  return page(
    <EditScreen
      title={header.title}
      status={header.status}
      objectKey={header.objectKey}
      reportSectionData={header.offerReport ? session.sectionData : undefined}
      summaryReportUri={header.summary?.reportUri}
      summaryUnavailableReason={header.summary?.unavailableReason}
      actions={{
        ...bar,
        onShowHistory: bar.offerHistory ? () => setShowHistory(true) : undefined,
        ...(header.source ? { view: session.view, onViewChange: session.setView } : {}),
        busy: session.busy,
        onAction: (action) => void handleAction(action),
      }}
      notices={notices}
      context={r.context}
      dialogs={
        <>
          {/* Mounted only while open, so their buttons are not on the screen otherwise. */}
          {showHistory && (
            <EditorHistoryModal
              visible
              onDismiss={() => setShowHistory(false)}
              sectionData={session.sectionData}
              baselineData={history.baseline}
              reviewHistory={history.reviewActivity}
            />
          )}
          {cancelDialog}
          {r.process.confirm && (
            <ProcessChangesConfirmModal
              visible={confirmProcess}
              structuralChanges={false}
              bdaMode={r.process.confirm.bdaMode}
              busy={session.busy === 'process'}
              onConfirm={() => void runProcess()}
              onCancel={() => setConfirmProcess(false)}
            />
          )}
          {board.dialog}
          {askSkipReason && r.skip && (
            <ReasonModal
              visible
              header={r.skip.header}
              reasons={r.skip.reasons}
              confirmLabel="Skip"
              loading={session.busy === 'skip'}
              onDismiss={() => setAskSkipReason(false)}
              onConfirm={(reason, note) => void confirmSkip(reason, note)}
            >
              {r.skip.body}
            </ReasonModal>
          )}
        </>
      }
    >
      {editor &&
        (editor.sections.length === 0 ? (
          <EditDocumentEmpty body={sourceView}>{editor.emptyText}</EditDocumentEmpty>
        ) : (
          <SectionEditor
            key={header.objectKey}
            objectKey={header.objectKey}
            sections={editor.sections}
            pages={editor.pages}
            pagesLoading={editor.pagesLoading}
            isReadOnly={editor.readOnly}
            loadSection={editor.loadSection}
            onSave={editor.saveSection}
            onSaved={editor.onSaved}
            onLoaded={editor.onLoaded}
            reloadToken={editor.reloadToken}
            initialSectionId={editor.initialSectionId}
            focusFieldPath={editor.focusFieldPath}
            buildFieldLink={editor.buildFieldLink}
            autosaveMs={editor.autosaveMs}
            confirmSectionSwitch={editor.confirmSectionSwitch}
            recordEditHistory={editor.recordEditHistory}
            mergedConfig={editor.mergedConfig}
            evaluation={editor.evaluation}
            defaultFilter={editor.defaultFilter}
            unsavedChangesMessage={editor.unsavedChangesMessage}
            headerExtras={editor.slots?.headerExtras}
            classSlot={editor.slots?.classSlot}
            emptyFieldsSlot={editor.slots?.emptyFieldsSlot}
            pagesNotice={editor.slots?.pagesNotice}
            {...session.editorBindings}
            /* The editor stays mounted behind the source view, so unsaved edits survive the switch. */
            body={sourceView}
          />
        ))}
    </EditScreen>,
  );
};

export default EditPage;
