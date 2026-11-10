// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { GuidedTourProvider, buildTutorial } from '../guidedTour';

const { annotation } = vi.hoisted(() => ({ annotation: { onFinish: undefined as undefined | (() => void) } }));
vi.mock('@cloudscape-design/components/annotation-context', () => ({
  default: ({ onFinish, children }: { onFinish: () => void; children: React.ReactNode }) => {
    annotation.onFinish = onFinish;
    return <>{children}</>;
  },
}));

const hotspots = (quickStart: boolean) =>
  buildTutorial({ customModels: false, capacityPlanning: true, quickStart }).tasks.flatMap((task) =>
    task.steps.map((step) => step.hotspotId),
  );

const finishTourCountingQuickStartOpens = () => {
  const opened = vi.fn();
  window.addEventListener('openQuickStart', opened);
  render(
    <MemoryRouter>
      <GuidedTourProvider>
        <div />
      </GuidedTourProvider>
    </MemoryRouter>,
  );
  annotation.onFinish?.();
  window.removeEventListener('openQuickStart', opened);
  return opened.mock.calls.length;
};

describe('the guided tour and Quick Start', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it('ends at the Quick Start button when Quick Start is on', () => {
    const tutorial = buildTutorial({ customModels: false, capacityPlanning: true, quickStart: true });
    expect(hotspots(true).at(-1)).toBe('nav-quick-start');
    expect(tutorial.completedScreenDescription).toContain('Quick Start');
  });

  it('never points at Quick Start when the stack turns it off', () => {
    const tutorial = buildTutorial({ customModels: false, capacityPlanning: true, quickStart: false });
    expect(hotspots(false)).not.toContain('nav-quick-start');
    expect(hotspots(false)).toEqual(hotspots(true).slice(0, -1));
    expect(tutorial.completedScreenDescription).not.toContain('Quick Start');
  });

  it('opens Quick Start on Finish only when Quick Start is on', () => {
    vi.stubEnv('VITE_ENABLE_QUICK_START_WIDGET', 'true');
    expect(finishTourCountingQuickStartOpens()).toBe(1);
    vi.stubEnv('VITE_ENABLE_QUICK_START_WIDGET', 'false');
    expect(finishTourCountingQuickStartOpens()).toBe(0);
  });
});
