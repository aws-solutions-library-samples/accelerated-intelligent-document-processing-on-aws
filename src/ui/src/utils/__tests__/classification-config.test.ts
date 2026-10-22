// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { describe, expect, it } from 'vitest';
import {
  classAssigningLlmPath,
  classificationModelOf,
  flatClassificationKeysIn,
  pageLimitBecomesOneDocument,
  setClassificationModel,
} from '../classification-config';

describe('classAssigningLlmPath', () => {
  it('is the one-stage call for single_pass, and by default', () => {
    expect(classAssigningLlmPath({ strategy: 'single_pass' })).toEqual(['one_stage', 'llm']);
    expect(classAssigningLlmPath({})).toEqual(['one_stage', 'llm']);
    expect(classAssigningLlmPath(undefined)).toEqual(['one_stage', 'llm']);
  });

  it('is section classification for two_stage', () => {
    expect(classAssigningLlmPath({ strategy: 'two_stage' })).toEqual(['two_stage', 'section_classification', 'llm']);
  });
});

describe('classificationModelOf', () => {
  const cls = {
    one_stage: { llm: { model: 'one-stage-model' } },
    two_stage: {
      segmentation: { llm: { model: 'boundary-model' } },
      section_classification: { llm: { model: 'section-model' } },
    },
  };

  it('reads the active strategy, never the boundary model', () => {
    expect(classificationModelOf({ ...cls, strategy: 'single_pass' })).toBe('one-stage-model');
    expect(classificationModelOf({ ...cls, strategy: 'two_stage' })).toBe('section-model');
  });

  it('is undefined for an absent or empty model', () => {
    expect(classificationModelOf({})).toBeUndefined();
    expect(classificationModelOf({ one_stage: { llm: { model: '' } } })).toBeUndefined();
  });
});

describe('setClassificationModel', () => {
  it('writes the class-assigning call and leaves segmentation alone', () => {
    const cls: Record<string, unknown> = {
      strategy: 'two_stage',
      two_stage: { segmentation: { llm: { model: 'boundary-model' } } },
    };
    setClassificationModel(cls, 'arn:custom');
    expect(classificationModelOf(cls)).toBe('arn:custom');
    expect((cls.two_stage as { segmentation: { llm: { model: string } } }).segmentation.llm.model).toBe('boundary-model');
    expect(cls).not.toHaveProperty('model');
  });

  it('creates the path in an empty block', () => {
    const cls: Record<string, unknown> = {};
    setClassificationModel(cls, 'arn:custom');
    expect(cls).toEqual({ one_stage: { llm: { model: 'arn:custom' } } });
  });
});

describe('flatClassificationKeysIn', () => {
  it('names the pre-0.9 keys a file still carries', () => {
    expect(flatClassificationKeysIn({ classification: { model: 'm', granularity: 'page', strategy: 'single_pass' } })).toEqual([
      'model',
      'granularity',
    ]);
  });

  it('is empty for a nested file and for a file with no classification', () => {
    expect(flatClassificationKeysIn({ classification: { one_stage: { granularity: 'page' } } })).toEqual([]);
    expect(flatClassificationKeysIn({ extraction: {} })).toEqual([]);
    expect(flatClassificationKeysIn(null)).toEqual([]);
  });
});

describe('pageLimitBecomesOneDocument', () => {
  it('is true for a page limit with boundary detection', () => {
    expect(pageLimitBecomesOneDocument({ classification: { maxPagesForClassification: '3' } })).toBe(true);
    expect(pageLimitBecomesOneDocument({ classification: { maxPagesForClassification: 2, sectionSplitting: 'page' } })).toBe(true);
  });

  it('is false without a limit or when the file is already one document', () => {
    expect(pageLimitBecomesOneDocument({ classification: { maxPagesForClassification: 'ALL' } })).toBe(false);
    expect(pageLimitBecomesOneDocument({ classification: { maxPagesForClassification: '3', sectionSplitting: 'disabled' } })).toBe(false);
    expect(pageLimitBecomesOneDocument({ classification: { model: 'm' } })).toBe(false);
  });
});
