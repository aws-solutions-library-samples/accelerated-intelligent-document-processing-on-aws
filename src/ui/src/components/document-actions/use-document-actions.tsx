// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The document page's action bar and the dialogs it opens. Left to right: the Details |
// Source switch; Reports (Summary, Evaluation Report, Rule Validation Summary,
// Processing Flow); More Actions (the changes; then the downloads, as a group; then
// Reprocess, Abort and Delete as a group, Delete last); Refresh; Troubleshoot; Start
// Review, where it applies; and Edit, the primary action. More Actions opens Edit Page Class (the edit
// page's board, with Save and Re-extract), Re-extract Sections, Edit Page Text and Reset
// Page Class. Which actions are offered and why one is
// disabled come from documentActionGates (document-action-gates.ts).
//
// It reads no configuration and no route itself: the page passes them in.
import React, { useEffect, useRef, useState } from 'react';
import { Alert, Box, Button, SpaceBetween, StatusIndicator } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';

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
import StepFunctionFlowViewer from '../step-function-flow/StepFunctionFlowViewer';
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
  const roles = useUserRole();
  const { user, setErrorMessage, setSuccessMessage } = useAppContext();
  const [busy, setBusy] = useState<Busy>(null);
  const [baselineRequestInFlight, setBaselineRequestInFlight] = useState(false);
  const [report, setReport] = useState<DocumentReport | null>(null);
  const [flowOpen, setFlowOpen] = useState(false);
  const [troubleshootOpen, setTroubleshootOpen] = useState(false);
  // The last Troubleshoot job per document, for this page visit: reopening shows it.
  const [troubleshootJobs, setTroubleshootJobs] = useState<Record<string, TroubleshootJob>>({});
  const [askSkipReason, setAskSkipReason] = useState(false);
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
      setErrorMessage(`The copy of ${name} to the evaluation baseline failed.`);
    }
    lastEvaluationStatus.current = item.evaluationStatus;
  }, [item.evaluationStatus]);

  const pages = item.pages ?? [];
  const gates = documentActionGates({
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
    displayed,
    isHistorical,
    holdsReview:
      (!!item.hitlReviewOwner && item.hitlReviewOwner === user?.username) ||
      (!!item.hitlReviewOwnerEmail && item.hitlReviewOwnerEmail.toLowerCase() === (user?.signInDetails?.loginId ?? '').toLowerCase()),
    bdaMode: isBdaModeConfig(mergedConfig),
    configLoading,
    baselineRequestInFlight,
  });

  const board = usePageClassBoard(useDetailPageClassBoard({ item, gates, mergedConfig, refresh }), null, null);

  const retryUnfinishedText = async () => {
    if (!unfinishedText) return;
    setRetryingText(true);
    try {
      await reprocessPageText(key, unfinishedText);
      setUnfinishedText(null);
      setSuccessMessage(`Saved page ${unfinishedText.pageId}'s text of ${name}. The sections that hold it are being extracted again.`);
      void refresh();
    } catch (error) {
      setErrorMessage(`Could not reprocess ${name}: ${getErrorMessage(error)}`);
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
        setSuccessMessage(`You now hold the review of ${name}. Choose Edit to work on it.`);
        void refresh();
      } catch (error) {
        logger.error('Failed to claim review:', error);
        setErrorMessage(`Failed to start review: ${getErrorMessage(error)}`);
      }
    });

  const useAsBaseline = () =>
    run('baseline', async () => {
      setBaselineRequestInFlight(true);
      try {
        // The Lambda returns as soon as the copy has started.
        await copyDocumentToBaseline(key);
        setSuccessMessage(`Copying ${name} to the evaluation baseline. Its evaluation status shows when the copy is done.`);
        await refresh();
      } catch (error) {
        logger.error('Error initiating copy to evaluation baseline:', error);
        setErrorMessage(`Could not use ${name} as the evaluation baseline: ${getErrorMessage(error)}`);
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
        const result = await client.graphql({ query: skipAllSectionsReview, variables: { objectKey: key, reason, note } });
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
        setSuccessMessage(`Skipped the review of ${name}.`);
        void refresh();
      } catch (error) {
        logger.error('Failed to skip all sections review:', error);
        setErrorMessage(`Failed to skip all sections: ${getErrorMessage(error)}`);
      }
    });

  const firstPageWithText = pages.find((p) => !!p.TextUri);
  const onMoreAction = (id: string) => {
    if (id === 'regroup') board.open();
    else if (id === 'reextract' || id === 'reset-class') setChangeDialog(id);
    else if (id === 'page-text' && firstPageWithText) setPageTextAt(String(firstPageWithText.Id));
    else if (id === 'baseline') void useAsBaseline();
    else if (id === 'skip-all') setAskSkipReason(true);
    else if (id === 'reprocess') input.onReprocess();
    else if (id === 'abort') input.onAbort();
    else if (id === 'delete') input.onDelete();
    else download.onItemClick(id);
  };

  const working = busy !== null;
  // The downloads' items are plain items (useDocumentDownload builds no groups).
  const downloads = download.items as ActionBarMenuItem[];
  const lifecycle = [
    ...offered(gates.reprocess, 'reprocess', 'Reprocess'),
    ...offered(gates.abort, 'abort', 'Abort'),
    ...offered(gates.delete, 'delete', 'Delete'),
  ];

  const bar: (ActionBarControl | false)[] = [
    {
      kind: 'switch',
      id: 'view',
      label: "Show the document's details or its source",
      selectedId: view,
      options: [
        { id: 'details', text: 'Details' },
        { id: 'source', text: 'Source' },
      ],
      onChange: (id) => onViewChange(id as DocumentView),
    },
    {
      kind: 'menu',
      id: 'reports',
      text: 'Reports',
      items: [
        ...offered(gates.summary, 'summary', 'Summary'),
        ...offered(gates.evaluation, 'evaluation', 'Evaluation Report'),
        ...offered(gates['rule-validation'], 'rule-validation', 'Rule Validation Summary'),
        ...offered(gates.flow, 'flow', 'Processing Flow'),
      ],
      onItemClick: (id) => {
        if (id === 'flow') setFlowOpen(true);
        else setReport(id as DocumentReport);
      },
    },
    {
      kind: 'menu',
      id: 'more',
      text: 'More Actions',
      items: [
        ...offered(gates.regroup, 'regroup', 'Edit Page Class'),
        ...offered(gates.reextract, 'reextract', 'Re-extract Sections'),
        ...offered(gates['page-text'], 'page-text', 'Edit Page Text'),
        ...offered(gates['reset-class'], 'reset-class', 'Reset Page Class'),
        ...offered(gates.baseline, 'baseline', 'Use As Evaluation Baseline'),
        ...offered(gates['skip-all'], 'skip-all', 'Skip All Reviews'),
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
      text: 'Refresh',
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
      text: 'Troubleshoot',
      iconName: 'gen-ai',
      onClick: () => setTroubleshootOpen(true),
    },
    gates['start-review'].shown && {
      kind: 'button',
      id: 'start-review',
      text: 'Start Review',
      iconName: 'user-profile',
      disabledReason: gates['start-review'].disabledReason,
      loading: busy === 'claim',
      onClick: () => void startReview(),
    },
    gates.edit.shown && {
      kind: 'button',
      id: 'edit',
      text: 'Edit',
      variant: 'primary',
      iconName: 'edit',
      href: documentEditHref(key),
      ariaLabel: gates.editInQueue ? 'Edit this document in its review queue' : undefined,
      disabledReason: gates.edit.disabledReason,
    },
  ];
  const controls = bar.filter((c): c is ActionBarControl => !!c);

  const evaluationStatusCell =
    baselineRequestInFlight || item.evaluationStatus === 'BASELINE_COPYING' ? (
      <StatusIndicator type="in-progress">Baseline copying in progress</StatusIndicator>
    ) : item.evaluationStatus === 'BASELINE_AVAILABLE' ? (
      <StatusIndicator type="success">Baseline available</StatusIndicator>
    ) : item.evaluationStatus === 'BASELINE_ERROR' ? (
      <StatusIndicator type="error">Baseline copy failed</StatusIndicator>
    ) : (
      item.evaluationStatus || 'N/A'
    );

  const dialogs = (
    <>
      {/* Mounted only while open, so their content is read only then. */}
      {report && <DocumentReportDialog report={report} displayed={displayed} onDismiss={() => setReport(null)} />}
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
          header="Skip All Section Reviews"
          reasons={SKIP_ALL_REASONS}
          confirmLabel="Skip All Reviews"
          loading={busy === 'skip-all'}
          onConfirm={(reason, note) => void skipAll(reason, note)}
        >
          <SpaceBetween size="s">
            <Alert type="warning">This action will skip all pending section reviews and continue the document processing workflow.</Alert>
            <Box>Skipping review will:</Box>
            <ul>
              <li>Mark all pending sections as review skipped without human verification</li>
              <li>Record this action, with your reason, in the review history</li>
            </ul>
            <Box>Are you sure you want to skip all section reviews?</Box>
          </SpaceBetween>
        </ReasonModal>
      )}
    </>
  );

  const notices: EditorNotice[] = unfinishedText
    ? [
        {
          id: 'page-text-unfinished',
          type: 'error',
          content: `Page ${unfinishedText.pageId}'s text was saved, but ${name} was not reprocessed.`,
          action: (
            <Button loading={retryingText} onClick={() => void retryUnfinishedText()}>
              Retry reprocess
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
