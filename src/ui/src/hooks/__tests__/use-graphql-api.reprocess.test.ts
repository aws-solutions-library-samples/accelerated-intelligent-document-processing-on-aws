// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * reprocessDocuments sends the profile, the revision and the start step only when each
 * is given, and returns what happened to each document: a refusal for one document is
 * an answer, not an error. A request that fails is reported and returns false.
 */

import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, renderHook } from '@testing-library/react';
import { IntlProvider } from 'react-intl';

const { graphql, setErrorMessage } = vi.hoisted(() => ({ graphql: vi.fn(), setErrorMessage: vi.fn() }));

vi.mock('../../api/client-shim', () => ({ generateClient: () => ({ graphql }) }));
vi.mock('../../contexts/app', () => ({ default: () => ({ setErrorMessage }) }));
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

// The hook words its error messages through react-intl, which needs a provider.
const English = ({ children }: { children: React.ReactNode }) =>
  React.createElement(IntlProvider, { locale: 'en', defaultLocale: 'en' }, children);

const RESULTS = [
  { objectKey: 'a.pdf', status: 'QUEUED', startStep: 'extraction' },
  { objectKey: 'b.pdf', status: 'REFUSED', startStep: 'extraction', reason: 'OUTPUT_MISSING', message: 'no OCR output' },
];

const reprocess = async (...args: Parameters<ReturnType<typeof useGraphQlApi>['reprocessDocuments']>) => {
  const hook = renderHook(() => useGraphQlApi({ initialPeriodsToLoad: 1 }), { wrapper: English });
  let answer: unknown;
  await act(async () => {
    answer = await hook.result.current.reprocessDocuments(...args);
  });
  return answer;
};
const sent = () =>
  graphql.mock.calls.find(([arg]) => (arg as { query: string }).query === 'reprocessDocument')![0] as { variables: unknown };

describe('reprocessDocuments', () => {
  beforeEach(() => {
    graphql.mockReset().mockResolvedValue({ data: { reprocessDocument: { queued: 1, results: RESULTS } } });
    setErrorMessage.mockReset();
  });
  afterEach(() => cleanup());

  it('sends the documents alone when nothing else is chosen', async () => {
    await reprocess(['a.pdf', 'b.pdf']);

    expect(sent().variables).toEqual({ objectKeys: ['a.pdf', 'b.pdf'] });
  });

  it('sends the profile, the revision and the start step when they are given', async () => {
    await reprocess(['a.pdf'], 'claims', 0, 'extraction');

    expect(sent().variables).toEqual({ objectKeys: ['a.pdf'], version: 'claims', revision: 0, startStep: 'extraction' });
  });

  it('returns each document’s result, a refusal among them, and reports no error', async () => {
    const answer = await reprocess(['a.pdf', 'b.pdf'], undefined, undefined, 'extraction');

    expect(answer).toEqual(RESULTS);
    expect(setErrorMessage).not.toHaveBeenCalled();
  });

  it('reports a request that failed and returns false', async () => {
    graphql.mockRejectedValue({ errors: [{ message: 'Access denied' }] });

    const answer = await reprocess(['a.pdf']);

    expect(answer).toBe(false);
    expect(setErrorMessage).toHaveBeenCalledWith('Failed to reprocess document(s) - please try again later');
  });
});
