// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Tests for saveAsNewVersion's create-only contract.
 *
 * The backend's `saveAsVersion` path writes straight to the named profile, so a
 * name that is already taken silently REPLACES that profile's configuration with
 * the one being saved. Every caller here means "create a new profile", so the
 * name check belongs in the hook rather than in each modal — this is the second
 * layer behind the modals' inline validation.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';

// Hoisted: the hook creates its GraphQL client at module scope, so the mock
// factory runs before ordinary top-level consts are initialized.
const { graphql, updateConfiguration } = vi.hoisted(() => ({ graphql: vi.fn(), updateConfiguration: vi.fn() }));

vi.mock('../../api/client-shim', () => ({
  generateClient: () => ({ graphql }),
}));

vi.mock('../use-configuration', () => ({
  default: () => ({ updateConfiguration }),
}));

vi.mock('../use-user-role', () => ({
  default: () => ({ allowedConfigVersions: null }),
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
  { versionName: 'experiment', isActive: false },
];

beforeEach(() => {
  graphql.mockReset();
  updateConfiguration.mockReset();
  graphql.mockResolvedValue({ data: { getConfigVersions: { success: true, versions: VERSIONS } } });
  updateConfiguration.mockResolvedValue(true);
});

const renderVersions = async () => {
  const { result } = renderHook(() => useConfigurationVersions());
  await waitFor(() => expect(result.current.versions).toHaveLength(3));
  return result;
};

describe('saveAsNewVersion', () => {
  it('creates a profile whose name is not taken', async () => {
    const result = await renderVersions();
    const outcome = await result.current.saveAsNewVersion({ extraction: {} }, 'brand-new', 'desc');
    expect(outcome).toEqual({ success: true });
    expect(updateConfiguration).toHaveBeenCalledWith('brand-new', { extraction: {}, saveAsVersion: true }, 'desc');
  });

  it('refuses an existing non-active profile instead of overwriting it', async () => {
    const result = await renderVersions();
    const outcome = await result.current.saveAsNewVersion({ extraction: {} }, 'experiment', 'desc');
    expect(outcome.success).toBe(false);
    expect(outcome.error).toMatch(/already exists/);
    expect(updateConfiguration).not.toHaveBeenCalled();
  });

  it('refuses the active profile with an explanation of how to proceed', async () => {
    const result = await renderVersions();
    const outcome = await result.current.saveAsNewVersion({}, 'lending', 'desc');
    expect(outcome.success).toBe(false);
    expect(outcome.error).toMatch(/active version/);
    expect(updateConfiguration).not.toHaveBeenCalled();
  });

  it('refuses the reserved name "default"', async () => {
    const result = await renderVersions();
    const outcome = await result.current.saveAsNewVersion({}, 'default', 'desc');
    expect(outcome.success).toBe(false);
    expect(outcome.error).toMatch(/reserved/);
    expect(updateConfiguration).not.toHaveBeenCalled();
  });
});
