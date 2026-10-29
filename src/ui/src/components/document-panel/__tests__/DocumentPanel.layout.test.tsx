// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Document Details' layout. What has to hold: the fields sit in an aligned four-column
 * grid that narrows on a narrow screen (ColumnLayout columns={4} with minColumnWidth,
 * text-grid), in the same order as before, each a key label over its value; the status
 * is the shared status indicator; and Estimated Cost is its own container below Document
 * Details, not inside it.
 */

import React from 'react';
import { render } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const state = vi.hoisted(() => ({
  graphql: vi.fn(),
  role: { isAdmin: true, allowedConfigVersions: null, loading: false } as Record<string, unknown>,
  config: { mergedConfig: {} as Record<string, unknown> | null, loading: false },
  polling: [] as { enabled?: boolean }[],
  setErrorMessage: vi.fn(),
  setSuccessMessage: vi.fn(),
  flowVisible: false,
  troubleshootVisible: false,
  columnLayouts: [] as Record<string, unknown>[],
}));

vi.mock('@cloudscape-design/components', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@cloudscape-design/components')>();
  return {
    ...actual,
    ColumnLayout: (props: React.ComponentProps<typeof actual.ColumnLayout>) => {
      state.columnLayouts.push(props as unknown as Record<string, unknown>);
      return <actual.ColumnLayout {...props} />;
    },
  };
});
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
vi.mock('../DocumentVersionsPanel', () => ({ default: () => null }));
vi.mock('../DocumentDownloadModals', () => ({ DownloadOptionsModal: () => null, DownloadProgressModal: () => null }));
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
vi.mock('../TroubleshootModal', () => ({
  default: ({ visible }: { visible: boolean }) => {
    state.troubleshootVisible = visible;
    return null;
  },
}));

import DocumentPanel, { DocumentAttributes } from '../DocumentPanel';

const ITEM = {
  objectKey: 'in/a.pdf',
  objectStatus: 'COMPLETED',
  initialEventTime: 't0',
  completionTime: 't1',
  duration: '1m',
  pageCount: 2,
  evaluationStatus: 'COMPLETED',
  hitlStatus: 'Review Skipped',
  hitlReviewHistory: [{ action: 'skip_all', reason: 'Duplicate document' }],
  malwareScanStatus: 'CLEAN',
  malwareScanResult: 'CLEAN',
  sections: [],
  pages: [],
  metering: { 'OCR/textract/pages': { pages: 2 } },
};

beforeEach(() => {
  document.body.innerHTML = '';
  state.columnLayouts = [];
});

describe('Document Details', () => {
  it('lays the fields out in four aligned columns that narrow on a narrow screen', () => {
    render(<DocumentAttributes item={ITEM as never} versions={[]} />);
    expect(state.columnLayouts.at(-1)).toMatchObject({ columns: 4, minColumnWidth: 160, variant: 'text-grid' });
  });

  it('keeps the fields in their order, each a key label over its value', () => {
    render(<DocumentAttributes item={ITEM as never} versions={[]} />);
    const labels = [...document.querySelectorAll('[class*="key-label"]')].map((e) => e.textContent);
    expect(labels).toEqual([
      'Document ID',
      'Status',
      'Submitted',
      'Completed',
      'Duration',
      'Config Profile',
      'Page Count',
      'Evaluation',
      'Review Status',
      'Review Owner',
      'Review Completed By',
      'Skip Reason',
      'Confidence Alerts',
      'Summary',
      'Malware scan',
    ]);
  });

  it('shows the status with the shared status indicator', () => {
    render(<DocumentAttributes item={ITEM as never} versions={[]} />);
    const statusField = [...document.querySelectorAll('[class*="key-label"]')].find((e) => e.textContent === 'Status')!.parentElement!;
    expect(createWrapper(statusField).findStatusIndicator()!.getElement().textContent).toBe('COMPLETED');
  });

  it('puts Estimated Cost in its own container, below Document Details', () => {
    render(<DocumentPanel item={ITEM as never} />);
    const details = createWrapper(document.body)
      .findAllContainers()
      .find((c) => c.findHeader()?.getElement().textContent === 'Document Details')!;
    const cost = createWrapper(document.body).findExpandableSection()!;
    expect(cost.findHeaderText()!.getElement().textContent).toMatch(/^Estimated Cost \(per page: \$/);
    expect(details.getElement().contains(cost.getElement())).toBe(false);
    expect(details.getElement().compareDocumentPosition(cost.getElement()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});
