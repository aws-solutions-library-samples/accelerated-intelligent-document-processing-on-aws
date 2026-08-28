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

import { DocumentsCommonHeader, type MappedDocument } from '../documents-table-config';

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
