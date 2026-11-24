// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from 'react';
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({ graphql: (...args: unknown[]) => graphql(...args) }),
}));

import { renderWithIntl } from '../../../test-utils/renderWithIntl';
import TenantSelectionGate from '../TenantSelectionGate';
import { TENANT_HINT_KEY, writeTenantHint } from '../../../utils/tenant-selection';

const reload = vi.fn();

describe('TenantSelectionGate', () => {
  beforeEach(() => {
    graphql.mockReset();
    reload.mockReset();
    sessionStorage.clear();
    Object.defineProperty(window, 'location', { configurable: true, value: { ...window.location, reload } });
  });
  afterEach(() => sessionStorage.clear());

  const renderGate = (onSignOut = vi.fn()) =>
    renderWithIntl(<TenantSelectionGate eligibleTenants={['acme', 'globex']} subject="sub-1" onSignOut={onSignOut} />);

  it('offers each tenant and keeps Confirm tenant disabled until one is chosen', () => {
    renderGate();
    expect(screen.getByLabelText('acme')).toBeTruthy();
    expect(screen.getByLabelText('globex')).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Confirm tenant' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('selects the chosen tenant, remembers it and reloads', async () => {
    graphql.mockResolvedValue({ data: { selectActiveTenant: { scopeTenant: 'globex' } } });
    renderGate();
    fireEvent.click(screen.getByLabelText('globex'));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm tenant' }));

    await waitFor(() => expect(reload).toHaveBeenCalled());
    expect(graphql).toHaveBeenCalledTimes(1);
    expect(graphql.mock.calls[0][0].variables).toEqual({ tenantId: 'globex' });
    expect(JSON.parse(sessionStorage.getItem(TENANT_HINT_KEY) as string)).toEqual({ sub: 'sub-1', tenantId: 'globex' });
  });

  it("shows the server's refusal and keeps the page open", async () => {
    graphql.mockRejectedValue({ errors: [{ message: "Tenant 'globex' is not one the identity provider grants this user." }] });
    renderGate();
    fireEvent.click(screen.getByLabelText('globex'));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm tenant' }));

    expect(await screen.findByText(/not one the identity provider grants/)).toBeTruthy();
    expect(reload).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(TENANT_HINT_KEY)).toBeNull();
  });

  it('applies a remembered tenant without asking', async () => {
    writeTenantHint('sub-1', 'acme');
    graphql.mockResolvedValue({ data: { selectActiveTenant: { scopeTenant: 'acme' } } });
    renderGate();

    await waitFor(() => expect(reload).toHaveBeenCalled());
    expect(graphql.mock.calls[0][0].variables).toEqual({ tenantId: 'acme' });
  });

  it('asks again, and forgets the choice, when the server refuses a remembered tenant', async () => {
    writeTenantHint('sub-1', 'acme');
    graphql.mockRejectedValue({ errors: [{ message: 'no longer granted' }] });
    renderGate();

    expect(await screen.findByRole('button', { name: 'Confirm tenant' })).toBeTruthy();
    expect(sessionStorage.getItem(TENANT_HINT_KEY)).toBeNull();
    expect(reload).not.toHaveBeenCalled();
  });

  it('does not apply a remembered tenant that is no longer on offer', () => {
    writeTenantHint('sub-1', 'initech');
    renderGate();
    expect(graphql).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Confirm tenant' })).toBeTruthy();
  });

  it("does not apply another user's remembered tenant", () => {
    writeTenantHint('someone-else', 'acme');
    renderGate();
    expect(graphql).not.toHaveBeenCalled();
  });

  it('signs out without selecting', () => {
    const onSignOut = vi.fn();
    renderGate(onSignOut);
    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }));
    expect(onSignOut).toHaveBeenCalled();
    expect(graphql).not.toHaveBeenCalled();
  });
});
