// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The hook fetches the profile it is given and nothing else: a caller that does
 * not yet know which profile the signed-in user may read passes null, and no
 * request is made until it passes a name.
 *
 * When requests overlap, only the most recent one is applied: an earlier request
 * that answers later, with a configuration or a refusal, must not replace what
 * the caller asked for last.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';

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

const answer = (name: string) => ({
  data: {
    getConfigVersion: {
      success: true,
      Schema: JSON.stringify({ type: 'object', properties: {} }),
      Default: JSON.stringify({ profile: name }),
      Custom: JSON.stringify({}),
    },
  },
});

const refusal = (name: string) => ({
  data: {
    getConfigVersion: {
      success: false,
      error: { type: 'Unauthorized', message: `Access denied: version '${name}' is not in your allowed scope` },
    },
  },
});

const requestedNames = () => graphql.mock.calls.map(([args]) => args.variables.versionName);

const holdEveryRequest = () => {
  const held = new Map<string, (value: unknown) => void>();
  graphql.mockImplementation(
    ({ variables }: { variables: { versionName: string } }) => new Promise((resolve) => held.set(variables.versionName, resolve)),
  );
  return (name: string, value: unknown) =>
    act(async () => {
      held.get(name)!(value);
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
};

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

describe('useConfiguration — overlapping requests', () => {
  it('keeps the profile asked for last when an earlier request answers after it', async () => {
    const release = holdEveryRequest();
    const { result, rerender } = renderHook(({ name }: { name: string | null }) => useConfiguration(name), {
      initialProps: { name: 'default' as string | null },
    });
    rerender({ name: 'lending' });
    expect(requestedNames()).toEqual(['default', 'lending']);

    await release('lending', answer('lending'));
    expect(result.current.mergedConfig).toEqual({ profile: 'lending' });
    expect(result.current.loading).toBe(false);

    await release('default', answer('default'));
    expect(result.current.mergedConfig).toEqual({ profile: 'lending' });
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it('does not report the refusal of an earlier request once a later one has been made', async () => {
    const release = holdEveryRequest();
    const { result, rerender } = renderHook(({ name }: { name: string | null }) => useConfiguration(name), {
      initialProps: { name: 'default' as string | null },
    });
    rerender({ name: 'tenant-a_dev' });

    await release('default', refusal('default'));
    expect(result.current.error).toBeNull();
    expect(result.current.loading).toBe(true);

    await release('tenant-a_dev', answer('tenant-a_dev'));
    expect(result.current.error).toBeNull();
    expect(result.current.loading).toBe(false);
    expect(result.current.mergedConfig).toEqual({ profile: 'tenant-a_dev' });
  });

  it('reports the refusal of the request it made last', async () => {
    const release = holdEveryRequest();
    const { result, rerender } = renderHook(({ name }: { name: string | null }) => useConfiguration(name), {
      initialProps: { name: 'tenant-a_dev' as string | null },
    });
    rerender({ name: 'default' });

    await release('tenant-a_dev', answer('tenant-a_dev'));
    await release('default', refusal('default'));
    expect(result.current.error).toBe("Failed to load configuration: Access denied: version 'default' is not in your allowed scope");
    expect(result.current.loading).toBe(false);
  });

  it('clears both flags when the latest request settles, even though a superseded request set the other one', async () => {
    const release = holdEveryRequest();
    const { result } = renderHook(() => useConfiguration('default'));
    await act(async () => {
      void result.current.fetchConfiguration('lending', true);
    });
    expect(result.current.loading).toBe(true);
    expect(result.current.refreshing).toBe(true);

    await release('lending', answer('lending'));
    expect(result.current.loading).toBe(false);
    expect(result.current.refreshing).toBe(false);
    expect(result.current.mergedConfig).toEqual({ profile: 'lending' });

    await release('default', answer('default'));
    expect(result.current.loading).toBe(false);
    expect(result.current.refreshing).toBe(false);
    expect(result.current.mergedConfig).toEqual({ profile: 'lending' });
  });
});
