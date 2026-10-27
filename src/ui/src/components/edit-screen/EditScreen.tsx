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
// (Edit Page Class, Use As Evaluation Baseline); then Skip, Cancel, Save and Process
// Changes, the primary action, last (or Claim, for an Annotator viewing an unassigned
// item, with the view switch and Reports only). A button that does not apply is left out;
// a menu item the mode offers but that does not apply now is disabled, with the reason on
// hover. The mode components decide which actions apply; the layout is decided here.
import React, { useState } from 'react';
import { Box, Button, ButtonDropdown, Container, Header, Modal, SegmentedControl, SpaceBetween } from '@cloudscape-design/components';
import type { ButtonDropdownProps } from '@cloudscape-design/components';

import EditScreenLayout from './EditScreenLayout';
import { MarkdownReport } from '../document-viewer/MarkdownViewer';
import ProcessingReportContent from './ProcessingReportContent';
import type { EditView } from './use-edit-view';

export type EditAction = 'process' | 'skip' | 'claim' | 'cancel' | 'save' | 'regroup' | 'baseline';

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
   * after which it can be worked. Shown with the view switch and Reports only.
   */
  showClaim?: boolean;
  /** Offer Edit Page Class (the pages and class of each section), in More Actions. */
  showRegroup?: boolean;
  regroupDisabledReason?: string;
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

/** A menu item: available, or disabled with the reason on hover. */
const menuItem = (id: string, text: string, disabledReason?: string): ButtonDropdownProps.Item => ({
  id,
  text,
  disabled: !!disabledReason,
  disabledReason,
});

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
  const viewSwitch = view && onViewChange && (
    <SegmentedControl
      label="Edit the document or view its source"
      selectedId={view}
      onChange={({ detail }) => onViewChange(detail.selectedId as EditView)}
      options={[
        { id: 'edit', text: 'Edit' },
        { id: 'source', text: 'View' },
      ]}
    />
  );

  // Reports: the read-only views of the document. An item the mode offers but that does
  // not apply now stays in the menu, disabled with its reason.
  const reportItems: ButtonDropdownProps.Item[] = [
    ...(onShowHistory ? [menuItem('history', 'History')] : []),
    ...(onShowSummary || summaryDisabledReason ? [menuItem('summary', 'Summary', onShowSummary ? undefined : summaryDisabledReason)] : []),
    ...(onShowReport ? [menuItem('report', 'Processing Report')] : []),
  ];
  const reports = reportItems.length > 0 && (
    <ButtonDropdown
      items={reportItems}
      disabled={!!working}
      onItemClick={({ detail }) => {
        if (detail.id === 'history') onShowHistory?.();
        else if (detail.id === 'summary') onShowSummary?.();
        else if (detail.id === 'report') onShowReport?.();
      }}
    >
      Reports
    </ButtonDropdown>
  );

  if (showClaim) {
    return (
      <SpaceBetween direction="horizontal" size="xs" alignItems="center">
        {viewSwitch}
        {reports}
        <span title={working ? undefined : 'Assign this item to you, then work it here or from Home › Tasks'}>
          <Button variant="primary" disabled={!!working && busy !== 'claim'} loading={busy === 'claim'} onClick={() => onAction('claim')}>
            Claim
          </Button>
        </span>
      </SpaceBetween>
    );
  }

  // More Actions: the occasional changes (the sections' pages and classes) and the admin
  // action (the evaluation baseline).
  const moreItems: ButtonDropdownProps.Item[] = [
    ...(showRegroup ? [menuItem('regroup', 'Edit Page Class', regroupDisabledReason)] : []),
    ...(showBaseline ? [menuItem('baseline', 'Use As Evaluation Baseline', baselineDisabledReason)] : []),
  ];
  const more = moreItems.length > 0 && (
    <ButtonDropdown
      items={moreItems}
      disabled={!!working}
      loading={busy === 'regroup' || busy === 'baseline'}
      onItemClick={({ detail }) => onAction(detail.id as EditAction)}
    >
      More Actions
    </ButtonDropdown>
  );

  const skipReason = working ?? skipDisabledReason;
  const cancelReason = working ?? cancelDisabledReason;
  const saveReason = working ?? saveDisabledReason;
  const primaryReason = working ?? primaryDisabledReason;
  return (
    <SpaceBetween direction="horizontal" size="xs" alignItems="center">
      {viewSwitch}
      {reports}
      {more}
      {showSkip && (
        <span title={skipReason ? undefined : SKIP_HINT}>
          <Button disabled={!!skipReason} disabledReason={skipReason} loading={busy === 'skip'} onClick={() => onAction('skip')}>
            Skip
          </Button>
        </span>
      )}
      {showCancel && (
        <span title={cancelReason ? undefined : 'Discard your unsaved changes and leave'}>
          <Button disabled={!!cancelReason} disabledReason={cancelReason} onClick={() => onAction('cancel')}>
            Cancel
          </Button>
        </span>
      )}
      {showSave && (
        <Button disabled={!!saveReason} disabledReason={saveReason} loading={busy === 'save'} onClick={() => onAction('save')}>
          Save
        </Button>
      )}
      <span title={primaryReason ? undefined : primaryHint}>
        <Button
          variant="primary"
          disabled={!!primaryReason}
          disabledReason={primaryReason}
          loading={busy === 'process'}
          onClick={() => onAction('process')}
        >
          Process Changes
        </Button>
      </span>
    </SpaceBetween>
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

/** A report opened from the bar: Summary, Processing Report. */
const ReportModal = ({ header, onDismiss, children }: { header: string; onDismiss: () => void; children: React.ReactNode }) => (
  <Modal
    visible
    size="large"
    onDismiss={onDismiss}
    header={header}
    footer={
      <Box float="right">
        <Button variant="primary" onClick={onDismiss}>
          Close
        </Button>
      </Box>
    }
  >
    {children}
  </Modal>
);

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
          <Header
            variant="h1"
            description={<span title={objectKey}>{objectKey}</span>}
            actions={
              <EditActions
                {...actions}
                onShowReport={offerReport ? () => setShowReport(true) : undefined}
                onShowSummary={summaryReportUri ? () => setShowSummary(true) : undefined}
                summaryDisabledReason={summaryReportUri ? undefined : summaryUnavailableReason}
              />
            }
          >
            {title}
            {status && (
              <>
                {' '}
                {/* The status reads as the subtitle does (font, size and colour), not as the title. */}
                <Box variant="span" fontSize="body-m" fontWeight="normal" color="text-body-secondary">
                  ({status})
                </Box>
              </>
            )}
          </Header>
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
        <ReportModal header="Document Summary" onDismiss={() => setShowSummary(false)}>
          <MarkdownReport
            reportUri={summaryReportUri}
            documentId={objectKey}
            title="Document Summary"
            emptyMessage="Summary report not available for this document"
          />
        </ReportModal>
      )}
    </EditScreenLayout>
  );
};

export default EditScreen;
