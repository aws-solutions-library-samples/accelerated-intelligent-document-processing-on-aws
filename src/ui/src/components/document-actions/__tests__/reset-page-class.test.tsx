// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * More Actions › Reset Page Class (replacing PagesPanel.readonly, PagesPanel.reviewQueue and
 * PagesPanel.reviewerFinished). What has to hold: it sends processChanges with
 * `classReset` for exactly the chosen pages and no section; before that it says a reset
 * classifies the whole document again and replaces values corrected by hand in every
 * section; it is disabled, with its reason, for a past version and a document in a review
 * queue (and open again once the queue review has finished); a user who is only a
 * Reviewer can't reset once the review has finished, but can while holding a review in
 * progress; and a dialog left open while the review finishes can no longer commit.
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
import { LegacyHitlStatus, ReviewStatus, inReviewQueueEditReason } from '../../common/review-status';
import { HISTORICAL_REASON, REVIEWER_FINISHED_REASON } from '../action-reasons';

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

const REVIEWER = { ...ADMIN, isAdmin: false, isReviewer: true, isReviewerOnly: true, canWorkReviewQueues: false };
const HELD = { hitlTriggered: true, hitlReviewOwner: 'rev' };

const openDialog = async (item = doc()) => {
  const view = renderPanel(item);
  detailBar.chooseMenuItem('More Actions', 'Reset Page Class');
  const dialog = await waitFor(() => shown(modalTitled('Reset Page Class')));
  return { dialog, view };
};
const choosePage = (dialog: NonNullable<ReturnType<typeof modalTitled>>, id: string) => {
  const select = dialog.findContent().findMultiselect()!;
  act(() => select.openDropdown());
  act(() => select.selectOptionByValue(id));
};
const commit = () => footerButton('Reset Page Class', 'Reset Page Class');

describe('Reset Page Class', () => {
  it('says the whole document is classified again, then sends classReset for the chosen pages only', async () => {
    const { dialog } = await openDialog();
    const text = dialog.findContent().getElement().textContent;
    expect(text).toContain("Resetting a page's class classifies the whole document again:");
    expect(text).toContain('Every page is classified again, so pages can move to other sections and sections can be added or removed.');
    expect(text).toContain(
      'Every section is rebuilt and extracted again. Values corrected by hand in any section, not only the ones holding these pages, are replaced.',
    );
    expect(isOff(commit())).toBe(true);
    choosePage(dialog, '3');
    await act(async () => commit().click());
    await waitFor(() => expect(sent('processChanges')).toHaveLength(1));
    expect(sent('processChanges')[0]).toEqual({
      objectKey: 'in/packet.pdf',
      modifiedSections: [],
      modifiedPages: [{ pageId: 3, classReset: true, textModified: false }],
    });
    expect(state.setSuccessMessage).toHaveBeenCalledWith(
      'Resetting the class of page 3 of packet.pdf: the document is being classified and extracted again.',
    );
  });

  it('keeps the dialog open with the reason when the reset fails', async () => {
    routes.processChanges = () => ({ data: { processChanges: { success: false, message: 'In review' } } });
    const { dialog } = await openDialog();
    choosePage(dialog, '1');
    await act(async () => commit().click());
    await waitFor(() =>
      expect(modalTitled('Reset Page Class')!.getElement().textContent).toContain('Could not reset the page class: In review'),
    );
  });

  it('is disabled while a past version is viewed', () => {
    renderPanel();
    viewPastVersion();
    expect(moreItem('Reset Page Class')).toMatchObject({ disabled: true, disabledReason: HISTORICAL_REASON });
  });

  it('is disabled for a document in a review queue, and open again once the queue review has finished', () => {
    renderPanel(doc({ hitlStatus: ReviewStatus.IN_REVIEW, hitlTriggered: true }));
    expect(moreItem('Reset Page Class')).toMatchObject({ disabled: true, disabledReason: inReviewQueueEditReason(true) });
    document.body.innerHTML = '';
    renderPanel(doc({ hitlStatus: ReviewStatus.COMPLETE, hitlTriggered: true }));
    expect(moreItem('Reset Page Class')).toMatchObject({ disabled: false });
  });
});

describe('Reset Page Class: the Reviewer-only lock on a finished review', () => {
  it.each([ReviewStatus.COMPLETE, ReviewStatus.REJECTED, LegacyHitlStatus.REVIEW_COMPLETED, LegacyHitlStatus.SKIPPED, 'COMPLETED'])(
    'is disabled once the review is %s',
    (hitlStatus) => {
      state.role = { ...REVIEWER };
      renderPanel(doc({ ...HELD, hitlStatus }));
      expect(moreItem('Reset Page Class')).toMatchObject({ disabled: true, disabledReason: REVIEWER_FINISHED_REASON });
    },
  );

  it('is open to the Reviewer who holds a review in progress', () => {
    state.role = { ...REVIEWER };
    renderPanel(doc({ ...HELD, hitlStatus: LegacyHitlStatus.IN_PROGRESS }));
    expect(moreItem('Reset Page Class')).toMatchObject({ disabled: false });
  });

  it.each([ReviewStatus.COMPLETE, ReviewStatus.REJECTED])(
    'can no longer commit when the review ends %s while the dialog is open',
    async (status) => {
      state.role = { ...REVIEWER };
      const { dialog, view } = await openDialog(doc({ ...HELD, hitlStatus: LegacyHitlStatus.IN_PROGRESS }));
      choosePage(dialog, '3');
      expect(isOff(commit())).toBe(false);
      view.rerender(
        <DocumentPanel
          item={doc({ ...HELD, hitlStatus: status }) as never}
          getDocumentDetailsFromIds={getDocumentDetailsFromIds}
          onDelete={vi.fn()}
          onReprocess={vi.fn()}
          onAbort={vi.fn()}
        />,
      );
      await waitFor(() => expect(isOff(commit())).toBe(true));
    },
  );
});
