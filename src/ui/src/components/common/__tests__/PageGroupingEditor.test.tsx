// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The page-regrouping board's behaviour, exercised through the non-drag path.
 *
 * Drag itself is not simulated: dnd-kit's pointer sensor needs real layout, which jsdom
 * does not provide, so a "drag" test here would assert against a mock rather than the
 * component. What IS tested is everything the drag ends up calling — `movePage` and the
 * validation and save gating around it — via the "Move to" menu, which shares that exact
 * code path.
 *
 * That the two paths converge is itself worth stating: the menu is not a lesser fallback
 * bolted on for tests, it is the keyboard and screen-reader route to the same operation,
 * and it is what makes this screen usable without a pointer.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { describe, expect, it, vi } from 'vitest';

import PageGroupingEditor from '../PageGroupingEditor';
import type { GroupedSection } from '../section-grouping';

const PAGES = [1, 2, 3, 4].map((id) => ({ id, imageUri: `blob:page-${id}` }));

const SECTIONS: GroupedSection[] = [
  { sectionId: '1', documentClass: 'FieldTicket', pageIds: [1, 2] },
  { sectionId: '2', documentClass: 'Invoice', pageIds: [3, 4] },
];

const renderEditor = (overrides: Partial<React.ComponentProps<typeof PageGroupingEditor>> = {}) => {
  const onSave = vi.fn();
  const onCancel = vi.fn();
  const result = render(
    <PageGroupingEditor
      pages={PAGES}
      sections={SECTIONS}
      classOptions={[
        { label: 'FieldTicket', value: 'FieldTicket' },
        { label: 'Invoice', value: 'Invoice' },
        { label: 'DeliveryNote', value: 'DeliveryNote' },
      ]}
      consequence="Saving keeps the field values."
      onSave={onSave}
      onCancel={onCancel}
      {...overrides}
    />,
  );
  return { onSave, onCancel, ...result };
};

/**
 * Move a page via the menu — the same code path a drop takes.
 *
 * Scoped to the open menu by role: plain text matching also hits the section *header*,
 * which reads "Section 1" too.
 */
const movePageTo = async (pageId: number, sectionId: string) => {
  await userEvent.click(screen.getByRole('button', { name: new RegExp(`^Move page ${pageId}\\b`) }));
  await userEvent.click(await screen.findByRole('menuitem', { name: new RegExp(`^Section ${sectionId}\\b`) }));
};

describe('PageGroupingEditor', () => {
  it('shows every page of the document, grouped by section', () => {
    renderEditor();

    expect(screen.getByText('Section 1')).toBeInTheDocument();
    expect(screen.getByText('Section 2')).toBeInTheDocument();
    for (const id of [1, 2, 3, 4]) {
      expect(screen.getByAltText(`Page ${id}`)).toBeInTheDocument();
    }
  });

  it('moves a page between sections and saves the new grouping', async () => {
    const { onSave } = renderEditor();

    await movePageTo(3, '1');
    await userEvent.click(screen.getByRole('button', { name: /Save grouping/i }));

    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave.mock.calls[0][0]).toEqual([
      { sectionId: '1', documentClass: 'FieldTicket', pageIds: [1, 2, 3] },
      { sectionId: '2', documentClass: 'Invoice', pageIds: [4] },
    ]);
  });

  it('keeps pages in document order, not the order they were moved', async () => {
    // Five pages so Section 2 still has one left afterwards — emptying it would block
    // the save for an unrelated reason and prove nothing about ordering.
    const { onSave } = renderEditor({
      pages: [1, 2, 3, 4, 5].map((id) => ({ id, imageUri: `blob:page-${id}` })),
      sections: [
        { sectionId: '1', documentClass: 'FieldTicket', pageIds: [1, 2] },
        { sectionId: '2', documentClass: 'Invoice', pageIds: [3, 4, 5] },
      ],
    });

    // Drop 5 before 4; the result must still read ascending.
    await movePageTo(5, '1');
    await movePageTo(4, '1');
    await userEvent.click(screen.getByRole('button', { name: /Save grouping/i }));

    expect(onSave.mock.calls[0][0][0].pageIds).toEqual([1, 2, 4, 5]);
  });

  it('never leaves a page in two sections', async () => {
    const { onSave } = renderEditor();

    await movePageTo(1, '2');
    await userEvent.click(screen.getByRole('button', { name: /Save grouping/i }));

    const saved = onSave.mock.calls[0][0] as GroupedSection[];
    const allPages = saved.flatMap((s) => s.pageIds);
    expect(new Set(allPages).size).toBe(allPages.length);
    expect(allPages.sort()).toEqual([1, 2, 3, 4]);
  });

  it('blocks saving while a section is empty, and says why', async () => {
    renderEditor();

    // Empty Section 2 by moving both its pages away.
    await movePageTo(3, '1');
    await movePageTo(4, '1');

    expect(screen.getByRole('button', { name: /Save grouping/i })).toBeDisabled();
    expect(screen.getByText(/This section has no pages/)).toBeInTheDocument();
  });

  it('only allows deleting a section once it is empty', async () => {
    renderEditor();

    // Section 2 still holds pages 3 and 4.
    expect(screen.getByRole('button', { name: /Delete section 2/i })).toBeDisabled();

    await movePageTo(3, '1');
    await movePageTo(4, '1');

    expect(screen.getByRole('button', { name: /Delete section 2/i })).toBeEnabled();
  });

  it('can empty a section, delete it, and then save', async () => {
    // The full merge journey: two sections become one.
    const { onSave } = renderEditor();

    await movePageTo(3, '1');
    await movePageTo(4, '1');
    await userEvent.click(screen.getByRole('button', { name: /Delete section 2/i }));
    await userEvent.click(screen.getByRole('button', { name: /Save grouping/i }));

    expect(onSave.mock.calls[0][0]).toEqual([{ sectionId: '1', documentClass: 'FieldTicket', pageIds: [1, 2, 3, 4] }]);
  });

  it('adds a section, which starts empty and therefore blocks saving until filled', async () => {
    const { onSave } = renderEditor();

    await userEvent.click(screen.getByRole('button', { name: /Add section/i }));
    expect(screen.getByText('Section 3')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Save grouping/i })).toBeDisabled();

    await movePageTo(4, '3');
    await userEvent.click(screen.getByRole('button', { name: /Save grouping/i }));

    const saved = onSave.mock.calls[0][0] as GroupedSection[];
    expect(saved.find((s) => s.sectionId === '3')?.pageIds).toEqual([4]);
  });

  it('does not offer Save until something has actually changed', () => {
    renderEditor();

    // Guards against a no-op write that would bump provenance for nothing.
    expect(screen.getByRole('button', { name: /Save grouping/i })).toBeDisabled();
  });

  it('states the consequence of saving, because it differs per surface', () => {
    renderEditor({ consequence: 'Saving reprocesses this document.' });

    expect(screen.getByText('Saving reprocesses this document.')).toBeInTheDocument();
  });

  it('offers a keyboard route for every page, not drag alone', () => {
    renderEditor();

    // The property that makes this screen usable without a pointer.
    for (const id of [1, 2, 3, 4]) {
      expect(screen.getByRole('button', { name: new RegExp(`^Move page ${id}\\b`) })).toBeInTheDocument();
    }
  });

  it('locks the class control when the caller cannot change classes', async () => {
    // Asserted through behaviour rather than Cloudscape's disabled markup, which is an
    // implementation detail that would make this test a liability on upgrade.
    renderEditor({ canChangeClass: false });

    await userEvent.click(screen.getByText('FieldTicket'));

    // The class is still readable, but no other class can be picked.
    expect(screen.getByText('FieldTicket')).toBeInTheDocument();
    expect(screen.queryByText('DeliveryNote')).not.toBeInTheDocument();
  });

  it('leaves the class control usable when the caller can change classes', async () => {
    renderEditor({ canChangeClass: true });

    await userEvent.click(screen.getByText('FieldTicket'));

    expect(await screen.findByText('DeliveryNote')).toBeInTheDocument();
  });

  it('hides the class control entirely when the config defines no classes', () => {
    // Distinct from "cannot change": there is nothing to choose from.
    renderEditor({ classOptions: [] });

    expect(screen.queryByPlaceholderText('Choose a document class')).not.toBeInTheDocument();
  });
});

/**
 * Selecting a run of pages and moving it in one action.
 *
 * A wrong packet split is normally a contiguous run — the classifier put pages 5-9 in the
 * wrong place — which is one mistake and should take one action, not five.
 */
describe('PageGroupingEditor multi-select', () => {
  const SIX_PAGES = [1, 2, 3, 4, 5, 6].map((id) => ({ id, imageUri: `blob:page-${id}` }));
  const TWO_SECTIONS: GroupedSection[] = [
    { sectionId: '1', documentClass: 'FieldTicket', pageIds: [1, 2] },
    { sectionId: '2', documentClass: 'Invoice', pageIds: [3, 4, 5, 6] },
  ];

  const renderSix = () => renderEditor({ pages: SIX_PAGES, sections: TWO_SECTIONS });

  it('moves every selected page when one of them is moved', async () => {
    const { onSave } = renderSix();

    await userEvent.click(screen.getByRole('checkbox', { name: /Select page 4/i }));
    await userEvent.click(screen.getByRole('checkbox', { name: /Select page 5/i }));
    // Moving page 4 must carry page 5 with it.
    await movePageTo(4, '1');
    await userEvent.click(screen.getByRole('button', { name: /Save grouping/i }));

    expect(onSave.mock.calls[0][0]).toEqual([
      { sectionId: '1', documentClass: 'FieldTicket', pageIds: [1, 2, 4, 5] },
      { sectionId: '2', documentClass: 'Invoice', pageIds: [3, 6] },
    ]);
  });

  it('shift-click selects the run between two pages, in document order', async () => {
    const { onSave } = renderSix();

    await userEvent.click(screen.getByRole('checkbox', { name: /Select page 3/i }));
    // Shift to page 6 should take 3,4,5,6 — the contiguous run, not just the endpoints.
    // fireEvent, not userEvent: userEvent.keyboard('{Shift>}') does not carry the
    // modifier into a subsequent click here — probed it, the handler saw shiftKey=false.
    // fireEvent dispatches the real shift-click a browser would.
    fireEvent.click(screen.getByRole('checkbox', { name: /Select page 6/i }), { shiftKey: true });

    expect(screen.getByText(/4 pages selected/)).toBeInTheDocument();

    // Moving the run empties Section 2, which correctly blocks the save until the now
    // empty section is deleted — so this is the whole merge, which is Spencer's case:
    // one wrong split becomes one correct section in three actions rather than five.
    await movePageTo(3, '1');
    expect(screen.getByRole('button', { name: /Save grouping/i })).toBeDisabled();

    await userEvent.click(screen.getByRole('button', { name: /Delete section 2/i }));
    await userEvent.click(screen.getByRole('button', { name: /Save grouping/i }));

    expect(onSave.mock.calls[0][0]).toEqual([{ sectionId: '1', documentClass: 'FieldTicket', pageIds: [1, 2, 3, 4, 5, 6] }]);
  });

  it('moves only the clicked page when it is not part of the selection', async () => {
    const { onSave } = renderSix();

    await userEvent.click(screen.getByRole('checkbox', { name: /Select page 5/i }));
    // Page 3 is not selected, so it travels alone and page 5 stays put.
    await movePageTo(3, '1');
    await userEvent.click(screen.getByRole('button', { name: /Save grouping/i }));

    expect(onSave.mock.calls[0][0]).toEqual([
      { sectionId: '1', documentClass: 'FieldTicket', pageIds: [1, 2, 3] },
      { sectionId: '2', documentClass: 'Invoice', pageIds: [4, 5, 6] },
    ]);
  });

  it('clears the selection after a move, so the next one cannot carry stragglers', async () => {
    renderSix();

    await userEvent.click(screen.getByRole('checkbox', { name: /Select page 4/i }));
    expect(screen.getByText(/1 page selected/)).toBeInTheDocument();

    await movePageTo(4, '1');

    // A selection outliving its move is a trap: the next drag would silently take pages
    // the reviewer had forgotten were selected.
    expect(screen.queryByText(/selected/)).not.toBeInTheDocument();
  });

  it('says the selection will travel together, and offers a way out', async () => {
    renderSix();

    await userEvent.click(screen.getByRole('checkbox', { name: /Select page 3/i }));
    await userEvent.click(screen.getByRole('checkbox', { name: /Select page 4/i }));

    expect(screen.getByText(/2 pages selected — moving any one moves all of them/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /Clear selection/i }));
    expect(screen.queryByText(/selected/)).not.toBeInTheDocument();
  });

  it('unticking a selected page removes just that one', async () => {
    renderSix();

    await userEvent.click(screen.getByRole('checkbox', { name: /Select page 3/i }));
    await userEvent.click(screen.getByRole('checkbox', { name: /Select page 4/i }));
    await userEvent.click(screen.getByRole('checkbox', { name: /Select page 3/i }));

    expect(screen.getByText(/1 page selected/)).toBeInTheDocument();
  });
});

/**
 * The expand affordance, for a packet too tall to work in-page.
 *
 * Renders the same body in a modal rather than a second layout, so the expanded view
 * cannot drift from the inline one.
 */
describe('PageGroupingEditor expand', () => {
  it('opens the same board in a modal, keeping the working state', async () => {
    renderEditor();

    await movePageTo(3, '1');
    await userEvent.click(screen.getByRole('button', { name: /^Expand$/i }));

    // The in-progress grouping survives the switch — it would be lost if expanding
    // remounted the board.
    expect(screen.getByRole('heading', { name: /Edit page grouping/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Save grouping/i })).toBeEnabled();
  });

  it('does not offer Expand again once expanded', async () => {
    renderEditor();

    await userEvent.click(screen.getByRole('button', { name: /^Expand$/i }));

    expect(screen.queryByRole('button', { name: /^Expand$/i })).not.toBeInTheDocument();
  });
});

/**
 * Two rendering-layer defects found by dragging on a real stack, not in jsdom.
 *
 * Both made the board "feel off" while every logic test above still passed, which is the
 * point of keeping them in their own block: the move was always computed correctly, it
 * was the *board* that misbehaved. jsdom can pin the ordering half directly. The overlay
 * half is a layout property jsdom has no opinion about — it computes no scroll boxes and
 * clips nothing — so what is pinned here is the invariant the fix rests on: the source
 * card must not carry a transform, because a transformed child cannot escape the
 * column's own `overflowY: auto`. The visible behaviour was verified in a browser.
 */
describe('PageGroupingEditor board stability', () => {
  const SIX_PAGES = [1, 2, 3, 4, 5, 6].map((id) => ({ id, imageUri: `blob:page-${id}` }));
  // Section 2 starts at page 3. Move page 1 into it and it starts at page 1, so a sort by
  // first page would pull it left of section 1 — the reshuffle this block exists for.
  const TWO_SECTIONS: GroupedSection[] = [
    { sectionId: '1', documentClass: 'FieldTicket', pageIds: [1, 2] },
    { sectionId: '2', documentClass: 'Invoice', pageIds: [3, 4, 5, 6] },
  ];

  const columnOrder = () =>
    screen
      .getAllByRole('heading', { level: 3 })
      .map((h) => h.textContent?.trim())
      .filter((t): t is string => Boolean(t));

  it('does not reshuffle the columns when a move changes which section starts first', async () => {
    renderEditor({ pages: SIX_PAGES, sections: TWO_SECTIONS });

    expect(columnOrder()).toEqual(['Section 1', 'Section 2']);

    await movePageTo(1, '2');

    // Section 2 now owns page 1, so document order says it belongs first. It must still
    // be rendered second: a column that swaps places under the cursor makes the next
    // drag land somewhere the reviewer did not aim.
    expect(columnOrder()).toEqual(['Section 1', 'Section 2']);
  });

  it('still saves sections in document order, however they are displayed', async () => {
    const { onSave } = renderEditor({ pages: SIX_PAGES, sections: TWO_SECTIONS });

    await movePageTo(1, '2');
    await userEvent.click(screen.getByRole('button', { name: /Save grouping/i }));

    // Holding the display steady must not leak into what is written: doc-split scoring
    // takes a group's index from its list position, so the saved order is load-bearing.
    expect((onSave.mock.calls[0][0] as GroupedSection[]).map((s) => s.sectionId)).toEqual(['2', '1']);
  });

  it('treats a move and its reverse as no change at all', async () => {
    renderEditor({ pages: SIX_PAGES, sections: TWO_SECTIONS });

    await movePageTo(1, '2');
    expect(screen.getByRole('button', { name: /Save grouping/i })).toBeEnabled();

    await movePageTo(1, '1');

    // Compared in canonical form, so returning to the original grouping disarms Save
    // rather than offering a write that would bump provenance for nothing.
    expect(screen.getByRole('button', { name: /Save grouping/i })).toBeDisabled();
  });

  it('lifts the dragged page into an overlay outside the scrolling columns', async () => {
    renderEditor({ pages: SIX_PAGES, sections: TWO_SECTIONS });

    // The keyboard sensor is the one drag jsdom can actually start, and starting a drag
    // is all this needs: the overlay either exists at that moment or it does not.
    // jsdom has no scrollIntoView, and dnd-kit's KeyboardSensor calls it precisely
    // because the page sits in a scrolling column — the same `overflow: auto` this fix is
    // about. Without the stub the sensor throws and no drag ever starts.
    const scrollIntoView = vi.fn();
    Object.defineProperty(Element.prototype, 'scrollIntoView', { value: scrollIntoView, writable: true, configurable: true });

    const handle = screen.getByLabelText('Page 1, drag to another section');
    handle.focus();
    // dnd-kit's KeyboardSensor matches on `event.code`, not `key`.
    fireEvent.keyDown(handle, { key: ' ', code: 'Space' });
    await waitFor(() => expect(document.body.querySelector('[data-page-grouping-overlay]')).toBeTruthy());

    // Portaled to the body, so it is outside the column's `overflowY: auto` and the
    // row's `overflowX: auto`. Rendered inside them, the page vanished at the column
    // edge on every cross-section drag.
    const overlay = document.body.querySelector('[data-page-grouping-overlay]');
    expect(overlay).toBeTruthy();
    expect(overlay!.closest('[data-page-grouping-columns]')).toBeNull();
    expect(overlay!.textContent).toContain('Page 1');
  });
});
