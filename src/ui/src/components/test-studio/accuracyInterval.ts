// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Sampling uncertainty for a measured per-field accuracy.
 *
 * The backend computes this too (`idp_common/evaluation/intervals.py`) and that is the
 * source of truth for API, MLflow and CLI consumers. This mirrors the same formula so
 * runs aggregated before the backend change still show an interval — otherwise the most
 * useful column would be blank on every historical run. Prefer the backend value when
 * present; only fall back to computing locally.
 *
 * Keep the two in step: same interval (Wilson), same denominator (tp+tn over
 * tp+fp+tn+fn).
 */

/** Two-sided 95% normal quantile. */
const Z_95 = 1.959963984540054;

export interface AccuracyInterval {
  point: number;
  low: number;
  high: number;
  /** Half the interval width, in proportion units. 0.059 === 5.9 percentage points. */
  margin: number;
  observations: number;
}

/**
 * Wilson score interval for a proportion.
 *
 * Not the normal approximation: per-field results routinely sit at 0% or 100% on few
 * observations, where the normal interval leaves [0, 1] entirely — at n=20 and p=0.90 it
 * reports an upper bound of 103%. An impossible accuracy makes the reader discount the
 * number rather than the sample size, which is the opposite of the point.
 */
export const wilsonInterval = (successes: number, total: number, z: number = Z_95): [number, number] => {
  if (!Number.isFinite(total) || total <= 0) return [0, 1];
  const p = successes / total;
  const denominator = 1 + (z * z) / total;
  const centre = (p + (z * z) / (2 * total)) / denominator;
  const halfWidth = (z * Math.sqrt((p * (1 - p)) / total + (z * z) / (4 * total * total))) / denominator;
  return [Math.max(0, centre - halfWidth), Math.min(1, centre + halfWidth)];
};

/**
 * Build the interval for one field, preferring values the backend already computed.
 *
 * Returns null when nothing was measured — a field no document contained has no
 * accuracy, and rendering 0% would read as "always wrong".
 */
export const accuracyIntervalForField = (metrics: {
  tp?: number;
  fp?: number;
  tn?: number;
  fn?: number;
  accuracy_observations?: number;
  accuracy_margin?: number;
  accuracy_low?: number;
  accuracy_high?: number;
}): AccuracyInterval | null => {
  const tp = metrics.tp ?? 0;
  const fp = metrics.fp ?? 0;
  const tn = metrics.tn ?? 0;
  const fn = metrics.fn ?? 0;
  const total = metrics.accuracy_observations ?? tp + fp + tn + fn;
  if (!total || total <= 0) return null;

  const point = (tp + tn) / (tp + fp + tn + fn || 1);

  if (metrics.accuracy_margin !== undefined && metrics.accuracy_low !== undefined && metrics.accuracy_high !== undefined) {
    return {
      point,
      low: metrics.accuracy_low,
      high: metrics.accuracy_high,
      margin: metrics.accuracy_margin,
      observations: total,
    };
  }

  const [low, high] = wilsonInterval(tp + tn, total);
  return { point, low, high, margin: (high - low) / 2, observations: total };
};

/** "±5.9" — the half-width in percentage points, for the table cell. */
export const formatMargin = (interval: AccuracyInterval | null): string => (interval ? `±${(interval.margin * 100).toFixed(1)}` : '—');

/** "82.6% – 94.5%" — the authoritative bounds, for the popover. */
export const formatBounds = (interval: AccuracyInterval | null): string =>
  interval ? `${(interval.low * 100).toFixed(1)}% – ${(interval.high * 100).toFixed(1)}%` : '—';

/**
 * True when the interval is too wide for the number to support a decision.
 *
 * Deliberately a hint and not a warning: a wide interval is a statement about how much
 * evidence there is, not a defect in the field.
 */
export const isLowEvidence = (interval: AccuracyInterval | null, thresholdPts = 10): boolean =>
  interval !== null && interval.margin * 100 > thresholdPts;
