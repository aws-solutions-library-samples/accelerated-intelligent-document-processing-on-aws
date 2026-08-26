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
import React from 'react';
import { describe, expect, it } from 'vitest';

import { QUALITY_TIER_COLORS, renderQualityTier } from '../TestSetDetail';

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
