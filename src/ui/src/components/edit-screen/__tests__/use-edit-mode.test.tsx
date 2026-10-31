// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The reads behind the edit page's mode. What has to hold: a document that is not
 * queue-managed costs one read (getDocument) and no item read; a queue-managed one read
 * by a caller who works queues adds exactly one getReviewItem; a caller who does not
 * work queues never reads the item; nothing is read until the role is known; and Retry
 * reads again.
 */

import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
const fetchReviewItem = vi.fn();
const role = { loading: false, canWorkReviewQueues: true, isAnnotatorOnly: false };
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }) }));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => role }));
vi.mock('../../review-queues/review-api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../review-queues/review-api')>()),
  fetchReviewItem: (...a: unknown[]) => fetchReviewItem(...a),
}));

import useEditMode from '../use-edit-mode';

const documentWith = (HITLStatus: string | null) => ({ data: { getDocument: { ObjectKey: 'a/b.pdf', HITLStatus } } });

beforeEach(() => {
  graphql.mockReset();
  fetchReviewItem.mockReset().mockResolvedValue({ item: { objectKey: 'a/b.pdf', state: 'OPEN' }, history: [] });
  Object.assign(role, { loading: false, canWorkReviewQueues: true, isAnnotatorOnly: false });
});

describe('the edit page’s mode reads', () => {
  it('reads the document once and no item when it is not queue-managed', async () => {
    graphql.mockResolvedValue(documentWith(null));
    const { result } = renderHook(() => useEditMode('a/b.pdf'));
    await waitFor(() => expect(result.current.status).toBe('resolved'));
    expect(graphql).toHaveBeenCalledTimes(1);
    expect(fetchReviewItem).not.toHaveBeenCalled();
    expect(result.current).toMatchObject({ decision: { mode: 'direct' } });
  });

  it('reads the document, then the item once, for an Admin on a queue-managed document', async () => {
    graphql.mockResolvedValue(documentWith('in_review'));
    const { result } = renderHook(() => useEditMode('a/b.pdf'));
    await waitFor(() => expect(result.current.status).toBe('resolved'));
    expect(graphql).toHaveBeenCalledTimes(1);
    expect(fetchReviewItem).toHaveBeenCalledTimes(1);
    expect(fetchReviewItem).toHaveBeenCalledWith('a/b.pdf');
    expect(graphql.mock.invocationCallOrder[0]).toBeLessThan(fetchReviewItem.mock.invocationCallOrder[0]);
    expect(result.current).toMatchObject({ decision: { mode: 'queue' } });
  });

  it('never reads the item for an Author', async () => {
    role.canWorkReviewQueues = false;
    graphql.mockResolvedValue(documentWith('in_review'));
    const { result } = renderHook(() => useEditMode('a/b.pdf'));
    await waitFor(() => expect(result.current.status).toBe('resolved'));
    expect(fetchReviewItem).not.toHaveBeenCalled();
    expect(result.current).toMatchObject({ decision: { mode: 'direct', lock: 'in-review-elsewhere' } });
  });

  it('waits for the role before reading', async () => {
    role.loading = true;
    graphql.mockResolvedValue(documentWith(null));
    const { result, rerender } = renderHook(() => useEditMode('a/b.pdf'));
    await act(async () => Promise.resolve());
    expect(graphql).not.toHaveBeenCalled();
    expect(result.current.status).toBe('loading');
    role.loading = false;
    rerender();
    await waitFor(() => expect(result.current.status).toBe('resolved'));
    expect(graphql).toHaveBeenCalledTimes(1);
  });

  it('reports a missing document, and a failed read with Retry that reads again', async () => {
    graphql.mockResolvedValueOnce({ data: { getDocument: null } });
    const missing = renderHook(() => useEditMode('a/b.pdf'));
    await waitFor(() => expect(missing.result.current.status).toBe('document-missing'));
    missing.unmount();

    graphql.mockReset().mockRejectedValueOnce(new Error('boom')).mockResolvedValueOnce(documentWith(null));
    const { result } = renderHook(() => useEditMode('a/b.pdf'));
    await waitFor(() => expect(result.current.status).toBe('document-failed'));
    act(() => (result.current as { retry: () => void }).retry());
    await waitFor(() => expect(result.current.status).toBe('resolved'));
    expect(graphql).toHaveBeenCalledTimes(2);
  });
});
