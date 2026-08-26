// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Tests for reading a page-level ground-truth-vs-predicted classification index
 * out of a document's evaluation results.json, and the verdicts the UI annotates
 * class values with.
 *
 * The shapes here mirror what stickler's doc-split metrics actually emit (see
 * `DocSplitClassificationMetrics` and `idp_common/evaluation/models.py`),
 * including its `"Missing"` placeholder for a page present on only one side.
 */

import { describe, expect, it } from 'vitest';
import {
  comparePageClass,
  compareSectionClass,
  evaluationResultsUriFrom,
  extractClassificationIndex,
  formatPageRanges,
  groundTruthClassForPage,
} from '../classification-comparison-utils';

// Ground truth: pages 1-2 are W2, page 3 is BankStatement, page 4 is Invoice.
// This run got page 3 wrong (classified Invoice).
const RESULTS = {
  doc_split_metrics: {
    page_level_accuracy: 0.75,
    total_pages: 4,
    correctly_classified_pages: 3,
    page_details: [
      { page_index: 0, ground_truth_class: 'W2', predicted_class: 'W2', correct: true },
      { page_index: 2, ground_truth_class: 'BankStatement', predicted_class: 'Invoice', correct: false },
      { page_index: 1, ground_truth_class: 'W2', predicted_class: 'W2', correct: true },
      { page_index: 3, ground_truth_class: 'Invoice', predicted_class: 'Invoice', correct: true },
    ],
  },
};

const INDEX = extractClassificationIndex(RESULTS);

describe('extractClassificationIndex', () => {
  it('reports no ground truth when the document has no doc-split metrics', () => {
    // This is what keeps a document with no evaluation looking exactly as before:
    // the panels annotate nothing when hasGroundTruth is false.
    expect(extractClassificationIndex({}).hasGroundTruth).toBe(false);
    expect(extractClassificationIndex(null).hasGroundTruth).toBe(false);
    expect(extractClassificationIndex({ doc_split_metrics: {} }).hasGroundTruth).toBe(false);
  });

  it('keys pages by 1-based page number regardless of the order they arrive in', () => {
    // results.json stores page_indices as page_id - min(page_id), so index 0 is
    // the document's first page. Keying on the raw index would annotate every
    // row one page off from what the page viewer shows.
    expect(INDEX.hasGroundTruth).toBe(true);
    expect(INDEX.byPageNumber.get(1)?.pageIndex).toBe(0);
    expect(INDEX.byPageNumber.get(3)?.groundTruthClass).toBe('BankStatement');
    expect(INDEX.byPageNumber.get(3)?.predictedClass).toBe('Invoice');
    expect(INDEX.byPageNumber.get(0)).toBeUndefined();
  });

  it('skips malformed entries rather than indexing them under NaN', () => {
    const index = extractClassificationIndex({
      doc_split_metrics: {
        page_details: [{ ground_truth_class: 'W2', predicted_class: 'W2', correct: true }, { page_index: 0 }],
      },
    });

    expect(index.byPageNumber.size).toBe(1);
    expect(index.byPageNumber.get(1)?.groundTruthClass).toBe('Missing');
  });
});

describe('groundTruthClassForPage', () => {
  it('returns the expected class for a known page', () => {
    expect(groundTruthClassForPage(INDEX, 3)).toBe('BankStatement');
  });

  it('returns null for a page the comparison does not cover', () => {
    expect(groundTruthClassForPage(INDEX, 99)).toBeNull();
  });

  it("treats stickler's 'Missing' placeholder as no ground truth", () => {
    // A page present only in the prediction has ground_truth_class "Missing".
    // Reading that as a class would claim ground truth expects a document type
    // literally called "Missing".
    const partial = extractClassificationIndex({
      doc_split_metrics: {
        page_details: [{ page_index: 0, ground_truth_class: 'Missing', predicted_class: 'W2', correct: false }],
      },
    });

    expect(groundTruthClassForPage(partial, 1)).toBeNull();
  });
});

describe('comparePageClass', () => {
  it('matches when the classes agree', () => {
    expect(comparePageClass(INDEX, 1, 'W2')).toBe('match');
  });

  it('mismatches when they disagree', () => {
    expect(comparePageClass(INDEX, 3, 'Invoice')).toBe('mismatch');
  });

  it('mismatches when the page was not classified at all', () => {
    expect(comparePageClass(INDEX, 3, null)).toBe('mismatch');
  });

  it('is unknown — not a mismatch — when there is no ground truth', () => {
    // An incomplete baseline must not be reported as a misclassification.
    expect(comparePageClass(INDEX, 99, 'W2')).toBe('unknown');
    expect(comparePageClass(extractClassificationIndex(null), 1, 'W2')).toBe('unknown');
  });
});

describe('compareSectionClass', () => {
  it('matches when every page of the section carries the section class', () => {
    const result = compareSectionClass(INDEX, [1, 2], 'W2');

    expect(result.verdict).toBe('match');
    expect(result.expected).toEqual([{ className: 'W2', pageNumbers: [1, 2] }]);
  });

  it('mismatches and names the expected class when the class is simply wrong', () => {
    const result = compareSectionClass(INDEX, [3], 'Invoice');

    expect(result.verdict).toBe('mismatch');
    expect(result.expected).toEqual([{ className: 'BankStatement', pageNumbers: [3] }]);
  });

  it('mismatches a section spanning two ground-truth classes even when one matches', () => {
    // Pages 3-4 are BankStatement and Invoice in ground truth. Passing this
    // because the section is "Invoice" would hide a merge: the section boundary
    // is what is wrong, and both classes are the diagnostic.
    const result = compareSectionClass(INDEX, [3, 4], 'Invoice');

    expect(result.verdict).toBe('mismatch');
    expect(result.expected.map((expected) => expected.className)).toEqual(['BankStatement', 'Invoice']);
  });

  it('orders expected classes by how many pages they cover', () => {
    const result = compareSectionClass(INDEX, [1, 2, 3], 'W2');

    expect(result.expected.map((expected) => expected.className)).toEqual(['W2', 'BankStatement']);
    expect(result.expected[0].pageNumbers).toEqual([1, 2]);
  });

  it('is unknown when ground truth covers none of the section pages', () => {
    const result = compareSectionClass(INDEX, [98, 99], 'W2');

    expect(result.verdict).toBe('unknown');
    expect(result.expected).toEqual([]);
    expect(result.unknownPageNumbers).toEqual([98, 99]);
  });

  it('still matches when the class agrees and only some pages have ground truth', () => {
    // Page 99 has no ground truth (an incomplete baseline). The class is right
    // for every page ground truth knows about, so calling this a *class* mismatch
    // would be a false positive; the uncovered page is recorded separately.
    const result = compareSectionClass(INDEX, [3, 99], 'BankStatement');

    expect(result.verdict).toBe('match');
    expect(result.expected).toEqual([{ className: 'BankStatement', pageNumbers: [3] }]);
    expect(result.unknownPageNumbers).toEqual([99]);
  });

  it('reports the uncovered pages when the class is also wrong', () => {
    const result = compareSectionClass(INDEX, [3, 99], 'Invoice');

    expect(result.verdict).toBe('mismatch');
    expect(result.unknownPageNumbers).toEqual([99]);
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

  it('refuses any report.md outside the evaluation prefix', () => {
    // Pinned to the documented evaluation key template rather than to any
    // `.md`, so another report's URI cannot yield a results path that does not
    // exist and would then read as a missing evaluation. A version snapshot's
    // longer prefix still ends `/evaluation/report.md`, so it is unaffected.
    expect(evaluationResultsUriFrom('s3://out/docs/pkg.pdf/summary/report.md')).toBeNull();
    expect(evaluationResultsUriFrom('s3://out/docs/pkg.pdf/evaluation/other.md')).toBeNull();
    expect(evaluationResultsUriFrom('s3://out/docs/pkg.pdf/runs/r1/evaluation/report.md')).toBe(
      's3://out/docs/pkg.pdf/runs/r1/evaluation/results.json',
    );
  });

  it('only rewrites the trailing report.md', () => {
    // A key that contains "report.md" earlier in the path must not be mangled.
    expect(evaluationResultsUriFrom('s3://out/report.md/x/evaluation/report.md')).toBe('s3://out/report.md/x/evaluation/results.json');
  });
});

describe('formatPageRanges', () => {
  it('collapses consecutive page numbers into ranges', () => {
    expect(formatPageRanges([1, 2, 3, 5])).toBe('1-3, 5');
    expect(formatPageRanges([4, 2, 3])).toBe('2-4');
    expect(formatPageRanges([7])).toBe('7');
    expect(formatPageRanges([])).toBe('—');
  });
});
