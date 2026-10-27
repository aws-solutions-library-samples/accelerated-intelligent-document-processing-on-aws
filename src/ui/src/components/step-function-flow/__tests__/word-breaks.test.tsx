// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Step names wrap between words, not inside them.
 *
 * Names are PascalCase with no spaces, so a narrow node broke them mid-word
 * ("ApplyPrepr / ocessingHo / okDocument"). The node now carries a <wbr> at each
 * word boundary.
 */

import { render } from '@testing-library/react';
import React from 'react';
import { describe, expect, it, vi } from 'vitest';

import FlowDiagram from '../FlowDiagram';
import { withWordBreaks } from '../word-breaks';

const html = (name: string): string => render(<span>{withWordBreaks(name)}</span>).container.firstElementChild?.innerHTML ?? '';

describe('withWordBreaks', () => {
  it.each([
    ['ApplyPreprocessingHookDocument', 'Apply<wbr>Preprocessing<wbr>Hook<wbr>Document'],
    ['OCRStep', 'OCR<wbr>Step'],
    ['PostOcrHook', 'Post<wbr>Ocr<wbr>Hook'],
    ['Extraction', 'Extraction'],
    ['Step2Extract', 'Step2<wbr>Extract'],
  ])('breaks %s at its word boundaries', (name, expected) => {
    expect(html(name)).toBe(expected);
  });

  it('leaves the visible text unchanged', () => {
    expect(render(<span>{withWordBreaks('RouteByProcessingMode')}</span>).container.textContent).toBe('RouteByProcessingMode');
  });
});

describe('FlowDiagram step node', () => {
  it('renders the step name with word breaks', () => {
    const { container } = render(
      <FlowDiagram
        steps={[{ name: 'ApplyPreprocessingHookDocument', type: 'Task', status: 'SUCCEEDED' }]}
        onStepClick={vi.fn()}
        getStepIcon={() => null}
      />,
    );

    expect(container.querySelectorAll('.step-name wbr')).toHaveLength(3);
  });
});
