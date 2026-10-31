// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Which profile a page may fetch, and which one it opens, for a caller whose
 * Configuration-Profile scope may exclude `default`.
 */

import { describe, expect, it } from 'vitest';

import { chooseOpeningProfile, isProfileReadable } from '../readable-profile';

const UNRESTRICTED = { allowedConfigVersions: null, loading: false };
const SCOPED = { allowedConfigVersions: ['tenant-a_*'], loading: false };
const SCOPE_UNREAD = { allowedConfigVersions: null, loading: true };

const SCOPED_LIST = [{ versionName: 'tenant-a_dev' }, { versionName: 'tenant-a_prod' }];

describe('isProfileReadable', () => {
  it('lets an unrestricted caller read any profile once their scope is read', () => {
    expect(isProfileReadable('default', [], UNRESTRICTED)).toBe(true);
    expect(isProfileReadable('lending', [], UNRESTRICTED)).toBe(true);
  });

  it('lets a restricted caller read only what their scope admits', () => {
    expect(isProfileReadable('default', [], SCOPED)).toBe(false);
    expect(isProfileReadable('tenant-b_prod', [], SCOPED)).toBe(false);
    expect(isProfileReadable('tenant-a_prod', [], SCOPED)).toBe(true);
  });

  it('answers not yet while the scope is unread, unless the profile is in the list', () => {
    expect(isProfileReadable('default', [], SCOPE_UNREAD)).toBe(false);
    expect(isProfileReadable('tenant-a_dev', SCOPED_LIST, SCOPE_UNREAD)).toBe(true);
  });

  it('never reads a missing name', () => {
    expect(isProfileReadable(null, SCOPED_LIST, UNRESTRICTED)).toBe(false);
    expect(isProfileReadable('', SCOPED_LIST, UNRESTRICTED)).toBe(false);
  });
});

describe('chooseOpeningProfile', () => {
  const list = (versions: { versionName: string; isActive?: boolean }[], loaded = true, error: string | null = null) => ({
    versions,
    loaded,
    loading: !loaded,
    error,
  });

  it('opens the active profile when it is in the list', () => {
    const versions = [{ versionName: 'tenant-a_dev' }, { versionName: 'tenant-a_prod', isActive: true }];
    expect(chooseOpeningProfile(list(versions), SCOPED)).toEqual({ status: 'ready', versionName: 'tenant-a_prod' });
  });

  it('opens the first profile in the list when the active one is out of scope', () => {
    expect(chooseOpeningProfile(list(SCOPED_LIST), SCOPED)).toEqual({ status: 'ready', versionName: 'tenant-a_dev' });
  });

  it('waits for the list of a restricted caller instead of falling back to default', () => {
    expect(chooseOpeningProfile(list([], false), SCOPED)).toEqual({ status: 'loading' });
  });

  it('waits while the scope of the caller is unread', () => {
    expect(chooseOpeningProfile(list([], false), SCOPE_UNREAD)).toEqual({ status: 'loading' });
    expect(chooseOpeningProfile(list([], true), SCOPE_UNREAD)).toEqual({ status: 'loading' });
  });

  it('says none when the list of a restricted caller has answered empty', () => {
    expect(chooseOpeningProfile(list([]), SCOPED)).toEqual({ status: 'none' });
  });

  it('waits for a restricted caller while a retried list request is in flight', () => {
    expect(chooseOpeningProfile({ versions: [], loaded: true, loading: true, error: null }, SCOPED)).toEqual({ status: 'loading' });
  });

  it('reports a failed list for a restricted caller as an error, not as an empty scope', () => {
    expect(chooseOpeningProfile(list([], true, 'Failed to fetch versions'), SCOPED)).toEqual({
      status: 'error',
      message: 'Failed to fetch versions',
    });
  });

  it('keeps default for an unrestricted caller whose list is loading, empty or failed', () => {
    const ready = { status: 'ready', versionName: 'default' };
    expect(chooseOpeningProfile(list([], false), UNRESTRICTED)).toEqual(ready);
    expect(chooseOpeningProfile(list([]), UNRESTRICTED)).toEqual(ready);
    expect(chooseOpeningProfile(list([], true, 'Failed to fetch versions'), UNRESTRICTED)).toEqual(ready);
  });

  it('opens the active profile for an unrestricted caller once the list answers', () => {
    const versions = [{ versionName: 'default' }, { versionName: 'lending', isActive: true }];
    expect(chooseOpeningProfile(list(versions), UNRESTRICTED)).toEqual({ status: 'ready', versionName: 'lending' });
  });
});
