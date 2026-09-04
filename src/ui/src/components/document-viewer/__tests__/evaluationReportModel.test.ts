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
