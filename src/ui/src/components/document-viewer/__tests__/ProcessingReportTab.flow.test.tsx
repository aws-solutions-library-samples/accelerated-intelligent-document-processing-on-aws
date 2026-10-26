// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * A single-pass stage in the Processing Path shows its label and nothing else.
 *
 * The backend records `fanout: 0` for a one-pass extraction or confidence call, which
 * is the default Simple-extraction shape. The stage box computed `fanout && fanout > 1`,
 * which is the number 0 there, and `{0 && …}` renders it: a stray "0" above Extract
 * and Confidence in every ordinary report.
 */

import { render, screen } from '@testing-library/react';
import React from 'react';
import { describe, expect, it } from 'vitest';

import ProcessingReportTab from '../ProcessingReportTab';

const renderFlow = () =>
  render(
    <ProcessingReportTab
      metadata={{
        extraction_method: 'traditional',
        processing_flow: {
          stages: [
            { key: 'ocr', label: 'OCR', detail: 'no markdown tables' },
            { key: 'extract', label: 'Extract', detail: 'single LLM pass', fanout: 0 },
            { key: 'confidence', label: 'Confidence', detail: '1 pass', fanout: 0 },
            { key: 'shards', label: 'Sharded', detail: 'three shards', fanout: 3 },
          ],
        },
      }}
    />,
  );

// The stage box is the label's grandparent: label div → card → positioned wrapper.
const stageText = (label: string): string | null | undefined => screen.getByText(label).parentElement?.parentElement?.textContent;

describe('Processing Path stages', () => {
  it.each([
    ['Extract', 'Extractsingle LLM pass'],
    ['Confidence', 'Confidence1 pass'],
  ])('renders no stray count on a single-pass %s stage', (label, expected) => {
    renderFlow();

    expect(stageText(label)).toBe(expected);
  });

  it('still marks a fanned-out stage with its count', () => {
    renderFlow();

    expect(stageText('Sharded ×3')).toBe('Sharded ×3three shards');
  });
});
