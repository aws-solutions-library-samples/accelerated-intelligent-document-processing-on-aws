// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const { graphqlMock, app } = vi.hoisted(() => ({
  graphqlMock: vi.fn(),
  app: { setSuccessMessage: vi.fn(), setErrorMessage: vi.fn() },
}));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: graphqlMock }) }));
vi.mock('../../../contexts/app', () => ({ default: () => app }));

import InboxPreferences, { PREFERENCES_EMPTY_TEXT } from '../InboxPreferences';

const TYPES = [
  {
    id: 'review.assigned',
    displayName: 'Review assigned',
    description: 'A review was assigned to you',
    defaultSeverity: 'info',
    muted: false,
  },
  {
    id: 'review.expiring',
    displayName: 'Claim expiring',
    description: 'A claim is about to expire',
    defaultSeverity: 'warning',
    muted: true,
  },
];

const prefs = (types = TYPES, mutedTypes = ['review.expiring']) => ({
  data: { getInboxPreferences: { types, mutedTypes, updatedAt: null } },
});

const toggles = () => createWrapper(document.body).findAllToggles();
const saveButton = () => screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement;

describe('InboxPreferences', () => {
  beforeEach(() => {
    graphqlMock.mockReset();
    app.setSuccessMessage.mockReset();
    app.setErrorMessage.mockReset();
  });

  it('explains the empty state', async () => {
    graphqlMock.mockResolvedValue(prefs([], []));
    render(<InboxPreferences visible onDismiss={() => undefined} />);
    expect(await screen.findByText(PREFERENCES_EMPTY_TEXT)).toBeTruthy();
    expect(PREFERENCES_EMPTY_TEXT).toBe(
      'No message types yet. Types appear here when a platform feature that sends Inbox messages is in use.',
    );
  });

  it('shows one Receive toggle per type, off for muted types', async () => {
    graphqlMock.mockResolvedValue(prefs());
    render(<InboxPreferences visible onDismiss={() => undefined} />);
    await screen.findByText('Review assigned');
    expect(screen.getByText('A claim is about to expire')).toBeTruthy();
    const [assigned, expiring] = toggles();
    expect(assigned.findNativeInput().getElement().checked).toBe(true);
    expect(expiring.findNativeInput().getElement().checked).toBe(false);
    expect(saveButton().disabled).toBe(true);
  });

  it('saves the muted types and flashes success', async () => {
    graphqlMock.mockImplementation((args: { query: string; variables?: { mutedTypes: string[] } }) =>
      args.query.includes('updateInboxPreferences')
        ? Promise.resolve({
            data: { updateInboxPreferences: { types: TYPES, mutedTypes: args.variables?.mutedTypes ?? [], updatedAt: null } },
          })
        : Promise.resolve(prefs()),
    );
    render(<InboxPreferences visible onDismiss={() => undefined} />);
    await screen.findByText('Review assigned');

    const [assigned, expiring] = toggles();
    act(() => assigned.findNativeInput().click());
    act(() => expiring.findNativeInput().click());
    expect(saveButton().disabled).toBe(false);
    await act(async () => saveButton().click());

    const call = graphqlMock.mock.calls.find(([args]) => (args as { query: string }).query.includes('updateInboxPreferences'));
    expect(call?.[0].variables).toEqual({ mutedTypes: ['review.assigned'] });
    await waitFor(() => expect(app.setSuccessMessage).toHaveBeenCalledWith('Inbox preferences saved'));
    expect(saveButton().disabled).toBe(true);
  });

  it('flashes the error when saving fails', async () => {
    graphqlMock.mockImplementation((args: { query: string }) =>
      args.query.includes('updateInboxPreferences')
        ? Promise.reject({ errors: [{ message: 'nope', errorType: 'InternalError' }] })
        : Promise.resolve(prefs()),
    );
    render(<InboxPreferences visible onDismiss={() => undefined} />);
    await screen.findByText('Review assigned');
    act(() => toggles()[0].findNativeInput().click());
    await act(async () => saveButton().click());

    await waitFor(() => expect(app.setErrorMessage).toHaveBeenCalledWith(expect.stringContaining('Failed to save Inbox preferences')));
    expect(app.setSuccessMessage).not.toHaveBeenCalled();
  });

  it('shows an Alert with Retry when loading fails', async () => {
    graphqlMock.mockRejectedValueOnce({ errors: [{ message: 'down', errorType: 'InternalError' }] });
    render(<InboxPreferences visible onDismiss={() => undefined} />);
    expect(await screen.findByText('Could not load preferences')).toBeTruthy();

    graphqlMock.mockResolvedValue(prefs());
    await act(async () => screen.getByRole('button', { name: 'Retry' }).click());
    expect(await screen.findByText('Review assigned')).toBeTruthy();
  });
});
