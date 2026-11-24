// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Production adapter's Refresh. What has to hold: it reads the review item, then the
 * document; a failed item read throws (for the bar to report) rather than turning the
 * page into its load error, and the document is not read. The adapter also keeps the
 * document's policy rule validation report, from the document it was seeded with and
 * from each later read, for the Reports menu.
 */

import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
const fetchReviewItem = vi.fn();
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }) }));
vi.mock('../../../contexts/app', () => ({ default: () => ({ setErrorMessage: vi.fn() }) }));
vi.mock('../../../hooks/use-polling', () => ({ default: () => undefined }));
vi.mock('../../review-queues/review-api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../review-queues/review-api')>()),
  fetchReviewItem: (...a: unknown[]) => fetchReviewItem(...a),
}));

import { getDocument } from '../../../graphql/generated';
import { useProductionItemEdit } from '../production-item-edit-adapter';

const DETAIL = { item: { objectKey: 'in/w2.pdf' }, sections: [], history: [] };
const order: string[] = [];

beforeEach(() => {
  order.length = 0;
  fetchReviewItem.mockReset().mockImplementation(async () => {
    order.push('getReviewItem');
    return DETAIL;
  });
  graphql.mockReset().mockImplementation(async ({ query }: { query: unknown }) => {
    if (query === getDocument) order.push('getDocument');
    return { data: { getDocument: { ObjectKey: 'in/w2.pdf', Pages: [], Sections: [] } } };
  });
});

describe('the Production adapter', () => {
  it('Refresh reads the item, then the document', async () => {
    const hook = renderHook(() => useProductionItemEdit('in/w2.pdf'));
    await waitFor(() => expect(order).toContain('getDocument'));
    order.length = 0;
    await act(() => hook.result.current.refresh());
    expect(order).toEqual(['getReviewItem', 'getDocument']);
  });

  it("keeps the document's policy rule validation report from the document read", async () => {
    graphql.mockResolvedValue({
      data: { getDocument: { ObjectKey: 'in/w2.pdf', Pages: [], Sections: [], RuleValidationResultUri: 's3://out/in/w2.pdf/rules.md' } },
    });
    const hook = renderHook(() => useProductionItemEdit('in/w2.pdf'));

    await waitFor(() => expect(hook.result.current.ruleValidationResultUri).toBe('s3://out/in/w2.pdf/rules.md'));
  });

  it('takes the policy rule validation report from the seed without a read, then from the next read', async () => {
    type Seed = NonNullable<Parameters<typeof useProductionItemEdit>[1]>;
    const seed = {
      detail: DETAIL,
      document: { ObjectKey: 'in/w2.pdf', Pages: [], Sections: [], RuleValidationResultUri: 's3://out/in/w2.pdf/seeded.md' },
    } as unknown as Seed;
    const hook = renderHook(() => useProductionItemEdit('in/w2.pdf', seed));

    expect(hook.result.current.ruleValidationResultUri).toBe('s3://out/in/w2.pdf/seeded.md');
    expect(order).toEqual([]);

    // A document with no policy report (the default answer here) has none after the read.
    await act(() => hook.result.current.refreshDocument());
    expect(hook.result.current.ruleValidationResultUri).toBeNull();
  });

  it('a failed item read throws, leaves the load error alone and reads no document', async () => {
    const hook = renderHook(() => useProductionItemEdit('in/w2.pdf'));
    await waitFor(() => expect(order).toContain('getDocument'));
    order.length = 0;
    fetchReviewItem.mockRejectedValueOnce(new Error('offline'));
    await expect(act(() => hook.result.current.refresh())).rejects.toThrow('offline');
    expect(hook.result.current.loadError).toBeNull();
    expect(order).toEqual([]);
  });
});
