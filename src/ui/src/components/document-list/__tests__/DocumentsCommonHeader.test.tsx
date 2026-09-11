// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Component tests for the document-list header's Download menu.
 *
 * The menu carries two actions that differ in scope — the Excel export covers
 * every filtered row, the ZIP exports cover only the selection — so these tests
 * pin that both are reachable from the single menu and dispatch to the right
 * callback. CircuitBreakerBadge is mocked out: it talks to the API on mount.
 */

import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

vi.mock('../CircuitBreakerBadge', () => ({ default: () => <span /> }));

import { DocumentViewSelector, DocumentsCommonHeader, type MappedDocument } from '../documents-table-config';

const row = (objectKey: string, overrides: Partial<MappedDocument> = {}): MappedDocument =>
  ({ objectKey, objectStatus: 'COMPLETED', evaluationStatus: 'NOT_EVALUATED', ...overrides }) as MappedDocument;

const openDownloadMenu = () => {
  fireEvent.click(screen.getByRole('button', { name: /download/i }));
};

describe('DocumentsCommonHeader download menu', () => {
  it('routes the Excel item to the list export', () => {
    const downloadToExcel = vi.fn();
    const onDownloadSelected = vi.fn();
    render(
      <DocumentsCommonHeader
        selectedItems={[]}
        totalItems={[row('a.pdf'), row('b.pdf')]}
        downloadToExcel={downloadToExcel}
        onDownloadSelected={onDownloadSelected}
      />,
    );

    openDownloadMenu();
    fireEvent.click(screen.getByText('Table as Excel (2 rows)'));

    expect(downloadToExcel).toHaveBeenCalledTimes(1);
    expect(onDownloadSelected).not.toHaveBeenCalled();
  });

  it('routes each ZIP scope to the bulk export with its scope', () => {
    const downloadToExcel = vi.fn();
    const onDownloadSelected = vi.fn();
    render(
      <DocumentsCommonHeader
        selectedItems={[row('a.pdf'), row('b.pdf', { evaluationStatus: 'BASELINE_AVAILABLE' })]}
        totalItems={[row('a.pdf'), row('b.pdf')]}
        downloadToExcel={downloadToExcel}
        onDownloadSelected={onDownloadSelected}
      />,
    );

    openDownloadMenu();
    expect(screen.getByText('Selected documents (2)')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Predictions (ZIP)'));
    expect(onDownloadSelected).toHaveBeenCalledWith('predictions');

    openDownloadMenu();
    fireEvent.click(screen.getByText('Baselines (ZIP)'));
    expect(onDownloadSelected).toHaveBeenLastCalledWith('baselines');

    openDownloadMenu();
    fireEvent.click(screen.getByText('All data (ZIP)'));
    expect(onDownloadSelected).toHaveBeenLastCalledWith('all');
    expect(downloadToExcel).not.toHaveBeenCalled();
  });

  it('leaves the Excel export usable while the ZIP scopes are disabled by an empty selection', () => {
    const downloadToExcel = vi.fn();
    const onDownloadSelected = vi.fn();
    render(
      <DocumentsCommonHeader
        selectedItems={[]}
        totalItems={[row('a.pdf')]}
        downloadToExcel={downloadToExcel}
        onDownloadSelected={onDownloadSelected}
      />,
    );

    openDownloadMenu();
    fireEvent.click(screen.getByText('Predictions (ZIP)'));
    expect(onDownloadSelected).not.toHaveBeenCalled();

    fireEvent.click(screen.getByText('Table as Excel (1 row)'));
    expect(downloadToExcel).toHaveBeenCalledTimes(1);
  });
});

/**
 * The Test Studio view reaches documents whose results are scored against them, so
 * the mutating actions are held there. These pin that the hold is a disable with an
 * explanation rather than the buttons disappearing, and that it does not leak into
 * the production view.
 */
describe('DocumentsCommonHeader destructive actions', () => {
  const props = {
    selectedItems: [row('run-20260417-125337/a.pdf')],
    totalItems: [row('run-20260417-125337/a.pdf')],
    onDelete: vi.fn(),
    onReprocess: vi.fn(),
    onAbort: vi.fn(),
  };
  const actionNames = [/^delete$/i, /^reprocess$/i, /^abort$/i];

  it('disables Delete, Reprocess and Abort when a reason is given', () => {
    const onDelete = vi.fn();
    const onReprocess = vi.fn();
    const onAbort = vi.fn();
    render(
      <DocumentsCommonHeader
        {...props}
        onDelete={onDelete}
        onReprocess={onReprocess}
        onAbort={onAbort}
        destructiveDisabledReason="Managed by their test run"
      />,
    );

    for (const name of actionNames) {
      const button = screen.getByRole('button', { name });
      expect(button).toBeDisabled();
      fireEvent.click(button);
    }
    expect(onDelete).not.toHaveBeenCalled();
    expect(onReprocess).not.toHaveBeenCalled();
    expect(onAbort).not.toHaveBeenCalled();
  });

  it('surfaces the reason as the tooltip instead of hiding the buttons', () => {
    render(<DocumentsCommonHeader {...props} destructiveDisabledReason="Managed by their test run" />);

    for (const name of actionNames) {
      expect(screen.getByRole('button', { name })).toBeInTheDocument();
      expect(screen.getByRole('button', { name }).closest('span[title]')).toHaveAttribute('title', 'Managed by their test run');
    }
  });

  it('leaves the actions live for a selection in the production view', () => {
    const onDelete = vi.fn();
    render(<DocumentsCommonHeader {...props} onDelete={onDelete} destructiveDisabledReason={null} />);

    const button = screen.getByRole('button', { name: /^delete$/i });
    expect(button).not.toBeDisabled();
    fireEvent.click(button);
    expect(onDelete).toHaveBeenCalledTimes(1);
  });
});

describe('DocumentViewSelector', () => {
  it('reports the selected partition and reflects the current one', () => {
    const setDocumentView = vi.fn();
    render(<DocumentViewSelector documentView="PRODUCTION" setDocumentView={setDocumentView} />);

    fireEvent.click(screen.getByText('Test Studio'));
    expect(setDocumentView).toHaveBeenCalledWith('TEST');
  });

  it('switches back to the production partition', () => {
    const setDocumentView = vi.fn();
    render(<DocumentViewSelector documentView="TEST" setDocumentView={setDocumentView} />);

    fireEvent.click(screen.getByText('Production'));
    expect(setDocumentView).toHaveBeenCalledWith('PRODUCTION');
  });

  it('holds both segments while a load is in flight', () => {
    const setDocumentView = vi.fn();
    render(<DocumentViewSelector documentView="PRODUCTION" setDocumentView={setDocumentView} disabled />);

    fireEvent.click(screen.getByText('Test Studio'));
    expect(setDocumentView).not.toHaveBeenCalled();
  });
});
