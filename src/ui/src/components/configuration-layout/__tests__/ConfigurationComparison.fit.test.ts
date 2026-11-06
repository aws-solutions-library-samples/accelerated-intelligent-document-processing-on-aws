// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Column fitting for the configuration comparison table. Cloudscape's resizable
 * columns need pixel widths; the fit must fill the dialog without exceeding it,
 * and stay usable (no column below its minimum) when the dialog is narrow.
 */

import { describe, expect, it } from 'vitest';

import { fitColumnWidths } from '../ConfigurationComparison';

describe('fitColumnWidths', () => {
  it('returns null until a width has been measured', () => {
    expect(fitColumnWidths(0, 2)).toBeNull();
  });

  it.each([
    [1280, 2],
    [900, 2],
    [1600, 3],
  ])('fills %ipx with %i value columns without overflowing', (available, n) => {
    const w = fitColumnWidths(available, n);
    expect(w).not.toBeNull();
    const total = w!.field + n * w!.value;
    expect(total).toBeLessThanOrEqual(available);
    expect(total).toBeGreaterThan(available - 40 - n);
  });

  it('caps the field column on a wide dialog so the values get the space', () => {
    const w = fitColumnWidths(3000, 2)!;
    expect(w.field).toBe(420);
    expect(w.value).toBeGreaterThan(w.field);
  });

  it('keeps every column at its minimum on a dialog too narrow to fit', () => {
    const w = fitColumnWidths(300, 2)!;
    expect(w.field).toBe(160);
    expect(w.value).toBe(160);
  });
});
