// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The language menu's slot in the top navigation: absent while only `en` is enabled (the
 * utilities array is exactly what it was before i18n), present once a second locale is.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { LocaleProvider } from '../../../i18n/LocaleProvider';
import type { Locale } from '../../../i18n/locales';
import GenAIIDPTopNavigation from '../GenAIIDPTopNavigation';

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

const LANGUAGE_MENU = /^Language, currently /;

const renderNav = (enabledLocales: readonly Locale[]) =>
  render(
    <LocaleProvider enabledLocales={enabledLocales}>
      <GenAIIDPTopNavigation />
    </LocaleProvider>,
  );

describe('the language menu slot in the top navigation', () => {
  it('adds nothing while only en is enabled: inbox, feedback and the user menu, as before', () => {
    const { container } = renderNav(['en']);
    expect(screen.queryAllByLabelText(LANGUAGE_MENU)).toEqual([]);
    expect(container.innerHTML).not.toContain('globe');
    expect(container.innerHTML).not.toContain('Language');
    // The utilities that were there before are still there.
    expect(screen.getAllByLabelText('Inbox, no unread messages').length).toBeGreaterThan(0);
    expect(screen.getAllByLabelText('Feedback and issue reporting').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Alice (Admin)').length).toBeGreaterThan(0);
  });

  it('shows the language menu once a second locale is enabled, between feedback and the user menu', () => {
    renderNav(['en', 'fr-CA']);
    // Cloudscape renders each utility more than once (one copy is for measuring), so the
    // first copies are compared: they belong to the same rendering and share its order.
    const [feedback] = screen.getAllByLabelText('Feedback and issue reporting');
    const [language] = screen.getAllByLabelText('Language, currently English');
    const [user] = screen.getAllByText('Alice (Admin)');
    const follows = (a: Element, b: Element): boolean => Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    expect(follows(feedback, language)).toBe(true);
    expect(follows(language, user)).toBe(true);
  });
});
