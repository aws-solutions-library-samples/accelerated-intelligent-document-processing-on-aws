// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Document Details' layout. What has to hold: the fields sit in an aligned four-column
 * grid that narrows on a narrow screen (ColumnLayout columns={4} with minColumnWidth,
 * text-grid), in the same order as before, each a key label over its value; the status
 * is the shared status indicator; and Estimated Cost is its own container below Document
 * Details, not inside it. A document routing placed in a review queue shows the queue
 * with the routing rule under its name, in the words the review pages use, and next to
 * it the reason the routing rule gave, shortened when it is long and rendered as text.
 */

import React from 'react';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';

import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { fireEvent, screen } from '@testing-library/react';
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
  MarkdownReport: ({ report, emptyMessage, reportUri }: { report: string; emptyMessage: string; reportUri: string }) => (
    <div data-testid="markdown-report">
      {report}|{emptyMessage}|{reportUri}
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
import { detailBar } from '../../document-actions/__tests__/detail-bar';

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
    renderWithIntl(<DocumentAttributes item={ITEM as never} versions={[]} />);
    expect(state.columnLayouts.at(-1)).toMatchObject({ columns: 4, minColumnWidth: 160, variant: 'text-grid' });
  });

  it('keeps the fields in their order, each a key label over its value', () => {
    renderWithIntl(<DocumentAttributes item={ITEM as never} versions={[]} />);
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
      'Missing Required Fields',
      'Summary',
      'Malware scan',
    ]);
  });

  /** The value of a field: its text nodes, top to bottom. */
  const field = (label: string): string[] | null => {
    const key = [...document.querySelectorAll('[class*="key-label"]')].find((e) => e.textContent === label);
    if (!key) return null;
    const value = key.nextElementSibling!;
    const walker = document.createTreeWalker(value, NodeFilter.SHOW_TEXT);
    const texts: string[] = [];
    while (walker.nextNode()) texts.push(walker.currentNode.textContent ?? '');
    return texts.filter((text) => text.trim() !== '');
  };
  const labels = (): (string | null)[] => [...document.querySelectorAll('[class*="key-label"]')].map((e) => e.textContent);
  const ROUTED_BY = {
    tableId: 'review-routing',
    ruleId: 'claims-over-1000',
    ruleLabel: 'Claims over 1000',
    reason: '',
    policyFailureCount: 0,
  };

  it('shows the review queue by name with the routing rule under it, then the reason, after the review fields', () => {
    const routed = {
      ...ITEM,
      reviewQueue: 'claims',
      reviewQueueName: 'Claims team',
      reviewRoutedBy: { ...ROUTED_BY, reason: 'Invoice total 1250.00 is over 1000. Check the total against the line items.' },
    };
    renderWithIntl(<DocumentAttributes item={routed as never} versions={[]} />);

    // The routing rule reads as it does on the review pages.
    expect(field('Review queue')).toEqual(['Claims team', 'Routed by rule: Claims over 1000']);
    expect(field('Routing reason')).toEqual(['Invoice total 1250.00 is over 1000. Check the total against the line items.']);
    const all = labels();
    expect(all.slice(all.indexOf('Review Completed By'), all.indexOf('Review Completed By') + 3)).toEqual([
      'Review Completed By',
      'Review queue',
      'Routing reason',
    ]);
    // The routing rule has no label of its own: its text says what it is.
    expect(all).not.toContain('Routed by');
  });

  it('shows the queue by its id when it has no name, and the rule by its id when it has no label', () => {
    const routed = {
      ...ITEM,
      reviewQueue: 'claims',
      reviewQueueName: null,
      reviewRoutedBy: { ...ROUTED_BY, ruleId: 'plain', ruleLabel: '' },
    };
    renderWithIntl(<DocumentAttributes item={routed as never} versions={[]} />);

    expect(field('Review queue')).toEqual(['claims', 'Routed by rule: plain']);
  });

  it('says the default outcome routed a document no rule matched', () => {
    const routed = { ...ITEM, reviewQueue: 'default', reviewRoutedBy: { ...ROUTED_BY, ruleId: '', ruleLabel: '' } };
    renderWithIntl(<DocumentAttributes item={routed as never} versions={[]} />);

    expect(field('Review queue')).toEqual(['default', 'Routed by the default outcome']);
  });

  it('shows no routing reason for a document placed before routing rules had one', () => {
    const routed = { ...ITEM, reviewQueue: 'claims', reviewQueueName: 'Claims team', reviewRoutedBy: ROUTED_BY };
    renderWithIntl(<DocumentAttributes item={routed as never} versions={[]} />);

    expect(field('Review queue')).toEqual(['Claims team', 'Routed by rule: Claims over 1000']);
    expect(labels()).not.toContain('Routing reason');
    // Every place in the grid is a field: the empty reason is not rendered as a text of its own.
    const grid = state.columnLayouts.at(-1)!.children as React.ReactNode;
    expect(React.Children.toArray(grid).filter((child) => typeof child === 'string')).toEqual([]);
  });

  it('shortens a long routing reason, and shows the rest on request', () => {
    const long = Array.from({ length: 12 }, (_, i) => `Sentence ${i + 1} of the reason says what to check.`).join(' ');
    const routed = { ...ITEM, reviewQueue: 'claims', reviewRoutedBy: { ...ROUTED_BY, reason: long } };
    renderWithIntl(<DocumentAttributes item={routed as never} versions={[]} />);

    expect(field('Routing reason')).toEqual([`${long.slice(0, 280).trimEnd()}…`, 'Show more']);
    const more = screen.getByRole('button', { name: 'Show more' });
    expect(more.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(more);
    expect(field('Routing reason')).toEqual([long, 'Show less']);
  });

  it('shows what the routing reason holds as text, not as markup', () => {
    const markup = 'Vendor <img src=x onerror="alert(1)"> **Acme** [site](https://example.com)';
    const routed = { ...ITEM, reviewQueue: 'claims', reviewRoutedBy: { ...ROUTED_BY, reason: markup } };
    renderWithIntl(<DocumentAttributes item={routed as never} versions={[]} />);

    expect(field('Routing reason')).toEqual([markup]);
    expect(document.querySelector('img')).toBeNull();
    expect(document.querySelector('a[href="https://example.com"]')).toBeNull();
  });

  it('shows neither field for a document that was not routed to a queue', () => {
    renderWithIntl(<DocumentAttributes item={ITEM as never} versions={[]} />);

    expect(labels()).not.toContain('Review queue');
    expect(labels()).not.toContain('Routing reason');
    expect(document.body.textContent).not.toContain('Routed by');
  });

  it('shows the queue alone for a document in a queue with no routing rule on record', () => {
    renderWithIntl(
      <DocumentAttributes
        item={{ ...ITEM, reviewQueue: 'default', reviewQueueName: 'Default', reviewRoutedBy: null } as never}
        versions={[]}
      />,
    );

    expect(field('Review queue')).toEqual(['Default']);
    expect(labels()).not.toContain('Routing reason');
    expect(document.body.textContent).not.toContain('Routed by');
  });

  it('shows the status with the shared status indicator', () => {
    renderWithIntl(<DocumentAttributes item={ITEM as never} versions={[]} />);
    const statusField = [...document.querySelectorAll('[class*="key-label"]')].find((e) => e.textContent === 'Status')!.parentElement!;
    expect(createWrapper(statusField).findStatusIndicator()!.getElement().textContent).toBe('COMPLETED');
  });

  it('has no Estimated Cost panel: Reports › Estimated Cost opens it in a dialog', () => {
    renderWithIntl(<DocumentPanel item={ITEM as never} />);
    expect(createWrapper(document.body).findExpandableSection()).toBeNull();
    expect(document.body.textContent).not.toContain('Estimated Cost (per page');
    detailBar.chooseMenuItem('Reports', 'Estimated Cost');
    const modal = createWrapper(document.body).findModal()!;
    expect(modal.findHeader().getElement().textContent).toMatch(/^Estimated Cost \(per page: US\$/);
    expect(modal.findContent().findTable()).not.toBeNull();
  });
});
