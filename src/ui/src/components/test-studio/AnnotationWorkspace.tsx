// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * AnnotationWorkspace — a test set's annotation queue page.
 * Route: /review-queues/test-sets/:testSetId
 *
 * Worst-first: documents are ordered by confidence-alert count so each review removes
 * the most expected error. The page shows the team's progress and, for an Admin,
 * assigns documents to the Annotators listed on the queue (an assignment does not
 * expire) and sets how long an assigned document waits before its Annotator is
 * reminded. A listed Annotator (from Home › Queues) claims an unassigned document
 * instead, which assigns it to them. Each row opens the edit screen
 * (TestSetItemEdit), where documents are edited (read-only unless assigned to the
 * viewer).
 *
 * The URL is safe to share: it only navigates, and every operation is authorized
 * server-side against the caller's allowedTestSets.
 */

import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import {
  Alert,
  Badge,
  Box,
  Button,
  ButtonDropdown,
  ColumnLayout,
  Container,
  ContentLayout,
  CopyToClipboard,
  Header,
  Link,
  Modal,
  Pagination,
  Popover,
  Select,
  SpaceBetween,
  Spinner,
  Table,
  TextFilter,
} from '@cloudscape-design/components';
import type { SelectProps } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from '../../api/client-shim';
import { estimateReviewEffort, getAnnotationQueue, removeDocumentsFromTestSet } from '../../graphql/generated';
import useAppContext from '../../contexts/app';
import useSettingsContext from '../../contexts/settings';
import { useOptionalReviewWork } from '../../hooks/use-review-work';
import useUserRole from '../../hooks/use-user-role';
import { describeApiError, isAuthorizationError } from '../../hooks/utils/graphql-error';
import { testSetAnnotateHref, testSetItemPath } from '../../routes/constants';
import AssignModal from '../review-queues/AssignModal';
import QueueAnnotatorsTable, { queueAnnotatorRows } from '../review-queues/QueueAnnotatorsTable';
import QueueProgress, { progressPercent } from '../review-queues/QueueProgress';
import QueueDefinitionModal from '../review-queues/QueueDefinitionModal';
import type { TestSetQueueTarget } from '../review-queues/QueueDefinitionModal';
import {
  assignTestSetDocuments,
  claimTestSetDocument,
  listAnnotatorEmails,
  listTestSetQueues,
  unassignTestSetDocuments,
} from '../review-queues/reviewer-assignment';
import type { AnnotationQueue, QueueItem } from '../edit-screen/test-set-item-edit-adapter';
import { renderAlertCount, renderLabelSource } from './TestSetDetail';
import {
  REVIEWED_HIDDEN_HINT,
  annotationQueueLink,
  currentDraft,
  draftBadgeHover,
  estimateDecimals as decimalsFor,
  formatPct,
  queueEmptyText,
  staleLinkVersion as staleVersionFor,
} from './annotation-edit-flags';
import { createLatestResponseGuard } from './latestResponseOnly';

export type { QueueItem } from '../edit-screen/test-set-item-edit-adapter';

const client = generateClient();
const logger = new ConsoleLogger('AnnotationWorkspace');

/** The queue table's status filter. The documents nobody holds yet are listed by default. */
type DocStatusFilter = 'all' | 'assigned' | 'unassigned';

const DEFAULT_STATUS_FILTER: DocStatusFilter = 'unassigned';

const STATUS_FILTER_OPTIONS: (SelectProps.Option & { value: DocStatusFilter })[] = [
  { value: 'all', label: 'All documents' },
  { value: 'assigned', label: 'Assigned' },
  { value: 'unassigned', label: 'Not assigned' },
];

/** The document's task is complete: reviewed, or skipped under the earlier behaviour. */
const isDone = (d: QueueItem): boolean => d.reviewed || !!d.skipped;

/**
 * Whether a queue document matches a status filter. "Not assigned" is exactly the
 * documents an Annotator can claim: nobody holds them and their task isn't complete.
 */
const matchesStatus = (d: QueueItem, filter: DocStatusFilter): boolean => {
  switch (filter) {
    case 'unassigned':
      return !isDone(d) && !d.assignedTo;
    case 'assigned':
      return !!d.assignedTo;
    default:
      return true;
  }
};

const QUEUE_PAGE_SIZE = 100;
const LABEL_JOB_POLL_MS = 5000;
/** Rows per page in the queue table. */
const QUEUE_ROWS_PER_PAGE = 20;

const plural = (n: number, word: string): string => `${n} ${word}${n === 1 ? '' : 's'}`;

/** What the review is buying: the set's estimated label accuracy, from estimateReviewEffort. */
interface ReviewImpact {
  baselineError: number;
  residualError: number;
  estimateConfidence: string;
  qualityTier?: string | null;
  qualityTierReason?: string | null;
  totalObservations: number;
}

interface AnnotationQueuePageProps {
  /** Told the test set's name once its queue has loaded (for the breadcrumbs). */
  onName?: (name: string) => void;
}

const AnnotationQueuePage = ({ onName }: AnnotationQueuePageProps): React.JSX.Element => {
  const { testSetId = '' } = useParams<{ testSetId: string }>();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  // A link from before documents had their own page: ?doc=x.pdf[&field=...].
  const legacyDoc = searchParams.get('doc');
  const legacyField = searchParams.get('field');
  // Which version transition a shared queue link belongs to (Copy queue link adds ?v=).
  const requestedVersion = searchParams.get('v');
  const { settings } = useSettingsContext();
  const { setErrorMessage, setSuccessMessage } = useAppContext();
  const { canAnnotate, isAdmin, isAnnotator, isAnnotatorOnly, loading: roleLoading } = useUserRole();
  const reviewWork = useOptionalReviewWork();
  // The Annotators listed on this set's queue (Admin): read with the page, and replaced by
  // a saved settings dialog at once rather than waiting for the next read.
  const [eligibleAnnotators, setEligibleAnnotators] = useState<string[] | null>(null);
  useEffect(() => {
    if (!isAdmin || !testSetId) return;
    listTestSetQueues()
      .then((sets) => setEligibleAnnotators(sets.find((s) => s.id === testSetId)?.eligibleAnnotators ?? []))
      .catch((err: unknown) => {
        logger.warn('listTestSetQueues failed', err);
        setEligibleAnnotators([]);
      });
  }, [isAdmin, testSetId]);
  // A non-Admin Annotator claims documents rather than assigning them.
  const claimsWork = isAnnotator && !isAdmin;
  const testSetBucket = (settings as Record<string, unknown>).TestSetBucket as string | undefined;

  const [queue, setQueue] = useState<AnnotationQueue | null>(null);
  // The breadcrumb names the set, as the title does, once the queue has loaded.
  const queueName = queue?.name;
  useEffect(() => {
    if (queueName) onName?.(queueName);
  }, [queueName, onName]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [queueFilter, setQueueFilter] = useState('');
  const [queuePage, setQueuePage] = useState(1);
  const [statusFilter, setStatusFilter] = useState<DocStatusFilter>(DEFAULT_STATUS_FILTER);
  const showReviewed = statusFilter === 'all';
  const [selectedRows, setSelectedRows] = useState<QueueItem[]>([]);
  const [assignOpen, setAssignOpen] = useState(false);
  const [candidates, setCandidates] = useState<string[]>([]);
  const [candidatesLoading, setCandidatesLoading] = useState(false);
  const [bulkBusy, setBulkBusy] = useState<'assign' | 'unassign' | 'claim' | null>(null);
  const [claimingKey, setClaimingKey] = useState<string | null>(null);
  const [settingsTarget, setSettingsTarget] = useState<TestSetQueueTarget | null>(null);
  const [deleting, setDeleting] = useState<QueueItem | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [pollTick, setPollTick] = useState(0);
  // Whether the queue in hand was read with reviewed documents included. The server
  // omits them unless asked, so revealing them costs a reload this tracks.
  const [queueIncludesReviewed, setQueueIncludesReviewed] = useState(false);
  /**
   * The set's estimated label accuracy. Review never rewrites a field's confidence;
   * what it improves is the estimate, so this is what a reviewer's work is buying.
   */
  const [impact, setImpact] = useState<ReviewImpact | null>(null);

  /**
   * One guard per mounted page: `useRef(createLatestResponseGuard())` would build a
   * fresh guard on every render and discard all but the first.
   */
  const responseGuard = useMemo(createLatestResponseGuard, []);
  /** The estimate's own guard, so it and the queue reload never cancel each other. */
  const impactGuard = useMemo(createLatestResponseGuard, []);

  /**
   * How many foreground loads are in flight. A count rather than a flag because the
   * spinner belongs to all of them collectively; see `loadQueue`'s `finally`.
   */
  const foregroundLoads = useRef(0);

  /**
   * `background` reconciles without blocking: the spinner (the table's loading state)
   * and the page banner are left alone. Used by the poll while draft labeling runs or
   * summaries build, which nobody asked for and which would otherwise blank the table
   * every few seconds.
   *
   * ⚠️ Reloads overlap — the poll, a status-filter change and the reload after an
   * assignment can all be in flight at once, and each reply's duration scales with the
   * sections it hydrates — so a reply has to prove it is still the newest before it may
   * replace the queue. Otherwise an older reply (say, one read without reviewed
   * documents) lands last and overwrites the one asked for; see `latestResponseOnly.ts`.
   *
   * A background failure is deliberately not raised as the page banner: nothing the
   * user did caused it, and the banner suppresses the "Queue complete" alert, which is
   * gated on there being no error. The next foreground load still reports.
   */
  const loadQueue = useCallback(
    async (background = false) => {
      if (!testSetId) return;
      // Claimed before the await, so precedence is decided by which request started
      // last rather than by which happened to finish last.
      const isCurrentLoad = responseGuard.claim();
      if (!background) {
        foregroundLoads.current += 1;
        setIsLoading(true);
        setError(null);
      }
      try {
        const r = await client.graphql({
          query: getAnnotationQueue,
          variables: { testSetId, limit: QUEUE_PAGE_SIZE, includeCompleted: showReviewed },
        });
        if (!isCurrentLoad()) return;
        const data = r.data?.getAnnotationQueue ?? null;
        if (!data) {
          if (!background) setError('The annotation queue could not be loaded.');
          return;
        }
        setQueue(data);
        setQueueIncludesReviewed(showReviewed);
      } catch (err) {
        logger.error('Error loading annotation queue:', err);
        // A superseded request's failure must not put a banner over the request whose
        // rows are actually being shown, for the same reason its success must not
        // replace them.
        if (!background && isCurrentLoad()) {
          setError(
            isAuthorizationError(err)
              ? 'You do not have access to this test set. Ask an Admin to give your account access.'
              : describeApiError(err, 'load the annotation queue'),
          );
        }
      } finally {
        // Cleared when the LAST foreground load finishes, not when this one does.
        //
        // ⚠️ Not `isCurrentLoad()`, which deadlocks: a background load claims the same
        // guard, so it can supersede a foreground one that then declines to clear the
        // spinner no background load will ever clear either — leaving isLoading true
        // for good. Counting in-flight foreground loads keeps the protective half (a
        // superseded reply cannot report "loaded" while the reply that will be shown is
        // still coming) without a state nothing can leave.
        if (!background) {
          foregroundLoads.current = Math.max(0, foregroundLoads.current - 1);
          if (foregroundLoads.current === 0) setIsLoading(false);
        }
      }
    },
    [testSetId, showReviewed, responseGuard],
  );

  const loadImpact = useCallback(async () => {
    if (!testSetId) return;
    const isCurrentEstimate = impactGuard.claim();
    try {
      const response = await client.graphql({ query: estimateReviewEffort, variables: { testSetId } });
      const est = response.data?.estimateReviewEffort;
      if (!est || !isCurrentEstimate()) return;
      setImpact({
        baselineError: est.baselineError ?? 0,
        residualError: est.residualError ?? 0,
        estimateConfidence: est.estimateConfidence ?? 'prior',
        qualityTier: est.qualityTier,
        qualityTierReason: est.qualityTierReason,
        totalObservations: est.calibration?.totalObservations ?? 0,
      });
    } catch (err) {
      // Best-effort: the queue is fully usable without the estimate.
      logger.debug('Could not load review impact:', err);
    }
  }, [testSetId, impactGuard]);

  useEffect(() => {
    void loadImpact();
  }, [loadImpact]);

  useEffect(() => {
    if (legacyDoc) {
      const field = legacyField ? `?field=${encodeURIComponent(legacyField)}` : '';
      navigate(`${testSetItemPath(testSetId, legacyDoc)}${field}`, { replace: true });
    }
  }, [legacyDoc, legacyField, testSetId, navigate]);

  useEffect(() => {
    void loadQueue();
  }, [loadQueue]);

  const labelJobRunning = queue?.labelJobStatus === 'RUNNING';
  // The set's document summaries are being built in the background (first open, or
  // after its labels changed as a whole); the page polls until they are ready.
  const summariesBuilding = queue?.summaryStatus === 'BUILDING';

  /**
   * Poll while draft labeling runs: labels are harvested on read, so polling is what
   * advances the job. Keyed on an explicit tick, not the labeled count, which can stay
   * the same for minutes.
   */
  useEffect(() => {
    if (!labelJobRunning && !summariesBuilding) return undefined;
    const timer = setTimeout(async () => {
      await loadQueue(true);
      setPollTick((n) => n + 1);
    }, LABEL_JOB_POLL_MS);
    return () => clearTimeout(timer);
  }, [labelJobRunning, summariesBuilding, pollTick, loadQueue]);

  // Drop selected rows that are no longer listed after a reload.
  useEffect(() => {
    setSelectedRows((prev) => prev.filter((r) => queue?.documents.some((d) => d.objectKey === r.objectKey)));
  }, [queue]);

  const filteredQueue = useMemo(() => {
    const all = (queue?.documents ?? []).filter((d) => matchesStatus(d, statusFilter));
    if (!queueFilter.trim()) return all;
    const needle = queueFilter.trim().toLowerCase();
    return all.filter((d) => d.objectKey.toLowerCase().includes(needle));
  }, [queue, queueFilter, statusFilter]);

  const queuePageCount = Math.max(1, Math.ceil(filteredQueue.length / QUEUE_ROWS_PER_PAGE));
  const pagedQueue = useMemo(() => {
    const start = (queuePage - 1) * QUEUE_ROWS_PER_PAGE;
    return filteredQueue.slice(start, start + QUEUE_ROWS_PER_PAGE);
  }, [filteredQueue, queuePage]);

  // Every document can be assigned: one with a review record saves through the review
  // API, one without (authored ground truth) through saveTestSetDocumentSection.
  const assignable = selectedRows;
  const unassignable = selectedRows.filter((d) => !!d.assignedTo);
  const sharedAssignee =
    assignable.length > 0 && assignable.every((d) => d.assignedTo === assignable[0].assignedTo) ? assignable[0].assignedTo : null;

  const openAssign = async () => {
    setAssignOpen(true);
    setCandidatesLoading(true);
    try {
      const [annotatorEmails, sets] = await Promise.all([listAnnotatorEmails(), listTestSetQueues()]);
      // Only the Annotators listed on this queue (none listed: no Annotator, Admins only).
      const eligible = new Set((sets.find((t) => t.id === testSetId)?.eligibleAnnotators ?? []).map((e) => e.toLowerCase()));
      setCandidates(annotatorEmails.filter((e) => eligible.has(e.toLowerCase())));
    } catch (err) {
      setErrorMessage(describeApiError(err, 'list the Annotators'));
    } finally {
      setCandidatesLoading(false);
    }
  };

  const handleAssign = async (email: string) => {
    setBulkBusy('assign');
    try {
      const done = await assignTestSetDocuments(
        testSetId,
        assignable.map((d) => d.objectKey),
        email,
      );
      setSuccessMessage(`Assigned ${plural(done.length, 'document')} to ${email}.`);
      setAssignOpen(false);
      setSelectedRows([]);
      await loadQueue();
    } catch (err) {
      setErrorMessage(describeApiError(err, 'assign the documents'));
    } finally {
      setBulkBusy(null);
    }
  };

  const handleUnassign = async () => {
    setBulkBusy('unassign');
    try {
      const done = await unassignTestSetDocuments(
        testSetId,
        unassignable.map((d) => d.objectKey),
      );
      setSuccessMessage(`Unassigned ${plural(done.length, 'document')}.`);
      setSelectedRows([]);
      await loadQueue();
    } catch (err) {
      setErrorMessage(describeApiError(err, 'unassign the documents'));
    } finally {
      setBulkBusy(null);
    }
  };

  /** Claim one document: it is assigned to the caller and shows in Home › Tasks. */
  const handleClaim = async (item: QueueItem) => {
    setClaimingKey(item.objectKey);
    reviewWork?.beginMutation();
    try {
      await claimTestSetDocument(testSetId, item.objectKey);
      setSuccessMessage(`Claimed ${item.objectKey}. Work it in Home › Tasks.`);
    } catch (err) {
      setErrorMessage(describeApiError(err, 'claim the document'));
    } finally {
      setClaimingKey(null);
    }
    await Promise.all([loadQueue(), reviewWork?.refresh()]);
  };

  /** Whether a document can be claimed: nobody holds it and its task isn't complete. */
  const isClaimable = (item: QueueItem): boolean => !item.assignedTo && !isDone(item);
  const claimable = selectedRows.filter(isClaimable);

  /**
   * Claim every selected document. One request per document, in order, so a document
   * another Annotator took in the meantime fails on its own and the rest are still
   * claimed; the server refuses any that are already held (only_if_unassigned).
   */
  const handleBulkClaim = async () => {
    const keys = claimable.map((d) => d.objectKey);
    if (keys.length === 0) return;
    setBulkBusy('claim');
    reviewWork?.beginMutation();
    const failed: string[] = [];
    try {
      for (const key of keys) {
        try {
          await claimTestSetDocument(testSetId, key);
        } catch (err) {
          logger.warn(`claim ${key} failed`, err);
          failed.push(key);
        }
      }
      const claimed = keys.length - failed.length;
      if (claimed > 0) {
        setSuccessMessage(`Claimed ${claimed} document${claimed === 1 ? '' : 's'}. Work them in Home › Tasks.`);
      }
      if (failed.length > 0) {
        setErrorMessage(
          `${failed.length} document${failed.length === 1 ? ' was' : 's were'} not claimed, usually because another Annotator took ${
            failed.length === 1 ? 'it' : 'them'
          } first: ${failed.join(', ')}`,
        );
      }
      setSelectedRows([]);
    } finally {
      setBulkBusy(null);
    }
    await Promise.all([loadQueue(), reviewWork?.refresh()]);
  };

  /** The set's queue settings, read fresh so the dialog never edits stale values. */
  const openSettings = async () => {
    try {
      const [sets, annotatorEmails] = await Promise.all([listTestSetQueues(), listAnnotatorEmails()]);
      const ref = sets.find((t) => t.id === testSetId);
      setCandidates(annotatorEmails);
      setSettingsTarget({
        testSetId,
        name: ref?.name || testSetId,
        description: ref?.description,
        priority: ref?.priority,
        claimLeaseMinutes: ref?.claimLeaseMinutes,
        eligibleAnnotators: ref?.eligibleAnnotators,
        checklist: ref?.checklist,
        skipReasons: ref?.skipReasons,
      });
    } catch (err) {
      setErrorMessage(describeApiError(err, 'load the queue settings'));
    }
  };

  // Completed: reviewed, or skipped without a review.
  const completedDocs = queue ? (queue.completedDocs ?? queue.reviewedDocs) : 0;
  const progressPct = queue ? progressPercent(completedDocs, queue.totalDocs) : 0;
  const draft = currentDraft(queue, null);
  const queueLink = annotationQueueLink(window.location.origin, testSetAnnotateHref(testSetId), draft);
  // A link from a transition that has since been published: still usable, but out of date.
  const staleLinkVersion = staleVersionFor(requestedVersion, draft);
  // Whole percent until the curve is measured on this set (see estimateDecimals).
  const estimateDecimals = decimalsFor(impact?.estimateConfidence);

  /** Yes (with the assignee on hover) or No. */
  const renderAssigned = (item: QueueItem): React.ReactNode =>
    item.assignedTo ? <span title={`Assigned to ${item.assignedTo}`}>Yes</span> : 'No';

  const onRowAction = (item: QueueItem, action: string) => {
    if (action === 'view') navigate(`${testSetItemPath(testSetId, item.objectKey)}?view=source`);
    else if (action === 'annotate') navigate(testSetItemPath(testSetId, item.objectKey));
    else if (action === 'claim') void handleClaim(item);
    else if (action === 'assign') {
      setSelectedRows([item]);
      void openAssign();
    } else if (action === 'delete') setDeleting(item);
  };

  const confirmDelete = async () => {
    if (!deleting) return;
    setDeleteBusy(true);
    try {
      await client.graphql({ query: removeDocumentsFromTestSet, variables: { testSetId, fileNames: [deleting.objectKey] } });
      setSuccessMessage(`Removed ${deleting.objectKey} from the test set.`);
      setSelectedRows((prev) => prev.filter((d) => d.objectKey !== deleting.objectKey));
      setDeleting(null);
      await loadQueue();
    } catch (err) {
      setErrorMessage(describeApiError(err, 'remove the document'));
    } finally {
      setDeleteBusy(false);
    }
  };

  const metric = (label: string, value: React.ReactNode, note?: string) => (
    <div key={label}>
      <Box variant="awsui-key-label">{label}</Box>
      <Box fontSize="heading-l" fontWeight="bold">
        {value}
      </Box>
      {note && (
        <Box fontSize="body-s" color="text-body-secondary">
          {note}
        </Box>
      )}
    </div>
  );

  const revealReviewed = () => {
    setStatusFilter('all');
    setQueuePage(1);
  };

  /**
   * The reveal for reviewed documents, shared by the two places that offer it: the
   * table's filter bar, and the action on the "Queue complete" alert.
   *
   * One definition because the progress line is the whole point and it is easy to fix
   * in one place and leave the other dead. Reviewed documents are not held client-side
   * — the server omits them unless asked — so the reveal costs a queue reload, slow
   * enough on a large set to look like a dead click. The alert stays up until that
   * reply lands, so without the progress line its button would simply vanish.
   *
   * Null once the queue in hand includes reviewed documents: the rows themselves are
   * the confirmation.
   */
  const showReviewedControl = !showReviewed ? (
    <Button iconName="status-positive" onClick={revealReviewed}>
      Show reviewed documents
    </Button>
  ) : isLoading && !queueIncludesReviewed ? (
    <Box fontSize="body-s" color="text-body-secondary">
      <Spinner /> Loading reviewed documents…
    </Box>
  ) : null;

  if (!roleLoading && !canAnnotate) {
    return (
      <ContentLayout header={<Header variant="h1">Edit</Header>}>
        <Alert type="error" header="Not available for your account">
          Editing ground truth requires an Annotator, Author or Admin role.
        </Alert>
      </ContentLayout>
    );
  }

  // Queue counts over the documents still to complete: pending (unassigned), assigned.
  const openDocs = (queue?.documents ?? []).filter((d) => !isDone(d));
  // Each listed Annotator with the open documents assigned to them, as on a Production queue.
  const assignedCounts = new Map<string, number>();
  openDocs.forEach((d) => d.assignedTo && assignedCounts.set(d.assignedTo, (assignedCounts.get(d.assignedTo) ?? 0) + 1));
  const annotatorRows = queueAnnotatorRows(
    eligibleAnnotators ?? [],
    [...assignedCounts].map(([email, claimed]) => ({ email, claimed })),
  );
  const pendingCount = openDocs.filter((d) => !d.assignedTo).length;
  const assignedCount = openDocs.filter((d) => d.assignedTo).length;
  const annotatorCount = new Set(openDocs.map((d) => d.assignedTo).filter(Boolean)).size;

  return (
    <ContentLayout
      header={
        <SpaceBetween size="xs">
          <Header
            variant="h1"
            description={
              <>
                Review the documents with the most confidence alerts first — each one corrected removes the most likely errors.
                {draft && (
                  <>
                    {' '}
                    <Popover dismissButton={false} position="bottom" size="medium" triggerType="custom" content={draftBadgeHover(draft)}>
                      <Badge color="blue">
                        v{draft.baseVersion} &rarr; v{draft.draftVersion}
                      </Badge>
                    </Popover>
                  </>
                )}
              </>
            }
            actions={
              <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                {isAdmin && (
                  <Button iconName="settings" onClick={() => void openSettings()}>
                    Settings
                  </Button>
                )}
                {!isAnnotatorOnly && (
                  <CopyToClipboard
                    variant="button"
                    copyButtonText="Copy queue link"
                    textToCopy={queueLink}
                    copySuccessText="Queue link copied"
                    copyErrorText="Could not copy the queue link"
                  />
                )}
              </SpaceBetween>
            }
          >
            {`Test set: ${queue?.name || testSetId}`}
          </Header>
        </SpaceBetween>
      }
    >
      <SpaceBetween size="l">
        {!testSetBucket && <Alert type="error">TestSetBucket is not configured in settings.</Alert>}
        {error && (
          <Alert type="error" action={<Button onClick={() => void loadQueue()}>Retry</Button>}>
            {error}
          </Alert>
        )}
        {/* Not an error, since the set is still annotatable, but working toward v5 while
            believing you are adding to v2 is worth interrupting for. */}
        {staleLinkVersion && (
          <Alert type="warning" header={`This link refers to version ${staleLinkVersion}, which is closed`}>
            That version has been published. You are now annotating toward version {draft?.draftVersion}; the link in your address bar is
            out of date.{!isAnnotatorOnly && ' Use Copy queue link for the current one.'}
          </Alert>
        )}

        {queue && (
          <QueueProgress
            count={completedDocs}
            total={queue.totalDocs}
            countLabel="completed"
            description="Shared across everyone annotating this test set"
          />
        )}

        {queue && (
          <Container header={<Header variant="h2">Key Metrics</Header>}>
            <SpaceBetween size="l">
              {/* One grid for every metric, so a second row stays in the first row's columns. */}
              <ColumnLayout columns={4} variant="text-grid">
                {metric('Status', `${progressPct}%`, `${completedDocs} of ${queue.totalDocs} documents completed`)}
                {metric('Pending', pendingCount, 'not assigned yet')}
                {metric('Assigned', assignedCount, 'assigned to an Annotator')}
                {metric('Annotators', annotatorCount, 'Annotators with assigned documents')}
                {impact &&
                  metric(
                    'Estimated label accuracy',
                    formatPct(1 - impact.baselineError, estimateDecimals),
                    impact.residualError < impact.baselineError
                      ? `${formatPct(1 - impact.residualError, estimateDecimals)} after the recommended review`
                      : 'reviewing more will refine this',
                  )}
                {impact &&
                  metric(
                    'Evidence',
                    impact.totalObservations.toLocaleString(),
                    /* `prior` means the number comes from other sets; reviewing changes that. */
                    impact.estimateConfidence === 'prior'
                      ? 'measurements — estimate still based on other sets'
                      : `measurements — ${impact.estimateConfidence.replace('-', ' ')} on this set`,
                  )}
              </ColumnLayout>
              {/* Worst-first ordering only ranks the documents inspected so far. */}
              {queue.inspectedDocs != null && queue.inspectedDocs < queue.totalDocs && (
                <Box fontSize="body-s" color="text-body-secondary">
                  Ordering covers the {queue.inspectedDocs} documents examined so far, not all {queue.totalDocs}.
                </Box>
              )}
            </SpaceBetween>
          </Container>
        )}

        {isAdmin && queue && <QueueAnnotatorsTable rows={annotatorRows} loading={eligibleAnnotators == null} />}

        {labelJobRunning && (
          <Alert type="info" header="Draft labeling in progress">
            <SpaceBetween size="xs">
              <Box>
                {queue?.labelJobLabeled ?? 0} of {queue?.labelJobTotal ?? 0} document(s) labeled. Documents appear in the queue as they
                finish — this page refreshes itself, no need to reload.
              </Box>
              {queue?.documents.length === 0 && (
                <Box fontSize="body-s" color="text-body-secondary">
                  Nothing to annotate yet. The first documents usually take a couple of minutes.
                </Box>
              )}
            </SpaceBetween>
          </Alert>
        )}

        {summariesBuilding && (
          <Alert type="info" header="Preparing this queue">
            {`Reading the labels of this test set's documents (${queue?.documents.length ?? 0} of ${queue?.totalDocs ?? 0} ready). This happens once, in the background; this page refreshes itself.`}
          </Alert>
        )}

        {isLoading && !queue && (
          <Box textAlign="center" padding="xl">
            <Spinner /> Loading the queue…
          </Box>
        )}

        {queue && queue.documents.length === 0 && !error && !labelJobRunning && !summariesBuilding && (
          <Alert type="success" header="Queue complete" action={showReviewedControl}>
            Every document in this test set has been reviewed. Open a reviewed document to check or change its labels.
          </Alert>
        )}

        {queue && queue.documents.length > 0 && (
          <Table
            variant="container"
            items={pagedQueue}
            trackBy="objectKey"
            loading={isLoading}
            loadingText="Loading the queue"
            selectionType={isAdmin || claimsWork ? 'multi' : undefined}
            // An Annotator selects documents to claim, so only claimable rows can be picked.
            isItemDisabled={isAdmin ? undefined : (item) => !isClaimable(item)}
            selectedItems={selectedRows}
            onSelectionChange={({ detail }) => setSelectedRows(detail.selectedItems)}
            ariaLabels={{
              selectionGroupLabel: 'Document selection',
              allItemsSelectionLabel: () => 'Select all documents on this page',
              itemSelectionLabel: (_, item) => `Select ${item.objectKey}`,
            }}
            header={
              <Header
                counter={`(${filteredQueue.length})`}
                description={
                  isAdmin
                    ? 'Worst-first: the documents with the most confidence alerts come first. Select documents to assign them to an Annotator.'
                    : claimsWork
                      ? 'Worst-first: the documents with the most confidence alerts come first. Select documents nobody holds and Claim them to work them in Home › Tasks.'
                      : 'Worst-first: the documents with the most confidence alerts come first.'
                }
                actions={
                  claimsWork ? (
                    <Button
                      variant="primary"
                      disabled={claimable.length === 0 || bulkBusy !== null || claimingKey !== null}
                      disabledReason={claimable.length === 0 ? 'Select documents nobody holds yet' : undefined}
                      loading={bulkBusy === 'claim'}
                      onClick={() => void handleBulkClaim()}
                    >
                      {claimable.length > 0 ? `Claim (${claimable.length})` : 'Claim'}
                    </Button>
                  ) : (
                    isAdmin && (
                      <SpaceBetween direction="horizontal" size="xs">
                        <Button
                          disabled={unassignable.length === 0 || bulkBusy !== null}
                          disabledReason={unassignable.length === 0 ? 'Select assigned documents' : undefined}
                          loading={bulkBusy === 'unassign'}
                          onClick={() => void handleUnassign()}
                        >
                          {unassignable.length > 0 ? `Unassign (${unassignable.length})` : 'Unassign'}
                        </Button>
                        <Button
                          variant="primary"
                          disabled={assignable.length === 0 || bulkBusy !== null}
                          disabledReason={assignable.length === 0 ? 'Select documents that still need review' : undefined}
                          onClick={() => void openAssign()}
                        >
                          {assignable.length > 0 ? `Assign to… (${assignable.length})` : 'Assign to…'}
                        </Button>
                      </SpaceBetween>
                    )
                  )
                }
              >
                Documents
              </Header>
            }
            filter={
              <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                <TextFilter
                  filteringText={queueFilter}
                  filteringPlaceholder="Find a document"
                  filteringAriaLabel="Find a document"
                  onChange={({ detail }) => {
                    setQueueFilter(detail.filteringText);
                    setQueuePage(1);
                  }}
                  countText={queueFilter ? `${filteredQueue.length} match${filteredQueue.length === 1 ? '' : 'es'}` : ''}
                />
                <Select
                  selectedOption={STATUS_FILTER_OPTIONS.find((o) => o.value === statusFilter) ?? STATUS_FILTER_OPTIONS[0]}
                  onChange={({ detail }) => {
                    setStatusFilter(detail.selectedOption.value as DocStatusFilter);
                    setQueuePage(1);
                  }}
                  options={STATUS_FILTER_OPTIONS}
                  ariaLabel="Filter by status"
                />
                {showReviewedControl}
              </SpaceBetween>
            }
            columnDefinitions={[
              {
                id: 'document',
                header: 'Document',
                isRowHeader: true,
                cell: (item) => <Link href={`#${testSetItemPath(testSetId, item.objectKey)}`}>{item.objectKey}</Link>,
              },
              { id: 'labels', header: 'Labels', cell: (item) => renderLabelSource(item.labelSource) ?? '—' },
              {
                id: 'confidence',
                header: 'Confidence',
                cell: (item) => renderAlertCount(item.alertCount, item.fieldCount, item.minConfidence, item.confidenceThreshold) ?? '—',
              },
              { id: 'assigned', header: 'Assigned', cell: renderAssigned },
              {
                id: 'actions',
                header: <span className="idp-visually-hidden">Actions</span>,
                width: 60,
                cell: (item) => (
                  <ButtonDropdown
                    variant="inline-icon"
                    ariaLabel={`Actions for ${item.objectKey}`}
                    expandToViewport
                    loading={claimingKey === item.objectKey}
                    items={[
                      { id: 'view', text: 'View' },
                      { id: 'annotate', text: 'Edit' },
                      ...(claimsWork
                        ? [
                            {
                              id: 'claim',
                              text: 'Claim',
                              disabled: !isClaimable(item) || claimingKey !== null || bulkBusy !== null,
                              disabledReason: item.claimedByMe
                                ? 'This document is assigned to you'
                                : item.assignedTo
                                  ? `Assigned to ${item.assignedTo}`
                                  : isDone(item)
                                    ? 'This document is complete'
                                    : claimingKey !== null
                                      ? 'Claiming…'
                                      : undefined,
                            },
                          ]
                        : []),
                      ...(isAdmin
                        ? [
                            { id: 'assign', text: item.assignedTo ? 'Reassign' : 'Assign' },
                            { id: 'delete', text: 'Delete' },
                          ]
                        : []),
                    ]}
                    onItemClick={({ detail }) => onRowAction(item, detail.id)}
                  />
                ),
              },
            ]}
            empty={
              <Box textAlign="center">
                <SpaceBetween size="xs">
                  <Box>
                    {queueFilter
                      ? queueEmptyText(queueFilter)
                      : !queue?.totalDocs
                        ? 'This test set has no documents yet.'
                        : 'No documents match this filter. Choose All documents to see the rest of the set.'}
                  </Box>
                  {/* A search can come up empty because the status filter hides reviewed
                      documents; say so, since the reveal is in the filter bar above. */}
                  {queueFilter && !showReviewed && (
                    <Box fontSize="body-s" color="text-body-secondary">
                      {REVIEWED_HIDDEN_HINT}
                    </Box>
                  )}
                </SpaceBetween>
              </Box>
            }
            pagination={
              queuePageCount > 1 && (
                <Pagination
                  currentPageIndex={queuePage}
                  pagesCount={queuePageCount}
                  onChange={({ detail }) => setQueuePage(detail.currentPageIndex)}
                />
              )
            }
          />
        )}
      </SpaceBetween>
      <Modal
        visible={!!deleting}
        onDismiss={() => setDeleting(null)}
        header="Delete document"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setDeleting(null)}>
                Cancel
              </Button>
              <Button variant="primary" loading={deleteBusy} onClick={() => void confirmDelete()}>
                Delete
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        {deleting ? `Remove ${deleting.objectKey} and its labels from this test set? Published versions are not changed.` : null}
      </Modal>
      <QueueDefinitionModal
        visible={!!settingsTarget}
        queue={null}
        testSet={settingsTarget}
        existingIds={[]}
        annotators={candidates}
        annotatorsLoading={false}
        onDismiss={() => setSettingsTarget(null)}
        onSaved={(_, saved) => {
          setSuccessMessage('Saved the queue settings.');
          setSettingsTarget(null);
          if (saved) setEligibleAnnotators([...(saved.eligibleAnnotators ?? [])]);
          void loadQueue();
        }}
      />
      <AssignModal
        visible={assignOpen}
        count={assignable.length}
        documentLabel={assignable.length === 1 ? assignable[0].objectKey : undefined}
        currentAssignee={sharedAssignee}
        candidates={candidates}
        candidatesLoading={candidatesLoading}
        loading={bulkBusy === 'assign'}
        onDismiss={() => setAssignOpen(false)}
        onConfirm={(email) => void handleAssign(email)}
      />
    </ContentLayout>
  );
};

/** A test set's queue page. An item opens in the edit screen (`TestSetItemEdit`), which the route renders. */
const AnnotationWorkspace = ({ onName }: AnnotationQueuePageProps): React.JSX.Element => <AnnotationQueuePage onName={onName} />;

export default AnnotationWorkspace;
