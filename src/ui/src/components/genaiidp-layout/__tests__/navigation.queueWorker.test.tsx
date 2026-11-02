// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * A user whose only work is the review queues — Annotator-only, or a Reviewer with no
 * Admin, Author or Viewer role — gets Home and Deployment Info and nothing else: no
 * Documents and no Extensions. Their work is reached from Home › Tasks and Queues
 * (docs/review-queues.md). A Reviewer who also holds another role keeps that role's
 * navigation.
 */

import React from 'react';
import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import type { SideNavigationProps } from '@cloudscape-design/components';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({
  role: {} as Record<string, unknown>,
  nav: null as SideNavigationProps | null,
  update: false,
}));

vi.mock('../../../hooks/use-user-role', () => ({ default: () => state.role }));
vi.mock('../../../hooks/use-installed-features', () => ({ default: () => ({ features: [] }) }));
vi.mock('../../../hooks/use-catalog-features', () => ({ default: () => ({ features: [] }) }));
vi.mock('../../../hooks/use-latest-version', () => ({
  default: () =>
    state.update
      ? { latestVersion: '9.9.9', templateUrl: 'https://example.com/t.yaml', isUpdateAvailable: true }
      : { latestVersion: null, templateUrl: null, isUpdateAvailable: false },
}));
vi.mock('../../../contexts/settings', () => ({
  default: () => ({ settings: { StackName: 'idp-stack', Version: '1.2.3', IDPPattern: 'Unified pattern' } }),
}));
vi.mock('../../../hooks/use-inbox', () => ({ default: () => ({ unreadCount: 0 }) }));
vi.mock('@cloudscape-design/components', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@cloudscape-design/components')>()),
  SideNavigation: (props: SideNavigationProps) => {
    state.nav = props;
    return null;
  },
}));

import Navigation from '../navigation';

const NO_ROLE = {
  loading: false,
  isAdmin: false,
  isAuthor: false,
  isReviewer: false,
  isReviewerOnly: false,
  isAnnotator: false,
  isAnnotatorOnly: false,
  isViewerOnly: false,
  isQueueWorker: false,
  allowedTestSets: null,
};

const QUEUE_WORKERS: Array<[string, Record<string, unknown>]> = [
  ['Annotator', { ...NO_ROLE, isAnnotator: true, isAnnotatorOnly: true, isQueueWorker: true }],
  ['Reviewer', { ...NO_ROLE, isReviewer: true, isReviewerOnly: true, isQueueWorker: true }],
  ['Reviewer + Annotator', { ...NO_ROLE, isReviewer: true, isReviewerOnly: true, isAnnotator: true, isQueueWorker: true }],
];

const renderNav = () => {
  render(
    <MemoryRouter initialEntries={['/my-work']}>
      <Navigation />
    </MemoryRouter>,
  );
  if (!state.nav) throw new Error('SideNavigation was not rendered');
  return state.nav;
};

const sections = (nav: SideNavigationProps) =>
  (nav.items ?? []).filter((i) => i.type === 'section').map((i) => (i as SideNavigationProps.Section).text);

const section = (nav: SideNavigationProps, text: string) =>
  (nav.items ?? []).find((i) => i.type === 'section' && (i as SideNavigationProps.Section).text === text) as
    | SideNavigationProps.Section
    | undefined;

const linkTexts = (s: SideNavigationProps.Section | undefined) => (s?.items ?? []).map((l) => (l as SideNavigationProps.Link).text);

beforeEach(() => {
  state.nav = null;
  state.update = false;
});

describe.each(QUEUE_WORKERS)('navigation for %s', (_name, role) => {
  beforeEach(() => {
    state.role = role;
  });

  it('is Home then Deployment Info, and nothing else', () => {
    expect(sections(renderNav())).toEqual(['Home', 'Deployment Info']);
  });

  it('has no Documents or Extensions entry', () => {
    const nav = renderNav();
    expect(sections(nav)).not.toContain('Documents');
    expect(sections(nav)).not.toContain('Extensions');
    expect(JSON.stringify(nav.items)).not.toContain('#/documents');
  });

  it('keeps Tasks and Queues in Home', () => {
    expect(linkTexts(section(renderNav(), 'Home'))).toEqual(['Inbox', 'Tasks', 'Queues']);
  });

  it('shows the same deployment details as everyone else', () => {
    expect(linkTexts(section(renderNav(), 'Deployment Info'))).toEqual(['Stack Name: idp-stack', 'Version: 1.2.3', 'Pattern: Unified']);
  });

  it('shows the Update badge when a newer release is published', () => {
    state.update = true;
    const version = section(renderNav(), 'Deployment Info')?.items[1] as SideNavigationProps.Link;
    expect(version.info).toBeDefined();
  });
});

describe.each([
  ['Viewer + Reviewer', { ...NO_ROLE, isViewerOnly: true, isReviewer: true, isQueueWorker: true }],
  ['Author + Reviewer', { ...NO_ROLE, isAuthor: true, isReviewer: true, isQueueWorker: true }],
  ['Admin', { ...NO_ROLE, isAdmin: true }],
])('navigation for %s', (_name, role) => {
  beforeEach(() => {
    state.role = role;
  });

  it("keeps the role's full navigation, Documents and Extensions included", () => {
    const nav = renderNav();
    expect(sections(nav)).toContain('Documents');
    expect(sections(nav)).toContain('Extensions');
    expect(sections(nav).at(-1)).toBe('Deployment Info');
  });
});
