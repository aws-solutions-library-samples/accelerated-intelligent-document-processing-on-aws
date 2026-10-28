// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Annotator-only shell, rendered for real (a real router and the real AuthRoutes;
 * only the pages are stand-ins). What has to hold: an Annotator-only user opens a
 * document's edit page, the key decoded once, because Inbox messages and Tasks link
 * there; the rest of /documents is not theirs and lands on Home › Tasks; and a user
 * with the full shell reaches the same edit page.
 */

import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, useLocation, useParams } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const roleRef = vi.hoisted(() => ({ current: {} as Record<string, unknown> }));

vi.mock('../../hooks/use-user-role', () => ({ default: () => roleRef.current }));
vi.mock('@aws-amplify/ui-react', () => ({
  useAuthenticator: () => ({ signOut: vi.fn() }),
  Button: ({ children }: { children?: React.ReactNode }) => <button type="button">{children}</button>,
}));
vi.mock('../../contexts/app', () => ({ default: () => ({ currentCredentials: { accessKeyId: 'AKIA' } }) }));
vi.mock('../../hooks/use-parameter-store', () => ({ default: () => ({ SomeSetting: 'x' }) }));
vi.mock('../../contexts/inbox', () => ({ InboxProvider: ({ children }: { children: React.ReactNode }) => <>{children}</> }));
vi.mock('../../contexts/review-work', () => ({ ReviewWorkProvider: ({ children }: { children: React.ReactNode }) => <>{children}</> }));

/** A page stand-in that says which page it is, and what the router gave it. */
const Probe = ({ name }: { name: string }): React.JSX.Element => {
  const params = useParams();
  const location = useLocation();
  return <div data-testid={name} data-key={params.objectKey ?? ''} data-path={location.pathname} />;
};
vi.mock('../DocumentEditRoute', () => ({ default: () => <Probe name="edit-page" /> }));
vi.mock('../MyWorkRoutes', () => ({ default: () => <Probe name="my-work" /> }));
vi.mock('../InboxRoutes', () => ({ default: () => <Probe name="inbox" /> }));
vi.mock('../ReviewQueuesRoutes', () => ({ default: () => <Probe name="review-queues" /> }));
vi.mock('../DocumentsQueryRoutes', () => ({ default: () => <div /> }));
vi.mock('../DocumentsAnalyticsRoutes', () => ({ default: () => <div /> }));
vi.mock('../TestStudioRoutes', () => ({ default: () => <div /> }));
vi.mock('../AgentChatRoutes', () => ({ default: () => <div /> }));
vi.mock('../FeaturesRoutes', () => ({ default: () => <div /> }));
vi.mock('../../pages/WelcomePage', () => ({ default: () => <div /> }));
vi.mock('../../components/agent-chat/QuickStartWidget', () => ({ default: () => <div /> }));
vi.mock('../../components/genaiidp-layout', () => ({ default: () => <Probe name="documents-app" /> }));
vi.mock('../../components/genai-idp-top-navigation', () => ({ default: () => null }));

import AuthRoutes from '../AuthRoutes';

const ANNOTATOR_ONLY = { loading: false, isAnnotator: true, isAnnotatorOnly: true, hasNoRole: false, sessionError: false };
const FULL_SHELL = { loading: false, isAnnotator: false, isAnnotatorOnly: false, hasNoRole: false, sessionError: false };

const renderAt = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <AuthRoutes redirectParam="" />
    </MemoryRouter>,
  );

beforeEach(() => {
  document.body.innerHTML = '';
  localStorage.setItem('idp-welcome-dismissed', 'true');
});

describe('the Annotator-only shell', () => {
  beforeEach(() => {
    roleRef.current = { ...ANNOTATOR_ONLY };
  });

  it('opens a document’s edit page, the key decoded once', () => {
    renderAt('/documents/a%2Fb.pdf/edit');
    expect(screen.getByTestId('edit-page').getAttribute('data-key')).toBe('a/b.pdf');
  });

  it.each(['/documents/a%2Fb.pdf', '/documents'])('sends %s, which is not theirs, to Home › Tasks', (path) => {
    renderAt(path);
    expect(screen.queryByTestId('edit-page')).toBeNull();
    expect(screen.queryByTestId('documents-app')).toBeNull();
    expect(screen.getByTestId('my-work').getAttribute('data-path')).toBe('/my-work/tasks');
  });
});

describe('the full shell', () => {
  it('opens the same edit page', () => {
    roleRef.current = { ...FULL_SHELL };
    renderAt('/documents/a%2Fb.pdf/edit');
    expect(screen.getByTestId('edit-page').getAttribute('data-key')).toBe('a/b.pdf');
  });
});
