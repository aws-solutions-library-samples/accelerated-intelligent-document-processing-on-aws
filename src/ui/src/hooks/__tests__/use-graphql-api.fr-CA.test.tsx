// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The document list's load errors under `fr-CA`. With a fixture catalog a failed list load
 * flashes one French sentence, its action phrase included, and the catalog's arrival lists the
 * documents no second time: the loads and the poll never read `intl`, and an effect words the
 * failure they hold. Under `en` the same loads flash today's English. The server's error text
 * stays as it is.
 */
import { act, waitFor } from '@testing-library/react';
import { I18n } from 'aws-amplify/utils';
import { useIntl } from 'react-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { renderHookWithIntl } from '../../test-utils/renderWithIntl';

// One value per mocked hook for every render, so no render hands out a fresh object. The hook
// builds its GraphQL client at module scope, so the client's graphql function is hoisted too.
const state = vi.hoisted(() => {
  const setErrorMessage = vi.fn();
  return { graphql: vi.fn(), setErrorMessage, app: { setErrorMessage } };
});

vi.mock('virtual:i18n-catalogs', () => import('../../test-utils/i18nCatalogsMock'));
vi.mock('../../api/client-shim', () => ({ generateClient: () => ({ graphql: state.graphql }) }));
vi.mock('../../contexts/app', () => ({ default: () => state.app }));
// The 5s poll would list the documents on its own; these tests count the load's requests.
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
import { LATEST_PERIODS } from '../../components/document-list/documents-table-config';

/** The strings the assertions read; the rest fall back to English, as the app's error policy allows. */
const FR = {
  'documents.api.listDocumentsAction': 'lister les documents',
  'common.apiError.failed': 'Échec de l’opération « {action} » : {error}',
};
const FR_CA = { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'], messages: FR } as const;

/** What the dispatcher sends for a resolver that raised, as the client throws it. */
const SERVER_ERROR = { errors: [{ message: 'boom', errorType: 'InternalError' }] };
const PAGE = { data: { listDocuments: { Documents: [{ ObjectKey: 'a.pdf', PK: 'doc#a.pdf', SK: 'none' }], nextToken: null } } };

/** The hook, and how many ids the catalog in force holds (none until the French one arrives). */
const useListAndCatalog = () => ({
  list: useGraphQlApi({ initialPeriodsToLoad: LATEST_PERIODS }),
  catalogSize: Object.keys(useIntl().messages).length,
});

const listRequests = () => state.graphql.mock.calls.filter(([arg]) => (arg as { query: string }).query === 'listDocuments');

beforeEach(() => {
  state.graphql.mockReset();
  state.setErrorMessage.mockReset();
});

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.lang = '';
  I18n.setLanguage('');
});

describe('the document list under fr-CA', () => {
  it('flashes a failed list load as one French sentence', async () => {
    state.graphql.mockRejectedValue(SERVER_ERROR);
    const hook = renderHookWithIntl(useListAndCatalog, FR_CA);
    // Awaited: the catalog arrives asynchronously, as the chunk does in the browser.
    await waitFor(() => expect(hook.result.current.catalogSize).toBeGreaterThan(0));
    act(() => hook.result.current.list.setIsDocumentsListLoading(true));
    await waitFor(() => expect(state.setErrorMessage).toHaveBeenCalledTimes(1));
    expect(state.setErrorMessage).toHaveBeenCalledWith('Échec de l’opération « lister les documents » : boom');
  });

  // The load starts before the catalog arrives, as a new `intl`; that must not list the documents again.
  it('lists the documents once, not again when the catalog arrives', async () => {
    state.graphql.mockResolvedValue(PAGE);
    const hook = renderHookWithIntl(useListAndCatalog, FR_CA);
    act(() => hook.result.current.list.setIsDocumentsListLoading(true));
    await waitFor(() => expect(hook.result.current.catalogSize).toBeGreaterThan(0));
    await waitFor(() => expect(hook.result.current.list.documents).toHaveLength(1));
    expect(listRequests()).toHaveLength(1);
    expect(state.setErrorMessage).not.toHaveBeenCalled();
  });
});

describe('the document list under en', () => {
  it('flashes a failed list load in English', async () => {
    state.graphql.mockRejectedValue(SERVER_ERROR);
    const hook = renderHookWithIntl(useListAndCatalog);
    act(() => hook.result.current.list.setIsDocumentsListLoading(true));
    await waitFor(() => expect(state.setErrorMessage).toHaveBeenCalledTimes(1));
    expect(state.setErrorMessage).toHaveBeenCalledWith('Failed to list documents: boom');
  });

  it('lists the documents once', async () => {
    state.graphql.mockResolvedValue(PAGE);
    const hook = renderHookWithIntl(useListAndCatalog);
    act(() => hook.result.current.list.setIsDocumentsListLoading(true));
    await waitFor(() => expect(hook.result.current.list.documents).toHaveLength(1));
    expect(hook.result.current.catalogSize).toBe(0);
    expect(listRequests()).toHaveLength(1);
    expect(state.setErrorMessage).not.toHaveBeenCalled();
  });
});
