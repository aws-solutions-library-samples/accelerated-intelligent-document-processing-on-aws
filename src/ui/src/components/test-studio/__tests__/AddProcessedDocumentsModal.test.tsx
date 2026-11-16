// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The "From processed documents" dialog. What has to hold: only completed
 * Production documents are offered, the mutation receives the picked keys and
 * the target set, documents without ground truth are named before submitting,
 * a labeled target set gets a warning that it will read as unlabeled, and a
 * failure stays in the dialog.
 *
 * Opened from the Document List the dialog runs the other way round: the
 * documents arrive preselected and a test-set picker replaces the fixed target.
 * Those cases are in the second describe block.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import React from 'react';
import { IntlProvider } from 'react-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }) }));
vi.mock('../../../graphql/generated', () => ({
  addDocumentsToTestSetByKey: 'addDocumentsToTestSetByKey',
  getTestSets: 'getTestSets',
  listDocuments: 'listDocuments',
}));

import AddProcessedDocumentsModal, { type PreselectedDocument } from '../AddProcessedDocumentsModal';

const English = ({ children }: { children: React.ReactNode }) => (
  <IntlProvider locale="en" defaultLocale="en">
    {children}
  </IntlProvider>
);

const doc = (key: string, evaluation: string, status = 'COMPLETED', at = '2026-09-20T10:00:00Z') => ({
  ObjectKey: key,
  ObjectStatus: status,
  InitialEventTime: at,
  EvaluationStatus: evaluation,
  ConfigVersion: 'lending',
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
      return { data: { getTestSets: [{ id: 'invoices', name: 'Invoice Samples', status: 'COMPLETED', fileCount, labelState }] } };
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
  render(<AddProcessedDocumentsModal visible testSet={target} onDismiss={vi.fn()} onSubmitted={onSubmitted} />, { wrapper: English });
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
    expect(result.message).toContain('Adding 2 documents to test set "Invoice Samples".');
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
    expect(await screen.findByText('"Invoice Samples" will read as unlabeled')).toBeInTheDocument();
    expect(screen.queryByText(/no ground truth yet/)).toBeNull();
    const listed = screen.getByRole('list', { name: 'Documents without ground truth' });
    expect(listed).toHaveTextContent('fresh.pdf');
    expect(listed).not.toHaveTextContent('labeled.pdf');
  });

  it('does not warn for an empty set', async () => {
    answer({ labelState: 'unlabeled', fileCount: 0 });
    renderModal();
    await waitFor(() => expect(rowTexts()).toHaveLength(2));
    select('fresh.pdf');
    expect(await screen.findByText('1 of 1 has no ground truth yet')).toBeInTheDocument();
    expect(screen.queryByText(/will read as unlabeled/)).toBeNull();
  });

  it("shows each document's profile and a readable submitted time, titled by the set's name", async () => {
    answer();
    renderModal();
    await waitFor(() => expect(rowTexts()).toHaveLength(2));

    expect(rowTexts()[0]).toContain('lending');
    expect(rowTexts()[0]).toContain(new Date('2026-09-22T10:00:00Z').toLocaleString());
    expect(rowTexts()[0]).not.toContain('2026-09-22T10:00:00Z');
    expect(await screen.findByText('Add processed documents to "Invoice Samples"')).toBeInTheDocument();
    expect(screen.getByText(/Documents already in this set are skipped/)).toBeInTheDocument();
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

const picked = (objectKey: string, evaluationStatus: string, objectStatus = 'COMPLETED'): PreselectedDocument => ({
  objectKey,
  objectStatus,
  evaluationStatus,
  submitted: '2026-09-20T10:00:00Z',
  configVersion: 'lending',
});

const testSetsAnswer = (sets: Record<string, unknown>[], add?: () => unknown) => {
  graphql.mockImplementation(async ({ query }: { query: string }) => {
    if (query === 'getTestSets') return { data: { getTestSets: sets } };
    if (query === 'addDocumentsToTestSetByKey') {
      if (add) return add();
      return { data: { addDocumentsToTestSetByKey: { id: 'invoices', fileCount: 41, status: 'UPDATING' } } };
    }
    throw new Error(`unexpected ${query}`);
  });
};

const SETS = [
  { id: 'invoices', name: 'Invoice Samples', status: 'COMPLETED', fileCount: 40, labelState: 'labeled' },
  { id: 'busy', name: 'Busy Set', status: 'UPDATING', fileCount: 3, labelState: 'unlabeled' },
];

const picker = () => {
  const found = createWrapper(createWrapper(document.body).findModal()!.getElement()).findSelect();
  if (!found) throw new Error('test set picker not rendered');
  return found;
};

const renderPicker = (preselected: PreselectedDocument[], onSubmitted = vi.fn()) => {
  render(<AddProcessedDocumentsModal visible testSet={null} preselected={preselected} onDismiss={vi.fn()} onSubmitted={onSubmitted} />, {
    wrapper: English,
  });
  return onSubmitted;
};

describe('AddProcessedDocumentsModal opened with a preselection', () => {
  beforeEach(() => {
    graphql.mockReset();
  });

  it('offers the preselected finished documents, all selected, and lists nothing from the API', async () => {
    testSetsAnswer(SETS);
    renderPicker([picked('labeled.pdf', 'BASELINE_AVAILABLE'), picked('fresh.pdf', 'NOT_EVALUATED'), picked('running.pdf', '', 'RUNNING')]);

    expect(screen.getByText('Add documents to a test set')).toBeInTheDocument();
    expect(screen.getByText('Selected documents')).toBeInTheDocument();
    expect(screen.getByText(/draft-labeled in Test Studio afterwards/)).toBeInTheDocument();
    expect(screen.queryByText(/Pick documents that have finished processing/)).toBeNull();
    expect(rowTexts()).toHaveLength(2);
    expect(rowTexts().join(' ')).not.toContain('running.pdf');
    expect(screen.getByText('1 selected document is not included')).toBeInTheDocument();
    expect(table().findSelectedRows()).toHaveLength(2);
    await waitFor(() => expect(graphql).toHaveBeenCalledWith(expect.objectContaining({ query: 'getTestSets' })));
    expect(graphql.mock.calls.some((c) => (c[0] as { query: string }).query === 'listDocuments')).toBe(false);
  });

  it('cannot submit until a test set is picked, and only a COMPLETED set can be picked', async () => {
    testSetsAnswer(SETS);
    renderPicker([picked('labeled.pdf', 'BASELINE_AVAILABLE')]);
    expect(screen.getByRole('button', { name: 'Add 1 document' })).toBeDisabled();

    picker().openDropdown();
    await waitFor(() => expect(picker().findDropdown().findOptions()).toHaveLength(2));
    const options = picker().findDropdown().findOptions();
    expect(options[0].getElement()).toHaveTextContent('Invoice Samples');
    expect(options[0].getElement()).toHaveTextContent('40 documents');
    expect(options[1].getElement()).toHaveTextContent('Unavailable while its status is UPDATING');
    picker().selectOptionByValue('busy');
    expect(screen.getByRole('button', { name: 'Add 1 document' })).toBeDisabled();

    // A disabled option does not close the dropdown, so it is still open here.
    picker().selectOptionByValue('invoices');
    expect(screen.getByRole('button', { name: 'Add 1 document' })).toBeEnabled();
  });

  it('sends the preselected keys to the picked set and reports it by name', async () => {
    testSetsAnswer(SETS);
    const onSubmitted = renderPicker([picked('labeled.pdf', 'BASELINE_AVAILABLE'), picked('fresh.pdf', 'NOT_EVALUATED')]);
    picker().openDropdown();
    await waitFor(() => expect(picker().findDropdown().findOptions()).toHaveLength(2));
    picker().selectOptionByValue('invoices');
    fireEvent.click(screen.getByRole('button', { name: 'Add 2 documents' }));

    await waitFor(() => expect(onSubmitted).toHaveBeenCalledTimes(1));
    const addCall = graphql.mock.calls.find((c) => (c[0] as { query: string }).query === 'addDocumentsToTestSetByKey');
    const variables = (addCall?.[0] as { variables: { testSetId: string; objectKeys: string[] } }).variables;
    expect(variables.testSetId).toBe('invoices');
    expect([...variables.objectKeys].sort()).toEqual(['fresh.pdf', 'labeled.pdf']);
    const result = onSubmitted.mock.calls[0][0];
    expect(result.testSet).toEqual({ id: 'invoices', status: 'UPDATING', fileCount: 41 });
    expect(result.message).toContain('Adding 2 documents to test set "Invoice Samples".');
    expect(result.message).toContain('1 has no ground truth');
  });

  it('sends only the documents still selected when one is unticked', async () => {
    testSetsAnswer(SETS);
    renderPicker([picked('labeled.pdf', 'BASELINE_AVAILABLE'), picked('fresh.pdf', 'NOT_EVALUATED')]);
    select('fresh.pdf');
    picker().openDropdown();
    await waitFor(() => expect(picker().findDropdown().findOptions()).toHaveLength(2));
    picker().selectOptionByValue('invoices');
    fireEvent.click(screen.getByRole('button', { name: 'Add 1 document' }));

    await waitFor(() =>
      expect(graphql.mock.calls.some((c) => (c[0] as { query: string }).query === 'addDocumentsToTestSetByKey')).toBe(true),
    );
    const addCall = graphql.mock.calls.find((c) => (c[0] as { query: string }).query === 'addDocumentsToTestSetByKey');
    expect((addCall?.[0] as { variables: { objectKeys: string[] } }).variables.objectKeys).toEqual(['labeled.pdf']);
  });

  it("warns against the picked set's label state", async () => {
    testSetsAnswer(SETS);
    renderPicker([picked('fresh.pdf', 'NOT_EVALUATED')]);
    expect(await screen.findByText('1 of 1 has no ground truth yet')).toBeInTheDocument();

    picker().openDropdown();
    await waitFor(() => expect(picker().findDropdown().findOptions()).toHaveLength(2));
    picker().selectOptionByValue('invoices');
    expect(await screen.findByText('"Invoice Samples" will read as unlabeled')).toBeInTheDocument();
  });

  it('shows a failure to load the test sets in the picker', async () => {
    graphql.mockImplementation(async () => {
      throw new Error('Unauthorized');
    });
    renderPicker([picked('labeled.pdf', 'BASELINE_AVAILABLE')]);
    picker().openDropdown();
    expect(await screen.findByText('Could not load test sets: Unauthorized')).toBeInTheDocument();
  });
});
