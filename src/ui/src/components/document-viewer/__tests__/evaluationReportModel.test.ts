// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';

import { evaluationMethodsUsed, formatScore, mismatchedAttributes, scoreBand, summarizeEvaluation } from '../evaluationReportModel';

describe('summarizeEvaluation', () => {
  const results = {
    overall_metrics: { precision: 0.9, recall: 0.8, f1_score: 0.85, weighted_overall_score: 0.93 },
    doc_split_metrics: { page_level_accuracy: 0.97 },
    section_results: [
      {
        section_id: '1',
        document_class: 'Invoice',
        attributes: [
          { name: 'a', matched: true },
          { name: 'b', matched: false },
          { name: 'c', matched: true },
        ],
      },
    ],
  };

  it('prefers the weighted score and says so', () => {
    const summary = summarizeEvaluation(results);
    expect(summary.extractionScore).toBeCloseTo(0.93, 5);
    expect(summary.extractionIsWeighted).toBe(true);
  });

  it('falls back to the raw match rate when nothing was weighted', () => {
    const summary = summarizeEvaluation({ ...results, overall_metrics: { f1_score: 0.5 } });
    expect(summary.extractionIsWeighted).toBe(false);
    expect(summary.extractionScore).toBeCloseTo(2 / 3, 5);
    expect(summary.matchedAttributes).toBe(2);
    expect(summary.totalAttributes).toBe(3);
  });

  it('keeps classification and extraction separate', () => {
    // They fail independently — a document can be classified perfectly and
    // extracted badly, or the reverse — so one number cannot stand for both.
    const summary = summarizeEvaluation(results);
    expect(summary.classificationScore).toBeCloseTo(0.97, 5);
    expect(summary.extractionScore).not.toBeCloseTo(0.97, 5);
  });

  it('reports null rather than zero for a figure that is absent', () => {
    // A zero would be indistinguishable from "scored 0%", which is the one
    // actively wrong reading available.
    const summary = summarizeEvaluation({ section_results: [] });
    expect(summary.extractionScore).toBeNull();
    expect(summary.classificationScore).toBeNull();
    expect(summary.f1Score).toBeNull();
    expect(summary.precision).toBeNull();
  });

  it('ignores non-numeric metric values', () => {
    const summary = summarizeEvaluation({
      overall_metrics: { f1_score: 'n/a', precision: null, weighted_overall_score: Number.NaN },
      section_results: [],
    });
    expect(summary.f1Score).toBeNull();
    expect(summary.precision).toBeNull();
    expect(summary.extractionScore).toBeNull();
  });

  it('surfaces an unscored document as excluded rather than as zero accuracy', () => {
    const summary = summarizeEvaluation({
      overall_metrics: { evaluation_excluded: true, exclusion_reason: 'no_extractable_schema' },
      section_results: [],
    });
    expect(summary.excluded).toBe(true);
    expect(summary.exclusionReason).toBe('no_extractable_schema');
  });

  it('counts sections and excluded sections', () => {
    const summary = summarizeEvaluation({ ...results, excluded_sections: ['2', '3'] });
    expect(summary.sectionCount).toBe(1);
    expect(summary.excludedSectionCount).toBe(2);
  });

  it('never throws on a malformed or empty payload', () => {
    expect(() => summarizeEvaluation(null)).not.toThrow();
    expect(() => summarizeEvaluation(undefined)).not.toThrow();
    expect(() => summarizeEvaluation({})).not.toThrow();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    expect(() => summarizeEvaluation({ section_results: 'nope' } as any)).not.toThrow();
    expect(summarizeEvaluation({}).totalAttributes).toBe(0);
  });
});

describe('mismatchedAttributes', () => {
  it('returns only the fields that did not match', () => {
    const section = {
      attributes: [{ name: 'a', matched: true }, { name: 'b', matched: false }, { name: 'c' }],
    };
    // An attribute with no `matched` at all counts as not matched — treating an
    // unknown as a pass would overstate accuracy.
    expect(mismatchedAttributes(section).map((a) => a.name)).toEqual(['b', 'c']);
  });

  it('handles a section with no attributes', () => {
    expect(mismatchedAttributes({})).toEqual([]);
  });
});

describe('evaluationMethodsUsed', () => {
  it('lists the distinct comparison methods, sorted', () => {
    const methods = evaluationMethodsUsed({
      section_results: [
        { attributes: [{ evaluation_method: 'FUZZY' }, { evaluation_method: 'EXACT' }] },
        { attributes: [{ evaluation_method: 'FUZZY' }, { evaluation_method: null }] },
      ],
    });
    expect(methods).toEqual(['EXACT', 'FUZZY']);
  });

  it('returns empty rather than throwing when there is nothing to read', () => {
    expect(evaluationMethodsUsed(null)).toEqual([]);
    expect(evaluationMethodsUsed({})).toEqual([]);
  });
});

describe('scoreBand and formatScore', () => {
  it('bands on the same thresholds the markdown report used', () => {
    // So the two do not disagree about what "good" means while both exist.
    expect(scoreBand(0.95)).toBe('good');
    expect(scoreBand(0.9)).toBe('good');
    expect(scoreBand(0.75)).toBe('fair');
    expect(scoreBand(0.6)).toBe('poor');
    expect(scoreBand(0.2)).toBe('bad');
    expect(scoreBand(null)).toBe('unknown');
  });

  it('formats a proportion as a percentage, and null as an em-dash', () => {
    expect(formatScore(0.9421)).toBe('94.2%');
    expect(formatScore(1)).toBe('100.0%');
    expect(formatScore(null)).toBe('—');
  });
});

// ---------------------------------------------------------------------------
// Parity shapes. Each function below mirrors one section of the markdown
// report; the fixtures use the key names results.json actually carries (taken
// from a live payload), so a renamed key fails here rather than rendering as
// an empty table.
// ---------------------------------------------------------------------------

import {
  attributeRows,
  describeValue,
  excludedSectionRows,
  formatDuration,
  metricRows,
  rateMetric,
  sectionFailure,
  skippedFieldCount,
  splitAnalysis,
} from '../evaluationReportModel';

describe('metricRows', () => {
  it('keeps numbers, rates the ones the markdown rates, and skips internal state', () => {
    const rows = metricRows({
      precision: 0.95,
      false_alarm_rate: 0.05,
      accuracy: 0.6,
      _stickler_counts: { tp: 1 },
      evaluation_failed: false,
      skipped_field_count: 2,
      failure_type: 'x',
      some_new_metric: 0.4,
    });
    expect(rows.map((r) => r.metric)).toEqual(['precision', 'false_alarm_rate', 'accuracy', 'some_new_metric']);
    expect(rows[0].band).toBe('good');
    // Lower is better for error rates: 0.05 is excellent, not poor.
    expect(rows[1].band).toBe('good');
    expect(rows[2].band).toBe('poor');
    // Unknown metrics get a value and no rating, exactly as the markdown prints them.
    expect(rows[3].band).toBeNull();
  });

  it('shows an explicit null as not scored rather than dropping it', () => {
    const rows = metricRows({ weighted_overall_score: null });
    expect(rows).toEqual([{ metric: 'weighted_overall_score', value: null, band: null }]);
  });

  it('rates error metrics on the inverted scale', () => {
    expect(rateMetric('false_discovery_rate', 0.6)).toBe('bad');
    expect(rateMetric('false_discovery_rate', 0.25)).toBe('fair');
    expect(rateMetric('unknown_metric', 0.99)).toBeNull();
  });
});

describe('splitAnalysis', () => {
  const payload = {
    doc_split_metrics: {
      page_level_accuracy: 0.5,
      split_accuracy_without_order: 0.0,
      split_accuracy_with_order: 0.0,
      total_pages: 2,
      total_splits: 1,
      correctly_classified_pages: 1,
      correctly_split_without_order: 0,
      correctly_split_with_order: 0,
      section_details_without_order: [
        {
          section_id: '1',
          ground_truth_class: 'W2',
          ground_truth_pages: [0],
          matched: false,
          matched_section_id: null,
          predicted_class: 'No Match',
          predicted_pages: [],
        },
      ],
      section_details_with_order: [
        {
          section_id: '1',
          ground_truth_class: 'W2',
          ground_truth_pages: [0],
          matched: false,
          order_matched: false,
          matched_section_id: null,
          predicted_class: 'No Match',
          predicted_pages: [],
        },
      ],
      predicted_sections: [
        { section_id: '1', document_class: 'form', page_indices: [0] },
        { section_id: '2', document_class: 'form', page_indices: [1] },
      ],
      errors: ['page 3 missing'],
      final_score: 1.0,
      clustering_score: 1.0,
      v_measure: null,
      rand_index: 1.0,
      avg_ordering_score: 1.0,
    },
  };

  it('is null for a document evaluated without section ground truth', () => {
    expect(splitAnalysis({})).toBeNull();
    expect(splitAnalysis(null)).toBeNull();
  });

  it('carries the three counts the markdown summary leads with', () => {
    const split = splitAnalysis(payload)!;
    expect(split.pageLevel).toEqual({ score: 0.5, correct: 1, total: 2 });
    expect(split.withoutOrder).toEqual({ score: 0, correct: 0, total: 1 });
    expect(split.withOrder).toEqual({ score: 0, correct: 0, total: 1 });
  });

  it('lists expected sections first, then predicted sections nothing matched', () => {
    const split = splitAnalysis(payload)!;
    expect(split.rows).toHaveLength(3);
    expect(split.rows[0]).toMatchObject({
      sectionId: '1',
      sectionMatched: false,
      orderMatched: false,
      expectedClass: 'W2',
      predictedClass: 'No Match',
    });
    // Unmatched predictions have no expected side; the markdown prints "N/A" there.
    expect(split.rows[1]).toMatchObject({ sectionId: null, expectedClass: null, predictedClass: 'form', matchedSectionId: '1' });
    expect(split.rows[2]).toMatchObject({ sectionId: null, predictedClass: 'form', matchedSectionId: '2' });
  });

  it('does not list a predicted section that an expected one matched', () => {
    const matched = JSON.parse(JSON.stringify(payload));
    matched.doc_split_metrics.section_details_with_order[0].matched_section_id = '1';
    const split = splitAnalysis(matched)!;
    expect(split.rows.map((r) => r.matchedSectionId)).toEqual(['1', '2']);
  });

  it('converts 0-based page indices to the 1-based numbers the UI shows', () => {
    const split = splitAnalysis(payload)!;
    expect(split.rows[0].expectedPages).toEqual([1]);
    expect(split.rows[2].predictedPages).toEqual([2]);
  });

  it('keeps the graded score and the error list', () => {
    const split = splitAnalysis(payload)!;
    expect(split.graded).toMatchObject({ finalScore: 1, vMeasure: null });
    expect(split.errors).toEqual(['page 3 missing']);
  });

  it('reports no graded score for an older payload that has none', () => {
    const older = JSON.parse(JSON.stringify(payload));
    delete older.doc_split_metrics.final_score;
    expect(splitAnalysis(older)!.graded).toBeNull();
  });
});

describe('excludedSectionRows', () => {
  it('names the section, class, reason and pages', () => {
    // page_ids come from Section.page_ids: 1-based, and strings. Not the 0-based
    // indices the split tables carry, so no shift.
    const rows = excludedSectionRows({
      excluded_sections: [
        { section_id: '3', classification: 'Cover', exclusion_reason: 'no extractable attributes', page_ids: ['4', '5'] },
      ],
    });
    expect(rows).toEqual([{ sectionId: '3', classification: 'Cover', reason: 'no extractable attributes', pages: [4, 5] }]);
  });

  it('defaults the reason as the markdown does', () => {
    expect(excludedSectionRows({ excluded_sections: [{ section_id: '1' }] })[0].reason).toBe('excluded');
  });
});

describe('sectionFailure', () => {
  it('is null for a scored section', () => {
    expect(sectionFailure({ metrics: { f1_score: 1 } })).toBeNull();
  });

  it('gives the reason and the steps for a known failure type', () => {
    const failure = sectionFailure({
      document_class: 'Invoice',
      metrics: { evaluation_failed: true, failure_type: 'missing_schema_configuration' },
      attributes: [{ reason: 'No evaluation configuration for Invoice' }],
    })!;
    expect(failure.reason).toBe('No evaluation configuration for Invoice');
    expect(failure.steps[0]).toContain("'Invoice'");
    expect(failure.steps).toHaveLength(3);
  });

  it('refuses to guess remediation for an unknown or missing failure type', () => {
    // The markdown does the same: advice for the wrong cause is worse than none.
    expect(sectionFailure({ metrics: { evaluation_failed: true } })!.steps).toEqual([]);
    expect(sectionFailure({ metrics: { evaluation_failed: true, failure_type: 'something_new' } })!.steps).toEqual([]);
  });
});

describe('skippedFieldCount', () => {
  it('reads the count and defaults to zero', () => {
    expect(skippedFieldCount({ metrics: { skipped_field_count: 2 } })).toBe(2);
    expect(skippedFieldCount({ metrics: {} })).toBe(0);
    expect(skippedFieldCount({})).toBe(0);
  });
});

describe('attributeRows', () => {
  it('attaches nested comparisons only above the threshold the markdown uses', () => {
    const rows = attributeRows([
      { name: 'total', expected: 1, actual: 1, matched: true, field_comparison_details: [{ expected_key: 'total', match: true }] },
      {
        name: 'items',
        expected: [{ a: 1 }],
        actual: [{ a: 1 }],
        matched: true,
        score: 1,
        field_comparison_details: [
          {
            expected_key: 'items[0].a',
            actual_key: 'items[1].a',
            expected_value: 1,
            actual_value: 1,
            match: 'true',
            score: 1,
            weight: 2,
            evaluation_method: 'Exact',
            reason: 'exact match',
          },
          {
            expected_key: 'items[1].a',
            actual_key: 'items[0].a',
            expected_value: 2,
            actual_value: 3,
            match: false,
            score: 0,
            evaluation_method: 'Exact',
            reason: 'differs',
          },
        ],
      },
    ]);
    // One detail restates the attribute itself: no children.
    expect(rows[0].children).toBeUndefined();
    expect(rows[1].children).toHaveLength(2);
    // A string "true" from Stickler counts as a match; a moved list index is reported.
    // Paths are relative to the parent attribute: "items[0].a" under "items" reads "[0].a".
    expect(rows[1].children![0]).toMatchObject({ name: '[0].a', matched: true, weight: 2, actualPath: '[1].a' });
    expect(rows[1].children![1]).toMatchObject({ matched: false, score: 0, reason: 'differs' });
  });

  it('gives every row a stable, unique key even when names repeat', () => {
    const rows = attributeRows([{ name: 'x' }, { name: 'x' }], 's1/');
    expect(new Set(rows.map((r) => r.key)).size).toBe(2);
    expect(rows[0].key.startsWith('s1/')).toBe(true);
  });
});

describe('describeValue', () => {
  it('summarises structures by size and leaves scalars alone', () => {
    expect(describeValue([1, 2, 3])).toEqual({ kind: 'list', summary: '3 items' });
    expect(describeValue([1])).toEqual({ kind: 'list', summary: '1 item' });
    expect(describeValue({ a: 1 })).toEqual({ kind: 'object', summary: '1 field' });
    expect(describeValue('Jane Doe')).toEqual({ kind: 'scalar', summary: 'Jane Doe' });
    expect(describeValue(0)).toEqual({ kind: 'scalar', summary: '0' });
  });

  it('treats null, undefined and empty string as absent', () => {
    for (const v of [null, undefined, '']) expect(describeValue(v).kind).toBe('empty');
  });
});

describe('formatDuration', () => {
  it('formats seconds and minutes, and null when unrecorded', () => {
    expect(formatDuration(12.34)).toBe('12.3 s');
    expect(formatDuration(125)).toBe('2 min 5 s');
    expect(formatDuration(undefined)).toBeNull();
  });
});
