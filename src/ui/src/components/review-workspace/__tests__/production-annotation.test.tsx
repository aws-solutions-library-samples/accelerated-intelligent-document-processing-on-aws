// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The production annotation screen's actions by role. What has to hold: an Admin on
 * an unassigned item can edit and Complete it, and the first draft save or Complete
 * claims it once (concurrent saves share the one claim) before the save or complete
 * is sent; if an Annotator got there first the screen says so and goes view-only; an
 * Admin on someone else's item sees it view-only with Take over, which calls
 * takeOverReviewItem; there is no Reject; Skip is offered only to the assignee; and
 * the screen reports the item's queue for the breadcrumbs.
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
const role = { isAdmin: true, isAnnotator: false };
/** The props the editor was last rendered with. */
let editor: { isReadOnly: boolean; onSave: (id: string, data: Record<string, unknown>) => Promise<unknown> } | null = null;

vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
vi.mock('../production-adapter', () => ({
  AUTOSAVE_MS: 2000,
  useProductionAnnotation: () => {
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
      reloadToken: 0,
      loadSection: vi.fn(),
      saveSection: (...a: unknown[]) => saveSection(...a),
      complete: (...a: unknown[]) => complete(...a),
    };
  },
}));
vi.mock('../SectionEditor', () => ({
  default: (props: NonNullable<typeof editor>) => {
    editor = props;
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
vi.mock('../../review-queues/ReviewPresence', () => ({ default: () => null }));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => role }));
vi.mock('../../../hooks/use-review-work', () => ({ useOptionalReviewWork: () => null }));
vi.mock('../../../contexts/app', () => ({ default: () => ({ setErrorMessage, setSuccessMessage }) }));
vi.mock('../use-annotation-exit', () => ({ default: () => exit }));

import ProductionAnnotation from '../ProductionAnnotation';

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

const onItemQueue = vi.fn();
const renderScreen = () =>
  render(
    <MemoryRouter>
      <ProductionAnnotation objectKey="in/w2.pdf" onItemQueue={onItemQueue} />
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
  for (const f of [saveSection, complete, claimItem, releaseItem, takeOverItem, setErrorMessage, setSuccessMessage, exit, onItemQueue])
    f.mockReset();
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
  it('can edit it, and says editing or completing assigns it to them', () => {
    renderScreen();
    expect(readOnly()).toBe(false);
    expect(document.body.textContent).toContain('Not assigned yet. Editing or completing assigns it to you.');
    expect(button('Complete').isDisabled()).toBe(false);
  });

  it('claims the item, then completes it', async () => {
    renderScreen();
    fireEvent.click(button('Complete').getElement());
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
    expect(document.body.textContent).toContain(`Assigned to ${ANN}. Take over to edit or complete it.`);
    expect(button('Complete').isDisabled()).toBe(true);
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
    expect(onItemQueue).toHaveBeenCalledWith({ id: 'default', name: 'Default' });
  });
});
