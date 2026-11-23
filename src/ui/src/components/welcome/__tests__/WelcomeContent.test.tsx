// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { fireEvent, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { IntlProvider } from 'react-intl';
import { afterEach, describe, expect, it, vi } from 'vitest';

import WelcomeContent from '../WelcomeContent';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

vi.mock('../../../contexts/settings', () => ({
  default: () => ({ settings: {} }),
}));
const { role } = vi.hoisted(() => ({ role: { isAnnotatorOnly: false, isReviewerOnly: false, isAdmin: true, canWrite: true } }));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => role }));

const English = ({ children }: { children: React.ReactNode }) => (
  <IntlProvider locale="en" defaultLocale="en">
    {children}
  </IntlProvider>
);

const renderWelcome = () =>
  renderWithIntl(
    <MemoryRouter>
      <WelcomeContent />
    </MemoryRouter>,
    { wrapper: English },
  );

describe('WelcomeContent', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    role.isAdmin = true;
    role.canWrite = true;
    role.isReviewerOnly = false;
  });

  it('does not offer the tour to a Reviewer-only user, whose navigation it has no step for', () => {
    vi.stubEnv('VITE_ENABLE_QUICK_START_WIDGET', 'true');
    role.isAdmin = false;
    role.canWrite = false;
    role.isReviewerOnly = true;
    renderWelcome();

    expect(screen.queryByRole('button', { name: 'Take the tour' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Enter IDP Console' })).toBeInTheDocument();
  });

  it('offers Quick Start twice, and each opens the Quick Start chat', () => {
    vi.stubEnv('VITE_ENABLE_QUICK_START_WIDGET', 'true');
    const opened = vi.fn();
    window.addEventListener('openQuickStart', opened);
    renderWelcome();

    const entries = screen.getAllByRole('button', { name: 'Quick Start' });
    entries.forEach((entry) => fireEvent.click(entry));

    window.removeEventListener('openQuickStart', opened);
    expect(entries).toHaveLength(2);
    expect(opened).toHaveBeenCalledTimes(2);
    expect(screen.getByText(/No configuration yet\?/)).toBeInTheDocument();
  });

  it('tells an Admin what Quick Start does and that the console has it too, and offers Discovery beside it', () => {
    vi.stubEnv('VITE_ENABLE_QUICK_START_WIDGET', 'true');
    renderWelcome();

    expect(screen.getByText(/also at the top of the side navigation/)).toBeInTheDocument();
    expect(screen.getByText(/creates and activates a configuration/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Discovery' })).toBeInTheDocument();
  });

  it('offers no Quick Start when the stack turns it off, and points a writer at Discovery', () => {
    vi.stubEnv('VITE_ENABLE_QUICK_START_WIDGET', 'false');
    renderWelcome();

    expect(screen.queryByText('Quick Start')).toBeNull();
    expect(screen.getByText(/No configuration yet\?/)).toBeInTheDocument();
    expect(screen.getByText('Discovery')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Take the tour' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Enter IDP Console' })).toBeInTheDocument();
  });

  it('points an Author at Discovery instead of Quick Start', () => {
    vi.stubEnv('VITE_ENABLE_QUICK_START_WIDGET', 'true');
    role.isAdmin = false;
    renderWelcome();

    expect(screen.queryByText('Quick Start')).toBeNull();
    expect(screen.getByText(/No configuration yet\?/)).toBeInTheDocument();
    expect(screen.getByText('Discovery')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Take the tour' })).toBeInTheDocument();
  });

  it('offers a Viewer no way to create a configuration, and keeps the other ways in', () => {
    vi.stubEnv('VITE_ENABLE_QUICK_START_WIDGET', 'true');
    role.isAdmin = false;
    role.canWrite = false;
    renderWelcome();

    expect(screen.queryByText('Quick Start')).toBeNull();
    expect(screen.queryByText(/No configuration yet\?/)).toBeNull();
    expect(screen.getByRole('button', { name: 'Take the tour' })).toBeInTheDocument();
  });

  it.each([
    [true, true],
    [false, false],
  ])('starts the tour with Quick Start only for a user offered it (Admin: %s)', (isAdmin, quickStart) => {
    vi.stubEnv('VITE_ENABLE_QUICK_START_WIDGET', 'true');
    role.isAdmin = isAdmin;
    const started = vi.fn();
    window.addEventListener('startTutorial', started);
    renderWelcome();
    fireEvent.click(screen.getByRole('button', { name: 'Take the tour' }));
    window.removeEventListener('startTutorial', started);
    expect((started.mock.calls[0][0] as CustomEvent).detail.quickStart).toBe(quickStart);
  });

  it('names the upload page as the side navigation does', () => {
    vi.stubEnv('VITE_ENABLE_QUICK_START_WIDGET', 'true');
    renderWelcome();
    expect(screen.getByText('Upload Documents')).toBeInTheDocument();
  });
});
