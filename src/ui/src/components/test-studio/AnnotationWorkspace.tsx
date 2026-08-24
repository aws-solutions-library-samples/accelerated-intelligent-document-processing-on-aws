// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * AnnotationWorkspace — scoped, worst-first annotation queue.
 * Route: /test-studio/sets/:testSetId/annotate
 *
 * The URL is safe to share: it only navigates, and every operation is authorized
 * server-side against the caller's allowedTestSets. Documents are ordered by
 * confidence-alert count so each review removes the most expected error.
 *
 * The annotation surface is the shared GroundTruthVisualEditor, but saves route
 * through completeSectionReview rather than its default direct-to-S3 write: that
 * engages claim-to-lock, tags the label reviewed-human so a later draft-labeling
 * run cannot overwrite it, and feeds the confidence curve the review-effort
 * estimator reads.
 */

import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { useParams, useSearchParams } from 'react-router-dom';
import {
  Alert,
  AppLayout,
  Badge,
  Box,
  BreadcrumbGroup,
  Button,
  Cards,
  ColumnLayout,
  Container,
  ContentLayout,
  CopyToClipboard,
  Flashbar,
  Grid,
  Header,
  ProgressBar,
  Pagination,
  SegmentedControl,
  SpaceBetween,
  Spinner,
  StatusIndicator,
  TextFilter,
} from '@cloudscape-design/components';
import type { FlashbarProps } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from '../../api/client-shim';
import { getAnnotationQueue, claimReview, releaseReview, completeSectionReview, estimateReviewEffort } from '../../graphql/generated';
import useAppContext from '../../contexts/app';
import useSettingsContext from '../../contexts/settings';
import useUserRole from '../../hooks/use-user-role';
import Navigation from '../genaiidp-layout/navigation';
import { appLayoutLabels } from '../common/labels';
import FileViewer from '../document-viewer/FileViewer';
import GroundTruthVisualEditor from './GroundTruthVisualEditor';
import ReviewCelebration from './ReviewCelebration';
import type { TestSetDocumentSectionRef } from './GroundTruthVisualEditor';
import { TEST_STUDIO_PATH, testSetDetailHref, testSetAnnotateHref } from '../../routes/constants';
import { renderAlertCount, renderLabelSource, renderQualityTier } from './TestSetDetail';

const client = generateClient();
const logger = new ConsoleLogger('AnnotationWorkspace');

/** One document in the queue, as returned by getAnnotationQueue. */
export interface QueueItem {
  objectKey: string;
  inputKey: string;
  reviewObjectKey?: string | null;
  minConfidence?: number | null;
  confidenceThreshold?: number | null;
  alertCount?: number | null;
  fieldCount?: number | null;
  labelSource?: string | null;
  sectionCount: number;
  sections?: TestSetDocumentSectionRef[] | null;
  claimedBy?: string | null;
  claimedByMe: boolean;
  reviewStatus?: string | null;
  reviewed: boolean;
  available: boolean;
}

interface QueueState {
  totalDocs: number;
  inspectedDocs?: number | null;
  reviewedDocs: number;
  remainingDocs: number;
  claimedByOthers: number;
  nextObjectKey?: string | null;
  labelJobStatus?: string | null;
  labelJobLabeled?: number | null;
  labelJobTotal?: number | null;
  documents: QueueItem[];
}

type DocView = 'ground-truth' | 'source';

const QUEUE_PAGE_SIZE = 100;

const LABEL_JOB_POLL_MS = 5000;

const formatPct = (fraction: number): string => (Number.isFinite(fraction) ? `${(fraction * 100).toFixed(1)}%` : '—');

/** Rows per page in the queue rail. */
const QUEUE_ROWS_PER_PAGE = 20;

const AnnotationWorkspace = (): React.JSX.Element => {
  const { testSetId } = useParams<{ testSetId: string }>();
  // ?doc= preselects one document, so a per-row Annotate link opens the queue on
  // that document.
  const [searchParams] = useSearchParams();
  const requestedDoc = searchParams.get('doc');
  const { navigationOpen, setNavigationOpen } = useAppContext();
  const { settings } = useSettingsContext();
  const { canAnnotate, isAnnotatorOnly, loading: roleLoading } = useUserRole();
  const testSetBucket = (settings as Record<string, unknown>).TestSetBucket as string | undefined;

  const [queue, setQueue] = useState<QueueState | null>(null);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [claimWarning, setClaimWarning] = useState<string | null>(null);
  const [isConfirming, setIsConfirming] = useState(false);
  const [isClaiming, setIsClaiming] = useState(false);
  const [railCollapsed, setRailCollapsed] = useState(false);
  const [queueFilter, setQueueFilter] = useState('');
  const [queuePage, setQueuePage] = useState(1);
  const [docView, setDocView] = useState<DocView>('ground-truth');
  const [flashItems, setFlashItems] = useState<FlashbarProps.MessageDefinition[]>([]);
  // Incremented on each completed document to fire the confetti burst.
  const [celebration, setCelebration] = useState(0);
  // Reviewed documents are hidden by default so the queue shows only outstanding
  // work; this reopens them so a confirmed label can be re-checked or changed.
  const [showReviewed, setShowReviewed] = useState(false);

  /**
   * What the review is buying, refreshed as documents are completed.
   *
   * Review never rewrites a field's confidence — that is the model's own
   * assessment and the calibration curve reads it as an observation. What review
   * improves is the estimate: residual error falls and estimateConfidence moves
   * off `prior` as the curve learns.
   */
  const [impact, setImpact] = useState<{
    baselineError: number;
    residualError: number;
    estimateConfidence: string;
    qualityTier?: string | null;
    qualityTierReason?: string | null;
    totalObservations: number;
  } | null>(null);

  const loadImpact = useCallback(async () => {
    if (!testSetId) return;
    try {
      const response = await client.graphql({
        query: estimateReviewEffort,
        variables: { testSetId },
      });
      const est = response.data?.estimateReviewEffort;
      if (!est) return;
      setImpact({
        baselineError: est.baselineError ?? 0,
        residualError: est.residualError ?? 0,
        estimateConfidence: est.estimateConfidence ?? 'prior',
        qualityTier: est.qualityTier,
        qualityTierReason: est.qualityTierReason,
        totalObservations: est.calibration?.totalObservations ?? 0,
      });
    } catch (err) {
      // Best-effort: the queue is fully usable without this panel.
      logger.debug('Could not load review impact:', err);
    }
  }, [testSetId]);

  const loadQueue = useCallback(
    async (preserveSelection = true) => {
      if (!testSetId) return;
      setIsLoading(true);
      setError(null);
      try {
        const response = await client.graphql({
          query: getAnnotationQueue,
          variables: { testSetId, limit: QUEUE_PAGE_SIZE, includeCompleted: showReviewed },
        });
        const data = response.data?.getAnnotationQueue as QueueState | null;
        if (!data) {
          setError('The annotation queue could not be loaded.');
          return;
        }
        setQueue(data);
        // Precedence: the document already being worked, then ?doc=, then the
        // first one the server says this caller can take.
        setSelectedKey((current) => {
          if (preserveSelection && current && data.documents.some((d) => d.objectKey === current)) {
            return current;
          }
          if (requestedDoc && data.documents.some((d) => d.objectKey === requestedDoc)) {
            return requestedDoc;
          }
          return data.nextObjectKey ?? data.documents.find((d) => d.available)?.objectKey ?? null;
        });
      } catch (err) {
        logger.error('Error loading annotation queue:', err);
        // A scope denial is the expected failure for an unassigned annotator, so
        // it gets actionable copy rather than "please try again".
        const message = String((err as { errors?: { message?: string }[] })?.errors?.[0]?.message ?? err);
        setError(
          message.includes('Unauthorized')
            ? 'You are not assigned to this test set. Ask the person who shared this link to assign it to your account.'
            : 'Failed to load the annotation queue. Please try again.',
        );
      } finally {
        setIsLoading(false);
      }
    },
    [testSetId, requestedDoc, showReviewed],
  );

  useEffect(() => {
    loadQueue(false);
    loadImpact();
  }, [loadQueue, loadImpact]);

  const labelJobRunning = queue?.labelJobStatus === 'RUNNING';
  const [pollTick, setPollTick] = useState(0);

  /**
   * Poll while draft labeling runs. Labels are harvested on read, so polling is
   * what advances the job; an annotator with no other poller open would otherwise
   * watch a queue that never fills.
   *
   * Keyed on an explicit tick, not the labeled count: a long run reports the same
   * count for minutes, which would stop re-arming the timer.
   */
  useEffect(() => {
    if (!labelJobRunning) return undefined;
    const timer = setTimeout(async () => {
      await loadQueue(true);
      setPollTick((n) => n + 1);
    }, LABEL_JOB_POLL_MS);
    return () => clearTimeout(timer);
  }, [labelJobRunning, pollTick, loadQueue]);

  const selected = useMemo(() => queue?.documents.find((d) => d.objectKey === selectedKey) ?? null, [queue, selectedKey]);

  /**
   * Select a document for viewing. Deliberately does NOT claim it: browsing the
   * queue must not lock documents away from teammates.
   */
  const selectDocument = useCallback((item: QueueItem) => {
    setClaimWarning(null);
    setSelectedKey(item.objectKey);
  }, []);

  /** Take ownership so no one else edits this document at the same time. */
  const claimSelected = useCallback(async () => {
    if (!selected?.reviewObjectKey) return;
    setClaimWarning(null);
    setIsClaiming(true);
    try {
      await client.graphql({ query: claimReview, variables: { objectKey: selected.reviewObjectKey } });
      await loadQueue(true);
    } catch (err) {
      const message = String((err as { errors?: { message?: string }[] })?.errors?.[0]?.message ?? err);
      logger.warn('Could not claim document:', message);
      // Losing a race is normal in a shared queue, not an error state.
      setClaimWarning(
        message.includes('already claimed')
          ? `${selected.objectKey} was just claimed by someone else. Pick another document from the queue.`
          : `Could not claim ${selected.objectKey}: ${message}`,
      );
      await loadQueue(false);
    } finally {
      setIsClaiming(false);
    }
  }, [selected, loadQueue]);

  /**
   * Give a claim back without completing the review, so an abandoned claim does
   * not block the document for everyone else.
   */
  const releaseSelected = useCallback(async () => {
    if (!selected?.reviewObjectKey) return;
    setClaimWarning(null);
    setIsClaiming(true);
    try {
      await client.graphql({ query: releaseReview, variables: { objectKey: selected.reviewObjectKey } });
      await loadQueue(true);
    } catch (err) {
      logger.error('Could not release document:', err);
      const message = (err as { errors?: { message?: string }[] })?.errors?.[0]?.message;
      setClaimWarning(message || `Could not release ${selected.objectKey}.`);
    } finally {
      setIsClaiming(false);
    }
  }, [selected, loadQueue]);

  /**
   * Persist a reviewed section through the review API instead of the editor's
   * default direct-S3 write, so the save claims, tags the label reviewed-human,
   * records provenance, and feeds the confidence curve.
   */
  const handleSave = useCallback(
    async (sectionId: string, data: Record<string, unknown>) => {
      if (!selected?.reviewObjectKey) {
        throw new Error('This document has no review record yet — generate draft labels for the test set first.');
      }
      await client.graphql({
        query: completeSectionReview,
        variables: {
          objectKey: selected.reviewObjectKey,
          sectionId,
          editedData: JSON.stringify(data),
        },
      });
    },
    [selected],
  );

  const advanceToNext = useCallback(async () => {
    const current = selectedKey;
    await loadQueue(false);
    loadImpact();
    setQueue((data) => {
      if (data) {
        const next = data.documents.find((d) => d.available && d.objectKey !== current);
        setSelectedKey(next?.objectKey ?? null);
      }
      return data;
    });
  }, [loadQueue, loadImpact, selectedKey]);

  /**
   * Confirm the draft labels are already correct, with no edits. "No changes
   * needed" is a verdict, not an absence of one: submitting each section
   * unchanged marks it reviewed, tags the labels reviewed-human, and gives the
   * calibration curve its correct-at-this-confidence signal, which only ever
   * arrives from a reviewer agreeing.
   */
  const handleConfirmCorrect = useCallback(async () => {
    if (!selected?.reviewObjectKey) return;
    setIsConfirming(true);
    setError(null);
    try {
      const sections = selected.sections ?? [];
      for (const section of sections) {
        // Sequential, not parallel: these all mutate the same document record and
        // the review API does not support concurrent section updates on one object.

        await client.graphql({
          query: completeSectionReview,
          variables: { objectKey: selected.reviewObjectKey, sectionId: section.sectionId },
        });
      }
      setFlashItems([
        {
          type: 'success',
          content: `${selected.objectKey} confirmed as correct and marked reviewed.`,
          dismissible: true,
          onDismiss: () => setFlashItems([]),
          id: 'annotation-confirmed',
        },
      ]);
      setCelebration((n) => n + 1);
      await advanceToNext();
    } catch (err) {
      logger.error('Error confirming labels:', err);
      const message = (err as { errors?: { message?: string }[] })?.errors?.[0]?.message;
      setError(message || 'Could not mark this document reviewed. Please try again.');
    } finally {
      setIsConfirming(false);
    }
  }, [selected, advanceToNext]);

  const handleSaved = useCallback(
    (baselineKey: string) => {
      setFlashItems([
        {
          type: 'success',
          content: `Saved. ${baselineKey.split('/').pop() ?? ''} is now marked reviewed.`,
          dismissible: true,
          onDismiss: () => setFlashItems([]),
          id: 'annotation-saved',
        },
      ]);
      setCelebration((n) => n + 1);
      advanceToNext();
    },
    [advanceToNext],
  );

  const filteredQueue = useMemo(() => {
    const all = queue?.documents ?? [];
    if (!queueFilter.trim()) return all;
    const needle = queueFilter.trim().toLowerCase();
    return all.filter((d) => d.objectKey.toLowerCase().includes(needle));
  }, [queue, queueFilter]);

  const queuePageCount = Math.max(1, Math.ceil(filteredQueue.length / QUEUE_ROWS_PER_PAGE));
  const pagedQueue = useMemo(() => {
    const start = (queuePage - 1) * QUEUE_ROWS_PER_PAGE;
    return filteredQueue.slice(start, start + QUEUE_ROWS_PER_PAGE);
  }, [filteredQueue, queuePage]);

  const progressPct = queue && queue.totalDocs > 0 ? Math.round((queue.reviewedDocs / queue.totalDocs) * 100) : 0;

  const queueLink = `${window.location.origin}/${testSetAnnotateHref(testSetId ?? '')}`;

  /**
   * Per-document actions live in the editor pane's header, not below it: on a long
   * document a footer button is below the fold.
   */
  const documentActions = selected && (
    <SpaceBetween direction="horizontal" size="xs">
      <Button onClick={advanceToNext} disabled={isLoading}>
        Skip to next document
      </Button>
      {selected.reviewObjectKey && !selected.claimedByMe && !selected.reviewed && (
        <Button variant="primary" onClick={claimSelected} loading={isClaiming} disabled={isLoading || Boolean(selected.claimedBy)}>
          {selected.claimedBy ? `Claimed by ${selected.claimedBy}` : 'Claim this document'}
        </Button>
      )}
      {/* Claimed state is a separate, differently-styled button rather than the
          same one relabelled, so the claim reads at a glance. */}
      {selected.claimedByMe && (
        <Button iconName="check" onClick={releaseSelected} loading={isClaiming} disabled={isLoading}>
          Claimed by you — release
        </Button>
      )}
      {/* Skipping advances the cursor without marking anything reviewed, so a
          correct document needs this to ever leave the queue. */}
      <Button variant="primary" onClick={handleConfirmCorrect} loading={isConfirming} disabled={isLoading || !selected.reviewObjectKey}>
        {selected.reviewed ? 'Re-confirm labels' : 'Labels are correct — mark reviewed'}
      </Button>
    </SpaceBetween>
  );

  const content = (
    <ContentLayout
      header={
        <SpaceBetween size="xs">
          {/* Annotator-only users get no breadcrumb trail: it links to pages they
              cannot open. */}
          {!isAnnotatorOnly && (
            <BreadcrumbGroup
              items={[
                { text: 'Test Studio', href: `#${TEST_STUDIO_PATH}?tab=sets` },
                { text: testSetId ?? '', href: testSetDetailHref(testSetId ?? '') },
                { text: 'Annotate', href: '' },
              ]}
            />
          )}
          <Header
            variant="h1"
            description="Review the documents with the most confidence alerts first — each one you correct removes the most likely errors."
            actions={
              !isAnnotatorOnly && (
                <CopyToClipboard
                  variant="button"
                  copyButtonText="Copy queue link"
                  textToCopy={queueLink}
                  copySuccessText="Queue link copied — share it with an assigned annotator"
                  copyErrorText="Could not copy the queue link"
                />
              )
            }
          >
            Annotate: {testSetId}
          </Header>
          {showReviewed && (
            <Alert type="info" action={<Button onClick={() => setShowReviewed(false)}>Hide reviewed</Button>}>
              Showing documents that have already been reviewed. Re-confirming one records the review again.
            </Alert>
          )}
          {/* The full link is rendered selectable alongside the copy button so the
              sharer can verify the URL before pasting it. */}
          {!isAnnotatorOnly && (
            <CopyToClipboard
              variant="inline"
              textToCopy={queueLink}
              copySuccessText="Queue link copied"
              copyErrorText="Could not copy the queue link"
            />
          )}
        </SpaceBetween>
      }
    >
      <SpaceBetween size="l">
        {!testSetBucket && <Alert type="error">TestSetBucket is not configured in settings.</Alert>}
        {error && <Alert type="error">{error}</Alert>}
        {claimWarning && (
          <Alert type="warning" dismissible onDismiss={() => setClaimWarning(null)}>
            {claimWarning}
          </Alert>
        )}

        {queue && (
          <Container>
            <SpaceBetween size="s">
              <ProgressBar
                value={progressPct}
                label="Team progress"
                description="Shared across everyone annotating this test set"
                additionalInfo={
                  `${queue.reviewedDocs} of ${queue.totalDocs} documents reviewed` +
                  (queue.claimedByOthers > 0 ? ` · ${queue.claimedByOthers} in progress by others` : '')
                }
              />
              {impact && (
                <ColumnLayout columns={3} variant="text-grid">
                  <div>
                    <Box variant="awsui-key-label">Estimated label accuracy</Box>
                    <Box fontSize="heading-m">{formatPct(1 - impact.baselineError)}</Box>
                    <Box fontSize="body-s" color="text-body-secondary">
                      {impact.residualError < impact.baselineError
                        ? `${formatPct(1 - impact.residualError)} after the recommended review`
                        : 'reviewing more will refine this'}
                    </Box>
                  </div>
                  <div>
                    <Box variant="awsui-key-label">Evidence</Box>
                    <Box fontSize="heading-m">{impact.totalObservations.toLocaleString()}</Box>
                    <Box fontSize="body-s" color="text-body-secondary">
                      {/* `prior` means the number comes from other sets, not this
                          one; reviewing is what changes that. */}
                      {impact.estimateConfidence === 'prior'
                        ? 'measurements — estimate still based on other sets'
                        : `measurements — ${impact.estimateConfidence.replace('-', ' ')} on this set`}
                    </Box>
                  </div>
                  <div>
                    <Box variant="awsui-key-label">Quality</Box>
                    {/* Shared renderer with the Test Sets list: one vocabulary and
                        color map, so a set cannot appear to change tier between
                        screens. */}
                    {renderQualityTier(impact.qualityTier, impact.qualityTierReason, 1 - impact.baselineError)}
                  </div>
                </ColumnLayout>
              )}

              {/* Worst-first ordering only ranks the documents inspected so far;
                  say so when that is a subset. */}
              {queue.inspectedDocs != null && queue.inspectedDocs < queue.totalDocs && (
                <Box fontSize="body-s" color="text-body-secondary">
                  Ordering covers the {queue.inspectedDocs} documents examined so far, not all {queue.totalDocs}.
                </Box>
              )}
            </SpaceBetween>
          </Container>
        )}

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

        {isLoading && !queue && (
          <Box textAlign="center" padding="xl">
            <Spinner /> Loading your queue…
          </Box>
        )}

        {queue && queue.documents.length === 0 && !error && !labelJobRunning && (
          <Alert
            type="success"
            header="Queue complete"
            action={!showReviewed && <Button onClick={() => setShowReviewed(true)}>Show reviewed documents</Button>}
          >
            Every document in this test set has been reviewed. Reopen a document to check or change a label you already confirmed.
          </Alert>
        )}

        {queue && queue.documents.length > 0 && testSetBucket && (
          <Grid
            gridDefinition={
              railCollapsed
                ? [{ colspan: { default: 12, m: 1 } }, { colspan: { default: 12, m: 11 } }]
                : [{ colspan: { default: 12, m: 3 } }, { colspan: { default: 12, m: 9 } }]
            }
          >
            <Container
              header={
                <Header
                  variant="h3"
                  counter={railCollapsed ? undefined : `(${filteredQueue.length})`}
                  actions={
                    <Button
                      variant="inline-icon"
                      iconName={railCollapsed ? 'angle-right' : 'angle-left'}
                      ariaLabel={railCollapsed ? 'Expand review queue' : 'Collapse review queue'}
                      onClick={() => setRailCollapsed((v) => !v)}
                    />
                  }
                >
                  {railCollapsed ? '' : 'Review queue'}
                </Header>
              }
            >
              {railCollapsed ? (
                <Box fontSize="body-s" color="text-body-secondary" textAlign="center">
                  {filteredQueue.length}
                </Box>
              ) : (
                <SpaceBetween size="s">
                  <TextFilter
                    filteringText={queueFilter}
                    filteringPlaceholder="Find a document"
                    onChange={({ detail }) => {
                      setQueueFilter(detail.filteringText);
                      setQueuePage(1);
                    }}
                    countText={queueFilter ? `${filteredQueue.length} match${filteredQueue.length === 1 ? '' : 'es'}` : ''}
                  />
                  <Cards
                    items={pagedQueue}
                    trackBy="objectKey"
                    selectionType="single"
                    selectedItems={selected ? [selected] : []}
                    onSelectionChange={({ detail }) => {
                      const item = detail.selectedItems[0];
                      if (item) selectDocument(item);
                    }}
                    // Reviewed items are selectable when explicitly shown, so a
                    // confirmed label can be re-checked or corrected.
                    isItemDisabled={(item) => item.reviewed && !showReviewed}
                    cardDefinition={{
                      header: (item) => (
                        <Box fontSize="body-s" fontWeight="bold">
                          {item.objectKey}
                        </Box>
                      ),
                      sections: [
                        {
                          id: 'meta',
                          content: (item) => (
                            <SpaceBetween direction="horizontal" size="xxs">
                              {/* Alerts first: the queue is ordered by this. */}
                              {renderAlertCount(item.alertCount, item.fieldCount, item.minConfidence, item.confidenceThreshold)}
                              {renderLabelSource(item.labelSource)}
                            </SpaceBetween>
                          ),
                        },
                        {
                          id: 'claim',
                          content: (item) => {
                            if (item.reviewed) return <StatusIndicator type="success">Reviewed</StatusIndicator>;
                            if (item.claimedByMe) return <Badge color="blue">You have this</Badge>;
                            if (item.claimedBy) return <StatusIndicator type="in-progress">{item.claimedBy}</StatusIndicator>;
                            // A missing review key means nothing to claim, but the
                            // reason differs: an unlabeled document needs a labeling
                            // run, authored ground truth needs nothing. Keying only
                            // on the missing key would label both "Ground truth" and
                            // contradict the Unlabeled badge above.
                            if (!item.reviewObjectKey) {
                              const isUnlabeled = !item.labelSource;
                              return (
                                <Box fontSize="body-s" color="text-body-secondary">
                                  {isUnlabeled ? 'Not labeled yet — generate draft labels first' : 'Ground truth — nothing to review'}
                                </Box>
                              );
                            }
                            return null;
                          },
                        },
                      ],
                    }}
                    cardsPerRow={[{ cards: 1 }]}
                    empty={<Box textAlign="center">No documents to review.</Box>}
                  />
                  {queuePageCount > 1 && (
                    <Pagination
                      currentPageIndex={queuePage}
                      pagesCount={queuePageCount}
                      onChange={({ detail }) => setQueuePage(detail.currentPageIndex)}
                    />
                  )}
                </SpaceBetween>
              )}
            </Container>

            <SpaceBetween size="s">
              <Header variant="h3" actions={documentActions}>
                {selected ? selected.objectKey : 'No document selected'}
              </Header>
              <SegmentedControl
                selectedId={docView}
                onChange={({ detail }) => setDocView(detail.selectedId as DocView)}
                options={[
                  { id: 'ground-truth', text: 'Annotate' },
                  { id: 'source', text: 'View source document' },
                ]}
              />
              {!selected && <Alert type="info">Choose a document from the queue to start.</Alert>}
              {selected && !selected.reviewObjectKey && (
                <Alert type="warning" header="Not ready to annotate">
                  This test set has no labeling run yet, so there is nothing to claim or review. Generate draft labels for the set first.
                </Alert>
              )}
              {selected && docView === 'source' && <FileViewer objectKey={selected.inputKey} bucket={testSetBucket} presignVia="server" />}
              {selected && docView === 'ground-truth' && (
                <GroundTruthVisualEditor
                  key={selected.objectKey}
                  bucket={testSetBucket}
                  inputKey={selected.inputKey}
                  objectKey={selected.objectKey}
                  sections={selected.sections ?? []}
                  isReadOnly={!canAnnotate || !selected.reviewObjectKey}
                  onSave={handleSave}
                  onSaved={handleSaved}
                  saveButtonText="Save & next in queue"
                  testSetId={testSetId}
                  onReextracted={() => loadQueue(false)}
                />
              )}
            </SpaceBetween>
          </Grid>
        )}
      </SpaceBetween>
    </ContentLayout>
  );

  if (!roleLoading && !canAnnotate) {
    return (
      <AppLayout
        headerSelector="#top-navigation"
        ariaLabels={appLayoutLabels}
        navigation={<Navigation />}
        navigationOpen={navigationOpen}
        onNavigationChange={({ detail }) => setNavigationOpen(detail.open)}
        toolsHide
        content={
          <ContentLayout header={<Header variant="h1">Annotate</Header>}>
            <Alert type="error" header="Not available for your account">
              Ground-truth annotation requires an Annotator, Author or Admin role.
            </Alert>
          </ContentLayout>
        }
      />
    );
  }

  return (
    <>
      <ReviewCelebration trigger={celebration} />
      <AppLayout
        headerSelector="#top-navigation"
        ariaLabels={appLayoutLabels}
        navigation={<Navigation />}
        navigationOpen={navigationOpen}
        onNavigationChange={({ detail }) => setNavigationOpen(detail.open)}
        toolsHide
        notifications={<Flashbar items={flashItems} />}
        content={content}
      />
    </>
  );
};

export default AnnotationWorkspace;
