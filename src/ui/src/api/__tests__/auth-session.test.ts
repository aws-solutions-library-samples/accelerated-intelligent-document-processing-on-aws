// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * App-wide de-duplication of fetchAuthSession.
 *
 * Concurrent identical GetCredentialsForIdentity calls race at Cognito and the
 * loser returns 400 "Invalid login token" with a valid token. De-duplicating
 * inside one hook only reduced that; this module is what removes it, so these
 * cases pin the two properties that matter — sharing, and NOT sharing a forced
 * refresh with an unforced one.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

const fetchAuthSession = vi.fn();
vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: (...args: unknown[]) => fetchAuthSession(...args),
}));

import { fetchSharedAuthSession, resetSharedAuthSession } from '../auth-session';

const deferred = () => {
  let resolve!: (v: unknown) => void;
  const promise = new Promise((r) => {
    resolve = r;
  });
  return { promise, resolve };
};

describe('fetchSharedAuthSession', () => {
  beforeEach(() => {
    fetchAuthSession.mockReset();
    resetSharedAuthSession();
  });

  it('collapses concurrent callers into one request', async () => {
    const d = deferred();
    fetchAuthSession.mockReturnValue(d.promise);

    const all = Promise.all([fetchSharedAuthSession(), fetchSharedAuthSession(), fetchSharedAuthSession()]);
    d.resolve({ credentials: { accessKeyId: 'A' } });
    const results = await all;

    expect(fetchAuthSession).toHaveBeenCalledTimes(1);
    expect(results.every((r) => r === results[0])).toBe(true);
  });

  it('fetches again once the shared promise has settled', async () => {
    fetchAuthSession.mockResolvedValue({ credentials: { accessKeyId: 'A' } });

    await fetchSharedAuthSession();
    await fetchSharedAuthSession();

    // Not a cache — a later call must still be able to refresh.
    expect(fetchAuthSession).toHaveBeenCalledTimes(2);
  });

  it('does NOT satisfy a forced refresh from a pending unforced call', async () => {
    // useUserRole forces a refresh precisely because it believes the cached
    // session is stale (after a group change). Serving it the in-flight
    // unforced result would hand back exactly the staleness it is trying to
    // escape.
    const pending = deferred();
    fetchAuthSession.mockReturnValueOnce(pending.promise).mockResolvedValue({ credentials: { accessKeyId: 'FRESH' } });

    const unforced = fetchSharedAuthSession();
    const forced = fetchSharedAuthSession({ forceRefresh: true });
    pending.resolve({ credentials: { accessKeyId: 'STALE' } });

    await unforced;
    const forcedResult = (await forced) as { credentials: { accessKeyId: string } };

    expect(fetchAuthSession).toHaveBeenCalledTimes(2);
    expect(fetchAuthSession).toHaveBeenCalledWith({ forceRefresh: true });
    expect(forcedResult.credentials.accessKeyId).toBe('FRESH');
  });

  it('shares between concurrent forced refreshes', async () => {
    const d = deferred();
    fetchAuthSession.mockReturnValue(d.promise);

    const all = Promise.all([fetchSharedAuthSession({ forceRefresh: true }), fetchSharedAuthSession({ forceRefresh: true })]);
    d.resolve({ credentials: { accessKeyId: 'A' } });
    await all;

    expect(fetchAuthSession).toHaveBeenCalledTimes(1);
  });

  it('does not wedge after a rejection', async () => {
    // A failed fetch must clear the slot, or one transient error would poison
    // every later caller with the same rejected promise.
    fetchAuthSession.mockRejectedValueOnce(new Error('boom')).mockResolvedValue({ credentials: { accessKeyId: 'A' } });

    await expect(fetchSharedAuthSession()).rejects.toThrow('boom');
    await expect(fetchSharedAuthSession()).resolves.toBeTruthy();
    expect(fetchAuthSession).toHaveBeenCalledTimes(2);
  });
});
