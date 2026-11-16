// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Estimated Cost table values a stored price the way the cost reports do (#1903).
 *
 * A stack can hold a custom price saved before prices were checked. `Number()`
 * reads `0x10` as 16, while the reports cannot read it at all, so the table must
 * show that unit as unpriced rather than at $16. A price with only surrounding
 * spaces is one both readers value, and the table values it too.
 */

import { render, screen, within } from '@testing-library/react';
import React from 'react';
import { describe, expect, it, vi } from 'vitest';

// One object for every render: the table re-derives its lookup when `pricing` changes identity.
const PRICING = vi.hoisted(() => ({
  pricing: {
    pricing: [
      {
        name: 'bedrock/m',
        units: [
          { name: 'inputTokens', price: '0x10' },
          { name: 'outputTokens', price: ' 2.0E-6 ' },
        ],
      },
    ],
  },
  nonChargeableUnits: {},
  loading: false,
}));
vi.mock('../../../hooks/use-pricing', () => ({ default: () => PRICING }));

import MeteringTable from '../MeteringTable';

const rowFor = (unit: string): string => {
  const row = within(screen.getByRole('table'))
    .getAllByRole('row')
    .find((r) => within(r).queryByText(unit) !== null);
  expect(row).toBeDefined();
  return row!.textContent ?? '';
};

describe('Estimated Cost table and stored prices', () => {
  it('shows a price the reports cannot read as unpriced, and values one with surrounding spaces', () => {
    render(<MeteringTable meteringData={{ 'Extraction/bedrock/m': { inputTokens: 100, outputTokens: 1000 } }} />);
    const input = rowFor('inputTokens');
    expect(input).toContain('None');
    expect(input).not.toContain('$16');
    expect(rowFor('outputTokens')).toContain('$0.0020');
  });
});
