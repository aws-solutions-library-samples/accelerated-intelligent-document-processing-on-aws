// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * A queue page's Queue Progress panel, shared by Production and Test Studio queues.
 * What has to hold: it is titled Queue Progress; the bar has nearly square corners and
 * takes about half the panel's width; it says what it counts ("<n> of <total>
 * documents <label>"); and an empty queue reads 0%, not NaN.
 */

import React from 'react';
import { render } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { describe, expect, it } from 'vitest';

import QueueProgress, { progressPercent } from '../QueueProgress';

describe('Queue Progress', () => {
  it('is titled Queue Progress, and says what it counts', () => {
    render(<QueueProgress count={12} total={40} countLabel="assigned or in progress" description="Work underway" />);
    const container = createWrapper(document.body).findContainer()!;
    expect(container.findHeader()!.getElement().textContent).toBe('Queue Progress');
    const bar = createWrapper(document.body).findProgressBar()!;
    expect(bar.findAdditionalInfo()!.getElement().textContent).toBe('12 of 40 documents assigned or in progress');
    expect(bar.findPercentageText()!.getElement().textContent).toBe('30%');
  });

  it('takes about half the panel, with a bar that is nearly square at the corners', () => {
    render(<QueueProgress count={1} total={2} countLabel="completed" description="d" />);
    const bar = createWrapper(document.body).findProgressBar()!.getElement();
    const wrapper = bar.parentElement as HTMLElement;
    expect(wrapper.style.width).toBe('50%');
    const track = bar.querySelector('progress') as HTMLElement | null;
    // Cloudscape applies the style API to the bar as CSS custom properties.
    const styled = (track ?? bar).getAttribute('style') ?? '';
    expect(styled).toContain('2px');
    expect(styled).not.toContain('8px');
  });

  it('reads 0% for an empty queue', () => {
    expect(progressPercent(0, 0)).toBe(0);
    expect(progressPercent(1, 3)).toBe(33);
  });
});
