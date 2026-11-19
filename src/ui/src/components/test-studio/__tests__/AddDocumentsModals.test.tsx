// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The shared Add documents dialogs, now used from two pages. What has to hold:
 * the pattern dialog cannot submit before a check has found something, the
 * mutations receive the target set's id, and the caller learns enough from
 * onSubmitted to update its own view of the set.
 */

import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import { IntlProvider } from 'react-intl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }) }));
vi.mock('../../../graphql/generated', () => ({
  addDocumentsToTestSet: 'addDocumentsToTestSet',
  addDocumentsToTestSetFromUpload: 'addDocumentsToTestSetFromUpload',
  listBucketFiles: 'listBucketFiles',
}));

// Imported after the mocks, which vitest hoists.
import AddDocumentsModals from '../AddDocumentsModals';

const English = ({ children }: { children: React.ReactNode }): React.JSX.Element => (
  <IntlProvider locale="en" defaultLocale="en">
    {children}
  </IntlProvider>
);

const target = { id: 'invoices', name: 'Invoices', filePattern: 'inv-*.pdf' };

describe('AddDocumentsModals, pattern dialog', () => {
  beforeEach(() => {
    graphql.mockReset();
  });

  it('prefills the set’s pattern and will not submit until a check finds files', async () => {
    graphql.mockImplementation(async ({ query }: { query: string }) => {
      if (query === 'listBucketFiles') return { data: { listBucketFiles: ['inv-1.pdf', 'inv-2.pdf'] } };
      if (query === 'addDocumentsToTestSet')
        return { data: { addDocumentsToTestSet: { id: 'invoices', name: 'Invoices', fileCount: 12 } } };
      throw new Error(`unexpected ${query}`);
    });
    const onSubmitted = vi.fn();
    render(<AddDocumentsModals testSet={target} mode="pattern" onDismiss={vi.fn()} onSubmitted={onSubmitted} />, { wrapper: English });

    const pattern = screen.getByDisplayValue('inv-*.pdf');
    expect(pattern).toBeTruthy();
    const submit = screen.getByRole('button', { name: 'Add Documents' });
    expect(submit).toHaveProperty('disabled', true);

    fireEvent.click(screen.getByRole('button', { name: 'Check Files' }));
    await waitFor(() => expect(screen.getByText('2 files to add')).toBeTruthy());
    expect(graphql).toHaveBeenCalledWith(
      expect.objectContaining({
        query: 'listBucketFiles',
        variables: expect.objectContaining({ bucketType: 'input', filePattern: 'inv-*.pdf' }),
      }),
    );
    expect(submit).toHaveProperty('disabled', false);

    fireEvent.click(submit);
    await waitFor(() => expect(onSubmitted).toHaveBeenCalledTimes(1));
    expect(graphql).toHaveBeenCalledWith(
      expect.objectContaining({
        query: 'addDocumentsToTestSet',
        variables: expect.objectContaining({ testSetId: 'invoices', filePattern: 'inv-*.pdf', bucketType: 'input', fileCount: 2 }),
      }),
    );
    const result = onSubmitted.mock.calls[0][0];
    expect(result.kind).toBe('pattern');
    expect(result.testSet).toMatchObject({ id: 'invoices', fileCount: 12 });
    expect(result.message).toMatch(/Adding documents to test set "Invoices"/);
  });

  it('reports a failed add inside the dialog rather than closing it', async () => {
    graphql.mockImplementation(async ({ query }: { query: string }) => {
      if (query === 'listBucketFiles') return { data: { listBucketFiles: ['x.pdf'] } };
      throw new Error('is not in COMPLETED status');
    });
    const onSubmitted = vi.fn();
    render(<AddDocumentsModals testSet={target} mode="pattern" onDismiss={vi.fn()} onSubmitted={onSubmitted} />, { wrapper: English });
    fireEvent.click(screen.getByRole('button', { name: 'Check Files' }));
    await waitFor(() => expect(screen.getByText('1 file to add')).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: 'Add Documents' }));
    await waitFor(() => expect(screen.getByText(/Failed to add documents: .*COMPLETED/)).toBeTruthy());
    expect(onSubmitted).not.toHaveBeenCalled();
  });
});

describe('AddDocumentsModals, Check Files counts what the copy adds', () => {
  beforeEach(() => {
    graphql.mockReset();
  });

  const listing = (matches: string[], setFiles: string[]) =>
    graphql.mockImplementation(async ({ query, variables }: { query: string; variables: { bucketType: string; filePattern: string } }) => {
      if (query !== 'listBucketFiles') throw new Error(`unexpected ${query}`);
      if (variables.bucketType === 'testset' && variables.filePattern === 'invoices/input/**') {
        return { data: { listBucketFiles: setFiles } };
      }
      return { data: { listBucketFiles: matches } };
    });

  it('leaves out Input-bucket .zip files and files the set already holds', async () => {
    listing(['inv-1.pdf', 'inv-2.pdf', 'inv-3.pdf', 'inv-all.zip'], ['invoices/input/inv-1.pdf']);
    render(<AddDocumentsModals testSet={target} mode="pattern" onDismiss={vi.fn()} onSubmitted={vi.fn()} />, { wrapper: English });
    fireEvent.click(screen.getByRole('button', { name: 'Check Files' }));
    await waitFor(() => expect(screen.getByText('2 files to add')).toBeTruthy());
    expect(screen.getByText('(1 already in the set and 1 .zip archive left out)')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Add Documents' })).toHaveProperty('disabled', false);
  });

  it('will not submit when every match would be left out, and says why', async () => {
    listing(['inv-1.pdf', 'inv-all.zip'], ['invoices/input/inv-1.pdf']);
    render(<AddDocumentsModals testSet={target} mode="pattern" onDismiss={vi.fn()} onSubmitted={vi.fn()} />, { wrapper: English });
    fireEvent.click(screen.getByRole('button', { name: 'Check Files' }));
    await waitFor(() =>
      expect(
        screen.getByText(/None of the 2 matching files would be added \(1 already in the set and 1 \.zip archive left out\)/),
      ).toBeTruthy(),
    );
    expect(screen.getByRole('button', { name: 'Add Documents' })).toHaveProperty('disabled', true);
  });

  it('forgets the result when the pattern changes', async () => {
    listing(['inv-1.pdf'], ['invoices/input/inv-1.pdf']);
    render(<AddDocumentsModals testSet={target} mode="pattern" onDismiss={vi.fn()} onSubmitted={vi.fn()} />, { wrapper: English });
    fireEvent.click(screen.getByRole('button', { name: 'Check Files' }));
    await waitFor(() => expect(screen.getByText(/None of the 1 matching file would be added/)).toBeTruthy());
    fireEvent.change(screen.getByDisplayValue('inv-*.pdf'), { target: { value: 'inv-2*.pdf' } });
    expect(screen.queryByText(/None of the 1 matching file would be added/)).toBeNull();
  });

  it('lists the set’s members without the time filter, which applies only to the pattern', async () => {
    listing(['inv-1.pdf'], []);
    render(<AddDocumentsModals testSet={target} mode="pattern" onDismiss={vi.fn()} onSubmitted={vi.fn()} />, { wrapper: English });
    const filter = createWrapper(document.body).findModal()!.findContent().findAllSelects()[1];
    filter.openDropdown();
    filter.selectOption(2);
    fireEvent.click(screen.getByRole('button', { name: 'Check Files' }));
    await waitFor(() => expect(screen.getByText('1 file to add')).toBeTruthy());
    const calls = graphql.mock.calls.map(([arg]) => arg as { variables: Record<string, unknown> });
    const patternCall = calls.find((c) => c.variables.filePattern === 'inv-*.pdf');
    const memberCall = calls.find((c) => c.variables.filePattern === 'invoices/input/**');
    expect(patternCall?.variables.modifiedAfter).toBeTruthy();
    expect(memberCall?.variables).toEqual({ bucketType: 'testset', filePattern: 'invoices/input/**' });
  });

  it('forgets the result when the time filter changes', async () => {
    listing(['inv-1.pdf', 'inv-2.pdf'], []);
    render(<AddDocumentsModals testSet={target} mode="pattern" onDismiss={vi.fn()} onSubmitted={vi.fn()} />, { wrapper: English });
    fireEvent.click(screen.getByRole('button', { name: 'Check Files' }));
    await waitFor(() => expect(screen.getByText('2 files to add')).toBeTruthy());
    const filter = createWrapper(document.body).findModal()!.findContent().findAllSelects()[1];
    filter.openDropdown();
    filter.selectOption(2);
    expect(screen.queryByText('2 files to add')).toBeNull();
  });

  it('forgets the result when the source bucket changes', async () => {
    listing(['inv-1.pdf', 'inv-2.pdf'], []);
    render(<AddDocumentsModals testSet={target} mode="pattern" onDismiss={vi.fn()} onSubmitted={vi.fn()} />, { wrapper: English });
    fireEvent.click(screen.getByRole('button', { name: 'Check Files' }));
    await waitFor(() => expect(screen.getByText('2 files to add')).toBeTruthy());
    const source = createWrapper(document.body).findModal()!.findContent().findSelect()!;
    source.openDropdown();
    source.selectOptionByValue('testset');
    expect(screen.queryByText('2 files to add')).toBeNull();
    expect(screen.getByRole('button', { name: 'Add Documents' })).toHaveProperty('disabled', true);
  });
});

describe('AddDocumentsModals, a Check Files reply that lands after an edit', () => {
  beforeEach(() => {
    graphql.mockReset();
  });

  /** Hold the pattern listing until the test releases it; the member listing answers at once. */
  const deferredPatternListing = (): { resolve: (files: string[]) => void; fail: (message: string) => void } => {
    let settle: { resolve: (files: string[]) => void; fail: (message: string) => void } = {
      resolve: () => undefined,
      fail: () => undefined,
    };
    graphql.mockImplementation(({ query, variables }: { query: string; variables: { bucketType: string } }) => {
      if (query !== 'listBucketFiles') throw new Error(`unexpected ${query}`);
      if (variables.bucketType === 'testset') return Promise.resolve({ data: { listBucketFiles: [] } });
      return new Promise((resolve, reject) => {
        settle = {
          resolve: (files) => resolve({ data: { listBucketFiles: files } }),
          fail: (message) => reject(new Error(message)),
        };
      });
    });
    return { resolve: (files) => settle.resolve(files), fail: (message) => settle.fail(message) };
  };

  it('is discarded, so the edited pattern shows no count and cannot be submitted', async () => {
    const listing = deferredPatternListing();
    render(<AddDocumentsModals testSet={target} mode="pattern" onDismiss={vi.fn()} onSubmitted={vi.fn()} />, { wrapper: English });
    fireEvent.click(screen.getByRole('button', { name: 'Check Files' }));
    fireEvent.change(screen.getByDisplayValue('inv-*.pdf'), { target: { value: 'inv-2*.pdf' } });

    await act(async () => listing.resolve(['inv-1.pdf', 'inv-2.pdf']));

    expect(screen.queryByText('2 files to add')).toBeNull();
    expect(screen.getByRole('button', { name: 'Add Documents' })).toHaveProperty('disabled', true);
  });

  it('is discarded when it is a failure, too', async () => {
    const listing = deferredPatternListing();
    render(<AddDocumentsModals testSet={target} mode="pattern" onDismiss={vi.fn()} onSubmitted={vi.fn()} />, { wrapper: English });
    fireEvent.click(screen.getByRole('button', { name: 'Check Files' }));
    fireEvent.change(screen.getByDisplayValue('inv-*.pdf'), { target: { value: 'inv-2*.pdf' } });

    await act(async () => listing.fail('throttled'));

    expect(screen.queryByText(/Failed to check files/)).toBeNull();
  });

  it('is discarded after the dialog was closed and reopened', async () => {
    const listing = deferredPatternListing();
    const { rerender } = render(<AddDocumentsModals testSet={target} mode="pattern" onDismiss={vi.fn()} onSubmitted={vi.fn()} />, {
      wrapper: English,
    });
    fireEvent.click(screen.getByRole('button', { name: 'Check Files' }));
    rerender(<AddDocumentsModals testSet={target} mode={null} onDismiss={vi.fn()} onSubmitted={vi.fn()} />);
    rerender(<AddDocumentsModals testSet={target} mode="pattern" onDismiss={vi.fn()} onSubmitted={vi.fn()} />);

    await act(async () => listing.resolve(['inv-1.pdf', 'inv-2.pdf']));

    expect(screen.queryByText('2 files to add')).toBeNull();
  });

  it('still lands when nothing was edited', async () => {
    const listing = deferredPatternListing();
    render(<AddDocumentsModals testSet={target} mode="pattern" onDismiss={vi.fn()} onSubmitted={vi.fn()} />, { wrapper: English });
    fireEvent.click(screen.getByRole('button', { name: 'Check Files' }));

    await act(async () => listing.resolve(['inv-1.pdf', 'inv-2.pdf']));

    await waitFor(() => expect(screen.getByText('2 files to add')).toBeTruthy());
  });
});

describe('AddDocumentsModals, pattern dialog text (#1817)', () => {
  it('says documents without ground truth from the input bucket are added unlabeled, not excluded', () => {
    render(<AddDocumentsModals testSet={target} mode="pattern" onDismiss={vi.fn()} onSubmitted={vi.fn()} />, { wrapper: English });
    // The Input Bucket is the default source, which is when this note is shown.
    expect(screen.getByText(/Files without matching baseline data in the evaluation bucket are added unlabeled\./)).toBeTruthy();
    expect(screen.queryByText(/excluded/i)).toBeNull();
  });
});

describe('AddDocumentsModals, zip dialog', () => {
  const realFetch = globalThis.fetch;
  const fetchMock = vi.fn();
  beforeEach(() => {
    graphql.mockReset();
    fetchMock.mockReset();
    globalThis.fetch = fetchMock as unknown as typeof fetch;
  });
  afterEach(() => {
    globalThis.fetch = realFetch;
  });

  it('asks for a presigned post for the set, uploads to it, and reports the set as updating', async () => {
    graphql.mockResolvedValue({
      data: {
        addDocumentsToTestSetFromUpload: {
          presignedUrl: JSON.stringify({ url: 'https://bucket.example/', fields: { key: 'invoices/more.zip' } }),
        },
      },
    });
    fetchMock.mockResolvedValue({ ok: true, status: 204, statusText: 'No Content' });
    const onSubmitted = vi.fn();
    render(<AddDocumentsModals testSet={target} mode="upload" onDismiss={vi.fn()} onSubmitted={onSubmitted} />, { wrapper: English });

    const submit = screen.getByRole('button', { name: 'Upload and Add Documents' });
    expect(submit).toHaveProperty('disabled', true);

    const file = new File([new Uint8Array([1, 2, 3])], 'more.zip', { type: 'application/zip' });
    fireEvent.change(screen.getByLabelText('Zip file'), { target: { files: [file] } });
    expect(submit).toHaveProperty('disabled', false);

    fireEvent.click(submit);
    await waitFor(() => expect(onSubmitted).toHaveBeenCalledTimes(1));
    expect(graphql).toHaveBeenCalledWith(
      expect.objectContaining({
        query: 'addDocumentsToTestSetFromUpload',
        variables: { input: { testSetId: 'invoices', fileName: 'more.zip', fileSize: 3 } },
      }),
    );
    expect(fetchMock).toHaveBeenCalledWith('https://bucket.example/', expect.objectContaining({ method: 'POST' }));
    expect(onSubmitted.mock.calls[0][0]).toMatchObject({ kind: 'upload', testSet: { id: 'invoices', status: 'UPDATING' } });
  });

  it('refuses a zip over the size limit without calling the server', () => {
    render(<AddDocumentsModals testSet={target} mode="upload" onDismiss={vi.fn()} onSubmitted={vi.fn()} />, { wrapper: English });
    const big = new File([], 'huge.zip');
    Object.defineProperty(big, 'size', { value: 1073741825 });
    fireEvent.change(screen.getByLabelText('Zip file'), { target: { files: [big] } });
    expect(screen.getByText(/exceeds maximum limit of 1 GB/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Upload and Add Documents' })).toHaveProperty('disabled', true);
    expect(graphql).not.toHaveBeenCalled();
  });
});
