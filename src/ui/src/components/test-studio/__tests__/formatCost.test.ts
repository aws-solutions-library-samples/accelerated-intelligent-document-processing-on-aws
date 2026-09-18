// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The values in here are the ones that actually appeared in the cost table on a
 * live run, not invented edge cases.
 */

import { describe, expect, it } from 'vitest';

import { asFiniteNumber, costCellLabels, formatCostUsd, formatUnitCostUsd } from '../formatCost';

describe('formatUnitCostUsd', () => {
  it('renders a price of exactly $2.40 without floating point noise', () => {
    // Shown on a live run as '$2.39999999999999996'.
    expect(formatUnitCostUsd(2.4)).toBe('$2.4');
    expect(formatUnitCostUsd(0.1 + 0.2)).toBe('$0.3');
  });

  it('never falls back to scientific notation', () => {
    // '$2.4e-7' and '$6e-8', both on screen.
    expect(formatUnitCostUsd(2.4e-7)).toBe('$0.00000024');
    expect(formatUnitCostUsd(6e-8)).toBe('$0.00000006');
    expect(formatUnitCostUsd(1.5e-8)).toBe('$0.000000015');
    // The guard that matters: no 'e' anywhere, at any magnitude.
    for (const exponent of [-1, -3, -6, -9, -12, -15]) {
      expect(formatUnitCostUsd(1.234 * 10 ** exponent)).not.toMatch(/e/);
    }
  });

  it('keeps significant digits on a small value rather than rounding it to zero', () => {
    expect(formatUnitCostUsd(2.4e-7)).not.toBe('$0');
    expect(formatUnitCostUsd(2.4e-7)).not.toBe('$0.0000');
  });

  it('handles ordinary and zero values', () => {
    expect(formatUnitCostUsd(3)).toBe('$3');
    expect(formatUnitCostUsd(0.015)).toBe('$0.015');
    expect(formatUnitCostUsd(0)).toBe('$0');
  });

  it('reports a non-numeric value as N/A instead of $NaN', () => {
    expect(formatUnitCostUsd(Number.NaN)).toBe('N/A');
    expect(formatUnitCostUsd(Number.POSITIVE_INFINITY)).toBe('N/A');
  });
});

describe('formatCostUsd', () => {
  it('uses fixed places so the money column aligns', () => {
    expect(formatCostUsd(2.4)).toBe('$2.4000');
    expect(formatCostUsd(0.0123)).toBe('$0.0123');
    expect(formatCostUsd(0)).toBe('$0.0000');
  });

  it('extends precision rather than rounding a sub-cent cost away to $0.0000', () => {
    expect(formatCostUsd(2.4e-7)).toBe('$0.00000024');
    expect(formatCostUsd(6e-8)).toBe('$0.00000006');
    expect(formatCostUsd(2.4e-7)).not.toBe('$0.0000');
  });

  it('does not emit scientific notation at any magnitude', () => {
    for (const exponent of [0, -2, -5, -8, -11]) {
      expect(formatCostUsd(9.87 * 10 ** exponent)).not.toMatch(/e/);
    }
  });

  it('reports a non-numeric value as N/A', () => {
    expect(formatCostUsd(Number.NaN)).toBe('N/A');
  });
});

describe('asFiniteNumber', () => {
  it('accepts a number or a numeric string, since the API sends both', () => {
    expect(asFiniteNumber(2.4)).toBe(2.4);
    expect(asFiniteNumber('2.4')).toBe(2.4);
    expect(asFiniteNumber('$1,234.5')).toBe(1234.5);
  });

  it('distinguishes a genuine zero from a missing value', () => {
    // The old code used a truthiness check, so a real 0 unit cost rendered as
    // 'None' — indistinguishable from not being priced at all. The table uses that
    // distinction to decide whether a row is a count ('—') or a zero-priced
    // charge; see costCellLabels below, which consumes exactly this distinction.
    expect(asFiniteNumber(0)).toBe(0);
    expect(asFiniteNumber(undefined)).toBeNull();
    expect(asFiniteNumber(null)).toBeNull();
    expect(asFiniteNumber('')).toBeNull();
    expect(asFiniteNumber('n/a')).toBeNull();
  });
});

describe('costCellLabels', () => {
  it('labels an unpriced service as such in BOTH columns, not as free', () => {
    // The regression this exists to pin. A service with no pricing entry writes
    // SQL NULL to both unit_cost and estimated_cost; the caller's `|| 0` turns
    // the cost into 0, so a not-chargeable test written as
    // `cost === 0 && (unitCost === null || unitCost === 0)` — or simply ordered
    // before the null test — matches here and renders '—'. An unpriced service
    // then reads as a free count, which is the misreading GitHub issue #926 set
    // out to eliminate, and the run total silently understates.
    expect(costCellLabels(null, 0)).toEqual({ unitCost: 'Not priced', estimatedCost: 'Not priced' });
    // NULL unit cost with a non-zero cost should not exist, but if the two ever
    // disagree the unpriced label still wins: it is the honest one.
    expect(costCellLabels(null, 1.25)).toEqual({ unitCost: 'Not priced', estimatedCost: 'Not priced' });
  });

  it('labels a metered-but-not-chargeable row with an em dash, not $0.0000', () => {
    // `totalTokens` and `requests` are counts that every Bedrock row meters and
    // no pricing row charges. The entry exists, so this is a real 0 rather than
    // a pricing gap, and it must not read as 'Not priced' either.
    expect(costCellLabels(0, 0)).toEqual({ unitCost: '—', estimatedCost: '—' });
  });

  it('formats a real charge as money in both columns', () => {
    expect(costCellLabels(2.4e-7, 0.0123)).toEqual({ unitCost: '$0.00000024', estimatedCost: '$0.0123' });
  });

  it('keeps a priced row priced even when its measured cost rounds to nothing', () => {
    // A priced unit whose count is 0 is charged at a known rate: the unit column
    // must still show the rate rather than collapsing to '—', so the reader can
    // tell "we know this costs $0.00000024 and used none of it" from "we do not
    // know what this costs".
    expect(costCellLabels(2.4e-7, 0)).toEqual({ unitCost: '$0.00000024', estimatedCost: 'N/A' });
  });
});
