// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The document page before its document can be shown. What has to hold: a spinner while
 * the first read runs, and while only a document-list row is known (a partial row would
 * flash a half-filled page); "Document not found" when the read finds nothing; "Couldn't
 * open this document" with Retry when the read is refused, Retry reading again and then
 * showing the page; a new key starting over at the spinner; and a Delete that failed not
 * leaving the page.
 */

import React from 'react';
import { act, render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({
  documents: [] as Record<string, unknown>[],
  getDocumentDetailsFromIds: vi.fn(),
  deleteDocuments: vi.fn(),
  panel: null as null | { onDelete: () => void },
  deleteModal: null as null | { onConfirm: () => Promise<void> },
}));

vi.mock('../../../contexts/documents', () => ({
  default: () => ({
    documents: state.documents,
    getDocumentDetailsFromIds: (...a: unknown[]) => state.getDocumentDetailsFromIds(...a),
    setToolsOpen: vi.fn(),
    deleteDocuments: (...a: unknown[]) => state.deleteDocuments(...a),
    reprocessDocuments: vi.fn(),
    abortWorkflows: vi.fn(),
  }),
}));
vi.mock('../../document-panel', () => ({
  default: (props: { item: { objectKey: string }; onDelete: () => void }) => {
    state.panel = props;
    return <div data-testid="panel">{props.item.objectKey}</div>;
  },
}));
vi.mock('../../common/DeleteDocumentModal', () => ({
  default: (props: { onConfirm: () => Promise<void> }) => {
    state.deleteModal = props;
    return null;
  },
}));
vi.mock('../../common/ReprocessDocumentModal', () => ({ default: () => null }));
vi.mock('../../common/AbortWorkflowModal', () => ({ default: () => null }));

import DocumentDetails from '../DocumentDetails';

let navigateTo: (to: string) => void = () => undefined;
const Probe = (): React.JSX.Element => {
  navigateTo = useNavigate();
  return <div data-testid="location">{useLocation().pathname}</div>;
};

const renderAt = (path: string) =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/documents/*" element={<DocumentDetails />} />
        <Route path="/documents" element={<div />} />
      </Routes>
      <Probe />
    </MemoryRouter>,
  );

const page = () => createWrapper(document.body);
const panel = () => document.querySelector('[data-testid="panel"]');
const spinner = () => page().findSpinner();
const alertShown = () => {
  const alert = page().findAlert();
  if (!alert) throw new Error('no alert yet');
  return alert;
};

const ROW = { ObjectKey: 'a.pdf', ObjectStatus: 'COMPLETED' };
const RICH = { ...ROW, Sections: [], Pages: [] };

beforeEach(() => {
  document.body.innerHTML = '';
  state.documents = [];
  state.panel = null;
  state.deleteModal = null;
  state.getDocumentDetailsFromIds.mockReset();
  state.deleteDocuments.mockReset();
});

describe('the document page’s states', () => {
  it('shows a spinner while the first read runs, then the page', async () => {
    let finish: (docs: unknown[]) => void = () => undefined;
    state.getDocumentDetailsFromIds.mockImplementation(() => new Promise((resolve) => (finish = resolve)));
    renderAt('/documents/a.pdf');
    expect(spinner()).not.toBeNull();
    expect(document.body.textContent).toContain('Loading document…');
    expect(state.getDocumentDetailsFromIds).toHaveBeenCalledWith(['a.pdf'], expect.objectContaining({ reportErrors: false }));
    await act(async () => finish([RICH]));
    await waitFor(() => expect(panel()).not.toBeNull());
  });

  it('keeps the spinner while only a list row is known', async () => {
    state.getDocumentDetailsFromIds.mockImplementation(() => new Promise(() => undefined));
    state.documents = [ROW];
    renderAt('/documents/a.pdf');
    await act(async () => undefined);
    expect(spinner()).not.toBeNull();
    expect(panel()).toBeNull();
  });

  it('says the document does not exist when the read finds nothing', async () => {
    state.getDocumentDetailsFromIds.mockResolvedValue([]);
    renderAt('/documents/a.pdf');
    const alert = await waitFor(alertShown);
    expect(alert.findHeader()!.getElement().textContent).toBe('Document not found');
    expect(alert.findActionSlot()?.findButton() ?? null).toBeNull();
  });

  it('shows a refused read with Retry, which reads again and then shows the page', async () => {
    state.getDocumentDetailsFromIds.mockImplementationOnce(
      async (_keys: string[], options: { onError: (k: string, e: unknown) => void }) => {
        options.onError('a.pdf', new Error('Access denied'));
        return [];
      },
    );
    state.getDocumentDetailsFromIds.mockResolvedValue([RICH]);
    renderAt('/documents/a.pdf');
    const alert = await waitFor(alertShown);
    expect(alert.findHeader()!.getElement().textContent).toBe("Couldn't open this document");
    expect(alert.getElement().textContent).toContain('Access denied');
    act(() => alert.findActionSlot()!.findButton()!.click());
    await waitFor(() => expect(panel()).not.toBeNull());
    expect(state.getDocumentDetailsFromIds).toHaveBeenCalledTimes(2);
  });

  it('starts over at the spinner for a new key', async () => {
    state.getDocumentDetailsFromIds.mockResolvedValueOnce([RICH]).mockImplementation(() => new Promise(() => undefined));
    renderAt('/documents/a.pdf');
    await waitFor(() => expect(panel()).not.toBeNull());
    act(() => navigateTo('/documents/b.pdf'));
    await waitFor(() => expect(spinner()).not.toBeNull());
    expect(panel()).toBeNull();
    expect(state.getDocumentDetailsFromIds).toHaveBeenLastCalledWith(['b.pdf'], expect.objectContaining({ reportErrors: false }));
  });

  it('stays on the page when Delete failed', async () => {
    state.getDocumentDetailsFromIds.mockResolvedValue([RICH]);
    state.deleteDocuments.mockResolvedValue(false);
    renderAt('/documents/a.pdf');
    await waitFor(() => expect(panel()).not.toBeNull());
    await act(async () => state.deleteModal!.onConfirm());
    expect(state.deleteDocuments).toHaveBeenCalledWith(['a.pdf']);
    expect(document.querySelector('[data-testid="location"]')!.textContent).toBe('/documents/a.pdf');

    state.deleteDocuments.mockResolvedValue({ success: true });
    await act(async () => state.deleteModal!.onConfirm());
    expect(document.querySelector('[data-testid="location"]')!.textContent).toBe('/documents');
  });
});
