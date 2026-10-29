// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * More Actions › Edit Page Class on the document page (replacing SectionsPanel.regrouping,
 * and SectionsPanel.reviewQueue's queue case). What has to hold: the board gets the
 * document's sections with page ids **unconverted** (1-based, or BDA's 0-based: no
 * arithmetic here); Save page grouping sends updateDocumentSections in the reviewer's
 * order and never processChanges, which would regenerate the values, and says so; a
 * refusal stays inside the open board; a good save closes it, says so and re-reads the
 * document; Save and Re-extract confirms the sections whose class or pages changed, then
 * sends updateDocumentSections before processChanges with exactly those sections; a
 * re-extract that fails after the save is reported with Re-extract again, which sends
 * processChanges only; and it is unavailable, with its reason, for a pure reorder, for a
 * changed section with no class, in BDA mode, and for a document in a review queue.
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

vi.mock('../../common/PageGroupingEditor', () => ({
  default: (props: Record<string, unknown>) => {
    state.board = props;
    return (
      <div data-testid="grouping-board">
        <div data-testid="board-consequence">{props.consequence as React.ReactNode}</div>
      </div>
    );
  },
}));
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
import { inReviewQueueEditReason } from '../../common/review-status';
import { BDA_REPROCESS_REASON, REEXTRACT_ORDER_ONLY_REASON, reextractNoClassReason } from '../action-reasons';

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

/** The operations sent, in order. */
const order = () =>
  state.graphql.mock.calls
    .map(([arg]) => String(arg.query))
    .map((q) =>
      q.includes('updateDocumentSections(') ? 'updateDocumentSections' : q.includes('processChanges(') ? 'processChanges' : 'other',
    )
    .filter((o) => o !== 'other');

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

type Draft = { sectionId: string; documentClass: string | null; pageIds: number[] }[];
interface BoardProps {
  sections: Draft;
  onSave: (next: Draft) => Promise<void>;
  secondarySave: { label: string; disabledReason: (d: Draft) => string | undefined; onSave: (next: Draft) => void };
}
const board = () => state.board as unknown as BoardProps;
const boardOpen = () => document.querySelector('[data-testid="grouping-board"]') !== null;

const AS_LOADED: Draft = [
  { sectionId: '1', documentClass: 'FieldTicket', pageIds: [1, 2, 3] },
  { sectionId: '2', documentClass: 'Invoice', pageIds: [4] },
];
/** Page 3 moved from section 1 to section 2. */
const MOVED: Draft = [
  { sectionId: '1', documentClass: 'FieldTicket', pageIds: [1, 2] },
  { sectionId: '2', documentClass: 'Invoice', pageIds: [3, 4] },
];

const openBoard = async (item = doc()) => {
  renderPanel(item);
  detailBar.chooseMenuItem('More Actions', 'Edit Page Class');
  await waitFor(() => expect(boardOpen()).toBe(true));
};

describe('Edit Page Class › Save page grouping', () => {
  it('opens the board on the document’s sections, page ids unconverted', async () => {
    await openBoard();
    expect(board().sections).toEqual(AS_LOADED);
  });

  it('sends page ids unconverted, in the document’s own numbering, and no processChanges', async () => {
    await openBoard();
    await act(async () => board().onSave(AS_LOADED));
    expect(sent('updateDocumentSections')).toEqual([
      {
        objectKey: 'in/packet.pdf',
        sections: [
          { sectionId: '1', classification: 'FieldTicket', pageIds: ['1', '2', '3'] },
          { sectionId: '2', classification: 'Invoice', pageIds: ['4'] },
        ],
      },
    ]);
    expect(sent('processChanges')).toEqual([]);
  });

  it('sends a reordered section in the reviewer’s order, not sorted', async () => {
    await openBoard();
    await act(async () => board().onSave([{ ...AS_LOADED[0], pageIds: [2, 1, 3] }, AS_LOADED[1]]));
    expect((sent('updateDocumentSections')[0].sections as { pageIds: string[] }[])[0].pageIds).toEqual(['2', '1', '3']);
  });

  it('says the values are kept and the document is not reprocessed, unless Save and Re-extract is chosen', async () => {
    await openBoard();
    // (Beside the thumbnails' own notice: this suite does not sign them.)
    expect(document.querySelector('[data-testid="board-consequence"]')!.textContent).toContain(
      "Save page grouping rewrites this document's section grouping and keeps the extracted field values, including any a reviewer corrected; the document is not reprocessed, so they may no longer match their pages. Save and Re-extract saves the grouping, then extracts again the sections whose class or pages changed, replacing their values.",
    );
  });

  it('keeps the board open with the reason when the save is refused', async () => {
    routes.updateDocumentSections = () => ({
      data: { updateDocumentSections: { success: false, message: 'The document is still processing' } },
    });
    await openBoard();
    await act(async () => board().onSave(MOVED));
    expect(boardOpen()).toBe(true);
    expect(document.querySelector('[data-testid="board-consequence"]')!.textContent).toContain('The document is still processing');
    expect(state.setSuccessMessage).not.toHaveBeenCalled();
  });

  it('closes on a good save, says so with the server’s message, and re-reads the document', async () => {
    await openBoard();
    await act(async () => board().onSave(MOVED));
    await waitFor(() => expect(boardOpen()).toBe(false));
    expect(state.setSuccessMessage).toHaveBeenCalledWith('Page grouping saved');
    expect(getDocumentDetailsFromIds).toHaveBeenCalledWith(['in/packet.pdf']);
  });
});

describe('Edit Page Class › Save and Re-extract', () => {
  it('is offered as the board’s second save', async () => {
    await openBoard();
    expect(board().secondarySave.label).toBe('Save and Re-extract');
  });

  it('confirms the changed sections, then saves the grouping before re-extracting exactly those', async () => {
    const changedOnly: Draft = [AS_LOADED[0], { sectionId: '2', documentClass: 'FieldTicket', pageIds: [4] }];
    await openBoard();
    act(() => board().secondarySave.onSave(changedOnly));
    const confirm = await waitFor(() => shown(modalTitled('Confirm Reprocessing')));
    expect([...confirm.findContent().getElement().querySelectorAll('li')][0].textContent).toBe('Section 2: FieldTicket, pages 4');
    expect(order()).toEqual([]);
    await act(async () => footerButton('Confirm Reprocessing', 'Confirm & Process').click());
    await waitFor(() => expect(order()).toEqual(['updateDocumentSections', 'processChanges']));
    expect(sent('processChanges')[0]).toEqual({
      objectKey: 'in/packet.pdf',
      modifiedSections: [{ sectionId: '2', classification: 'FieldTicket', pageIds: [4], isNew: false, isDeleted: false }],
    });
    await waitFor(() =>
      expect(state.setSuccessMessage).toHaveBeenCalledWith('Saved the page grouping of packet.pdf, and re-extracting section 2.'),
    );
    expect(boardOpen()).toBe(false);
  });

  it('reports a re-extract that failed after the save, and Re-extract again sends processChanges only', async () => {
    let refuse = true;
    routes.processChanges = () =>
      refuse ? { data: { processChanges: { success: false, message: 'Throttled' } } } : { data: { processChanges: { success: true } } };
    await openBoard();
    act(() => board().secondarySave.onSave(MOVED));
    await waitFor(() => shown(modalTitled('Confirm Reprocessing')));
    await act(async () => footerButton('Confirm Reprocessing', 'Confirm & Process').click());
    const failure = await waitFor(() => shown(modalTitled('Edit Page Class')));
    expect(failure.findContent().getElement().textContent).toContain(
      'The page grouping was saved, but the sections were not re-extracted: Throttled',
    );
    expect(boardOpen()).toBe(false);
    expect(order()).toEqual(['updateDocumentSections', 'processChanges']);

    refuse = false;
    await act(async () => footerButton('Edit Page Class', 'Re-extract again').click());
    await waitFor(() => expect(order()).toEqual(['updateDocumentSections', 'processChanges', 'processChanges']));
    expect(sent('processChanges')[1].modifiedSections).toEqual([
      { sectionId: '1', classification: 'FieldTicket', pageIds: [1, 2], isNew: false, isDeleted: false },
      { sectionId: '2', classification: 'Invoice', pageIds: [3, 4], isNew: false, isDeleted: false },
    ]);
    await waitFor(() => expect(modalTitled('Edit Page Class')).toBeUndefined());
  });

  it('is unavailable for a pure reorder, which needs no re-extracting', async () => {
    await openBoard();
    expect(board().secondarySave.disabledReason([{ ...AS_LOADED[0], pageIds: [3, 1, 2] }, AS_LOADED[1]])).toBe(REEXTRACT_ORDER_ONLY_REASON);
    expect(board().secondarySave.disabledReason(MOVED)).toBeUndefined();
  });

  it('is unavailable while a changed section has no class', async () => {
    await openBoard();
    const withNew: Draft = [...MOVED.slice(0, 1), { sectionId: '3', documentClass: null, pageIds: [3, 4] }];
    expect(board().secondarySave.disabledReason(withNew)).toBe(reextractNoClassReason(['3']));
  });

  it('is unavailable in BDA mode', async () => {
    state.config = { mergedConfig: { use_bda: true, classes: [] }, loading: false };
    await openBoard();
    expect(board().secondarySave.disabledReason(MOVED)).toBe(BDA_REPROCESS_REASON);
  });
});

describe('Edit Page Class availability', () => {
  it('is disabled with the queue reason while the document is in a review queue', () => {
    renderPanel(doc({ hitlStatus: 'in_review', hitlTriggered: true }));
    expect(moreItem('Edit Page Class')).toEqual({ text: 'Edit Page Class', disabled: true, disabledReason: inReviewQueueEditReason(true) });
  });

  it('is not offered to a Viewer', () => {
    state.role = { ...ADMIN, isAdmin: false, isViewer: true, canWorkReviewQueues: false };
    renderPanel();
    expect(moreItem('Edit Page Class')).toBeUndefined();
  });
});
