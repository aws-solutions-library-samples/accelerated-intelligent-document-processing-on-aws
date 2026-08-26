// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * How a test set's label-quality verdict is presented.
 *
 * From the UX review: reviewing one document moved a set from "91.7% est. Bronze"
 * to a red "Not rated / Unrated". The *logic* was right — with enough evidence to
 * test whether confidence ranks correctness, the estimator found it does not and
 * withdrew a figure it had been inferring from a cross-set prior. The presentation
 * was not: red reads as a fault the reviewer caused, when in fact the preceding
 * Bronze number was the less honest of the two states.
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import {
  LabelAccuracyLegend,
  QUALITY_TIER_COLORS,
  renderLabelAccuracy,
  renderLabelSource,
  renderLoadedLabelSource,
  renderQualityTier,
} from '../TestSetDetail';

describe('renderQualityTier', () => {
  it('does not colour "unrated" as an error', () => {
    // An absence of a defensible claim is not a fault, and a review that reveals
    // it must not look like a review that caused it.
    expect(QUALITY_TIER_COLORS.unrated).not.toBe('red');
  });

  it('prints no accuracy figure when nothing is defensible', () => {
    render(renderQualityTier('unrated', 'confidence does not rank correctness on this set', 0.917));

    expect(screen.getByText('Not rated')).toBeInTheDocument();
    // The number it would otherwise have shown must not appear.
    expect(screen.queryByText(/91\.7% est\./)).not.toBeInTheDocument();
  });

  it('states the reason inline for unrated, not only on hover', () => {
    // The reason IS the content of an unrated verdict, and the one case a user
    // cannot infer for themselves.
    render(renderQualityTier('unrated', 'confidence does not rank correctness on this set', null));

    expect(screen.getByText(/confidence does not rank correctness/)).toBeInTheDocument();
  });

  it('still leads with the number for a rated tier', () => {
    render(renderQualityTier('gold', 'measured on this set', 0.982));

    expect(screen.getByText('98.2% est.')).toBeInTheDocument();
    expect(screen.getByText('Gold')).toBeInTheDocument();
  });

  it('does not print a rated tier reason inline, keeping the row compact', () => {
    render(renderQualityTier('bronze', 'estimate from a cross-set prior', 0.917));

    expect(screen.getByText('91.7% est.')).toBeInTheDocument();
    expect(screen.queryByText(/— estimate from a cross-set prior/)).not.toBeInTheDocument();
  });

  it('renders a dash when there is no tier at all', () => {
    const { container } = render(renderQualityTier(null, null, null));
    expect(container.textContent).toBe('-');
  });
});

/**
 * The same column, but for the rows where there is no estimate — which is where
 * the trust signal was inverted. A set of hand-authored ground truth showed a bare
 * '-', while the machine drafts being measured against it showed a percentage, so
 * the reference looked worse than the thing it was the reference for.
 */
describe('renderLabelAccuracy', () => {
  it('does not present authored ground truth as unrated', () => {
    const { container } = render(renderLabelAccuracy(null, 'labeled'));

    expect(screen.getByText('Ground truth')).toBeInTheDocument();
    expect(container.textContent).not.toBe('-');
    expect(container.textContent).not.toMatch(/not rated|not assessed/i);
  });

  it('says a draft set is simply not assessed yet, which is a different claim', () => {
    render(renderLabelAccuracy(null, 'draft'));

    expect(screen.getByText('Not assessed yet')).toBeInTheDocument();
    expect(screen.queryByText('Ground truth')).not.toBeInTheDocument();
  });

  it('does not state a verdict while the estimate request is still in flight', () => {
    // Found on a live stack: estimates arrive one request per set after the table
    // renders, so for a second or two every labeled row claimed "Ground truth"
    // and then flipped to a percentage. A pending request is not a verdict.
    render(renderLabelAccuracy(null, 'labeled', true));

    expect(screen.getByText('Estimating')).toBeInTheDocument();
    expect(screen.queryByText('Ground truth')).not.toBeInTheDocument();
    expect(screen.queryByText('Not assessed yet')).not.toBeInTheDocument();
  });

  it('prefers a returned estimate over the pending flag, so a settled row does not regress', () => {
    render(renderLabelAccuracy({ tier: 'bronze', reason: 'from a cross-set prior', accuracy: 0.761 }, 'labeled', true));

    expect(screen.getByText('76.1% est.')).toBeInTheDocument();
    expect(screen.queryByText('Estimating')).not.toBeInTheDocument();
  });

  it('shows the estimate whenever there is one, whatever the label state', () => {
    render(renderLabelAccuracy({ tier: 'silver', reason: 'partly measured here', accuracy: 0.961 }, 'labeled'));

    expect(screen.getByText('96.1% est.')).toBeInTheDocument();
    expect(screen.getByText('Silver')).toBeInTheDocument();
  });
});

describe('LabelAccuracyLegend', () => {
  it('names every tier, so "Bronze" is readable without already knowing the scale', async () => {
    render(<LabelAccuracyLegend />);

    await userEvent.click(screen.getByText(/Est. label accuracy/));

    for (const tier of ['Gold', 'Silver', 'Bronze', 'Unrated']) {
      expect(screen.getByText(tier)).toBeInTheDocument();
    }
    // And the thresholds themselves, which are what the tier names stand for.
    expect(screen.getByText(/at least 99%/)).toBeInTheDocument();
    expect(screen.getByText(/at least 95%/)).toBeInTheDocument();
  });

  it('does not claim authored ground truth carries no figure, because it does', async () => {
    // quality_tier() returns at least BRONZE even at EstimateConfidence.PRIOR, so
    // a set of uploaded ground truth is given a prior-derived percentage. The
    // legend said the opposite, and the row next to it proved the legend wrong.
    render(<LabelAccuracyLegend />);

    await userEvent.click(screen.getByText(/Est. label accuracy/));

    expect(screen.queryByText(/carries no estimate/i)).not.toBeInTheDocument();
    expect(screen.getByText(/a statement about the confidence data behind the estimate/i)).toBeInTheDocument();
  });
});

/**
 * Provenance of a baseline that has already been read off S3.
 *
 * Found on a live stack: one document read "Uploaded" in the review queue and
 * "Unlabeled" in the ground-truth editor header, two lines below an alert saying
 * "Already ground truth". Three statements, one document, two of them wrong.
 */
describe('renderLoadedLabelSource', () => {
  it('treats a loaded baseline with no labelSource as uploaded ground truth', () => {
    // The pipeline writes labelSource; a hand-uploaded ground-truth file does not.
    // Once the bytes have parsed, absence means authored — not absent.
    render(renderLoadedLabelSource(undefined));

    expect(screen.getByText('Uploaded')).toBeInTheDocument();
    expect(screen.queryByText('Unlabeled')).not.toBeInTheDocument();
  });

  it('agrees with the server, which applies the same fallback', () => {
    // _attach_label_metadata: `result.get("labelSource") or LABEL_SOURCE_UPLOADED`.
    render(renderLoadedLabelSource(''));

    expect(screen.getByText('Uploaded')).toBeInTheDocument();
  });

  it('still reports a real labelSource as itself', () => {
    render(renderLoadedLabelSource('draft-machine'));

    expect(screen.getByText('Draft (machine)')).toBeInTheDocument();
  });

  it('leaves renderLabelSource alone, where absence really can mean no baseline', () => {
    // A document row may have no baseline at all, so "Unlabeled" is right there.
    render(renderLabelSource(undefined));

    expect(screen.getByText('Unlabeled')).toBeInTheDocument();
  });
});
