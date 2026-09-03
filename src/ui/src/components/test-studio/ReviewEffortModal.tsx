// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * ReviewEffortModal — how much of this set a human must actually review. Sits
 * between draft labeling and annotation so the owner sizes the commitment before
 * a team enters the queue.
 *
 * Every number comes from estimateReviewEffort; nothing is computed client-side,
 * so the UI cannot drift from the backend's model. estimateConfidence drives the
 * trust banner, since a prior-derived figure otherwise looks measured.
 */

import React, { useState, useEffect, useCallback } from 'react';
import {
  Alert,
  Box,
  Button,
  ColumnLayout,
  ExpandableSection,
  Form,
  FormField,
  Header,
  Modal,
  Slider,
  SpaceBetween,
  Spinner,
  StatusIndicator,
  Table,
  Tiles,
} from '@cloudscape-design/components';
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from '../../api/client-shim';
import { estimateReviewEffort } from '../../graphql/generated';

const client = generateClient();
const logger = new ConsoleLogger('ReviewEffortModal');

export type ReviewStrategy = 'worst-first' | 'everything' | 'accept-drafts';

interface CalibrationHealth {
  ece?: number | null;
  auroc?: number | null;
  binCoverage: number;
  totalObservations: number;
  degenerate: boolean;
  overconfident: boolean;
  undiscriminating: boolean;
  reliable: boolean;
}

interface BurndownPoint {
  docsReviewed: number;
  residualErrorPct: number;
  cutoff?: number | null;
}

interface ReliabilityBin {
  binStart: number;
  binEnd: number;
  observations: number;
  observedAccuracy?: number | null;
  blendedAccuracy?: number | null;
}

export interface ReviewEffortEstimate {
  testSetId: string;
  targetAccuracy: number;
  configVersion?: string | null;
  docsToReview: number;
  docsToReviewLow: number;
  docsToReviewHigh: number;
  totalDocs: number;
  sampledDocs?: number | null;
  impliedCutoff?: number | null;
  residualError: number;
  baselineError: number;
  effortMinutes: number;
  effortMinutesPerDoc?: number | null;
  estimateConfidence: string;
  auditSampleSize: number;
  recommendReviewAll: boolean;
  calibration: CalibrationHealth;
  burndown: BurndownPoint[];
  reliabilityTable: ReliabilityBin[];
}

interface Props {
  visible: boolean;
  testSetId: string;
  configVersion?: string | null;
  onDismiss: () => void;
  /** Called with the chosen strategy and the estimate it was based on. */
  onContinue: (strategy: ReviewStrategy, estimate: ReviewEffortEstimate | null) => void;
}

const DEFAULT_TARGET = 99;

const formatEffort = (minutes: number): string => {
  if (!Number.isFinite(minutes) || minutes <= 0) return '—';
  if (minutes < 60) return `≈${Math.round(minutes)} min`;
  return `≈${(minutes / 60).toFixed(minutes < 600 ? 1 : 0)} hrs`;
};

/**
 * Cost of reviewing every document.
 *
 * Uses the backend's per-document figure when present. The fallback divides
 * effortMinutes by docsToReview, which overstates it — effortMinutes also covers
 * the audit sample, and reviewing everything leaves nothing to audit.
 */
const effortForAll = (e: ReviewEffortEstimate): number =>
  e.effortMinutesPerDoc != null && e.effortMinutesPerDoc > 0
    ? e.effortMinutesPerDoc * e.totalDocs
    : (e.effortMinutes / Math.max(e.docsToReview + e.auditSampleSize, 1)) * e.totalDocs;

const pct = (fraction?: number | null): string =>
  fraction === null || fraction === undefined || !Number.isFinite(fraction) ? '—' : `${(fraction * 100).toFixed(1)}%`;

/** Per-estimateConfidence copy for how much the number should be trusted. */
const CONFIDENCE_COPY: Record<string, { type: 'info' | 'warning' | 'success'; header: string; body: string }> = {
  prior: {
    type: 'warning',
    header: 'Rough estimate — nothing measured on this set yet',
    body: 'Based on a generic cross-set confidence curve. It self-corrects as your team reviews, and as scoring runs measure the high-confidence range that review never opens.',
  },
  'partially-measured': {
    type: 'info',
    header: 'Partly measured',
    body: 'Some of this set has been observed, but thin bins are still blended toward a prior. The range will tighten as more documents are reviewed or scored.',
  },
  measured: {
    type: 'success',
    header: 'Measured on this set',
    body: 'Derived from observed confidence-vs-accuracy on these documents under this configuration profile.',
  },
  unreliable: {
    type: 'warning',
    header: 'Confidence is not reliable on this set',
    body: 'Confidence cannot be trusted to rank correctness here, so a small worst-first sample is not defensible. Reviewing everything is the honest option.',
  },
};

const ReviewEffortModal = ({ visible, testSetId, configVersion, onDismiss, onContinue }: Props): React.JSX.Element => {
  const [strategy, setStrategy] = useState<ReviewStrategy>('worst-first');
  const [target, setTarget] = useState(DEFAULT_TARGET);
  const [estimate, setEstimate] = useState<ReviewEffortEstimate | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  // Distinct from "still loading" and from an error: a successful estimate over a
  // set with no draft labels left to review.
  const nothingToReview = Boolean(estimate) && (estimate?.totalDocs ?? 0) === 0;

  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (targetAccuracy: number) => {
      if (!testSetId) return;
      setIsLoading(true);
      setError(null);
      try {
        const response = await client.graphql({
          query: estimateReviewEffort,
          variables: { testSetId, targetAccuracy, configVersion: configVersion ?? undefined },
        });
        const data = response.data?.estimateReviewEffort as ReviewEffortEstimate | null;
        if (!data) {
          setError('The review estimate could not be calculated for this test set.');
          return;
        }
        setEstimate(data);
        // The backend owns the call on when a worst-first sample is indefensible.
        if (data.recommendReviewAll) setStrategy('everything');
      } catch (err) {
        logger.error('Error estimating review effort:', err);
        setError('Failed to calculate the review estimate. Please try again.');
      } finally {
        setIsLoading(false);
      }
    },
    [testSetId, configVersion],
  );

  useEffect(() => {
    if (visible) load(DEFAULT_TARGET);
  }, [visible, load]);

  const reviewCount =
    strategy === 'everything' ? (estimate?.totalDocs ?? 0) : strategy === 'accept-drafts' ? 0 : (estimate?.docsToReview ?? 0);

  const banner = CONFIDENCE_COPY[estimate?.estimateConfidence ?? ''] ?? null;

  const chartData = (estimate?.burndown ?? []).map((p) => ({
    docs: p.docsReviewed,
    error: Number((p.residualErrorPct ?? 0).toFixed(2)),
  }));

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header={
        <Header
          variant="h2"
          description={
            estimate ? `${testSetId} · ${estimate.totalDocs} document(s) with draft labels · choose how much to review` : testSetId
          }
        >
          Set up team annotation
        </Header>
      }
      size="large"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss}>
              Cancel
            </Button>
            <Button variant="primary" onClick={() => onContinue(strategy, estimate)} disabled={isLoading || nothingToReview}>
              Continue to annotation
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <Form>
        <SpaceBetween size="l">
          {error && <Alert type="error">{error}</Alert>}

          {/* Every document already carries ground truth, so there is no review to
              size. Without this the dialog priced three strategies over nothing —
              "about 0 of 0 documents (0–0)", "Est. effort —", "Audit sample 0" —
              with Continue still enabled, next to a screen saying the set was fully
              labelled. Say the one true thing instead. */}
          {nothingToReview && !isLoading && !error && (
            <Alert type="success" header="Nothing to review">
              Every document in this set already carries ground truth, so there are no draft labels to check. Run a test to score a
              configuration against it, or add documents and generate draft labels for those.
            </Alert>
          )}

          {isLoading && !estimate && (
            <Box textAlign="center" padding="l">
              <Spinner /> Calculating review effort…
            </Box>
          )}

          {banner && (
            <Alert type={banner.type} header={banner.header}>
              {banner.body}
            </Alert>
          )}

          {estimate?.calibration?.degenerate && (
            <Alert type="warning" header="Confidence barely varies across this set">
              Only {estimate.calibration.binCoverage} of 10 confidence bins carry observations, so ranking documents worst-first is close to
              arbitrary. Reviewing everything is the safer choice.
            </Alert>
          )}

          {estimate?.calibration?.overconfident && (
            <Alert type="warning" header="Model is overconfident on this set">
              Errors are landing in the high-confidence range that worst-first review never opens, so a small sample would miss them. The
              audit sample below is the only check on that zone.
            </Alert>
          )}

          {estimate?.calibration?.undiscriminating && (
            <Alert type="warning" header="Confidence does not identify the errors on this set">
              Confidence ranks correctness no better than chance
              {estimate.calibration.auroc !== null && estimate.calibration.auroc !== undefined
                ? ` (AUROC ${estimate.calibration.auroc.toFixed(2)}, where 0.5 is a coin flip)`
                : ''}
              , so reviewing the documents with the most confidence alerts would find no more errors than reviewing at random. Review
              everything, or change the confidence model for this configuration profile and re-score.
            </Alert>
          )}

          <FormField label="How much should annotators review?">
            <Tiles
              value={strategy}
              onChange={({ detail }) => setStrategy(detail.value as ReviewStrategy)}
              items={[
                {
                  value: 'worst-first',
                  label: 'Review the documents with the most confidence alerts',
                  description: estimate
                    ? `Focus effort where the model is least sure — about ${estimate.docsToReview} of ${estimate.totalDocs} documents (${estimate.docsToReviewLow}–${estimate.docsToReviewHigh}). Highest accuracy gain per hour.`
                    : 'Focus human effort where the model is least sure. Highest accuracy gain per hour.',
                  disabled: estimate?.recommendReviewAll === true,
                },
                {
                  value: 'everything',
                  label: 'Review everything',
                  description: estimate
                    ? `All ${estimate.totalDocs} documents get human eyes (${formatEffort(effortForAll(estimate))}). Highest confidence in the result; most effort.`
                    : 'Every document gets human eyes. Highest confidence in the result; most effort.',
                },
                {
                  value: 'accept-drafts',
                  label: 'Accept machine labels as-is',
                  description: estimate
                    ? `No human review. Leaves an estimated ${pct(estimate.baselineError)} of fields wrong, unreviewed. Fastest; lowest trust.`
                    : 'No human review — use the draft labels directly. Fastest; lowest trust.',
                },
              ]}
            />
          </FormField>

          {estimate && strategy !== 'accept-drafts' && (
            <ColumnLayout columns={4} variant="text-grid">
              <div>
                <Box variant="awsui-key-label">Review</Box>
                <Box fontSize="display-l" fontWeight="bold" color="text-status-info">
                  {reviewCount}
                </Box>
                <Box fontSize="body-s" color="text-body-secondary">
                  of {estimate.totalDocs} documents
                </Box>
              </div>
              <div>
                <Box variant="awsui-key-label">Est. effort</Box>
                <Box fontSize="heading-l">{formatEffort(strategy === 'everything' ? effortForAll(estimate) : estimate.effortMinutes)}</Box>
              </div>
              <div>
                <Box variant="awsui-key-label">Est. accuracy after</Box>
                <Box fontSize="heading-l">{pct(1 - (strategy === 'everything' ? 0 : estimate.residualError))}</Box>
                <Box fontSize="body-s" color="text-body-secondary">
                  from {pct(1 - estimate.baselineError)} now
                </Box>
              </div>
              <div>
                <Box variant="awsui-key-label">Audit sample</Box>
                <Box fontSize="heading-l">{estimate.auditSampleSize}</Box>
                <Box fontSize="body-s" color="text-body-secondary">
                  spot-check of accepted docs
                </Box>
              </div>
            </ColumnLayout>
          )}

          {estimate && (
            <ExpandableSection headerText="Show the math — target a specific label accuracy">
              <SpaceBetween size="l">
                <FormField
                  label="Target label accuracy"
                  description={
                    estimate.impliedCutoff !== null && estimate.impliedCutoff !== undefined
                      ? `Implied confidence cutoff ≈${estimate.impliedCutoff.toFixed(2)} — documents below this get reviewed.`
                      : 'Documents with the most confidence alerts are reviewed first, until the target is met.'
                  }
                >
                  <SpaceBetween size="xs">
                    <Slider
                      value={target}
                      onChange={({ detail }) => setTarget(detail.value)}
                      min={90}
                      max={100}
                      step={0.5}
                      valueFormatter={(v) => `${v}%`}
                      referenceValues={[95]}
                    />
                    <Button onClick={() => load(target)} loading={isLoading} iconName="refresh">
                      Recalculate at {target}%
                    </Button>
                  </SpaceBetween>
                </FormField>

                {chartData.length > 1 && (
                  <div>
                    <Box variant="awsui-key-label">Error burndown</Box>
                    <Box fontSize="body-s" color="text-body-secondary" padding={{ bottom: 's' }}>
                      Remaining error as more documents are reviewed, worst-first.
                    </Box>
                    <div style={{ height: 220 }}>
                      <ResponsiveContainer width="100%" height="100%">
                        <LineChart data={chartData} margin={{ top: 10, right: 20, left: 10, bottom: 20 }}>
                          <CartesianGrid strokeDasharray="3 3" />
                          <XAxis dataKey="docs" label={{ value: 'documents reviewed', position: 'insideBottom', offset: -10 }} />
                          <YAxis label={{ value: 'est. error %', angle: -90, position: 'insideLeft', style: { textAnchor: 'middle' } }} />
                          <Tooltip
                            formatter={(value) => [`${value}%`, 'Est. remaining error']}
                            labelFormatter={(label) => `${label} documents reviewed`}
                          />
                          <ReferenceLine
                            x={estimate.docsToReview}
                            stroke="#b8860b"
                            strokeDasharray="4 4"
                            label={{ value: `${estimate.docsToReview} docs → target`, fontSize: 11, position: 'top' }}
                          />
                          <Line type="monotone" dataKey="error" stroke="#0073bb" strokeWidth={2} dot={false} />
                        </LineChart>
                      </ResponsiveContainer>
                    </div>
                  </div>
                )}

                <div>
                  <Box variant="awsui-key-label">Reliability table — the measured curve</Box>
                  <Box fontSize="body-s" color="text-body-secondary" padding={{ bottom: 's' }}>
                    {estimate.calibration.totalObservations} observation(s) across {estimate.calibration.binCoverage} of 10 bins
                    {estimate.calibration.ece !== null && estimate.calibration.ece !== undefined
                      ? ` · ECE ${estimate.calibration.ece.toFixed(3)}`
                      : ''}
                    {estimate.calibration.auroc !== null && estimate.calibration.auroc !== undefined
                      ? ` · AUROC ${estimate.calibration.auroc.toFixed(3)}`
                      : ''}
                    {estimate.sampledDocs !== null && estimate.sampledDocs !== undefined && estimate.sampledDocs < estimate.totalDocs
                      ? ` · sampled ${estimate.sampledDocs} of ${estimate.totalDocs} documents`
                      : ''}
                  </Box>
                  <Table
                    variant="embedded"
                    items={estimate.reliabilityTable.filter((b) => b.observations > 0)}
                    empty={<Box textAlign="center">No observations recorded yet.</Box>}
                    columnDefinitions={[
                      {
                        id: 'bin',
                        header: 'Confidence',
                        cell: (b: ReliabilityBin) => `${b.binStart.toFixed(1)}–${b.binEnd.toFixed(1)}`,
                      },
                      { id: 'n', header: 'Observations', cell: (b: ReliabilityBin) => b.observations },
                      {
                        id: 'observed',
                        header: 'Observed accuracy',
                        cell: (b: ReliabilityBin) => pct(b.observedAccuracy),
                      },
                      {
                        id: 'blended',
                        header: 'Used in estimate',
                        cell: (b: ReliabilityBin) => pct(b.blendedAccuracy),
                      },
                    ]}
                  />
                </div>

                <StatusIndicator type={estimate.calibration.reliable ? 'success' : 'warning'}>
                  {estimate.calibration.reliable
                    ? 'Confidence is reliable enough to rank documents on this set.'
                    : 'Confidence is not yet reliable enough to rank documents on this set.'}
                </StatusIndicator>
              </SpaceBetween>
            </ExpandableSection>
          )}
        </SpaceBetween>
      </Form>
    </Modal>
  );
};

export default ReviewEffortModal;
