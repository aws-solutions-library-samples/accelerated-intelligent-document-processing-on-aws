// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The guided tour over the real side navigation, per role.
 *
 * An Author's navigation has no Knowledge Base, Agent Chat or User Management link,
 * and the tour used to stop on the step before the first of them. Rendered inside
 * the tour provider, the navigation's own hotspots decide the steps: an Author's
 * tour skips those three, and an Admin's keeps them.
 */

import React from 'react';
import { act, render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { IntlProvider } from 'react-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AnnotationContextProps } from '@cloudscape-design/components/annotation-context';

import { GuidedTourProvider, useGuidedTour } from '../guidedTour';
import Navigation from '../../components/genaiidp-layout/navigation';

const state = vi.hoisted(() => ({ role: {} as Record<string, unknown> }));

vi.mock('../../hooks/use-user-role', () => ({ default: () => state.role }));
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

const tourFor = (role: Record<string, unknown>): string[] => {
  state.role = role;
  render(
    <IntlProvider locale="en" defaultLocale="en">
      <MemoryRouter>
        <GuidedTourProvider>
          <Navigation />
          <Probe />
        </GuidedTourProvider>
      </MemoryRouter>
    </IntlProvider>,
  );
  act(() => {
    window.dispatchEvent(new CustomEvent('startTutorial', { detail: { customModels: false, capacityPlanning: true } }));
    vi.advanceTimersByTime(350);
  });
  return (shown.tutorial?.tasks ?? []).flatMap((task) => task.steps.map((step) => step.hotspotId));
};

describe('the guided tour over the side navigation', () => {
  beforeEach(() => {
    vi.stubEnv('VITE_ENABLE_QUICK_START_WIDGET', 'false');
    vi.useFakeTimers();
    shown.tutorial = undefined;
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
});
