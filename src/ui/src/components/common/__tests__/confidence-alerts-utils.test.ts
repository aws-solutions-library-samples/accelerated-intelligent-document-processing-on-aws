// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Regression tests for getFieldConfidenceInfo's handling of decomposed-string fields.
 *
 * Some assessments split a single plain-string field (e.g. "Insurance Company")
 * into sub-keyed child assessments rather than returning a flat {confidence} on the
 * field itself. The field object then has no top-level `confidence`, so the
 * Document Data panel previously showed no confidence at all. getFieldConfidenceInfo
 * now aggregates the children (minimum confidence — the worst-case signal for
 * review) so these fields display a confidence again.
 */

import { describe, expect, it } from 'vitest';
import { getFieldConfidenceInfo } from '../confidence-alerts-utils';

describe('getFieldConfidenceInfo decomposed-string aggregation', () => {
  it('reads a flat field confidence as before', () => {
    const explainability = [{ Insured_Property: { confidence: 0.99, confidence_threshold: 0.8 } }];
    const info = getFieldConfidenceInfo('Insured_Property', explainability);
    expect(info.hasConfidenceInfo).toBe(true);
    expect(info.confidence).toBe(0.99);
    expect(info.confidenceThreshold).toBe(0.8);
  });

  it('aggregates child confidences (minimum) when the field has no top-level confidence', () => {
    const explainability = [
      {
        'Insurance Company': {
          'Fake Insurance Co': { confidence: 0.99, confidence_threshold: 0.8 },
          '650 Davis Street': { confidence: 1.0, confidence_threshold: 0.8 },
          'San Francisco, CA 94111': { confidence: 0.98, confidence_threshold: 0.8 },
          confidence_threshold: 0.8,
        },
      },
    ];
    const info = getFieldConfidenceInfo('Insurance Company', explainability);
    expect(info.hasConfidenceInfo).toBe(true);
    // Minimum of the three children.
    expect(info.confidence).toBe(0.98);
    expect(info.confidenceThreshold).toBe(0.8);
    expect(info.isAboveThreshold).toBe(true);
  });

  it('flags below-threshold when the worst child is below threshold', () => {
    const explainability = [
      {
        'Insurance Company': {
          'Fake Insurance Co': { confidence: 0.99, confidence_threshold: 0.8 },
          '650 Davis Street': { confidence: 0.5, confidence_threshold: 0.8 },
        },
      },
    ];
    const info = getFieldConfidenceInfo('Insurance Company', explainability);
    expect(info.confidence).toBe(0.5);
    expect(info.isAboveThreshold).toBe(false);
    expect(info.shouldHighlight).toBe(true);
  });

  it('returns no confidence info when neither the field nor its children have confidence', () => {
    const explainability = [{ SomeGroup: { State: { geometry: [] }, ZipCode: { geometry: [] } } }];
    const info = getFieldConfidenceInfo('SomeGroup', explainability);
    expect(info.hasConfidenceInfo).toBe(false);
  });

  it('ignores array children when aggregating', () => {
    const explainability = [
      {
        Field: {
          notes: ['a', 'b'],
          child: { confidence: 0.7, confidence_threshold: 0.8 },
        },
      },
    ];
    const info = getFieldConfidenceInfo('Field', explainability);
    expect(info.confidence).toBe(0.7);
  });
});

describe('getFieldConfidenceInfo entry resolution', () => {
  const scored = { confidence: 0.9, confidence_threshold: 0.8 };

  it('resolves a primitive list item, whose name is its index', () => {
    // The renderer names a primitive list item `[0]` and puts the index in the
    // path, so the navigation has already reached the element: looking for a
    // child key called "[0]" finds nothing, and the field reads as never
    // assessed. `Keywords: ['alpha']` is the shape.
    const info = getFieldConfidenceInfo('[0]', [{ Keywords: [scored] }], ['Keywords', 0], null);
    expect(info.hasEntry).toBe(true);
    expect(info.confidence).toBe(0.9);
  });

  it('resolves a named field inside a list row unchanged', () => {
    // Array items that are objects were never affected — their leaves are named
    // fields — and must stay that way.
    const info = getFieldConfidenceInfo('Cell', [{ Rows: [{ Cell: scored }] }], ['Rows', 0], null);
    expect(info.hasEntry).toBe(true);
  });

  it('reports no entry for a field that is genuinely absent', () => {
    // The distinction the caller draws between "assessed" and "skipped" depends
    // on this staying false.
    expect(getFieldConfidenceInfo('Absent', [{ Other: scored }], [], null).hasEntry).toBe(false);
  });

  it('reports an entry that carries no usable confidence number', () => {
    // Assessed at a finer granularity: an entry exists, with sub-field
    // confidences and none of its own.
    const nested = { Sub_one: scored, Sub_two: scored };
    const info = getFieldConfidenceInfo('Field', [{ Field: nested }], [], null);
    expect(info.hasEntry).toBe(true);
  });
});
