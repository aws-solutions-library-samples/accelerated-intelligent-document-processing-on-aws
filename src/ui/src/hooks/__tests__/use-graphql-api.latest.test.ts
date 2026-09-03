// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Tests for the document list's Latest option and its pagination stop conditions.
 *
 * Latest is unbounded in time, so unlike the windowed options it cannot just drain
 * to the end of the partition — it needs a stop condition. The subtle part is that
 * the resolver applies the reviewer RBAC FilterExpression and the config-version
 * scope filter AFTER DynamoDB's Limit, so a requested page of 200 can come back
 * with 3 rows. Counting requests instead of delivered rows would hand a scoped user
 * a near-empty list and present it as "the latest 200", which is the exact
 * filter-after-limit trap the ItemType partitioning was introduced to avoid.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';

// Hoisted: the hook builds its GraphQL client at module scope, before ordinary
// top-level consts in this file are initialized.
const { graphql, setErrorMessage } = vi.hoisted(() => ({ graphql: vi.fn(), setErrorMessage: vi.fn() }));

vi.mock('../../api/client-shim', () => ({ generateClient: () => ({ graphql }) }));
vi.mock('../../contexts/app', () => ({ default: () => ({ setErrorMessage }) }));
// The 5s poll would re-enter the fetch mid-assertion; the loop under test is
// exercised directly instead.
vi.mock('../use-polling', () => ({ default: () => undefined, usePolling: () => undefined }));
vi.mock('../../graphql/generated', () => ({
  listDocuments: 'listDocuments',
  getDocumentCount: 'getDocumentCount',
  getDocument: 'getDocument',
  deleteDocument: 'deleteDocument',
  reprocessDocument: 'reprocessDocument',
  abortWorkflow: 'abortWorkflow',
}));

import useGraphQlApi from '../use-graphql-api';
import { DOCUMENT_LIST_SHARDS_PER_DAY, LATEST_PERIODS } from '../../components/document-list/documents-table-config';

/** A page of `count` documents with unique keys, plus an optional continuation. */
const page = (count: number, offset: number, nextToken: string | null) => ({
  data: {
    listDocuments: {
      Documents: Array.from({ length: count }, (_, i) => ({
        ObjectKey: `doc-${offset + i}.pdf`,
        PK: `doc#doc-${offset + i}.pdf`,
        SK: 'none',
      })),
      nextToken,
    },
  },
});

/** Every page carries `count` rows and always claims there is more to come. */
const endlessPages = (count: number) => {
  let served = 0;
  return () => {
    const result = page(count, served, `token-${served}`);
    served += count;
    return Promise.resolve(result);
  };
};

const variablesOf = (call: number) => graphql.mock.calls[call][0].variables;

const load = async (periods: number) => {
  const hook = renderHook(() => useGraphQlApi({ initialPeriodsToLoad: periods }));
  await act(async () => {
    hook.result.current.setIsDocumentsListLoading(true);
  });
  await waitFor(() => expect(graphql).toHaveBeenCalled());
  await waitFor(() => expect(hook.result.current.isDocumentsListLoading).toBe(false));
  return hook;
};

beforeEach(() => {
  graphql.mockReset();
  setErrorMessage.mockReset();
});

// Unmount between tests: a hook still paging when a test ends would keep consuming
// the next test's mockResolvedValueOnce queue and make failures look unrelated.
afterEach(() => {
  cleanup();
});

describe('Latest', () => {
  it('sends no date bounds, so the GSI query is newest-first over the whole partition', async () => {
    graphql.mockResolvedValue(page(5, 0, null));

    await load(LATEST_PERIODS);

    expect(variablesOf(0)).not.toHaveProperty('startDateTime');
    expect(variablesOf(0)).not.toHaveProperty('endDateTime');
    expect(variablesOf(0)).toMatchObject({ limit: 200, view: 'PRODUCTION' });
  });

  it('stops once the target row count is delivered instead of draining the partition', async () => {
    graphql.mockImplementation(endlessPages(200));

    const hook = await load(LATEST_PERIODS);

    // One full page satisfies the 200 target; without the stop condition this would
    // page until the partition ran out.
    expect(graphql).toHaveBeenCalledTimes(1);
    expect(hook.result.current.documents).toHaveLength(200);
  });

  it('keeps paging when server-side filtering thins each page, rather than stopping short at one', async () => {
    // 3 survivors per requested 200 — a config-version-scoped or reviewer-scoped
    // caller. Counting pages instead of rows would end here with 3 documents.
    graphql.mockImplementation(endlessPages(3));

    const hook = await load(LATEST_PERIODS);

    expect(graphql).toHaveBeenCalledTimes(10); // LATEST_MAX_PAGES
    expect(hook.result.current.documents).toHaveLength(30);
  });

  it('reports truncation when it gives up with pages remaining', async () => {
    graphql.mockImplementation(endlessPages(3));

    const hook = await load(LATEST_PERIODS);

    expect(hook.result.current.latestTruncated).toBe(true);
  });

  it('reports truncation when it stops on the target with more available', async () => {
    graphql.mockImplementation(endlessPages(200));

    const hook = await load(LATEST_PERIODS);

    expect(hook.result.current.latestTruncated).toBe(true);
  });

  it('does not claim truncation when the partition is exhausted below the target', async () => {
    graphql.mockResolvedValue(page(50, 0, null));

    const hook = await load(LATEST_PERIODS);

    expect(graphql).toHaveBeenCalledTimes(1);
    expect(hook.result.current.latestTruncated).toBe(false);
    expect(hook.result.current.documents).toHaveLength(50);
  });

  it('carries the requested view, so Latest works in the Test Studio partition too', async () => {
    graphql.mockResolvedValue(page(2, 0, null));
    const hook = await load(LATEST_PERIODS);
    expect(variablesOf(0)).toMatchObject({ view: 'PRODUCTION' });

    await act(async () => {
      hook.result.current.setDocumentView('TEST');
    });
    await waitFor(() => expect(graphql).toHaveBeenCalledTimes(2));

    expect(variablesOf(1)).toMatchObject({ view: 'TEST' });
    expect(variablesOf(1)).not.toHaveProperty('startDateTime');
  });
});

describe('default scope', () => {
  it('is Latest when the caller passes no period, matching the storage default', async () => {
    // The two used to disagree — 2 days here against 2 hours in GenAIIDPLayout.
    graphql.mockResolvedValue(page(5, 0, null));
    const hook = renderHook(() => useGraphQlApi());

    await act(async () => {
      hook.result.current.setIsDocumentsListLoading(true);
    });
    await waitFor(() => expect(graphql).toHaveBeenCalled());

    expect(variablesOf(0)).not.toHaveProperty('startDateTime');
    expect(variablesOf(0)).not.toHaveProperty('endDateTime');
  });
});

describe('view switching', () => {
  it('does not fetch on a view change before the list has ever been requested', async () => {
    // The hook is mounted on every page under the documents layout, not just the
    // list, so it must stay quiet until the list asks for data.
    graphql.mockResolvedValue(page(2, 0, null));
    const hook = renderHook(() => useGraphQlApi({ initialPeriodsToLoad: LATEST_PERIODS }));

    await act(async () => {
      hook.result.current.setDocumentView('TEST');
    });

    expect(graphql).not.toHaveBeenCalled();
  });

  it('clears the previous partition rather than merging the two', async () => {
    // setDocumentsDeduped keeps rows the response did not mention, so without the
    // reset the test documents would linger in the production list.
    graphql.mockResolvedValueOnce(page(3, 0, null));
    const hook = await load(DOCUMENT_LIST_SHARDS_PER_DAY);
    expect(hook.result.current.documents).toHaveLength(3);

    graphql.mockResolvedValueOnce(page(2, 500, null));
    await act(async () => {
      hook.result.current.setDocumentView('TEST');
    });
    await waitFor(() => expect(hook.result.current.documents).toHaveLength(2));

    expect(hook.result.current.documents.map((d) => d.ObjectKey)).toEqual(['doc-500.pdf', 'doc-501.pdf']);
  });
});

describe('windowed periods', () => {
  it('still send both bounds and drain every page in the range', async () => {
    graphql
      .mockResolvedValueOnce(page(200, 0, 'a'))
      .mockResolvedValueOnce(page(200, 200, 'b'))
      .mockResolvedValueOnce(page(7, 400, null));

    const hook = await load(DOCUMENT_LIST_SHARDS_PER_DAY);

    expect(graphql).toHaveBeenCalledTimes(3);
    expect(hook.result.current.documents).toHaveLength(407);
    expect(variablesOf(0)).toHaveProperty('startDateTime');
    expect(variablesOf(0)).toHaveProperty('endDateTime');
    // The 10-page Latest ceiling must not leak into the windowed path, and a
    // windowed load is never marked truncated.
    expect(hook.result.current.latestTruncated).toBe(false);
  });

  it('drains past the Latest page ceiling when a window genuinely holds more', async () => {
    graphql.mockImplementation(() => {
      const call = graphql.mock.calls.length;
      return Promise.resolve(call < 15 ? page(200, call * 200, `t-${call}`) : page(1, 3000, null));
    });

    const hook = await load(DOCUMENT_LIST_SHARDS_PER_DAY);

    expect(graphql.mock.calls.length).toBeGreaterThan(10);
    expect(hook.result.current.latestTruncated).toBe(false);
  });
});
