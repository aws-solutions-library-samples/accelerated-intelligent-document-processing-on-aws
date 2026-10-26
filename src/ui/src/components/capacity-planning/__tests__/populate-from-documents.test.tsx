// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * "Populate tokens from Documents" reads processed documents' metering.
 *
 * It read the Document List's rows, which carry no `Metering` and no `Sections`, so
 * it reported "No Metering Data Found" beside a list of completed documents; and it
 * had nothing at all unless the Document List page had been opened first, because
 * only that page started the list load. It now starts the load itself and fetches
 * the full record of the most recent completed documents.
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { DETAIL_FETCH_CHUNK_SIZE, documentsNeedingDetail, fetchInChunks, MAX_DOCUMENTS_TO_DETAIL, withDetails } from '../populate-sources';

const ctx = vi.hoisted(() => ({
  value: {} as Record<string, unknown>,
}));
// Stable objects: the page re-runs effects on identity changes.
const stable = vi.hoisted(() => ({
  configuration: {
    mergedConfig: { classes: [{ name: 'Payslip' }] },
    fetchConfiguration: () => undefined,
  },
  versions: {
    versions: [{ versionName: 'default', isActive: true }],
    getVersionOptions: () => [{ label: 'default', value: 'default' }],
  },
  settings: { settings: {} },
}));

vi.mock('../../../contexts/documents', () => ({ default: () => ctx.value }));
vi.mock('../../../hooks/use-configuration', () => ({ default: () => stable.configuration }));
vi.mock('../../../hooks/use-configuration-versions', () => ({ default: () => stable.versions }));
vi.mock('../../../contexts/settings', () => ({ default: () => stable.settings }));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));

import CapacityPlanningLayout from '../CapacityPlanningLayout';

const LIST_ROW = { ObjectKey: 'docs/payslip.pdf', ObjectStatus: 'COMPLETED', CompletionTime: '2026-10-24T12:00:00Z' };
const FULL_RECORD = {
  ...LIST_ROW,
  PageCount: 1,
  Sections: [{ Id: '1', Class: 'Payslip', PageIds: ['1'] }],
  Metering: JSON.stringify({ 'Extraction/bedrock/us.amazon.nova-2-lite-v1:0': { inputTokens: 1200, outputTokens: 300 } }),
};

const contextWith = (overrides: Record<string, unknown>) => {
  ctx.value = {
    documents: [LIST_ROW],
    hasListBeenLoaded: true,
    isDocumentsListLoading: false,
    setIsDocumentsListLoading: vi.fn(),
    getDocumentDetailsFromIds: vi.fn().mockResolvedValue([FULL_RECORD]),
    periodsToLoad: 0,
    setPeriodsToLoad: vi.fn(),
    customDateRange: null,
    setCustomDateRange: vi.fn(),
    ...overrides,
  };
  return ctx.value;
};

beforeEach(() => {
  vi.stubEnv('VITE_DEFAULT_MAX_LATENCY', '7');
});

describe('Populate tokens from Documents', () => {
  it('fetches the full record of completed documents and offers them', async () => {
    const value = contextWith({});
    render(<CapacityPlanningLayout />);

    await userEvent.click(screen.getByRole('button', { name: /Populate tokens from Documents/ }));

    await waitFor(() => expect(value.getDocumentDetailsFromIds).toHaveBeenCalledWith(['docs/payslip.pdf']));
    expect(screen.queryByText('No Metering Data Found')).not.toBeInTheDocument();
    expect(await screen.findAllByText(/docs\/payslip\.pdf/)).not.toHaveLength(0);
  });

  it('requests full records a few at a time, not all at once', async () => {
    const rows = Array.from({ length: DETAIL_FETCH_CHUNK_SIZE + 2 }, (_, i) => ({ ...LIST_ROW, ObjectKey: `docs/d${i}.pdf` }));
    const value = contextWith({ documents: rows, getDocumentDetailsFromIds: vi.fn().mockResolvedValue([]) });
    render(<CapacityPlanningLayout />);

    await userEvent.click(screen.getByRole('button', { name: /Populate tokens from Documents/ }));

    const fetch = value.getDocumentDetailsFromIds as ReturnType<typeof vi.fn>;
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    expect(fetch.mock.calls.map(([keys]) => (keys as string[]).length)).toEqual([DETAIL_FETCH_CHUNK_SIZE, 2]);
  });

  it('starts the document list load when no page has asked for it', () => {
    const value = contextWith({ documents: [], hasListBeenLoaded: false });
    render(<CapacityPlanningLayout />);

    expect(value.setIsDocumentsListLoading).toHaveBeenCalledWith(true);
  });
});

describe('documentsNeedingDetail', () => {
  it('picks the most recent completed rows that have no metering, up to the limit', () => {
    const rows = [
      { ObjectKey: 'old.pdf', ObjectStatus: 'COMPLETED', CompletionTime: '2026-01-01T00:00:00Z' },
      { ObjectKey: 'new.pdf', ObjectStatus: 'COMPLETED', CompletionTime: '2026-10-01T00:00:00Z' },
      { ObjectKey: 'running.pdf', ObjectStatus: 'RUNNING' },
      { ObjectKey: 'metered.pdf', ObjectStatus: 'COMPLETED', Metering: '{}' },
    ];

    expect(documentsNeedingDetail(rows)).toEqual(['new.pdf', 'old.pdf']);
    expect(documentsNeedingDetail(rows, 1)).toEqual(['new.pdf']);
  });

  it('caps the fetch', () => {
    const rows = Array.from({ length: MAX_DOCUMENTS_TO_DETAIL + 5 }, (_, i) => ({ ObjectKey: `d${i}.pdf`, ObjectStatus: 'COMPLETED' }));

    expect(documentsNeedingDetail(rows)).toHaveLength(MAX_DOCUMENTS_TO_DETAIL);
  });
});

describe('withDetails', () => {
  it('replaces fetched rows and keeps the rest', () => {
    const merged = withDetails([{ ObjectKey: 'a' }, { ObjectKey: 'b' }], [{ ObjectKey: 'b', extra: 1 } as { ObjectKey: string }]);

    expect(merged).toEqual([{ ObjectKey: 'a' }, { ObjectKey: 'b', extra: 1 }]);
  });
});

describe('fetchInChunks', () => {
  it('fetches in rounds and concatenates the results in order', async () => {
    const fetchDetails = vi.fn(async (chunk: string[]) => chunk.map((key) => key.toUpperCase()));

    expect(await fetchInChunks(['a', 'b', 'c'], fetchDetails, 2)).toEqual(['A', 'B', 'C']);
    expect(fetchDetails.mock.calls).toEqual([[['a', 'b']], [['c']]]);
  });

  it('makes no request for no keys', async () => {
    const fetchDetails = vi.fn();

    expect(await fetchInChunks([], fetchDetails)).toEqual([]);
    expect(fetchDetails).not.toHaveBeenCalled();
  });
});
