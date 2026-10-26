// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The edit screen on a direct edit. What has to hold: the bar is History, Edit Page
 * Grouping, Cancel, Save and Reprocess, with no Skip; the title is the document and its
 * status over the object key; the editor is the Edit Document widget; Save saves through
 * the editor; Cancel leaves for the document page, asking first when there are unsaved
 * edits; Reprocess confirms, then calls processChanges with no section changes; an Author can save but not process; a
 * document in a review queue, or still processing, is read-only, and an in-review one
 * offers "Review in queue" to an Annotator; and the evaluation baseline reaches the
 * editor.
 */

import React from 'react';
import { act, render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { MemoryRouter, useLocation } from 'react-router-dom';
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
vi.mock('../SectionEditor', () => ({
  default: (props: Record<string, unknown>) => {
    editorProps = props;
    (props.controllerRef as { current: unknown }).current = controller;
    return <div data-testid="editor" data-readonly={String(props.isReadOnly)} />;
  },
}));

import DocumentEdit from '../DocumentEdit';

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

const renderEditor = async () => {
  render(
    <MemoryRouter initialEntries={['/documents/in%2Fdoc.pdf/edit']}>
      <DocumentEdit objectKey="in/doc.pdf" />
      <LocationProbe />
    </MemoryRouter>,
  );
  await waitFor(() => expect(editorProps).not.toBeNull());
};

const header = () => createWrapper(document.body).findHeader()!;
const barLabel = (b: ReturnType<ReturnType<typeof createWrapper>['findAllButtons']>[number]) =>
  b.findTextRegion()?.getElement().textContent ?? b.getElement().textContent;
const bar = () => header().findActions()!.findAllButtons().map(barLabel);
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
  });
  graphql.mockReset().mockImplementation(async (req: { query: string }) => {
    if (req.query.includes('processChanges')) return { data: { processChanges: { success: true } } };
    return { data: { getDocument: server.doc } };
  });
  setSuccessMessage.mockReset();
  controller.save.mockReset().mockResolvedValue(true);
  controller.discard.mockReset();
  controller.isDirty.mockReset().mockReturnValue(false);
});

describe('the direct edit', () => {
  it('offers History, Edit Page Grouping, Cancel, Save and Reprocess, with no Skip', async () => {
    await renderEditor();
    expect(bar()).toEqual(['History', 'Edit Page Grouping', 'Cancel', 'Save', 'Reprocess']);
    expect(editorProps!.variant).toBe('widget');
    expect(editorProps!.title).toBeUndefined();
    expect(editorProps!.isReadOnly).toBe(false);
    expect(editorProps!.actionsPlacement).toBe('external');
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
    button('Reprocess').click();
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
    expect(disabledReason('Reprocess')).toBe('Reprocess needs the Admin or Reviewer role.');
  });

  it('opens a document in a review queue read-only, with Review in queue for an Annotator', async () => {
    Object.assign(role, { isAdmin: false, isAnnotator: true, canWrite: false, canReview: false, canWorkReviewQueues: true });
    server.doc = rawDoc({ HITLStatus: 'in_review', ObjectStatus: 'HITL_IN_PROGRESS' });
    await renderEditor();
    expect(editorProps!.isReadOnly).toBe(true);
    expect(document.body.textContent).toContain('This document is in a review queue. Edits are made in the queue.');
    const review = createWrapper(document.body)
      .findAllButtons()
      .find((b) => barLabel(b) === 'Review in queue');
    expect(review?.getElement().getAttribute('href')).toBe('#/review-queues/items/in%2Fdoc.pdf');
    expect(disabledReason('Save')).toBe('This document is in a review queue. Choose Review to work on it there.');
    expect(disabledReason('Edit Page Grouping')).toBe('This document is in a review queue. Choose Review to work on it there.');
    expect(disabledReason('Reprocess')).toBe('This document is in a review queue. Choose Review to work on it there.');
  });

  it('is read-only while the document is still processing', async () => {
    server.doc = rawDoc({ ObjectStatus: 'RUNNING' });
    await renderEditor();
    expect(editorProps!.isReadOnly).toBe(true);
    expect(disabledReason('Reprocess')).toBe('Available when processing finishes');
  });

  it('titles the document with its status over the object key, with no Edit history tab in the editor', async () => {
    await renderEditor();
    expect(header().findHeadingText().getElement().textContent).toBe('doc.pdf (COMPLETED)');
    expect(header().findDescription()?.getElement().textContent).toBe('in/doc.pdf');
    expect(editorProps!.showHistoryTab).toBe(false);
  });

  it('lists the document review history under History › Review Activity', async () => {
    server.doc = rawDoc({
      HITLReviewHistory: JSON.stringify([
        { action: 'skipped', timestamp: '2026-10-20T09:00:00Z', user: 'rev@example.com', reason: 'Duplicate document' },
      ]),
    });
    await renderEditor();
    button('History').click();
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
});
