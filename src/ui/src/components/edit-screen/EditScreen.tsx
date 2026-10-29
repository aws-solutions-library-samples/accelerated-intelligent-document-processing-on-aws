// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit screen, the same for a direct edit of a document, a production queue item
// and a Test Studio item: one action bar, the title "<document> (<status>)", the
// object key under it, the notices, the task context (a queue item's Review Reason)
// and the Document Editor panel.
//
// The bar is one row, left to right: Edit | View (the source document, where it can be
// shown); the Reports menu (History, Summary, Processing Report); the More Actions menu
// (Edit Page Class, Use As Evaluation Baseline); Refresh; then Skip, Cancel, Save and
// Process Changes, the primary action, last (or Claim, for an Annotator viewing an
// unassigned item, with the view switch, Reports and Refresh only). A button that does not apply is left out;
// a menu item the mode offers but that does not apply now is disabled, with the reason on
// hover. The model decides which actions apply (see edit-model.ts and EditPage); the
// layout is decided here, and rendered through the shared ActionBar
// (common/ActionBar.tsx), which the document page's bar uses too. The labels stay in
// this file.
import React, { useState } from 'react';
import { Box, Container, Header, SpaceBetween } from '@cloudscape-design/components';

import ActionBar from '../common/ActionBar';
import type { ActionBarControl } from '../common/ActionBar';
import DocumentPageHeader from '../common/DocumentPageHeader';
import { ReportModal, SummaryReportModal } from '../common/ReportModal';
import EditScreenLayout from './EditScreenLayout';
import ProcessingReportContent from './ProcessingReportContent';
import type { EditView } from './use-edit-view';

export type EditAction = 'process' | 'skip' | 'claim' | 'cancel' | 'save' | 'regroup' | 'baseline' | 'refresh';

/**
 * Accessible name of Cancel. It says what Cancel does to unsaved edits, and it is the
 * marker the UX test's commit guard reads as "this editor may save its own edits"
 * (`AUTOSAVE_CONTROLS` in `scripts/ux_runner/story_relay.py`), so keep the two in step.
 */
export const CANCEL_ACCESSIBLE_NAME = 'Cancel and discard changes';

/** Hover text of Skip while it is available. */
export const SKIP_HINT = 'Give this item back to its queue, unassigned and not finished.';

export interface EditActionsProps {
  /** The action running now; every button and menu is disabled meanwhile. */
  busy: EditAction | null;
  onAction: (action: EditAction) => void;
  /** Offer History (field edits and review activity), in Reports. */
  onShowHistory?: () => void;
  /** Offer Processing Report (how the open section was extracted), in Reports. EditScreen sets it. */
  onShowReport?: () => void;
  /** Summary in Reports: available (opens it). EditScreen sets it. */
  onShowSummary?: () => void;
  /** Summary in Reports, disabled with this reason (a document with no summary yet). EditScreen sets it. */
  summaryDisabledReason?: string;
  /** Offer Use As Evaluation Baseline, in More Actions: copy the finished document's results into the baseline. */
  showBaseline?: boolean;
  baselineDisabledReason?: string;
  /**
   * Offer Claim (an Annotator viewing an unassigned item): assigns the item to them,
   * after which it can be worked. Shown with the view switch, Reports and Refresh only.
   */
  showClaim?: boolean;
  /** Offer Edit Page Class (the pages and class of each section), in More Actions. */
  showRegroup?: boolean;
  regroupDisabledReason?: string;
  /** Offer Refresh: re-read the document (asking first when there are unsaved edits). */
  showRefresh?: boolean;
  refreshDisabledReason?: string;
  /** Offer Skip: give the item back to its queue, unassigned and not finished. */
  showSkip?: boolean;
  skipDisabledReason?: string;
  /** Offer Cancel: discard the unsaved edits and leave the screen. */
  showCancel?: boolean;
  cancelDisabledReason?: string;
  /** Offer Save: save the edits without finishing. */
  showSave?: boolean;
  saveDisabledReason?: string;
  /** Why the primary action is unavailable; undefined when it is available. */
  primaryDisabledReason?: string;
  /** Hover text while the primary action is available. */
  primaryHint?: string;
  /**
   * Offer the Edit | View switch (the source document), first in the bar: `view` is the one
   * shown. Omitted where the source document cannot be shown.
   */
  view?: EditView;
  onViewChange?: (view: EditView) => void;
}

const EditActions = ({
  busy,
  onAction,
  onShowHistory,
  onShowReport,
  onShowSummary,
  summaryDisabledReason,
  showBaseline = false,
  baselineDisabledReason,
  showClaim = false,
  showRegroup = false,
  regroupDisabledReason,
  showRefresh = false,
  refreshDisabledReason,
  showSkip = false,
  skipDisabledReason,
  showCancel = false,
  cancelDisabledReason,
  showSave = false,
  saveDisabledReason,
  primaryDisabledReason,
  primaryHint,
  view,
  onViewChange,
}: EditActionsProps): React.JSX.Element => {
  const working = busy !== null ? 'Working…' : undefined;
  const viewSwitch: ActionBarControl | false = !!view &&
    !!onViewChange && {
      kind: 'switch',
      id: 'view',
      label: 'Edit the document or view its source',
      selectedId: view,
      onChange: (id) => onViewChange(id as EditView),
      options: [
        { id: 'edit', text: 'Edit' },
        { id: 'source', text: 'View' },
      ],
    };

  // Reports: the read-only views of the document. An item the mode offers but that does
  // not apply now stays in the menu, disabled with its reason.
  const reports: ActionBarControl = {
    kind: 'menu',
    id: 'reports',
    text: 'Reports',
    items: [
      ...(onShowHistory ? [{ id: 'history', text: 'History' }] : []),
      ...(onShowSummary || summaryDisabledReason
        ? [{ id: 'summary', text: 'Summary', disabledReason: onShowSummary ? undefined : summaryDisabledReason }]
        : []),
      ...(onShowReport ? [{ id: 'report', text: 'Processing Report' }] : []),
    ],
    disabled: !!working,
    onItemClick: (id) => {
      if (id === 'history') onShowHistory?.();
      else if (id === 'summary') onShowSummary?.();
      else if (id === 'report') onShowReport?.();
    },
  };

  const refresh: ActionBarControl | false = showRefresh && {
    kind: 'button',
    id: 'refresh',
    text: 'Refresh',
    iconName: 'refresh',
    disabledReason: working ?? refreshDisabledReason,
    loading: busy === 'refresh',
    onClick: () => onAction('refresh'),
  };

  if (showClaim) {
    return (
      <ActionBar
        controls={[
          viewSwitch,
          reports,
          refresh,
          {
            kind: 'button',
            id: 'claim',
            text: 'Claim',
            variant: 'primary',
            disabled: !!working && busy !== 'claim',
            loading: busy === 'claim',
            hint: { title: working ? undefined : 'Assign this item to you, then work it here or from Home › Tasks' },
            onClick: () => onAction('claim'),
          },
        ]}
      />
    );
  }

  // More Actions: the occasional changes (the sections' pages and classes) and the admin
  // action (the evaluation baseline).
  const more: ActionBarControl = {
    kind: 'menu',
    id: 'more',
    text: 'More Actions',
    items: [
      ...(showRegroup ? [{ id: 'regroup', text: 'Edit Page Class', disabledReason: regroupDisabledReason }] : []),
      ...(showBaseline ? [{ id: 'baseline', text: 'Use As Evaluation Baseline', disabledReason: baselineDisabledReason }] : []),
    ],
    disabled: !!working,
    loading: busy === 'regroup' || busy === 'baseline',
    onItemClick: (id) => onAction(id as EditAction),
  };

  const skipReason = working ?? skipDisabledReason;
  const cancelReason = working ?? cancelDisabledReason;
  const saveReason = working ?? saveDisabledReason;
  const primaryReason = working ?? primaryDisabledReason;
  return (
    <ActionBar
      controls={[
        viewSwitch,
        reports,
        more,
        refresh,
        showSkip && {
          kind: 'button',
          id: 'skip',
          text: 'Skip',
          disabledReason: skipReason,
          loading: busy === 'skip',
          hint: { title: skipReason ? undefined : SKIP_HINT },
          onClick: () => onAction('skip'),
        },
        showCancel && {
          kind: 'button',
          id: 'cancel',
          text: 'Cancel',
          ariaLabel: CANCEL_ACCESSIBLE_NAME,
          disabledReason: cancelReason,
          hint: { title: cancelReason ? undefined : 'Discard your unsaved changes and leave' },
          onClick: () => onAction('cancel'),
        },
        showSave && {
          kind: 'button',
          id: 'save',
          text: 'Save',
          disabledReason: saveReason,
          loading: busy === 'save',
          onClick: () => onAction('save'),
        },
        {
          kind: 'button',
          id: 'process',
          text: 'Process Changes',
          variant: 'primary',
          disabledReason: primaryReason,
          loading: busy === 'process',
          hint: { title: primaryReason ? undefined : primaryHint },
          onClick: () => onAction('process'),
        },
      ]}
    />
  );
};

/**
 * The Document Editor panel when there is nothing to edit, titled as it is otherwise:
 * `body` (the source document) when it is being viewed, or the message.
 */
export const EditDocumentEmpty = ({ children, body }: { children: React.ReactNode; body?: React.ReactNode }): React.JSX.Element => (
  <Container header={<Header variant="h2">Document Editor</Header>}>{body ?? <Box color="text-body-secondary">{children}</Box>}</Container>
);

interface EditScreenProps {
  /** The document's name. */
  title: string;
  /** Shown after the title in parentheses, as text: the item's or the document's status. */
  status?: string | null;
  /** The full object key, shown under the title. */
  objectKey: string;
  actions: EditActionsProps;
  /** Alerts about the item's state (one EditorNotices). */
  notices?: React.ReactNode;
  /** The task context: a queue item's Review Reason. */
  context?: React.ReactNode;
  /** Dialogs the actions open (the skip reason, History, page grouping). */
  dialogs?: React.ReactNode;
  /**
   * The open section's result, for Processing Report; null while no section is open.
   * Omitted, the bar has no Processing Report.
   */
  reportSectionData?: Record<string, unknown> | null;
  /**
   * The document's summary report, for Summary. Passed only where the document has a
   * summary to show (a finished document); omitted, the bar has no Summary.
   */
  summaryReportUri?: string;
  /**
   * Summary offered but unavailable: why (a document still processing, or a review not yet
   * complete). Ignored when `summaryReportUri` is given; both omitted, there is no Summary.
   */
  summaryUnavailableReason?: string;
  /** The Document Editor panel. */
  children: React.ReactNode;
}

const EditScreen = ({
  title,
  status,
  objectKey,
  actions,
  notices,
  context,
  dialogs,
  reportSectionData,
  summaryReportUri,
  summaryUnavailableReason,
  children,
}: EditScreenProps): React.JSX.Element => {
  const [showReport, setShowReport] = useState(false);
  const [showSummary, setShowSummary] = useState(false);
  const offerReport = reportSectionData !== undefined;
  return (
    <EditScreenLayout
      header={
        <SpaceBetween size="s">
          <DocumentPageHeader
            title={title}
            status={status}
            objectKey={objectKey}
            actions={
              <EditActions
                {...actions}
                onShowReport={offerReport ? () => setShowReport(true) : undefined}
                onShowSummary={summaryReportUri ? () => setShowSummary(true) : undefined}
                summaryDisabledReason={summaryReportUri ? undefined : summaryUnavailableReason}
              />
            }
          />
          {notices}
        </SpaceBetween>
      }
      context={context}
    >
      {children}
      {dialogs}
      {/* Mounted only while open, so their buttons are not on the screen otherwise. */}
      {showReport && offerReport && (
        <ReportModal header="Processing Report" onDismiss={() => setShowReport(false)}>
          <ProcessingReportContent sectionData={reportSectionData ?? null} />
        </ReportModal>
      )}
      {showSummary && summaryReportUri && (
        <SummaryReportModal reportUri={summaryReportUri} objectKey={objectKey} onDismiss={() => setShowSummary(false)} />
      )}
    </EditScreenLayout>
  );
};

export default EditScreen;
