// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { describe, expect, it } from 'vitest';
import { classAssigningLlmPath, classificationModelOf, setClassificationModel } from '../classification-config';

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
