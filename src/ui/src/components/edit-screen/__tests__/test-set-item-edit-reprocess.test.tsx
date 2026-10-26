// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The primary action on a Test Studio document. What has to hold: the bar is History,
 * Edit Page Grouping, Skip, Cancel, Save and the primary action, which is Reprocess for
 * a document with a review copy and Complete for authored ground truth; there is no
 * Save as Draft (no test-set save leaves labels unreviewed); the primary action saves
 * the open section's edits first, then confirms only the sections not saved, and exits
 * once; the Edit | View Source Document switch sits in the Edit Document widget's
 * header; and the screen tells the breadcrumbs the set's name.
 */

import React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { act, render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const confirmSections = vi.fn();
const exit = vi.fn();
const controller = { save: vi.fn(), discard: vi.fn(), isDirty: vi.fn() };
let editorProps: Record<string, unknown> | null = null;
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
    saveSection: vi.fn(),
    confirmSections: (...a: unknown[]) => confirmSections(...a),
    skip: vi.fn(),
  }),
}));
vi.mock('../../test-studio/GroundTruthSectionEditor', () => ({
  default: (props: Record<string, unknown>) => {
    editorProps = props;
    (props.controllerRef as { current: unknown }).current = controller;
    return <div data-testid="editor" />;
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
vi.mock('../../../contexts/app', () => ({ default: () => ({ setErrorMessage: vi.fn(), setSuccessMessage: vi.fn() }) }));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: { TestSetBucket: 'test-set-bucket' } }) }));
vi.mock('../../../hooks/use-review-work', () => ({ useOptionalReviewWork: () => null }));
vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({ isAdmin: false, isAuthor: false, isReviewer: false, isAnnotator: true, canAnnotate: true, loading: false }),
}));
vi.mock('../../document-viewer/FileViewer', () => ({ default: () => <div data-testid="source-viewer" /> }));

import TestSetItemEdit from '../TestSetItemEdit';

const onTestSet = vi.fn();

const renderScreen = () =>
  render(
    <MemoryRouter>
      <TestSetItemEdit testSetId="ts-1" objectKey="a.pdf" onTestSet={onTestSet} />
    </MemoryRouter>,
  );

const barButtons = () =>
  createWrapper(document.body)
    .findHeader()!
    .findActions()!
    .findAllButtons()
    .map((b) => b.findTextRegion()?.getElement().textContent ?? b.getElement().textContent);
const button = (text: string) =>
  createWrapper(document.body)
    .findHeader()!
    .findActions()!
    .findAllButtons()
    .find((b) => (b.findTextRegion()?.getElement().textContent ?? b.getElement().textContent) === text)!;

beforeEach(() => {
  document.body.innerHTML = '';
  editorProps = null;
  for (const k of Object.keys(docOver)) delete docOver[k];
  confirmSections.mockReset().mockResolvedValue(undefined);
  exit.mockReset();
  onTestSet.mockReset();
  controller.isDirty.mockReset().mockReturnValue(true);
  // Saving the open section reports it saved, as the editor does.
  controller.save.mockReset().mockImplementation(async () => {
    (editorProps!.onSaved as (key: string) => void)('b1');
    return true;
  });
});

describe('the primary action on a Test Studio document', () => {
  it('offers Save, Cancel and Reprocess for a document with a review copy', () => {
    renderScreen();
    expect(barButtons()).toEqual(['History', 'Edit Page Grouping', 'Skip', 'Cancel', 'Save', 'Reprocess']);
  });

  it('offers Complete for authored ground truth, which nothing reprocesses', () => {
    docOver.reviewObjectKey = null;
    renderScreen();
    expect(barButtons()).toEqual(['History', 'Edit Page Grouping', 'Skip', 'Cancel', 'Save', 'Complete']);
  });

  it('saves the open section, then confirms the rest, and exits once', async () => {
    renderScreen();
    button('Reprocess').click();
    await waitFor(() => expect(exit).toHaveBeenCalledTimes(1));
    expect(controller.save).toHaveBeenCalledTimes(1);
    expect(confirmSections).toHaveBeenCalledWith(['2']);
    expect(controller.save.mock.invocationCallOrder[0]).toBeLessThan(confirmSections.mock.invocationCallOrder[0]);
  });

  it('saves the open section from Save, without completing', async () => {
    renderScreen();
    await act(async () => (editorProps!.onDirtyChange as (d: boolean) => void)(true));
    button('Save').click();
    await waitFor(() => expect(controller.save).toHaveBeenCalledTimes(1));
    expect(confirmSections).not.toHaveBeenCalled();
  });

  it('stops when the save fails', async () => {
    controller.save.mockResolvedValue(false);
    renderScreen();
    button('Reprocess').click();
    await waitFor(() => expect(controller.save).toHaveBeenCalled());
    expect(confirmSections).not.toHaveBeenCalled();
    expect(exit).not.toHaveBeenCalled();
  });

  it('places save, discard and page grouping in the top bar', () => {
    renderScreen();
    expect(editorProps).toMatchObject({ actionsPlacement: 'external', saveOnSectionSwitch: true, regroupPlacement: 'external' });
    button('Edit Page Grouping').click();
    expect(editorProps!.regroupOpen).toBe(true);
  });

  it('puts the Edit | View Source Document switch in the Edit Document widget, opening on Edit', async () => {
    renderScreen();
    expect(editorProps!.variant).toBe('widget');
    expect(editorProps!.body).toBeUndefined();
    // The editor is a stand-in here, so render the controls it is handed for its header.
    const { container } = render(<>{editorProps!.headerActions as React.ReactNode}</>);
    const control = createWrapper(container).findSegmentedControl()!;
    expect(control.findSegments().map((x) => x.getElement().textContent)).toEqual(['Edit', 'View Source Document']);
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
    expect(editorProps).toMatchObject({ variant: 'widget', hideBadges: true });
  });

  it('shows no notice when there is nothing to act on, and no assignment note', () => {
    renderScreen();
    expect(createWrapper(document.body).findAllAlerts()).toHaveLength(0);
    expect(document.body.textContent).not.toContain('You can still correct and complete it');
  });

  it('tells the breadcrumbs the set name', () => {
    renderScreen();
    expect(onTestSet).toHaveBeenCalledWith({ id: 'ts-1', name: 'Invoices' });
  });
});
