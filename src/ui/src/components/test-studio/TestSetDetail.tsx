// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * TestSetDetail — list a test set's documents (route: /test-studio/sets/:testSetId).
 *
 * Mirrors the Document List -> Document Details structure of the main app:
 * each row links to the TestSetDocumentDetail page, with per-row quick
 * actions ("View Source" / "Ground Truth") that deep-link straight to the
 * corresponding view on that page.
 */

import React, { useState, useEffect, useCallback } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  Alert,
  AppLayout,
  Badge,
  Box,
  BreadcrumbGroup,
  Button,
  ContentLayout,
  ExpandableSection,
  FormField,
  Header,
  Icon,
  Input,
  Link,
  Modal,
  Pagination,
  Popover,
  SpaceBetween,
  StatusIndicator,
  Table,
  TextFilter,
} from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from '../../api/client-shim';
import { getTestSetDocuments, generateDraftLabels, getDraftLabelJob, clearDraftLabels, resetTestSetLabels } from '../../graphql/generated';
import useAppContext from '../../contexts/app';
import useSettingsContext from '../../contexts/settings';
import useUserRole from '../../hooks/use-user-role';
import Navigation from '../genaiidp-layout/navigation';
import { appLayoutLabels } from '../common/labels';
import { TEST_STUDIO_PATH, testSetDocumentHref, testSetAnnotateHref } from '../../routes/constants';
import TestDocThumbnail from './TestDocThumbnail';
import ReviewEffortModal from './ReviewEffortModal';
import GenerateDraftLabelsModal from './GenerateDraftLabelsModal';
import type { TestSetDocumentSectionRef } from './GroundTruthVisualEditor';

const client = generateClient();
const logger = new ConsoleLogger('TestSetDetail');

const PAGE_SIZE = 50;

export interface TestSetDocumentItem {
  objectKey: string;
  inputKey: string;
  size?: number | null;
  lastModified?: string | null;
  sections: TestSetDocumentSectionRef[];
  labelSource?: string | null;
  minConfidence?: number | null;
  confidenceThreshold?: number | null;
  alertCount?: number | null;
  fieldCount?: number | null;
}

/**
 * Shared label-provenance vocabulary. Machine-drafted labels must never render
 * like human-verified ones, so every surface that shows labels uses this map.
 */
export const LABEL_SOURCE_BADGES: Record<string, { color: 'blue' | 'green' | 'grey' | 'severity-neutral'; text: string }> = {
  'draft-machine': { color: 'blue', text: 'Draft (machine)' },
  'reviewed-human': { color: 'green', text: 'Reviewed (human)' },
  synthetic: { color: 'grey', text: 'Synthetic' },
  uploaded: { color: 'grey', text: 'Uploaded' },
};

export const renderLabelSource = (labelSource?: string | null): React.JSX.Element => {
  if (!labelSource) return <Badge color="severity-neutral">Unlabeled</Badge>;
  const badge = LABEL_SOURCE_BADGES[labelSource];
  return badge ? <Badge color={badge.color}>{badge.text}</Badge> : <Badge color="grey">{labelSource}</Badge>;
};

/**
 * Provenance for a baseline whose bytes are already loaded.
 *
 * `renderLabelSource(undefined)` renders "Unlabeled", which is correct for a
 * document row where the baseline may not exist at all. It is wrong once the file
 * has been read: the pipeline writes `labelSource` and a hand-uploaded ground-truth
 * file does not, so absence in a loaded baseline means authored ground truth.
 *
 * The server already codifies this — `_attach_label_metadata` falls back to
 * `uploaded` for exactly this reason — and the editor did not, so the same document
 * read "Uploaded" in the review queue and "Unlabeled" in the editor header, two
 * lines below an alert saying "Already ground truth".
 */
export const renderLoadedLabelSource = (labelSource?: string | null): React.JSX.Element => renderLabelSource(labelSource || 'uploaded');

/**
 * Confidence as a percentage, colored against the configured alert threshold: red
 * below it, amber within 10 points above it, otherwise plain. Fixed bands would
 * contradict the assessment config, where whether 0.85 passes depends on the
 * threshold. The 80% default applies only when the result carries no threshold.
 */
const DEFAULT_CONFIDENCE_THRESHOLD_PCT = 80;
const NEAR_THRESHOLD_MARGIN_PCT = 10;

export const renderConfidence = (value?: number | null, threshold?: number | null): React.JSX.Element | string => {
  if (value === null || value === undefined) return '-';
  const pct = value <= 1 ? value * 100 : value;
  const rawThreshold = threshold ?? null;
  const thresholdPct = rawThreshold === null ? DEFAULT_CONFIDENCE_THRESHOLD_PCT : rawThreshold <= 1 ? rawThreshold * 100 : rawThreshold;
  const below = pct < thresholdPct;
  const near = !below && pct < thresholdPct + NEAR_THRESHOLD_MARGIN_PCT;
  const color = below ? 'text-status-error' : near ? 'text-status-warning' : 'text-status-success';
  return (
    <Box color={color} fontWeight={below ? 'bold' : 'normal'}>
      {pct.toFixed(1)}%
    </Box>
  );
};

/**
 * A test set's label quality, led by the estimated accuracy. The number is the
 * primary signal and the tier is shorthand for it: a bare "Gold" badge reads as a
 * certification claim, so the percentage is always shown alongside.
 *
 * No tier uses bare `grey`, which renders near-black in Cloudscape and makes the
 * weakest tier look like the emphatic one.
 */
export const QUALITY_TIER_COLORS: Record<string, 'green' | 'blue' | 'severity-neutral' | 'red'> = {
  gold: 'green',
  silver: 'blue',
  bronze: 'severity-neutral',
  // NOT red. "Unrated" is the absence of a defensible claim, not a fault — and
  // reviewing a document can legitimately move a set INTO it: once there is
  // enough evidence to test whether confidence ranks correctness, the estimator
  // may find it does not, and withdraw the number it had been inferring from a
  // cross-set prior. Red made that read as "your review broke this set", when in
  // fact the preceding Bronze figure was the less honest of the two states.
  unrated: 'severity-neutral',
};

/**
 * What the four tiers mean, all of them at once.
 *
 * The per-row popover explains only the tier that row happens to have, so a set
 * badged "Bronze" told you Bronze was bad without telling you what better looked
 * like or how to get there. Mirrors TIER_EXPLANATIONS in
 * `idp_common/evaluation/confidence_curve.py` — if the thresholds move there,
 * they move here.
 */
export const LabelAccuracyLegend = (): React.JSX.Element => (
  <Popover
    dismissButton={false}
    position="bottom"
    size="large"
    triggerType="custom"
    header="Estimated label accuracy"
    content={
      <SpaceBetween size="xs">
        <Box variant="span" fontSize="body-s">
          An estimate of how often these labels are right, inferred from the confidence scores of the run that produced them. It is not a
          measurement against a known answer — a tier is earned from evidence on this set, never asserted.
        </Box>
        <Box variant="span" fontSize="body-s">
          <Badge color="green">Gold</Badge> — at least 99%, measured on this set rather than extrapolated.
        </Box>
        <Box variant="span" fontSize="body-s">
          <Badge color="blue">Silver</Badge> — at least 95%, with the confidence curve at least partly measured here.
        </Box>
        <Box variant="span" fontSize="body-s">
          <Badge color="severity-neutral">Bronze</Badge> — below 95%, or still estimated from a cross-set prior. Review or score the set to
          earn a higher tier.
        </Box>
        <Box variant="span" fontSize="body-s">
          <Badge color="severity-neutral">Unrated</Badge> — confidence does not rank errors on this set, so no accuracy claim is defensible.
          Reviewing only a subset would not be meaningful.
        </Box>
        <Box variant="span" fontSize="body-s" color="text-body-secondary">
          A set whose labels you uploaded or authored by hand is the reference other runs are scored against, so a low figure here is a
          statement about the confidence data behind the estimate, not about those labels. Where no estimate exists at all, the column says
          so rather than implying one.
        </Box>
      </SpaceBetween>
    }
  >
    <Box variant="span" fontSize="body-s" color="text-status-info">
      Est. label accuracy <Icon name="status-info" size="small" />
    </Box>
  </Popover>
);

/**
 * The accuracy cell, including the cases where there is no estimate to show.
 *
 * Separate from `renderQualityTier` because the honest answer depends on WHY the
 * estimate is missing, and only the caller knows: a machine-drafted set with no
 * curve yet is genuinely unassessed, whereas a set of human ground truth has
 * nothing to assess. Rendering both as "-" inverted the trust signal — the
 * authored set, which is the reference, looked worse than the draft that was
 * being measured against it.
 */
export const renderLabelAccuracy = (
  entry?: { tier?: string | null; reason?: string | null; accuracy?: number | null } | null,
  labelState?: string | null,
  isEstimating?: boolean,
): React.JSX.Element => {
  if (entry?.tier) return renderQualityTier(entry.tier, entry.reason, entry.accuracy);

  // Estimates arrive one request per set, after the table has already rendered.
  // Without this the column asserted a verdict for the second or two the calls
  // were in flight, then replaced it with a percentage — caught on a live stack,
  // where a 2000-document set flashed "Ground truth" before settling on
  // "76.1% est. Bronze".
  if (isEstimating) return <StatusIndicator type="loading">Estimating</StatusIndicator>;

  if (labelState === 'labeled') {
    return (
      <Popover
        dismissButton={false}
        position="top"
        size="medium"
        triggerType="custom"
        content={
          <Box variant="span" fontSize="body-s">
            No accuracy estimate was returned for this set. Its labels are the reference other runs are scored against, so the absence is
            not a low rating — there is simply no confidence data here to infer a figure from.
          </Box>
        }
      >
        <Badge color="green">Ground truth</Badge>
      </Popover>
    );
  }

  return <Box color="text-status-inactive">Not assessed yet</Box>;
};

export const renderQualityTier = (
  tier?: string | null,
  reason?: string | null,
  accuracy?: number | null,
  /* Callers that know the estimate is unmeasured pass 0, so the tier's number does
     not contradict a headline rounded for the same reason. Defaults to the existing
     precision, leaving every current caller unchanged. */
  decimals = 1,
): React.JSX.Element => {
  if (!tier) return <Box color="text-status-inactive">-</Box>;
  const label = tier.charAt(0).toUpperCase() + tier.slice(1);
  const detail = (
    <SpaceBetween size="xxs">
      <Box variant="span" fontWeight="bold">
        {label}
      </Box>
      <Box variant="span">{reason || ''}</Box>
    </SpaceBetween>
  );
  return (
    <Popover dismissButton={false} position="top" size="medium" triggerType="custom" content={detail}>
      <SpaceBetween direction="horizontal" size="xxs" alignItems="center">
        {/* Unrated means no accuracy claim is defensible, so don't print one. */}
        {accuracy !== null && accuracy !== undefined && tier !== 'unrated' ? (
          <Box variant="span">{(accuracy * 100).toFixed(decimals)}% est.</Box>
        ) : (
          <Box variant="span" color="text-body-secondary">
            Not rated
          </Box>
        )}
        <Badge color={QUALITY_TIER_COLORS[tier] ?? 'severity-neutral'}>{label}</Badge>
        {/* The reason is the whole content of an unrated verdict, and it is the one
            case where the user cannot guess it — so it is not left to a hover. */}
        {tier === 'unrated' && reason ? (
          <Box variant="span" fontSize="body-s" color="text-body-secondary">
            — {reason}
          </Box>
        ) : null}
      </SpaceBetween>
    </Popover>
  );
};

/**
 * How many fields need a human, as a count rather than a score — the same signal
 * human review uses in the Document List, and a direct measure of the work. The
 * lowest score stays in the popover because the calibration curve is built on it.
 */
export const renderAlertCount = (
  alertCount?: number | null,
  fieldCount?: number | null,
  minConfidence?: number | null,
  threshold?: number | null,
): React.JSX.Element | string => {
  if (alertCount === null || alertCount === undefined) return '-';
  const detail = (
    <SpaceBetween size="xxs">
      <Box variant="span">
        Lowest field confidence: {minConfidence === null || minConfidence === undefined ? '-' : renderConfidence(minConfidence, threshold)}
      </Box>
      <Box variant="span" fontSize="body-s" color="text-body-secondary">
        {threshold === null || threshold === undefined
          ? 'Alerts count fields below the default 80% threshold.'
          : `Alerts count fields below their configured threshold (${((threshold <= 1 ? threshold * 100 : threshold) as number).toFixed(0)}%).`}
      </Box>
    </SpaceBetween>
  );
  return (
    <Popover dismissButton={false} position="top" size="medium" triggerType="custom" content={detail}>
      <Box color={alertCount > 0 ? 'text-status-error' : 'text-status-success'} fontWeight={alertCount > 0 ? 'bold' : 'normal'}>
        {alertCount === 0 ? `None of ${fieldCount ?? 0} fields flagged` : `${alertCount} of ${fieldCount ?? 0} fields flagged`}
      </Box>
    </Popover>
  );
};

/**
 * Where this document stands in the review loop, as distinct from the model's
 * confidence, which review never changes.
 */
export const renderReviewState = (labelSource?: string | null): React.JSX.Element => {
  if (labelSource === 'reviewed-human') {
    return <StatusIndicator type="success">Reviewed</StatusIndicator>;
  }
  if (labelSource === 'draft-machine') {
    return <StatusIndicator type="pending">Awaiting review</StatusIndicator>;
  }
  if (!labelSource) {
    return <StatusIndicator type="info">Unlabeled</StatusIndicator>;
  }
  // Uploaded or generated ground truth: authored, so there is nothing to review.
  return <StatusIndicator type="success">Ground truth</StatusIndicator>;
};

export const formatSize = (size?: number | null): string => {
  if (size === null || size === undefined) return '-';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
};

const TestSetDetail = (): React.JSX.Element => {
  const { testSetId } = useParams<{ testSetId: string }>();
  const navigate = useNavigate();
  const { navigationOpen, setNavigationOpen } = useAppContext();
  const { settings } = useSettingsContext();
  const testSetBucket = (settings as Record<string, unknown>).TestSetBucket as string | undefined;

  const [documents, setDocuments] = useState<TestSetDocumentItem[]>([]);
  // Server pagination: pageTokens[i] is the nextToken that fetches page i+1.
  const [pageTokens, setPageTokens] = useState<(string | null)[]>([null]);
  const [currentPageIndex, setCurrentPageIndex] = useState(1);
  const [hasMore, setHasMore] = useState(false);
  // The set's size, from the server. The page length is not the total.
  const [totalCount, setTotalCount] = useState<number | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filterText, setFilterText] = useState('');
  const [labelJob, setLabelJob] = useState<{
    jobId: string;
    status: string;
    total: number;
    labeled: number;
    skippedAlreadyLabeled?: number | null;
  } | null>(null);
  const [isStartingLabels, setIsStartingLabels] = useState(false);
  const [showEffortModal, setShowEffortModal] = useState(false);
  const [showLabelModal, setShowLabelModal] = useState(false);
  const { isAdmin } = useUserRole();
  const [showClearDraftsModal, setShowClearDraftsModal] = useState(false);
  const [resetConfirmText, setResetConfirmText] = useState('');
  const [isResetting, setIsResetting] = useState(false);
  const [isClearingDrafts, setIsClearingDrafts] = useState(false);
  const [clearedMessage, setClearedMessage] = useState<string | null>(null);
  const [worstFirst, setWorstFirst] = useState(true);

  const fetchPage = useCallback(
    async (pageIndex: number, tokens: (string | null)[]) => {
      if (!testSetId) return;
      setIsLoading(true);
      setError(null);
      try {
        const response = await client.graphql({
          query: getTestSetDocuments,
          variables: {
            testSetId,
            limit: PAGE_SIZE,
            nextToken: tokens[pageIndex - 1] ?? undefined,
          },
        });
        const page = response.data?.getTestSetDocuments;
        setDocuments((page?.documents ?? []) as TestSetDocumentItem[]);
        setHasMore(Boolean(page?.nextToken));
        setTotalCount(page?.totalCount ?? null);
        setPageTokens((prev) => {
          const next = [...prev];
          next[pageIndex] = page?.nextToken ?? null;
          return next;
        });
        // Resume polling a job this session did not start: harvesting happens on
        // read, so an unpolled job stays RUNNING forever.
        if (page?.activeLabelJobId) {
          setLabelJob((current) =>
            current?.jobId === page.activeLabelJobId
              ? current
              : { jobId: page.activeLabelJobId as string, status: 'RUNNING', total: 0, labeled: 0 },
          );
        }
      } catch (err) {
        logger.error('Error loading test set documents:', err);
        setError('Failed to load test set documents. Please try again.');
      } finally {
        setIsLoading(false);
      }
    },
    [testSetId],
  );

  useEffect(() => {
    setPageTokens([null]);
    setCurrentPageIndex(1);
    fetchPage(1, [null]);
  }, [testSetId, fetchPage]);

  const handlePageChange = (pageIndex: number) => {
    setCurrentPageIndex(pageIndex);
    fetchPage(pageIndex, pageTokens);
  };

  const handleResetLabels = async () => {
    setIsResetting(true);
    setError(null);
    try {
      const response = await client.graphql({
        query: resetTestSetLabels,
        variables: { testSetId: testSetId ?? '' },
      });
      setShowClearDraftsModal(false);
      setResetConfirmText('');
      setLabelJob(null);
      setClearedMessage(response.data?.resetTestSetLabels?.lastAddResult ?? 'All labels reset.');
      fetchPage(1, [null]);
      setCurrentPageIndex(1);
    } catch (err) {
      logger.error('Error resetting labels:', err);
      setError('Failed to reset labels. Please try again.');
    } finally {
      setIsResetting(false);
    }
  };

  const handleClearDraftLabels = async () => {
    setIsClearingDrafts(true);
    setError(null);
    try {
      const response = await client.graphql({
        query: clearDraftLabels,
        variables: { testSetId: testSetId ?? '' },
      });
      setShowClearDraftsModal(false);
      // The job pointer is gone server-side, so drop the local banner with it.
      setLabelJob(null);
      setClearedMessage(response.data?.clearDraftLabels?.lastAddResult ?? 'Draft labels cleared.');
      fetchPage(1, [null]);
      setCurrentPageIndex(1);
    } catch (err) {
      logger.error('Error clearing draft labels:', err);
      setError('Failed to clear draft labels. Please try again.');
    } finally {
      setIsClearingDrafts(false);
    }
  };

  const handleGenerateDraftLabels = async (configVersion?: string, objectKeys?: string[], configRevision?: number) => {
    if (!testSetId) return;
    setIsStartingLabels(true);
    setError(null);
    try {
      const response = await client.graphql({
        query: generateDraftLabels,
        variables: { input: { testSetId, configVersion, objectKeys, configRevision } },
      });
      const job = response.data?.generateDraftLabels;
      if (job) {
        setLabelJob({
          jobId: job.jobId,
          status: job.status,
          total: job.total ?? 0,
          labeled: job.labeled ?? 0,
          skippedAlreadyLabeled: job.skippedAlreadyLabeled ?? 0,
        });
        setShowLabelModal(false);
      }
    } catch (err) {
      logger.error('Error starting draft labeling:', err);
      // Surface the server's message: several are deliberate and actionable.
      const message = (err as { errors?: { message?: string }[] })?.errors?.[0]?.message;
      setError(message || 'Failed to start draft labeling. Please try again.');
    } finally {
      setIsStartingLabels(false);
    }
  };

  /**
   * Poll the labeling job while it runs; the resolver harvests finished documents
   * on read, so polling is what advances the job.
   *
   * Keyed on an explicit tick and never on fetchPage/pageTokens: a tick mutates
   * pageTokens, so depending on it would tear the effect down mid-flight and each
   * tick would cancel its own successor.
   */
  const [labelPollTick, setLabelPollTick] = useState(0);
  const [documentsStale, setDocumentsStale] = useState(false);
  const jobId = labelJob?.jobId;
  const jobRunning = labelJob?.status === 'RUNNING';

  useEffect(() => {
    if (!testSetId || !jobId || !jobRunning) return undefined;
    const timer = setTimeout(async () => {
      try {
        const response = await client.graphql({
          query: getDraftLabelJob,
          variables: { testSetId, jobId },
        });
        const job = response.data?.getDraftLabelJob;
        if (job) {
          setLabelJob({
            jobId: job.jobId,
            status: job.status,
            total: job.total ?? 0,
            labeled: job.labeled ?? 0,
            skippedAlreadyLabeled: job.skippedAlreadyLabeled ?? 0,
          });
          setDocumentsStale(true);
          if (job.status === 'FAILED') {
            setError(job.error ? `Draft labeling failed: ${job.error}` : 'Draft labeling failed.');
          }
        }
      } catch (err) {
        logger.error('Error polling draft label job:', err);
      } finally {
        setLabelPollTick((n) => n + 1);
      }
    }, 5000);
    return () => clearTimeout(timer);
  }, [testSetId, jobId, jobRunning, labelPollTick]);

  // Table refresh lives in its own effect to keep fetchPage out of the poll
  // loop's dependencies.
  useEffect(() => {
    if (!documentsStale) return;
    setDocumentsStale(false);
    fetchPage(currentPageIndex, pageTokens);
  }, [documentsStale, currentPageIndex, pageTokens, fetchPage]);

  const filteredDocs = filterText ? documents.filter((d) => d.objectKey.toLowerCase().includes(filterText.toLowerCase())) : documents;

  const hasConfidence = documents.some((d) => d.minConfidence !== null && d.minConfidence !== undefined);
  // Sorts the current page only: pagination is server-side and opaque, so a
  // set-wide ranking is not available here.
  const visibleDocs =
    worstFirst && hasConfidence
      ? [...filteredDocs].sort((a, b) => {
          const av = a.minConfidence ?? Number.POSITIVE_INFINITY;
          const bv = b.minConfidence ?? Number.POSITIVE_INFINITY;
          return av - bv;
        })
      : filteredDocs;

  return (
    <AppLayout
      headerSelector="#top-navigation"
      ariaLabels={appLayoutLabels}
      navigation={<Navigation />}
      navigationOpen={navigationOpen}
      onNavigationChange={({ detail }) => setNavigationOpen(detail.open)}
      toolsHide
      content={
        <ContentLayout
          header={
            <SpaceBetween size="xs">
              <BreadcrumbGroup
                items={[
                  { text: 'Test Studio', href: `#${TEST_STUDIO_PATH}?tab=sets` },
                  { text: testSetId ?? '', href: '' },
                ]}
              />
              <Header variant="h1" description="Browse this test set's documents and view or edit their ground truth">
                Test Set: {testSetId}
              </Header>
            </SpaceBetween>
          }
        >
          <SpaceBetween size="l">
            {error && <Alert type="error">{error}</Alert>}

            {labelJob && labelJob.status === 'RUNNING' && (
              <Alert type="info">
                <StatusIndicator type="in-progress">
                  Draft labeling in progress — {labelJob.labeled} of {labelJob.total} document(s) labeled. Labels appear here as they
                  complete.
                  {labelJob.skippedAlreadyLabeled
                    ? ` Skipping ${labelJob.skippedAlreadyLabeled} document(s) that already have ground truth.`
                    : ''}
                </StatusIndicator>
              </Alert>
            )}

            {clearedMessage && (
              <Alert type="success" dismissible onDismiss={() => setClearedMessage(null)}>
                {clearedMessage} Generate draft labels again to re-label with a different configuration.
              </Alert>
            )}

            {labelJob && labelJob.status === 'COMPLETED' && (
              <Alert type="success" dismissible onDismiss={() => setLabelJob(null)}>
                Draft labeling complete — {labelJob.labeled} document(s) labeled
                {labelJob.skippedAlreadyLabeled ? `, ${labelJob.skippedAlreadyLabeled} skipped (already had ground truth)` : ''}. Review the
                documents with the most confidence alerts first, then publish a version to freeze them as ground truth.
              </Alert>
            )}

            <Table
              header={
                <Header
                  counter={
                    // No count at all while the first page is loading: '(0)' reads
                    // as "this set is empty", which is a statement we cannot make
                    // yet and the one that most misleads.
                    isLoading && filteredDocs.length === 0
                      ? undefined
                      : totalCount !== null && totalCount > filteredDocs.length
                        ? `(${filteredDocs.length} of ${totalCount})`
                        : `(${filteredDocs.length})`
                  }
                  description={
                    hasConfidence
                      ? 'Confidence alerts are the fields below their configured threshold — review the documents with the most first.'
                      : undefined
                  }
                  actions={
                    <SpaceBetween direction="horizontal" size="xs">
                      {hasConfidence && (
                        <Button onClick={() => setWorstFirst((prev) => !prev)}>{worstFirst ? 'Sort by name' : 'Sort worst-first'}</Button>
                      )}
                      <Button
                        iconName="gen-ai"
                        onClick={() => setShowLabelModal(true)}
                        loading={isStartingLabels}
                        disabled={isLoading || labelJob?.status === 'RUNNING'}
                      >
                        Generate draft labels
                      </Button>
                      {/* Routed through the effort estimate rather than straight to
                          the queue, so "how much to review" is decided before
                          committing a team. */}
                      <Button onClick={() => setShowEffortModal(true)} iconName="user-profile">
                        Annotate
                      </Button>
                      {/* Needed because the harvest only replaces a draft when the
                          new run produces a section for it: a run that splits
                          differently leaves orphan sections behind. */}
                      {/* The only action here that destroys work. It sat in the
                          same row as Refresh and Annotate with identical styling,
                          so nothing but the label distinguished it. */}
                      <Button
                        iconName="remove"
                        onClick={() => setShowClearDraftsModal(true)}
                        disabled={isLoading || labelJob?.status === 'RUNNING'}
                      >
                        Clear draft labels
                      </Button>
                      <Button iconName="refresh" onClick={() => fetchPage(currentPageIndex, pageTokens)} disabled={isLoading}>
                        Refresh
                      </Button>
                    </SpaceBetween>
                  }
                >
                  Documents
                </Header>
              }
              columnDefinitions={[
                {
                  id: 'thumbnail',
                  header: 'Preview',
                  cell: (item: TestSetDocumentItem) =>
                    testSetBucket ? <TestDocThumbnail bucket={testSetBucket} inputKey={item.inputKey} /> : null,
                  width: 130,
                },
                {
                  id: 'objectKey',
                  header: 'Document',
                  cell: (item: TestSetDocumentItem) => (
                    <Link href={testSetDocumentHref(testSetId ?? '', item.objectKey)}>{item.objectKey}</Link>
                  ),
                  sortingField: 'objectKey',
                },
                {
                  id: 'labelSource',
                  // Provenance of the extracted field values only — not of class or
                  // split labels, which are separate things.
                  header: 'Extraction labels',
                  cell: (item: TestSetDocumentItem) => renderLabelSource(item.labelSource),
                  sortingField: 'labelSource',
                },
                {
                  id: 'alertCount',
                  header: 'Confidence alerts',
                  cell: (item: TestSetDocumentItem) =>
                    renderAlertCount(item.alertCount, item.fieldCount, item.minConfidence, item.confidenceThreshold),
                  sortingField: 'alertCount',
                },
                {
                  id: 'reviewState',
                  // The only column that moves as annotation progresses; confidence
                  // alerts are the model's own assessment and review never changes
                  // them.
                  header: 'Review state',
                  cell: (item: TestSetDocumentItem) => renderReviewState(item.labelSource),
                  sortingField: 'labelSource',
                },
                {
                  id: 'size',
                  header: 'Size',
                  cell: (item: TestSetDocumentItem) => formatSize(item.size),
                },
                {
                  id: 'lastModified',
                  header: 'Last modified',
                  cell: (item: TestSetDocumentItem) => (item.lastModified ? new Date(item.lastModified).toLocaleString() : '-'),
                },
                {
                  id: 'sections',
                  header: 'GT sections',
                  cell: (item: TestSetDocumentItem) => item.sections.length,
                },
                {
                  id: 'rowActions',
                  header: '',
                  // Explicit width: as the last column with an empty header this
                  // collapses and the label wraps one character per line.
                  width: 120,
                  cell: (item: TestSetDocumentItem) => (
                    <Box variant="span" fontSize="body-s">
                      <Link href={testSetAnnotateHref(testSetId ?? '', item.objectKey)}>Annotate</Link>
                    </Box>
                  ),
                },
              ]}
              items={visibleDocs}
              loading={isLoading}
              loadingText="Loading documents"
              trackBy="inputKey"
              filter={
                <TextFilter
                  filteringText={filterText}
                  filteringPlaceholder="Find documents on this page"
                  onChange={({ detail }) => setFilterText(detail.filteringText)}
                />
              }
              pagination={
                <Pagination
                  currentPageIndex={currentPageIndex}
                  pagesCount={hasMore ? currentPageIndex + 1 : currentPageIndex}
                  openEnd={hasMore}
                  onChange={({ detail }) => handlePageChange(detail.currentPageIndex)}
                />
              }
              empty={
                <Box textAlign="center" color="inherit">
                  <b>No documents</b>
                  <Box variant="p" color="inherit">
                    This test set has no documents{filterText ? ' matching the filter' : ''}.
                  </Box>
                </Box>
              }
            />

            <GenerateDraftLabelsModal
              setTotalCount={totalCount}
              visible={showLabelModal}
              testSetId={testSetId ?? ''}
              submitting={isStartingLabels}
              onDismiss={() => setShowLabelModal(false)}
              onSubmit={handleGenerateDraftLabels}
            />

            <ReviewEffortModal
              visible={showEffortModal}
              testSetId={testSetId ?? ''}
              onDismiss={() => setShowEffortModal(false)}
              onContinue={() => {
                setShowEffortModal(false);
                navigate(testSetAnnotateHref(testSetId ?? '').replace(/^#/, ''));
              }}
            />

            <Modal
              visible={showClearDraftsModal}
              onDismiss={() => setShowClearDraftsModal(false)}
              header="Clear draft labels"
              footer={
                <Box float="right">
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button variant="link" onClick={() => setShowClearDraftsModal(false)}>
                      Cancel
                    </Button>
                    {/* Names what will be deleted, so the confirm button is not
                        interchangeable with every other primary in the app. */}
                    <Button variant="primary" iconName="remove" onClick={handleClearDraftLabels} loading={isClearingDrafts}>
                      Delete draft labels
                    </Button>
                  </SpaceBetween>
                </Box>
              }
            >
              <SpaceBetween size="s">
                <Box>
                  Deletes every machine-generated draft label in this test set, leaving the documents in place so you can re-label them with
                  a different configuration.
                </Box>
                {/* Only draft-machine labels are removed; reviewed, uploaded and
                    generated ground truth survives a re-label. */}
                <Alert type="info">
                  Reviewed labels, and any ground truth you uploaded or generated, are kept. Only labels tagged <b>Draft (machine)</b> are
                  removed.
                </Alert>

                {/* Collapsed by default and Admin-only: a full reset discards the
                    team's confirmed annotations, so it is deliberately harder to
                    reach than the safe action above and requires the set id typed
                    to confirm. */}
                {isAdmin && (
                  <ExpandableSection headerText="Advanced" variant="footer">
                    <SpaceBetween size="s">
                      <Alert type="warning" header="Reset all labels, including reviewed ones">
                        This deletes <b>every</b> label in this test set — reviewed and confirmed annotations, uploaded ground truth, the
                        review history, and the calibration measurements collected from past reviews. The documents themselves are kept. The
                        test set returns to <b>Unlabeled</b>, as if it had just been created.
                        <Box variant="p" padding={{ top: 'xs' }}>
                          There is no undo. Annotation work discarded here cannot be recovered.
                        </Box>
                      </Alert>
                      <FormField
                        label={
                          <>
                            To confirm, type <b>{testSetId}</b>
                          </>
                        }
                      >
                        <Input
                          value={resetConfirmText}
                          onChange={({ detail }) => setResetConfirmText(detail.value)}
                          placeholder={testSetId}
                          disabled={isResetting}
                        />
                      </FormField>
                      <Button onClick={handleResetLabels} loading={isResetting} disabled={resetConfirmText !== testSetId}>
                        Reset all labels
                      </Button>
                    </SpaceBetween>
                  </ExpandableSection>
                )}
              </SpaceBetween>
            </Modal>
          </SpaceBetween>
        </ContentLayout>
      }
    />
  );
};

export default TestSetDetail;
