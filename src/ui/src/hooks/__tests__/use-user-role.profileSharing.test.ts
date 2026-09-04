// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * One `getMyProfile` per signed-in user, however many components ask for the role.
 *
 * Measured on a live stack: loading the annotate screen as an Annotator issued six
 * identical getMyProfile calls. The hook is consumed by the navigation, the annotation
 * landing page, the annotation workspace and each feature page, all mounted together,
 * and each ran its own effect for an answer that is per-user and fixed for the session.
 *
 * The cache is keyed by the token's subject rather than being a bare "already fetched"
 * flag, and that is the case worth protecting: signing out and back in as a different
 * user is routine here — it is how the annotator role gets exercised — and a key-less
 * cache would hand the new session the previous user's scope. That fails OPEN for the
 * least-privileged role, which is the one allowedTestSets exists to constrain.
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

// Imported after the mocks, which vitest hoists.
import useUserRole, { resetSharedProfileScope } from '../use-user-role';

/** An id token for a non-Admin (Admin skips the profile call entirely). */
const sessionFor = (sub: string, groups: string[] = ['Annotator']) => ({
  tokens: { idToken: { payload: { 'cognito:groups': groups, sub } } },
});

const profile = (sets: string[]) => ({
  data: { getMyProfile: { allowedConfigVersions: [], allowedTestSets: sets } },
});

describe('useUserRole profile sharing', () => {
  beforeEach(() => {
    fetchSharedAuthSession.mockReset();
    graphql.mockReset();
    resetSharedProfileScope();
  });

  it('fetches the profile once for several simultaneous consumers', async () => {
    fetchSharedAuthSession.mockResolvedValue(sessionFor('user-1'));
    graphql.mockResolvedValue(profile(['set-a']));

    const first = renderHook(() => useUserRole());
    const second = renderHook(() => useUserRole());
    const third = renderHook(() => useUserRole());

    await waitFor(() => {
      expect(first.result.current.loading).toBe(false);
      expect(second.result.current.loading).toBe(false);
      expect(third.result.current.loading).toBe(false);
    });

    expect(graphql).toHaveBeenCalledTimes(1);
    // Every consumer still gets the answer — deduplicating must not starve the
    // later mounts, which is the failure mode a naive in-flight guard has.
    for (const hook of [first, second, third]) {
      expect(hook.result.current.allowedTestSets).toEqual(['set-a']);
    }
  });

  it('does not serve one user the scope cached for another', async () => {
    fetchSharedAuthSession.mockResolvedValue(sessionFor('user-1'));
    graphql.mockResolvedValue(profile(['set-a']));

    const before = renderHook(() => useUserRole());
    await waitFor(() => expect(before.result.current.allowedTestSets).toEqual(['set-a']));

    // Sign out, sign in as someone assigned a different set.
    fetchSharedAuthSession.mockResolvedValue(sessionFor('user-2'));
    graphql.mockResolvedValue(profile(['set-b']));

    const after = renderHook(() => useUserRole());
    await waitFor(() => expect(after.result.current.allowedTestSets).toEqual(['set-b']));

    expect(graphql).toHaveBeenCalledTimes(2);
  });

  it('lets a failed fetch be retried instead of caching the failure', async () => {
    fetchSharedAuthSession.mockResolvedValue(sessionFor('user-1'));
    graphql.mockRejectedValueOnce(new Error('throttled'));

    const failed = renderHook(() => useUserRole());
    // The hook treats a profile failure as non-critical and defaults to unrestricted,
    // so the observable outcome is that it stopped loading with no scope.
    await waitFor(() => expect(failed.result.current.loading).toBe(false));
    expect(failed.result.current.allowedTestSets).toBeNull();

    graphql.mockResolvedValue(profile(['set-a']));
    const retried = renderHook(() => useUserRole());
    await waitFor(() => expect(retried.result.current.allowedTestSets).toEqual(['set-a']));

    // A cached rejection would have made this the same call count as the first case.
    expect(graphql).toHaveBeenCalledTimes(2);
  });

  it('does not cache at all for a token with no subject', async () => {
    // A constant fallback key would pool every subject-less session into one
    // bucket, which is the cross-user leak the keying exists to prevent.
    fetchSharedAuthSession.mockResolvedValue({ tokens: { idToken: { payload: { 'cognito:groups': ['Annotator'] } } } });
    graphql.mockResolvedValueOnce(profile(['set-a'])).mockResolvedValueOnce(profile(['set-b']));

    const first = renderHook(() => useUserRole());
    await waitFor(() => expect(first.result.current.allowedTestSets).toEqual(['set-a']));
    const second = renderHook(() => useUserRole());
    await waitFor(() => expect(second.result.current.allowedTestSets).toEqual(['set-b']));

    expect(graphql).toHaveBeenCalledTimes(2);
  });

  it('still skips the call entirely for an Admin, who is never scoped', async () => {
    fetchSharedAuthSession.mockResolvedValue(sessionFor('admin-1', ['Admin']));

    const hook = renderHook(() => useUserRole());
    await waitFor(() => expect(hook.result.current.loading).toBe(false));

    expect(hook.result.current.isAdmin).toBe(true);
    expect(graphql).not.toHaveBeenCalled();
  });
});
