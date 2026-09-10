// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The shared Add documents dialogs, now used from two pages. What has to hold:
 * the pattern dialog cannot submit before a check has found something, the
 * mutations receive the target set's id, and the caller learns enough from
 * onSubmitted to update its own view of the set.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
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
    render(<AddDocumentsModals testSet={target} mode="pattern" onDismiss={vi.fn()} onSubmitted={onSubmitted} />);

    const pattern = screen.getByDisplayValue('inv-*.pdf');
    expect(pattern).toBeTruthy();
    const submit = screen.getByRole('button', { name: 'Add Documents' });
    expect(submit).toHaveProperty('disabled', true);

    fireEvent.click(screen.getByRole('button', { name: 'Check Files' }));
    await waitFor(() => expect(screen.getByText('2 files found')).toBeTruthy());
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
    render(<AddDocumentsModals testSet={target} mode="pattern" onDismiss={vi.fn()} onSubmitted={onSubmitted} />);
    fireEvent.click(screen.getByRole('button', { name: 'Check Files' }));
    await waitFor(() => expect(screen.getByText('1 file found')).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: 'Add Documents' }));
    await waitFor(() => expect(screen.getByText(/Failed to add documents: .*COMPLETED/)).toBeTruthy());
    expect(onSubmitted).not.toHaveBeenCalled();
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
    render(<AddDocumentsModals testSet={target} mode="upload" onDismiss={vi.fn()} onSubmitted={onSubmitted} />);

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
    render(<AddDocumentsModals testSet={target} mode="upload" onDismiss={vi.fn()} onSubmitted={vi.fn()} />);
    const big = new File([], 'huge.zip');
    Object.defineProperty(big, 'size', { value: 1073741825 });
    fireEvent.change(screen.getByLabelText('Zip file'), { target: { files: [big] } });
    expect(screen.getByText(/exceeds maximum limit of 1 GB/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Upload and Add Documents' })).toHaveProperty('disabled', true);
    expect(graphql).not.toHaveBeenCalled();
  });
});
