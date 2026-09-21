// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Tests for how the hook applies the caller's Configuration-Profile scope.
 *
 * `allowedConfigVersions` entries may be exact profile names or glob patterns —
 * the server's `scope_allows` honours both. This hook produces every
 * Configuration-Profile picklist in the UI, so an exact comparison here shows a
 * caller scoped to `tenant-a_*` an EMPTY list: the reprocess modal then disables
 * its own button for want of a selection, and that caller cannot reprocess
 * anything the server would happily have reprocessed for them.
 *
 * The matcher itself is tested in `utils/__tests__/config-scope.test.ts`; what
 * these cases pin down is that this hook consults it rather than comparing
 * strings, and that the picklist a component reads (`getVersionOptions`) is
 * filtered the same way as `versions`.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';

// Hoisted: the hook creates its GraphQL client at module scope, so the mock
// factory runs before ordinary top-level consts are initialized. `scope` is a
// mutable holder rather than a fixed value so each case can pick a scope.
const { graphql, updateConfiguration, scope } = vi.hoisted(() => ({
  graphql: vi.fn(),
  updateConfiguration: vi.fn(),
  scope: { value: null as string[] | null },
}));

vi.mock('../../api/client-shim', () => ({
  generateClient: () => ({ graphql }),
}));

vi.mock('../use-configuration', () => ({
  default: () => ({ updateConfiguration }),
}));

vi.mock('../use-user-role', () => ({
  default: () => ({ allowedConfigVersions: scope.value }),
}));

vi.mock('../../graphql/generated', () => ({
  getConfigVersions: 'getConfigVersions',
  getConfigVersion: 'getConfigVersion',
  setActiveVersion: 'setActiveVersion',
  deleteConfigVersion: 'deleteConfigVersion',
}));

import useConfigurationVersions from '../use-configuration-versions';

const VERSIONS = [
  { versionName: 'default', isActive: false, managed: true },
  { versionName: 'lending', isActive: true },
  { versionName: 'tenant-a_prod', isActive: false },
  { versionName: 'tenant-a_dev', isActive: false },
  { versionName: 'tenant-b_prod', isActive: false },
];

beforeEach(() => {
  graphql.mockReset();
  updateConfiguration.mockReset();
  scope.value = null;
  graphql.mockResolvedValue({ data: { getConfigVersions: { success: true, versions: VERSIONS } } });
  updateConfiguration.mockResolvedValue(true);
});

/** Render the hook and wait for the initial fetch to settle. */
const renderScoped = async (allowed: string[] | null, expectedCount: number) => {
  scope.value = allowed;
  const { result } = renderHook(() => useConfigurationVersions());
  await waitFor(() => expect(result.current.loading).toBe(false));
  await waitFor(() => expect(result.current.versions).toHaveLength(expectedCount));
  return result;
};

const names = (result: { current: { versions: { versionName: string }[] } }) => result.current.versions.map((v) => v.versionName);

describe('useConfigurationVersions — config-version scope', () => {
  it('offers every profile when the caller is unrestricted', async () => {
    const result = await renderScoped(null, 5);
    expect(names(result)).toEqual(VERSIONS.map((v) => v.versionName));
  });

  it('gives a caller scoped to a glob a non-empty list of the profiles it covers', async () => {
    const result = await renderScoped(['tenant-a_*'], 2);
    expect(names(result).sort()).toEqual(['tenant-a_dev', 'tenant-a_prod']);
    // The picklist the reprocess modal reads must be non-empty too, or its
    // Reprocess button stays disabled for want of a selectable profile.
    const offered = result.current.getVersionOptions().map((o) => o.value);
    expect(offered.sort()).toEqual(['tenant-a_dev', 'tenant-a_prod']);
  });

  it('honours ? and [seq] patterns as well as *', async () => {
    const single = await renderScoped(['tenant-?_prod'], 2);
    expect(names(single).sort()).toEqual(['tenant-a_prod', 'tenant-b_prod']);

    const klass = await renderScoped(['tenant-[b]_*'], 1);
    expect(names(klass)).toEqual(['tenant-b_prod']);
  });

  it('still applies an exact entry exactly', async () => {
    const result = await renderScoped(['lending'], 1);
    expect(names(result)).toEqual(['lending']);
  });

  it('applies a mix of exact entries and patterns', async () => {
    const result = await renderScoped(['lending', 'tenant-b_*'], 2);
    expect(names(result).sort()).toEqual(['lending', 'tenant-b_prod']);
  });

  it('shows nothing when the scope covers none of the profiles that exist', async () => {
    // Genuinely out of scope, which is not the same as the pattern bug: here the
    // server would refuse these profiles too.
    const result = await renderScoped(['tenant-c_*'], 0);
    expect(result.current.getVersionOptions()).toEqual([]);
  });
});
