// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * More Actions › Edit Page Text. What has to hold: Save and Reprocess uploads the page's
 * text, then sends processChanges with `textModified` for that page, in that order; an
 * upload that fails sends nothing and says nothing was changed; a reprocess that fails
 * after the upload keeps the editor open, saying so, where Retry reprocess sends
 * processChanges again with no new upload and Restore original text uploads the text as
 * it was opened; closing then leaves a notice on the page with Retry reprocess; it is
 * offered to Admins only (uploadDocument and processChanges admit no other role in
 * common); and it is disabled, with its reason, for a past version, a document in a review
 * queue (open again once the queue review has finished) and in BDA mode (replacing, with
 * reset-page-class.test.tsx, PagesPanel.readonly and PagesPanel.reviewQueue).
 */

import React from 'react';
import { act, render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({
  graphql: vi.fn(),
  role: {} as Record<string, unknown>,
  config: { mergedConfig: {} as Record<string, unknown> | null, loading: false },
  polling: [] as { enabled?: boolean }[],
  setErrorMessage: vi.fn(),
  setSuccessMessage: vi.fn(),
  flowVisible: false,
  troubleshootVisible: false,
  board: null as null | Record<string, unknown>,
  versionsPanel: null as null | Record<string, unknown>,
}));

vi.mock('@monaco-editor/react', () => ({
  Editor: ({ value, onChange }: { value: string; onChange: (v: string) => void }) => (
    <textarea data-testid="text-editor" value={value} onChange={(e) => onChange(e.target.value)} />
  ),
}));
vi.mock('../../common/PageImageViewer', () => ({ default: () => <div data-testid="page-image" /> }));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => state.graphql(...a) }) }));
vi.mock('../../../hooks/use-configuration-versions', () => ({
  default: () => ({ versions: [], loaded: true, loading: false, error: null }),
}));
vi.mock('../../../hooks/use-configuration', () => ({ default: () => state.config }));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => state.role }));
vi.mock('../../../hooks/use-pricing', () => ({ default: () => ({ pricing: null, nonChargeableUnits: [], loading: false }) }));
vi.mock('../../../hooks/use-polling', () => ({
  default: (_fn: unknown, options: { enabled?: boolean }) => {
    state.polling.push(options);
  },
}));
vi.mock('../../../hooks/use-classification-comparison', () => ({ default: () => null }));
vi.mock('../../../contexts/app', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../contexts/app')>()),
  default: () => ({ currentCredentials: null, setErrorMessage: state.setErrorMessage, setSuccessMessage: state.setSuccessMessage }),
}));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../../sections-panel', () => ({ default: () => <div data-testid="sections-panel" /> }));
vi.mock('../../pages-panel', () => ({ default: () => <div data-testid="pages-panel" /> }));
vi.mock('../../chat-panel', () => ({ default: () => <div data-testid="chat-panel" /> }));
vi.mock('../../document-panel/DocumentVersionsPanel', () => ({
  default: (props: Record<string, unknown>) => {
    state.versionsPanel = props;
    return null;
  },
}));
vi.mock('../../document-panel/DocumentDownloadModals', () => ({ DownloadOptionsModal: () => null, DownloadProgressModal: () => null }));
vi.mock('../../document-viewer/FileViewer', () => ({ default: () => <div data-testid="source-viewer" /> }));
vi.mock('../../document-viewer/MarkdownViewer', () => ({
  default: () => <div data-testid="markdown" />,
  MarkdownReport: ({ title, emptyMessage, reportUri }: { title: string; emptyMessage: string; reportUri: string }) => (
    <div data-testid="markdown-report">
      {title}|{emptyMessage}|{reportUri}
    </div>
  ),
}));
vi.mock('../../document-viewer/EvaluationReport', () => ({
  default: ({ reportUri }: { reportUri: string }) => <div data-testid="evaluation-report">{reportUri}</div>,
}));
vi.mock('../../step-function-flow/StepFunctionFlowViewer', () => ({
  default: ({ visible }: { visible: boolean }) => {
    state.flowVisible = visible;
    return null;
  },
}));
vi.mock('../../document-panel/TroubleshootModal', () => ({
  default: ({ visible }: { visible: boolean }) => {
    state.troubleshootVisible = visible;
    return null;
  },
}));

import DocumentPanel from '../../document-panel/DocumentPanel';
import { detailBar } from './detail-bar';
import { fireEvent } from '@testing-library/react';
import { inReviewQueueEditReason } from '../../common/review-status';
import { BDA_REPROCESS_REASON, HISTORICAL_REASON } from '../action-reasons';

const ADMIN = {
  isAdmin: true,
  isAuthor: false,
  isReviewer: false,
  isAnnotator: false,
  isViewer: false,
  isReviewerOnly: false,
  canWorkReviewQueues: true,
  allowedConfigVersions: null,
  loading: false,
};

const doc = (over: Record<string, unknown> = {}) => ({
  objectKey: 'in/packet.pdf',
  objectStatus: 'COMPLETED',
  evaluationStatus: 'COMPLETED',
  sections: [
    { Id: '1', Class: 'FieldTicket', PageIds: ['1', '2', '3'] },
    { Id: '2', Class: 'Invoice', PageIds: ['4'] },
  ],
  pages: [1, 2, 3, 4].map((Id) => ({
    Id,
    Class: Id < 4 ? 'FieldTicket' : 'Invoice',
    ImageUri: `s3://out/in/packet.pdf/pages/${Id}/image.jpg`,
    TextUri: `s3://out/in/packet.pdf/pages/${Id}/result.json`,
  })),
  ...over,
});

const getDocumentDetailsFromIds = vi.fn();
const renderPanel = (item = doc()) =>
  render(
    <DocumentPanel
      item={item as never}
      getDocumentDetailsFromIds={getDocumentDetailsFromIds}
      onDelete={vi.fn()}
      onReprocess={vi.fn()}
      onAbort={vi.fn()}
    />,
  );

/** The variables of each call that sent `op`, in order. */
const sent = (op: string) =>
  state.graphql.mock.calls.filter(([arg]) => String(arg.query).includes(`${op}(`)).map(([arg]) => arg.variables as Record<string, unknown>);

/** For waitFor: the element, or a throw until it is there. */
const shown = <T,>(found: T | null | undefined): T => {
  if (!found) throw new Error('not shown yet');
  return found;
};

const modalTitled = (title: string) =>
  createWrapper(document.body)
    .findAllModals()
    .find((m) => m.findHeader().getElement().textContent === title);

const footerButton = (title: string, label: string) =>
  modalTitled(title)!
    .findFooter()!
    .findAllButtons()
    .find((b) => (b.findTextRegion()?.getElement().textContent ?? b.getElement().textContent) === label)!;

/** View a past version, as Version History does. */
const viewPastVersion = () =>
  act(() =>
    (state.versionsPanel!.onViewVersion as (runId: string, detail: Record<string, unknown>) => void)('run-1', {
      Sections: [],
      Pages: [],
      Files: [],
    }),
  );

const moreItem = (text: string) => detailBar.menuItems('More Actions').find((i) => i.text === text);

const routes: Record<string, () => unknown> = {};
beforeEach(() => {
  document.body.innerHTML = '';
  state.role = { ...ADMIN };
  state.config = { mergedConfig: { classes: [{ $id: 'FieldTicket' }, { $id: 'Invoice' }] }, loading: false };
  state.board = null;
  state.setErrorMessage.mockReset();
  state.setSuccessMessage.mockReset();
  getDocumentDetailsFromIds.mockReset().mockResolvedValue([]);
  for (const k of Object.keys(routes)) delete routes[k];
  state.graphql.mockReset().mockImplementation(async ({ query }: { query: string }) => {
    const q = String(query);
    for (const [op, answer] of Object.entries(routes)) if (q.includes(`${op}(`)) return answer();
    if (q.includes('updateDocumentSections('))
      return { data: { updateDocumentSections: { success: true, message: 'Page grouping saved' } } };
    if (q.includes('processChanges(')) return { data: { processChanges: { success: true } } };
    return { data: {} };
  });
});

const REVIEWER = { ...ADMIN, isAdmin: false, isReviewer: true, isReviewerOnly: true, canWorkReviewQueues: false };
const TITLE = 'Edit Page 1 Text';
const uploads: string[] = [];

beforeEach(() => {
  uploads.length = 0;
  routes.getFileContents = () => ({ data: { getFileContents: { content: JSON.stringify({ text: 'original text' }), isBinary: false } } });
  routes.uploadDocument = () => ({
    data: {
      uploadDocument: { presignedUrl: JSON.stringify({ url: 'https://upload.example', fields: { key: 'k' } }), usePostMethod: 'true' },
    },
  });
  global.fetch = vi.fn(async (_url: unknown, init?: { body?: FormData }) => {
    const file = init?.body?.get('file') as Blob | null;
    uploads.push(file ? await file.text() : '');
    return { ok: true, text: async () => '' };
  }) as never;
});

const openEditor = async () => {
  renderPanel();
  detailBar.chooseMenuItem('More Actions', 'Edit Page Text');
  const dialog = await waitFor(() => shown(modalTitled(TITLE)));
  // The page has no OCR lines, so the editor opens on Markdown; its raw source is the editable view.
  await waitFor(() => expect(dialog.getElement().textContent).toContain('Raw (editable)'));
  const raw = [...dialog.getElement().querySelectorAll('button')].find((b) => b.textContent === 'Raw (editable)')!;
  act(() => raw.click());
  const editor = await waitFor(() => shown(document.querySelector('[data-testid="text-editor"]') as HTMLTextAreaElement | null));
  act(() => {
    fireEvent.change(editor, { target: { value: 'corrected text' } });
  });
  return dialog;
};
const save = () => footerButton(TITLE, 'Save and Reprocess');
const alertAction = (label: string) =>
  [...modalTitled(TITLE)!.getElement().querySelectorAll('button')].find((b) => b.textContent === label) as HTMLButtonElement;

describe('Edit Page Text', () => {
  it('says what Save and Reprocess does, uploads the text, then reprocesses that page', async () => {
    const dialog = await openEditor();
    expect(dialog.getElement().textContent).toContain(
      "Save and Reprocess saves this page's text, then extracts again the sections that hold the page, replacing their values",
    );
    await act(async () => save().click());
    await waitFor(() => expect(sent('processChanges')).toHaveLength(1));
    expect(uploads).toEqual([JSON.stringify({ text: 'corrected text' }, null, 2)]);
    const calls = state.graphql.mock.calls.map(([arg]) => String(arg.query)).filter((q) => /uploadDocument\(|processChanges\(/.test(q));
    expect(calls.map((q) => (q.includes('uploadDocument(') ? 'upload' : 'process'))).toEqual(['upload', 'process']);
    expect(sent('processChanges')[0]).toEqual({
      objectKey: 'in/packet.pdf',
      modifiedSections: [],
      modifiedPages: [{ pageId: 1, textModified: true, classReset: false, newTextUri: 's3://out/in/packet.pdf/pages/1/result.json' }],
    });
    expect(state.setSuccessMessage).toHaveBeenCalledWith(
      "Saved page 1's text of packet.pdf. The sections that hold it are being extracted again.",
    );
    await waitFor(() => expect(modalTitled(TITLE)).toBeUndefined());
  });

  it('sends nothing and says nothing changed when the upload fails', async () => {
    global.fetch = vi.fn(async () => ({ ok: false, text: async () => 'denied' })) as never;
    await openEditor();
    await act(async () => save().click());
    await waitFor(() =>
      expect(modalTitled(TITLE)!.getElement().textContent).toContain(
        "Could not save page 1's text: Upload failed: denied. Nothing was changed.",
      ),
    );
    expect(sent('processChanges')).toEqual([]);
  });

  it('keeps the editor open when the reprocess fails; Retry reprocess sends no new upload', async () => {
    let refuse = true;
    routes.processChanges = () =>
      refuse ? { data: { processChanges: { success: false, message: 'Throttled' } } } : { data: { processChanges: { success: true } } };
    await openEditor();
    await act(async () => save().click());
    await waitFor(() =>
      expect(modalTitled(TITLE)!.getElement().textContent).toContain(
        'The page text was saved, but the document was not reprocessed: Throttled',
      ),
    );
    expect(uploads).toHaveLength(1);
    refuse = false;
    await act(async () => alertAction('Retry reprocess').click());
    await waitFor(() => expect(sent('processChanges')).toHaveLength(2));
    expect(uploads).toHaveLength(1);
    await waitFor(() => expect(modalTitled(TITLE)).toBeUndefined());
  });

  it('Restore original text uploads the text as it was opened, and reprocesses nothing', async () => {
    routes.processChanges = () => ({ data: { processChanges: { success: false, message: 'Throttled' } } });
    await openEditor();
    await act(async () => save().click());
    await waitFor(() => expect(alertAction('Restore original text')).toBeDefined());
    await act(async () => alertAction('Restore original text').click());
    await waitFor(() =>
      expect(modalTitled(TITLE)!.getElement().textContent).toContain("Page 1's original text is restored. Nothing was reprocessed."),
    );
    expect(uploads).toEqual([JSON.stringify({ text: 'corrected text' }, null, 2), JSON.stringify({ text: 'original text' }, null, 2)]);
    expect(sent('processChanges')).toHaveLength(1);
  });

  it('leaves a notice with Retry reprocess when closed with the text saved but not reprocessed', async () => {
    let refuse = true;
    routes.processChanges = () =>
      refuse ? { data: { processChanges: { success: false, message: 'Throttled' } } } : { data: { processChanges: { success: true } } };
    await openEditor();
    await act(async () => save().click());
    await waitFor(() => expect(alertAction('Retry reprocess')).toBeDefined());
    await act(async () => footerButton(TITLE, 'Close').click());
    await waitFor(() => expect(modalTitled(TITLE)).toBeUndefined());
    expect(document.body.textContent).toContain("Page 1's text was saved, but packet.pdf was not reprocessed.");
    refuse = false;
    const retry = [...document.querySelectorAll('button')].find((b) => b.textContent === 'Retry reprocess')!;
    await act(async () => retry.click());
    await waitFor(() => expect(sent('processChanges')).toHaveLength(2));
    await waitFor(() => expect(document.body.textContent).not.toContain('was not reprocessed.'));
  });

  it('is offered to Admins only', () => {
    state.role = { ...REVIEWER };
    renderPanel();
    expect(moreItem('Edit Page Text')).toBeUndefined();
    expect(moreItem('Reset Page Class')).toBeDefined();
  });

  it('is disabled while a past version is viewed', () => {
    renderPanel();
    viewPastVersion();
    expect(moreItem('Edit Page Text')).toMatchObject({ disabled: true, disabledReason: HISTORICAL_REASON });
  });

  it('is disabled for a document in a review queue, and open again once the queue review has finished', () => {
    renderPanel(doc({ hitlStatus: 'in_review', hitlTriggered: true }));
    expect(moreItem('Edit Page Text')).toMatchObject({ disabled: true, disabledReason: inReviewQueueEditReason(true) });
    document.body.innerHTML = '';
    renderPanel(doc({ hitlStatus: 'complete', hitlTriggered: true }));
    expect(moreItem('Edit Page Text')).toMatchObject({ disabled: false });
  });

  it('is disabled in BDA mode', () => {
    state.config = { mergedConfig: { use_bda: true }, loading: false };
    renderPanel();
    expect(moreItem('Edit Page Text')).toMatchObject({ disabled: true, disabledReason: BDA_REPROCESS_REASON });
  });
});
