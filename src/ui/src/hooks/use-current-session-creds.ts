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
  // Guards against a late response from a previous auth state overwriting the
  // current one after sign-out.
  const mountedRef = useRef(true);

  const refreshCredentials = useCallback(async (): Promise<void> => {
    setCredentialsStatus((prev) => (prev === 'ready' ? prev : 'pending'));

    // One more attempt than there are delays: the delays sit *between* attempts.
    for (let attempt = 0; attempt <= retryDelaysInMs.length; attempt += 1) {
      try {
        const session = await fetchSharedAuthSession();
        if (!mountedRef.current) return;
        setCurrentSession(session);
        setCurrentCredentials(session.credentials);
        setCredentialsStatus(session.credentials ? 'ready' : 'error');
        logger.debug('successfully refreshed credentials');
        return;
      } catch (error) {
        if (!mountedRef.current) return;
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
        if (!mountedRef.current) return;
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
      clearRefreshInterval();
      refreshCredentials();
      intervalRef.current = setInterval(refreshCredentials, credsIntervalInMs);
    } else {
      clearRefreshInterval();
    }
    if (authStatus === 'unauthenticated') {
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
