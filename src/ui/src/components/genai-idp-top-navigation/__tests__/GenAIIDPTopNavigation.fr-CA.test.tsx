// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The top navigation's text comes from the catalog of the active locale: under `fr-CA` with a
 * small fixture catalog, the user label with its role, the feedback menu's accessible name and
 * the sign-out item read French; under `en` they read the inline English, as before.
 */
import React from 'react';
import { fireEvent, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';
import GenAIIDPTopNavigation from '../GenAIIDPTopNavigation';

vi.mock('virtual:i18n-catalogs', () => import('../../../test-utils/i18nCatalogsMock'));
vi.mock('../../../contexts/app', () => ({ default: () => ({ user: { username: 'alice' } }) }));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({
    isAdmin: true,
    isAuthor: false,
    isReviewer: false,
    isViewer: false,
    isAnnotator: false,
    isAnnotatorOnly: false,
    loading: false,
  }),
}));
vi.mock('../../../hooks/use-user-display-name', () => ({ default: () => ({ displayName: 'Alice', loading: false }) }));
vi.mock('../../../hooks/use-deployment-context', () => ({ default: () => ({}) }));
vi.mock('../../../hooks/use-inbox', () => ({
  default: () => ({ unreadCount: 0, recent: [], loaded: true, markMessages: vi.fn(), markAllRead: vi.fn() }),
}));

const FR = {
  'topNavigation.overflowMenuTriggerText': 'Plus',
  'topNavigation.user.signOut': 'Se déconnecter',
  'topNavigation.role.admin': 'Administrateur',
  'topNavigation.user.withRole': '{user} ({role})',
  'topNavigation.feedback.ariaLabel': 'Commentaires et signalement de problèmes',
};

/**
 * Open the user menu by its label. `hidden: true` because, unmeasured under jsdom, the top
 * navigation keeps both of its responsive variants aria-hidden; the roles are rendered all the same.
 */
const openUserMenu = (label: string): void => {
  fireEvent.click(screen.getAllByRole('button', { name: label, hidden: true })[0]);
};

describe('the top navigation under a locale', () => {
  afterEach(() => {
    window.localStorage.clear();
    document.documentElement.lang = '';
  });

  it('reads its text from the French catalog under fr-CA', async () => {
    renderWithIntl(<GenAIIDPTopNavigation />, { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR });
    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    expect((await screen.findAllByText('Alice (Administrateur)')).length).toBeGreaterThan(0);
    expect(screen.queryAllByText('Alice (Admin)')).toEqual([]);
    expect(screen.getAllByLabelText('Commentaires et signalement de problèmes').length).toBeGreaterThan(0);
    expect(screen.queryAllByLabelText('Feedback and issue reporting')).toEqual([]);
    openUserMenu('Alice (Administrateur)');
    expect((await screen.findAllByText('Se déconnecter')).length).toBeGreaterThan(0);
    expect(screen.queryAllByText('Sign out')).toEqual([]);
  });

  it('reads the inline English under en', async () => {
    renderWithIntl(<GenAIIDPTopNavigation />);
    expect(screen.getAllByText('Alice (Admin)').length).toBeGreaterThan(0);
    openUserMenu('Alice (Admin)');
    expect((await screen.findAllByText('Sign out')).length).toBeGreaterThan(0);
  });
});
