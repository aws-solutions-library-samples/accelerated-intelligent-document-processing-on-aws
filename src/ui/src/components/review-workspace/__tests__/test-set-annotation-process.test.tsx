// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Complete on a Test Studio document. What has to hold: there is no Save as
 * Draft (no test-set save leaves labels unreviewed); Complete saves the open
 * section's edits first, then confirms only the sections not saved, and exits once;
 * and the screen tells the breadcrumbs the set's name.
 */

import React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { render, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const confirmSections = vi.fn();
const exit = vi.fn();
const controller = { save: vi.fn(), discard: vi.fn(), isDirty: vi.fn() };
let editorProps: Record<string, unknown> | null = null;

vi.mock('../test-set-adapter', () => ({
  NOT_ASSIGNED_MESSAGE: 'not assigned',
  useTestSetAnnotation: () => ({
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
vi.mock('../../test-studio/GroundTruthVisualEditor', () => ({
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
vi.mock('../use-annotation-exit', () => ({ default: () => exit }));
vi.mock('../../../contexts/app', () => ({ default: () => ({ setErrorMessage: vi.fn(), setSuccessMessage: vi.fn() }) }));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: { TestSetBucket: 'test-set-bucket' } }) }));
vi.mock('../../../hooks/use-review-work', () => ({ useOptionalReviewWork: () => null }));
vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({ isAdmin: false, isAuthor: false, isReviewer: false, isAnnotator: true, canAnnotate: true, loading: false }),
}));
vi.mock('../../review-queues/ReviewPresence', () => ({ default: () => null }));

import TestSetAnnotation from '../TestSetAnnotation';

const onTestSet = vi.fn();

const renderScreen = () =>
  render(
    <MemoryRouter>
      <TestSetAnnotation testSetId="ts-1" objectKey="a.pdf" onTestSet={onTestSet} />
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

describe('Complete on a Test Studio document', () => {
  it('offers no Save as Draft', () => {
    renderScreen();
    expect(barButtons()).toEqual(['History', 'Skip', 'Discard Changes', 'Edit Page Grouping', 'Processing Report', 'Complete']);
  });

  it('saves the open section, then confirms the rest, and exits once', async () => {
    renderScreen();
    button('Complete').click();
    await waitFor(() => expect(exit).toHaveBeenCalledTimes(1));
    expect(controller.save).toHaveBeenCalledTimes(1);
    expect(confirmSections).toHaveBeenCalledWith(['2']);
    expect(controller.save.mock.invocationCallOrder[0]).toBeLessThan(confirmSections.mock.invocationCallOrder[0]);
  });

  it('stops when the save fails', async () => {
    controller.save.mockResolvedValue(false);
    renderScreen();
    button('Complete').click();
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

  it('puts the Edit | View switch in the top bar, opening on Edit', () => {
    renderScreen();
    const control = createWrapper(document.body).findHeader()!.findActions()!.findSegmentedControl()!;
    expect(control.findSegments().map((x) => x.getElement().textContent)).toEqual(['Edit', 'View']);
    expect(control.findSelectedSegment()!.getElement().textContent).toBe('Edit');
  });

  it('names the document and its ground truth under the title', () => {
    renderScreen();
    expect(createWrapper(document.body).findHeader()!.findDescription()!.getElement().textContent).toBe(
      'Document: ts-1/input/a.pdfGround Truth: a.pdf',
    );
    expect(editorProps).toMatchObject({ hideHeader: true });
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
