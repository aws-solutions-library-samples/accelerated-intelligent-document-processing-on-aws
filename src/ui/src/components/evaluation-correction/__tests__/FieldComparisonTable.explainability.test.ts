// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * What the evaluation table knows about a field's assessment entry.
 *
 * This surface renders the shared bounding-box control directly, with no
 * coverage line beside it to cross-check against — so the only thing standing
 * between a wrong verdict and the reader is this lookup. It had no test file at
 * all, which is why the same verdict could be corrected repeatedly in the field
 * renderer and stay wrong here.
 *
 * `hasEntry` and `confidence` answer different questions and must not be
 * conflated: the lookup populates `confidence` only for a numeric value, while
 * "the assessment step skipped this field" is a claim about whether anything was
 * recorded at all. Getting that backwards sends the reader to check a value
 * against the page when the remedy is to assess the field, or the reverse.
 */

import { describe, expect, it } from 'vitest';

import { getFieldExplainabilityInfo } from '../FieldComparisonTable';

const scored = { confidence: 0.9, confidence_threshold: 0.8 };
const boxed = {
  confidence: 0.9,
  geometry: [{ boundingBox: { left: 0.1, top: 0.2, width: 0.3, height: 0.04 }, page: 1 }],
};

describe('getFieldExplainabilityInfo', () => {
  it('reports no entry when the field is absent from explainability', () => {
    // The case that matters: absent must not read as present, or the row claims
    // the value could not be matched on the page when nothing was ever recorded.
    const result = getFieldExplainabilityInfo(['Absent'], [{ Other: scored }]);
    expect(result.hasEntry).toBe(false);
    expect(result.confidence).toBeNull();
    expect(result.geometry).toBeNull();
  });

  it('reports no entry when there is no explainability at all', () => {
    expect(getFieldExplainabilityInfo(['Field'], null).hasEntry).toBe(false);
    expect(getFieldExplainabilityInfo(['Field'], []).hasEntry).toBe(false);
  });

  it('reports an entry that carries a confidence', () => {
    const result = getFieldExplainabilityInfo(['Field'], [{ Field: scored }]);
    expect(result.hasEntry).toBe(true);
    expect(result.confidence).toBe(0.9);
  });

  it('reports an entry that carries no numeric confidence', () => {
    // A field assessed at a finer granularity — a scalar whose entry is an object
    // of sub-field confidences — has an entry and no number of its own. It was
    // assessed, so it must not be reported as skipped.
    const nested = { Sub_one: scored, Sub_two: scored };
    const result = getFieldExplainabilityInfo(['Field'], [{ Field: nested }]);
    expect(result.hasEntry).toBe(true);
    expect(result.confidence).toBeNull();
  });

  it('returns geometry only when the entry carries a drawable box', () => {
    expect(getFieldExplainabilityInfo(['Field'], [{ Field: boxed }]).geometry).not.toBeNull();
    // A box needs both a boundingBox and a page; the overlay cannot place one
    // without them, so offering the locate control would depict the impossible.
    expect(getFieldExplainabilityInfo(['Field'], [{ Field: { confidence: 0.9, geometry: [{ page: 1 }] } }]).geometry).toBeNull();
    expect(getFieldExplainabilityInfo(['Field'], [{ Field: { confidence: 0.9, geometry: [] } }]).geometry).toBeNull();
  });

  it('resolves a field nested inside a list row', () => {
    const result = getFieldExplainabilityInfo(['Rows', 0, 'Cell'], [{ Rows: [{ Cell: scored }] }]);
    expect(result.hasEntry).toBe(true);
    expect(result.confidence).toBe(0.9);
  });
});
