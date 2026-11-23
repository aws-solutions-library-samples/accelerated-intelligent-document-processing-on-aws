// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * listReviewQueues is paged on the server. The helper Review Management, the queue
 * page and the queue editor share reads every page and returns one list.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }),
}));

import { listReviewQueues } from '../reviewer-assignment';

const queue = (id: string) => ({ id, name: id });
const page = (ids: string[], nextToken: string | null = null) => ({
  data: { listReviewQueues: { items: ids.map(queue), nextToken } },
});
const variablesOf = (call: number) => (graphql.mock.calls[call][0] as { variables: Record<string, unknown> }).variables;

describe('listReviewQueues', () => {
  beforeEach(() => {
    graphql.mockReset();
  });

  it('returns the queues of a single page with one request', async () => {
    graphql.mockResolvedValueOnce(page(['default', 'claims']));

    const queues = await listReviewQueues();

    expect(queues.map((q) => q.id)).toEqual(['default', 'claims']);
    expect(graphql).toHaveBeenCalledTimes(1);
    expect(variablesOf(0).nextToken).toBeUndefined();
  });

  it('follows nextToken until the last page and keeps the order', async () => {
    graphql
      .mockResolvedValueOnce(page(['a', 'b'], 'token-1'))
      .mockResolvedValueOnce(page(['c'], 'token-2'))
      .mockResolvedValueOnce(page(['d']));

    const queues = await listReviewQueues();

    expect(queues.map((q) => q.id)).toEqual(['a', 'b', 'c', 'd']);
    expect(graphql).toHaveBeenCalledTimes(3);
    expect(variablesOf(1).nextToken).toBe('token-1');
    expect(variablesOf(2).nextToken).toBe('token-2');
  });

  it('asks for the largest page the resolver serves', async () => {
    graphql.mockResolvedValueOnce(page([]));

    await listReviewQueues();

    expect(variablesOf(0).limit).toBe(100);
  });

  it('returns an empty list when there are no queues', async () => {
    graphql.mockResolvedValueOnce(page([]));

    expect(await listReviewQueues()).toEqual([]);
  });

  it('lets a failed page fail the whole read rather than returning part of the list', async () => {
    graphql.mockResolvedValueOnce(page(['a'], 'token-1')).mockRejectedValueOnce(new Error('boom'));

    await expect(listReviewQueues()).rejects.toThrow('boom');
  });
});
