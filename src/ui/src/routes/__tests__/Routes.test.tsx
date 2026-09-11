// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * What Routes renders in each credential state — the thing that was actually
 * broken on screen.
 *
 * The hook tests cover the credential fetch; none of them covers the decision
 * this file makes, which is where the blank page came from: "authenticated but
 * no credentials yet" was treated as "not signed in", routed to the
 * unauthenticated tree, and rendered nothing.
 */

import { render, screen } from '@testing-library/react';
import React from 'react';
import { describe, expect, it, vi } from 'vitest';

const authStatusRef = { current: 'authenticated' as string };
const appContextRef = { current: {} as Record<string, unknown> };

vi.mock('@aws-amplify/ui-react', () => ({
  useAuthenticator: () => ({ authStatus: authStatusRef.current }),
  Authenticator: () => <div>sign-in form</div>,
}));

vi.mock('../../contexts/app', () => ({
  default: () => appContextRef.current,
}));

vi.mock('../AuthRoutes', () => ({ default: () => <div>the app</div> }));

// Mirrors the REAL behaviour rather than a friendly placeholder: UnauthRoutes
// sends /login to Amplify's <Authenticator>, which renders its children once
// authStatus is 'authenticated' — so while authenticated it renders NOTHING.
// A mock that always showed a form would make the "never empty" assertion below
// pass against the very bug it exists to catch.
vi.mock('../UnauthRoutes', () => ({
  default: () => (authStatusRef.current === 'authenticated' ? null : <div>sign-in form</div>),
}));

vi.mock('react-router-dom', () => ({
  useLocation: () => ({ pathname: '/', search: '' }),
}));

// Imported after the mocks above, which vitest hoists.
import Routes from '../Routes';

const CREDS = { accessKeyId: 'AKIA' };

const renderWith = (ctx: Record<string, unknown>, authStatus = 'authenticated') => {
  authStatusRef.current = authStatus;
  appContextRef.current = { retryCredentials: vi.fn(), ...ctx };
  return render(<Routes />);
};

describe('Routes credential gating', () => {
  it('renders the app once authenticated with credentials', () => {
    renderWith({ user: { username: 'u' }, currentCredentials: CREDS, credentialsStatus: 'ready' });

    expect(screen.getByText('the app')).toBeInTheDocument();
  });

  it('shows a loading state — NOT the sign-in form — while credentials are arriving', () => {
    // The bug: this case fell through to the unauthenticated tree, whose
    // <Authenticator> renders nothing when you are already authenticated.
    renderWith({ user: { username: 'u' }, currentCredentials: undefined, credentialsStatus: 'pending' });

    expect(screen.getByText(/Establishing your session/i)).toBeInTheDocument();
    expect(screen.queryByText('sign-in form')).not.toBeInTheDocument();
    expect(screen.queryByText('the app')).not.toBeInTheDocument();
  });

  it('shows an actionable error when credentials could not be obtained', () => {
    renderWith({ user: { username: 'u' }, currentCredentials: undefined, credentialsStatus: 'error' });

    expect(screen.getByText(/Could not establish your session/i)).toBeInTheDocument();
    // A way out, which the blank page never offered.
    expect(screen.getByRole('button', { name: /Retry/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Reload the page/i })).toBeInTheDocument();
  });

  it('never renders an empty page in any credential state', () => {
    // The regression guard. Whatever the combination, something must be on screen.
    for (const status of ['idle', 'pending', 'ready', 'error'] as const) {
      const { container, unmount } = renderWith({
        user: { username: 'u' },
        currentCredentials: status === 'ready' ? CREDS : undefined,
        credentialsStatus: status,
      });
      expect(container).not.toBeEmptyDOMElement();
      unmount();
    }
  });

  it('still shows the sign-in form when genuinely unauthenticated', () => {
    renderWith({ user: undefined, currentCredentials: undefined, credentialsStatus: 'idle' }, 'unauthenticated');

    expect(screen.getByText('sign-in form')).toBeInTheDocument();
  });
});
