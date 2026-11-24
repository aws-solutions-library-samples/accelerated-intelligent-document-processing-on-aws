// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from 'react';
import { fireEvent, screen, waitFor, act } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({ graphql: (...args: unknown[]) => graphql(...args) }),
}));

import { renderHookWithIntl, renderWithIntl } from '../../../test-utils/renderWithIntl';
import useTenantSwitcher from '../useTenantSwitcher';

const reload = vi.fn();

describe('useTenantSwitcher', () => {
  beforeEach(() => {
    graphql.mockReset();
    reload.mockReset();
    sessionStorage.clear();
    Object.defineProperty(window, 'location', { configurable: true, value: { ...window.location, reload } });
  });

  it('offers no switcher to a user with fewer than two tenants', () => {
    for (const eligibleTenants of [undefined, [], ['acme']]) {
      const { result } = renderHookWithIntl(() => useTenantSwitcher({ eligibleTenants, activeTenant: 'acme', subject: 'sub-1' }));
      expect(result.current.utility).toBeNull();
      expect(result.current.modal).toBeNull();
    }
  });

  it('lists the tenants and marks the active one', () => {
    const { result } = renderHookWithIntl(() =>
      useTenantSwitcher({ eligibleTenants: ['acme', 'globex'], activeTenant: 'acme', subject: 'sub-1' }),
    );
    const utility = result.current.utility as unknown as { text: string; items: { id: string; disabled?: boolean }[] };
    expect(utility.text).toBe('Tenant: acme');
    expect(utility.items.map((i) => i.id)).toEqual(['tenant-acme', 'tenant-globex']);
    expect(utility.items.find((i) => i.id === 'tenant-acme')?.disabled).toBe(true);
    expect(utility.items.find((i) => i.id === 'tenant-globex')?.disabled).toBe(false);
  });

  it('asks before switching, then selects and reloads', async () => {
    graphql.mockResolvedValue({ data: { selectActiveTenant: { scopeTenant: 'globex' } } });
    const Harness = () => {
      const { utility, modal } = useTenantSwitcher({ eligibleTenants: ['acme', 'globex'], activeTenant: 'acme', subject: 'sub-1' });
      return (
        <div>
          <button
            onClick={() => (utility as unknown as { onItemClick: (e: unknown) => void }).onItemClick({ detail: { id: 'tenant-globex' } })}
          >
            pick globex
          </button>
          {modal}
        </div>
      );
    };
    renderWithIntl(<Harness />);
    fireEvent.click(screen.getByText('pick globex'));
    expect(await screen.findByText(/Switch to globex\?/)).toBeTruthy();
    // Nothing is sent until the user confirms.
    expect(graphql).not.toHaveBeenCalled();

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Confirm switch' }));
    });
    await waitFor(() => expect(reload).toHaveBeenCalled());
    expect(graphql.mock.calls[0][0].variables).toEqual({ tenantId: 'globex' });
  });

  it('ignores a pick of the active tenant or one that is not on offer', () => {
    const { result } = renderHookWithIntl(() =>
      useTenantSwitcher({ eligibleTenants: ['acme', 'globex'], activeTenant: 'acme', subject: 'sub-1' }),
    );
    const onItemClick = (result.current.utility as unknown as { onItemClick: (e: unknown) => void }).onItemClick;
    act(() => onItemClick({ detail: { id: 'tenant-acme' } }));
    act(() => onItemClick({ detail: { id: 'tenant-initech' } }));
    expect(graphql).not.toHaveBeenCalled();
  });
});
