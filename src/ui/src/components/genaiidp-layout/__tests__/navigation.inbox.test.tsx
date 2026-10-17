// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Inbox belongs to Annotators: it is the first entry of the My Work section,
 * which leads the navigation for anyone in the Annotator group (Annotator-only, or
 * Annotator alongside another role). Every other role has no Inbox entry at all
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
  allowedTestSets: null,
};

/** Roles that have My Work, and so the Inbox. */
const ANNOTATOR_ROLES: Array<[string, Record<string, unknown>]> = [
  ['Annotator', { ...NO_ROLE, isAnnotator: true, isAnnotatorOnly: true, allowedTestSets: ['set-1'] }],
  ['Admin + Annotator', { ...NO_ROLE, isAdmin: true, isAnnotator: true }],
  ['Viewer + Annotator', { ...NO_ROLE, isViewerOnly: true, isAnnotator: true }],
];

/** Roles without the Annotator group, which have no Inbox. */
const OTHER_ROLES: Array<[string, Record<string, unknown>]> = [
  ['Admin', { ...NO_ROLE, isAdmin: true }],
  ['Author', { ...NO_ROLE, isAuthor: true }],
  ['Reviewer', { ...NO_ROLE, isReviewerOnly: true }],
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

/** The first link of the first item, which must be the My Work section. */
const firstMyWorkLink = (nav: SideNavigationProps) => {
  const first = (nav.items ?? [])[0] as SideNavigationProps.Section;
  expect(first.type).toBe('section');
  expect(first.text).toBe('My Work');
  return first.items[0] as SideNavigationProps.Link;
};

const infoText = (link: SideNavigationProps.Link): string => {
  if (!link.info) return '';
  const { container } = render(<>{link.info}</>);
  return container.textContent ?? '';
};

describe.each(ANNOTATOR_ROLES)('Inbox navigation entry for %s', (_name, role) => {
  beforeEach(() => {
    state.role = role;
    state.unreadCount = 0;
    state.nav = null;
  });

  it('is the first entry of My Work, which comes first, and links to the Inbox', () => {
    const link = firstMyWorkLink(renderNav());
    expect(link.type).toBe('link');
    expect(link.text).toBe('Inbox');
    expect(link.href).toBe('#/inbox');
  });

  it('shows no badge with nothing unread', () => {
    expect(firstMyWorkLink(renderNav()).info).toBeUndefined();
  });

  it('announces the unread count', () => {
    state.unreadCount = 3;
    expect(infoText(firstMyWorkLink(renderNav()))).toContain('3 unread');
  });

  it('caps the badge at 99+', () => {
    state.unreadCount = 150;
    expect(infoText(firstMyWorkLink(renderNav()))).toContain('99+');
  });

  it('is the active link on the Inbox page', () => {
    expect(renderNav('/inbox').activeHref).toBe('#/inbox');
  });

  it('appears exactly once', () => {
    expect(inboxLinks(renderNav())).toHaveLength(1);
  });
});

describe.each(OTHER_ROLES)('No Inbox navigation entry for %s', (_name, role) => {
  beforeEach(() => {
    state.role = role;
    state.unreadCount = 3;
    state.nav = null;
  });

  it('has no Inbox link anywhere in the navigation', () => {
    expect(inboxLinks(renderNav())).toHaveLength(0);
  });

  it('has no My Work section', () => {
    const sections = (renderNav().items ?? []).filter((i) => i.type === 'section').map((i) => (i as SideNavigationProps.Section).text);
    expect(sections).not.toContain('My Work');
  });
});
