// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * More Actions › Re-extract Sections. What has to hold: it sends processChanges with
 * exactly the chosen sections, each with its current class and pages, and nothing about
 * pages; before that it lists them and says what re-extracting replaces; a section with
 * no class, or one its configuration skips, can't be chosen; a failure stays in the open
 * dialog; and the item is disabled, with its reason, for a past version, a document in a
 * review queue, one still processing, and in BDA mode.
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
import { PROCESSING_REASON } from '../../edit-screen/document-edit-status';
import { BDA_REPROCESS_REASON, HISTORICAL_REASON, SECTION_NO_CLASS_REASON, SECTION_SKIPPED_REASON } from '../action-reasons';

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

const isOff = (b: { getElement: () => HTMLElement }) =>
  b.getElement().hasAttribute('disabled') || b.getElement().getAttribute('aria-disabled') === 'true';

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

const SECTIONS = [
  { Id: '1', Class: 'FieldTicket', PageIds: ['1', '2'] },
  { Id: '2', Class: 'Invoice', PageIds: ['3'] },
  { Id: '3', Class: null, PageIds: ['4'] },
  { Id: '4', Class: 'Cover', PageIds: ['5'], Excluded: true },
];

const openDialog = async () => {
  renderPanel(doc({ sections: SECTIONS }));
  detailBar.chooseMenuItem('More Actions', 'Re-extract Sections');
  return waitFor(() => shown(modalTitled('Re-extract Sections')));
};

const choose = (dialog: ReturnType<typeof modalTitled>, ...ids: string[]) => {
  const select = dialog!.findContent().findMultiselect()!;
  for (const id of ids) {
    act(() => select.openDropdown());
    act(() => select.selectOptionByValue(id));
  }
};

describe('Re-extract Sections', () => {
  it('sends processChanges with exactly the chosen sections, after saying what that replaces', async () => {
    const dialog = await openDialog();
    expect(isOff(footerButton('Re-extract Sections', 'Re-extract'))).toBe(true);
    choose(dialog, '2');
    expect(dialog.findContent().getElement().textContent).toContain(
      'These sections are extracted again: Section 2. Their extracted values, including any corrected by hand, are replaced',
    );
    await act(async () => footerButton('Re-extract Sections', 'Re-extract').click());
    await waitFor(() => expect(sent('processChanges')).toHaveLength(1));
    expect(sent('processChanges')[0]).toEqual({
      objectKey: 'in/packet.pdf',
      modifiedSections: [{ sectionId: '2', classification: 'Invoice', pageIds: [3], isNew: false, isDeleted: false }],
    });
    expect(state.setSuccessMessage).toHaveBeenCalledWith('Re-extracting section 2 of packet.pdf.');
    expect(getDocumentDetailsFromIds).toHaveBeenCalledWith(['in/packet.pdf']);
    await waitFor(() => expect(modalTitled('Re-extract Sections')).toBeUndefined());
  });

  it('can’t choose a section with no class, or one its configuration skips', async () => {
    const dialog = await openDialog();
    const select = dialog.findContent().findMultiselect()!;
    act(() => select.openDropdown());
    const disabled = [...document.querySelectorAll('[role="option"]')].map((o) => o.getAttribute('aria-disabled') === 'true');
    expect(disabled).toEqual([false, false, true, true]);
    expect(document.body.textContent).toContain(SECTION_NO_CLASS_REASON);
    expect(document.body.textContent).toContain(SECTION_SKIPPED_REASON);
  });

  it('keeps the dialog open with the reason when the re-extract fails', async () => {
    routes.processChanges = () => ({ data: { processChanges: { success: false, message: 'Throttled' } } });
    const dialog = await openDialog();
    choose(dialog, '1');
    await act(async () => footerButton('Re-extract Sections', 'Re-extract').click());
    await waitFor(() =>
      expect(modalTitled('Re-extract Sections')!.getElement().textContent).toContain('Could not re-extract the sections: Throttled'),
    );
    expect(state.setSuccessMessage).not.toHaveBeenCalled();
  });

  it.each([
    ['in a review queue', { hitlStatus: 'in_review', hitlTriggered: true }, inReviewQueueEditReason(true)],
    ['still processing', { objectStatus: 'RUNNING' }, PROCESSING_REASON],
  ])('is disabled for a document %s', (_what, over, reason) => {
    renderPanel(doc(over));
    expect(moreItem('Re-extract Sections')).toEqual({ text: 'Re-extract Sections', disabled: true, disabledReason: reason });
  });

  it('is disabled in BDA mode', () => {
    state.config = { mergedConfig: { use_bda: true }, loading: false };
    renderPanel();
    expect(moreItem('Re-extract Sections')).toMatchObject({ disabled: true, disabledReason: BDA_REPROCESS_REASON });
  });

  it('is disabled while a past version is viewed', () => {
    renderPanel();
    viewPastVersion();
    expect(moreItem('Re-extract Sections')).toMatchObject({ disabled: true, disabledReason: HISTORICAL_REASON });
  });
});
