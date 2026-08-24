// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';

import { accuracyIntervalForField, formatBounds, formatMargin, isLowEvidence, wilsonInterval } from '../accuracyInterval';

describe('wilsonInterval', () => {
  it('never leaves [0, 1]', () => {
    // The reason this is Wilson and not the normal approximation: at n=20, p=0.90 the
    // normal interval puts the upper bound at 103%, in exactly the low-evidence case
    // these columns exist to surface.
    const [low, high] = wilsonInterval(18, 20);
    expect(low).toBeGreaterThanOrEqual(0);
    expect(high).toBeLessThanOrEqual(1);
    expect(high).toBeLessThan(1);
  });

  it('does not report a perfect small sample as certain', () => {
    const [low, high] = wilsonInterval(3, 3);
    expect(high).toBeCloseTo(1, 5);
    expect(low).toBeLessThan(0.5);
  });

  it('is uninformative rather than throwing when nothing was measured', () => {
    expect(wilsonInterval(0, 0)).toEqual([0, 1]);
    expect(wilsonInterval(1, -5)).toEqual([0, 1]);
  });

  it('tightens as observations accumulate', () => {
    const widths = [20, 100, 300, 500].map((n) => {
      const [low, high] = wilsonInterval(Math.round(0.9 * n), n);
      return high - low;
    });
    expect(widths).toEqual([...widths].sort((a, b) => b - a));
  });
});

describe('accuracyIntervalForField', () => {
  it('prefers the values the backend computed', () => {
    const interval = accuracyIntervalForField({
      tp: 90,
      fp: 5,
      tn: 0,
      fn: 5,
      accuracy_observations: 100,
      accuracy_margin: 0.0588,
      accuracy_low: 0.8412,
      accuracy_high: 0.9588,
    });
    expect(interval?.margin).toBeCloseTo(0.0588, 4);
    expect(interval?.low).toBeCloseTo(0.8412, 4);
  });

  it('falls back to the raw counts for runs aggregated before the backend change', () => {
    // Without this, the most useful column would be blank on every historical run.
    const interval = accuracyIntervalForField({ tp: 90, fp: 5, tn: 0, fn: 5 });
    expect(interval?.observations).toBe(100);
    expect(interval?.point).toBeCloseTo(0.9, 5);
    expect(interval?.low).toBeLessThan(0.9);
    expect(interval?.high).toBeGreaterThan(0.9);
  });

  it('returns null when nothing was measured', () => {
    // Rather than 0%, which reads as "always wrong" for a field never seen.
    expect(accuracyIntervalForField({})).toBeNull();
    expect(accuracyIntervalForField({ tp: 0, fp: 0, tn: 0, fn: 0 })).toBeNull();
  });

  it('separates a small sample from a large one at the same accuracy', () => {
    const few = accuracyIntervalForField({ tp: 9, fp: 0, tn: 0, fn: 1 });
    const many = accuracyIntervalForField({ tp: 900, fp: 0, tn: 0, fn: 100 });
    expect(few!.margin).toBeGreaterThan(5 * many!.margin);
  });
});

describe('formatting', () => {
  it('renders the margin in percentage points and bounds as a range', () => {
    const interval = accuracyIntervalForField({ tp: 90, fp: 5, tn: 0, fn: 5 });
    expect(formatMargin(interval)).toMatch(/^±\d+\.\d$/);
    expect(formatBounds(interval)).toMatch(/^\d+\.\d% – \d+\.\d%$/);
  });

  it('renders an em-dash when there is nothing to report', () => {
    expect(formatMargin(null)).toBe('—');
    expect(formatBounds(null)).toBe('—');
  });

  it('flags only intervals too wide to decide on', () => {
    expect(isLowEvidence(accuracyIntervalForField({ tp: 9, fp: 0, tn: 0, fn: 1 }))).toBe(true);
    expect(isLowEvidence(accuracyIntervalForField({ tp: 900, fp: 0, tn: 0, fn: 100 }))).toBe(false);
    expect(isLowEvidence(null)).toBe(false);
  });
});

describe('sorting', () => {
  it('orders margins numerically, not as formatted strings', () => {
    // n=30 at 90% gives ±11.1; n=40 gives ±9.5. As strings "±11.1" < "±9.5", so a
    // string sort puts the WIDER margin first — the opposite of what the column is
    // for. The accuracy column gets away with a string because toFixed(3) is
    // fixed-width; margins cross from one digit to two.
    const wide = accuracyIntervalForField({ tp: 27, fp: 0, tn: 0, fn: 3 })!;
    const narrow = accuracyIntervalForField({ tp: 36, fp: 0, tn: 0, fn: 4 })!;

    expect(wide.margin).toBeGreaterThan(narrow.margin);
    expect(formatMargin(wide) < formatMargin(narrow)).toBe(true);
  });
});
