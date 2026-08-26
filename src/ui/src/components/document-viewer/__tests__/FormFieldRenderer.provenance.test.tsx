// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Whose value is on screen, and whose confidence.
 *
 * From the UX review: a field a human had corrected still displayed
 * `Confidence: 100.0%` and was still labelled `Predicted:`. The model never
 * produced that text, so neither claim was about the value being shown —
 * hand-typed ground truth was being decorated with a score belonging to the value
 * it replaced, which is exactly backwards for a tool whose job is to establish
 * which values are trustworthy.
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import FormFieldRenderer from '../FormFieldRenderer';

/**
 * Confidence reaches the renderer through the assessment's explainability data,
 * keyed by field name — not through a `confidence` prop.
 */
const EXPLAINABILITY = [{ AccountNumber: { confidence: 1.0 }, RoutingNumber: { confidence: 1.0 } }];

const renderField = (overrides: Record<string, unknown> = {}) =>
  render(
    <FormFieldRenderer
      fieldKey="AccountNumber"
      value="123456"
      onChange={vi.fn()}
      isReadOnly={false}
      explainabilityInfo={EXPLAINABILITY}
      path={['AccountNumber']}
      {...overrides}
    />,
  );

/** The renderer keys edits off the canonical comparison path, not the raw key. */
const EDITED = new Map([['AccountNumber', true]]);

describe('FormFieldRenderer value provenance', () => {
  it('shows the model confidence for a value the model produced', () => {
    renderField();

    expect(screen.getByText(/Confidence: 100\.0%/)).toBeInTheDocument();
    expect(screen.getByText('Predicted:')).toBeInTheDocument();
  });

  it('drops the model confidence once a human has replaced the value', () => {
    renderField({ predictionChanges: EDITED });

    expect(screen.queryByText(/Confidence: 100\.0%/)).not.toBeInTheDocument();
    // Silence would be ambiguous — it could read as "no confidence data" — so the
    // reason the number is gone is stated.
    expect(screen.getByText(/the model's confidence no longer applies/i)).toBeInTheDocument();
  });

  it('stops calling an edited value "Predicted"', () => {
    renderField({ predictionChanges: EDITED });

    expect(screen.queryByText('Predicted:')).not.toBeInTheDocument();
    expect(screen.getByText('Your value:')).toBeInTheDocument();
  });

  it('does not suppress confidence on a field the user has not touched', () => {
    // The suppression must be per field, not per document: correcting one field
    // says nothing about the confidence of the other 277.
    renderField({ fieldKey: 'RoutingNumber', path: ['RoutingNumber'], predictionChanges: EDITED });

    expect(screen.getByText(/Confidence: 100\.0%/)).toBeInTheDocument();
    expect(screen.getByText('Predicted:')).toBeInTheDocument();
  });
});
