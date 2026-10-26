// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * A background refresh that never reaches the server does not raise a banner.
 *
 * The list re-fetches every few seconds. Error banners do not dismiss themselves, so
 * one dropped request ("Failed to fetch") left "Failed to list documents" on screen
 * after the next poll had succeeded. Only a run of such failures is reported now;
 * a load the user started, and any error the server answered, still report at once.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';

const { graphql, setErrorMessage, poll } = vi.hoisted(() => ({
  graphql: vi.fn(),
  setErrorMessage: vi.fn(),
  poll: { current: null as null | (() => void | Promise<void>) },
}));

vi.mock('../../api/client-shim', () => ({ generateClient: () => ({ graphql }) }));
vi.mock('../../contexts/app', () => ({ default: () => ({ setErrorMessage }) }));
// Capture the poll callback so each test fires ticks itself.
vi.mock('../use-polling', () => {
  const usePolling = (callback: () => void | Promise<void>) => {
    poll.current = callback;
  };
  return { default: usePolling, usePolling };
});
vi.mock('../../graphql/generated', () => ({
  listDocuments: 'listDocuments',
  getDocumentCount: 'getDocumentCount',
  getDocument: 'getDocument',
  deleteDocument: 'deleteDocument',
  reprocessDocument: 'reprocessDocument',
  abortWorkflow: 'abortWorkflow',
}));

import useGraphQlApi, { POLL_NETWORK_FAILURES_BEFORE_REPORT } from '../use-graphql-api';
import { LATEST_PERIODS } from '../../components/document-list/documents-table-config';

const OK = { data: { listDocuments: { Documents: [{ ObjectKey: 'a.pdf', PK: 'doc#a.pdf', SK: 'none' }], nextToken: null } } };
// The shape api/rest-client.ts throws when `fetch` itself rejects.
const NETWORK = { errors: [{ message: 'Failed to fetch', errorType: 'NetworkError' }] };
const SERVER = { errors: [{ message: 'boom', errorType: 'InternalError' }] };

const loaded = async () => {
  graphql.mockResolvedValue(OK);
  const hook = renderHook(() => useGraphQlApi({ initialPeriodsToLoad: LATEST_PERIODS }));
  await act(async () => {
    hook.result.current.setIsDocumentsListLoading(true);
  });
  await waitFor(() => expect(hook.result.current.isDocumentsListLoading).toBe(false));
  graphql.mockReset();
  return hook;
};

const tick = async () => {
  await act(async () => {
    await poll.current?.();
  });
};

beforeEach(() => {
  graphql.mockReset();
  setErrorMessage.mockReset();
  poll.current = null;
});

afterEach(() => cleanup());

describe('document list polling errors', () => {
  it('stays quiet for a poll that does not reach the server, and reports a run of them', async () => {
    await loaded();
    graphql.mockRejectedValue(NETWORK);

    for (let i = 1; i < POLL_NETWORK_FAILURES_BEFORE_REPORT; i += 1) await tick();
    expect(setErrorMessage).not.toHaveBeenCalled();

    await tick();
    expect(setErrorMessage).toHaveBeenCalledTimes(1);
  });

  it('starts counting again after a poll succeeds', async () => {
    await loaded();
    graphql.mockRejectedValueOnce(NETWORK).mockRejectedValueOnce(NETWORK).mockResolvedValueOnce(OK).mockRejectedValueOnce(NETWORK);

    for (let i = 0; i < 4; i += 1) await tick();

    expect(setErrorMessage).not.toHaveBeenCalled();
  });

  it('counts only consecutive network failures', async () => {
    await loaded();
    graphql
      .mockRejectedValueOnce(NETWORK)
      .mockRejectedValueOnce(NETWORK)
      .mockRejectedValueOnce(SERVER)
      .mockRejectedValueOnce(NETWORK)
      .mockRejectedValueOnce(NETWORK);

    for (let i = 0; i < 5; i += 1) await tick();

    // Only the server error is reported; the run of network failures restarted after it.
    expect(setErrorMessage).toHaveBeenCalledTimes(1);
  });

  it('reports a server error on a poll at once', async () => {
    await loaded();
    graphql.mockRejectedValue(SERVER);

    await tick();

    expect(setErrorMessage).toHaveBeenCalledTimes(1);
  });

  it('reports a network error on a load the user started at once', async () => {
    graphql.mockRejectedValue(NETWORK);
    const hook = renderHook(() => useGraphQlApi({ initialPeriodsToLoad: LATEST_PERIODS }));

    await act(async () => {
      hook.result.current.setIsDocumentsListLoading(true);
    });

    await waitFor(() => expect(setErrorMessage).toHaveBeenCalledTimes(1));
  });
});
