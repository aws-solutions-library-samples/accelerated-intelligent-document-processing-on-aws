// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { Box, Popover, SpaceBetween } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';
import { useFormatters } from '../../i18n/format';

const messages = defineMessages<{
  header: NoMessageValues;
  considered: NoMessageValues;
  sourceNote: NoMessageValues;
  why: NoMessageValues;
}>({
  header: {
    id: 'documents.classConfidence.header',
    defaultMessage: 'Why this class?',
    description: "Heading of the popover that explains a page's class: the classifier's reasoning and the other classes it considered.",
  },
  considered: {
    id: 'documents.classConfidence.considered',
    defaultMessage: 'Considered',
    description: 'Label above the ranked list of classes the classifier considered for the page.',
  },
  sourceNote: {
    id: 'documents.classConfidence.sourceNote',
    defaultMessage: "The classifier's own explanation, recorded at classification time.",
    description: 'Note at the foot of the class explanation popover, saying where the explanation comes from.',
  },
  why: {
    id: 'documents.classConfidence.why',
    defaultMessage: 'Why?',
    description: 'Link that opens the class explanation of a page that has no confidence score.',
  },
});

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
   * `confidence.mode: topk` on the class-assigning call (the default).
   */
  candidates?: (ClassCandidate | null)[] | null;
}

/**
 * "Not scored" — deliberately an em-dash rather than a blank cell or a zero.
 *
 * A blank reads as a rendering bug in a column that has values in every other
 * row, and `0%` would be a lie: not scored is the absence of a measurement, not
 * a measurement of zero (see the `Page.confidence` docstring in models.py).
 */
const NotScored = (): React.JSX.Element => (
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
const ClassConfidence = ({ confidence, reason, candidates }: ClassConfidenceProps): React.JSX.Element => {
  const intl = useIntl();
  const { formatPercent } = useFormatters();
  /** Percentage with one decimal, e.g. 0.9345 -> "93.5%". */
  const asPercent = (value: number): string => formatPercent(value, { fractionDigits: 1 });
  const hasConfidence = typeof confidence === 'number';
  const hasReason = typeof reason === 'string' && reason.trim().length > 0;
  const ranked = (candidates ?? []).filter((c): c is ClassCandidate => !!c && !!c.Class);
  const hasDetail = hasReason || ranked.length > 0;

  if (!hasConfidence && !hasDetail) {
    return <NotScored />;
  }

  // Scored, but the model gave nothing to explain it: plain text, since there is
  // nothing for a click to open. Deliberately not a filled badge, which would
  // make these rows louder than their informative neighbours.
  if (!hasDetail) {
    return <span>{asPercent(confidence as number)}</span>;
  }

  return (
    <Popover
      dismissButton={false}
      position="top"
      size="large"
      triggerType="text"
      header={intl.formatMessage(messages.header)}
      content={
        <SpaceBetween size="xs">
          {hasReason && <Box variant="p">{reason}</Box>}
          {/* The ranked alternatives answer "what else could this have been?",
              which is the question a suspicious classification actually raises. */}
          {ranked.length > 0 && (
            <SpaceBetween size="xxs">
              <Box variant="awsui-key-label">
                <FormattedMessage {...messages.considered} />
              </Box>
              {ranked.map((candidate) => (
                <Box key={candidate.Class}>
                  {candidate.Class}
                  {typeof candidate.Probability === 'number' && ` — ${asPercent(candidate.Probability)}`}
                </Box>
              ))}
            </SpaceBetween>
          )}
          <Box variant="small" color="text-body-secondary">
            <FormattedMessage {...messages.sourceNote} />
          </Box>
        </SpaceBetween>
      }
    >
      {hasConfidence ? asPercent(confidence as number) : <FormattedMessage {...messages.why} />}
    </Popover>
  );
};

/**
 * Sort comparator for a class-confidence column.
 *
 * Unscored rows sort LAST when ASCENDING — the direction that matters, since
 * least-confident-first is how a reviewer finds the pages worth a second look and
 * a block of "—" at the top of that list defeats the click. Absence of a
 * measurement is not a low measurement, so they are not treated as zero.
 *
 * Descending puts them first, because `useCollection` negates the comparator for
 * the reverse direction — there is no single ordering that keeps nulls last both
 * ways. Two attempts to beat that (reversing the sorted array, then swapping the
 * comparator's arguments) each produced the bug they were meant to avoid, and the
 * cost of the remaining wart is one direction of one column, so this defers to the
 * design system's behaviour instead of hand-rolling sorting for every column.
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
