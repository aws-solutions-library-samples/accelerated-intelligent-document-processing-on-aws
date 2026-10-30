// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * A page can say which side-navigation entry to highlight when its URL does not (the
 * edit page, from its mode). What has to hold, in the full navigation and the
 * Annotator-only one: a given entry is highlighted; `null` highlights nothing; and
 * omitted, the entry still comes from the URL.
 */

import React from 'react';
import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import type { SideNavigationProps } from '@cloudscape-design/components';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({ role: {} as Record<string, unknown>, nav: null as SideNavigationProps | null }));

vi.mock('../../../hooks/use-user-role', () => ({ default: () => state.role }));
vi.mock('../../../hooks/use-installed-features', () => ({ default: () => ({ features: [] }) }));
vi.mock('../../../hooks/use-catalog-features', () => ({ default: () => ({ features: [] }) }));
vi.mock('../../../hooks/use-latest-version', () => ({
  default: () => ({ latestVersion: null, templateUrl: null, isUpdateAvailable: false }),
}));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../../../hooks/use-inbox', () => ({ default: () => ({ unreadCount: null }) }));
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

const ROLES: Array<[string, Record<string, unknown>, string]> = [
  ['the full navigation', { ...NO_ROLE, isAdmin: true }, '#/documents'],
  ['the Annotator-only navigation', { ...NO_ROLE, isAnnotator: true, isAnnotatorOnly: true }, '#/my-work/tasks'],
];

const activeAt = (path: string, activeHref?: string | null) => {
  render(
    <MemoryRouter initialEntries={[path]}>
      <Navigation activeHref={activeHref} />
    </MemoryRouter>,
  );
  return state.nav!.activeHref;
};

beforeEach(() => {
  state.nav = null;
  document.body.innerHTML = '';
});

describe.each(ROLES)('%s', (_name, role, fromUrl) => {
  beforeEach(() => {
    state.role = role;
  });

  it('highlights the entry the page gives', () => {
    expect(activeAt('/documents/a%2Fb.pdf/edit', '#/inbox')).toBe('#/inbox');
  });

  it('highlights nothing for null', () => {
    expect(activeAt('/documents/a%2Fb.pdf/edit', null)).toBeUndefined();
  });

  it('derives the entry from the URL when the page gives none', () => {
    expect(activeAt('/documents/a%2Fb.pdf/edit')).toBe(fromUrl);
  });
});
