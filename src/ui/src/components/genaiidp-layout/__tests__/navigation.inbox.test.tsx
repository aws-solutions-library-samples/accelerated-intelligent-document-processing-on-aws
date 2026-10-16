// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Inbox entry is injected once, ahead of every role's own items, so these
 * tests check it for each role rather than trusting that one role's list is
 * representative.
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
  isAdmin: false,
  isAuthor: false,
  isReviewerOnly: false,
  isAnnotatorOnly: false,
  isViewerOnly: false,
  allowedTestSets: null,
};

const ROLES: Array<[string, Record<string, unknown>]> = [
  ['Admin', { ...NO_ROLE, isAdmin: true }],
  ['Author', { ...NO_ROLE, isAuthor: true }],
  ['Reviewer', { ...NO_ROLE, isReviewerOnly: true }],
  ['Annotator', { ...NO_ROLE, isAnnotatorOnly: true, allowedTestSets: ['set-1'] }],
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

const firstLink = (nav: SideNavigationProps) => (nav.items ?? [])[0] as SideNavigationProps.Link;

const infoText = (link: SideNavigationProps.Link): string => {
  if (!link.info) return '';
  const { container } = render(<>{link.info}</>);
  return container.textContent ?? '';
};

describe.each(ROLES)('Inbox navigation entry for %s', (_name, role) => {
  beforeEach(() => {
    state.role = role;
    state.unreadCount = 0;
    state.nav = null;
  });

  it('is the first item and links to the Inbox', () => {
    const link = firstLink(renderNav());
    expect(link.type).toBe('link');
    expect(link.text).toBe('Inbox');
    expect(link.href).toBe('#/inbox');
  });

  it('shows no badge with nothing unread', () => {
    expect(firstLink(renderNav()).info).toBeUndefined();
  });

  it('announces the unread count', () => {
    state.unreadCount = 3;
    expect(infoText(firstLink(renderNav()))).toContain('3 unread');
  });

  it('caps the badge at 99+', () => {
    state.unreadCount = 150;
    expect(infoText(firstLink(renderNav()))).toContain('99+');
  });

  it('is the active link on the Inbox page', () => {
    expect(renderNav('/inbox').activeHref).toBe('#/inbox');
  });

  it('appears exactly once', () => {
    const inboxLinks = (renderNav().items ?? []).filter((i) => (i as SideNavigationProps.Link).href === '#/inbox');
    expect(inboxLinks).toHaveLength(1);
  });
});
