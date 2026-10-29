// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The edit screen on a production item, by role. What has to hold: an Admin on an
 * unassigned item can edit it and use Reprocess, and the first draft save or Reprocess
 * claims it once (concurrent saves share the one claim) before the save or complete
 * is sent; if an Annotator got there first the screen says so and goes view-only; an
 * Admin on someone else's item sees it view-only with Take over, which calls
 * takeOverReviewItem; there is no Reject; Skip is offered only to the assignee; and
 * the screen gives the breadcrumbs the item's queue. The bar adds Save
 * and Discard Changes (through the editor's controller), Processing Report, and an Edit
 * page grouping that stays disabled while the item is in review; History has the item's
 * review activity; and there is no "Queue · profile · priority" meta line.
 */

import React, { useCallback, useState } from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { ReviewItem, ReviewItemDetail } from '../../../graphql/generated/operation-types';

// ---------------------------------------------------------------- the server
const server: { detail: ReviewItemDetail | null } = { detail: null };
const saveSection = vi.fn();
const complete = vi.fn();
const claimItem = vi.fn();
const releaseItem = vi.fn();
const takeOverItem = vi.fn();
const setErrorMessage = vi.fn();
const setSuccessMessage = vi.fn();
const exit = vi.fn();
const refreshDocument = vi.fn();
const saveDocumentGrouping = vi.fn();
const role = { isAdmin: true, isAnnotator: false, canWrite: true };
/** The props the editor was last rendered with. */
let editor: {
  isReadOnly: boolean;
  onSave: (id: string, data: Record<string, unknown>) => Promise<unknown>;
  controllerRef?: { current: unknown };
  body?: unknown;
  defaultFilter?: string;
} | null = null;
const controller = { save: vi.fn(), discard: vi.fn(), isDirty: vi.fn() };

vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
vi.mock('../production-item-edit-adapter', () => ({
  AUTOSAVE_MS: 2000,
  useProductionItemEdit: () => {
    const [detail, setDetail] = useState<ReviewItemDetail | null>(server.detail);
    const reload = useCallback(async () => {
      setDetail(server.detail);
      return server.detail;
    }, []);
    return {
      detail,
      loadError: null,
      reload,
      pages: [],
      pagesLoading: false,
      sections: [{ sectionId: '1', documentClass: 'W2', resultUri: 's3://b/r', draftUri: null, pageIds: [] }],
      // The whole document: the section under review and one that isn't.
      documentSections: [
        { sectionId: '1', documentClass: 'W2', resultUri: 's3://b/r', pageIds: ['1'] },
        { sectionId: '2', documentClass: 'W2', resultUri: 's3://b/r2', pageIds: ['2'] },
      ],
      refreshDocument: (...a: unknown[]) => refreshDocument(...a),
      summaryReportUri: 's3://out/in/w2.pdf/summary/summary.md',
      evaluationStatus: null,
      reloadToken: 0,
      loadSection: vi.fn(),
      saveSection: (...a: unknown[]) => saveSection(...a),
      complete: (...a: unknown[]) => complete(...a),
    };
  },
}));
/** The breadcrumb root the page gives its layout. */
let layoutRoot: unknown = null;
/** Every breadcrumb root the page gave, in order, and the navigation entry it highlights. */
const layoutRoots: unknown[] = [];
let layoutActiveHref: unknown;
vi.mock('../../review-queues/ReviewQueuesLayout', () => ({
  default: ({ children, root, navActiveHref }: { children: React.ReactNode; root?: unknown; navActiveHref?: unknown }) => {
    layoutRoot = root;
    layoutRoots.push(root);
    layoutActiveHref = navActiveHref;
    return <>{children}</>;
  },
}));
vi.mock('../SectionEditor', () => ({
  default: (props: NonNullable<typeof editor>) => {
    editor = props;
    if (props.controllerRef) props.controllerRef.current = controller;
    return <div data-testid="editor" data-readonly={String(props.isReadOnly)} />;
  },
}));
vi.mock('../../review-queues/review-api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../review-queues/review-api')>()),
  claimItem: (...a: unknown[]) => claimItem(...a),
  releaseItem: (...a: unknown[]) => releaseItem(...a),
  takeOverItem: (...a: unknown[]) => takeOverItem(...a),
}));
vi.mock('../../review-queues/ReviewTaskContext', () => ({ default: () => null }));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => role }));
vi.mock('../../../hooks/use-review-work', () => ({ useOptionalReviewWork: () => null }));
vi.mock('../../../contexts/app', () => ({ default: () => ({ setErrorMessage, setSuccessMessage }) }));
vi.mock('../use-edit-exit', () => ({ default: () => exit, useLeavePath: () => (queuePath: string) => queuePath }));
vi.mock('../../../hooks/use-page-thumbnails', () => ({ default: () => ({ urls: {}, failure: null }) }));
// The model saves through saveDocumentGroupingOrThrow, which reads saveDocumentGrouping's
// result the same way: a refusal throws with the server's message, a transport failure
// with the model's own wording.
vi.mock('../../common/document-grouping', () => ({
  saveDocumentGrouping: (...a: unknown[]) => saveDocumentGrouping(...a),
  saveDocumentGroupingOrThrow: async (objectKey: string, sections: unknown, describeError: (err: unknown) => string) => {
    let result: { success: boolean; message: string | null };
    try {
      result = await saveDocumentGrouping(objectKey, sections);
    } catch (err) {
      throw new Error(describeError(err));
    }
    if (!result.success) throw new Error(result.message ?? 'The page grouping could not be saved.');
    return result.message;
  },
}));
/** The grouping board's props, while it is open. */
let board: { sections: { sectionId: string }[]; onSave: (next: unknown[]) => Promise<void> } | null = null;
vi.mock('../../common/PageGroupingEditor', () => ({
  default: (props: NonNullable<typeof board>) => {
    board = props;
    return <div data-testid="grouping-board" />;
  },
}));

import ProductionItemEdit from '../ProductionItemEdit';
import { QUEUE_REGROUP_ROLE_REASON } from '../production-item-edit-model';
import { barButtonLabels, chooseMenuItem, menuItems, menuNames } from './edit-bar';

const ADMIN = 'admin@example.com';
const ANN = 'ann@example.com';

const detailFor = (over: Partial<ReviewItem>): ReviewItemDetail =>
  ({
    item: {
      objectKey: 'in/w2.pdf',
      cycleId: 'c1',
      profile: 'default',
      queue: 'default',
      priority: 50,
      state: 'OPEN',
      ownerEmail: null,
      isMine: false,
      claimedAt: null,
      ...over,
    },
    queueName: 'Default',
    sections: [],
    checklist: [],
    rejectReasons: ['Illegible'],
    claimCap: 10,
    myClaimCount: 0,
    history: [],
    canClaim: true,
  }) as unknown as ReviewItemDetail;

const mine = () => detailFor({ state: 'CLAIMED', ownerEmail: ADMIN, isMine: true });
const annotators = () => detailFor({ state: 'CLAIMED', ownerEmail: ANN, isMine: false });

const renderScreen = (path = '/documents/in%2Fw2.pdf/edit') =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <ProductionItemEdit objectKey="in/w2.pdf" />
    </MemoryRouter>,
  );

/** A button's label, without a disabled button's reason (rendered for screen readers). */
const label = (b: ReturnType<ReturnType<typeof createWrapper>['findAllButtons']>[number]) =>
  b.findTextRegion()?.getElement().textContent ?? b.getElement().textContent;
const buttons = () => createWrapper(document.body).findAllButtons().map(label);
const button = (text: string) => {
  const b = createWrapper(document.body)
    .findAllButtons()
    .find((w) => label(w) === text);
  if (!b) throw new Error(`no ${text} button`);
  return b;
};
const readOnly = () => screen.getByTestId('editor').getAttribute('data-readonly') === 'true';

beforeEach(() => {
  document.body.innerHTML = '';
  editor = null;
  role.isAdmin = true;
  role.isAnnotator = false;
  role.canWrite = true;
  for (const f of [saveSection, complete, claimItem, releaseItem, takeOverItem, setErrorMessage, setSuccessMessage, exit]) f.mockReset();
  controller.save.mockReset().mockResolvedValue(true);
  refreshDocument.mockReset().mockResolvedValue(undefined);
  saveDocumentGrouping.mockReset().mockResolvedValue({ success: true, message: 'Page grouping saved' });
  board = null;
  controller.discard.mockReset();
  controller.isDirty.mockReset().mockReturnValue(false);
  saveSection.mockResolvedValue(undefined);
  complete.mockResolvedValue(undefined);
  // A claim succeeds: the next read shows the item assigned to the Admin.
  claimItem.mockImplementation(async () => {
    server.detail = mine();
    return server.detail.item;
  });
  takeOverItem.mockImplementation(async () => {
    server.detail = mine();
    return server.detail.item;
  });
  server.detail = detailFor({});
});

describe('an Admin on an unassigned item', () => {
  it('can edit it, and says editing or reprocessing assigns it to them', () => {
    renderScreen();
    expect(readOnly()).toBe(false);
    expect(document.body.textContent).toContain('Not assigned yet. Editing it, or choosing Process Changes, assigns it to you.');
    expect(button('Process Changes').isDisabled()).toBe(false);
  });

  it('claims the item, then reprocesses it', async () => {
    renderScreen();
    fireEvent.click(button('Process Changes').getElement());
    await waitFor(() => expect(complete).toHaveBeenCalled());
    expect(claimItem).toHaveBeenCalledTimes(1);
    expect(claimItem.mock.calls[0][0]).toMatchObject({ objectKey: 'in/w2.pdf', cycleId: 'c1' });
    expect(claimItem.mock.invocationCallOrder[0]).toBeLessThan(complete.mock.invocationCallOrder[0]);
    await waitFor(() => expect(exit).toHaveBeenCalled());
    expect(setErrorMessage).not.toHaveBeenCalled();
  });

  it('claims once on the first edit, however many saves are waiting', async () => {
    let release: () => void = () => undefined;
    claimItem.mockImplementation(
      () =>
        new Promise((resolve) => {
          release = () => {
            server.detail = mine();
            resolve(server.detail.item);
          };
        }),
    );
    renderScreen();
    const first = editor!.onSave('1', { a: 1 });
    const second = editor!.onSave('1', { a: 2 });
    expect(saveSection).not.toHaveBeenCalled();
    await act(async () => {
      release();
      await Promise.all([first, second]);
    });
    expect(claimItem).toHaveBeenCalledTimes(1);
    expect(saveSection).toHaveBeenCalledTimes(2);
    // A later save finds the item already the Admin's and doesn't claim again.
    await act(async () => {
      await editor!.onSave('1', { a: 3 });
    });
    expect(claimItem).toHaveBeenCalledTimes(1);
    expect(saveSection).toHaveBeenCalledTimes(3);
  });

  it('goes view-only, saying who was assigned, when an Annotator got there first', async () => {
    claimItem.mockImplementation(async () => {
      server.detail = annotators();
      throw { errors: [{ message: `ALREADY_CLAIMED: claimed by ${ANN}` }] };
    });
    renderScreen();
    let error: unknown = null;
    await act(async () => {
      await editor!.onSave('1', { a: 1 }).catch((e: unknown) => {
        error = e;
      });
    });
    expect(error).toBeInstanceOf(Error);
    expect(saveSection).not.toHaveBeenCalled();
    await waitFor(() => expect(document.body.textContent).toContain(`Assigned to ${ANN} just now.`));
    expect(readOnly()).toBe(true);
    expect(buttons()).toContain('Take over');
    expect(setErrorMessage).not.toHaveBeenCalled();
  });
});

describe('an Admin on an item assigned to someone else', () => {
  beforeEach(() => {
    server.detail = annotators();
  });

  it('is view-only with Take over, and no Skip', () => {
    renderScreen();
    expect(readOnly()).toBe(true);
    expect(document.body.textContent).toContain(`Assigned to ${ANN}. Take over to edit it and process its changes.`);
    expect(button('Process Changes').isDisabled()).toBe(true);
    expect(buttons()).not.toContain('Skip');
  });

  it('takes the item over, after which it can be edited', async () => {
    renderScreen();
    fireEvent.click(button('Take over').getElement());
    await waitFor(() => expect(takeOverItem).toHaveBeenCalledTimes(1));
    expect(takeOverItem.mock.calls[0][0]).toMatchObject({ objectKey: 'in/w2.pdf', cycleId: 'c1' });
    await waitFor(() => expect(readOnly()).toBe(false));
    expect(buttons()).not.toContain('Take over');
    expect(claimItem).not.toHaveBeenCalled();
  });
});

describe('the actions', () => {
  it('offer no Reject', () => {
    for (const d of [detailFor({}), mine(), annotators()]) {
      document.body.innerHTML = '';
      server.detail = d;
      renderScreen();
      expect(buttons()).not.toContain('Reject');
    }
  });

  it('offer Skip only on an item assigned to me, describing what it does', () => {
    server.detail = mine();
    renderScreen();
    expect(buttons()).toContain('Skip');
    expect(button('Skip').getElement().closest('span[title]')?.getAttribute('title')).toBe(
      'Give this item back to its queue, unassigned and not finished.',
    );
    expect(buttons()).not.toContain('Take over');
    document.body.innerHTML = '';
    server.detail = detailFor({});
    renderScreen();
    expect(buttons()).not.toContain('Skip');
  });

  it('open History, with the review activity, which is not on the screen otherwise', () => {
    server.detail = {
      ...mine(),
      history: [{ at: '2026-10-20T09:00:00.000Z', event: 'claimed', actor: ADMIN, details: null }],
    } as ReviewItemDetail;
    renderScreen();
    expect(menuItems('Reports').map((i) => i.text)).toContain('History');
    expect(createWrapper(document.body).findModal()).toBeNull();
    chooseMenuItem('Reports', 'History');
    const modal = createWrapper(document.body).findModal()!;
    expect(modal.findHeader().getElement().textContent).toBe('History');
    const tabs = createWrapper(document.body).findTabs()!;
    expect(tabs.findTabLinks().map((t) => t.getElement().textContent)).toEqual(['Field Edits', 'Review Activity']);
    tabs.findTabLinkByIndex(2)!.click();
    expect(modal.findContent().getElement().textContent).toContain('Claimed');
  });

  it("offer Edit | View first in the bar, showing the source in the editor's place", async () => {
    server.detail = mine();
    renderScreen();
    const control = createWrapper(document.body).findHeader()!.findActions()!.findSegmentedControl()!;
    expect(control.findSegments().map((s) => s.getElement().textContent)).toEqual(['Edit', 'View']);
    expect(editor!.body).toBeUndefined();
    await act(async () => control.findSegments()[1].click());
    expect(editor!.body).toBeTruthy();
  });

  it('open the editor on the confidence alerts when the item has review reasons', () => {
    server.detail = { ...mine(), item: { ...mine().item, reasonCount: 2 } } as ReviewItemDetail;
    renderScreen();
    expect(editor!.defaultFilter).toBe('confidence-alerts');
  });

  it('disable Summary and the baseline while the item is being worked, saying why', () => {
    server.detail = mine();
    renderScreen();
    const reason = 'Available once the review is complete';
    expect(menuItems('Reports').find((i) => i.text === 'Summary')).toMatchObject({ disabled: true, disabledReason: reason });
    expect(menuItems('More Actions').find((i) => i.text === 'Use As Evaluation Baseline')).toMatchObject({
      disabled: true,
      disabledReason: reason,
    });
  });

  it('enable Summary and the baseline once the review is complete', () => {
    server.detail = detailFor({ state: 'COMPLETE' });
    renderScreen();
    expect(menuItems('Reports').find((i) => i.text === 'Summary')).toMatchObject({ disabled: false });
    expect(menuItems('More Actions').find((i) => i.text === 'Use As Evaluation Baseline')).toMatchObject({ disabled: false });
  });

  it('show no queue, profile and priority line', () => {
    server.detail = mine();
    renderScreen();
    expect(document.body.textContent).not.toMatch(/priority/i);
  });

  it('title the document with its state, with no assignment line', () => {
    server.detail = { ...mine(), item: { ...mine().item, claimedAt: '2026-10-20T09:00:00.000Z' } } as ReviewItemDetail;
    renderScreen();
    const header = createWrapper(document.body).findHeader()!;
    expect(header.findHeadingText().getElement().textContent).toBe('w2.pdf (Claimed)');
    expect(header.findDescription()?.getElement().textContent).toBe('in/w2.pdf');
    expect(document.body.textContent).not.toMatch(/Assigned to you/);
  });

  it('let the holder regroup the whole document during review, then re-read it', async () => {
    server.detail = mine();
    renderScreen();
    chooseMenuItem('More Actions', 'Edit Page Class');
    await waitFor(() => expect(board).not.toBeNull());
    // Every section of the document, not only the one under review.
    expect(board!.sections.map((s) => s.sectionId)).toEqual(['1', '2']);
    const next = [{ sectionId: '1', documentClass: 'W2', pageIds: [1, 2] }];
    await act(async () => board!.onSave(next));
    expect(saveDocumentGrouping).toHaveBeenCalledWith('in/w2.pdf', next);
    expect(refreshDocument).toHaveBeenCalled();
    expect(screen.queryByTestId('grouping-board')).toBeNull();
    expect(claimItem).not.toHaveBeenCalled();
  });

  it('claim an unassigned item for an Admin before regrouping it', async () => {
    renderScreen();
    chooseMenuItem('More Actions', 'Edit Page Class');
    await waitFor(() => expect(board).not.toBeNull());
    expect(claimItem).toHaveBeenCalledTimes(1);
  });

  it('keep the board open with the server’s message when it refuses', async () => {
    server.detail = mine();
    saveDocumentGrouping.mockResolvedValue({ success: false, message: 'NOT_LEASE_HOLDER: this item is assigned to someone else' });
    renderScreen();
    chooseMenuItem('More Actions', 'Edit Page Class');
    await waitFor(() => expect(board).not.toBeNull());
    await act(async () => board!.onSave([]));
    expect(screen.getByTestId('grouping-board')).toBeTruthy();
    expect(refreshDocument).not.toHaveBeenCalled();
  });

  it('disable Edit Page Class for an Annotator holding the item, saying why', () => {
    Object.assign(role, { isAdmin: false, isAnnotator: true, canWrite: false });
    server.detail = detailFor({ state: 'CLAIMED', ownerEmail: ANN, isMine: true });
    renderScreen();
    expect(menuItems('More Actions').find((i) => i.text === 'Edit Page Class')).toMatchObject({
      disabled: true,
      disabledReason: QUEUE_REGROUP_ROLE_REASON,
    });
  });

  it('disable Edit Page Grouping on an item assigned to someone else, saying why', () => {
    server.detail = annotators();
    renderScreen();
    expect(menuItems('More Actions').find((i) => i.text === 'Edit Page Class')).toMatchObject({
      disabled: true,
      disabledReason: `Assigned to ${ANN}`,
    });
  });

  it('save a draft through the editor, and Cancel discards unsaved edits after asking', async () => {
    server.detail = mine();
    renderScreen();
    // Disabled until the editor reports unsaved edits.
    expect(button('Save').isDisabled()).toBe(true);
    await act(async () => {
      (editor as unknown as { onDirtyChange: (d: boolean) => void }).onDirtyChange(true);
    });
    button('Save').click();
    await waitFor(() => expect(controller.save).toHaveBeenCalledTimes(1));
    controller.isDirty.mockReturnValue(true);
    button('Cancel').click();
    const dialog = await waitFor(() => createWrapper(document.body).findModal()!);
    expect(dialog.findHeader().getElement().textContent).toBe('Discard your unsaved changes?');
    dialog
      .findFooter()!
      .findAllButtons()
      .find((b) => b.getElement().textContent === 'Discard and leave')!
      .click();
    expect(controller.discard).toHaveBeenCalledTimes(1);
  });

  it('save unsaved edits before reprocessing on Reprocess', async () => {
    server.detail = mine();
    controller.isDirty.mockReturnValue(true);
    renderScreen();
    fireEvent.click(button('Process Changes').getElement());
    await waitFor(() => expect(complete).toHaveBeenCalled());
    expect(controller.save.mock.invocationCallOrder[0]).toBeLessThan(complete.mock.invocationCallOrder[0]);
  });

  it('offer History and Refresh beside Claim to an Annotator viewing an unassigned item', () => {
    role.isAdmin = false;
    role.isAnnotator = true;
    renderScreen();
    expect(menuNames()).toEqual(['Reports']);
    expect(barButtonLabels()).toEqual(['Refresh', 'Claim']);
    expect(menuItems('Reports').map((i) => i.text)).toContain('History');
  });

  it('ask why before skipping, and skip with that reason', async () => {
    server.detail = { ...mine(), skipReasons: ['Out of time', 'Needs expert review'] } as ReviewItemDetail;
    releaseItem.mockResolvedValue({});
    renderScreen();
    button('Skip').click();
    // Nothing is released until a reason is chosen.
    expect(releaseItem).not.toHaveBeenCalled();
    const modal = createWrapper(document.body).findModal()!;
    const select = modal.findContent().findSelect()!;
    select.openDropdown();
    select.selectOptionByValue('Needs expert review');
    modal.findContent().findTextarea()!.setTextareaValue('tax schedule');
    modal
      .findFooter()!
      .findAllButtons()
      .find((b) => b.getElement().textContent === 'Skip')!
      .click();
    await waitFor(() => expect(releaseItem).toHaveBeenCalledTimes(1));
    expect(releaseItem.mock.calls[0].slice(1)).toEqual(['Needs expert review', 'tax schedule']);
    await waitFor(() => expect(exit).toHaveBeenCalled());
  });

  it('leave Take over to Admins: an Annotator viewing someone else’s item has neither', () => {
    role.isAdmin = false;
    role.isAnnotator = true;
    server.detail = annotators();
    renderScreen();
    expect(readOnly()).toBe(true);
    expect(buttons()).not.toContain('Take over');
    expect(document.body.textContent).toContain(`View only. This item is assigned to ${ANN}.`);
  });

  it('let an Annotator claim an unassigned item instead of editing it', () => {
    role.isAdmin = false;
    role.isAnnotator = true;
    renderScreen();
    expect(readOnly()).toBe(true);
    expect(buttons()).toContain('Claim');
    expect(document.body.textContent).not.toContain('Editing or completing assigns it to you');
  });
});

describe('the breadcrumbs', () => {
  it('are told the item’s queue id and name', () => {
    renderScreen();
    expect(layoutRoot).toEqual({ text: 'Default', href: '#/review-queues/queues/default' });
  });
});

describe('the edit page’s URL and navigation', () => {
  it('opens the ?section= section and copies field links to the edit page', () => {
    server.detail = mine();
    renderScreen('/documents/in%2Fw2.pdf/edit?section=2&field=Box1');
    const props = editor as unknown as { initialSectionId?: string; focusFieldPath?: string; buildFieldLink: (f: string) => string };
    expect(props.initialSectionId).toBe('2');
    expect(props.focusFieldPath).toBe('Box1');
    expect(props.buildFieldLink('Box 1')).toBe(`${window.location.origin}/#/documents/in%2Fw2.pdf/edit?field=Box%201`);
  });

  it('names the queue in the breadcrumbs from the first render, never another root first', () => {
    server.detail = mine();
    layoutRoots.length = 0;
    renderScreen();
    expect(layoutRoots[0]).toEqual({ text: 'Default', href: '#/review-queues/queues/default' });
    expect(new Set(layoutRoots.map((r) => JSON.stringify(r))).size).toBe(1);
  });

  it('highlights Queue Management for an Admin and Tasks for an Annotator', () => {
    server.detail = mine();
    renderScreen();
    expect(layoutActiveHref).toBe('#/review-queues');
    document.body.innerHTML = '';
    Object.assign(role, { isAdmin: false, isAnnotator: true, canWrite: false });
    server.detail = annotators();
    renderScreen();
    expect(layoutActiveHref).toBe('#/my-work/tasks');
  });

  it.each([
    ['HOLDING', 'This item is being prepared for review. It can be edited once it reaches its queue.'],
    [
      'FINALIZE_FAILED',
      "Applying this review's corrections failed, so it can no longer be edited here. An Admin can see it in Queue Management.",
    ],
  ])('says what happens next on a %s item', (state, notice) => {
    server.detail = detailFor({ state });
    renderScreen();
    expect(document.body.textContent).toContain(notice);
    expect(document.body.textContent).not.toContain('This review is finished');
  });
});
