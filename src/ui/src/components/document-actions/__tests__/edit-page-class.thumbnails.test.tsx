// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Edit Page Class's page thumbnails on the document page (replacing
 * SectionsPanel.thumbnailDeferral). What has to hold: the page signs no thumbnail on
 * mount (they feed only the board); opening the board signs every page once, in one
 * batch; and a refused batch says why the board has no page images.
 */

import React from 'react';
import { render, waitFor } from '@testing-library/react';
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
import { FILE_ACCESS_DENIED_MESSAGE } from '../../../hooks/utils/graphql-error';

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

/** Calls that asked the batch presign resolver for URLs. */
const presignCalls = () => state.graphql.mock.calls.filter(([arg]) => String(arg.query).includes('getFilePresignedUrls('));

const signAll = () => {
  routes.getFilePresignedUrls = () => {
    const files = (presignCalls().at(-1)![0].variables as { files: { s3Uri: string }[] }).files;
    return {
      data: {
        getFilePresignedUrls: {
          results: files.map((f) => ({
            s3Uri: f.s3Uri,
            presignedUrl: `https://signed.example/${f.s3Uri.slice(5)}`,
            contentType: 'image/jpeg',
          })),
        },
      },
    };
  };
};

describe('Edit Page Class thumbnails', () => {
  it('signs nothing on mount, and signs every page once the board is opened', async () => {
    signAll();
    renderPanel();
    await waitFor(() => expect(detailBar.menuNames()).toContain('More Actions'));
    expect(presignCalls()).toHaveLength(0);

    detailBar.chooseMenuItem('More Actions', 'Edit Page Class');
    await waitFor(() => expect(presignCalls()).toHaveLength(1));
    expect((presignCalls()[0][0].variables as { files: { s3Uri: string }[] }).files.map((f) => f.s3Uri)).toEqual(
      doc().pages.map((p) => p.ImageUri),
    );
  });

  it('says why the board has no page images when the resolver refuses them', async () => {
    routes.getFilePresignedUrls = () => Promise.reject({ errors: [{ errorType: 'Unauthorized', message: 'Unauthorized' }] });
    renderPanel();
    detailBar.chooseMenuItem('More Actions', 'Edit Page Class');
    await waitFor(() => expect(document.body.textContent).toContain(FILE_ACCESS_DENIED_MESSAGE));
    expect(document.body.textContent).toContain('Page thumbnails unavailable');
  });
});
