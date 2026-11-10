// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import WelcomeContent from '../WelcomeContent';

vi.mock('../../../contexts/settings', () => ({
  default: () => ({ settings: {} }),
}));
vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({ isAnnotatorOnly: false }),
}));

const renderWelcome = () =>
  render(
    <MemoryRouter>
      <WelcomeContent />
    </MemoryRouter>,
  );

describe('WelcomeContent', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
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

  it('offers no Quick Start when the stack turns it off, and keeps the other ways in', () => {
    vi.stubEnv('VITE_ENABLE_QUICK_START_WIDGET', 'false');
    renderWelcome();

    expect(screen.queryByText('Quick Start')).toBeNull();
    expect(screen.queryByText(/No configuration yet\?/)).toBeNull();
    expect(screen.getByRole('button', { name: 'Take the tour' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Enter IDP Console' })).toBeInTheDocument();
  });

  it('names the upload page as the side navigation does', () => {
    vi.stubEnv('VITE_ENABLE_QUICK_START_WIDGET', 'true');
    renderWelcome();
    expect(screen.getByText('Upload Documents')).toBeInTheDocument();
  });
});
