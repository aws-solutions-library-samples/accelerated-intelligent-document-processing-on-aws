// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit page, the same for every mode: it renders one EditModel (see edit-model.ts).
// The layout and breadcrumbs, the load and error states, the bar (EditScreen), the
// shared SectionEditor, the Edit | View switch, History, the reason asked by Skip and by
// Reject, Cancel, Refresh, the Process Changes confirmation (which for a review asks how it
// ends: complete, or send back, and shows what each ending runs), Edit Page Grouping and
// the exit once the work is done are all here, once. What applies, what each action does
// and what it says come from the model, so this file holds no mode-specific code (pinned
// by edit-page.no-mode-branching.test.ts).
import React, { useState } from 'react';
import { Button } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';

import useAppContext from '../../contexts/app';
import LoadState from '../common/LoadState';
import ProcessChangesConfirmModal from '../common/ProcessChangesConfirmModal';
import ReasonModal from '../common/ReasonModal';
import FileViewer from '../document-viewer/FileViewer';
import ReviewCompletionModal from '../review-queues/ReviewCompletionModal';
import ReviewQueuesLayout from '../review-queues/ReviewQueuesLayout';
import EditScreen, { EditDocumentEmpty, skipMessage } from './EditScreen';
import type { EditAction } from './EditScreen';
import EditorHistoryModal from './EditorHistoryModal';
import EditorNotices from './EditorNotices';
import { editScreenSharedMessages } from './edit-messages';
import type { EditExit, EditModel, EditReady } from './edit-model';
import SectionEditor from './SectionEditor';
import EditScreenToolsPanel from './tools-panel';
import useDiscardConfirm from './use-discard-confirm';
import useEditCancel from './use-edit-cancel';
import useEditExit from './use-edit-exit';
import useEditorSession from './use-editor-session';
import type { EditorSession } from './use-editor-session';
import usePageClassBoard from './use-page-class-board';

const pageMessages = defineMessages<{
  claim: NoMessageValues;
  refreshDiscardHeader: NoMessageValues;
  refreshDiscardConfirm: NoMessageValues;
}>({
  claim: {
    id: 'editScreen.page.claim',
    defaultMessage: 'Claim',
    description: "Button in the edit page's notice that the fields are read-only, which assigns the item to the user so it can be edited.",
  },
  refreshDiscardHeader: {
    id: 'editScreen.page.refreshDiscard.header',
    defaultMessage: 'Discard your changes and reload?',
    description: 'Title of the dialog the edit page shows when Refresh would lose unsaved changes.',
  },
  refreshDiscardConfirm: {
    id: 'editScreen.page.refreshDiscard.confirm',
    defaultMessage: 'Discard and reload',
    description:
      'Confirm button of the dialog that asks before Refresh loses unsaved changes: it discards them and reads the document again.',
  },
});

interface EditPageProps {
  model: EditModel;
  /** The mode's editor session; a page that only shows a load or error state has none. */
  session?: EditorSession;
}

const EditPage = ({ model, session: given }: EditPageProps): React.JSX.Element => {
  const intl = useIntl();
  const ownSession = useEditorSession();
  const session = given ?? ownSession;
  const { setErrorMessage, setSuccessMessage } = useAppContext();
  const exit = useEditExit();
  const ready: EditReady | null = model.state.kind === 'ready' ? model.state : null;
  const [error, setError] = useState<string | null>(null);
  const [showHistory, setShowHistory] = useState(false);
  const [askSkipReason, setAskSkipReason] = useState(false);
  const [askRejectReason, setAskRejectReason] = useState(false);
  const [confirmProcess, setConfirmProcess] = useState(false);
  // Why the last send-back did not go through; shown in the dialog, which stays open.
  const [sendBackError, setSendBackError] = useState<string | null>(null);
  const { cancel, dialog: cancelDialog } = useEditCancel(session.controllerRef, ready?.cancelTo ?? '');
  const discard = useDiscardConfirm();
  // Bumped by Refresh, so the open section is read again; the section open then stays open.
  const [refreshToken, setRefreshToken] = useState(0);
  const [keepSectionId, setKeepSectionId] = useState<string | null>(null);
  const board = usePageClassBoard(ready?.board ?? null, session.selectedSectionId, session.sectionData);
  const { navigation } = model;

  const page = (content: React.ReactNode) => (
    <ReviewQueuesLayout trail={navigation.trail} navActiveHref={navigation.activeHref} tools={<EditScreenToolsPanel />}>
      {content}
    </ReviewQueuesLayout>
  );

  if (model.state.kind !== 'ready') return page(<LoadState state={model.state} />);

  const r = model.state;
  const controller = () => session.controllerRef.current;

  /**
   * Report a failed action where the model says (or, with `show`, where the caller
   * says: a dialog that stays open shows its own failure), then let it recover.
   */
  const failed = (action: EditAction, show?: (message: string) => void) => (err: unknown) => {
    const message = r.failures.describe(action, err);
    if (message) {
      if (show) show(message);
      else if (r.failures.surface === 'notice') setError(message);
      else setErrorMessage(message);
    }
    r.failures.after?.(action);
  };

  /** Run an action that may finish the work; leave once it has. */
  const runToExit = async (action: EditAction, fn: () => Promise<EditExit | null>, show?: (message: string) => void): Promise<boolean> => {
    let done: EditExit | null = null;
    const ok = await session.run(
      action,
      async () => {
        done = await fn();
      },
      failed(action, show),
    );
    if (ok && done) exit(done);
    return ok;
  };

  const runProcess = async () => {
    await runToExit('process', r.process.run);
    setConfirmProcess(false);
  };

  /** Send the document back from the review-completion dialog, which stays open if that fails. */
  const runSendBack = async (note: string | undefined) => {
    const { sendBack } = r;
    if (!sendBack) return;
    setSendBackError(null);
    if (await runToExit('send-back', () => sendBack.run(note), setSendBackError)) setConfirmProcess(false);
  };

  const confirmSkip = async (reason: string, note: string | undefined) => {
    const { skip } = r;
    if (!skip) return;
    await runToExit('skip', () => skip.run(reason, note));
    setAskSkipReason(false);
  };

  const confirmReject = async (reason: string, note: string | undefined) => {
    const { reject } = r;
    if (!reject) return;
    await runToExit('reject', () => reject.run(reason, note));
    setAskRejectReason(false);
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
    if (action === 'reject') {
      // The reason is asked for first; confirmReject does the reject.
      setAskRejectReason(true);
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
    if (action === 'refresh') {
      const { refresh } = r;
      if (!refresh) return;
      const go = async () => {
        setKeepSectionId(session.selectedSectionId);
        const ok = await session.run(
          'refresh',
          async () => {
            controller()?.discard();
            await refresh();
          },
          failed('refresh'),
        );
        if (ok) setRefreshToken((t) => t + 1);
      };
      // Unsaved edits are lost by a reload: ask first.
      if (controller()?.isDirty())
        discard.ask({
          header: pageMessages.refreshDiscardHeader,
          confirmLabel: pageMessages.refreshDiscardConfirm,
          onConfirm: () => void go(),
        });
      else void go();
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
    setSendBackError(null);
    setConfirmProcess(true);
  };

  const { header, bar, editor, history } = r;
  const sourceView = header.source && session.view === 'source' ? <FileViewer {...header.source} /> : undefined;
  // Why the fields can't be edited comes first, with the action that unlocks them.
  const readOnlyAction =
    bar.showClaim && r.claim ? (
      <Button loading={session.busy === 'claim'} disabled={session.busy !== null} onClick={() => void handleAction('claim')}>
        <FormattedMessage {...pageMessages.claim} />
      </Button>
    ) : (
      editor?.readOnlyAction
    );
  const notices = (
    <EditorNotices
      notices={[
        !!error && { id: 'error', type: 'error', content: error, onDismiss: () => setError(null) },
        !!editor?.readOnlyReason && { id: 'read-only', type: 'info', content: editor.readOnlyReason, action: readOnlyAction },
        ...r.notices,
      ]}
    />
  );

  return page(
    <EditScreen
      title={header.title}
      status={header.status}
      objectKey={header.objectKey}
      badge={header.badge}
      reportSectionData={header.offerReport ? session.sectionData : undefined}
      summaryReportUri={header.summary?.reportUri}
      summaryUnavailableReason={header.summary?.unavailableReason}
      ruleValidationReportUri={header.ruleValidation?.reportUri}
      ruleValidationUnavailableReason={header.ruleValidation?.unavailableReason}
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
          {discard.dialog}
          {r.process.confirm && r.process.confirm.kind !== 'review-completion' && confirmProcess && (
            <ProcessChangesConfirmModal
              visible
              variant={r.process.confirm.kind === 'version' ? 'version' : 'reprocess'}
              objectKey={header.objectKey}
              busy={session.busy === 'process'}
              onConfirm={() => void runProcess()}
              onCancel={() => setConfirmProcess(false)}
            />
          )}
          {r.process.confirm?.kind === 'review-completion' && confirmProcess && (
            <ReviewCompletionModal
              visible
              objectKey={r.process.confirm.objectKey}
              cycleId={r.process.confirm.cycleId}
              canSendBack={!!r.sendBack}
              busy={session.busy === 'process' ? 'complete' : session.busy === 'send-back' ? 'send-back' : null}
              error={sendBackError}
              onComplete={() => void runProcess()}
              onSendBack={(note) => void runSendBack(note)}
              onCancel={() => setConfirmProcess(false)}
            />
          )}
          {board.dialog}
          {askSkipReason && r.skip && (
            <ReasonModal
              visible
              header={r.skip.header}
              reasons={r.skip.reasons}
              confirmLabel={intl.formatMessage(skipMessage)}
              loading={session.busy === 'skip'}
              onDismiss={() => setAskSkipReason(false)}
              onConfirm={(reason, note) => void confirmSkip(reason, note)}
            >
              {r.skip.body}
            </ReasonModal>
          )}
          {askRejectReason && r.reject && (
            <ReasonModal
              visible
              header={r.reject.header}
              reasons={r.reject.reasons}
              confirmLabel={r.reject.confirmLabel}
              loading={session.busy === 'reject'}
              onDismiss={() => setAskRejectReason(false)}
              onConfirm={(reason, note) => void confirmReject(reason, note)}
            >
              {r.reject.body}
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
            isReadOnly={editor.readOnly || !!editor.readOnlyReason}
            loadSection={editor.loadSection}
            onSave={editor.saveSection}
            onSaved={editor.onSaved}
            onLoaded={editor.onLoaded}
            reloadToken={(editor.reloadToken ?? 0) + refreshToken}
            initialSectionId={keepSectionId ?? editor.initialSectionId}
            focusFieldPath={editor.focusFieldPath}
            focusRequest={session.focusRequest}
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
            regroup={
              bar.showRegroup
                ? {
                    disabledReason:
                      session.busy !== null ? intl.formatMessage(editScreenSharedMessages.working) : bar.regroupDisabledReason,
                    loading: session.busy === 'regroup',
                    onClick: () => void handleAction('regroup'),
                  }
                : undefined
            }
          />
        ))}
    </EditScreen>,
  );
};

export default EditPage;
