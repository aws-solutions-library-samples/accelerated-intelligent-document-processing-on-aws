// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * A signed-in account in no application role has to be recognisable as such.
 *
 * The API refuses such a caller the document reads (`listDocuments`, `getDocument`,
 * `getFileContents`, …) with 403, because those operations require an assigned
 * Cognito group. Without `hasNoRole` the app mounted the full Viewer navigation for
 * them — `navigation.tsx` falls through to `viewerNavItems` when no role flag is set
 * — so every page failed in turn and the result read as a broken deployment rather
 * than an account nobody had finished setting up.
 *
 * Two properties matter and are easy to get wrong:
 *
 *  * it must be false while the session is still resolving, or a normal sign-in
 *    flashes the "no access" screen;
 *  * it must test membership of the app's own group vocabulary, not
 *    `groups.length`. An IdP-mapped group name this app does not know grants
 *    nothing here, so it must not read as a role.
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

const sessionWithGroups = (groups: string[] | undefined) => ({
  tokens: { idToken: { payload: { ...(groups ? { 'cognito:groups': groups } : {}), sub: 'user-1' } } },
});

describe('useUserRole hasNoRole', () => {
  beforeEach(() => {
    fetchSharedAuthSession.mockReset();
    graphql.mockReset();
    resetSharedProfileScope();
    // A groupless caller is refused getMyProfile's siblings but not getMyProfile
    // itself, which stays ANY precisely so this case can resolve.
    graphql.mockResolvedValue({ data: { getMyProfile: { allowedConfigVersions: [], allowedTestSets: [] } } });
  });

  it('is true for a self-registered user whose groups claim is absent', async () => {
    fetchSharedAuthSession.mockResolvedValue(sessionWithGroups(undefined));

    const { result } = renderHook(() => useUserRole());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.hasNoRole).toBe(true);
    expect(result.current.groups).toEqual([]);
  });

  it('is true when the claim carries only a group this app does not know', async () => {
    fetchSharedAuthSession.mockResolvedValue(sessionWithGroups(['SomeIdpGroup']));

    const { result } = renderHook(() => useUserRole());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.hasNoRole).toBe(true);
  });

  it.each(['Admin', 'Author', 'Reviewer', 'Annotator', 'Viewer'])('is false for a %s', async (group) => {
    fetchSharedAuthSession.mockResolvedValue(sessionWithGroups([group]));

    const { result } = renderHook(() => useUserRole());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.hasNoRole).toBe(false);
  });

  it('is false while the session is still being read', () => {
    // A promise that never settles: the hook is mid-flight, which must not render
    // as "no access".
    fetchSharedAuthSession.mockReturnValue(new Promise(() => {}));

    const { result } = renderHook(() => useUserRole());

    expect(result.current.loading).toBe(true);
    expect(result.current.hasNoRole).toBe(false);
  });

  it('is never true at any point during a grouped user’s load', async () => {
    // The property that actually matters and that the "false while loading" case
    // above cannot see: `setGroups` and `setLoading(false)` are separated by an
    // await (the profile fetch), so they land in different renders. If the order
    // were ever inverted there would be one render with loading false and groups
    // still empty — a flash of the full-screen "no access" message for an entitled
    // user. So record EVERY rendered value rather than sampling the settled one.
    fetchSharedAuthSession.mockResolvedValue(sessionWithGroups(['Viewer']));
    const seen: boolean[] = [];

    const { result } = renderHook(() => {
      const role = useUserRole();
      seen.push(role.hasNoRole);
      return role;
    });

    await waitFor(() => expect(result.current.loading).toBe(false));
    await waitFor(() => expect(result.current.groups).toEqual(['Viewer']));
    expect(seen.length).toBeGreaterThan(1); // more than the initial render
    expect(seen).not.toContain(true);
  });

  it('reports a failed session read as sessionError, not as having no role', async () => {
    // `api/auth-session.ts` documents this live: a `400 NotAuthorizedException` on a
    // valid token, shared by every consumer of the one in-flight promise. Telling an
    // Admin to go and ask an administrator for a role sends them to someone with
    // nothing to fix, and nothing retries this effect for the life of the mount.
    fetchSharedAuthSession.mockRejectedValue(new Error('NotAuthorizedException'));

    const { result } = renderHook(() => useUserRole());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.sessionError).toBe(true);
    expect(result.current.hasNoRole).toBe(false);
  });

  it('does not set sessionError when the groups are genuinely absent', async () => {
    fetchSharedAuthSession.mockResolvedValue(sessionWithGroups(undefined));

    const { result } = renderHook(() => useUserRole());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.sessionError).toBe(false);
    expect(result.current.hasNoRole).toBe(true);
  });
});
