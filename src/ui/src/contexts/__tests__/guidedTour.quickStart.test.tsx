// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { act, render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { createIntl, IntlProvider } from 'react-intl';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { GuidedTourProvider, buildTutorial } from '../guidedTour';

const { annotation, role } = vi.hoisted(() => ({
  annotation: { onFinish: undefined as undefined | (() => void) },
  role: { isAdmin: false },
}));
vi.mock('../../hooks/use-user-role', () => ({ default: () => role }));
vi.mock('@cloudscape-design/components/annotation-context', () => ({
  default: ({ onFinish, children }: { onFinish: () => void; children: React.ReactNode }) => {
    annotation.onFinish = onFinish;
    return <>{children}</>;
  },
}));

const intl = createIntl({ locale: 'en', defaultLocale: 'en' });

const English = ({ children }: { children: React.ReactNode }) => (
  <IntlProvider locale="en" defaultLocale="en">
    {children}
  </IntlProvider>
);

const hotspots = (quickStart: boolean) =>
  buildTutorial(intl, { customModels: false, capacityPlanning: true, quickStart }).tasks.flatMap((task) =>
    task.steps.map((step) => step.hotspotId),
  );

const startThenFinishCountingQuickStartOpens = (detail?: Record<string, boolean>) => {
  const opened = vi.fn();
  window.addEventListener('openQuickStart', opened);
  render(
    <MemoryRouter>
      <GuidedTourProvider>
        <div />
      </GuidedTourProvider>
    </MemoryRouter>,
    { wrapper: English },
  );
  act(() => {
    window.dispatchEvent(new CustomEvent('startTutorial', { detail }));
  });
  act(() => annotation.onFinish?.());
  window.removeEventListener('openQuickStart', opened);
  return opened.mock.calls.length;
};

describe('the guided tour and Quick Start', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    role.isAdmin = false;
  });

  it('ends at the Quick Start button when Quick Start is on', () => {
    const tutorial = buildTutorial(intl, { customModels: false, capacityPlanning: true, quickStart: true });
    expect(hotspots(true).at(-1)).toBe('nav-quick-start');
    expect(tutorial.completedScreenDescription).toContain('Quick Start');
  });

  it('never points at Quick Start when the stack turns it off', () => {
    const tutorial = buildTutorial(intl, { customModels: false, capacityPlanning: true, quickStart: false });
    expect(hotspots(false)).not.toContain('nav-quick-start');
    expect(hotspots(false)).toEqual(hotspots(true).slice(0, -1));
    expect(tutorial.completedScreenDescription).not.toContain('Quick Start');
  });

  it('opens Quick Start on Finish only when the tour was started with Quick Start', () => {
    expect(startThenFinishCountingQuickStartOpens({ quickStart: true })).toBe(1);
    expect(startThenFinishCountingQuickStartOpens({ quickStart: false })).toBe(0);
    expect(startThenFinishCountingQuickStartOpens()).toBe(0);
  });

  it('takes Quick Start from whoever starts the tour, not from its own role check', () => {
    vi.stubEnv('VITE_ENABLE_QUICK_START_WIDGET', 'true');
    role.isAdmin = false;
    expect(startThenFinishCountingQuickStartOpens({ quickStart: true })).toBe(1);
    role.isAdmin = true;
    expect(startThenFinishCountingQuickStartOpens({ quickStart: false })).toBe(0);
  });
});
