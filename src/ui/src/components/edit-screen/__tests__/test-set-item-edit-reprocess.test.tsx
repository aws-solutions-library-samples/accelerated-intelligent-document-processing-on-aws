// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The primary action on a Test Studio document. What has to hold: the bar is Reports
 * (History, Processing Report), More Actions (Edit Page Grouping), Skip, Cancel, Save and
 * Process Changes, for a document with a review copy and for authored ground truth alike;
 * Save keeps a draft (the adapter's draft save) and never leaves the page; the primary
 * action saves the open section's unsaved edits first, then applies every section and
 * leaves only when the server says the document is reviewed, staying with the reason
 * otherwise; switching sections asks before discarding; the Edit | View switch is first in the bar and shows the source
 * in the Document Editor's place; and the page gives the breadcrumbs the set's name.
 */

import React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { act, render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const applyAndComplete = vi.fn();
const saveDraft = vi.fn();
const exit = vi.fn();
const setErrorMessage = vi.fn();
const setSuccessMessage = vi.fn();
const controller = { save: vi.fn(), discard: vi.fn(), isDirty: vi.fn() };
let editorProps: Record<string, unknown> | null = null;
let boardProps: Record<string, unknown> | null = null;
/** Overrides of the document the adapter returns (authored ground truth has no review copy). */
const docOver: Record<string, unknown> = {};

vi.mock('../test-set-item-edit-adapter', () => ({
  NOT_ASSIGNED_MESSAGE: 'not assigned',
  useTestSetItemEdit: () => ({
    queue: { testSetId: 'ts-1', name: 'Invoices', skipReasons: ['Out of time'], documents: [] },
    doc: {
      objectKey: 'a.pdf',
      inputKey: 'ts-1/input/a.pdf',
      claimedByMe: true,
      assignedTo: 'ann@example.com',
      reviewed: false,
      skipped: false,
      labelSource: 'draft-machine',
      reviewObjectKey: 'run/a.pdf',
      sections: [
        { sectionId: '1', baselineKey: 'b1', documentClass: 'Invoice', pageIndices: [0] },
        { sectionId: '2', baselineKey: 'b2', documentClass: 'Invoice', pageIndices: [1] },
      ],
      ...docOver,
    },
    draft: { baseVersion: 1, draftVersion: 2 },
    loading: false,
    loadError: null,
    reload: vi.fn(),
    openDraft: vi.fn(),
    saveDraft: (...a: unknown[]) => saveDraft(...a),
    loadSection: vi.fn(),
    reloadToken: 0,
    applyAndComplete: (...a: unknown[]) => applyAndComplete(...a),
    skip: vi.fn(),
  }),
}));
// Mocked at stable seams (the shared editor and the board), so the suite holds whichever
// component wires the test-set editor between them.
vi.mock('../SectionEditor', () => ({
  default: (props: Record<string, unknown>) => {
    editorProps = props;
    (props.controllerRef as { current: unknown }).current = controller;
    const ctx = { data: null, setData: vi.fn(), selectedSectionId: '1', hasChanges: false };
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
    return (
      <div data-testid="editor">
        {regroupButton}
        {(props.headerExtras as ((c: unknown) => React.ReactNode) | undefined)?.(ctx)}
      </div>
    );
  },
}));
vi.mock('../../common/PageGroupingEditor', () => ({
  default: (props: Record<string, unknown>) => {
    boardProps = props;
    return <div data-testid="board" />;
  },
}));
vi.mock('../../../hooks/use-test-doc-pages', () => ({
  default: () => ({
    pages: [
      { Id: '1', ImageUri: 'blob:1' },
      { Id: '2', ImageUri: 'blob:2' },
    ],
    isLoading: false,
    error: null,
    previewUnavailable: false,
  }),
}));
vi.mock('../../../hooks/use-configuration', () => ({ default: () => ({ mergedConfig: null }) }));
vi.mock('../../../hooks/use-configuration-versions', () => ({ default: () => ({ versions: [] }) }));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
/** The breadcrumbs the page gives its layout. */
let layoutTrail: { text: string; href: string }[] | null = null;
vi.mock('../../review-queues/ReviewQueuesLayout', () => ({
  default: ({ children, trail }: { children: React.ReactNode; trail?: { text: string; href: string }[] }) => {
    layoutTrail = trail ?? null;
    return <>{children}</>;
  },
}));
vi.mock('../../test-studio/annotation-edit-flags', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../test-studio/annotation-edit-flags')>()),
  annotationEditFlags: () => ({
    isReadOnly: false,
    canChangeClass: true,
    saveViaReview: true,
    canSaveViaReview: true,
    canSaveDirectToBaseline: false,
  }),
}));
vi.mock('../use-edit-exit', () => ({ default: () => exit, useLeavePath: () => (queuePath: string) => queuePath }));
vi.mock('../../../contexts/app', () => ({ default: () => ({ setErrorMessage, setSuccessMessage }) }));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: { TestSetBucket: 'test-set-bucket' } }) }));
vi.mock('../../../hooks/use-review-work', () => ({ useOptionalReviewWork: () => null }));
vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({
    isAdmin: false,
    isAuthor: false,
    isReviewer: false,
    isAnnotator: true,
    canAnnotate: true,
    loading: false,
    allowedConfigVersions: null,
  }),
}));
vi.mock('../../document-viewer/FileViewer', () => ({ default: () => <div data-testid="source-viewer" /> }));

import TestSetItemEdit from '../TestSetItemEdit';
import { barButtonLabels, menuItems, menuNames } from './edit-bar';
import { chooseRegroup, regroupItem } from './regroup-stand-in';

const renderScreen = (path = '/') =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <TestSetItemEdit testSetId="ts-1" objectKey="a.pdf" />
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
  for (const k of Object.keys(docOver)) delete docOver[k];
  applyAndComplete.mockReset().mockResolvedValue({ reviewed: true });
  saveDraft.mockReset().mockResolvedValue(undefined);
  exit.mockReset();
  setErrorMessage.mockReset();
  setSuccessMessage.mockReset();
  layoutTrail = null;
  controller.isDirty.mockReset().mockReturnValue(true);
  controller.save.mockReset().mockResolvedValue(true);
});

describe('the primary action on a Test Studio document', () => {
  it('offers Refresh, Skip, Cancel, Save and Process Changes for a document with a review copy, with no Summary or baseline', () => {
    renderScreen();
    // Test Studio has no summary, and its labels already are the baseline, so More Actions
    // has nothing in it and is not shown; Edit Page Grouping is on the Document Editor panel.
    expect(menuNames()).toEqual(['Reports']);
    expect(barButtonLabels()).toEqual(['Refresh', 'Skip', 'Cancel', 'Save', 'Process Changes']);
    expect(menuItems('Reports').map((i) => i.text)).toEqual(['History', 'Processing Report']);
    expect(regroupItem()).toMatchObject({ disabled: false });
  });

  it('offers Reprocess for authored ground truth too, saying nothing is reprocessed', () => {
    docOver.reviewObjectKey = null;
    renderScreen();
    expect(barButtonLabels()).toEqual(['Refresh', 'Skip', 'Cancel', 'Save', 'Process Changes']);
    expect(button('Process Changes').getElement().closest('span[title]')?.getAttribute('title')).toContain('nothing is reprocessed');
  });

  it('saves a draft first, then applies every section and exits only on the server’s reviewed result', async () => {
    renderScreen();
    button('Process Changes').click();
    await waitFor(() => expect(exit).toHaveBeenCalledTimes(1));
    expect(controller.save).toHaveBeenCalledTimes(1);
    expect(applyAndComplete).toHaveBeenCalledTimes(1);
    expect(controller.save.mock.invocationCallOrder[0]).toBeLessThan(applyAndComplete.mock.invocationCallOrder[0]);
    expect(exit.mock.calls[0][0].message).toContain('Its labels are marked reviewed.');
  });

  it('stays and shows the reason when the server says the document is not reviewed yet', async () => {
    applyAndComplete.mockResolvedValue({ reviewed: false });
    renderScreen();
    button('Process Changes').click();
    await waitFor(() =>
      expect(setErrorMessage).toHaveBeenCalledWith(
        'Not every section of a.pdf is marked reviewed yet. Check its sections and choose Process Changes again.',
      ),
    );
    expect(exit).not.toHaveBeenCalled();
  });

  it('stops when the save fails', async () => {
    controller.save.mockResolvedValue(false);
    renderScreen();
    button('Process Changes').click();
    await waitFor(() => expect(controller.save).toHaveBeenCalled());
    expect(applyAndComplete).not.toHaveBeenCalled();
    expect(exit).not.toHaveBeenCalled();
  });

  it('saves a draft from Save, which reviews nothing and never leaves, even for every section', async () => {
    renderScreen();
    await act(async () => (editorProps!.onDirtyChange as (d: boolean) => void)(true));
    button('Save').click();
    await waitFor(() => expect(controller.save).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(setSuccessMessage).toHaveBeenCalledWith(
        'Saved your changes to a.pdf. They count as reviewed once you choose Process Changes.',
      ),
    );
    // The editor saves through the adapter's draft save, for each section in turn.
    const save = editorProps!.onSave as (id: string, d: Record<string, unknown>) => Promise<unknown>;
    await act(async () => {
      await save('1', { inference_result: {} });
      await save('2', { inference_result: {} });
    });
    expect(saveDraft.mock.calls.map((c) => c[0])).toEqual(['1', '2']);
    expect(applyAndComplete).not.toHaveBeenCalled();
    expect(exit).not.toHaveBeenCalled();
  });

  it('asks before switching sections with unsaved edits, rather than saving them', () => {
    renderScreen();
    expect(editorProps!.saveOnSectionSwitch).toBeUndefined();
    // The editor's default: ask to stay or discard.
    expect(editorProps!.confirmSectionSwitch).toBeUndefined();
  });

  it('places save and discard in the top bar, and page grouping on the Document Editor panel', () => {
    renderScreen();
    // The editor shows no Save of its own: Save is the bar's.
    expect(barButtonLabels()).toContain('Save');
    expect(boardProps).toBeNull();
    chooseRegroup();
    // The board opens as a dialog, not inline in the editor.
    expect(boardProps).toMatchObject({ presentation: 'modal', title: 'Edit Page Grouping' });
  });

  it('puts the Edit | View switch in the bar, opening on Edit', async () => {
    renderScreen();
    expect(editorProps!.body).toBeUndefined();
    const control = createWrapper(document.body).findHeader()!.findActions()!.findSegmentedControl()!;
    expect(control.findSegments().map((x) => x.getElement().textContent)).toEqual(['Edit', 'View']);
    expect(control.findSelectedSegment()!.getElement().textContent).toBe('Edit');
    await act(async () => control.findSegments()[1].click());
    // The source document replaces the panes; the editor itself stays mounted.
    const { container: shown } = render(<>{editorProps!.body as React.ReactNode}</>);
    expect(shown.querySelector('[data-testid="source-viewer"]')).not.toBeNull();
  });

  it('titles the document with its status over its key, with no Test Studio badge', () => {
    renderScreen();
    const header = createWrapper(document.body).findHeader()!;
    expect(header.findHeadingText().getElement().textContent).toBe('a.pdf (To review)');
    expect(header.findDescription()!.getElement().textContent).toBe('a.pdf');
    expect(document.body.textContent).not.toContain('Test Studio');
    // No version-transition badge, and the editor hides its own title and provenance badges.
    expect(document.body.textContent).not.toMatch(/v1\s*→\s*v2/);
    // No provenance badges: a machine draft's label source and the class source stay off the editor.
    const extras = (editorProps!.headerExtras as (c: unknown) => React.ReactNode)({
      data: { labelSource: 'draft-machine' },
      setData: vi.fn(),
      selectedSectionId: '1',
      hasChanges: false,
    });
    const { container: badges } = render(<>{extras}</>);
    expect(badges.textContent).not.toContain('Classes from:');
    expect(badges.textContent).not.toContain('Draft (machine)');
  });

  it('shows no notice when there is nothing to act on, and no assignment note', () => {
    renderScreen();
    expect(createWrapper(document.body).findAllAlerts()).toHaveLength(0);
    expect(document.body.textContent).not.toContain('You can still correct and complete it');
  });

  it('tells the breadcrumbs the set name, on the trail the Annotator came by', () => {
    renderScreen('/?from=queues');
    expect(layoutTrail?.map((c) => c.text)).toEqual(['Home', 'Queues', 'Test set: Invoices', 'a.pdf']);
    expect(layoutTrail?.[2]).toEqual({ text: 'Test set: Invoices', href: '#/review-queues/test-sets/ts-1' });
  });

  it('disables Edit Page Grouping on a document assigned to someone else, naming them', () => {
    docOver.claimedByMe = false;
    docOver.assignedTo = 'other@example.com';
    renderScreen();
    expect(regroupItem()).toMatchObject({ disabled: true, disabledReason: 'Assigned to other@example.com' });
  });

  it('asks an Annotator to claim an unassigned document before regrouping it', () => {
    docOver.claimedByMe = false;
    docOver.assignedTo = null;
    renderScreen();
    expect(regroupItem()).toMatchObject({
      disabled: true,
      disabledReason: 'Claim this document first to change its page grouping',
    });
  });

  it('lets the Annotator holding the document regroup it', () => {
    docOver.claimedByMe = true;
    renderScreen();
    expect(regroupItem()).toMatchObject({ disabled: false });
  });

  it('offers Edit Page Grouping on a single-section document too: splitting one section is a fix', () => {
    docOver.sections = [{ sectionId: '1', baselineKey: 'b1', documentClass: 'Invoice', pageIndices: [0, 1] }];
    renderScreen();
    expect(regroupItem()).toMatchObject({ disabled: false });
    chooseRegroup();
    expect(boardProps).toMatchObject({ presentation: 'modal' });
  });
});
