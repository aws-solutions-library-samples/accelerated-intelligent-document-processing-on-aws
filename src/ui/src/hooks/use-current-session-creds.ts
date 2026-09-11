// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { useCallback, useEffect, useRef, useState } from 'react';

import { ConsoleLogger } from 'aws-amplify/utils';
import { useAuthenticator } from '@aws-amplify/ui-react';

import { fetchSharedAuthSession } from '../api/auth-session';

const DEFAULT_CREDS_REFRESH_INTERVAL_IN_MS = 60 * 15 * 1000;

/**
 * Retry schedule for a failed credential fetch, in milliseconds.
 *
 * Without this a single failure left `currentCredentials` undefined until the
 * next 15-minute tick, and because the whole authenticated app is gated on those
 * credentials, the user got a blank page for 15 minutes after a *successful*
 * sign-in. Seconds of retry make the race invisible; the alternative is a dead
 * end with no on-screen explanation.
 */
const RETRY_DELAYS_IN_MS = [400, 1200, 3000];

const logger = new ConsoleLogger('useCurrentSessionCreds');

export type CredentialsStatus = 'idle' | 'pending' | 'ready' | 'error';

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

const useCurrentSessionCreds = ({
  credsIntervalInMs = DEFAULT_CREDS_REFRESH_INTERVAL_IN_MS,
  // Injectable so tests can exercise the exhaust-all-retries path without
  // actually sleeping the real schedule (~4.6s per case).
  //
  // MUST be a stable reference — a module constant, or memoized. It lands in
  // `refreshCredentials`' dep list, which lands in the auth effect's dep list,
  // which bumps `cycleRef`. Pass an inline array literal and its identity
  // changes every render: the effect re-runs, the cycle bumps, and each render
  // cancels the fetch the previous one started, so credentials never resolve.
  retryDelaysInMs = RETRY_DELAYS_IN_MS,
}: {
  credsIntervalInMs?: number;
  retryDelaysInMs?: number[];
}): {
  currentSession: unknown;
  currentCredentials: unknown;
  /**
   * Lets a caller tell "authenticated, credentials still arriving" apart from
   * "not authenticated". Routes.tsx treated those as the same thing, which is
   * what turned a transient failure into a blank page.
   */
  credentialsStatus: CredentialsStatus;
  /** Retry now, for an on-screen recovery action. */
  retryCredentials: () => void;
} => {
  const { authStatus } = useAuthenticator((context) => [context.authStatus]);
  const [currentSession, setCurrentSession] = useState<unknown>();
  const [currentCredentials, setCurrentCredentials] = useState<unknown>();
  const [credentialsStatus, setCredentialsStatus] = useState<CredentialsStatus>('idle');
  // A ref, not a local: as a local it was re-initialised to null on every
  // render, so `if (!interval)` was always true, the else branch was dead, and
  // clearInterval never had a handle to clear — leaking one 15-minute timer per
  // authStatus transition, each fetching forever.
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // Set false on component UNMOUNT to guard synchronous state writes from
  // in-flight promises. Sign-out is handled instead by the cycleRef bump
  // below — flipping mountedRef on 'unauthenticated' would break the
  // subsequent authenticated remount (mountedRef stays false).
  const mountedRef = useRef(true);
  // Bumps on every fresh authenticated cycle AND on authStatus flipping
  // to 'unauthenticated' — the refresh loop captures its start value
  // and bails when the ref moves. Same pattern React Query uses for
  // cancellation. Round-7 review fix: unmount alone left a signed-out
  // user racing with a still-in-flight fetchSharedAuthSession that
  // would overwrite the just-cleared state, and its retry loop would
  // keep hitting Cognito after signout.
  const cycleRef = useRef(0);

  const refreshCredentials = useCallback(async (): Promise<void> => {
    const cycle = cycleRef.current;
    setCredentialsStatus((prev) => (prev === 'ready' ? prev : 'pending'));

    // One more attempt than there are delays: the delays sit *between* attempts.
    for (let attempt = 0; attempt <= retryDelaysInMs.length; attempt += 1) {
      try {
        const session = await fetchSharedAuthSession();
        // Bail if the component unmounted OR the auth cycle changed while
        // the request was in flight — writing this session into state now
        // would restore a previous user's credentials over just-cleared
        // state, or reset a fresh cycle's just-issued credentials.
        if (!mountedRef.current || cycleRef.current !== cycle) return;
        setCurrentSession(session);
        setCurrentCredentials(session.credentials);
        setCredentialsStatus(session.credentials ? 'ready' : 'error');
        logger.debug('successfully refreshed credentials');
        return;
      } catch (error) {
        if (!mountedRef.current || cycleRef.current !== cycle) return;
        const delay = retryDelaysInMs[attempt];
        if (delay === undefined) {
          // Out of attempts. Surfaced rather than only logged (this is the
          // `// XXX surface credential refresh error` that used to live here):
          // the caller renders an actionable error instead of nothing.
          logger.error('failed to get credentials after retries', error);
          setCredentialsStatus('error');
          return;
        }
        logger.warn(`credential fetch failed, retrying in ${delay}ms`, error);
        await sleep(delay);
        // Re-check AFTER the sleep — authStatus may have flipped to
        // 'unauthenticated' during the delay, and we must not issue
        // another Cognito call post-signout.
        if (!mountedRef.current || cycleRef.current !== cycle) return;
      }
    }
  }, [retryDelaysInMs]);

  const clearRefreshInterval = useCallback(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (authStatus === 'authenticated') {
      // Bump cycleRef so any in-flight refresh from the previous auth
      // cycle bails when it resolves (see `refreshCredentials`).
      cycleRef.current += 1;
      clearRefreshInterval();
      refreshCredentials();
      intervalRef.current = setInterval(refreshCredentials, credsIntervalInMs);
    } else {
      clearRefreshInterval();
    }
    if (authStatus === 'unauthenticated') {
      // Bump cycleRef so in-flight refreshes from the previous
      // (authenticated) cycle bail before they overwrite the cleared
      // state. Without this, a delayed fetchSharedAuthSession resolving
      // after the state clear would write the signed-out user's session
      // right back into state. Round-7 review fix.
      cycleRef.current += 1;
      setCurrentSession(undefined);
      setCurrentCredentials(undefined);
      setCredentialsStatus('idle');
    }

    return () => {
      clearRefreshInterval();
    };
  }, [authStatus, credsIntervalInMs, clearRefreshInterval, refreshCredentials]);

  return { currentSession, currentCredentials, credentialsStatus, retryCredentials: refreshCredentials };
};

export default useCurrentSessionCreds;
