// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Reprocess from the Document List. What has to hold: the dialog is given the table's
 * selection; its choice (profile, revision, start step) reaches the reprocess call with
 * the selected documents' keys; the dialog is handed each document's result, so it can
 * list the ones that were not queued; the selection is cleared once the request went
 * through, and kept when the request itself failed, so it can be tried again. The
 * contexts are mocked and the dialog is a stand-in; the table and its selection are real.
 */

import React from 'react';
import { IntlProvider } from 'react-intl';
import { act, fireEvent, render } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

type Confirm = (version?: string, revision?: number, startStep?: string) => Promise<unknown>;
interface ModalProps {
  visible: boolean;
  isLoading: boolean;
  selectedItems: readonly { objectKey: string }[];
  onConfirm: Confirm;
}

const { documentsRef, reprocessDocuments, modalRef } = vi.hoisted(() => ({
  documentsRef: { current: [] as Record<string, unknown>[] },
  reprocessDocuments: vi.fn(),
  modalRef: { current: null as ModalProps | null },
}));

vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
vi.mock('../../../api/auth-session', () => ({
  fetchSharedAuthSession: () => Promise.resolve({ tokens: { idToken: { payload: { 'cognito:username': 'admin' } } } }),
}));
vi.mock('react-router-dom', async (importOriginal) => ({
  ...(await importOriginal<typeof import('react-router-dom')>()),
  useNavigate: () => vi.fn(),
}));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../../../contexts/app', () => ({ default: () => ({ currentCredentials: null }) }));
vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({ isAdmin: true, isReviewerOnly: false, canWrite: true, canReview: true, canWorkReviewQueues: false }),
}));
vi.mock('../../../hooks/use-configuration-versions', () => ({ default: () => ({ versions: [] }) }));
vi.mock('../CircuitBreakerBadge', () => ({ default: () => <span /> }));
vi.mock('../../common/ReprocessDocumentModal', () => ({
  default: (props: ModalProps) => {
    modalRef.current = props;
    return null;
  },
}));
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
    documentView: 'PRODUCTION',
    setDocumentView: vi.fn(),
    latestTruncated: false,
    getDocumentDetailsFromIds: vi.fn(),
    deleteDocuments: vi.fn(),
    reprocessDocuments: (...a: unknown[]) => reprocessDocuments(...a),
    abortWorkflows: vi.fn(),
  }),
}));

import DocumentList from '../DocumentList';

const English = ({ children }: { children: React.ReactNode }) => (
  <IntlProvider locale="en" defaultLocale="en">
    {children}
  </IntlProvider>
);

const doc = (ObjectKey: string) => ({
  ObjectKey,
  ObjectStatus: 'COMPLETED',
  InitialEventTime: '2026-10-01T00:00:00Z',
  EvaluationStatus: 'NOT_EVALUATED',
  ConfigVersion: 'lending',
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
const selectedKeys = () => modalRef.current!.selectedItems.map((item) => item.objectKey).sort();
const confirm = async (...args: Parameters<Confirm>) => {
  let answer: unknown;
  await act(async () => {
    answer = await modalRef.current!.onConfirm(...args);
  });
  return answer;
};

beforeEach(() => {
  localStorage.clear();
  modalRef.current = null;
  reprocessDocuments.mockReset();
  documentsRef.current = [doc('a.pdf'), doc('b.pdf'), doc('c.pdf')];
  render(<DocumentList />, { wrapper: English });
  selectRowByKey('a.pdf');
  selectRowByKey('c.pdf');
});

describe('Reprocess from the Document List', () => {
  it('gives the dialog the selection', () => {
    expect(selectedKeys()).toEqual(['a.pdf', 'c.pdf']);
  });

  it('sends the selected documents with the dialog’s profile, revision and start step, and hands the results back', async () => {
    const results = [
      { objectKey: 'a.pdf', status: 'QUEUED', startStep: 'extraction' },
      { objectKey: 'c.pdf', status: 'REFUSED', startStep: 'extraction', reason: 'OUTPUT_MISSING' },
    ];
    reprocessDocuments.mockResolvedValue(results);

    const answer = await confirm('lending', 3, 'extraction');

    expect(reprocessDocuments).toHaveBeenCalledTimes(1);
    const [keys, ...rest] = reprocessDocuments.mock.calls[0] as [string[], ...unknown[]];
    expect([...keys].sort()).toEqual(['a.pdf', 'c.pdf']);
    expect(rest).toEqual(['lending', 3, 'extraction']);
    // The refusal is the dialog's to show: it gets every document's result.
    expect(answer).toEqual(results);
    expect(modalRef.current!.isLoading).toBe(false);
  });

  it('clears the selection once the request went through', async () => {
    reprocessDocuments.mockResolvedValue([
      { objectKey: 'a.pdf', status: 'QUEUED' },
      { objectKey: 'c.pdf', status: 'QUEUED' },
    ]);

    await confirm('lending');

    expect(selectedKeys()).toEqual([]);
  });

  it('keeps the selection when the request itself failed, and tells the dialog there is nothing to show', async () => {
    reprocessDocuments.mockResolvedValue(false);

    const answer = await confirm('lending', undefined, 'beginning');

    expect(answer).toBeNull();
    expect(selectedKeys()).toEqual(['a.pdf', 'c.pdf']);
    expect(modalRef.current!.isLoading).toBe(false);
  });
});
