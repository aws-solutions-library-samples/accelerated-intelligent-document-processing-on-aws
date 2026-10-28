// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The edit screen on a direct edit. What has to hold: the bar is History, Edit Page
 * Grouping, Cancel, Save and Reprocess, with no Skip; the title is the document and its
 * status over the object key; the editor is the Edit Document widget; Save saves through
 * the editor; Cancel leaves for the document page, asking first when there are unsaved
 * edits; Reprocess confirms, then calls processChanges with no section changes; an Author can save but not process; a
 * document in a review queue, or still processing, is read-only (with no way to the
 * queue from here: the edit page itself opens the queue for those who work it); the
 * `?field=` field opens and links to the edit page; a finished queue review's activity
 * is in History; an Annotator-only user gets no link into the documents app; and the
 * evaluation baseline reaches the editor.
 */

import React from 'react';
import { act, render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { barButtonLabels, chooseMenuItem, menuItems, menuNames } from './edit-bar';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
const setSuccessMessage = vi.fn();
const controller = { save: vi.fn(), discard: vi.fn(), isDirty: vi.fn() };
const evaluation = { available: true, baseline: { inference_result: { Name: 'X' } }, marker: 'evaluation' };
const role = {
  isAdmin: true,
  isAuthor: false,
  isReviewer: false,
  isAnnotator: false,
  isReviewerOnly: false,
  canWrite: true,
  canReview: true,
  canWorkReviewQueues: true,
  isAnnotatorOnly: false,
};
let editorProps: Record<string, unknown> | null = null;
const server: { doc: Record<string, unknown> } = { doc: {} };

vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }) }));
vi.mock('../../../contexts/app', () => ({
  default: () => ({ user: { username: 'alice', signInDetails: { loginId: 'alice@example.com' } }, setSuccessMessage }),
}));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: { EvaluationBaselineBucket: 'baseline-bucket' } }) }));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => role }));
vi.mock('../../../hooks/use-configuration', () => ({ default: () => ({ mergedConfig: { classes: [] } }) }));
vi.mock('../../../hooks/use-page-thumbnails', () => ({ default: () => ({ urls: {}, failure: null }) }));
vi.mock('../use-section-evaluation', () => ({ default: () => evaluation }));
vi.mock('../../document-viewer/MarkdownViewer', () => ({
  MarkdownReport: ({ reportUri }: { reportUri: string }) => <div data-testid="summary-report">{reportUri}</div>,
}));
/** The breadcrumbs the page gives its layout. */
let layout: { root?: { href?: string } | null; crumbs?: { href?: string }[] } = {};
vi.mock('../../review-queues/ReviewQueuesLayout', () => ({
  default: ({ children, ...props }: { children: React.ReactNode }) => {
    layout = props;
    return <>{children}</>;
  },
}));
vi.mock('../SectionEditor', () => ({
  default: (props: Record<string, unknown>) => {
    editorProps = props;
    (props.controllerRef as { current: unknown }).current = controller;
    return <div data-testid="editor" data-readonly={String(props.isReadOnly)} />;
  },
}));

import DocumentEdit from '../DocumentEdit';
import { MIRROR_LAG_REASON, REGROUP_ROLE_REASON } from '../direct-edit-model';
import type { DirectEditOptions } from '../direct-edit-model';
import { inReviewQueueEditReason } from '../../common/review-status';

/** Where the screen has navigated to. */
const LocationProbe = (): React.JSX.Element => <div data-testid="location">{useLocation().pathname}</div>;
const location = () => document.querySelector('[data-testid="location"]')?.textContent;
/** The Cancel dialog, found by its title: the Reprocess confirmation is always mounted, hidden. */
const DISCARD_TITLE = 'Discard your unsaved changes?';
const discardDialog = () =>
  createWrapper(document.body)
    .findAllModals()
    .find((m) => m.findHeader().getElement().textContent === DISCARD_TITLE);

const rawDoc = (over: Record<string, unknown> = {}) => ({
  ObjectKey: 'in/doc.pdf',
  ObjectStatus: 'COMPLETED',
  EvaluationStatus: 'COMPLETED',
  Sections: [{ Id: '1', Class: 'W2', PageIds: [1], OutputJSONUri: 's3://out/in/doc.pdf/sections/1/result.json' }],
  Pages: [{ Id: 1, ImageUri: 's3://out/in/doc.pdf/pages/1/image.jpg' }],
  ...over,
});

const queryOf = (call: unknown[]) => String((call[0] as { query: string }).query);
const processCalls = () => graphql.mock.calls.filter((c) => queryOf(c).includes('processChanges'));

const renderEditor = async (seed?: DirectEditOptions['seed'], path = '/documents/in%2Fdoc.pdf/edit') => {
  render(
    <MemoryRouter initialEntries={[path]}>
      <DocumentEdit objectKey="in/doc.pdf" seed={seed} />
      <LocationProbe />
    </MemoryRouter>,
  );
  await waitFor(() => expect(editorProps).not.toBeNull());
};

const header = () => createWrapper(document.body).findHeader()!;
const barLabel = (b: ReturnType<ReturnType<typeof createWrapper>['findAllButtons']>[number]) =>
  b.findTextRegion()?.getElement().textContent ?? b.getElement().textContent;
const button = (text: string) =>
  header()
    .findActions()!
    .findAllButtons()
    .find((b) => barLabel(b) === text)!;
const disabledReason = (text: string) => {
  const b = button(text);
  b.focus();
  return b.findDisabledReason()?.getElement().textContent;
};

beforeEach(() => {
  document.body.innerHTML = '';
  editorProps = null;
  server.doc = rawDoc();
  Object.assign(role, {
    isAdmin: true,
    isAuthor: false,
    isReviewer: false,
    isAnnotator: false,
    isReviewerOnly: false,
    canWrite: true,
    canReview: true,
    canWorkReviewQueues: true,
    isAnnotatorOnly: false,
  });
  graphql.mockReset().mockImplementation(async (req: { query: string }) => {
    if (req.query.includes('processChanges')) return { data: { processChanges: { success: true } } };
    if (req.query.includes('copyToBaseline')) return { data: { copyToBaseline: { success: true, message: 'started' } } };
    return { data: { getDocument: server.doc } };
  });
  setSuccessMessage.mockReset();
  controller.save.mockReset().mockResolvedValue(true);
  controller.discard.mockReset();
  controller.isDirty.mockReset().mockReturnValue(false);
});

describe('the direct edit', () => {
  it('Refresh reads the document once more, and says so when the read fails', async () => {
    await renderEditor();
    const reads = () => graphql.mock.calls.filter((c) => queryOf(c).includes('getDocument')).length;
    const before = reads();
    button('Refresh').click();
    await waitFor(() => expect(reads()).toBe(before + 1));
    graphql.mockRejectedValueOnce(new Error('offline'));
    button('Refresh').click();
    await waitFor(() => expect(document.body.textContent).toContain('Could not reload doc.pdf: offline'));
    expect(reads()).toBe(before + 2);
    // The page stays on the document: a failed Refresh is not a failed open.
    expect(editorProps).not.toBeNull();
    expect(document.body.textContent).not.toContain("Couldn't open this document");
  });

  it('offers Reports, More Actions, Refresh, Cancel, Save and Process Changes, with no Skip', async () => {
    await renderEditor();
    expect(menuNames()).toEqual(['Reports', 'More Actions']);
    expect(barButtonLabels()).toEqual(['Refresh', 'Cancel', 'Save', 'Process Changes']);
    expect(menuItems('Reports').map((i) => i.text)).toEqual(['History', 'Summary', 'Processing Report']);
    expect(menuItems('More Actions').map((i) => i.text)).toEqual(['Edit Page Class', 'Use As Evaluation Baseline']);
    expect(editorProps!.isReadOnly).toBe(false);
  });

  it('saves a draft through the editor', async () => {
    await renderEditor();
    await act(async () => (editorProps!.onDirtyChange as (d: boolean) => void)(true));
    button('Save').click();
    await waitFor(() => expect(controller.save).toHaveBeenCalledTimes(1));
    expect(processCalls()).toHaveLength(0);
  });

  it('confirms, then calls processChanges with no section changes', async () => {
    controller.isDirty.mockReturnValue(true);
    await renderEditor();
    button('Process Changes').click();
    await waitFor(() => expect(createWrapper(document.body).findModal()).not.toBeNull());
    // The edits are saved before the confirmation, and nothing is sent until it is confirmed.
    expect(controller.save).toHaveBeenCalledTimes(1);
    expect(processCalls()).toHaveLength(0);
    const modal = createWrapper(document.body).findModal()!;
    expect(modal.findHeader().getElement().textContent).toBe('Confirm Reprocessing');
    modal
      .findFooter()!
      .findAllButtons()
      .find((b) => b.getElement().textContent === 'Confirm & Process')!
      .click();
    await waitFor(() => expect(processCalls()).toHaveLength(1));
    expect((processCalls()[0][0] as { variables: unknown }).variables).toEqual({ objectKey: 'in/doc.pdf', modifiedSections: [] });
    await waitFor(() => expect(setSuccessMessage).toHaveBeenCalled());
  });

  it('lets an Author save but not process, saying why', async () => {
    Object.assign(role, { isAdmin: false, isAuthor: true, canWrite: true, canReview: false, canWorkReviewQueues: false });
    await renderEditor();
    expect(button('Save')).toBeTruthy();
    expect(disabledReason('Process Changes')).toBe('Process Changes needs the Admin or Reviewer role.');
  });

  it('shows an Author Edit Page Class disabled, saying why', async () => {
    Object.assign(role, { isAdmin: false, isAuthor: true, canWrite: true, canReview: false, canWorkReviewQueues: false });
    await renderEditor();
    expect(menuItems('More Actions').find((i) => i.text === 'Edit Page Class')).toMatchObject({
      disabled: true,
      disabledReason: REGROUP_ROLE_REASON,
    });
  });

  it('opens a document in a review queue read-only for an Author, with no Review in queue', async () => {
    Object.assign(role, { isAdmin: false, isAuthor: true, canWrite: true, canReview: false, canWorkReviewQueues: false });
    server.doc = rawDoc({ HITLStatus: 'in_review', ObjectStatus: 'HITL_IN_PROGRESS' });
    await renderEditor();
    expect(editorProps!.isReadOnly).toBe(true);
    expect(document.body.textContent).toContain('This document is in a review queue. Edits are made in the queue.');
    expect(
      createWrapper(document.body)
        .findAllButtons()
        .find((b) => barLabel(b) === 'Review in queue'),
    ).toBeUndefined();
    const reason = inReviewQueueEditReason(false);
    expect(disabledReason('Save')).toBe(reason);
    expect(menuItems('More Actions').find((i) => i.text === 'Edit Page Class')).toMatchObject({ disabled: true, disabledReason: reason });
    expect(disabledReason('Process Changes')).toBe(reason);
  });

  it('opens read-only, saying the review is being updated, while the queue mirror lags', async () => {
    server.doc = rawDoc({ HITLStatus: 'in_review', ObjectStatus: 'HITL_IN_PROGRESS' });
    await renderEditor({ document: server.doc as never, lock: 'mirror-lag' });
    expect(editorProps!.isReadOnly).toBe(true);
    expect(document.body.textContent).toContain(
      'View only. This document is marked as in a review queue, but its review item is missing or has finished. Reload in a moment.',
    );
    expect(disabledReason('Save')).toBe(MIRROR_LAG_REASON);
    // The seeded document is not read again.
    expect(graphql.mock.calls.filter((c) => queryOf(c).includes('getDocument'))).toHaveLength(0);
  });

  it('opens on the ?field= field and copies a link to it on the edit page', async () => {
    await renderEditor(undefined, '/documents/in%2Fdoc.pdf/edit?field=LineItems%5B0%5D.Rate');
    expect(editorProps!.focusFieldPath).toBe('LineItems[0].Rate');
    const link = (editorProps!.buildFieldLink as (f: string) => string)('LineItems[0].Rate');
    expect(link).toBe(`${window.location.origin}/#/documents/in%2Fdoc.pdf/edit?field=LineItems%5B0%5D.Rate`);
  });

  it('lists a finished queue review’s activity under History, then the document’s own', async () => {
    server.doc = rawDoc({
      HITLStatus: 'complete',
      HITLReviewHistory: JSON.stringify([{ action: 'skipped', timestamp: '2026-10-20T09:00:00Z', user: 'rev@example.com' }]),
    });
    await renderEditor({
      document: server.doc as never,
      reviewActivity: [{ at: '2026-10-21T09:00:00Z', event: 'completed', actor: 'ann@example.com', details: null }],
    });
    chooseMenuItem('Reports', 'History');
    createWrapper(document.body).findTabs()!.findTabLinkByIndex(2)!.click();
    const content = createWrapper(document.body)
      .findAllModals()
      .find((m) => m.findHeader().getElement().textContent === 'History')!
      .findContent()
      .getElement().textContent;
    expect(content).toContain('ann@example.com');
    expect(content).toContain('rev@example.com');
  });

  it('gives an Annotator-only user no link into the documents app', async () => {
    Object.assign(role, { isAdmin: false, isAnnotator: true, isAnnotatorOnly: true, canWrite: false, canReview: false });
    await renderEditor();
    expect(document.querySelector('a[href^="#/documents"]')).toBeNull();
    const hrefs = [layout.root?.href, ...(layout.crumbs ?? []).map((c) => c.href)];
    expect(hrefs[0]).toBe('#/my-work/tasks');
    expect(hrefs.filter((h) => h?.startsWith('#/documents'))).toEqual([]);
  });

  it('is read-only while the document is still processing', async () => {
    server.doc = rawDoc({ ObjectStatus: 'RUNNING' });
    await renderEditor();
    expect(editorProps!.isReadOnly).toBe(true);
    expect(disabledReason('Process Changes')).toBe('Available when processing finishes');
  });

  it('titles the document with its status over the object key', async () => {
    await renderEditor();
    expect(header().findHeadingText().getElement().textContent).toBe('doc.pdf (COMPLETED)');
    expect(header().findDescription()?.getElement().textContent).toBe('in/doc.pdf');
  });

  it('lists the document review history under History › Review Activity', async () => {
    server.doc = rawDoc({
      HITLReviewHistory: JSON.stringify([
        { action: 'skipped', timestamp: '2026-10-20T09:00:00Z', user: 'rev@example.com', reason: 'Duplicate document' },
      ]),
    });
    await renderEditor();
    chooseMenuItem('Reports', 'History');
    const tabGroup = createWrapper(document.body).findTabs()!;
    tabGroup.findTabLinkByIndex(2)!.click();
    const history = createWrapper(document.body)
      .findAllModals()
      .find((m) => m.findHeader().getElement().textContent === 'History')!;
    const content = history.findContent().getElement().textContent;
    expect(content).toContain('rev@example.com');
    expect(content).toContain('Duplicate document');
  });

  it('passes the evaluation baseline to the editor', async () => {
    await renderEditor();
    expect(editorProps!.evaluation).toBe(evaluation);
  });

  it("offers Edit | View first in the bar, and shows the source in the editor's place", async () => {
    await renderEditor();
    const control = header().findActions()!.findSegmentedControl()!;
    expect(control.findSegments().map((s) => s.getElement().textContent)).toEqual(['Edit', 'View']);
    expect(editorProps!.body).toBeUndefined();
    await act(async () => control.findSegments()[1].click());
    expect(editorProps!.body).toBeTruthy();
    // The editor stays mounted behind it, with its unsaved edits.
    expect(document.querySelector('[data-testid="editor"]')).not.toBeNull();
  });

  it('offers Summary on a finished document that has one, and opens it', async () => {
    server.doc = rawDoc({ SummaryReportUri: 's3://out/in/doc.pdf/summary/summary.md' });
    await renderEditor();
    expect(menuItems('Reports').find((i) => i.text === 'Summary')).toMatchObject({ disabled: false });
    chooseMenuItem('Reports', 'Summary');
    await waitFor(() => expect(document.querySelector('[data-testid="summary-report"]')).not.toBeNull());
    expect(document.querySelector('[data-testid="summary-report"]')!.textContent).toBe('s3://out/in/doc.pdf/summary/summary.md');
  });

  it('keeps Summary, disabled, when the document has none yet', async () => {
    await renderEditor();
    expect(menuItems('Reports').find((i) => i.text === 'Summary')).toMatchObject({
      disabled: true,
      disabledReason: 'This document has no summary',
    });
  });

  it('copies a finished document to the evaluation baseline, for an Admin or Author', async () => {
    await renderEditor();
    chooseMenuItem('More Actions', 'Use As Evaluation Baseline');
    await waitFor(() => expect(graphql.mock.calls.some((c) => queryOf(c).includes('copyToBaseline'))).toBe(true));
    const call = graphql.mock.calls.find((c) => queryOf(c).includes('copyToBaseline'))!;
    expect((call[0] as { variables: unknown }).variables).toEqual({ objectKey: 'in/doc.pdf' });
    await waitFor(() => expect(setSuccessMessage).toHaveBeenCalledWith(expect.stringContaining('evaluation baseline')));
  });

  it('disables Summary and the baseline on a document in a review queue, saying why', async () => {
    server.doc = rawDoc({ HITLStatus: 'in_review', ObjectStatus: 'HITL_IN_PROGRESS', SummaryReportUri: 's3://x/summary.md' });
    await renderEditor();
    const reason = 'This document is in a review queue';
    expect(menuItems('Reports').find((i) => i.text === 'Summary')).toMatchObject({ disabled: true, disabledReason: reason });
    expect(menuItems('More Actions').find((i) => i.text === 'Use As Evaluation Baseline')).toMatchObject({
      disabled: true,
      disabledReason: reason,
    });
  });

  it('disables Summary and the baseline while the document is still processing', async () => {
    server.doc = rawDoc({ ObjectStatus: 'EXTRACTING', SummaryReportUri: 's3://x/summary.md' });
    await renderEditor();
    const reason = 'Available when processing finishes';
    expect(menuItems('Reports').find((i) => i.text === 'Summary')).toMatchObject({ disabled: true, disabledReason: reason });
    expect(menuItems('More Actions').find((i) => i.text === 'Use As Evaluation Baseline')).toMatchObject({ disabledReason: reason });
  });

  it('offers the baseline only to a role that may copy it (Admin, Author)', async () => {
    Object.assign(role, { isAdmin: false, isReviewer: true, canWrite: false });
    await renderEditor();
    expect(menuItems('More Actions').map((i) => i.text)).not.toContain('Use As Evaluation Baseline');
  });

  it('Cancel with no unsaved edits leaves for the document page at once', async () => {
    await renderEditor();
    button('Cancel').click();
    await waitFor(() => expect(location()).toBe('/documents/in%2Fdoc.pdf'));
    expect(discardDialog()).toBeUndefined();
  });

  it('Cancel with unsaved edits asks first, then discards them and leaves', async () => {
    await renderEditor();
    controller.isDirty.mockReturnValue(true);
    button('Cancel').click();
    const modal = await waitFor(() => {
      const found = discardDialog();
      expect(found).toBeDefined();
      return found!;
    });
    expect(location()).toBe('/documents/in%2Fdoc.pdf/edit');
    modal
      .findFooter()!
      .findAllButtons()
      .find((b) => b.getElement().textContent === 'Discard and leave')!
      .click();
    await waitFor(() => expect(location()).toBe('/documents/in%2Fdoc.pdf'));
    expect(controller.discard).toHaveBeenCalledTimes(1);
  });

  it('Keep editing stays on the screen with the edits', async () => {
    await renderEditor();
    controller.isDirty.mockReturnValue(true);
    button('Cancel').click();
    const modal = await waitFor(() => {
      const found = discardDialog();
      expect(found).toBeDefined();
      return found!;
    });
    modal
      .findFooter()!
      .findAllButtons()
      .find((b) => b.getElement().textContent === 'Keep editing')!
      .click();
    await waitFor(() => expect(discardDialog()).toBeUndefined());
    expect(location()).toBe('/documents/in%2Fdoc.pdf/edit');
    expect(controller.discard).not.toHaveBeenCalled();
  });

  it('saves a Reviewer-only caller’s edits through saveSectionReviewEdits, never uploadDocument', async () => {
    Object.assign(role, {
      isAdmin: false,
      isReviewer: true,
      isReviewerOnly: true,
      canWrite: false,
      canReview: true,
      canWorkReviewQueues: false,
    });
    server.doc = rawDoc({ HITLReviewOwner: 'alice' });
    graphql.mockImplementation(async (req: { query: string }) => {
      if (req.query.includes('saveSectionReviewEdits')) return { data: { saveSectionReviewEdits: { success: true } } };
      return { data: { getDocument: server.doc } };
    });
    await renderEditor();
    await act(async () => {
      await (editorProps!.onSave as (id: string, d: Record<string, unknown>) => Promise<unknown>)('1', { inference_result: { Name: 'B' } });
    });
    const sent = graphql.mock.calls.map(queryOf);
    expect(sent.filter((q) => q.includes('saveSectionReviewEdits'))).toHaveLength(1);
    expect(sent.some((q) => q.includes('uploadDocument'))).toBe(false);
    expect(sent.some((q) => q.includes('completeSectionReview'))).toBe(false);
  });
});
