// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Tests for reading a ground-truth-vs-predicted classification view out of a
 * document's evaluation results.json.
 *
 * The shapes here mirror what stickler's doc-split metrics actually emit (see
 * `DocSplitClassificationMetrics` and `idp_common/evaluation/models.py`),
 * including its placeholders: `"Missing"` for a page present on only one side
 * and `"No Match"` for a ground-truth section nothing matched.
 */

import { describe, expect, it } from 'vitest';
import {
  countPageMismatches,
  countSectionMismatches,
  evaluationResultsUriFrom,
  extractClassificationComparison,
  formatAccuracy,
  formatPageRanges,
} from '../classification-comparison-utils';

const RESULTS = {
  doc_split_metrics: {
    page_level_accuracy: 0.75,
    split_accuracy_without_order: 0.5,
    split_accuracy_with_order: 0.5,
    total_pages: 4,
    correctly_classified_pages: 3,
    page_details: [
      { page_index: 0, ground_truth_class: 'W2', predicted_class: 'W2', correct: true },
      { page_index: 2, ground_truth_class: 'BankStatement', predicted_class: 'Invoice', correct: false },
      { page_index: 1, ground_truth_class: 'W2', predicted_class: 'W2', correct: true },
      { page_index: 3, ground_truth_class: 'Invoice', predicted_class: 'Invoice', correct: true },
    ],
    section_details_without_order: [
      {
        section_id: 'gt-1',
        ground_truth_class: 'W2',
        ground_truth_pages: [0, 1],
        matched: true,
        matched_section_id: '1',
        predicted_class: 'W2',
        predicted_pages: [0, 1],
      },
      {
        section_id: 'gt-2',
        ground_truth_class: 'BankStatement',
        ground_truth_pages: [2],
        matched: false,
        matched_section_id: null,
        predicted_class: 'No Match',
        predicted_pages: [],
      },
    ],
    section_details_with_order: [
      {
        section_id: 'gt-1',
        ground_truth_class: 'W2',
        ground_truth_pages: [0, 1],
        matched: true,
        order_matched: true,
        matched_section_id: '1',
        predicted_class: 'W2',
        predicted_pages: [0, 1],
      },
      {
        section_id: 'gt-2',
        ground_truth_class: 'BankStatement',
        ground_truth_pages: [2],
        matched: false,
        order_matched: false,
        matched_section_id: null,
        predicted_class: 'No Match',
        predicted_pages: [],
      },
    ],
    predicted_sections: [
      { section_id: '1', document_class: 'W2', page_indices: [0, 1] },
      { section_id: '2', document_class: 'Invoice', page_indices: [2, 3] },
    ],
    errors: [],
  },
};

describe('extractClassificationComparison', () => {
  it('returns null when the document has no doc-split metrics', () => {
    // Absent metrics mean "not evaluated against section-level ground truth",
    // which the panel must treat as nothing to show rather than a perfect score.
    expect(extractClassificationComparison({})).toBeNull();
    expect(extractClassificationComparison(null)).toBeNull();
    expect(extractClassificationComparison({ doc_split_metrics: {} })).toBeNull();
  });

  it('reads per-page ground truth and predicted classes, sorted by page', () => {
    const comparison = extractClassificationComparison(RESULTS)!;

    expect(comparison.pages.map((page) => page.pageNumber)).toEqual([1, 2, 3, 4]);
    const mismatch = comparison.pages.find((page) => !page.correct)!;
    expect(mismatch.groundTruthClass).toBe('BankStatement');
    expect(mismatch.predictedClass).toBe('Invoice');
  });

  it('converts 0-based page indices to 1-based page numbers', () => {
    // results.json stores page_indices as page_id - min(page_id), so index 0 is
    // the document's first page. Displaying the raw index would report every
    // page one lower than the page viewer does.
    const comparison = extractClassificationComparison(RESULTS)!;
    const firstPage = comparison.pages[0];

    expect(firstPage.pageIndex).toBe(0);
    expect(firstPage.pageNumber).toBe(1);
    expect(comparison.sections[0].groundTruthPages).toEqual([1, 2]);
  });

  it('carries the summary accuracies through', () => {
    const comparison = extractClassificationComparison(RESULTS)!;

    expect(comparison.pageLevelAccuracy).toBe(0.75);
    expect(comparison.splitAccuracyWithoutOrder).toBe(0.5);
    expect(comparison.splitAccuracyWithOrder).toBe(0.5);
    expect(comparison.correctPages).toBe(3);
    expect(comparison.totalPages).toBe(4);
  });

  it("renders stickler's 'No Match' placeholder as no predicted class", () => {
    const comparison = extractClassificationComparison(RESULTS)!;
    const unmatched = comparison.sections.find((section) => section.sectionId === 'gt-2')!;

    expect(unmatched.matched).toBe(false);
    // Not the literal string — a class named "No Match" does not exist.
    expect(unmatched.predictedClass).toBeNull();
  });

  it('includes predicted sections that no ground-truth section matched', () => {
    // Section '2' covers pages 3-4 and matches nothing in ground truth. Omitting
    // it would show only half of the split error (the missing GT section) and
    // leave the extra predicted section invisible.
    const comparison = extractClassificationComparison(RESULTS)!;
    const extra = comparison.sections.find((section) => section.predictedSectionId === '2')!;

    expect(extra.groundTruthClass).toBeNull();
    expect(extra.predictedClass).toBe('Invoice');
    expect(extra.predictedPages).toEqual([3, 4]);
    expect(extra.matched).toBe(false);
  });

  it('does not repeat a predicted section that already matched', () => {
    const comparison = extractClassificationComparison(RESULTS)!;
    const rowsForSection1 = comparison.sections.filter((section) => section.predictedSectionId === '1');

    expect(rowsForSection1).toHaveLength(1);
  });

  it('gives every section row a unique key', () => {
    const comparison = extractClassificationComparison(RESULTS)!;
    const keys = comparison.sections.map((section) => section.rowKey);

    expect(new Set(keys).size).toBe(keys.length);
  });

  it('flags a section matched on pages and class but not on order', () => {
    const results = {
      doc_split_metrics: {
        section_details_without_order: [
          { section_id: 'gt-1', ground_truth_class: 'W2', ground_truth_pages: [1, 0], matched: true, matched_section_id: '1' },
        ],
        section_details_with_order: [
          {
            section_id: 'gt-1',
            ground_truth_class: 'W2',
            ground_truth_pages: [1, 0],
            matched: true,
            order_matched: false,
            matched_section_id: '1',
            predicted_class: 'W2',
            predicted_pages: [0, 1],
          },
        ],
      },
    };

    const section = extractClassificationComparison(results)!.sections[0];
    expect(section.matched).toBe(true);
    expect(section.orderMatched).toBe(false);
  });

  it('handles a page present on only one side', () => {
    const results = {
      doc_split_metrics: {
        page_details: [{ page_index: 0, ground_truth_class: 'W2', predicted_class: 'Missing', correct: false }],
      },
    };

    const comparison = extractClassificationComparison(results)!;
    expect(comparison.pages[0].predictedClass).toBe('Missing');
    expect(countPageMismatches(comparison)).toBe(1);
    // Derived when the metrics block omits the totals.
    expect(comparison.totalPages).toBe(1);
    expect(comparison.correctPages).toBe(0);
    expect(comparison.pageLevelAccuracy).toBeNull();
  });

  it('surfaces section-load errors rather than dropping them', () => {
    const results = {
      doc_split_metrics: {
        page_details: [{ page_index: 0, ground_truth_class: 'W2', predicted_class: 'W2', correct: true }],
        errors: ['Error loading section data from s3://bucket/key: denied'],
      },
    };

    expect(extractClassificationComparison(results)!.errors).toHaveLength(1);
  });
});

describe('mismatch counts', () => {
  it('counts page and section mismatches', () => {
    const comparison = extractClassificationComparison(RESULTS)!;

    expect(countPageMismatches(comparison)).toBe(1);
    // gt-2 (nothing matched) plus predicted section '2' (extra).
    expect(countSectionMismatches(comparison)).toBe(2);
  });
});

describe('evaluationResultsUriFrom', () => {
  it('derives results.json from the report URI', () => {
    expect(evaluationResultsUriFrom('s3://out/docs/pkg.pdf/evaluation/report.md')).toBe('s3://out/docs/pkg.pdf/evaluation/results.json');
  });

  it('returns null when there is no report URI or it is not the report', () => {
    expect(evaluationResultsUriFrom(undefined)).toBeNull();
    expect(evaluationResultsUriFrom('')).toBeNull();
    expect(evaluationResultsUriFrom('s3://out/docs/pkg.pdf/summary/summary.md')).toBeNull();
  });

  it('only rewrites the trailing report.md', () => {
    // A key that contains "report.md" earlier in the path must not be mangled.
    expect(evaluationResultsUriFrom('s3://out/report.md/x/evaluation/report.md')).toBe('s3://out/report.md/x/evaluation/results.json');
  });
});

describe('formatting helpers', () => {
  it('formats accuracy as a percentage, or an em dash when absent', () => {
    expect(formatAccuracy(0.75)).toBe('75.0%');
    expect(formatAccuracy(1)).toBe('100.0%');
    expect(formatAccuracy(null)).toBe('—');
  });

  it('collapses consecutive page numbers into ranges', () => {
    expect(formatPageRanges([1, 2, 3, 5])).toBe('1-3, 5');
    expect(formatPageRanges([4, 2, 3])).toBe('2-4');
    expect(formatPageRanges([7])).toBe('7');
    expect(formatPageRanges([])).toBe('—');
  });
});
