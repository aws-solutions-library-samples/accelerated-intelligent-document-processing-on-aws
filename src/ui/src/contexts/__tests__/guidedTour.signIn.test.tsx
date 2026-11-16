// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { act, render, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { GuidedTourProvider } from '../guidedTour';

const { annotation, auth, session } = vi.hoisted(() => ({
  annotation: { onFinish: undefined as undefined | (() => void) },
  auth: { user: undefined as undefined | { userId: string } },
  session: { current: {} as Record<string, unknown> },
}));

vi.mock('@aws-amplify/ui-react', () => ({ useAuthenticator: () => ({ user: auth.user }) }));
vi.mock('../../api/auth-session', () => ({
  fetchSharedAuthSession: vi.fn(async () => session.current),
}));
vi.mock('@cloudscape-design/components/annotation-context', () => ({
  default: ({ onFinish, children }: { onFinish: () => void; children: React.ReactNode }) => {
    annotation.onFinish = onFinish;
    return <>{children}</>;
  },
}));

const ADMIN_SESSION = { tokens: { idToken: { payload: { 'cognito:groups': ['Admin'], sub: 'admin-sub' } } } };

const tree = () => (
  <MemoryRouter>
    <GuidedTourProvider>
      <div />
    </GuidedTourProvider>
  </MemoryRouter>
);

const quickStartOpensOnFinish = () => {
  const opened = vi.fn();
  window.addEventListener('openQuickStart', opened);
  act(() => annotation.onFinish?.());
  window.removeEventListener('openQuickStart', opened);
  return opened.mock.calls.length;
};

describe('the guided tour across a sign-in', () => {
  beforeEach(() => {
    vi.stubEnv('VITE_ENABLE_QUICK_START_WIDGET', 'true');
    auth.user = undefined;
    session.current = {};
  });
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it('offers Quick Start to an Admin who signs in after the tour mounted', async () => {
    const view = render(tree());
    expect(quickStartOpensOnFinish()).toBe(0);

    auth.user = { userId: 'admin-1' };
    session.current = ADMIN_SESSION;
    view.rerender(tree());

    await waitFor(() => expect(quickStartOpensOnFinish()).toBe(1));
  });

  it('stops offering Quick Start once that Admin signs out', async () => {
    auth.user = { userId: 'admin-1' };
    session.current = ADMIN_SESSION;
    const view = render(tree());
    await waitFor(() => expect(quickStartOpensOnFinish()).toBe(1));

    auth.user = undefined;
    session.current = {};
    view.rerender(tree());

    await waitFor(() => expect(quickStartOpensOnFinish()).toBe(0));
  });
});
