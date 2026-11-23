// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Document List's "Add to test set" action (#812). What has to hold: it is
 * offered to Admins only and only in the Production view; it opens Test Studio's
 * own add dialog with the table's selection already chosen and a test-set picker;
 * and submitting calls the existing by-key mutation with the selected finished
 * documents' keys and the picked set. The contexts and the API client are mocked;
 * the table, the selection, the toolbar and the dialog are real.
 */

import React from 'react';
import { IntlProvider } from 'react-intl';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const { graphqlMock, documentsRef, roleRef, viewRef } = vi.hoisted(() => ({
  graphqlMock: vi.fn(),
  documentsRef: { current: [] as Record<string, unknown>[] },
  roleRef: { current: {} as Record<string, boolean> },
  viewRef: { current: 'PRODUCTION' },
}));

vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: graphqlMock }) }));
vi.mock('../../../api/auth-session', () => ({
  fetchSharedAuthSession: () => Promise.resolve({ tokens: { idToken: { payload: { 'cognito:username': 'admin' } } } }),
}));
vi.mock('react-router-dom', async (importOriginal) => ({
  ...(await importOriginal<typeof import('react-router-dom')>()),
  useNavigate: () => vi.fn(),
}));
vi.mock('../../../graphql/generated', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../graphql/generated')>()),
  addDocumentsToTestSetByKey: 'addDocumentsToTestSetByKey',
  getTestSets: 'getTestSets',
  listDocuments: 'listDocuments',
}));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../../../contexts/app', () => ({ default: () => ({ currentCredentials: null }) }));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => roleRef.current }));
vi.mock('../../../hooks/use-configuration-versions', () => ({ default: () => ({ versions: [] }) }));
vi.mock('../CircuitBreakerBadge', () => ({ default: () => <span /> }));
vi.mock('../../common/ReprocessDocumentModal', () => ({ default: () => null }));
vi.mock('../../../contexts/documents', () => ({
  default: () => ({
    documents: documentsRef.current,
    isDocumentsListLoading: false,
    hasListBeenLoaded: true,
    setIsDocumentsListLoading: vi.fn(),
    setPeriodsToLoad: vi.fn(),
    setSelectedItems: vi.fn(),
    setToolsOpen: vi.fn(),
    periodsToLoad: 1,
    customDateRange: null,
    setCustomDateRange: vi.fn(),
    documentView: viewRef.current,
    setDocumentView: vi.fn(),
    latestTruncated: false,
    getDocumentDetailsFromIds: vi.fn(),
    deleteDocuments: vi.fn(),
    reprocessDocuments: vi.fn(),
    abortWorkflows: vi.fn(),
  }),
}));

import DocumentList from '../DocumentList';

const ADMIN = { isAdmin: true, isReviewerOnly: false, canWrite: true, canReview: true, canWorkReviewQueues: false };
const AUTHOR = { isAdmin: false, isReviewerOnly: false, canWrite: true, canReview: false, canWorkReviewQueues: false };

const English = ({ children }: { children: React.ReactNode }) => (
  <IntlProvider locale="en" defaultLocale="en">
    {children}
  </IntlProvider>
);

const doc = (ObjectKey: string, over: Record<string, unknown> = {}) => ({
  ObjectKey,
  ObjectStatus: 'COMPLETED',
  InitialEventTime: '2026-10-01T00:00:00Z',
  EvaluationStatus: 'NOT_EVALUATED',
  ConfigVersion: 'lending',
  ...over,
});

const documentTable = () => createWrapper().findTable()!;

const selectRowByKey = (key: string) => {
  const rows = documentTable().findRows();
  const index = rows.findIndex((r) => (r.getElement().textContent ?? '').includes(key));
  if (index < 0) throw new Error(`no row for ${key}`);
  fireEvent.click(
    documentTable()
      .findRowSelectionArea(index + 1)!
      .find('input')!
      .getElement(),
  );
};

const addButton = () => screen.queryByRole('button', { name: 'Add to test set' });

const PICKER_HEADER = 'Add documents to a test set';

// The list keeps its other dialogs mounted while hidden, so find this one by its header.
const dialog = () =>
  createWrapper()
    .findAllModals()
    .find((m) => (m.findHeader()?.getElement().textContent ?? '').includes(PICKER_HEADER)) ?? null;

beforeEach(() => {
  graphqlMock.mockReset();
  localStorage.clear();
  roleRef.current = ADMIN;
  viewRef.current = 'PRODUCTION';
  documentsRef.current = [
    doc('labeled.pdf', { EvaluationStatus: 'BASELINE_AVAILABLE' }),
    doc('fresh.pdf'),
    doc('running.pdf', { ObjectStatus: 'RUNNING' }),
  ];
  graphqlMock.mockImplementation(async ({ query }: { query: string }) => {
    if (query === 'getTestSets')
      return {
        data: { getTestSets: [{ id: 'invoices', name: 'Invoice Samples', status: 'COMPLETED', fileCount: 4, labelState: 'unlabeled' }] },
      };
    if (query === 'addDocumentsToTestSetByKey')
      return { data: { addDocumentsToTestSetByKey: { id: 'invoices', fileCount: 4, status: 'UPDATING' } } };
    throw new Error(`unexpected ${query}`);
  });
});

describe('DocumentList Add to test set', () => {
  it('is offered to an Admin, and enabled only once a finished document is selected', () => {
    render(<DocumentList />, { wrapper: English });
    expect(addButton()).toBeDisabled();

    selectRowByKey('running.pdf');
    expect(addButton()).toBeDisabled();

    expect(addButton()!.closest('span[title]')).toHaveAttribute('title', 'Select at least one document that has finished processing');

    selectRowByKey('fresh.pdf');
    expect(addButton()).toBeEnabled();
    expect(addButton()!.closest('span[title]')).toHaveAttribute('title', 'Add the selected documents to a test set');
  });

  it('is not offered to an Author, although Authors can write', () => {
    roleRef.current = AUTHOR;
    render(<DocumentList />, { wrapper: English });
    expect(screen.getByRole('button', { name: 'Reprocess' })).toBeInTheDocument();
    expect(addButton()).toBeNull();
  });

  it('is not offered in the Test Studio view', () => {
    viewRef.current = 'TEST';
    render(<DocumentList />, { wrapper: English });
    expect(addButton()).toBeNull();
  });

  it('opens the test-set picker with the selection and adds the selected keys to the picked set', async () => {
    render(<DocumentList />, { wrapper: English });
    selectRowByKey('labeled.pdf');
    selectRowByKey('fresh.pdf');
    selectRowByKey('running.pdf');
    expect(dialog()).toBeNull();

    fireEvent.click(addButton()!);

    expect(dialog()).not.toBeNull();
    expect(screen.getByText('1 selected document is not included')).toBeInTheDocument();
    const picker = createWrapper(dialog()!.getElement()).findSelect()!;
    picker.openDropdown();
    await waitFor(() => expect(picker.findDropdown().findOptions()).toHaveLength(1));
    picker.selectOptionByValue('invoices');
    fireEvent.click(screen.getByRole('button', { name: 'Add 2 documents' }));

    await waitFor(() => expect(dialog()).toBeNull());
    const addCalls = graphqlMock.mock.calls.filter(([args]) => args.query === 'addDocumentsToTestSetByKey');
    expect(addCalls).toHaveLength(1);
    expect(addCalls[0][0].variables.testSetId).toBe('invoices');
    expect([...addCalls[0][0].variables.objectKeys].sort()).toEqual(['fresh.pdf', 'labeled.pdf']);
    expect(graphqlMock.mock.calls.some(([args]) => args.query === 'listDocuments')).toBe(false);
    expect(await screen.findByText(/Adding 2 documents to test set "Invoice Samples"\./)).toBeInTheDocument();
    // Ground truth is read from the list's own rows: only fresh.pdf has none.
    expect(screen.getByText(/1 has no ground truth/)).toBeInTheDocument();
  });

  it('closes on Cancel without adding anything', () => {
    render(<DocumentList />, { wrapper: English });
    selectRowByKey('fresh.pdf');
    fireEvent.click(addButton()!);
    expect(dialog()).not.toBeNull();

    const cancel = createWrapper(dialog()!.getElement())
      .findAllButtons()
      .find((b) => b.getElement().textContent === 'Cancel')!;
    fireEvent.click(cancel.getElement());
    expect(dialog()).toBeNull();
    expect(graphqlMock.mock.calls.some(([args]) => args.query === 'addDocumentsToTestSetByKey')).toBe(false);
  });

  it('clears the previous confirmation when the dialog is opened again', async () => {
    render(<DocumentList />, { wrapper: English });
    selectRowByKey('fresh.pdf');
    fireEvent.click(addButton()!);
    const picker = createWrapper(dialog()!.getElement()).findSelect()!;
    picker.openDropdown();
    await waitFor(() => expect(picker.findDropdown().findOptions()).toHaveLength(1));
    picker.selectOptionByValue('invoices');
    fireEvent.click(screen.getByRole('button', { name: 'Add 1 document' }));
    expect(await screen.findByText(/Adding 1 document to test set/)).toBeInTheDocument();

    fireEvent.click(addButton()!);
    expect(screen.queryByText(/Adding 1 document to test set/)).toBeNull();
  });
});
