// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Which Configuration Profiles the UI may fetch for the signed-in caller, and
 * which one a page opens when nothing names one.
 *
 * `getConfigVersion` refuses a profile outside the caller's
 * `allowedConfigVersions`, so a page must not ask for one before it knows the
 * caller may read it. Either of two things establishes that: the profile is in
 * the caller's list, which the server has already filtered by their scope, or
 * the caller's scope has been read and `scopeAllows` admits the profile. Until
 * one holds the answer is "not yet", not "no".
 */

import { isScopeRestricted, scopeAllows, type ConfigVersionScope } from './config-scope';

/** The part of a profile list entry these functions read. */
interface ListedProfile {
  versionName: string;
  isActive?: boolean | null;
}

/** What `useConfigurationVersions` reports about the caller's profiles. */
export interface ProfileList {
  /** The caller's profiles, already filtered by their scope. */
  versions: readonly ListedProfile[];
  /** True once the first list request has settled, whether or not it succeeded. */
  loaded: boolean;
  /** True while a list request is in flight. */
  loading: boolean;
  /** The last list request's error, if it failed. */
  error: string | null;
}

/** The part of `useUserRole` these functions read. */
export interface CallerScope {
  allowedConfigVersions: ConfigVersionScope;
  /** True until the caller's scope has been read. */
  loading: boolean;
}

/** Whether the caller is known to be allowed to read a profile. */
export const isProfileReadable = (
  versionName: string | null | undefined,
  versions: readonly ListedProfile[],
  { allowedConfigVersions, loading }: CallerScope,
): boolean =>
  Boolean(versionName) &&
  (versions.some((v) => v.versionName === versionName) || (!loading && scopeAllows(allowedConfigVersions, versionName)));

/** The profile a page should open, or why it cannot open one yet. */
export type OpeningProfile =
  | { status: 'loading' }
  | { status: 'ready'; versionName: string }
  | { status: 'none' }
  | { status: 'error'; message: string };

/**
 * The profile a page opens when the URL names none.
 *
 * The active profile when it is in the caller's list, otherwise the list's first
 * entry. With an empty list an unrestricted caller gets `default`; a restricted
 * caller waits for the list and then gets `none`, or `error` when the list could
 * not be read, rather than a profile the server would refuse.
 */
export const chooseOpeningProfile = (list: ProfileList, scope: CallerScope): OpeningProfile => {
  if (list.versions.length > 0) {
    const active = list.versions.find((v) => v.isActive);
    return { status: 'ready', versionName: (active ?? list.versions[0]).versionName };
  }
  if (scope.loading) return { status: 'loading' };
  if (!isScopeRestricted(scope.allowedConfigVersions)) return { status: 'ready', versionName: 'default' };
  if (!list.loaded || list.loading) return { status: 'loading' };
  if (list.error) return { status: 'error', message: list.error };
  return { status: 'none' };
};
