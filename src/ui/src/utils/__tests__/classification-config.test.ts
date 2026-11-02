// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { describe, expect, it } from 'vitest';
import {
  classAssigningLlmPath,
  classificationModelOf,
  flatClassificationKeysIn,
  pageLimitBecomesOneDocument,
  setClassificationModel,
  SECTION_SPLITTING_VALUES,
  unknownSectionSplittingIn,
} from '../classification-config';
import pageLimitFixtures from './page-limit.fixtures.json';

describe('classAssigningLlmPath', () => {
  it('is the combined call for combined, and by default', () => {
    expect(classAssigningLlmPath({ strategy: 'combined' })).toEqual(['combined', 'llm']);
    expect(classAssigningLlmPath({})).toEqual(['combined', 'llm']);
    expect(classAssigningLlmPath(undefined)).toEqual(['combined', 'llm']);
  });

  it('is section classification for split_then_classify', () => {
    expect(classAssigningLlmPath({ strategy: 'split_then_classify' })).toEqual(['split_then_classify', 'section_classification', 'llm']);
  });
});

describe('classificationModelOf', () => {
  const cls = {
    combined: { llm: { model: 'combined-model' } },
    split_then_classify: {
      splitting: { llm: { model: 'splitting-model' } },
      section_classification: { llm: { model: 'section-model' } },
    },
  };

  it('reads the active strategy, never the splitting model', () => {
    expect(classificationModelOf({ ...cls, strategy: 'combined' })).toBe('combined-model');
    expect(classificationModelOf({ ...cls, strategy: 'split_then_classify' })).toBe('section-model');
  });

  it('is undefined for an absent or empty model', () => {
    expect(classificationModelOf({})).toBeUndefined();
    expect(classificationModelOf({ combined: { llm: { model: '' } } })).toBeUndefined();
  });
});

describe('setClassificationModel', () => {
  it('writes the class-assigning call and leaves the splitting call alone', () => {
    const cls: Record<string, unknown> = {
      strategy: 'split_then_classify',
      split_then_classify: { splitting: { llm: { model: 'splitting-model' } } },
    };
    setClassificationModel(cls, 'arn:custom');
    expect(classificationModelOf(cls)).toBe('arn:custom');
    expect((cls.split_then_classify as { splitting: { llm: { model: string } } }).splitting.llm.model).toBe('splitting-model');
    expect(cls).not.toHaveProperty('model');
  });

  it('creates the path in an empty block', () => {
    const cls: Record<string, unknown> = {};
    setClassificationModel(cls, 'arn:custom');
    expect(cls).toEqual({ combined: { llm: { model: 'arn:custom' } } });
  });
});

describe('flatClassificationKeysIn', () => {
  it('names the pre-0.9 keys a file still carries', () => {
    expect(flatClassificationKeysIn({ classification: { model: 'm', granularity: 'page', strategy: 'combined' } })).toEqual([
      'model',
      'granularity',
    ]);
  });

  it('is empty for a nested file and for a file with no classification', () => {
    expect(flatClassificationKeysIn({ classification: { combined: { granularity: 'page' } } })).toEqual([]);
    expect(flatClassificationKeysIn({ extraction: {} })).toEqual([]);
    expect(flatClassificationKeysIn(null)).toEqual([]);
  });
});

describe('pageLimitBecomesOneDocument', () => {
  it('is true for a page limit with boundary detection', () => {
    expect(pageLimitBecomesOneDocument({ classification: { maxPagesForClassification: '3' } })).toBe(true);
    expect(pageLimitBecomesOneDocument({ classification: { maxPagesForClassification: 2, sectionSplitting: 'page' } })).toBe(true);
  });

  it('is true for a blank section splitting, which means take the default', () => {
    // Unset is not unrecognised. The conversion still runs for a blank value, so
    // the dialog must still announce it -- `_known_section_splitting` in
    // `v08_to_v09.py` makes the same distinction, and collapsing the two there
    // dropped the operator's page limit silently.
    for (const blank of ['', '   ', null, undefined]) {
      expect(pageLimitBecomesOneDocument({ classification: { maxPagesForClassification: '3', sectionSplitting: blank } })).toBe(true);
    }
  });

  it('is false without a limit or when the file is already one document', () => {
    expect(pageLimitBecomesOneDocument({ classification: { maxPagesForClassification: 'ALL' } })).toBe(false);
    expect(pageLimitBecomesOneDocument({ classification: { maxPagesForClassification: '3', sectionSplitting: 'disabled' } })).toBe(false);
    expect(pageLimitBecomesOneDocument({ classification: { model: 'm' } })).toBe(false);
  });

  it('is false for an unrecognised value, which is refused rather than converted', () => {
    // Promising a conversion for a value the backend will reject describes
    // something that will not happen; `unknownSectionSplittingIn` reports it.
    expect(pageLimitBecomesOneDocument({ classification: { maxPagesForClassification: '3', sectionSplitting: 'bogus' } })).toBe(false);
  });

  it('is false for a boolean page limit, which is not a page count', () => {
    expect(pageLimitBecomesOneDocument({ classification: { maxPagesForClassification: true } })).toBe(false);
  });
});

describe('isPageLimit, through pageLimitBecomesOneDocument', () => {
  // The TypeScript half of the shared table. Its Python half is
  // `test_the_page_limit_predicate_answers_the_shared_table` in
  // `scripts/tests/test_classification_flat_keys_ui_parity.py`; the fixture's own
  // `$comment` says why the cases are authored once rather than in two suites.
  const fixtures = pageLimitFixtures as { cases: { json: unknown; limit: boolean; why: string }[] };

  it('reads a table worth exercising', () => {
    expect(fixtures.cases.length).toBeGreaterThanOrEqual(10);
  });

  it.each(fixtures.cases)('$why ($json)', ({ json, limit }) => {
    // `llm_determined` is the shipped default, so a real limit means the
    // conversion runs -- which makes this predicate the observable half of
    // `isPageLimit`.
    expect(pageLimitBecomesOneDocument({ classification: { maxPagesForClassification: json } })).toBe(limit);
  });
});

describe('unknownSectionSplittingIn', () => {
  it('names a value the backend will refuse', () => {
    expect(unknownSectionSplittingIn({ classification: { sectionSplitting: 'bogus' } })).toBe('bogus');
    expect(unknownSectionSplittingIn({ classification: { document_shape: { sectionSplitting: 'Bogus' } } })).toBe('Bogus');
  });

  it('renders a malformed nesting readably rather than as [object Object]', () => {
    expect(unknownSectionSplittingIn({ classification: { sectionSplitting: { mode: 'page' } } })).toBe('{"mode":"page"}');
  });

  it('prefers the flat key, which is the one the migration keeps', () => {
    // `v08_to_v09.py`: in a hybrid the flat key wins, and `_put` overwrites
    // `document_shape.sectionSplitting` with it. Warning about the nested value would
    // name one the migration discards.
    expect(unknownSectionSplittingIn({ classification: { sectionSplitting: 'pages', document_shape: { sectionSplitting: 'page' } } })).toBe(
      'pages',
    );
    expect(
      unknownSectionSplittingIn({ classification: { sectionSplitting: 'page', document_shape: { sectionSplitting: 'pages' } } }),
    ).toBeNull();
  });

  it('is null for every value the backend accepts, and for an unset one', () => {
    for (const value of [...SECTION_SPLITTING_VALUES, 'DISABLED', ' page ', '', '   ', null, undefined]) {
      expect(unknownSectionSplittingIn({ classification: { sectionSplitting: value } })).toBeNull();
    }
    expect(unknownSectionSplittingIn({ classification: { model: 'm' } })).toBeNull();
    expect(unknownSectionSplittingIn({})).toBeNull();
  });
});
