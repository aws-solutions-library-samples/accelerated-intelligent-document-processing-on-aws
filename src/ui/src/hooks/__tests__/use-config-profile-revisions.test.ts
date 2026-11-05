// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * `loadRevisions` keeps only the newest request's answer.
 *
 * The configuration editor reloads on every profile switch, and the warning it
 * shows is the answer's `profileMatchesPublished` rendered under the open
 * profile's name. If an older request resolved last, one profile's verdict would
 * be shown as another's.
 */

import { describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';

const graphql = vi.hoisted(() => vi.fn());
vi.mock('../../api/client-shim', () => ({ generateClient: () => ({ graphql }) }));

import useConfigProfileRevisions from '../use-config-profile-revisions';

const answer = (matches: boolean | null) => ({
  data: {
    listConfigProfileRevisions: {
      success: true,
      revisions: [{ revision: 1, published: true }],
      profileMatchesPublished: matches,
      error: null,
    },
  },
});

describe('useConfigProfileRevisions', () => {
  it('ignores an older request that resolves after a newer one', async () => {
    let resolveFirst: (v: unknown) => void = () => undefined;
    graphql
      .mockImplementationOnce(() => new Promise((resolve) => (resolveFirst = resolve)))
      .mockImplementationOnce(() => Promise.resolve(answer(true)));

    const { result } = renderHook(() => useConfigProfileRevisions());
    let first: Promise<void> = Promise.resolve();
    await act(async () => {
      first = result.current.loadRevisions('diverged-profile');
      await result.current.loadRevisions('matching-profile');
    });
    expect(result.current.profileMatchesPublished).toBe(true);

    await act(async () => {
      resolveFirst(answer(false));
      await first;
    });
    expect(result.current.profileMatchesPublished).toBe(true);
    expect(result.current.loading).toBe(false);
  });

  it('reports a mismatch from the newest request', async () => {
    graphql.mockImplementationOnce(() => Promise.resolve(answer(false)));
    const { result } = renderHook(() => useConfigProfileRevisions());
    await act(async () => {
      await result.current.loadRevisions('MyI9');
    });
    expect(result.current.profileMatchesPublished).toBe(false);
  });
});
