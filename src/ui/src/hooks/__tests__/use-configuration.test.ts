// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The hook fetches the profile it is given and nothing else: a caller that does
 * not yet know which profile the signed-in user may read passes null, and no
 * request is made until it passes a name.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';

const { graphql } = vi.hoisted(() => ({ graphql: vi.fn() }));

vi.mock('../../api/client-shim', () => ({
  generateClient: () => ({ graphql }),
}));

vi.mock('../../graphql/generated', () => ({
  getConfigVersion: 'getConfigVersion',
  updateConfiguration: 'updateConfiguration',
}));

import useConfiguration from '../use-configuration';

const PROFILE = {
  success: true,
  Schema: JSON.stringify({ type: 'object', properties: {} }),
  Default: JSON.stringify({ classes: [] }),
  Custom: JSON.stringify({}),
};

const requestedNames = () => graphql.mock.calls.map(([args]) => args.variables.versionName);

beforeEach(() => {
  graphql.mockReset();
  graphql.mockResolvedValue({ data: { getConfigVersion: PROFILE } });
});

describe('useConfiguration', () => {
  it('fetches nothing while it is given null', async () => {
    const { result } = renderHook(() => useConfiguration(null));
    await Promise.resolve();
    expect(graphql).not.toHaveBeenCalled();
    expect(result.current.loading).toBe(true);
    expect(result.current.error).toBeNull();
  });

  it('fetches the profile once it is given a name', async () => {
    const { result, rerender } = renderHook(({ name }: { name: string | null }) => useConfiguration(name), {
      initialProps: { name: null as string | null },
    });
    rerender({ name: 'tenant-a_dev' });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(requestedNames()).toEqual(['tenant-a_dev']);
    expect(result.current.mergedConfig).toEqual({ classes: [] });
  });
});
