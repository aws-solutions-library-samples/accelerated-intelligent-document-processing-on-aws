// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The guided tour over the real side navigation, per role.
 *
 * An Author's navigation has no Knowledge Base, Agent Chat or User Management link,
 * and the tour used to stop on the step before the first of them. Rendered inside
 * the tour provider, the navigation's own hotspots decide the steps: an Author's
 * tour skips those three, and an Admin's keeps them. The navigation reads the role
 * once to choose itself and again inside the full navigation, so the tour must not
 * read its links while that second read is still loading.
 */

import React from 'react';
import { act, render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { IntlProvider } from 'react-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AnnotationContextProps } from '@cloudscape-design/components/annotation-context';

import { GuidedTourProvider, useGuidedTour } from '../guidedTour';
import Navigation from '../../components/genaiidp-layout/navigation';

const state = vi.hoisted(() => ({ role: {} as Record<string, unknown>, innerLoading: false, calls: 0 }));

vi.mock('../../hooks/use-user-role', () => ({
  default: () => {
    state.calls += 1;
    if (state.innerLoading && state.calls > 1) {
      return { ...state.role, loading: true, isAdmin: false, isAuthor: false, isViewerOnly: false };
    }
    return state.role;
  },
}));
vi.mock('../../hooks/use-installed-features', () => ({ default: () => ({ features: [] }) }));
vi.mock('../../hooks/use-catalog-features', () => ({ default: () => ({ features: [] }) }));
vi.mock('../../hooks/use-latest-version', () => ({
  default: () => ({ latestVersion: null, templateUrl: null, isUpdateAvailable: false }),
}));
vi.mock('../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../../hooks/use-inbox', () => ({ default: () => ({ unreadCount: null }) }));
vi.mock('@cloudscape-design/components/annotation-context', () => ({
  default: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock('@cloudscape-design/components/hotspot', () => ({
  default: ({ children }: { children?: React.ReactNode }) => <>{children}</>,
}));

const NO_ROLE = {
  loading: false,
  isAdmin: false,
  isAuthor: false,
  isViewerOnly: false,
  isReviewerOnly: false,
  isAnnotator: false,
  isAnnotatorOnly: false,
  isQueueWorker: false,
  canReview: false,
  allowedTestSets: null,
};

const SKIPPED_FOR_AN_AUTHOR = ['nav-document-kb', 'nav-agent-chat', 'nav-user-management'];

const shown: { tutorial?: AnnotationContextProps.Tutorial | null } = {};
const Probe = (): null => {
  shown.tutorial = useGuidedTour().tutorial;
  return null;
};

const Tree = (): React.JSX.Element => (
  <IntlProvider locale="en" defaultLocale="en">
    <MemoryRouter>
      <GuidedTourProvider>
        <Navigation />
        <Probe />
      </GuidedTourProvider>
    </MemoryRouter>
  </IntlProvider>
);

const startTour = (): void => {
  act(() => {
    window.dispatchEvent(new CustomEvent('startTutorial', { detail: { customModels: false, capacityPlanning: true } }));
    vi.advanceTimersByTime(350);
  });
};

const shownSteps = (): string[] => (shown.tutorial?.tasks ?? []).flatMap((task) => task.steps.map((step) => step.hotspotId));

const tourFor = (role: Record<string, unknown>): string[] => {
  state.role = role;
  render(<Tree />);
  startTour();
  return shownSteps();
};

describe('the guided tour over the side navigation', () => {
  beforeEach(() => {
    vi.stubEnv('VITE_ENABLE_QUICK_START_WIDGET', 'false');
    vi.useFakeTimers();
    shown.tutorial = undefined;
    state.innerLoading = false;
    state.calls = 0;
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllEnvs();
  });

  it("skips the pages an Author's navigation does not have", () => {
    const steps = tourFor({ ...NO_ROLE, isAuthor: true });
    expect(steps).toContain('nav-documents');
    expect(steps).toContain('nav-upload');
    SKIPPED_FOR_AN_AUTHOR.forEach((hotspot) => expect(steps).not.toContain(hotspot));
  });

  it('keeps those pages for an Admin, whose navigation has them', () => {
    const steps = tourFor({ ...NO_ROLE, isAdmin: true, isAuthor: true });
    SKIPPED_FOR_AN_AUTHOR.forEach((hotspot) => expect(steps).toContain(hotspot));
  });

  it("waits for the full navigation to know the role, rather than touring the Viewer's links", () => {
    state.role = { ...NO_ROLE, isAdmin: true, isAuthor: true };
    state.innerLoading = true;
    const { rerender } = render(<Tree />);
    startTour();
    expect(shown.tutorial).toBeNull();

    state.innerLoading = false;
    rerender(<Tree />);
    SKIPPED_FOR_AN_AUTHOR.forEach((hotspot) => expect(shownSteps()).toContain(hotspot));
  });
});
