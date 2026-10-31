// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Estimated Cost table keeps its short columns wide enough for their contents.
 *
 * With no floor, a narrow window shrank Context, Unit, Value, Unit Cost and
 * Estimated Cost below their own one-word values, and the table wrapped inside the
 * word ("Contex t", "Classifi cation", "gb_second s"). Each of those columns now
 * carries a CSS min-width, so the table scrolls instead.
 */

import { render, screen, within } from '@testing-library/react';
import React from 'react';
import { describe, expect, it, vi } from 'vitest';

// One object for every render: the table re-derives its price lookup whenever
// `pricing` changes identity, so a fresh object per call never settles.
const PRICING = vi.hoisted(() => ({ pricing: { pricing: [] }, nonChargeableUnits: [], loading: false }));
vi.mock('../../../hooks/use-pricing', () => ({ default: () => PRICING }));

import MeteringTable from '../MeteringTable';

describe('Estimated Cost table columns', () => {
  it.each(['Context', 'Unit', 'Value', 'Unit Cost', 'Estimated Cost'])('gives the %s column a minimum width', (header) => {
    render(<MeteringTable meteringData={{ 'Classification/bedrock/us.amazon.nova-2-lite-v1:0': { inputTokens: 4055 } }} />);

    const table = screen.getByRole('table');
    const th = within(table)
      .getAllByRole('columnheader')
      .find((cell) => cell.textContent?.trim() === header);

    expect(th).toBeDefined();
    expect(parseInt(th?.style.minWidth ?? '', 10)).toBeGreaterThanOrEqual(100);
  });
});
