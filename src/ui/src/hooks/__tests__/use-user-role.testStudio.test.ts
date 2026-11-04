// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Test Studio role flags.
 *
 * A Reviewer works Production review queues only. Test Studio ground truth is
 * annotated by Admins, Authors and Annotators (`canAnnotate`), and test-set queues
 * are claimed and worked by Admins and Annotators (`canWorkTestSetQueues`). A
 * Reviewer who also holds one of those roles keeps that role's access. The server
 * enforces the same split; these flags only keep the UI from offering work it
 * would refuse.
 */

import { renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const fetchSharedAuthSession = vi.fn();
const graphql = vi.fn();

vi.mock('../../api/auth-session', () => ({
  fetchSharedAuthSession: (...args: unknown[]) => fetchSharedAuthSession(...args),
}));

vi.mock('../../api/client-shim', () => ({
  generateClient: () => ({ graphql: (...args: unknown[]) => graphql(...args) }),
}));

// Imported after the mocks, which vitest hoists.
import useUserRole, { resetSharedProfileScope } from '../use-user-role';

const sessionWithGroups = (groups: string[]) => ({
  tokens: { idToken: { payload: { 'cognito:groups': groups, sub: 'user-1' } } },
});

describe('useUserRole Test Studio flags', () => {
  beforeEach(() => {
    fetchSharedAuthSession.mockReset();
    graphql.mockReset();
    resetSharedProfileScope();
    graphql.mockResolvedValue({ data: { getMyProfile: { allowedConfigVersions: [], allowedTestSets: [] } } });
  });

  it.each([
    [['Admin'], true, true],
    [['Author'], true, false],
    [['Annotator'], true, true],
    [['Reviewer'], false, false],
    [['Viewer'], false, false],
    [['Reviewer', 'Author'], true, false],
    [['Reviewer', 'Annotator'], true, true],
  ])('%j: canAnnotate=%s, canWorkTestSetQueues=%s', async (groups, canAnnotate, canWorkTestSetQueues) => {
    fetchSharedAuthSession.mockResolvedValue(sessionWithGroups(groups));

    const { result } = renderHook(() => useUserRole());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.canAnnotate).toBe(canAnnotate);
    expect(result.current.canWorkTestSetQueues).toBe(canWorkTestSetQueues);
  });

  it('leaves a Reviewer-only user a Production queue worker', async () => {
    fetchSharedAuthSession.mockResolvedValue(sessionWithGroups(['Reviewer']));

    const { result } = renderHook(() => useUserRole());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.isReviewerOnly).toBe(true);
    expect(result.current.isQueueWorker).toBe(true);
    expect(result.current.canWorkReviewQueues).toBe(true);
  });
});
