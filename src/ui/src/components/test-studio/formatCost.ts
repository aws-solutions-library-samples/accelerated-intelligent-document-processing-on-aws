// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Money formatting for the run cost breakdown.
 *
 * The table used to interpolate raw numbers (`` `$${details.unit_cost}` ``), which
 * hands the reader whatever JavaScript's default number-to-string produces. On a
 * live run that meant two unreadable forms in the same column:
 *
 * - `$2.39999999999999996` — binary floating point noise, from a price that is
 *   exactly $2.40.
 * - `$2.4e-7` and `$6e-8` — exponent notation, which JS switches to below 1e-6.
 *   Nobody reads a bill in scientific notation.
 *
 * And `toFixed(4)` is not the fix on its own: applied to a per-unit price of
 * 2.4e-7 it renders `$0.0000`, which reads as free.
 *
 * So there are two formatters here, because the two columns have genuinely
 * different jobs. Estimated cost is a money column — aligned, fixed places.
 * Unit cost spans eight orders of magnitude in one table (dollars per 1M tokens
 * next to dollars per page), so it needs adaptive precision.
 */

/** Decimal places in the money column, and the floor below which it would read as $0.0000. */
const MONEY_PLACES = 4;

/** Significant digits kept for a value too small for the money column's fixed places. */
const SMALL_VALUE_SIGNIFICANT_DIGITS = 2;

/**
 * Decimal places needed to show `digits` significant digits of a sub-1 value,
 * capped at `toFixed`'s limit of 100.
 */
const placesForSmallValue = (abs: number, digits: number): number => Math.min(100, Math.ceil(-Math.log10(abs)) + digits);

/** Strip trailing zeros a fixed-place rendering added, without falling back to exponent notation. */
const trimTrailingZeros = (fixed: string): string => (fixed.includes('.') ? fixed.replace(/0+$/, '').replace(/\.$/, '') : fixed);

/**
 * A dollar amount for the Estimated Cost column: fixed places so the column
 * aligns, extended only when the value would otherwise round away to nothing.
 */
export const formatCostUsd = (value: number): string => {
  if (!Number.isFinite(value)) return 'N/A';
  if (value === 0) return `$${(0).toFixed(MONEY_PLACES)}`;

  const abs = Math.abs(value);
  if (abs >= 10 ** -MONEY_PLACES) return `$${value.toFixed(MONEY_PLACES)}`;

  // Too small for four places. Showing a couple of significant digits is honest;
  // $0.0000 is not. Trailing zeros are trimmed here and not above, because these
  // rows fall outside the fixed-place alignment anyway — a padded '$0.000000240'
  // buys nothing.
  return `$${trimTrailingZeros(value.toFixed(placesForSmallValue(abs, SMALL_VALUE_SIGNIFICANT_DIGITS)))}`;
};

/**
 * A per-unit price: as many places as the value needs and no more, so $2.40 does
 * not arrive as `$2.39999999999999996` and $0.00000024 does not arrive as
 * `$2.4e-7`.
 */
export const formatUnitCostUsd = (value: number): string => {
  if (!Number.isFinite(value)) return 'N/A';
  if (value === 0) return '$0';

  const abs = Math.abs(value);
  // Round away binary noise first, then trim what the rounding padded.
  const places = abs >= 0.01 ? MONEY_PLACES : placesForSmallValue(abs, 3);
  return `$${trimTrailingZeros(value.toFixed(places))}`;
};

/** Parse a value that may arrive from the API as either a number or a numeric string. */
export const asFiniteNumber = (value: unknown): number | null => {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (typeof value === 'string') {
    const parsed = Number.parseFloat(value.replace(/[$,]/g, ''));
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
};
