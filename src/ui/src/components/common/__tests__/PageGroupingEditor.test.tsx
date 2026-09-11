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

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
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

  it('renders that menu outside the scrolling column it sits in', () => {
    // Measured on the annotate surface, where the section column is a 320px
    // `overflow: auto` box: the menu ran to 628px against a container ending at 596px,
    // so its last item was clipped away, and it inherited the column's 118px width,
    // wrapping all three labels onto two lines each. The keyboard route above was
    // therefore reachable but unreadable, and "Move later" was not reachable at all.
    //
    // Asserted on the prop rather than on measured geometry: jsdom has no layout, so a
    // dimension-based test here would pass whatever the markup did. Cloudscape's
    // expandToViewport is what portals the menu out of the clipping ancestor — the same
    // remedy the drag preview needed when it moved to a body-level overlay.
    const source = readFileSync(join(__dirname, '..', 'PageGroupingEditor.tsx'), 'utf-8');
    const menu = source.slice(source.indexOf('<ButtonDropdown'), source.indexOf('<ButtonDropdown') + 900);
    expect(menu).toMatch(/expandToViewport/);
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
    // empty section is deleted — so this is the whole merge, which is the motivating case:
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

    const handle = screen.getByLabelText(/^Page 1, drag to reorder/);
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

/**
 * Reordering pages within a section.
 *
 * Position within a section is scored — `split_accuracy_with_order` compares `page_indices`
 * by list equality and half the graded packet score is Kendall's Tau over each page's
 * position — so this is annotation, not presentation. An earlier version of the board sorted
 * on save and lost it.
 *
 * The drop-onto-a-card gesture is not exercised here: dnd-kit resolves a drop from measured
 * rects and jsdom reports every rect as zero, so a drop test would assert against the
 * fixture rather than the component. The placement logic it calls is covered directly in
 * `section-grouping.test.ts`, and the gesture itself was verified in a browser. What is
 * covered here is the route that has to work without a pointer.
 */
describe('PageGroupingEditor page ordering', () => {
  const FOUR = [1, 2, 3, 4].map((id) => ({ id, imageUri: `blob:page-${id}` }));
  const ONE_SECTION: GroupedSection[] = [{ sectionId: '1', documentClass: 'Invoice', pageIds: [1, 2, 3, 4] }];

  const nudge = async (pageId: number, label: 'Move earlier' | 'Move later') => {
    await userEvent.click(screen.getByRole('button', { name: new RegExp(`^Move page ${pageId}\\b`) }));
    await userEvent.click(await screen.findByRole('menuitem', { name: label }));
  };

  const savedPages = (onSave: ReturnType<typeof vi.fn>) => (onSave.mock.calls[0][0] as GroupedSection[])[0].pageIds;

  it('moves a page later without a pointer, and saves the new order', async () => {
    const { onSave } = renderEditor({ pages: FOUR, sections: ONE_SECTION });

    await nudge(2, 'Move later');
    await userEvent.click(screen.getByRole('button', { name: /Save grouping/i }));

    expect(savedPages(onSave)).toEqual([1, 3, 2, 4]);
  });

  it('moves a page earlier without a pointer', async () => {
    const { onSave } = renderEditor({ pages: FOUR, sections: ONE_SECTION });

    await nudge(3, 'Move earlier');
    await userEvent.click(screen.getByRole('button', { name: /Save grouping/i }));

    expect(savedPages(onSave)).toEqual([1, 3, 2, 4]);
  });

  it('offers no move earlier on the first page, nor later on the last', async () => {
    // Disabled rather than absent, so the menu does not change shape per page, and
    // clamped rather than wrapped — see nudgePageIds.
    renderEditor({ pages: FOUR, sections: ONE_SECTION });

    await userEvent.click(screen.getByRole('button', { name: /^Move page 1\b/ }));
    expect(await screen.findByRole('menuitem', { name: 'Move earlier' })).toHaveAttribute('aria-disabled', 'true');
    expect(screen.getByRole('menuitem', { name: 'Move later' })).not.toHaveAttribute('aria-disabled', 'true');
  });

  it('treats a reorder as a change worth saving', async () => {
    renderEditor({ pages: FOUR, sections: ONE_SECTION });

    expect(screen.getByRole('button', { name: /Save grouping/i })).toBeDisabled();
    await nudge(1, 'Move later');

    // Order is part of the grouping, so a pure reorder has to arm Save.
    expect(screen.getByRole('button', { name: /Save grouping/i })).toBeEnabled();
  });

  it('carries the whole selection when reordering one of its members', async () => {
    const { onSave } = renderEditor({ pages: FOUR, sections: ONE_SECTION });

    await userEvent.click(screen.getByRole('checkbox', { name: /Select page 3/i }));
    await userEvent.click(screen.getByRole('checkbox', { name: /Select page 4/i }));
    await nudge(3, 'Move earlier');
    await userEvent.click(screen.getByRole('button', { name: /Save grouping/i }));

    // Consistent with dragging, which also carries the selection.
    expect(savedPages(onSave)).toEqual([1, 3, 4, 2]);
  });

  it('labels a section whose pages are out of document order', async () => {
    renderEditor({ pages: FOUR, sections: ONE_SECTION });

    expect(screen.queryByText('Custom page order')).not.toBeInTheDocument();
    await nudge(1, 'Move later');

    // Otherwise a deliberately reordered section reads as a rendering bug.
    expect(screen.getByText('Custom page order')).toBeInTheDocument();
  });

  it('offers a way back to document order, only once it is needed', async () => {
    const { onSave } = renderEditor({ pages: FOUR, sections: ONE_SECTION });

    expect(screen.queryByRole('button', { name: /Sort section 1 pages in document order/i })).not.toBeInTheDocument();
    await nudge(1, 'Move later');
    await userEvent.click(screen.getByRole('button', { name: /Sort section 1 pages in document order/i }));

    // Back to the original, so Save disarms again.
    expect(screen.queryByText('Custom page order')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Save grouping/i })).toBeDisabled();
    expect(onSave).not.toHaveBeenCalled();
  });

  it('sorts only the section asked for', async () => {
    const { onSave } = renderEditor({
      pages: FOUR,
      sections: [
        { sectionId: '1', documentClass: 'Invoice', pageIds: [2, 1] },
        { sectionId: '2', documentClass: 'FieldTicket', pageIds: [4, 3] },
      ],
    });

    await userEvent.click(screen.getByRole('button', { name: /Sort section 1 pages in document order/i }));
    await userEvent.click(screen.getByRole('button', { name: /Save grouping/i }));

    const saved = onSave.mock.calls[0][0] as GroupedSection[];
    expect(saved.find((s) => s.sectionId === '1')?.pageIds).toEqual([1, 2]);
    // Section 2's custom order is left exactly as it was.
    expect(saved.find((s) => s.sectionId === '2')?.pageIds).toEqual([4, 3]);
  });

  it('places a page moved from another section in document order, not at the end', async () => {
    // A move with no drop position is the ordinary case; it must not manufacture a custom
    // order as a side effect.
    const { onSave } = renderEditor({
      pages: FOUR,
      sections: [
        // Section 2 keeps a spare page: emptying it would block the save for an unrelated
        // reason and prove nothing about placement.
        { sectionId: '1', documentClass: 'Invoice', pageIds: [1, 4] },
        { sectionId: '2', documentClass: 'FieldTicket', pageIds: [2, 3] },
      ],
    });

    await movePageTo(2, '1');
    await userEvent.click(screen.getByRole('button', { name: /Save grouping/i }));

    const saved = onSave.mock.calls[0][0] as GroupedSection[];
    // Between 1 and 4, not appended after them — and so no 'Custom page order' badge.
    expect(saved.find((s) => s.sectionId === '1')?.pageIds).toEqual([1, 2, 4]);
    expect(screen.queryByText('Custom page order')).not.toBeInTheDocument();
  });
});

/**
 * Seeing a page at readable size.
 *
 * The thumbnail is too small to see the detail needed to judge a page.
 * Deciding which section a page belongs to means reading the page, and the card shows it
 * at 112px.
 *
 * Cheap because nothing is fetched — pages already render at 1200px wide
 * (`use-test-doc-pages`) and the document view presigns the pipeline's full-size image,
 * so the preview displays what is already in memory.
 */
describe('PageGroupingEditor page preview', () => {
  const openPreviewFor = async (pageId: number) => {
    await userEvent.click(screen.getByRole('button', { name: new RegExp(`^Move page ${pageId}\\b`) }));
    await userEvent.click(await screen.findByRole('menuitem', { name: 'View full page' }));
  };

  it('opens that page at full size from the menu', async () => {
    renderEditor();
    await openPreviewFor(3);

    // A dialog, so it traps focus and is dismissible by keyboard — the preview is
    // offered as a menu item rather than a click target on the card so it does not
    // compete with the drag gesture.
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('Page 3');
    // The same source the card uses, at unconstrained width.
    const image = within(dialog).getByAltText('Page 3');
    expect(image).toHaveAttribute('src', 'blob:page-3');
  });

  it('previews the page whose menu was used, not the first one', async () => {
    renderEditor();
    await openPreviewFor(4);

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByAltText('Page 4')).toBeInTheDocument();
    expect(within(dialog).queryByAltText('Page 1')).not.toBeInTheDocument();
  });

  it('closes again, leaving the board untouched', async () => {
    const { onSave } = renderEditor();
    await openPreviewFor(2);
    await userEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: /Close/i }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    // Looking at a page is not an edit.
    expect(screen.getByRole('button', { name: /Save grouping/i })).toBeDisabled();
    expect(onSave).not.toHaveBeenCalled();
  });

  it('is reachable while the board is expanded', async () => {
    // The preview renders as a SIBLING of the expanded board's modal, not nested inside
    // it: Cloudscape stacks sibling modals by z-index but makes no promise about
    // nesting one in another.
    renderEditor();
    await userEvent.click(screen.getByRole('button', { name: /^Expand$/i }));
    await openPreviewFor(1);

    const dialogs = await screen.findAllByRole('dialog');
    expect(dialogs.length).toBeGreaterThan(1);
    expect(dialogs.some((d) => d.textContent?.includes('Page 1'))).toBe(true);
  });

  it('says so when a page has not finished rendering, rather than showing a broken image', async () => {
    renderEditor({ pages: [{ id: 1 }, { id: 2, imageUri: 'blob:page-2' }], sections: [{ sectionId: '1', pageIds: [1, 2] }] });
    await openPreviewFor(1);

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent(/has not finished rendering/i);
    expect(within(dialog).queryByAltText('Page 1')).not.toBeInTheDocument();
  });
});
