// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Reprocess from the document page. What has to hold: the dialog's choice (profile,
 * revision, start step) reaches the reprocess call; the dialog is handed the result so
 * it can show a refusal; the page shows the document as queued only when a run was
 * queued, clearing its sections and reports only for a run from the beginning (a later
 * start keeps what the earlier steps produced); and a refused or failed reprocess
 * leaves the page as it was.
 */

import React from 'react';
import { act, render, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

type Confirm = (version?: string, revision?: number, startStep?: string) => Promise<unknown>;
type PanelItem = { objectKey: string; objectStatus: string; sections: unknown[]; pages: unknown[]; summaryReportUri?: string };

const state = vi.hoisted(() => ({
  getDocumentDetailsFromIds: vi.fn(),
  reprocessDocuments: vi.fn(),
  panel: null as null | { item: PanelItem },
  reprocessModal: null as null | { onConfirm: Confirm; visible: boolean; isLoading: boolean },
}));

vi.mock('../../../contexts/documents', () => ({
  default: () => ({
    documents: [],
    getDocumentDetailsFromIds: (...a: unknown[]) => state.getDocumentDetailsFromIds(...a),
    setToolsOpen: vi.fn(),
    deleteDocuments: vi.fn(),
    reprocessDocuments: (...a: unknown[]) => state.reprocessDocuments(...a),
    abortWorkflows: vi.fn(),
  }),
}));
vi.mock('../../document-panel', () => ({
  default: (props: { item: PanelItem }) => {
    state.panel = props;
    return <div data-testid="panel">{props.item.objectKey}</div>;
  },
}));
vi.mock('../../common/DeleteDocumentModal', () => ({ default: () => null }));
vi.mock('../../common/ReprocessDocumentModal', () => ({
  default: (props: NonNullable<typeof state.reprocessModal>) => {
    state.reprocessModal = props;
    return null;
  },
}));
vi.mock('../../common/AbortWorkflowModal', () => ({ default: () => null }));

import DocumentDetails from '../DocumentDetails';

const RICH = {
  ObjectKey: 'a.pdf',
  ObjectStatus: 'COMPLETED',
  Sections: [{ Id: '1', Class: 'W2', PageIds: ['1'] }],
  Pages: [{ Id: '1', Class: 'W2' }],
  SummaryReportUri: 's3://out/a.pdf/summary/summary.md',
};

const openPage = async () => {
  render(
    <MemoryRouter initialEntries={['/documents/a.pdf']}>
      <Routes>
        <Route path="/documents/*" element={<DocumentDetails />} />
      </Routes>
    </MemoryRouter>,
  );
  await waitFor(() => expect(state.panel).not.toBeNull());
};
const item = () => state.panel!.item;
const confirm = async (...args: Parameters<Confirm>) => {
  let answer: unknown;
  await act(async () => {
    answer = await state.reprocessModal!.onConfirm(...args);
  });
  return answer;
};

beforeEach(() => {
  document.body.innerHTML = '';
  state.panel = null;
  state.reprocessModal = null;
  state.getDocumentDetailsFromIds.mockReset().mockResolvedValue([RICH]);
  state.reprocessDocuments.mockReset();
});

describe('Reprocess on the document page', () => {
  it('sends the dialog’s profile, revision and start step, and hands the result back to the dialog', async () => {
    const results = [{ objectKey: 'a.pdf', status: 'QUEUED', startStep: 'extraction' }];
    state.reprocessDocuments.mockResolvedValue(results);
    await openPage();

    const answer = await confirm('claims', 2, 'extraction');

    expect(state.reprocessDocuments).toHaveBeenCalledWith(['a.pdf'], 'claims', 2, 'extraction');
    expect(answer).toEqual(results);
    expect(state.reprocessModal!.isLoading).toBe(false);
  });

  it.each([[undefined], ['beginning']])(
    'shows a run from the beginning (%s) as queued, with nothing left of the last run',
    async (startStep) => {
      state.reprocessDocuments.mockResolvedValue([{ objectKey: 'a.pdf', status: 'QUEUED' }]);
      await openPage();
      expect(item().sections).toHaveLength(1);

      await confirm('claims', undefined, startStep);

      expect(item().objectStatus).toBe('QUEUED');
      expect(item().sections).toEqual([]);
      expect(item().pages).toEqual([]);
      expect(item().summaryReportUri).toBe('');
    },
  );

  it('shows a run from a later step as queued and keeps what the earlier steps produced', async () => {
    state.reprocessDocuments.mockResolvedValue([{ objectKey: 'a.pdf', status: 'QUEUED', startStep: 'summarization' }]);
    await openPage();

    await confirm('claims', undefined, 'summarization');

    expect(item().objectStatus).toBe('QUEUED');
    expect(item().sections).toHaveLength(1);
    expect(item().pages).toHaveLength(1);
    expect(item().summaryReportUri).toBe('s3://out/a.pdf/summary/summary.md');
  });

  it.each([
    ['refused', [{ objectKey: 'a.pdf', status: 'REFUSED', reason: 'RUN_IN_PROGRESS' }]],
    ['failed to queue', [{ objectKey: 'a.pdf', status: 'FAILED' }]],
  ])('leaves the page as it was when the document was %s, and gives the dialog the result to show', async (_what, results) => {
    state.reprocessDocuments.mockResolvedValue(results);
    await openPage();

    const answer = await confirm('claims', undefined, 'beginning');

    expect(answer).toEqual(results);
    expect(item().objectStatus).toBe('COMPLETED');
    expect(item().sections).toHaveLength(1);
  });

  it('leaves the page as it was when the request failed, and tells the dialog there is nothing to show', async () => {
    state.reprocessDocuments.mockResolvedValue(false);
    await openPage();

    const answer = await confirm('claims');

    expect(answer).toBeNull();
    expect(item().objectStatus).toBe('COMPLETED');
    expect(item().sections).toHaveLength(1);
    expect(state.reprocessModal!.isLoading).toBe(false);
  });

  it('shows the dialog as working while the request runs', async () => {
    let finish: (value: unknown) => void = () => undefined;
    state.reprocessDocuments.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    await openPage();

    let pending: Promise<unknown> = Promise.resolve();
    act(() => {
      pending = state.reprocessModal!.onConfirm('claims');
    });
    await waitFor(() => expect(state.reprocessModal!.isLoading).toBe(true));
    await act(async () => {
      finish([{ objectKey: 'a.pdf', status: 'QUEUED' }]);
      await pending;
    });

    expect(state.reprocessModal!.isLoading).toBe(false);
  });
});
