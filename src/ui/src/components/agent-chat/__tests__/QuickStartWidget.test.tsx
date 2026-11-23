// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { act, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import QuickStartWidget from '../QuickStartWidget';
import { WELCOME_PATH } from '../../../routes/constants';

vi.mock('../../../contexts/agentChat', () => ({
  AgentChatProvider: ({ initialMode, children }: { initialMode: string; children: React.ReactNode }) => (
    <div data-testid="agent-chat-provider" data-mode={initialMode}>
      {children}
    </div>
  ),
  useAgentChatContext: () => ({ agentChatState: { mode: 'quick_start' } }),
}));
vi.mock('../AgentChatLayout', () => ({
  default: () => <div data-testid="agent-chat-layout" />,
}));

const renderAt = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <QuickStartWidget />
    </MemoryRouter>,
  );

describe('QuickStartWidget', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it('renders nothing when the stack turns the widget off', () => {
    vi.stubEnv('VITE_ENABLE_QUICK_START_WIDGET', 'false');
    const { container } = renderAt('/documents');
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing on the Welcome page, which has its own Quick Start entry', () => {
    vi.stubEnv('VITE_ENABLE_QUICK_START_WIDGET', 'true');
    const { container } = renderAt(WELCOME_PATH);
    expect(container).toBeEmptyDOMElement();
  });

  it('opens a Quick Start chat when an entry point asks for it', () => {
    vi.stubEnv('VITE_ENABLE_QUICK_START_WIDGET', 'true');
    renderAt('/documents');
    expect(screen.queryByTestId('agent-chat-provider')).toBeNull();

    act(() => {
      window.dispatchEvent(new CustomEvent('openQuickStart'));
    });

    expect(screen.getByTestId('agent-chat-provider')).toHaveAttribute('data-mode', 'quick_start');
    expect(screen.getByTestId('agent-chat-layout')).toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: 'Assistant' })).toHaveTextContent('Quick Start');
  });
});
