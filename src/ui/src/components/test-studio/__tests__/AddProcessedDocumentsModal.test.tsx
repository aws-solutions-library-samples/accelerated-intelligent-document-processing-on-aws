// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The "From processed documents" dialog. What has to hold: only completed
 * Production documents are offered, the mutation receives the picked keys and
 * the target set, documents without ground truth are named before submitting,
 * a labeled target set gets a warning that it will read as unlabeled, and a
 * failure stays in the dialog.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }) }));
vi.mock('../../../graphql/generated', () => ({
  addDocumentsToTestSetByKey: 'addDocumentsToTestSetByKey',
  getTestSets: 'getTestSets',
  listDocuments: 'listDocuments',
}));

import AddProcessedDocumentsModal from '../AddProcessedDocumentsModal';

const doc = (key: string, evaluation: string, status = 'COMPLETED', at = '2026-09-20T10:00:00Z') => ({
  ObjectKey: key,
  ObjectStatus: status,
  InitialEventTime: at,
  EvaluationStatus: evaluation,
});

const documents = [
  doc('labeled.pdf', 'BASELINE_AVAILABLE', 'COMPLETED', '2026-09-22T10:00:00Z'),
  doc('fresh.pdf', 'NOT_EVALUATED', 'COMPLETED', '2026-09-21T10:00:00Z'),
  doc('running.pdf', 'NOT_EVALUATED', 'RUNNING', '2026-09-23T10:00:00Z'),
];

const answer = ({
  labelState = 'labeled',
  fileCount = 40,
  add,
  nextToken = null as string | null,
}: { labelState?: string; fileCount?: number; add?: () => unknown; nextToken?: string | null } = {}) => {
  graphql.mockImplementation(async ({ query, variables }: { query: string; variables?: Record<string, unknown> }) => {
    if (query === 'listDocuments') {
      if (variables?.nextToken === 'page-2')
        return {
          data: { listDocuments: { Documents: [doc('older.pdf', 'COMPLETED', 'COMPLETED', '2026-09-01T10:00:00Z')], nextToken: null } },
        };
      return { data: { listDocuments: { Documents: documents, nextToken } } };
    }
    if (query === 'getTestSets')
      return { data: { getTestSets: [{ id: 'invoices', name: 'Invoices', status: 'COMPLETED', fileCount, labelState }] } };
    if (query === 'addDocumentsToTestSetByKey') {
      if (add) return add();
      return { data: { addDocumentsToTestSetByKey: { id: 'invoices', name: 'Invoices', fileCount, status: 'UPDATING' } } };
    }
    throw new Error(`unexpected ${query}`);
  });
};

const table = () => {
  const t = createWrapper(document.body).findTable();
  if (!t) throw new Error('document table not rendered');
  return t;
};

const rowTexts = () =>
  table()
    .findRows()
    .map((r) => r.getElement().textContent ?? '');

const select = (key: string) => {
  const index = rowTexts().findIndex((t) => t.includes(key));
  if (index < 0) throw new Error(`no row for ${key}`);
  fireEvent.click(
    table()
      .findRowSelectionArea(index + 1)!
      .find('input')!
      .getElement(),
  );
};

const target = { id: 'invoices', name: 'Invoices' };

const renderModal = (onSubmitted = vi.fn()) => {
  render(<AddProcessedDocumentsModal visible testSet={target} onDismiss={vi.fn()} onSubmitted={onSubmitted} />);
  return onSubmitted;
};

describe('AddProcessedDocumentsModal', () => {
  beforeEach(() => {
    graphql.mockReset();
  });

  it('offers only completed Production documents, newest first', async () => {
    answer();
    renderModal();

    await waitFor(() => expect(rowTexts()).toHaveLength(2));
    expect(rowTexts()[0]).toContain('labeled.pdf');
    expect(rowTexts()[0]).toContain('Available');
    expect(rowTexts()[1]).toContain('fresh.pdf');
    expect(rowTexts()[1]).toContain('None yet');
    expect(rowTexts().join(' ')).not.toContain('running.pdf');
    expect(graphql).toHaveBeenCalledWith(
      expect.objectContaining({ query: 'listDocuments', variables: expect.objectContaining({ view: 'PRODUCTION' }) }),
    );
  });

  it('sends the picked keys to the set and reports the unlabeled count', async () => {
    answer({ labelState: 'unlabeled' });
    const onSubmitted = renderModal();
    await waitFor(() => expect(rowTexts()).toHaveLength(2));

    const submit = screen.getByRole('button', { name: 'Add documents' });
    expect(submit).toBeDisabled();

    select('labeled.pdf');
    select('fresh.pdf');
    const addTwo = screen.getByRole('button', { name: 'Add 2 documents' });
    fireEvent.click(addTwo);

    await waitFor(() => expect(onSubmitted).toHaveBeenCalledTimes(1));
    const addCall = graphql.mock.calls.find((c) => (c[0] as { query: string }).query === 'addDocumentsToTestSetByKey');
    expect((addCall?.[0] as { variables: { testSetId: string; objectKeys: string[] } }).variables.testSetId).toBe('invoices');
    expect([...(addCall?.[0] as { variables: { objectKeys: string[] } }).variables.objectKeys].sort()).toEqual([
      'fresh.pdf',
      'labeled.pdf',
    ]);
    const result = onSubmitted.mock.calls[0][0];
    expect(result.kind).toBe('documents');
    expect(result.testSet).toEqual({ id: 'invoices', status: 'UPDATING', fileCount: 40 });
    expect(result.message).toContain('Adding 2 documents to test set "Invoices".');
    expect(result.message).toContain('1 has no ground truth');
  });

  it('names the documents without ground truth and warns when the set is labeled', async () => {
    answer({ labelState: 'labeled' });
    renderModal();
    await waitFor(() => expect(rowTexts()).toHaveLength(2));

    select('labeled.pdf');
    expect(screen.queryByText(/no ground truth yet/)).toBeNull();
    expect(screen.queryByText(/will read as unlabeled/)).toBeNull();

    select('fresh.pdf');
    expect(await screen.findByText('1 of 2 has no ground truth yet')).toBeInTheDocument();
    const listed = screen.getByRole('list', { name: 'Documents without ground truth' });
    expect(listed).toHaveTextContent('fresh.pdf');
    expect(listed).not.toHaveTextContent('labeled.pdf');
    expect(await screen.findByText('"Invoices" will read as unlabeled')).toBeInTheDocument();
  });

  it('does not warn for an empty set', async () => {
    answer({ labelState: 'unlabeled', fileCount: 0 });
    renderModal();
    await waitFor(() => expect(rowTexts()).toHaveLength(2));
    select('fresh.pdf');
    expect(await screen.findByText('1 of 1 has no ground truth yet')).toBeInTheDocument();
    expect(screen.queryByText(/will read as unlabeled/)).toBeNull();
  });

  it('loads older documents on request', async () => {
    answer({ nextToken: 'page-2' });
    renderModal();
    await waitFor(() => expect(rowTexts()).toHaveLength(2));

    fireEvent.click(screen.getByRole('button', { name: 'Load older documents' }));
    await waitFor(() => expect(rowTexts()).toHaveLength(3));
    expect(rowTexts()[2]).toContain('older.pdf');
    expect(screen.queryByRole('button', { name: 'Load older documents' })).toBeNull();
  });

  it('keeps a failed add in the dialog', async () => {
    answer({
      add: () => {
        throw new Error("Test set 'invoices' is not in COMPLETED status (current: UPDATING)");
      },
    });
    const onSubmitted = renderModal();
    await waitFor(() => expect(rowTexts()).toHaveLength(2));
    select('labeled.pdf');
    fireEvent.click(screen.getByRole('button', { name: 'Add 1 document' }));

    expect(await screen.findByText(/not in COMPLETED status/)).toBeInTheDocument();
    expect(onSubmitted).not.toHaveBeenCalled();
  });
});
