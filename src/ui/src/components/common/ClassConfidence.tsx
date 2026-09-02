// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { Badge, Box, Popover, SpaceBetween } from '@cloudscape-design/components';

interface ClassCandidate {
  Class?: string | null;
  Probability?: number | null;
}

interface ClassConfidenceProps {
  /** Confidence in the class (0-1). Null/undefined means NOT SCORED. */
  confidence?: number | null;
  /** The model's stated evidence for the class, when it gave one. */
  reason?: string | null;
  /**
   * Ranked alternative classes with probabilities, from
   * `classification.confidence.mode: topk` (the default).
   */
  candidates?: (ClassCandidate | null)[] | null;
}

/** Percentage with one decimal, e.g. 0.9345 -> "93.5%". */
const asPercent = (confidence: number): string => `${(confidence * 100).toFixed(1)}%`;

/**
 * "Not scored" — deliberately an em-dash rather than a blank cell or a zero.
 *
 * A blank reads as a rendering bug in a column that has values in every other
 * row, and `0%` would be a lie: not scored is the absence of a measurement, not
 * a measurement of zero (see the `Page.confidence` docstring in models.py).
 */
export const NotScored = (): React.JSX.Element => (
  <Box color="text-status-inactive" textAlign="left">
    —
  </Box>
);

/**
 * The classifier's confidence in a page's or section's CLASS, as a table cell.
 *
 * Two presentations, because the two tables carry different things behind the
 * number:
 *
 * - `badge` (Document Sections) — a static value. The section score is an
 *   aggregate (the minimum across its pages); there is no per-section reasoning
 *   to show, so it must not look clickable.
 * - `link` (Document Pages) — the number IS the affordance for the model's own
 *   reasoning and its ranked runner-up classes. Rendered with Cloudscape's
 *   `Popover triggerType="text"`, which gives link styling, keyboard focus and
 *   the aria wiring for free — matching the "95% margin" column in Test Studio
 *   rather than hand-rolling a dotted underline.
 *
 * **Deliberately un-colored at every value.** There is no configured
 * classification confidence threshold (unlike extraction fields, which have
 * `hitl.confidence_threshold`), so a green/amber/red band would assert a
 * pass/fail the system has not defined. It would also actively mislead: the
 * default classifier answers ~0.95 for the large majority of pages including
 * many of its errors, so banding would paint a coarse two-level flag as a
 * calibrated traffic light. See docs/benchmarking/classification-confidence.md.
 */
const ClassConfidence = ({
  confidence,
  reason,
  candidates,
  variant = 'link',
}: ClassConfidenceProps & { variant?: 'badge' | 'link' }): React.JSX.Element => {
  const hasConfidence = typeof confidence === 'number';
  const hasReason = typeof reason === 'string' && reason.trim().length > 0;
  const ranked = (candidates ?? []).filter((c): c is ClassCandidate => !!c && !!c.Class);
  const hasDetail = hasReason || ranked.length > 0;

  if (!hasConfidence && !hasDetail) {
    return <NotScored />;
  }

  // Sections: a plain neutral badge. `grey` rather than blue/green because the
  // number is information, not a status.
  if (variant === 'badge') {
    return hasConfidence ? <Badge color="grey">{asPercent(confidence as number)}</Badge> : <NotScored />;
  }

  // Pages: the value opens the model's own account of the decision. When the
  // model gave a score but no reasoning and no candidates there is nothing to
  // open, so it stays a plain badge rather than a link that does nothing.
  if (!hasDetail) {
    return <Badge color="grey">{asPercent(confidence as number)}</Badge>;
  }

  return (
    <Popover
      dismissButton={false}
      position="top"
      size="large"
      triggerType="text"
      header="Why this class?"
      content={
        <SpaceBetween size="xs">
          {hasReason && <Box variant="p">{reason}</Box>}
          {/* The ranked alternatives answer "what else could this have been?",
              which is the question a suspicious classification actually raises. */}
          {ranked.length > 0 && (
            <SpaceBetween size="xxs">
              <Box variant="awsui-key-label">Considered</Box>
              {ranked.map((candidate) => (
                <Box key={candidate.Class}>
                  {candidate.Class}
                  {typeof candidate.Probability === 'number' && ` — ${asPercent(candidate.Probability)}`}
                </Box>
              ))}
            </SpaceBetween>
          )}
          <Box variant="small" color="text-body-secondary">
            The classifier&apos;s own explanation, recorded at classification time.
          </Box>
        </SpaceBetween>
      }
    >
      {hasConfidence ? asPercent(confidence as number) : 'Why?'}
    </Popover>
  );
};

/**
 * Sort comparator for a class-confidence column.
 *
 * Unscored rows sort LAST in both directions, instead of clumping at the top of
 * an ascending sort. Sorting least-confident-first is how a reviewer finds the
 * pages worth a second look, and a wall of "—" at the top of that list defeats
 * the click — absence of a measurement is not a low measurement.
 */
export const compareClassConfidence = (a?: number | null, b?: number | null): number => {
  const av = typeof a === 'number' ? a : null;
  const bv = typeof b === 'number' ? b : null;
  if (av === null && bv === null) return 0;
  if (av === null) return 1;
  if (bv === null) return -1;
  return av - bv;
};

export default ClassConfidence;
