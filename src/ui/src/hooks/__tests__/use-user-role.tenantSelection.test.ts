// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The tenant fields `useUserRole` reports for `TenantSelection=UserSelect`: the tenants
 * a user may choose between, the one that is active, and the subject the remembered
 * choice is keyed on. Absent for everyone the feature does not apply to.
 */

import { renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const fetchSharedAuthSession = vi.fn();
const graphql = vi.fn();

vi.mock('../../api/auth-session', () => ({
  fetchSharedAuthSession: (...args: unknown[]) => fetchSharedAuthSession(...args),
}));
vi.mock('../../api/client-shim', () => ({
  generateClient: () => ({ graphql: (...args: unknown[]) => graphql(...args) }),
}));

import useUserRole, { resetSharedProfileScope } from '../use-user-role';

const session = (groups: string[], sub = 'sub-1') => ({ tokens: { idToken: { payload: { 'cognito:groups': groups, sub } } } });
const profile = (fields: Record<string, unknown>) => ({ data: { getMyProfile: fields } });

describe('useUserRole tenant selection fields', () => {
  beforeEach(() => {
    fetchSharedAuthSession.mockReset();
    graphql.mockReset();
    resetSharedProfileScope();
  });

  it('reports the tenants on offer while none is active', async () => {
    fetchSharedAuthSession.mockResolvedValue(session(['Viewer']));
    graphql.mockResolvedValue(
      profile({ allowedConfigVersions: ['!no-tenant-granted'], eligibleTenants: ['acme', 'globex'], scopeTenant: null }),
    );
    const { result } = renderHook(() => useUserRole());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.eligibleTenants).toEqual(['acme', 'globex']);
    expect(result.current.activeTenant).toBeNull();
    expect(result.current.allowedConfigVersions).toEqual(['!no-tenant-granted']);
    expect(result.current.subject).toBe('sub-1');
  });

  it('reports the active tenant once one is chosen', async () => {
    fetchSharedAuthSession.mockResolvedValue(session(['Reviewer']));
    graphql.mockResolvedValue(profile({ allowedConfigVersions: ['globex-*'], eligibleTenants: ['acme', 'globex'], scopeTenant: 'globex' }));
    const { result } = renderHook(() => useUserRole());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.activeTenant).toBe('globex');
    expect(result.current.eligibleTenants).toEqual(['acme', 'globex']);
  });

  it('reports nothing on offer for a user the feature does not apply to', async () => {
    fetchSharedAuthSession.mockResolvedValue(session(['Viewer']));
    graphql.mockResolvedValue(profile({ allowedConfigVersions: ['acme-*'], scopeTenant: 'acme' }));
    const { result } = renderHook(() => useUserRole());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.eligibleTenants).toEqual([]);
    expect(result.current.activeTenant).toBe('acme');
  });

  it('reports nothing on offer for an Admin, whose profile is not read', async () => {
    fetchSharedAuthSession.mockResolvedValue(session(['Admin']));
    const { result } = renderHook(() => useUserRole());
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(graphql).not.toHaveBeenCalled();
    expect(result.current.eligibleTenants).toEqual([]);
    expect(result.current.activeTenant).toBeNull();
  });
});
