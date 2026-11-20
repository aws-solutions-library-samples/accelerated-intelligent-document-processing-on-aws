// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';

import { emptyValueSummary } from '../emptyValueMetrics';

describe('emptyValueSummary', () => {
  it('reads the empty-prediction block, and nothing for older runs', () => {
    expect(
      emptyValueSummary({
        empty_values: {
          count: 7,
          wrong: 5,
          with_confidence: 6,
          sections_without_record: 1,
          overall: { auroc: { value: 0 } },
          including_empty: { auroc: { value: 0.25 } },
        },
      }),
    ).toEqual({ count: 7, wrong: 5, aurocEmptyOnly: 0, aurocIncludingEmpty: 0.25, sectionsWithoutRecord: 1 });
    // A run aggregated before the block existed renders no row at all.
    expect(emptyValueSummary({ overall: { auroc: { value: 0.7 } } })).toBeNull();
    // An undefined AUROC (one class only) stays null rather than reading as 0.
    expect(
      emptyValueSummary({
        empty_values: {
          count: 2,
          wrong: 2,
          with_confidence: 2,
          sections_without_record: 0,
          overall: { auroc: { value: null } },
          including_empty: { auroc: { value: null } },
        },
      }),
    ).toMatchObject({ aurocEmptyOnly: null, aurocIncludingEmpty: null });
  });
});
