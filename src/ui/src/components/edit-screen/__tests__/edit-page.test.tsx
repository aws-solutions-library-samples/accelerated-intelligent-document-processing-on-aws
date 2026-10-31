// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The edit page over hand-built models. What has to hold: the loading text and the
 * failure Alert (with Retry) come from the model; a ready model's bar is what the model
 * says; Save saves through the editor and toasts the model's message; Process Changes
 * with a confirmation saves unsaved edits first, asks, then runs and leaves; without one
 * it runs and leaves, dropping the finished work item from Tasks; a failure is shown
 * where the model says (inline notice or flash bar), or not at all when the model says
 * it is reported elsewhere; Edit Page Grouping opens only when the model's check allows it,
 * and a save error stays inside the open board; Skip passes the reason and note; Claim
 * toasts the model's message.
 */

import React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { act, render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const setErrorMessage = vi.fn();
const setSuccessMessage = vi.fn();
const exit = vi.fn();
const controller = { save: vi.fn(), discard: vi.fn(), isDirty: vi.fn() };
let editorProps: Record<string, unknown> | null = null;
let boardProps: Record<string, unknown> | null = null;

vi.mock('../../../contexts/app', () => ({ default: () => ({ setErrorMessage, setSuccessMessage }) }));
vi.mock('../use-edit-exit', () => ({ default: () => exit }));
vi.mock('../../review-queues/ReviewQueuesLayout', () => ({ default: ({ children }: { children: React.ReactNode }) => <>{children}</> }));
vi.mock('../../../hooks/use-page-thumbnails', () => ({ default: () => ({ urls: {}, failure: null }) }));
vi.mock('../../document-viewer/FileViewer', () => ({ default: () => <div data-testid="source-viewer" /> }));
vi.mock('../SectionEditor', () => ({
  default: (props: Record<string, unknown>) => {
    editorProps = props;
    (props.controllerRef as { current: unknown }).current = controller;
    const regroup = props.regroup as { disabledReason?: string; onClick: () => void } | undefined;
    const regroupButton = regroup && (
      <button
        data-testid="editor-regroup"
        disabled={!!regroup.disabledReason}
        data-reason={regroup.disabledReason}
        onClick={regroup.onClick}
      >
        Edit Page Grouping
      </button>
    );
    return <div data-testid="editor">{regroupButton}</div>;
  },
}));
vi.mock('../../common/PageGroupingEditor', () => ({
  default: (props: Record<string, unknown>) => {
    boardProps = props;
    return <div data-testid="board">{props.consequence as React.ReactNode}</div>;
  },
}));

import EditPage from '../EditPage';
import type { EditModel, EditReady } from '../edit-model';
import { barButtonLabels, menuItems } from './edit-bar';
import { chooseRegroup } from './regroup-stand-in';

const NAV = { trail: [] };

const ready = (over: Partial<EditReady> = {}): EditModel => ({
  navigation: NAV,
  state: {
    kind: 'ready',
    header: { title: 'a.pdf', status: 'Done', objectKey: 'in/a.pdf', source: { objectKey: 'in/a.pdf' }, offerReport: true },
    notices: [],
    bar: { showSave: true, showCancel: true, showRegroup: true, offerHistory: true },
    editor: {
      sections: [{ sectionId: '1', resultUri: 's3://o/1', pageIds: [] }],
      pages: [],
      readOnly: false,
      loadSection: vi.fn(),
      saveSection: vi.fn(),
      emptyText: 'Nothing to edit.',
    },
    board: null,
    history: { reviewActivity: [] },
    process: { run: vi.fn().mockResolvedValue(null) },
    cancelTo: '/documents/in%2Fa.pdf',
    failures: { surface: 'flash', describe: (_a, err) => `Failed: ${(err as Error).message}` },
    ...over,
  },
});

const renderPage = (model: EditModel) =>
  render(
    <MemoryRouter>
      <EditPage model={model} />
    </MemoryRouter>,
  );

const button = (text: string) =>
  createWrapper(document.body)
    .findHeader()!
    .findActions()!
    .findAllButtons()
    .find((b) => (b.findTextRegion()?.getElement().textContent ?? b.getElement().textContent) === text)!;

beforeEach(() => {
  document.body.innerHTML = '';
  editorProps = null;
  boardProps = null;
  setErrorMessage.mockReset();
  setSuccessMessage.mockReset();
  exit.mockReset();
  controller.save.mockReset().mockResolvedValue(true);
  controller.isDirty.mockReset().mockReturnValue(false);
});

describe('the edit page', () => {
  it('shows the model’s loading text', () => {
    renderPage({ navigation: NAV, state: { kind: 'loading', text: 'Loading document…' } });
    expect(document.body.textContent).toContain('Loading document…');
    expect(editorProps).toBeNull();
  });

  it('shows a failure with Retry', () => {
    const retry = vi.fn();
    renderPage({ navigation: NAV, state: { kind: 'failure', type: 'error', header: "Couldn't open this document", body: 'Boom', retry } });
    const alert = createWrapper(document.body).findAlert()!;
    expect(alert.findHeader()!.getElement().textContent).toBe("Couldn't open this document");
    alert.findActionSlot()!.findButton()!.click();
    expect(retry).toHaveBeenCalled();
  });

  it('renders the bar the model describes', () => {
    renderPage(ready({ bar: { showSkip: true, showSave: true, offerHistory: false } }));
    expect(barButtonLabels()).toEqual(['Skip', 'Save', 'Process Changes']);
    expect(menuItems('Reports').map((i) => i.text)).toEqual(['Processing Report']);
    expect(editorProps).toMatchObject({ isReadOnly: false });
  });

  it('saves through the editor and toasts the model’s message', async () => {
    renderPage(ready({ savedMessage: 'Saved your changes to a.pdf.' }));
    button('Save').click();
    await waitFor(() => expect(setSuccessMessage).toHaveBeenCalledWith('Saved your changes to a.pdf.'));
    expect(controller.save).toHaveBeenCalledTimes(1);
  });

  it('with a confirmation, saves unsaved edits first, asks, then runs and leaves', async () => {
    const leave = { to: '/documents/in%2Fa.pdf', message: 'Processing.' };
    const run = vi.fn().mockResolvedValue(leave);
    controller.isDirty.mockReturnValue(true);
    renderPage(ready({ process: { confirm: { bdaMode: false }, run } }));
    button('Process Changes').click();
    await waitFor(() => expect(controller.save).toHaveBeenCalledTimes(1));
    expect(run).not.toHaveBeenCalled();
    const modal = createWrapper(document.body)
      .findAllModals()
      .find((m) =>
        m
          .findFooter()
          ?.findAllButtons()
          .some((b) => b.getElement().textContent === 'Confirm & Process'),
      );
    expect(modal).toBeDefined();
    await act(async () =>
      modal!
        .findFooter()!
        .findAllButtons()
        .find((b) => b.getElement().textContent === 'Confirm & Process')!
        .click(),
    );
    await waitFor(() => expect(exit).toHaveBeenCalledWith(leave));
    expect(controller.save.mock.invocationCallOrder[0]).toBeLessThan(run.mock.invocationCallOrder[0]);
  });

  it('without a confirmation, runs and leaves with the work item settled', async () => {
    const leave = { to: '/my-work/tasks', message: 'Done.', workItemId: 'p:in/a.pdf' };
    renderPage(ready({ process: { run: vi.fn().mockResolvedValue(leave) } }));
    button('Process Changes').click();
    await waitFor(() => expect(exit).toHaveBeenCalledWith(leave));
  });

  it('shows no error when the model says the failure is reported elsewhere', async () => {
    renderPage(
      ready({ process: { run: vi.fn().mockRejectedValue(new Error('x')) }, failures: { surface: 'flash', describe: () => null } }),
    );
    button('Process Changes').click();
    await waitFor(() => expect(button('Process Changes').getElement().getAttribute('aria-disabled')).not.toBe('true'));
    expect(setErrorMessage).not.toHaveBeenCalled();
    expect(createWrapper(document.body).findAllAlerts()).toHaveLength(0);
    expect(exit).not.toHaveBeenCalled();
  });

  it('shows a failure in the flash bar, or inline, as the model says', async () => {
    const after = vi.fn();
    renderPage(
      ready({
        process: { run: vi.fn().mockRejectedValue(new Error('nope')) },
        failures: { surface: 'flash', describe: (_a, e) => `Failed: ${(e as Error).message}`, after },
      }),
    );
    button('Process Changes').click();
    await waitFor(() => expect(setErrorMessage).toHaveBeenCalledWith('Failed: nope'));
    expect(after).toHaveBeenCalledWith('process');
    document.body.innerHTML = '';
    renderPage(
      ready({
        process: { run: vi.fn().mockRejectedValue(new Error('nope')) },
        failures: { surface: 'notice', describe: (_a, e) => `Failed: ${(e as Error).message}` },
      }),
    );
    button('Process Changes').click();
    await waitFor(() => expect(createWrapper(document.body).findAlert()?.getElement().textContent).toContain('Failed: nope'));
    expect(setErrorMessage).toHaveBeenCalledTimes(1);
  });

  describe('Edit Page Grouping', () => {
    const board = (over: Record<string, unknown> = {}) => ({
      pages: { kind: 'rendered' as const, pages: [{ id: 1, imageUri: 'blob:1' }] },
      sections: () => [{ sectionId: '1', documentClass: 'W2', pageIds: [1] }],
      canChangeClass: true,
      consequence: 'Moving pages rewrites the grouping.',
      save: vi.fn().mockResolvedValue('Saved the grouping.'),
      ...over,
    });

    it('opens after the model’s check allows it, and stays closed when it does not', async () => {
      const beforeOpen = vi.fn().mockResolvedValue(false);
      const { unmount } = renderPage(ready({ board: board({ beforeOpen }) }));
      chooseRegroup();
      await waitFor(() => expect(beforeOpen).toHaveBeenCalled());
      expect(boardProps).toBeNull();
      unmount();
      beforeOpen.mockResolvedValue(true);
      renderPage(ready({ board: board({ beforeOpen }) }));
      chooseRegroup();
      await waitFor(() =>
        expect(boardProps).toMatchObject({ presentation: 'modal', title: 'Edit Page Grouping', saveLabel: 'Save page grouping' }),
      );
    });

    it('keeps the board open and shows a save error inside it', async () => {
      const save = vi.fn().mockRejectedValue(new Error('The page grouping could not be saved.'));
      renderPage(ready({ board: board({ save }) }));
      chooseRegroup();
      await act(async () => (boardProps!.onSave as (n: unknown[]) => Promise<void>)([]));
      const shown = document.querySelector('[data-testid="board"]');
      expect(shown).not.toBeNull();
      expect(shown!.textContent).toContain('The page grouping could not be saved.');
      expect(setErrorMessage).not.toHaveBeenCalled();
    });

    it('closes and toasts after a save', async () => {
      renderPage(ready({ board: board() }));
      chooseRegroup();
      await act(async () => (boardProps!.onSave as (n: unknown[]) => Promise<void>)([]));
      expect(document.querySelector('[data-testid="board"]')).toBeNull();
      expect(setSuccessMessage).toHaveBeenCalledWith('Saved the grouping.');
    });
  });

  it('passes the skip reason and note, then leaves', async () => {
    const leave = { to: '/q', message: 'Returned.' };
    const run = vi.fn().mockResolvedValue(leave);
    renderPage(
      ready({
        bar: { showSkip: true, offerHistory: true },
        skip: { header: 'Skip this item', reasons: ['Out of time'], body: 'Back to the queue.', run },
      }),
    );
    button('Skip').click();
    const modal = createWrapper(document.body).findModal()!;
    const select = modal.findContent().findSelect()!;
    select.openDropdown();
    select.selectOptionByValue('Out of time');
    modal.findContent().findTextarea()!.setTextareaValue('tax');
    modal
      .findFooter()!
      .findAllButtons()
      .find((b) => b.getElement().textContent === 'Skip')!
      .click();
    await waitFor(() => expect(run).toHaveBeenCalledWith('Out of time', 'tax'));
    await waitFor(() => expect(exit).toHaveBeenCalledWith(leave));
  });

  it('claims and toasts the model’s message', async () => {
    const claim = vi.fn().mockResolvedValue('Claimed a.pdf. It is in Home › Tasks.');
    renderPage(ready({ bar: { showClaim: true, offerHistory: true }, claim }));
    button('Claim').click();
    await waitFor(() => expect(setSuccessMessage).toHaveBeenCalledWith('Claimed a.pdf. It is in Home › Tasks.'));
  });

  it('shows the empty text when there are no sections, and the source in View', async () => {
    const model = ready();
    if (model.state.kind === 'ready' && model.state.editor) model.state.editor.sections = [];
    renderPage(model);
    expect(document.body.textContent).toContain('Nothing to edit.');
    expect(editorProps).toBeNull();
    const control = createWrapper(document.body).findHeader()!.findActions()!.findSegmentedControl()!;
    await act(async () => control.findSegments()[1].click());
    expect(document.querySelector('[data-testid="source-viewer"]')).not.toBeNull();
  });
});
