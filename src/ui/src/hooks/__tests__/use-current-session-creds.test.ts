// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The credential bootstrap that used to strand a signed-in user on a blank page.
 *
 * Four defects composed into a dead end: two concurrent identical
 * `GetCredentialsForIdentity` calls raced and one was rejected; the failure was
 * swallowed with no retry for 15 minutes; the whole authenticated app was gated
 * on the resulting undefined credentials; and the unauthenticated tree renders
 * nothing when you are in fact authenticated. These cover the first two, plus a
 * timer leak found in the same file.
 */

import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const fetchAuthSession = vi.fn();
const authStatusRef = { current: 'authenticated' as string };

vi.mock('aws-amplify/auth', () => ({
  fetchAuthSession: (...args: unknown[]) => fetchAuthSession(...args),
}));

vi.mock('@aws-amplify/ui-react', () => ({
  useAuthenticator: () => ({ authStatus: authStatusRef.current }),
}));

// Imported after the vi.mock calls above, which vitest hoists regardless.
// The de-duplication now lives in api/auth-session so every caller in the app
// shares it, not just this hook — mocking aws-amplify/auth still covers it,
// since that is what the shared module calls.
import { resetSharedAuthSession } from '../../api/auth-session';
import useCurrentSessionCreds from '../use-current-session-creds';

const CREDS = { accessKeyId: 'AKIA', secretAccessKey: 's' };
// The production schedule is 400/1200/3000ms; sleeping it in three cases cost
// ~9s of a 16s suite and left thin margin on a loaded runner. Same code path.
const FAST_RETRIES = [1, 2, 3];

describe('useCurrentSessionCreds', () => {
  beforeEach(() => {
    fetchAuthSession.mockReset();
    resetSharedAuthSession();
    authStatusRef.current = 'authenticated';
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('reports credentials once they arrive', async () => {
    fetchAuthSession.mockResolvedValue({ credentials: CREDS });

    const { result } = renderHook(() => useCurrentSessionCreds({}));

    await waitFor(() => expect(result.current.credentialsStatus).toBe('ready'));
    expect(result.current.currentCredentials).toEqual(CREDS);
  });

  it('issues ONE fetch when several consumers mount together', async () => {
    // The root cause. The hook is mounted from three places, each fetching
    // unconditionally, and the duplicate call is the one Cognito rejects with
    // "Invalid login token" even though the token is fine.
    fetchAuthSession.mockResolvedValue({ credentials: CREDS });

    const a = renderHook(() => useCurrentSessionCreds({}));
    const b = renderHook(() => useCurrentSessionCreds({}));
    const c = renderHook(() => useCurrentSessionCreds({}));

    await waitFor(() => expect(a.result.current.credentialsStatus).toBe('ready'));
    await waitFor(() => expect(b.result.current.credentialsStatus).toBe('ready'));
    await waitFor(() => expect(c.result.current.credentialsStatus).toBe('ready'));

    expect(fetchAuthSession).toHaveBeenCalledTimes(1);
  });

  it('retries a transient failure instead of giving up for 15 minutes', async () => {
    // This alone makes the race invisible: the previous behaviour left
    // credentials undefined until the next interval tick.
    fetchAuthSession.mockRejectedValueOnce(new Error('NotAuthorizedException')).mockResolvedValue({ credentials: CREDS });

    const { result } = renderHook(() => useCurrentSessionCreds({ retryDelaysInMs: FAST_RETRIES }));

    await waitFor(() => expect(result.current.credentialsStatus).toBe('ready'));
    expect(result.current.currentCredentials).toEqual(CREDS);
    expect(fetchAuthSession.mock.calls.length).toBeGreaterThan(1);
  });

  it('surfaces an error state when every attempt fails', async () => {
    // Not silence. The caller renders an actionable message rather than nothing,
    // which is what the `// XXX surface credential refresh error` TODO cost.
    // FAST_RETRIES exercises the same loop without sleeping the real schedule.
    fetchAuthSession.mockRejectedValue(new Error('still failing'));

    const { result } = renderHook(() => useCurrentSessionCreds({ retryDelaysInMs: FAST_RETRIES }));

    await waitFor(() => expect(result.current.credentialsStatus).toBe('error'));
    expect(result.current.currentCredentials).toBeUndefined();
    // Every attempt was made: one per delay, plus the initial one.
    expect(fetchAuthSession).toHaveBeenCalledTimes(FAST_RETRIES.length + 1);
  });

  it('offers a retry that can recover without a reload', async () => {
    fetchAuthSession.mockRejectedValue(new Error('down'));
    const { result } = renderHook(() => useCurrentSessionCreds({ retryDelaysInMs: FAST_RETRIES }));
    await waitFor(() => expect(result.current.credentialsStatus).toBe('error'));

    fetchAuthSession.mockReset();
    fetchAuthSession.mockResolvedValue({ credentials: CREDS });
    await act(async () => {
      result.current.retryCredentials();
    });

    await waitFor(() => expect(result.current.credentialsStatus).toBe('ready'));
  });

  it('clears its refresh timer on unmount', async () => {
    // The handle used to live in a plain local, re-initialised to null every
    // render, so clearInterval never had anything to clear and every authStatus
    // transition leaked another 15-minute timer fetching forever.
    fetchAuthSession.mockResolvedValue({ credentials: CREDS });
    const clearSpy = vi.spyOn(globalThis, 'clearInterval');

    const { result, unmount } = renderHook(() => useCurrentSessionCreds({ credsIntervalInMs: 60_000 }));
    await waitFor(() => expect(result.current.credentialsStatus).toBe('ready'));
    clearSpy.mockClear();
    unmount();

    expect(clearSpy).toHaveBeenCalled();
    clearSpy.mockRestore();
  });

  it('drops credentials on sign-out', async () => {
    fetchAuthSession.mockResolvedValue({ credentials: CREDS });
    const { result, rerender } = renderHook(() => useCurrentSessionCreds({}));
    await waitFor(() => expect(result.current.credentialsStatus).toBe('ready'));

    authStatusRef.current = 'unauthenticated';
    rerender();

    await waitFor(() => expect(result.current.currentCredentials).toBeUndefined());
    expect(result.current.credentialsStatus).toBe('idle');
  });

  it('treats a session with no credentials as an error, not as ready', async () => {
    // Otherwise the app mounts against undefined credentials and fails later, in
    // a place with no explanation.
    fetchAuthSession.mockResolvedValue({ credentials: undefined });

    const { result } = renderHook(() => useCurrentSessionCreds({}));

    await waitFor(() => expect(result.current.credentialsStatus).toBe('error'));
  });
});
