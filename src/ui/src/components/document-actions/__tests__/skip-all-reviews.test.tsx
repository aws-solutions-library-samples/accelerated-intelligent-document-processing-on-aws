// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * More Actions › Skip All Reviews (replacing SectionsPanel.skipAllReason, and the Skip
 * cases of SectionsPanel.reviewQueue). What has to hold: it asks why, in the "Skip All
 * Section Reviews" dialog, and sends skipAllSectionsReview with the chosen reason and
 * note, so the document's review history says why nobody checked it; afterwards the page
 * says so and re-reads; it is available for a legacy review pending, and disabled for a
 * document in a review queue (its queue item is where it is skipped) and for a document
 * with no pending review.
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
vi.mock('../../document-panel/DocumentVersionsPanel', () => ({ default: () => null }));
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
import { skipAllSectionsReview } from '../../../graphql/generated';
import { inReviewQueueEditReason } from '../../common/review-status';
import { NO_PENDING_REVIEW_REASON } from '../action-reasons';
import { detailBar } from './detail-bar';

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
  objectKey: 'packet.pdf',
  objectStatus: 'COMPLETED',
  hitlTriggered: true,
  hitlStatus: 'Review Pending',
  sections: [{ Id: '1', Class: 'Invoice', PageIds: [1] }],
  pages: [{ Id: 1 }],
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

const skipItem = () => detailBar.menuItems('More Actions').find((i) => i.text === 'Skip All Reviews');

/** For waitFor: the element, or a throw until it is there. */
const shown = <T,>(found: T | null | undefined): T => {
  if (!found) throw new Error('not shown yet');
  return found;
};

beforeEach(() => {
  document.body.innerHTML = '';
  state.role = { ...ADMIN };
  state.config = { mergedConfig: {}, loading: false };
  state.setErrorMessage.mockReset();
  state.setSuccessMessage.mockReset();
  getDocumentDetailsFromIds.mockReset().mockResolvedValue([]);
  state.graphql.mockReset().mockResolvedValue({ data: { skipAllSectionsReview: { HITLStatus: 'Review Skipped' } } });
});

describe('Skip All Reviews', () => {
  it('asks why, then sends the chosen reason and note with the skip', async () => {
    renderPanel();
    detailBar.chooseMenuItem('More Actions', 'Skip All Reviews');
    const modal = await waitFor(() =>
      shown(
        createWrapper(document.body)
          .findAllModals()
          .find((m) => m.findHeader().getElement().textContent === 'Skip All Section Reviews'),
      ),
    );
    const select = modal.findContent().findSelect()!;
    act(() => select.openDropdown());
    act(() => select.selectOptionByValue('Duplicate document'));
    act(() => modal.findContent().findTextarea()!.setTextareaValue('same as invoice-2'));
    act(() =>
      modal
        .findFooter()!
        .findAllButtons()
        .find((b) => b.getElement().textContent === 'Skip All Reviews')!
        .click(),
    );
    await waitFor(() => expect(state.graphql).toHaveBeenCalled());
    expect(state.graphql.mock.calls[0][0]).toEqual({
      query: skipAllSectionsReview,
      variables: { objectKey: 'packet.pdf', reason: 'Duplicate document', note: 'same as invoice-2' },
    });
    await waitFor(() => expect(state.setSuccessMessage).toHaveBeenCalledWith('Skipped the review of packet.pdf.'));
    expect(getDocumentDetailsFromIds).toHaveBeenCalledWith(['packet.pdf']);
  });

  it.each([
    ['an Error', new Error('denied'), 'denied'],
    // The REST client rejects with the dispatcher's body, not an Error.
    [
      "the dispatcher's body",
      { errors: [{ message: 'REVIEW_FINISHED: this review is already finished', errorType: 'BadRequest' }] },
      'REVIEW_FINISHED: this review is already finished',
    ],
  ])('reports a skip the server refused, as %s, with its message', async (_label, rejection, message) => {
    state.graphql.mockRejectedValue(rejection);
    renderPanel();
    detailBar.chooseMenuItem('More Actions', 'Skip All Reviews');
    const modal = await waitFor(() => shown(createWrapper(document.body).findModal()));
    const select = modal.findContent().findSelect()!;
    act(() => select.openDropdown());
    act(() => select.selectOptionByValue('Duplicate document'));
    act(() =>
      modal
        .findFooter()!
        .findAllButtons()
        .find((b) => b.getElement().textContent === 'Skip All Reviews')!
        .click(),
    );
    await waitFor(() => expect(state.setErrorMessage).toHaveBeenCalledWith(`Failed to skip all sections: ${message}`));
  });

  it('is available for a legacy review pending', () => {
    renderPanel();
    expect(skipItem()).toEqual({ text: 'Skip All Reviews', disabled: false, disabledReason: null });
  });

  it('is disabled for a document in a review queue, which is skipped from its queue item', () => {
    renderPanel(doc({ hitlStatus: 'in_review' }));
    expect(skipItem()).toEqual({ text: 'Skip All Reviews', disabled: true, disabledReason: inReviewQueueEditReason(true) });
  });

  it('is disabled for a document with no pending review', () => {
    renderPanel(doc({ hitlStatus: 'Review Completed' }));
    expect(skipItem()).toMatchObject({ disabled: true, disabledReason: NO_PENDING_REVIEW_REASON });
  });
});
