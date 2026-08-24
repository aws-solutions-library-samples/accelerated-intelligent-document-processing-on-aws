// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';

import { classificationComparisonForSection, describeVerdict } from '../classificationComparison';

const withDetails = (details: unknown[]) => ({
  doc_split_metrics: { section_details_with_order: details },
});

describe('classificationComparisonForSection', () => {
  it('reports a class mismatch for the matching section', () => {
    const result = classificationComparisonForSection(
      withDetails([
        { section_id: 'section_1', ground_truth_class: 'Invoice', predicted_class: 'Receipt' },
        { section_id: 'section_2', ground_truth_class: 'W2', predicted_class: 'W2' },
      ]),
      '1',
      2,
    );

    expect(result?.verdict).toBe('class');
    expect(result?.expected).toBe('Invoice');
    expect(result?.predicted).toBe('Receipt');
  });

  it('matches across both section-id conventions in the codebase', () => {
    // "1" (IDP section results) and "section_1" (doc-split details) both occur;
    // a raw string compare would silently never match one of them, which is
    // indistinguishable from "correctly classified".
    const bare = classificationComparisonForSection(
      withDetails([{ section_id: '1', ground_truth_class: 'Invoice', predicted_class: 'Receipt' }]),
      'section_1',
      2,
    );
    const prefixed = classificationComparisonForSection(
      withDetails([{ section_id: 'section_1', ground_truth_class: 'Invoice', predicted_class: 'Receipt' }]),
      1,
      2,
    );

    expect(bare?.verdict).toBe('class');
    expect(prefixed?.verdict).toBe('class');
  });

  it('reports a match when the class agrees', () => {
    const result = classificationComparisonForSection(
      withDetails([{ section_id: '1', ground_truth_class: 'Invoice', predicted_class: 'Invoice', order_matched: true }]),
      '1',
      1,
    );

    expect(result?.verdict).toBe('match');
  });

  it('separates a splitting failure from a labelling one', () => {
    const unmatched = classificationComparisonForSection(
      withDetails([{ section_id: '1', ground_truth_class: 'Invoice', predicted_class: null }]),
      '1',
      1,
    );
    const order = classificationComparisonForSection(
      withDetails([{ section_id: '1', ground_truth_class: 'Invoice', predicted_class: 'Invoice', order_matched: false }]),
      '1',
      1,
    );

    expect(unmatched?.verdict).toBe('unmatched');
    expect(order?.verdict).toBe('order');
  });

  it('falls back to the only entry for a single-section document', () => {
    // Ids need not agree when there is exactly one section on each side.
    const result = classificationComparisonForSection(
      withDetails([{ section_id: 'whatever-upstream-calls-it', ground_truth_class: 'Invoice', predicted_class: 'Receipt' }]),
      'sec-abc',
      1,
    );

    expect(result?.verdict).toBe('class');
  });

  it('refuses to guess in a multi-section document it cannot match', () => {
    // The important case: showing the WRONG expected class would make a reviewer
    // "correct" a document that was already right.
    const result = classificationComparisonForSection(
      withDetails([
        { section_id: 'a', ground_truth_class: 'Invoice', predicted_class: 'Receipt' },
        { section_id: 'b', ground_truth_class: 'W2', predicted_class: 'W2' },
      ]),
      'no-such-section',
      2,
    );

    expect(result).toBeNull();
  });

  it('returns null rather than throwing on missing or malformed evaluation data', () => {
    expect(classificationComparisonForSection(null, '1', 1)).toBeNull();
    expect(classificationComparisonForSection({}, '1', 1)).toBeNull();
    expect(classificationComparisonForSection(withDetails([]), '1', 1)).toBeNull();
    expect(classificationComparisonForSection({ doc_split_metrics: { section_details_with_order: 'nope' } }, '1', 1)).toBeNull();
    expect(classificationComparisonForSection(withDetails(['x', 7, null]), '1', 1)).toBeNull();
  });

  it('carries the page lists through for display', () => {
    const result = classificationComparisonForSection(
      withDetails([
        {
          section_id: '1',
          ground_truth_class: 'Invoice',
          predicted_class: 'Receipt',
          ground_truth_pages: [0, 1],
          predicted_pages: [0, 1, 2],
        },
      ]),
      '1',
      1,
    );

    expect(result?.expectedPages).toEqual([0, 1]);
    expect(result?.predictedPages).toEqual([0, 1, 2]);
  });
});

describe('describeVerdict', () => {
  it('warns that the fields below were extracted with the wrong schema', () => {
    const text = describeVerdict({
      expected: 'Invoice',
      predicted: 'Receipt',
      expectedPages: [],
      predictedPages: [],
      verdict: 'class',
    });

    expect(text).toContain('Receipt schema');
  });

  it('says extraction is unaffected by a page-order difference', () => {
    const text = describeVerdict({
      expected: 'Invoice',
      predicted: 'Invoice',
      expectedPages: [],
      predictedPages: [],
      verdict: 'order',
    });

    expect(text).toContain('Extraction is unaffected');
  });
});
