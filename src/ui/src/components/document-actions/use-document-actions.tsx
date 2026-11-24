// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The document page's action bar and the dialogs it opens. Left to right: the Details |
// Source switch; Reports (Summary, Evaluation Report, Rule Validation Summary,
// Estimated Cost, Processing Flow); More Actions (the changes; then the downloads, as a group; then
// Reprocess, Abort and Delete as a group, Delete last); Refresh; Troubleshoot; Start
// Review, where it applies; and Edit, the primary action. More Actions opens Edit Page Grouping (the edit
// page's board, with Save and Re-extract), Re-extract Sections, Edit Page Text and Reset
// Page Class. Which actions are offered and why one is
// disabled come from documentActionGates (document-action-gates.ts).
//
// It reads no configuration and no route itself: the page passes them in.
import React, { useEffect, useRef, useState } from 'react';
import { Alert, Box, Button, Checkbox, SpaceBetween, StatusIndicator } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';

import { generateClient } from '../../api/client-shim';
import useAppContext from '../../contexts/app';
import { parseHITLReviewHistory } from '../../graphql/awsjson-parsers';
import { claimReview, skipAllSectionsReview } from '../../graphql/generated';
import useUserRole from '../../hooks/use-user-role';
import { documentEditHref } from '../../routes/constants';
import { getErrorMessage } from '../../utils/errorUtils';
import { isBdaModeConfig } from '../../utils/processing-mode';
import type { ActionBarControl, ActionBarMenu, ActionBarMenuItem } from '../common/ActionBar';
import { copyDocumentToBaseline } from '../common/copy-to-baseline';
import ReasonModal from '../common/ReasonModal';
import { SKIP_ALL_REASONS } from '../common/review-status';
import TroubleshootModal from '../document-panel/TroubleshootModal';
import type { EditorNotice } from '../edit-screen/EditorNotices';
import usePageClassBoard from '../edit-screen/use-page-class-board';
import { documentName } from '../review-queues/review-api';
import SendToReviewModal from '../review-bulk/SendToReviewModal';
import useReviewActionSettings from '../review-bulk/use-review-action-settings';
import { outcomeView } from '../review-bulk/bulk-outcomes';
import StepFunctionFlowViewer from '../step-function-flow/StepFunctionFlowViewer';
import EstimatedCostModal from '../document-panel/EstimatedCostModal';
import DocumentReportDialog from './DocumentReportDialog';
import EditPageTextDialog, { reprocessPageText } from './EditPageTextDialog';
import ReextractSectionsDialog from './ReextractSectionsDialog';
import ResetPageClassDialog from './ResetPageClassDialog';
import useDetailPageClassBoard from './use-detail-page-class-board';
import type { DocumentReport } from './DocumentReportDialog';
import { documentActionGates } from './document-action-gates';
import type { ActionGate, DocumentActionGates } from './document-action-gates';

const client = generateClient();
const logger = new ConsoleLogger('DocumentActions');

const messages = defineMessages<{
  sendToReview: NoMessageValues;
  resubmitLabel: NoMessageValues;
  resubmitDescription: NoMessageValues;
  sentToReview: { readonly name: string };
  sendToReviewFailed: { readonly name: string; readonly outcome: string };
  viewSwitch: NoMessageValues;
  details: NoMessageValues;
  source: NoMessageValues;
  reports: NoMessageValues;
  moreActions: NoMessageValues;
  refresh: NoMessageValues;
  troubleshoot: NoMessageValues;
  startReview: NoMessageValues;
  edit: NoMessageValues;
  editInQueue: NoMessageValues;
  summary: NoMessageValues;
  evaluationReport: NoMessageValues;
  ruleValidationSummary: NoMessageValues;
  estimatedCost: NoMessageValues;
  processingFlow: NoMessageValues;
  editPageGrouping: NoMessageValues;
  reextractSections: NoMessageValues;
  editPageText: NoMessageValues;
  resetPageClass: NoMessageValues;
  useAsBaseline: NoMessageValues;
  skipAllReviews: NoMessageValues;
  reprocess: NoMessageValues;
  abort: NoMessageValues;
  deleteDocument: NoMessageValues;
  baselineCopying: NoMessageValues;
  baselineAvailable: NoMessageValues;
  baselineFailed: NoMessageValues;
  baselineNotApplicable: NoMessageValues;
  baselineCopyStarted: { readonly name: string };
  baselineUseFailed: { readonly name: string; readonly error: string };
  baselineCopyFailed: { readonly name: string };
  reviewClaimed: { readonly name: string };
  reviewClaimFailed: { readonly error: string };
  skipAllHeader: NoMessageValues;
  skipAllConfirm: NoMessageValues;
  skipAllWarning: NoMessageValues;
  skipAllIntro: NoMessageValues;
  skipAllMarkSkipped: NoMessageValues;
  skipAllRecorded: NoMessageValues;
  skipAllQuestion: NoMessageValues;
  skippedAll: { readonly name: string };
  skipAllFailed: { readonly error: string };
  pageTextSaved: { readonly pageId: string; readonly name: string };
  pageTextReprocessFailed: { readonly name: string; readonly error: string };
  pageTextUnfinished: { readonly pageId: string; readonly name: string };
  pageTextRetry: NoMessageValues;
}>({
  sendToReview: {
    id: 'documentActions.moreActions.sendToReview',
    defaultMessage: 'Send to review',
    description: "Item of the document page's More Actions menu that sends a finished document to a review queue.",
  },
  resubmitLabel: {
    id: 'documentActions.skipAllReviews.resubmitLabel',
    defaultMessage: 'Resubmit for downstream processing (Summarization, Evaluation, post-processing hook)',
    description: 'Checkbox label in the Skip All Section Reviews dialog: whether skipping re-runs the downstream steps.',
  },
  resubmitDescription: {
    id: 'documentActions.skipAllReviews.resubmitDescription',
    defaultMessage: 'Re-runs Summarization and Evaluation on the unedited results and fires the post-processing hook.',
    description: 'Help text under the Resubmit checkbox in the Skip All Section Reviews dialog.',
  },
  sentToReview: {
    id: 'documentActions.sendToReview.sent',
    defaultMessage: 'Sent {name} to review.',
    description: 'Success notification after Send to review. {name} is the document name.',
  },
  sendToReviewFailed: {
    id: 'documentActions.sendToReview.failed',
    defaultMessage: 'Could not send {name} to review: {outcome}.',
    description:
      'Error notification when Send to review did not send the document. {name} is the document name; {outcome} is the reason shown for the outcome, e.g. "Not permitted".',
  },
  viewSwitch: {
    id: 'documentActions.bar.viewSwitch',
    defaultMessage: "Show the document's details or its source",
    description: "Screen-reader name of the Details | Source switch at the start of the document page's action bar.",
  },
  details: {
    id: 'documentActions.bar.details',
    defaultMessage: 'Details',
    description: "Option of the document page's view switch: shows the document's details, sections and pages.",
  },
  source: {
    id: 'documentActions.bar.source',
    defaultMessage: 'Source',
    description: "Option of the document page's view switch: shows the source document in place of its details.",
  },
  reports: {
    id: 'documentActions.bar.reports',
    defaultMessage: 'Reports',
    description: "Menu of the document page's action bar that opens the document's reports.",
  },
  moreActions: {
    id: 'documentActions.bar.moreActions',
    defaultMessage: 'More Actions',
    description: "Menu of the document page's action bar with the changes, the downloads, and Reprocess, Abort and Delete.",
  },
  refresh: {
    id: 'documentActions.bar.refresh',
    defaultMessage: 'Refresh',
    description: "Button of the document page's action bar that reads the document again.",
  },
  troubleshoot: {
    id: 'documentActions.bar.troubleshoot',
    defaultMessage: 'Troubleshoot',
    description: "Button of the document page's action bar that opens Troubleshoot, where an agent looks into the document's processing.",
  },
  startReview: {
    id: 'documentActions.bar.startReview',
    defaultMessage: 'Start Review',
    description: "Button of the document page's action bar with which a reviewer takes the document's pending review.",
  },
  edit: {
    id: 'documentActions.bar.edit',
    defaultMessage: 'Edit',
    description: "Primary button of the document page's action bar: opens the document's edit page.",
  },
  editInQueue: {
    id: 'documentActions.bar.editInQueue',
    defaultMessage: 'Edit this document in its review queue',
    description: 'Screen-reader name of the Edit button while the document is in a review queue, where Edit opens its queue item.',
  },
  summary: {
    id: 'documentActions.reports.summary',
    defaultMessage: 'Summary',
    description: "Item of the document page's Reports menu: opens the document's summary.",
  },
  evaluationReport: {
    id: 'documentActions.reports.evaluationReport',
    defaultMessage: 'Evaluation Report',
    description: "Item of the document page's Reports menu: opens the document's evaluation report.",
  },
  ruleValidationSummary: {
    id: 'documentActions.reports.ruleValidationSummary',
    defaultMessage: 'Rule Validation Summary',
    description: "Item of the document page's Reports menu: opens the document's rule validation summary.",
  },
  estimatedCost: {
    id: 'documentActions.reports.estimatedCost',
    defaultMessage: 'Estimated Cost',
    description: "Item of the document page's Reports menu: opens the estimated cost of processing the document.",
  },
  processingFlow: {
    id: 'documentActions.reports.processingFlow',
    defaultMessage: 'Processing Flow',
    description: "Item of the document page's Reports menu: opens the diagram of the document's processing workflow.",
  },
  editPageGrouping: {
    id: 'documentActions.moreActions.editPageGrouping',
    defaultMessage: 'Edit Page Grouping',
    description: "Item of the document page's More Actions menu: opens the board where the document's pages are regrouped into sections.",
  },
  reextractSections: {
    id: 'documentActions.moreActions.reextractSections',
    defaultMessage: 'Re-extract Sections',
    description: "Item of the document page's More Actions menu: opens the dialog that extracts chosen sections again.",
  },
  editPageText: {
    id: 'documentActions.moreActions.editPageText',
    defaultMessage: 'Edit Page Text',
    description: "Item of the document page's More Actions menu: opens the dialog that edits a page's text and reprocesses it.",
  },
  resetPageClass: {
    id: 'documentActions.moreActions.resetPageClass',
    defaultMessage: 'Reset Page Class',
    description: "Item of the document page's More Actions menu: opens the dialog that classifies chosen pages again.",
  },
  useAsBaseline: {
    id: 'documentActions.moreActions.useAsBaseline',
    defaultMessage: 'Use As Evaluation Baseline',
    description: "Item of the document page's More Actions menu: copies the document's results into the evaluation baseline.",
  },
  skipAllReviews: {
    id: 'documentActions.moreActions.skipAllReviews',
    defaultMessage: 'Skip All Reviews',
    description: "Item of the document page's More Actions menu: asks why, then skips all of the document's pending section reviews.",
  },
  reprocess: {
    id: 'documentActions.moreActions.reprocess',
    defaultMessage: 'Reprocess',
    description: "Item of the document page's More Actions menu: processes the document again.",
  },
  abort: {
    id: 'documentActions.moreActions.abort',
    defaultMessage: 'Abort',
    description: "Item of the document page's More Actions menu: stops the document's processing.",
  },
  deleteDocument: {
    id: 'documentActions.moreActions.delete',
    defaultMessage: 'Delete',
    description: "Item of the document page's More Actions menu, the last one: deletes the document.",
  },
  baselineCopying: {
    id: 'documentActions.baseline.copying',
    defaultMessage: 'Baseline copying in progress',
    description: "Value of the Evaluation field of Document Details while the document's results are copied to the evaluation baseline.",
  },
  baselineAvailable: {
    id: 'documentActions.baseline.available',
    defaultMessage: 'Baseline available',
    description: "Value of the Evaluation field of Document Details once the document's results are in the evaluation baseline.",
  },
  baselineFailed: {
    id: 'documentActions.baseline.failed',
    defaultMessage: 'Baseline copy failed',
    description:
      "Value of the Evaluation field of Document Details when the copy of the document's results to the evaluation baseline failed.",
  },
  baselineNotApplicable: {
    id: 'documentActions.baseline.notApplicable',
    defaultMessage: 'N/A',
    description: 'Value of the Evaluation field of Document Details when the document has no evaluation status.',
  },
  baselineCopyStarted: {
    id: 'documentActions.baseline.copyStarted',
    defaultMessage: 'Copying {name} to the evaluation baseline. Its evaluation status shows when the copy is done.',
    description: 'Success notification after Use As Evaluation Baseline started the copy. {name} is the document name.',
  },
  baselineUseFailed: {
    id: 'documentActions.baseline.useFailed',
    defaultMessage: 'Could not use {name} as the evaluation baseline: {error}',
    description:
      "Error notification when Use As Evaluation Baseline could not start the copy. {name} is the document name; {error} is the server's reason.",
  },
  baselineCopyFailed: {
    id: 'documentActions.baseline.copyFailed',
    defaultMessage: 'The copy of {name} to the evaluation baseline failed.',
    description:
      'Error notification when a running copy of the document to the evaluation baseline ended in an error. {name} is the document name.',
  },
  reviewClaimed: {
    id: 'documentActions.startReview.claimed',
    defaultMessage: 'You now hold the review of {name}. Choose Edit to work on it.',
    description: 'Success notification after Start Review gave the user the review of the document. {name} is the document name.',
  },
  reviewClaimFailed: {
    id: 'documentActions.startReview.failed',
    defaultMessage: 'Failed to start review: {error}',
    description: "Error notification when Start Review did not give the user the review. {error} is the server's reason.",
  },
  skipAllHeader: {
    id: 'documentActions.skipAllReviews.header',
    defaultMessage: 'Skip All Section Reviews',
    description: "Title of the dialog that asks why before Skip All Reviews skips all of the document's pending section reviews.",
  },
  skipAllConfirm: {
    id: 'documentActions.skipAllReviews.confirm',
    defaultMessage: 'Skip All Reviews',
    description: 'Confirm button of the Skip All Section Reviews dialog.',
  },
  skipAllWarning: {
    id: 'documentActions.skipAllReviews.warning',
    defaultMessage: 'This action will skip all pending section reviews and continue the document processing workflow.',
    description: 'Warning at the top of the Skip All Section Reviews dialog.',
  },
  skipAllIntro: {
    id: 'documentActions.skipAllReviews.intro',
    defaultMessage: 'Skipping review will:',
    description: 'Lead-in of the list of what skipping does, in the Skip All Section Reviews dialog.',
  },
  skipAllMarkSkipped: {
    id: 'documentActions.skipAllReviews.markSkipped',
    defaultMessage: 'Mark all pending sections as review skipped without human verification',
    description: 'First item of the list of what skipping does, in the Skip All Section Reviews dialog.',
  },
  skipAllRecorded: {
    id: 'documentActions.skipAllReviews.recorded',
    defaultMessage: 'Record this action, with your reason, in the review history',
    description: 'Second item of the list of what skipping does, in the Skip All Section Reviews dialog.',
  },
  skipAllQuestion: {
    id: 'documentActions.skipAllReviews.question',
    defaultMessage: 'Are you sure you want to skip all section reviews?',
    description: 'Question above the reason in the Skip All Section Reviews dialog.',
  },
  skippedAll: {
    id: 'documentActions.skipAllReviews.skipped',
    defaultMessage: 'Skipped the review of {name}.',
    description: "Success notification after Skip All Reviews skipped the document's pending section reviews. {name} is the document name.",
  },
  skipAllFailed: {
    id: 'documentActions.skipAllReviews.failed',
    defaultMessage: 'Failed to skip all sections: {error}',
    description: "Error notification when Skip All Reviews did not skip the reviews. {error} is the server's reason.",
  },
  pageTextSaved: {
    id: 'documentActions.unfinishedPageText.saved',
    defaultMessage: "Saved page {pageId}'s text of {name}. The sections that hold it are being extracted again.",
    description:
      "Success notification after Retry reprocess reprocessed a page whose edited text was saved earlier. {pageId} is the page's number; {name} is the document name.",
  },
  pageTextReprocessFailed: {
    id: 'documentActions.unfinishedPageText.reprocessFailed',
    defaultMessage: 'Could not reprocess {name}: {error}',
    description: "Error notification when Retry reprocess failed again. {name} is the document name; {error} is the server's reason.",
  },
  pageTextUnfinished: {
    id: 'documentActions.unfinishedPageText.notice',
    defaultMessage: "Page {pageId}'s text was saved, but {name} was not reprocessed.",
    description:
      "Error notice on the document page when a page's edited text was saved but reprocessing the document failed. {pageId} is the page's number; {name} is the document name.",
  },
  pageTextRetry: {
    id: 'documentActions.unfinishedPageText.retry',
    defaultMessage: 'Retry reprocess',
    description: 'Button of the notice that a saved page text was not reprocessed: reprocesses the document again.',
  },
});

export type DocumentView = 'details' | 'source';

/** The document fields the actions read (the page's mapped document). */
export interface ActionDocument {
  objectKey: string;
  objectStatus?: string;
  hitlStatus?: string;
  hitlTriggered?: boolean;
  hitlReviewOwner?: string;
  hitlReviewOwnerEmail?: string;
  evaluationStatus?: string;
  summaryReportUri?: string;
  evaluationReportUri?: string;
  ruleValidationResultUri?: string;
  executionArn?: string;
  sections?: Record<string, unknown>[];
  pages?: Record<string, unknown>[];
  [key: string]: unknown;
}

interface TroubleshootJob {
  jobId: string;
  status: string | null;
  result: string | Record<string, unknown> | null;
  agentMessages: unknown;
  error: string | null;
  timestamp: number;
  documentKey: string | undefined;
}

export interface DocumentActionsInput {
  /** The live document. */
  item: ActionDocument;
  /** What the page shows: the live document, or a past version. */
  displayed: ActionDocument;
  isHistorical: boolean;
  /** The configuration the document was processed with. */
  mergedConfig: Record<string, unknown> | null | undefined;
  configLoading: boolean;
  view: DocumentView;
  onViewChange: (view: DocumentView) => void;
  /** Re-read the document (and its sections and pages). */
  refresh: () => Promise<unknown>;
  /** Apply a change to the page's copy of the document at once (a server response). */
  onDocumentUpdate: (update: (prev: ActionDocument) => ActionDocument) => void;
  /** The downloads (useDocumentDownload), a group in More Actions. */
  download: ActionBarMenu;
  onReprocess: () => void;
  onAbort: () => void;
  onDelete: () => void;
}

export interface DocumentActions {
  controls: ActionBarControl[];
  dialogs: React.ReactNode;
  notices: EditorNotice[];
  /** The Evaluation field of Document Details, with the baseline copy's state. */
  evaluationStatusCell: React.ReactNode;
  gates: DocumentActionGates;
  /** Edit Page Text for a page's row menu; null when the user is not offered it. */
  pageTextEdit: { disabledReason?: string; open: (pageId: string) => void } | null;
}

type Busy = 'refresh' | 'claim' | 'baseline' | 'skip-all' | null;

/** A menu item for an offered action, disabled with its reason; nothing when it is not offered. */
const offered = (gate: ActionGate, id: string, text: string): ActionBarMenuItem[] =>
  gate.shown ? [{ id, text, disabledReason: gate.disabledReason }] : [];

export const useDocumentActions = (input: DocumentActionsInput): DocumentActions => {
  const { item, displayed, isHistorical, mergedConfig, configLoading, view, onViewChange, refresh, onDocumentUpdate, download } = input;
  const intl = useIntl();
  const roles = useUserRole();
  const { user, setErrorMessage, setSuccessMessage } = useAppContext();
  const [busy, setBusy] = useState<Busy>(null);
  const [baselineRequestInFlight, setBaselineRequestInFlight] = useState(false);
  const [report, setReport] = useState<DocumentReport | null>(null);
  const [flowOpen, setFlowOpen] = useState(false);
  const [costOpen, setCostOpen] = useState(false);
  const [troubleshootOpen, setTroubleshootOpen] = useState(false);
  // The last Troubleshoot job per document, for this page visit: reopening shows it.
  const [troubleshootJobs, setTroubleshootJobs] = useState<Record<string, TroubleshootJob>>({});
  const [askSkipReason, setAskSkipReason] = useState(false);
  const [skipResubmit, setSkipResubmit] = useState(true);
  const [askSendToReview, setAskSendToReview] = useState(false);
  const reviewSettings = useReviewActionSettings();
  const [changeDialog, setChangeDialog] = useState<'reextract' | 'reset-class' | null>(null);
  // Edit Page Text: the page it is open at; and a page whose text was saved but not reprocessed.
  const [pageTextAt, setPageTextAt] = useState<string | null>(null);
  const [unfinishedText, setUnfinishedText] = useState<{ pageId: string; textUri: string } | null>(null);
  const [retryingText, setRetryingText] = useState(false);

  const key = item.objectKey;
  const name = documentName(key);

  // A baseline copy that ends in an error is reported, not only shown in its field.
  const lastEvaluationStatus = useRef(item.evaluationStatus);
  useEffect(() => {
    if (lastEvaluationStatus.current === 'BASELINE_COPYING' && item.evaluationStatus === 'BASELINE_ERROR') {
      // A notice, worded when it is raised; `intl` stays out of the dependencies.
      setErrorMessage(intl.formatMessage(messages.baselineCopyFailed, { name }));
    }
    lastEvaluationStatus.current = item.evaluationStatus;
  }, [item.evaluationStatus]);

  const pages = item.pages ?? [];
  const displayedMetering = displayed.metering as Record<string, Record<string, unknown>> | null | undefined;
  const gates = documentActionGates(
    {
      roles: {
        isAdmin: !!roles.isAdmin,
        isAuthor: !!roles.isAuthor,
        isReviewer: !!roles.isReviewer,
        isAnnotator: !!roles.isAnnotator,
        isViewer: !!roles.isViewer,
        isReviewerOnly: !!roles.isReviewerOnly,
        canWorkReviewQueues: !!roles.canWorkReviewQueues,
      },
      doc: {
        ...item,
        sectionCount: (item.sections ?? []).length,
        pageCount: pages.length,
        pagesWithText: pages.filter((p) => !!p.TextUri).length,
      },
      displayed: { ...displayed, hasCost: !!displayedMetering && Object.keys(displayedMetering).length > 0 },
      isHistorical,
      holdsReview:
        (!!item.hitlReviewOwner && item.hitlReviewOwner === user?.username) ||
        (!!item.hitlReviewOwnerEmail && item.hitlReviewOwnerEmail.toLowerCase() === (user?.signInDetails?.loginId ?? '').toLowerCase()),
      bdaMode: isBdaModeConfig(mergedConfig),
      configLoading,
      baselineRequestInFlight,
      defaultQueueEnabled: reviewSettings.defaultQueueEnabled,
    },
    intl,
  );

  const board = usePageClassBoard(useDetailPageClassBoard({ item, gates, mergedConfig, refresh }), null, null);

  const retryUnfinishedText = async () => {
    if (!unfinishedText) return;
    setRetryingText(true);
    try {
      await reprocessPageText(key, unfinishedText);
      setUnfinishedText(null);
      setSuccessMessage(intl.formatMessage(messages.pageTextSaved, { pageId: unfinishedText.pageId, name }));
      void refresh();
    } catch (error) {
      setErrorMessage(intl.formatMessage(messages.pageTextReprocessFailed, { name, error: getErrorMessage(error) }));
    } finally {
      setRetryingText(false);
    }
  };

  /** Run an action: `busy` while it runs. */
  const run = async (action: Exclude<Busy, null>, fn: () => Promise<void>) => {
    setBusy(action);
    try {
      await fn();
    } finally {
      setBusy(null);
    }
  };

  const startReview = () =>
    run('claim', async () => {
      try {
        const result = await client.graphql({ query: claimReview, variables: { objectKey: key } });
        const claimed = result.data?.claimReview;
        if (claimed) {
          onDocumentUpdate((prev) => ({
            ...prev,
            hitlReviewOwner: (claimed.HITLReviewOwner as string | undefined) ?? prev.hitlReviewOwner,
            hitlReviewOwnerEmail: (claimed.HITLReviewOwnerEmail as string | undefined) ?? prev.hitlReviewOwnerEmail,
            hitlStatus: (claimed.HITLStatus as string | undefined) ?? prev.hitlStatus,
          }));
        }
        setSuccessMessage(intl.formatMessage(messages.reviewClaimed, { name }));
        void refresh();
      } catch (error) {
        logger.error('Failed to claim review:', error);
        setErrorMessage(intl.formatMessage(messages.reviewClaimFailed, { error: getErrorMessage(error) }));
      }
    });

  const useAsBaseline = () =>
    run('baseline', async () => {
      setBaselineRequestInFlight(true);
      try {
        // The Lambda returns as soon as the copy has started.
        await copyDocumentToBaseline(key);
        setSuccessMessage(intl.formatMessage(messages.baselineCopyStarted, { name }));
        await refresh();
      } catch (error) {
        logger.error('Error initiating copy to evaluation baseline:', error);
        setErrorMessage(intl.formatMessage(messages.baselineUseFailed, { name, error: getErrorMessage(error) }));
      } finally {
        setBaselineRequestInFlight(false);
      }
    });

  // Skip All Reviews (Admin or Reviewer); the reason and note are recorded with the skip
  // in the review history.
  const skipAll = (reason: string, note: string | undefined) =>
    run('skip-all', async () => {
      setAskSkipReason(false);
      try {
        const result = await client.graphql({
          query: skipAllSectionsReview,
          variables: { objectKey: key, reason, note, resubmit: skipResubmit },
        });
        const updatedData = result.data.skipAllSectionsReview;
        if (updatedData) {
          const reviewHistory = parseHITLReviewHistory(updatedData.HITLReviewHistory as string);
          onDocumentUpdate((prev) => ({
            ...prev,
            objectStatus: updatedData.ObjectStatus ?? prev.objectStatus,
            hitlStatus: updatedData.HITLStatus ?? prev.hitlStatus,
            hitlTriggered: updatedData.HITLTriggered ?? prev.hitlTriggered,
            hitlCompleted: updatedData.HITLCompleted ?? true,
            hitlSectionsPending: updatedData.HITLSectionsPending ?? [],
            hitlSectionsCompleted: updatedData.HITLSectionsCompleted ?? prev.hitlSectionsCompleted,
            hitlSectionsSkipped: updatedData.HITLSectionsSkipped ?? [],
            hitlReviewOwner: updatedData.HITLReviewOwner || prev.hitlReviewOwner,
            hitlReviewOwnerEmail: updatedData.HITLReviewOwnerEmail || prev.hitlReviewOwnerEmail,
            hitlReviewHistory: reviewHistory ?? prev.hitlReviewHistory,
          }));
        }
        setSuccessMessage(intl.formatMessage(messages.skippedAll, { name }));
        void refresh();
      } catch (error) {
        logger.error('Failed to skip all sections review:', error);
        setErrorMessage(intl.formatMessage(messages.skipAllFailed, { error: getErrorMessage(error) }));
      }
    });

  const firstPageWithText = pages.find((p) => !!p.TextUri);
  const onMoreAction = (id: string) => {
    if (id === 'regroup') board.open();
    else if (id === 'reextract' || id === 'reset-class') setChangeDialog(id);
    else if (id === 'page-text' && firstPageWithText) setPageTextAt(String(firstPageWithText.Id));
    else if (id === 'baseline') void useAsBaseline();
    else if (id === 'skip-all') {
      setSkipResubmit(true);
      setAskSkipReason(true);
    } else if (id === 'send-to-review') setAskSendToReview(true);
    else if (id === 'reprocess') input.onReprocess();
    else if (id === 'abort') input.onAbort();
    else if (id === 'delete') input.onDelete();
    else download.onItemClick(id);
  };

  const working = busy !== null;
  // The downloads' items are plain items (useDocumentDownload builds no groups).
  const downloads = download.items as ActionBarMenuItem[];
  const lifecycle = [
    ...offered(gates.reprocess, 'reprocess', intl.formatMessage(messages.reprocess)),
    ...offered(gates.abort, 'abort', intl.formatMessage(messages.abort)),
    ...offered(gates.delete, 'delete', intl.formatMessage(messages.deleteDocument)),
  ];

  const bar: (ActionBarControl | false)[] = [
    {
      kind: 'switch',
      id: 'view',
      label: intl.formatMessage(messages.viewSwitch),
      selectedId: view,
      options: [
        { id: 'details', text: intl.formatMessage(messages.details) },
        { id: 'source', text: intl.formatMessage(messages.source) },
      ],
      onChange: (id) => onViewChange(id as DocumentView),
    },
    {
      kind: 'menu',
      id: 'reports',
      text: intl.formatMessage(messages.reports),
      items: [
        ...offered(gates.summary, 'summary', intl.formatMessage(messages.summary)),
        ...offered(gates.evaluation, 'evaluation', intl.formatMessage(messages.evaluationReport)),
        ...offered(gates['rule-validation'], 'rule-validation', intl.formatMessage(messages.ruleValidationSummary)),
        ...offered(gates.cost, 'cost', intl.formatMessage(messages.estimatedCost)),
        ...offered(gates.flow, 'flow', intl.formatMessage(messages.processingFlow)),
      ],
      onItemClick: (id) => {
        if (id === 'flow') setFlowOpen(true);
        else if (id === 'cost') setCostOpen(true);
        else setReport(id as DocumentReport);
      },
    },
    {
      kind: 'menu',
      id: 'more',
      text: intl.formatMessage(messages.moreActions),
      items: [
        ...offered(gates.regroup, 'regroup', intl.formatMessage(messages.editPageGrouping)),
        ...offered(gates.reextract, 'reextract', intl.formatMessage(messages.reextractSections)),
        ...offered(gates['page-text'], 'page-text', intl.formatMessage(messages.editPageText)),
        ...offered(gates['reset-class'], 'reset-class', intl.formatMessage(messages.resetPageClass)),
        ...offered(gates.baseline, 'baseline', intl.formatMessage(messages.useAsBaseline)),
        ...offered(gates['skip-all'], 'skip-all', intl.formatMessage(messages.skipAllReviews)),
        ...offered(gates['send-to-review'], 'send-to-review', intl.formatMessage(messages.sendToReview)),
        ...(downloads.length > 0 ? [{ kind: 'group' as const, items: downloads }] : []),
        ...(lifecycle.length > 0 ? [{ kind: 'group' as const, items: lifecycle }] : []),
      ],
      disabled: working || !!download.disabled,
      loading: busy === 'baseline' || busy === 'skip-all' || !!download.loading,
      expandToViewport: true,
      onItemClick: onMoreAction,
    },
    {
      kind: 'button',
      id: 'refresh',
      text: intl.formatMessage(messages.refresh),
      iconName: 'refresh',
      disabledReason: gates.refresh.disabledReason,
      loading: busy === 'refresh',
      onClick: () =>
        void run('refresh', async () => {
          await refresh();
        }),
    },
    gates.troubleshoot.shown && {
      kind: 'button',
      id: 'troubleshoot',
      text: intl.formatMessage(messages.troubleshoot),
      iconName: 'gen-ai',
      onClick: () => setTroubleshootOpen(true),
    },
    gates['start-review'].shown && {
      kind: 'button',
      id: 'start-review',
      text: intl.formatMessage(messages.startReview),
      iconName: 'user-profile',
      disabledReason: gates['start-review'].disabledReason,
      loading: busy === 'claim',
      onClick: () => void startReview(),
    },
    gates.edit.shown && {
      kind: 'button',
      id: 'edit',
      text: intl.formatMessage(messages.edit),
      variant: 'primary',
      iconName: 'edit',
      href: documentEditHref(key, { from: 'document' }),
      ariaLabel: gates.editInQueue ? intl.formatMessage(messages.editInQueue) : undefined,
      disabledReason: gates.edit.disabledReason,
    },
  ];
  const controls = bar.filter((c): c is ActionBarControl => !!c);

  // The baseline copy's states are messages; any other evaluation status shows as stored.
  const evaluationStatusCell =
    baselineRequestInFlight || item.evaluationStatus === 'BASELINE_COPYING' ? (
      <StatusIndicator type="in-progress">
        <FormattedMessage {...messages.baselineCopying} />
      </StatusIndicator>
    ) : item.evaluationStatus === 'BASELINE_AVAILABLE' ? (
      <StatusIndicator type="success">
        <FormattedMessage {...messages.baselineAvailable} />
      </StatusIndicator>
    ) : item.evaluationStatus === 'BASELINE_ERROR' ? (
      <StatusIndicator type="error">
        <FormattedMessage {...messages.baselineFailed} />
      </StatusIndicator>
    ) : (
      item.evaluationStatus || <FormattedMessage {...messages.baselineNotApplicable} />
    );

  const dialogs = (
    <>
      {/* Mounted only while open, so their content is read only then. */}
      {report && <DocumentReportDialog report={report} displayed={displayed} onDismiss={() => setReport(null)} />}
      {costOpen && displayedMetering && (
        <EstimatedCostModal
          meteringData={displayedMetering}
          pageCount={displayed.pageCount as number | undefined}
          onDismiss={() => setCostOpen(false)}
        />
      )}
      {item.executionArn && (
        <StepFunctionFlowViewer
          executionArn={item.executionArn}
          visible={flowOpen}
          onDismiss={() => setFlowOpen(false)}
          mergedConfig={mergedConfig ?? undefined}
          malwareScan={item}
        />
      )}
      {gates.troubleshoot.shown && (
        <TroubleshootModal
          visible={troubleshootOpen}
          onDismiss={() => setTroubleshootOpen(false)}
          documentItem={item}
          existingJob={troubleshootJobs[key] as unknown as { jobId: string; status: string }}
          onJobUpdate={(job: TroubleshootJob) => setTroubleshootJobs((prev) => ({ ...prev, [key]: job }))}
        />
      )}
      {board.dialog}
      {changeDialog === 'reextract' && (
        <ReextractSectionsDialog
          objectKey={key}
          sections={(item.sections ?? []) as never}
          disabledReason={gates.reextract.disabledReason}
          onDone={() => void refresh()}
          onDismiss={() => setChangeDialog(null)}
        />
      )}
      {changeDialog === 'reset-class' && (
        <ResetPageClassDialog
          objectKey={key}
          pages={pages as never}
          disabledReason={gates['reset-class'].disabledReason}
          onDone={() => void refresh()}
          onDismiss={() => setChangeDialog(null)}
        />
      )}
      {pageTextAt !== null && (
        <EditPageTextDialog
          objectKey={key}
          pages={pages as never}
          initialPageId={pageTextAt}
          disabledReason={gates['page-text'].disabledReason}
          onDone={() => void refresh()}
          onUnfinished={setUnfinishedText}
          onDismiss={() => setPageTextAt(null)}
        />
      )}
      {askSkipReason && (
        <ReasonModal
          visible
          onDismiss={() => setAskSkipReason(false)}
          header={intl.formatMessage(messages.skipAllHeader)}
          reasons={SKIP_ALL_REASONS}
          confirmLabel={intl.formatMessage(messages.skipAllConfirm)}
          loading={busy === 'skip-all'}
          extra={
            <Checkbox
              checked={skipResubmit}
              onChange={({ detail }) => setSkipResubmit(detail.checked)}
              description={intl.formatMessage(messages.resubmitDescription)}
            >
              <FormattedMessage {...messages.resubmitLabel} />
            </Checkbox>
          }
          onConfirm={(reason, note) => void skipAll(reason, note)}
        >
          <SpaceBetween size="s">
            <Alert type="warning">
              <FormattedMessage {...messages.skipAllWarning} />
            </Alert>
            <Box>
              <FormattedMessage {...messages.skipAllIntro} />
            </Box>
            <ul>
              <li>
                <FormattedMessage {...messages.skipAllMarkSkipped} />
              </li>
              <li>
                <FormattedMessage {...messages.skipAllRecorded} />
              </li>
            </ul>
            <Box>
              <FormattedMessage {...messages.skipAllQuestion} />
            </Box>
          </SpaceBetween>
        </ReasonModal>
      )}
      {askSendToReview && (
        <SendToReviewModal
          objectKeys={[key]}
          onDismiss={() => setAskSendToReview(false)}
          onDone={(result) => {
            setAskSendToReview(false);
            const outcome = result.results[0]?.outcome ?? 'FAILED';
            if (outcome === 'SENT') {
              onDocumentUpdate((prev) => ({ ...prev, hitlStatus: 'in_review', hitlCompleted: false }));
              setSuccessMessage(intl.formatMessage(messages.sentToReview, { name }));
              void refresh();
            } else {
              setErrorMessage(intl.formatMessage(messages.sendToReviewFailed, { name, outcome: outcomeView(outcome, intl).label }));
            }
          }}
        />
      )}
    </>
  );

  const notices: EditorNotice[] = unfinishedText
    ? [
        {
          id: 'page-text-unfinished',
          type: 'error',
          content: intl.formatMessage(messages.pageTextUnfinished, { pageId: unfinishedText.pageId, name }),
          action: (
            <Button loading={retryingText} onClick={() => void retryUnfinishedText()}>
              <FormattedMessage {...messages.pageTextRetry} />
            </Button>
          ),
          onDismiss: () => setUnfinishedText(null),
        },
      ]
    : [];

  const pageTextEdit = gates['page-text'].shown
    ? { disabledReason: gates['page-text'].disabledReason, open: (pageId: string) => setPageTextAt(pageId) }
    : null;

  return { controls, dialogs, notices, evaluationStatusCell, gates, pageTextEdit };
};

export default useDocumentActions;
