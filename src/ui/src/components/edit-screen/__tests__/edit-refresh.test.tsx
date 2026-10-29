// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The edit page's Refresh. What has to hold: the bar offers Refresh after More Actions
 * and before Skip (after Reports, before Claim, in Claim mode), reports 'refresh', and is
 * disabled while another action runs; with no unsaved edits it re-reads at once, then
 * re-reads the open section and keeps it open; with unsaved edits it asks first ("Discard
 * your changes and reload?"), Keep editing changes nothing, and Discard and reload
 * discards the edits before re-reading; a failed read is reported through the model's
 * failures and the open section is not re-read.
 */

import React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const setErrorMessage = vi.fn();
const setSuccessMessage = vi.fn();
const order: string[] = [];
const controller = { save: vi.fn(), discard: vi.fn(() => order.push('discard')), isDirty: vi.fn() };
let editorProps: Record<string, unknown> | null = null;

vi.mock('../../../contexts/app', () => ({ default: () => ({ setErrorMessage, setSuccessMessage }) }));
vi.mock('../use-edit-exit', () => ({ default: () => vi.fn() }));
vi.mock('../../review-queues/ReviewQueuesLayout', () => ({ default: ({ children }: { children: React.ReactNode }) => <>{children}</> }));
vi.mock('../../../hooks/use-page-thumbnails', () => ({ default: () => ({ urls: {}, failure: null }) }));
vi.mock('../../document-viewer/FileViewer', () => ({ default: () => <div data-testid="source-viewer" /> }));
vi.mock('../SectionEditor', () => ({
  default: (props: Record<string, unknown>) => {
    editorProps = props;
    (props.controllerRef as { current: unknown }).current = controller;
    // The user has moved to section 2 since the page opened.
    const select = props.onSelectedSectionChange as (id: string) => void;
    React.useEffect(() => select('2'), [select]);
    return <div data-testid="editor" />;
  },
}));

import EditPage from '../EditPage';
import EditScreen from '../EditScreen';
import type { EditModel, EditReady } from '../edit-model';
import { barButtonLabels, barButtons, buttonLabel } from './edit-bar';

const ready = (over: Partial<EditReady> = {}): EditModel => ({
  navigation: { trail: [] },
  state: {
    kind: 'ready',
    header: { title: 'a.pdf', status: 'Done', objectKey: 'in/a.pdf', source: null, offerReport: false },
    notices: [],
    bar: { showSave: true, showCancel: true, showRefresh: true, offerHistory: false },
    editor: {
      sections: [
        { sectionId: '1', resultUri: 's3://o/1', pageIds: [] },
        { sectionId: '2', resultUri: 's3://o/2', pageIds: [] },
      ],
      pages: [],
      readOnly: false,
      loadSection: vi.fn(),
      saveSection: vi.fn(),
      reloadToken: 3,
      initialSectionId: '1',
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

const button = (name: string) => {
  const found = barButtons().find((b) => buttonLabel(b) === name);
  if (!found) throw new Error(`no button ${name}`);
  return found;
};
const modal = () => createWrapper(document.body).findModal();

beforeEach(() => {
  document.body.innerHTML = '';
  editorProps = null;
  order.length = 0;
  setErrorMessage.mockReset();
  controller.discard.mockClear();
  controller.isDirty.mockReset().mockReturnValue(false);
});

describe('the edit screen bar with Refresh', () => {
  it('puts Refresh after the menus and before Skip, and reports refresh', () => {
    const onAction = vi.fn();
    render(
      <EditScreen
        title="a.pdf"
        objectKey="in/a.pdf"
        actions={{ busy: null, onAction, showRefresh: true, showSkip: true, showCancel: true, showSave: true }}
      >
        <div />
      </EditScreen>,
    );
    expect(barButtonLabels()).toEqual(['Refresh', 'Skip', 'Cancel', 'Save', 'Process Changes']);
    button('Refresh').click();
    expect(onAction).toHaveBeenCalledWith('refresh');
  });

  it('disables Refresh while another action runs', () => {
    render(
      <EditScreen title="a.pdf" objectKey="in/a.pdf" actions={{ busy: 'save', onAction: vi.fn(), showRefresh: true }}>
        <div />
      </EditScreen>,
    );
    const refresh = createWrapper(document.body)
      .findHeader()!
      .findActions()!
      .findAllButtons()
      .find((b) => buttonLabel(b.getElement()) === 'Refresh')!;
    expect(refresh.getElement().getAttribute('aria-disabled')).toBe('true');
    refresh.focus();
    expect(refresh.findDisabledReason()?.getElement().textContent).toBe('Working…');
  });

  it('shows Refresh before Claim in Claim mode', () => {
    render(
      <EditScreen title="a.pdf" objectKey="in/a.pdf" actions={{ busy: null, onAction: vi.fn(), showRefresh: true, showClaim: true }}>
        <div />
      </EditScreen>,
    );
    expect(barButtonLabels()).toEqual(['Refresh', 'Claim']);
  });
});

describe('Refresh on the edit page', () => {
  it('re-reads at once with no unsaved edits, then re-reads the open section and keeps it open', async () => {
    const refresh = vi.fn().mockResolvedValue(undefined);
    renderPage(ready({ refresh }));
    expect(editorProps).toMatchObject({ reloadToken: 3, initialSectionId: '1' });
    button('Refresh').click();
    await waitFor(() => expect(editorProps).toMatchObject({ reloadToken: 4, initialSectionId: '2' }));
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(modal()).toBeNull();
  });

  it('asks first with unsaved edits: Keep editing changes nothing, Discard and reload discards then re-reads', async () => {
    controller.isDirty.mockReturnValue(true);
    const refresh = vi.fn(async () => {
      order.push('refresh');
    });
    renderPage(ready({ refresh }));
    button('Refresh').click();
    await waitFor(() => expect(modal()).not.toBeNull());
    expect(modal()!.findHeader().getElement().textContent).toBe('Discard your changes and reload?');
    expect(modal()!.findContent().getElement().textContent).toBe(
      'The changes you made since the last save are lost. Anything already saved is kept.',
    );
    const footer = () => modal()!.findFooter()!.findAllButtons();
    expect(footer().map((b) => b.getElement().textContent)).toEqual(['Keep editing', 'Discard and reload']);
    footer()[0].click();
    await waitFor(() => expect(modal()).toBeNull());
    expect(refresh).not.toHaveBeenCalled();
    expect(controller.discard).not.toHaveBeenCalled();

    button('Refresh').click();
    await waitFor(() => expect(modal()).not.toBeNull());
    footer()[1].click();
    await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
    expect(order).toEqual(['discard', 'refresh']);
    await waitFor(() => expect(editorProps).toMatchObject({ reloadToken: 4 }));
  });

  it('reports a failed read through the model and does not re-read the open section', async () => {
    const describeFailure = vi.fn((_a: string, err: unknown) => `Could not reload a.pdf: ${(err as Error).message}`);
    const refresh = vi.fn().mockRejectedValue(new Error('offline'));
    renderPage(ready({ refresh, failures: { surface: 'flash', describe: describeFailure } }));
    button('Refresh').click();
    await waitFor(() => expect(setErrorMessage).toHaveBeenCalledWith('Could not reload a.pdf: offline'));
    expect(describeFailure).toHaveBeenCalledWith('refresh', expect.any(Error));
    expect(editorProps).toMatchObject({ reloadToken: 3 });
  });
});
