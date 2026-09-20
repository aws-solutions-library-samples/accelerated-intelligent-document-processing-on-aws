// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * What `AuthRoutes` renders for an account with no role, and for a session it
 * could not read.
 *
 * `Routes.test.tsx` mocks this component out, so its new branches had no coverage
 * at all — and the hook tests cannot see them, which is exactly the gap that
 * file's own docstring describes for the blank page it was written about.
 *
 * Three decisions are asserted here, and the second is the one that was wrong
 * before review: a failed session read must render `SessionError` ("usually
 * temporary"), NOT the no-role message. `api/auth-session.ts` records a live
 * `400 NotAuthorizedException` on a perfectly valid token, shared across every
 * consumer of one in-flight promise, and nothing retries it — so sending that
 * user to an administrator for a role would strand an entitled Admin in front of
 * a message about a problem nobody can fix.
 */

import { render, screen } from '@testing-library/react';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const roleRef = { current: {} as Record<string, unknown> };
const signOut = vi.fn();

vi.mock('../../hooks/use-user-role', () => ({
  default: () => roleRef.current,
}));

vi.mock('@aws-amplify/ui-react', () => ({
  useAuthenticator: () => ({ signOut }),
  Button: ({ children, ...rest }: { children?: React.ReactNode }) => (
    <button type="button" {...rest}>
      {children}
    </button>
  ),
}));

vi.mock('../../contexts/app', () => ({ default: () => ({ currentCredentials: { accessKeyId: 'AKIA' } }) }));

// Settings must be non-empty or the component short-circuits to a spinner before
// reaching either branch under test.
vi.mock('../../hooks/use-parameter-store', () => ({ default: () => ({ SomeSetting: 'x' }) }));

// The route trees are irrelevant here and pull in the whole app if left real.
vi.mock('../DocumentsRoutes', () => ({ default: () => <div>documents</div> }));
vi.mock('../DocumentsQueryRoutes', () => ({ default: () => <div /> }));
vi.mock('../DocumentsAnalyticsRoutes', () => ({ default: () => <div /> }));
vi.mock('../TestStudioRoutes', () => ({ default: () => <div /> }));
vi.mock('../AgentChatRoutes', () => ({ default: () => <div /> }));
vi.mock('../FeaturesRoutes', () => ({ default: () => <div /> }));
vi.mock('../../pages/WelcomePage', () => ({ default: () => <div>welcome</div> }));
vi.mock('../../components/agent-chat/QuickStartWidget', () => ({ default: () => <div /> }));

vi.mock('react-router-dom', () => ({
  Routes: ({ children }: { children?: React.ReactNode }) => <div>{children}</div>,
  Route: () => null,
  Navigate: () => null,
}));

// Imported after the mocks, which vitest hoists.
import AuthRoutes from '../AuthRoutes';

const GROUPED = { isAnnotatorOnly: false, hasNoRole: false, sessionError: false };

describe('AuthRoutes role gating', () => {
  beforeEach(() => {
    signOut.mockReset();
    roleRef.current = { ...GROUPED };
  });

  it('explains the missing role instead of mounting the app', () => {
    roleRef.current = { ...GROUPED, hasNoRole: true };

    render(<AuthRoutes redirectParam="" />);

    expect(screen.getByText(/has not been granted access yet/i)).toBeTruthy();
    // Actionable: names the roles, and does not tell the user to retry a request
    // that cannot succeed.
    expect(screen.getByText(/Admin, Author, Reviewer, Annotator or Viewer/)).toBeTruthy();
    expect(screen.getByText(/Sign out/)).toBeTruthy();
    // And the app really is not mounted behind it.
    expect(screen.queryByText('documents')).toBeNull();
  });

  it('shows the transient-session message, not the no-role message, on a failed read', () => {
    roleRef.current = { ...GROUPED, sessionError: true };

    render(<AuthRoutes redirectParam="" />);

    expect(screen.getByText(/Could not establish your session/i)).toBeTruthy();
    expect(screen.getByText(/usually temporary/i)).toBeTruthy();
    expect(screen.queryByText(/has not been granted access yet/i)).toBeNull();
  });

  it('mounts the app for a user who holds a role', () => {
    render(<AuthRoutes redirectParam="" />);

    expect(screen.queryByText(/has not been granted access yet/i)).toBeNull();
    expect(screen.queryByText(/Could not establish your session/i)).toBeNull();
  });
});
