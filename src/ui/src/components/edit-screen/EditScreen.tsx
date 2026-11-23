// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit screen, the same for a direct edit of a document, a production queue item
// and a Test Studio item: one action bar, the title "<document> (<status>)", the
// object key under it, the notices, the task context (a queue item's Review Reason)
// and the Document Editor panel.
//
// The bar is one row, left to right: Edit | View (the source document, where it can be
// shown); the Reports menu (History, Summary, Policy Rule Validation Summary, Processing
// Report); the More Actions menu (Use As Evaluation Baseline); Refresh; then Skip, Reject
// (a review item), Cancel, Save and
// Process Changes, the primary action, last (or Claim, for an Annotator viewing an
// unassigned item, with the view switch, Reports and Refresh only). A button that does not apply is left out;
// a menu item the mode offers but that does not apply now is disabled, with the reason on
// hover. The model decides which actions apply (see edit-model.ts and EditPage); the
// layout is decided here, and rendered through the shared ActionBar
// (common/ActionBar.tsx), which the document page's bar uses too. The labels stay in
// this file.
import React, { useState } from 'react';
import { Box, Container, Header, SpaceBetween } from '@cloudscape-design/components';
import { defineMessages, useIntl, type NoMessageValues } from 'react-intl';

import ActionBar from '../common/ActionBar';
import type { ActionBarControl } from '../common/ActionBar';
import DocumentPageHeader from '../common/DocumentPageHeader';
import { PROCESS_CHANGES_LABEL } from '../common/labels';
import { ReportModal, SummaryReportModal } from '../common/ReportModal';
import VersionBadge from '../common/VersionBadge';
import DocumentReportDialog from '../document-actions/DocumentReportDialog';
import EditScreenLayout from './EditScreenLayout';
import ProcessingReportContent from './ProcessingReportContent';
import type { EditView } from './use-edit-view';

export type EditAction = 'process' | 'skip' | 'reject' | 'send-back' | 'claim' | 'cancel' | 'save' | 'regroup' | 'baseline' | 'refresh';
/** The two endings of a review that only a Production review item offers. */
export type ReviewOutcomeAction = 'reject' | 'send-back';

const messages = defineMessages<{
  reject: NoMessageValues;
  rejectHint: NoMessageValues;
  policyRuleValidation: NoMessageValues;
}>({
  policyRuleValidation: {
    id: 'editScreen.reports.policyRuleValidation',
    defaultMessage: 'Policy Rule Validation Summary',
    description:
      "Item of the Reports menu on the edit screen: opens the document's policy rule validation summary, which lists the policy rules it passed and failed.",
  },
  reject: {
    id: 'editScreen.bar.reject',
    defaultMessage: 'Reject',
    description:
      'Button in the bar of the edit screen of a review item: end the review with the document rejected. It asks for a reason first.',
  },
  rejectHint: {
    id: 'editScreen.bar.rejectHint',
    defaultMessage: 'End the review and reject this document. A reason is asked for first.',
    description: 'Hover text of the Reject button of the edit screen while it is available.',
  },
});

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
  /** Policy Rule Validation Summary in Reports: available (opens it). EditScreen sets it. */
  onShowRuleValidation?: () => void;
  /** Policy Rule Validation Summary in Reports, disabled with this reason (a document with no such report). EditScreen sets it. */
  ruleValidationDisabledReason?: string;
  /** Offer Use As Evaluation Baseline, in More Actions: copy the finished document's results into the baseline. */
  showBaseline?: boolean;
  baselineDisabledReason?: string;
  /**
   * Offer Claim (an Annotator viewing an unassigned item): assigns the item to them,
   * after which it can be worked. Shown with the view switch, Reports and Refresh only.
   */
  showClaim?: boolean;
  /**
   * Edit Page Grouping (the pages and class of each section) is offered on the Document
   * Editor panel, not in this bar; EditPage passes these to it.
   */
  showRegroup?: boolean;
  regroupDisabledReason?: string;
  /** Offer Refresh: re-read the document (asking first when there are unsaved edits). */
  showRefresh?: boolean;
  refreshDisabledReason?: string;
  /** Offer Skip: give the item back to its queue, unassigned and not finished. */
  showSkip?: boolean;
  skipDisabledReason?: string;
  /** Offer Reject: end the review with the document rejected, for a reason from the queue's list. */
  showReject?: boolean;
  rejectDisabledReason?: string;
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
  onShowRuleValidation,
  ruleValidationDisabledReason,
  showBaseline = false,
  baselineDisabledReason,
  showClaim = false,
  showRefresh = false,
  refreshDisabledReason,
  showSkip = false,
  skipDisabledReason,
  showReject = false,
  rejectDisabledReason,
  showCancel = false,
  cancelDisabledReason,
  showSave = false,
  saveDisabledReason,
  primaryDisabledReason,
  primaryHint,
  view,
  onViewChange,
}: EditActionsProps): React.JSX.Element => {
  const intl = useIntl();
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
      ...(onShowRuleValidation || ruleValidationDisabledReason
        ? [
            {
              id: 'rule-validation',
              text: intl.formatMessage(messages.policyRuleValidation),
              disabledReason: onShowRuleValidation ? undefined : ruleValidationDisabledReason,
            },
          ]
        : []),
      ...(onShowReport ? [{ id: 'report', text: 'Processing Report' }] : []),
    ],
    disabled: !!working,
    onItemClick: (id) => {
      if (id === 'history') onShowHistory?.();
      else if (id === 'summary') onShowSummary?.();
      else if (id === 'rule-validation') onShowRuleValidation?.();
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

  // More Actions: the admin action (the evaluation baseline).
  const more: ActionBarControl = {
    kind: 'menu',
    id: 'more',
    text: 'More Actions',
    items: [...(showBaseline ? [{ id: 'baseline', text: 'Use As Evaluation Baseline', disabledReason: baselineDisabledReason }] : [])],
    disabled: !!working,
    loading: busy === 'baseline',
    onItemClick: (id) => onAction(id as EditAction),
  };

  const skipReason = working ?? skipDisabledReason;
  const rejectReason = working ?? rejectDisabledReason;
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
        showReject && {
          kind: 'button',
          id: 'reject',
          text: intl.formatMessage(messages.reject),
          disabledReason: rejectReason,
          loading: busy === 'reject',
          hint: { title: rejectReason ? undefined : intl.formatMessage(messages.rejectHint) },
          onClick: () => onAction('reject'),
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
          text: PROCESS_CHANGES_LABEL,
          variant: 'primary',
          disabledReason: primaryReason,
          // A send-back is one of the two ways Process Changes ends a review.
          loading: busy === 'process' || busy === 'send-back',
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
  /** After the status: the open test-set version's `vN → vM` badge, with its hover text. */
  badge?: { text: string; hover: string };
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
  /**
   * The document's policy rule validation report, for Policy Rule Validation Summary.
   * Passed where the document has one; omitted, the item is disabled or absent (below).
   */
  ruleValidationReportUri?: string;
  /**
   * Policy Rule Validation Summary offered but unavailable: why (a document with no such
   * report). Ignored when `ruleValidationReportUri` is given; both omitted, there is no
   * such item.
   */
  ruleValidationUnavailableReason?: string;
  /** The Document Editor panel. */
  children: React.ReactNode;
}

const EditScreen = ({
  title,
  status,
  objectKey,
  badge,
  actions,
  notices,
  context,
  dialogs,
  reportSectionData,
  summaryReportUri,
  summaryUnavailableReason,
  ruleValidationReportUri,
  ruleValidationUnavailableReason,
  children,
}: EditScreenProps): React.JSX.Element => {
  const [showReport, setShowReport] = useState(false);
  const [showSummary, setShowSummary] = useState(false);
  const [showRuleValidation, setShowRuleValidation] = useState(false);
  const offerReport = reportSectionData !== undefined;
  return (
    <EditScreenLayout
      header={
        <SpaceBetween size="s">
          <DocumentPageHeader
            title={title}
            status={status}
            objectKey={objectKey}
            badge={badge && <VersionBadge text={badge.text} hover={badge.hover} />}
            actions={
              <EditActions
                {...actions}
                onShowReport={offerReport ? () => setShowReport(true) : undefined}
                onShowSummary={summaryReportUri ? () => setShowSummary(true) : undefined}
                summaryDisabledReason={summaryReportUri ? undefined : summaryUnavailableReason}
                onShowRuleValidation={ruleValidationReportUri ? () => setShowRuleValidation(true) : undefined}
                ruleValidationDisabledReason={ruleValidationReportUri ? undefined : ruleValidationUnavailableReason}
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
      {showRuleValidation && ruleValidationReportUri && (
        <DocumentReportDialog
          report="rule-validation"
          displayed={{ objectKey, ruleValidationResultUri: ruleValidationReportUri }}
          onDismiss={() => setShowRuleValidation(false)}
        />
      )}
    </EditScreenLayout>
  );
};

export default EditScreen;
