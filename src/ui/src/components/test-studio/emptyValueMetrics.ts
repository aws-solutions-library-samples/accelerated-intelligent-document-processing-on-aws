// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Reads `confidenceMetrics.empty_values` (#2052).
 *
 * The run-level AUROC, ECE and Brier score are computed over fields the extractor
 * returned a value for. A field it returned as empty is reported in a separate block,
 * so a scorer that rates missed fields highly shows up next to those figures rather
 * than being silently left out of them. Test runs aggregated before the block existed
 * have no `empty_values` key; this returns `null` for them and no row is rendered.
 */

import type { ConfidenceMetrics } from '../../graphql/awsjson-types';

export interface EmptyValueSummary {
  /** Fields the extractor returned no value for. */
  count: number;
  /** How many of those had a value in the ground truth. */
  wrong: number;
  /** AUROC over the empty predictions alone; null when undefined (one class only). */
  aurocEmptyOnly: number | null;
  /** AUROC over every scored field with the empty predictions included. */
  aurocIncludingEmpty: number | null;
  /** Sections evaluated before empty-value confidences were recorded. */
  sectionsWithoutRecord: number;
}

const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);

export const emptyValueSummary = (metrics: ConfidenceMetrics | null | undefined): EmptyValueSummary | null => {
  const block = metrics?.empty_values;
  if (!block || !finite(block.count) || !finite(block.wrong)) return null;
  const emptyOnly = block.overall?.auroc?.value;
  const including = block.including_empty?.auroc?.value;
  return {
    count: block.count,
    wrong: block.wrong,
    aurocEmptyOnly: finite(emptyOnly) ? emptyOnly : null,
    aurocIncludingEmpty: finite(including) ? including : null,
    sectionsWithoutRecord: finite(block.sections_without_record) ? block.sections_without_record : 0,
  };
};
