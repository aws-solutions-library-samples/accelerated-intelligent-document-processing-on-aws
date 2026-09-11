// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * One `fetchAuthSession` for the whole app.
 *
 * Concurrent identical `GetCredentialsForIdentity` calls race at Cognito, and the
 * loser comes back `400 NotAuthorizedException: Invalid login token` — with a
 * perfectly valid token. Observed live: two byte-identical requests in the same
 * second, one 200, one 400, and the app stranded on a blank page because the
 * rejected one was the call it was waiting on.
 *
 * Duplicates are easy to create here without meaning to. `useCurrentSessionCreds`
 * is mounted from three places, `useUserRole` from roughly fifteen components,
 * and seven modules call `fetchAuthSession` directly — each with its own
 * on-mount effect. React StrictMode doubles every one of those in development.
 *
 * So every caller shares one in-flight promise. De-duplicating in a single hook
 * would only reduce the race; this removes it.
 */

import { fetchAuthSession } from 'aws-amplify/auth';

type AuthSession = Awaited<ReturnType<typeof fetchAuthSession>>;

/**
 * Two slots, keyed on `forceRefresh`.
 *
 * A forced refresh must never be satisfied by a pending *unforced* call — the
 * caller is asking precisely because it believes the cached session is stale
 * (`useUserRole` does this after a group change). Concurrent forced calls may
 * legitimately share, since they all want the same fresh result.
 */
const inFlight: { normal: Promise<AuthSession> | null; forced: Promise<AuthSession> | null } = {
  normal: null,
  forced: null,
};

/**
 * `fetchAuthSession`, de-duplicated across the app.
 *
 * Drop-in: same signature and same return value, so a caller only has to change
 * its import.
 */
export const fetchSharedAuthSession = (options?: { forceRefresh?: boolean }): Promise<AuthSession> => {
  const slot = options?.forceRefresh ? 'forced' : 'normal';
  if (!inFlight[slot]) {
    inFlight[slot] = fetchAuthSession(options).finally(() => {
      inFlight[slot] = null;
    });
  }
  return inFlight[slot] as Promise<AuthSession>;
};

/** Exposed for tests: drop any shared promise so cases start from a clean slate. */
export const resetSharedAuthSession = (): void => {
  inFlight.normal = null;
  inFlight.forced = null;
};
