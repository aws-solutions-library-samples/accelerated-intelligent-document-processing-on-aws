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

import { render, screen } from '@testing-library/react';
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
  await userEvent.click(screen.getByRole('button', { name: new RegExp(`Move page ${pageId} to another section`) }));
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
      expect(screen.getByRole('button', { name: new RegExp(`Move page ${id} to another section`) })).toBeInTheDocument();
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
