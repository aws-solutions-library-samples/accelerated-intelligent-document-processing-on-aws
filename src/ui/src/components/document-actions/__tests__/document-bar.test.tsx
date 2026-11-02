// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The document page's header and action bar. What has to hold: the page opens on one
 * h1, "<document> (<status>)" over the object key, whose bar reads, left to right, the
 * Details | Source switch, Reports, More Actions, Refresh, Troubleshoot, Start Review
 * (where it applies) and Edit, the primary action; Reports lists Summary, Evaluation
 * Report, Rule Validation Summary and Processing Flow, each disabled with its reason when
 * it doesn't apply, and each opens its dialog; More Actions holds the changes, then the
 * downloads as one group, then Reprocess, Abort and Delete as another, Delete last; Source shows the source
 * document in place of the page's widgets, which stay mounted; Refresh re-reads the
 * document; a role is offered only what the server lets it do; Use As Evaluation
 * Baseline sends copyToBaseline, says so, re-reads, shows the copy in the Evaluation
 * field, keeps the page polling while the copy runs, and reports a copy that fails.
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
  user: null as { username: string } | null,
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
  default: () => ({
    currentCredentials: null,
    user: state.user,
    setErrorMessage: state.setErrorMessage,
    setSuccessMessage: state.setSuccessMessage,
  }),
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
import { BDA_REGROUP_REASON } from '../../edit-screen/document-edit-status';
import {
  BDA_REPROCESS_REASON,
  NO_COST_REASON,
  NO_EVALUATION_REPORT_REASON,
  NO_RULE_VALIDATION_REASON,
  REVIEWER_FINISHED_REASON,
  REVIEWER_UNCLAIMED_REASON,
  REVIEW_OWNER_REASON,
} from '../action-reasons';
import { detailBar } from './detail-bar';

const ADMIN = {
  isAdmin: true,
  isAuthor: false,
  isReviewer: false,
  isAnnotator: false,
  isViewer: false,
  isReviewerOnly: false,
  canWorkReviewQueues: true,
  canWrite: true,
  canReview: true,
  allowedConfigVersions: null,
  loading: false,
};
const VIEWER = { ...ADMIN, isAdmin: false, isViewer: true, canWorkReviewQueues: false, canWrite: false, canReview: false };
const REVIEWER = { ...ADMIN, isAdmin: false, isReviewer: true, isReviewerOnly: true, canWorkReviewQueues: false, canWrite: false };

const doc = (over: Record<string, unknown> = {}) => ({
  objectKey: 'in/a.pdf',
  objectStatus: 'COMPLETED',
  hitlTriggered: true,
  hitlStatus: 'PendingReview',
  evaluationStatus: 'COMPLETED',
  summaryReportUri: 's3://o/in/a.pdf/summary.md',
  evaluationReportUri: 's3://o/in/a.pdf/evaluation.md',
  ruleValidationResultUri: 's3://o/in/a.pdf/rules.md',
  executionArn: 'arn:aws:states:us-east-1:000000000000:execution:sm:x',
  sections: [{ Id: '1', Class: 'W2', PageIds: [1] }],
  pages: [{ Id: 1, TextUri: 's3://o/in/a.pdf/pages/1/text.txt' }],
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

const header = () => createWrapper(document.body).findHeader()!;
const button = (name: string) =>
  header()
    .findActions()!
    .findAllButtons()
    .find((b) => (b.findTextRegion()?.getElement().textContent ?? b.getElement().textContent) === name);

/** For waitFor: the element, or a throw until it is there. */
const shown = <T,>(found: T | null | undefined): T => {
  if (!found) throw new Error('not shown yet');
  return found;
};

beforeEach(() => {
  document.body.innerHTML = '';
  state.role = { ...ADMIN };
  state.config = { mergedConfig: {}, loading: false };
  state.polling = [];
  state.flowVisible = false;
  state.troubleshootVisible = false;
  state.user = null;
  state.setErrorMessage.mockReset();
  state.setSuccessMessage.mockReset();
  getDocumentDetailsFromIds.mockReset().mockResolvedValue([]);
  state.graphql.mockReset().mockImplementation(async ({ query }: { query: string }) => {
    if (String(query).includes('copyToBaseline')) return { data: { copyToBaseline: { success: true, message: 'started' } } };
    return { data: {} };
  });
});

describe('the document page header', () => {
  it('is the first Header: an h1 reading "<document> (<status>)" over the object key', () => {
    renderPanel();
    expect(header().getElement().querySelector('h1')).not.toBeNull();
    expect(header().findHeadingText().getElement().textContent).toBe('a.pdf (COMPLETED)');
    expect(header().findDescription()!.getElement().textContent).toBe('in/a.pdf');
  });

  it('reads Details | Source, Reports, More Actions, Refresh, Troubleshoot, Start Review, then Edit, the primary action', () => {
    renderPanel();
    const control = header().findActions()!.findSegmentedControl()!;
    expect(control.findSegments().map((s) => s.getElement().textContent)).toEqual(['Details', 'Source']);
    expect(
      control.getElement().compareDocumentPosition(detailBar.findMenu('Reports')!.getElement()) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(detailBar.menuNames()).toEqual(['Reports', 'More Actions']);
    expect(detailBar.barButtonLabels()).toEqual(['Refresh', 'Troubleshoot', 'Start Review', 'Edit']);
    const edit = button('Edit')!;
    expect(edit.getElement().getAttribute('href')).toBe(`#/documents/${encodeURIComponent('in/a.pdf')}/edit?from=document`);
    expect(edit.getElement().className).toMatch(/variant-primary/);
    const more = detailBar.findMenu('More Actions')!.getElement();
    expect(more.compareDocumentPosition(button('Refresh')!.getElement()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});

describe('Reports', () => {
  it('lists Summary, Evaluation Report, Rule Validation Summary, Estimated Cost and Processing Flow', () => {
    renderPanel(doc({ metering: { 'OCR/textract/pages': { pages: 1 } } }));
    expect(detailBar.menuItems('Reports')).toEqual([
      { text: 'Summary', disabled: false, disabledReason: null },
      { text: 'Evaluation Report', disabled: false, disabledReason: null },
      { text: 'Rule Validation Summary', disabled: false, disabledReason: null },
      { text: 'Estimated Cost', disabled: false, disabledReason: null },
      { text: 'Processing Flow', disabled: false, disabledReason: null },
    ]);
  });

  it('keeps Estimated Cost for a document with no metering yet, disabled with its reason', () => {
    renderPanel();
    expect(detailBar.menuItems('Reports').find((i) => i.text === 'Estimated Cost')).toEqual({
      text: 'Estimated Cost',
      disabled: true,
      disabledReason: NO_COST_REASON,
    });
  });

  it('offers Estimated Cost to the roles that may read the pricing, not to a Reviewer', () => {
    state.role = { ...REVIEWER };
    renderPanel(doc({ metering: { 'OCR/textract/pages': { pages: 1 } } }));
    expect(detailBar.menuItems('Reports').map((i) => i.text)).not.toContain('Estimated Cost');
  });

  it('keeps a report that does not exist, disabled with its reason', () => {
    renderPanel(doc({ evaluationReportUri: '', ruleValidationResultUri: '' }));
    const items = detailBar.menuItems('Reports');
    expect(items.find((i) => i.text === 'Evaluation Report')).toEqual({
      text: 'Evaluation Report',
      disabled: true,
      disabledReason: NO_EVALUATION_REPORT_REASON,
    });
    expect(items.find((i) => i.text === 'Rule Validation Summary')).toMatchObject({ disabledReason: NO_RULE_VALIDATION_REASON });
  });

  it.each([
    ['Summary', 'Document Summary', 'Document Summary|Summary report not available for this document|s3://o/in/a.pdf/summary.md'],
    [
      'Rule Validation Summary',
      'Rule Validation Summary',
      'Rule Validation Summary|Rule validation summary not available for this document|s3://o/in/a.pdf/rules.md',
    ],
  ])('%s opens its dialog', async (item, title, content) => {
    renderPanel();
    detailBar.chooseMenuItem('Reports', item);
    const modal = await waitFor(() => shown(createWrapper(document.body).findModal()));
    expect(modal.findHeader().getElement().textContent).toBe(title);
    expect(modal.findContent().getElement().textContent).toBe(content);
  });

  it('Evaluation Report opens its dialog with the evaluation report', async () => {
    renderPanel();
    detailBar.chooseMenuItem('Reports', 'Evaluation Report');
    const modal = await waitFor(() => shown(createWrapper(document.body).findModal()));
    expect(modal.findHeader().getElement().textContent).toBe('Evaluation Report');
    expect(modal.find('[data-testid="evaluation-report"]')!.getElement().textContent).toBe('s3://o/in/a.pdf/evaluation.md');
  });

  it('Processing Flow opens the flow viewer', async () => {
    renderPanel();
    expect(state.flowVisible).toBe(false);
    detailBar.chooseMenuItem('Reports', 'Processing Flow');
    await waitFor(() => expect(state.flowVisible).toBe(true));
  });
});

describe('More Actions', () => {
  it('holds the changes, then the downloads as one group, then Reprocess, Abort and Delete as another, Delete last', () => {
    renderPanel(doc({ objectStatus: 'RUNNING' }));
    const menu = detailBar.findMenu('More Actions')!;
    act(() => menu.openDropdown());
    // More Actions expands to the viewport, so its open menu is rendered outside the trigger.
    const open = menu.findOpenDropdown()!;
    const idsIn = (group: ReturnType<typeof open.findAll>[number]) =>
      group
        .findAll('[data-testid]')
        .map((e) => e.getElement().getAttribute('data-testid'))
        .filter((id) => ['all', 'predictions', 'baselines', 'reprocess', 'abort', 'delete'].includes(id ?? ''));
    const groups = open.findAll('[role="group"]');
    expect(groups).toHaveLength(2);
    expect(idsIn(groups[0])).toEqual(['all', 'predictions', 'baselines']);
    expect(idsIn(groups[1])).toEqual(['reprocess', 'abort', 'delete']);
    act(() => menu.openDropdown());
    const items = detailBar.menuItems('More Actions').map((i) => i.text);
    expect(items).toEqual([
      'Edit Page Grouping',
      'Re-extract Sections',
      'Edit Page Text',
      'Reset Page Class',
      'Use As Evaluation Baseline',
      'Skip All Reviews',
      'Download All (ZIP)',
      'Download Predictions (ZIP)',
      'Download Baselines (ZIP)',
      'Reprocess',
      'Abort',
      'Delete',
    ]);
  });
});

describe('Details | Source', () => {
  it('shows the source document in place of the widgets, which stay mounted', async () => {
    renderPanel();
    expect(document.querySelector('[data-testid="source-viewer"]')).toBeNull();
    header().findActions()!.findSegmentedControl()!.findSegments()[1].click();
    await waitFor(() => expect(document.querySelector('[data-testid="source-viewer"]')).not.toBeNull());
    const chat = document.querySelector('[data-testid="chat-panel"]') as HTMLElement;
    expect(chat).not.toBeNull();
    expect(chat).not.toBeVisible();
  });
});

describe('Refresh', () => {
  it('re-reads the document, loading meanwhile', async () => {
    let finish: () => void = () => undefined;
    getDocumentDetailsFromIds.mockImplementation(() => new Promise<void>((resolve) => (finish = resolve)));
    renderPanel();
    act(() => button('Refresh')!.click());
    expect(getDocumentDetailsFromIds).toHaveBeenCalledWith(['in/a.pdf']);
    await waitFor(() => expect(button('Refresh')!.getElement().getAttribute('aria-disabled')).toBe('true'));
    await act(async () => finish());
    await waitFor(() => expect(button('Refresh')!.getElement().getAttribute('aria-disabled')).not.toBe('true'));
  });
});

describe('by role', () => {
  it('offers a Viewer no changes, only the downloads in More Actions, and Troubleshoot', () => {
    state.role = { ...VIEWER };
    renderPanel();
    expect(detailBar.menuNames()).toEqual(['Reports', 'More Actions']);
    expect(detailBar.menuItems('More Actions').map((i) => i.text)).toEqual([
      'Download All (ZIP)',
      'Download Predictions (ZIP)',
      'Download Baselines (ZIP)',
    ]);
    expect(detailBar.barButtonLabels()).toEqual(['Refresh', 'Troubleshoot']);
  });

  it('offers a Reviewer no Troubleshoot', () => {
    state.role = { ...REVIEWER };
    renderPanel();
    expect(detailBar.barButtonLabels()).toEqual(['Refresh', 'Start Review', 'Edit']);
  });
});

describe('Use As Evaluation Baseline', () => {
  it('sends copyToBaseline, says so, re-reads, and shows the copy in the Evaluation field', async () => {
    let finish: () => void = () => undefined;
    state.graphql.mockImplementation(
      () => new Promise((resolve) => (finish = () => resolve({ data: { copyToBaseline: { success: true, message: 'started' } } }))),
    );
    renderPanel();
    detailBar.chooseMenuItem('More Actions', 'Use As Evaluation Baseline');
    await waitFor(() => expect(document.body.textContent).toContain('Baseline copying in progress'));
    const call = state.graphql.mock.calls.find(([arg]) => String(arg.query).includes('copyToBaseline'))!;
    expect(call[0].variables).toEqual({ objectKey: 'in/a.pdf' });
    await act(async () => finish());
    await waitFor(() =>
      expect(state.setSuccessMessage).toHaveBeenCalledWith(
        'Copying a.pdf to the evaluation baseline. Its evaluation status shows when the copy is done.',
      ),
    );
    expect(getDocumentDetailsFromIds).toHaveBeenCalledWith(['in/a.pdf']);
  });

  it('reports a copy that could not start', async () => {
    state.graphql.mockResolvedValue({ data: { copyToBaseline: { success: false, message: 'no evaluation' } } });
    renderPanel();
    detailBar.chooseMenuItem('More Actions', 'Use As Evaluation Baseline');
    await waitFor(() =>
      expect(state.setErrorMessage).toHaveBeenCalledWith('Could not use a.pdf as the evaluation baseline: no evaluation'),
    );
  });

  it('keeps polling a finished document while its copy runs', () => {
    renderPanel(doc({ evaluationStatus: 'BASELINE_COPYING' }));
    expect(state.polling.at(-1)?.enabled).toBe(true);
    document.body.innerHTML = '';
    renderPanel(doc({ evaluationStatus: 'COMPLETED' }));
    expect(state.polling.at(-1)?.enabled).toBe(false);
  });

  it('reports a copy that ends in an error', () => {
    const { rerender } = renderPanel(doc({ evaluationStatus: 'BASELINE_COPYING' }));
    expect(document.body.textContent).toContain('Baseline copying in progress');
    rerender(
      <DocumentPanel
        item={doc({ evaluationStatus: 'BASELINE_ERROR' }) as never}
        getDocumentDetailsFromIds={getDocumentDetailsFromIds}
        onDelete={vi.fn()}
        onReprocess={vi.fn()}
        onAbort={vi.fn()}
      />,
    );
    expect(state.setErrorMessage).toHaveBeenCalledWith('The copy of a.pdf to the evaluation baseline failed.');
    expect(document.body.textContent).toContain('Baseline copy failed');
  });
});

describe('Troubleshoot', () => {
  it('opens its dialog', async () => {
    renderPanel();
    act(() => button('Troubleshoot')!.click());
    await waitFor(() => expect(state.troubleshootVisible).toBe(true));
  });
});

describe('BDA mode, read from the document’s own configuration', () => {
  const reasons = () =>
    Object.fromEntries(
      detailBar
        .menuItems('More Actions')
        .filter((i) => ['Edit Page Grouping', 'Re-extract Sections', 'Edit Page Text', 'Reset Page Class'].includes(i.text))
        .map((i) => [i.text, i.disabledReason]),
    );

  it.each([true, 'true'])('disables the structural and re-extraction actions when use_bda is %j', (useBda) => {
    state.config = { mergedConfig: { use_bda: useBda }, loading: false };
    renderPanel();
    expect(reasons()).toEqual({
      'Edit Page Grouping': BDA_REGROUP_REASON,
      'Re-extract Sections': BDA_REPROCESS_REASON,
      'Edit Page Text': BDA_REPROCESS_REASON,
      'Reset Page Class': BDA_REPROCESS_REASON,
    });
  });

  it.each([false, 'false', undefined])('offers them in pipeline mode (use_bda %j)', (useBda) => {
    state.config = { mergedConfig: { use_bda: useBda }, loading: false };
    renderPanel();
    expect(Object.values(reasons())).toEqual([null, null, null, null]);
  });
});

describe('the processChanges actions for a caller who is not an Admin', () => {
  // The server's rule: hold the document's OPEN review.
  const reextract = () => detailBar.menuItems('More Actions').find((i) => i.text === 'Re-extract Sections');

  it('are offered to the reviewer holding the open review', () => {
    state.role = { ...REVIEWER };
    state.user = { username: 'alice' };
    renderPanel(doc({ hitlStatus: 'InProgress', hitlReviewOwner: 'alice' }));
    expect(reextract()).toMatchObject({ disabled: false });
  });

  it.each([
    ['a finished review the caller holds', { hitlStatus: 'Review Completed', hitlReviewOwner: 'alice' }, REVIEWER_FINISHED_REASON],
    ['an unclaimed pending review', { hitlStatus: 'PendingReview' }, REVIEWER_UNCLAIMED_REASON],
    ['an open review someone else holds', { hitlStatus: 'InProgress', hitlReviewOwner: 'bob' }, REVIEW_OWNER_REASON],
    ['a document with no review', { hitlTriggered: false, hitlStatus: null }, REVIEW_OWNER_REASON],
  ])('are not offered to a Reviewer on %s', (_label, over, reason) => {
    state.role = { ...REVIEWER };
    state.user = { username: 'alice' };
    renderPanel(doc(over));
    expect(reextract()).toMatchObject({ disabled: true, disabledReason: reason });
  });

  it('are not offered to a Reviewer who is also an Author, on a review they do not hold', () => {
    state.role = { ...REVIEWER, isAuthor: true, isReviewerOnly: false, canWrite: true };
    state.user = { username: 'alice' };
    renderPanel(doc({ hitlStatus: 'InProgress', hitlReviewOwner: 'bob' }));
    expect(reextract()).toMatchObject({ disabled: true, disabledReason: REVIEW_OWNER_REASON });
  });
});

describe('Start Review', () => {
  it('reports a claim the server refused with its message', async () => {
    // The REST client rejects with the dispatcher's body, not an Error.
    state.graphql.mockRejectedValue({ errors: [{ message: 'REVIEW_FINISHED: this review is already finished', errorType: 'BadRequest' }] });
    renderPanel();
    act(() => button('Start Review')!.click());
    await waitFor(() =>
      expect(state.setErrorMessage).toHaveBeenCalledWith('Failed to start review: REVIEW_FINISHED: this review is already finished'),
    );
  });
});
