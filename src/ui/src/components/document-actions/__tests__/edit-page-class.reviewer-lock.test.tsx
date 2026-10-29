// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Edit Page Class's Reviewer-only class lock on the document page (replacing
 * SectionsPanel.reviewerFinished). What has to hold: a user who is only a Reviewer can't
 * change a section's class once the document's review has finished (a queue review that
 * ended complete or rejected, a legacy review completed, or the `COMPLETED` spelling an
 * earlier flow stored), but can while holding a legacy review in progress.
 */

import React from 'react';
import { render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { describe, expect, it, vi } from 'vitest';

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
import { LegacyHitlStatus, ReviewStatus } from '../../common/review-status';
import userEvent from '@testing-library/user-event';

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

/** For waitFor: the element, or a throw until it is there. */
const shown = <T,>(found: T | null | undefined): T => {
  if (!found) throw new Error('not shown yet');
  return found;
};

const modalTitled = (title: string) =>
  createWrapper(document.body)
    .findAllModals()
    .find((m) => m.findHeader().getElement().textContent === title);

const REVIEWER = { ...ADMIN, isAdmin: false, isReviewer: true, isReviewerOnly: true, canWorkReviewQueues: false };

/** Whether another class can be picked for a section on the board. */
const classIsChangeable = async (hitlStatus: string) => {
  state.role = { ...REVIEWER };
  state.config = { mergedConfig: { classes: [{ $id: 'Invoice' }, { $id: 'W2' }] }, loading: false };
  renderPanel(
    doc({
      hitlTriggered: true,
      hitlStatus,
      hitlReviewOwner: 'rev',
      sections: [{ Id: '1', Class: 'Invoice', PageIds: ['1'] }],
      pages: [{ Id: 1, ImageUri: 's3://out/1.jpg' }],
    }),
  );
  detailBar.chooseMenuItem('More Actions', 'Edit Page Class');
  const dialog = await waitFor(() => shown(modalTitled('Edit Page Class')));
  // A locked control still shows the class but offers no other.
  const trigger = [...dialog.getElement().querySelectorAll('button[aria-haspopup="listbox"]')].find((b) =>
    b.textContent?.includes('Invoice'),
  );
  expect(trigger).toBeDefined();
  await userEvent.click(trigger as HTMLElement);
  return [...document.querySelectorAll('[role="option"]')].some((o) => o.textContent?.includes('W2'));
};

describe('Edit Page Class: the Reviewer-only lock on a finished review', () => {
  it.each([ReviewStatus.COMPLETE, ReviewStatus.REJECTED])('locks the class once a queue review ended %s', async (status) => {
    expect(await classIsChangeable(status)).toBe(false);
  });

  it.each([LegacyHitlStatus.REVIEW_COMPLETED, 'COMPLETED'])('locks the class once a legacy review finished (%s)', async (status) => {
    expect(await classIsChangeable(status)).toBe(false);
  });

  it('leaves the class changeable for the Reviewer who holds an open legacy review', async () => {
    expect(await classIsChangeable(LegacyHitlStatus.IN_PROGRESS)).toBe(true);
  });
});
