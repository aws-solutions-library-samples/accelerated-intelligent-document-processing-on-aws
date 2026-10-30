// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Processing Report's Processing Path flow. What has to hold: the stages and the
 * arrows between them are one row that never wraps (a wrapped flow strands an arrow at
 * a line end and reads as two flows), centred on one line, with every stage box the
 * same height whether or not it has a detail line; and a flow wider than its panel
 * scrolls sideways.
 */

import React from 'react';
import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import ProcessingReportTab from '../ProcessingReportTab';

const STAGES = [
  { key: 'ocr', label: 'OCR', status: 'ok' },
  { key: 'classify', label: 'Classify', status: 'ok', detail: 'packet' },
  { key: 'extract', label: 'Extract', status: 'ok', fanout: 3, detail: '3 shards' },
  { key: 'confidence', label: 'Confidence', status: 'warning' },
];

const renderFlow = () => {
  render(<ProcessingReportTab metadata={{ processing_flow: { stages: STAGES } }} processingReport={undefined} inferenceResult={{}} />);
  const flow = document.querySelector('[data-testid="process-flow"]') as HTMLElement;
  expect(flow).not.toBeNull();
  return flow;
};

describe('the Processing Path flow', () => {
  it('is one row that never wraps, centred on one line, and scrolls sideways', () => {
    const flow = renderFlow();
    expect(flow.style.display).toBe('flex');
    expect(flow.style.flexWrap).toBe('nowrap');
    expect(flow.style.alignItems).toBe('center');
    expect(flow.style.overflowX).toBe('auto');
  });

  it('puts an arrow between each pair of stages, in order', () => {
    const items = [...renderFlow().children].map((c) => c.textContent?.trim());
    expect(items).toEqual(['OCR', '→', 'Classifypacket', '→', 'Extract ×33 shards', '→', '⚠ Confidence']);
  });

  it('gives every stage box the same height, with or without a detail line', () => {
    const flow = renderFlow();
    const boxes = [...flow.querySelectorAll<HTMLElement>('div')].filter((d) => d.style.minHeight);
    expect(boxes).toHaveLength(STAGES.length);
    expect(new Set(boxes.map((b) => b.style.minHeight)).size).toBe(1);
    boxes.forEach((b) => expect(b.style.justifyContent).toBe('center'));
  });
});
