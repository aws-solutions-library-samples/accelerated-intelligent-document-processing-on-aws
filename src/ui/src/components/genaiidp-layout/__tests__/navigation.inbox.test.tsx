// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Every user has an Inbox: it is the first entry of the Home section, which leads the
 * navigation for every role. Home adds Tasks and Queues only for the Annotator group
 * (Annotator-only, or Annotator alongside another role), who are assigned HITL work
 * (docs/web-ui.md, "Inbox"; docs/rbac.md). These tests check each role rather than
 * trusting that one role's list is representative.
 */

import React from 'react';
import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import type { SideNavigationProps } from '@cloudscape-design/components';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({
  role: {} as Record<string, unknown>,
  unreadCount: null as number | null,
  nav: null as SideNavigationProps | null,
}));

vi.mock('../../../hooks/use-user-role', () => ({ default: () => state.role }));
vi.mock('../../../hooks/use-installed-features', () => ({ default: () => ({ features: [] }) }));
vi.mock('../../../hooks/use-catalog-features', () => ({ default: () => ({ features: [] }) }));
vi.mock('../../../hooks/use-latest-version', () => ({
  default: () => ({ latestVersion: null, templateUrl: null, isUpdateAvailable: false }),
}));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../../../hooks/use-inbox', () => ({ default: () => ({ unreadCount: state.unreadCount }) }));
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
  isReviewerOnly: false,
  isAnnotator: false,
  isAnnotatorOnly: false,
  isViewerOnly: false,
  isQueueWorker: false,
  allowedTestSets: null,
};

/** Roles that work Production review queues (Annotator or Reviewer): Home has their Tasks and Queues. */
const ANNOTATOR_ROLES: Array<[string, Record<string, unknown>]> = [
  ['Annotator', { ...NO_ROLE, isAnnotator: true, isAnnotatorOnly: true, isQueueWorker: true, allowedTestSets: ['set-1'] }],
  ['Admin + Annotator', { ...NO_ROLE, isAdmin: true, isAnnotator: true, isQueueWorker: true }],
  ['Viewer + Annotator', { ...NO_ROLE, isViewerOnly: true, isAnnotator: true, isQueueWorker: true }],
  ['Reviewer', { ...NO_ROLE, isReviewerOnly: true, isQueueWorker: true }],
];

/** Roles that work no review queue as a member: Home has the Inbox only. */
const OTHER_ROLES: Array<[string, Record<string, unknown>]> = [
  ['Admin', { ...NO_ROLE, isAdmin: true }],
  ['Author', { ...NO_ROLE, isAuthor: true }],
  ['Viewer', { ...NO_ROLE, isViewerOnly: true }],
];

const renderNav = (path = '/documents') => {
  render(
    <MemoryRouter initialEntries={[path]}>
      <Navigation />
    </MemoryRouter>,
  );
  if (!state.nav) throw new Error('SideNavigation was not rendered');
  return state.nav;
};

/** Every link in the navigation, sections and expandable groups flattened. */
const allLinks = (items: ReadonlyArray<SideNavigationProps.Item>): SideNavigationProps.Link[] =>
  items.flatMap((i) => {
    if (i.type === 'link') return [i as SideNavigationProps.Link];
    const nested = (i as { items?: ReadonlyArray<SideNavigationProps.Item> }).items;
    return nested ? allLinks(nested) : [];
  });

const inboxLinks = (nav: SideNavigationProps) => allLinks(nav.items ?? []).filter((l) => l.href === '#/inbox');

/** The first item, which must be the Home section. */
const homeSection = (nav: SideNavigationProps) => {
  const first = (nav.items ?? [])[0] as SideNavigationProps.Section;
  expect(first.type).toBe('section');
  expect(first.text).toBe('Home');
  return first;
};
const homeLinks = (nav: SideNavigationProps) => homeSection(nav).items.map((l) => (l as SideNavigationProps.Link).text);
/** The first link of the Home section. */
const firstHomeLink = (nav: SideNavigationProps) => homeSection(nav).items[0] as SideNavigationProps.Link;

const infoText = (link: SideNavigationProps.Link): string => {
  if (!link.info) return '';
  const { container } = render(<>{link.info}</>);
  return container.textContent ?? '';
};

describe.each([...ANNOTATOR_ROLES, ...OTHER_ROLES])('Inbox navigation entry for %s', (_name, role) => {
  beforeEach(() => {
    state.role = role;
    state.unreadCount = 0;
    state.nav = null;
  });

  it('is the first entry of Home, which comes first, and links to the Inbox', () => {
    const link = firstHomeLink(renderNav());
    expect(link.type).toBe('link');
    expect(link.text).toBe('Inbox');
    expect(link.href).toBe('#/inbox');
  });

  it('shows no badge with nothing unread', () => {
    expect(firstHomeLink(renderNav()).info).toBeUndefined();
  });

  it('announces the unread count', () => {
    state.unreadCount = 3;
    expect(infoText(firstHomeLink(renderNav()))).toContain('3 unread');
  });

  it('caps the badge at 99+', () => {
    state.unreadCount = 150;
    expect(infoText(firstHomeLink(renderNav()))).toContain('99+');
  });

  it('is the active link on the Inbox page', () => {
    expect(renderNav('/inbox').activeHref).toBe('#/inbox');
  });

  it('appears exactly once', () => {
    expect(inboxLinks(renderNav())).toHaveLength(1);
  });

  it('has no My Work section', () => {
    const sections = (renderNav().items ?? []).filter((i) => i.type === 'section').map((i) => (i as SideNavigationProps.Section).text);
    expect(sections).not.toContain('My Work');
  });
});

describe.each(ANNOTATOR_ROLES)('Home for %s', (_name, role) => {
  beforeEach(() => {
    state.role = role;
    state.unreadCount = 0;
    state.nav = null;
  });

  it('lists Inbox, Tasks and Queues', () => {
    expect(homeLinks(renderNav())).toEqual(['Inbox', 'Tasks', 'Queues']);
  });
});

describe.each(OTHER_ROLES)('Home for %s', (_name, role) => {
  beforeEach(() => {
    state.role = role;
    state.unreadCount = 0;
    state.nav = null;
  });

  it("lists the Inbox only: Tasks and Queues are an Annotator's", () => {
    expect(homeLinks(renderNav())).toEqual(['Inbox']);
  });
});
