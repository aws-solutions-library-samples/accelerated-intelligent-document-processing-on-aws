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
  Popover,
  SegmentedControl,
  SpaceBetween,
  Spinner,
  StatusIndicator,
  TextFilter,
} from '@cloudscape-design/components';
import type { FlashbarProps } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from '../../api/client-shim';
import {
  getAnnotationQueue,
  claimReview,
  releaseReview,
  completeSectionReview,
  estimateReviewEffort,
  openTestSetAnnotationDraft,
} from '../../graphql/generated';
import { getErrorMessage } from '../../utils/errorUtils';
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

/**
 * The document a baseline key belongs to.
 *
 * A key is `{setId}/baseline/{objectKey}/sections/{n}/result.json`, so the save
 * confirmation used to take the last segment and announce "result.json is now marked
 * reviewed" — a filename every document in every set shares, which told the reviewer
 * nothing about what had just been saved.
 *
 * Derived from the key rather than from the current selection, because the queue may
 * already have advanced by the time this renders; reading `selected` would name the
 * next document instead of the saved one. Exported so the parsing is testable without
 * standing up the whole workspace.
 */
export const documentNameFromBaselineKey = (baselineKey: string): string => {
  const afterBaseline = baselineKey.split('/baseline/')[1];
  return afterBaseline?.split('/')[0] || baselineKey;
};

/** One document in the queue, as returned by getAnnotationQueue. */
export interface QueueItem {
  objectKey: string;
  inputKey: string;
  reviewObjectKey?: string | null;
  minConfidence?: number | null;
  confidenceThreshold?: number | null;
  alertCount?: number | null;
  documentClasses?: (string | null)[] | null;
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
  draftVersion?: number | null;
  baseVersion?: number | null;
  documents: QueueItem[];
}

type DocView = 'ground-truth' | 'source';

/**
 * The version transition an annotation session is working within.
 *
 * `baseVersion` is the state being left, which the server has snapshotted to
 * `{testSetId}/versions/{baseVersion}/baseline/`; `draftVersion` is what this session is
 * producing. Carried in the queue link so a link identifies its own transition.
 */
interface AnnotationDraft {
  baseVersion: number;
  draftVersion: number;
  snapshotObjectCount?: number | null;
  alreadyOpen?: boolean | null;
}

const QUEUE_PAGE_SIZE = 100;

const LABEL_JOB_POLL_MS = 5000;

/**
 * `decimals` carries how much precision the number has earned. EstimateConfidence
 * exists, per its own docstring, "so a cold-start estimate is never rendered with
 * the same authority as a measured one" — but rendering every tier to 0.1% spent
 * that distinction on the qualifier line while the headline still read like a
 * measurement. One review of a 12-document set moved a displayed 85.1% to 92.2%,
 * a swing two orders of magnitude wider than the digit being shown.
 */
const formatPct = (fraction: number, decimals = 1): string => (Number.isFinite(fraction) ? `${(fraction * 100).toFixed(decimals)}%` : '—');

/** Rows per page in the queue rail. */
const QUEUE_ROWS_PER_PAGE = 20;

const AnnotationWorkspace = (): React.JSX.Element => {
  const { testSetId } = useParams<{ testSetId: string }>();
  // ?doc= preselects one document, so a per-row Annotate link opens the queue on
  // that document.
  const [searchParams] = useSearchParams();
  const requestedDoc = searchParams.get('doc');
  // Canonical field path from a shared link, e.g. "?doc=x.pdf&field=LineItems[0].Rate".
  const requestedField = searchParams.get('field');
  // Which version transition a shared queue link belongs to. Annotating a set that
  // already has ground truth commits to producing a new version of it, so a link that
  // does not say which transition it refers to is ambiguous once the next one opens.
  const requestedVersion = searchParams.get('v');
  const { navigationOpen, setNavigationOpen } = useAppContext();
  const { settings } = useSettingsContext();
  const { canAnnotate, isAdmin, isAuthor, isReviewer, isAnnotator, isAnnotatorOnly, loading: roleLoading } = useUserRole();

  /**
   * Who the SERVER will accept, per save path — the two differ, and neither matches
   * `canAnnotate`.
   *
   * A document with a review record saves through `completeSectionReview`
   * (`Admin, Reviewer, Annotator` — schema.graphql:1350-1351). One without saves
   * through the editor's direct-to-S3 write, which needs `uploadDocument`
   * (`Admin, Author` — schema.graphql:1239-1246). So `Author` is refused the first
   * and `Annotator` the second.
   *
   * Gating on `canAnnotate` (Admin | Author | Annotator) offered editing to users the
   * server would refuse: an Annotator would have corrected a class and lost it to an
   * authorization error at save. The server is still what enforces this; these
   * booleans exist so the UI does not invite work it cannot persist.
   */
  const canSaveViaReview = isAdmin || isReviewer || isAnnotator;
  const canSaveDirectToBaseline = isAdmin || isAuthor;
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
   * The open version transition, once one exists.
   *
   * Null means annotation has not begun on this set yet and the reviewer has not been
   * told what beginning it entails. Editing ground truth in place used to be silent —
   * the point of asking first is that the commitment is the objection, not the
   * mechanism.
   */
  const [openedDraft, setOpenedDraft] = useState<AnnotationDraft | null>(null);
  const [isOpeningDraft, setIsOpeningDraft] = useState(false);

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

  /**
   * Open the version transition this session will work in.
   *
   * Asked for rather than done on arrival, because the commitment is the thing
   * objected to being invisible: annotating a set that already has ground truth produces
   * a new version of it whether or not anyone said so. The server snapshots the state
   * being left, so agreeing here is also what makes the previous labels recoverable.
   *
   * Idempotent server-side, so a reviewer returning to a set mid-session re-opens the
   * same transition and nothing is copied again.
   */
  const openDraft = useCallback(async () => {
    if (!testSetId) return;
    setIsOpeningDraft(true);
    setError(null);
    try {
      const result = await client.graphql({
        query: openTestSetAnnotationDraft,
        variables: { input: { testSetId } },
      });
      const opened = result.data.openTestSetAnnotationDraft as AnnotationDraft | null;
      if (opened) {
        setOpenedDraft(opened);
        // The commitment, confirmed at the moment it is made. Without this the alert
        // simply vanished and a small badge appeared, and the server's report of what
        // was preserved went unread.
        setFlashItems([
          {
            type: 'success',
            content: opened.alreadyOpen
              ? `Version ${opened.draftVersion} was already open on this set; your corrections continue into it.`
              : `Version ${opened.baseVersion} preserved (${(opened.snapshotObjectCount ?? 0).toLocaleString()} objects). Your corrections go into version ${opened.draftVersion}.`,
            dismissible: true,
            onDismiss: () => setFlashItems([]),
            id: 'annotation-draft-opened',
          },
        ]);
      }
    } catch (err) {
      logger.error('Could not open an annotation draft:', err);
      setError(`Could not start annotating this set: ${getErrorMessage(err)}`);
    } finally {
      setIsOpeningDraft(false);
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
      const documentName = documentNameFromBaselineKey(baselineKey);
      setFlashItems([
        {
          type: 'success',
          content: `Saved. ${documentName} is now marked reviewed.`,
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

  /**
   * The open transition, preferring what the server reported on the queue.
   *
   * Read from `getAnnotationQueue` rather than probed with the mutation: opening a draft
   * snapshots the baselines, so using it to *find out* whether one exists would open a
   * transition merely by visiting the page — the silent commitment this exists to remove.
   * `openedDraft` covers the gap between clicking Start annotating and the next queue
   * refresh returning the same numbers.
   */
  const draft: AnnotationDraft | null =
    queue?.draftVersion != null
      ? { baseVersion: queue.baseVersion ?? queue.draftVersion - 1, draftVersion: queue.draftVersion }
      : openedDraft;

  /**
   * How precisely the accuracy estimate may be printed. A tenth of a percent is a
   * claim about the second significant figure, which only a curve measured on this
   * set supports; below that the number moves by whole points per review.
   */
  const estimateDecimals = impact?.estimateConfidence === 'measured' ? 1 : 0;

  /**
   * The queue link, carrying the transition it belongs to.
   *
   * A link should name the transition it belongs to. Without `?v=`, a shared link means
   * "annotate this set" — which silently becomes a different job once the current
   * transition is published and the next one opens. With it, a stale link can say so.
   *
   * No `?v=` before a draft exists, because there is no transition yet to name.
   */
  const queueLink = draft
    ? `${window.location.origin}/${testSetAnnotateHref(testSetId ?? '')}?v=${draft.draftVersion}`
    : `${window.location.origin}/${testSetAnnotateHref(testSetId ?? '')}`;

  /**
   * A link from a transition that has since closed.
   *
   * Not an error: the set is still annotatable, and the reviewer probably wants to
   * continue in the current transition. But silently treating v2's link as v5's work
   * would let someone believe they were adding to a version that had already shipped.
   */
  // Only an integer can name a version: `?v=abc` used to produce a warning about
  // "version abc, which is closed".
  const requestedVersionNumber = requestedVersion && Number.isInteger(Number(requestedVersion)) ? Number(requestedVersion) : null;
  const staleLinkVersion =
    draft && requestedVersionNumber !== null && requestedVersionNumber !== draft.draftVersion ? requestedVersionNumber : null;

  /**
   * Link to one field of the open document, for "what should this value be?".
   *
   * Deliberately a link rather than an in-app handoff: the alternative considered
   * was routing a question to a subject-matter expert's own queue, which assumes
   * an organisation structured that way. A URL works for anyone with a chat tool, and the
   * recipient's access is still checked on arrival — the link only navigates.
   */
  const buildFieldLink = useCallback(
    (fieldPath: string) => {
      const params = new URLSearchParams();
      if (selected?.objectKey) params.set('doc', selected.objectKey);
      params.set('field', fieldPath);
      return `${queueLink}?${params.toString()}`;
    },
    [queueLink, selected?.objectKey],
  );

  /**
   * Per-document actions live in the editor pane's header, not below it: on a long
   * document a footer button is below the fold.
   */
  const documentActions = selected && (
    <SpaceBetween direction="horizontal" size="xs">
      <Button onClick={advanceToNext} disabled={isLoading}>
        Skip to next document
      </Button>
      {/* Exactly one primary at a time, and it is whichever action comes next:
          claim an unclaimed document, then confirm the one you hold. Both were
          primary before, which put two solid-blue buttons side by side and left
          the order of operations to be guessed.
          Before a transition is open, neither is next: "Start annotating" is, and
          it is primary in the alert below. Claiming stays available — it is a lock
          for coordinating with other annotators, not a write to ground truth — but
          it stops competing for the eye with the step that actually unblocks work. */}
      {selected.reviewObjectKey && !selected.claimedByMe && !selected.reviewed && (
        <Button
          variant={draft ? 'primary' : 'normal'}
          onClick={claimSelected}
          loading={isClaiming}
          disabled={isLoading || Boolean(selected.claimedBy)}
        >
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
          correct document needs this to ever leave the queue.
          Gated on `draft` for the same reason the editor is: confirming is a write to
          ground truth — it tags every section reviewed-human, which is precisely the
          draft-machine → reviewed-human transition a version brackets. Gating only the
          editor left this as an ungated second route to the same commitment, so a whole
          set could be confirmed without a version ever recording what the labels had
          been. The alert below says what to do about it, and is the only primary until
          it is done. */}
      <Button
        variant={draft && (selected.claimedByMe || selected.reviewed || !selected.reviewObjectKey) ? 'primary' : 'normal'}
        onClick={handleConfirmCorrect}
        loading={isConfirming}
        disabled={isLoading || !selected.reviewObjectKey || !draft}
      >
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
            description={
              <>
                Review the documents with the most confidence alerts first — each one you correct removes the most likely errors.
                {/* The transition, kept in view rather than only at the moment of
                    agreeing to it: which version you are producing is the thing a
                    reviewer needs to be able to answer later, when a run's score is
                    attributed to one. */}
                {draft && (
                  <>
                    {' '}
                    {/* Explained on hover, for the reader who did not click Start
                        annotating themselves and has nothing else on screen saying
                        what the two numbers are. */}
                    <Popover
                      dismissButton={false}
                      position="bottom"
                      size="medium"
                      triggerType="custom"
                      content={`Corrections made here go into version ${draft.draftVersion}. Version ${draft.baseVersion}, the labels this set had before, is preserved and can still be scored against.`}
                    >
                      <Badge color="blue">
                        v{draft.baseVersion} &rarr; v{draft.draftVersion}
                      </Badge>
                    </Popover>
                  </>
                )}
              </>
            }
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
                    {/* Whole percent until the curve is actually measured on this set:
                        see formatPct. */}
                    <Box fontSize="heading-m">{formatPct(1 - impact.baselineError, estimateDecimals)}</Box>
                    <Box fontSize="body-s" color="text-body-secondary">
                      {impact.residualError < impact.baselineError
                        ? `${formatPct(1 - impact.residualError, estimateDecimals)} after the recommended review`
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
                    {renderQualityTier(impact.qualityTier, impact.qualityTierReason, 1 - impact.baselineError, estimateDecimals)}
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
                  {/* Reviewing a document drops it out of the queue, so this is the only
                      route back to one — and it used to exist solely as the action on the
                      "Queue complete" alert, i.e. only once every other document was done.
                      Searching a reviewed document by name until then returned "0 matches"
                      with no hint it was being filtered out, which is exactly the state an
                      annotator who has just mis-saved is in. */}
                  {!showReviewed && (
                    <Button variant="inline-link" onClick={() => setShowReviewed(true)}>
                      Show reviewed documents
                    </Button>
                  )}
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
                              {/* The class, shown but NOT scored and NOT part of the
                                  ordering. A wrong class is invisible from every other
                                  column: extraction under the wrong schema can be
                                  confidently wrong, so the alert count and confidence
                                  look entirely normal. Nothing here can tell whether
                                  the class is wrong — the draft under review IS the
                                  candidate ground truth — so it is put in front of a
                                  human rather than turned into a number. */}
                              {(item.documentClasses ?? []).filter(Boolean).map((cls) => (
                                <Badge key={cls as string} color="grey">
                                  {cls}
                                </Badge>
                              ))}
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
                    /* "No documents to review" is wrong when a filter is what emptied the
                       list — the documents are there, the text just does not match one.
                       Said plainly, because the likeliest reason a search finds nothing is
                       that the document was already reviewed and is hidden. */
                    empty={
                      <Box textAlign="center">
                        <SpaceBetween size="xs">
                          <Box>{queueFilter ? `No document matches "${queueFilter}".` : 'No documents to review.'}</Box>
                          {queueFilter && !showReviewed && (
                            <Box fontSize="body-s" color="text-body-secondary">
                              Reviewed documents are hidden — show them to search those too.
                            </Box>
                          )}
                        </SpaceBetween>
                      </Box>
                    }
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

              {/* The commitment, stated before it is made rather than discovered later.
                  Annotating a set that already has ground truth produces a new version of
                  it — which used to happen silently, in place, with nothing recording what
                  the labels had been. Agreeing here is also what snapshots them. */}
              {selected && !draft && (
                <Alert
                  type="info"
                  header="Annotating this set creates a new version of it"
                  action={
                    <Button variant="primary" loading={isOpeningDraft} disabled={!canAnnotate} onClick={openDraft}>
                      Start annotating
                    </Button>
                  }
                >
                  Its current labels are preserved as a version you can go back to and score against, so a run that was measured against
                  them stays reproducible. Your corrections go into the next version.
                  {!canAnnotate && ' Your role cannot start an annotation session on this set.'}
                </Alert>
              )}

              {/* A link from a transition that has since been published. Not an error —
                  the set is still annotatable — but working in v5 while believing you are
                  adding to v2 is worth interrupting for. */}
              {staleLinkVersion && (
                <Alert type="warning" header={`This link refers to version ${staleLinkVersion}, which is closed`}>
                  That version has been published. You are now annotating toward version {draft?.draftVersion}; the link in your address bar
                  is out of date. Use <b>Copy queue link</b> for the current one.
                </Alert>
              )}
              {/* A missing review key has TWO opposite causes and they need
                  opposite advice. The queue rail beside this already distinguishes
                  them; this pane did not, and told a reviewer whose every document
                  already had ground truth to "generate draft labels first" — while
                  the same screen said "nothing to review". Keyed on labelSource,
                  exactly as the rail is. */}
              {selected && !selected.reviewObjectKey && !selected.labelSource && (
                <Alert type="warning" header="Not ready to annotate">
                  This document has no labels yet. Generate draft labels for the set first, then it will appear here for review.
                </Alert>
              )}
              {selected && !selected.reviewObjectKey && selected.labelSource && (
                <Alert type="success" header="Already ground truth">
                  This document carries authored ground truth, so there is nothing to draft-label or review — draft labeling skips it
                  deliberately, and nothing here will overwrite it.
                  {/* Three states, not two. Before a transition is open the editor is
                      read-only, so promising "you can still correct it below" here
                      contradicted the alert above it; and the direct save now counts
                      as a review, so the old copy denying that was false. */}
                  {canSaveDirectToBaseline
                    ? draft
                      ? ' You can still correct it below, including its class. Saving writes the ground truth directly and counts the document as reviewed; there is no machine draft here to confirm.'
                      : ' Use Start annotating, above, to correct it.'
                    : ' Correcting a document that already has authored ground truth writes it directly, which an Admin or Author has to do — you can inspect the values below.'}
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
                  /* Read-only only when the server would refuse this document's save
                     path — see canSaveViaReview / canSaveDirectToBaseline above.

                     Previously `!canAnnotate || !selected.reviewObjectKey`, so a
                     missing review record forced read-only whatever the role: an
                     Admin opening a document that already carried authored ground
                     truth got a disabled class dropdown reading "You do not have
                     permission to change this class", two lines under an alert
                     promising they could "correct the values" ([#674]). The review
                     record gates the review WORKFLOW — claim, release, mark reviewed,
                     handled by separate props above — not editing. */
                  /* Read-only until the transition is open, as well as by role. Letting
                     someone edit first and open the version afterwards would put the
                     commitment back where it was — implicit, and discovered only once the
                     labels had already changed. Looking is always allowed. */
                  isReadOnly={!draft || (selected.reviewObjectKey ? !canSaveViaReview : !canSaveDirectToBaseline)}
                  /* Wider than isReadOnly on purpose. A class correction persists via
                     reextractTestSetDocument (Admin, Author, Annotator), which stamps
                     the baseline server-side and needs no review record — so every
                     role that may work this queue may also correct a class, even on a
                     document whose FIELD edits they could not save. */
                  canChangeClass={canAnnotate}
                  /* Route through the review API only when there IS a review to
                     complete. completeSectionReview requires reviewObjectKey, so
                     leaving it wired for an authored-ground-truth document would let
                     someone edit and then lose the work to "no review record yet" on
                     save. Falling back to the editor's direct-to-S3 write is what
                     TestSetDocumentDetail already does, and it is the semantically
                     correct path here: there is no draft to confirm, and no
                     confidence-curve signal to record, because there was no prediction
                     to be right or wrong about.

                     This used to say there was also "nothing to tag reviewed-human".
                     That reasoning was the bug: a reviewer could correct every document
                     in an authored-ground-truth set and the queue still reported 0
                     reviewed. Who authored a label and whether *this* reviewer has now
                     checked it are different facts, and the progress metric asks the
                     second. The editor tags it itself on this path. */
                  onSave={selected.reviewObjectKey ? handleSave : undefined}
                  onSaved={handleSaved}
                  saveButtonText="Save & next in queue"
                  testSetId={testSetId}
                  onReextracted={() => loadQueue(false)}
                  focusFieldPath={requestedField}
                  buildFieldLink={buildFieldLink}
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
