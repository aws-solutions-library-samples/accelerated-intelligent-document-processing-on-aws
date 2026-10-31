// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * History › Field Edits. What has to hold: it is a table like Review Activity (When,
 * By, then the change), with an empty table rather than an alert when nothing was
 * edited; it reads the entries every save path writes — the edit screen's own
 * (`buildEditEntry`, a direct edit or a production draft) and the review API's for a
 * Test Studio label (`baselineEdits`, by email) — one row per changed field, newest
 * first; an entry written to both the prediction and the baseline is listed once; and
 * an entry that changed no field (a confirmation) adds no row.
 */

import React from 'react';
import { render } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { describe, expect, it } from 'vitest';

import EditorHistoryModal from '../EditorHistoryModal';
import FieldEditsTable, { fieldEditRows } from '../FieldEditsTable';
import { buildEditEntry, diffLeafPaths } from '../document-edit-save';

const savedWith = (entries: unknown[]) => ({ inference_result: { Name: 'Bob' }, _editHistory: entries });

/** The entry a save on the edit screen writes, from the before and after of one section. */
const clientEntry = (timestamp: string) => ({
  ...buildEditEntry(
    'alice',
    diffLeafPaths({ inference_result: { Name: 'Ann', Total: 10 } }, { inference_result: { Name: 'Bob', Total: 12 } }),
    new Map(),
  ),
  timestamp,
});

/** The entry the review API writes for a Test Studio label. */
const serverEntry = {
  timestamp: '2026-10-22T12:00:00Z',
  editedBy: 'ann',
  editedByEmail: 'ann@example.com',
  source: 'annotation-review',
  baselineEdits: { changedFields: ['total'], changeCount: 1, diffs: { total: { originalValue: '100.00', newValue: '105.00' } } },
};

const table = () => createWrapper(document.body).findTable()!;
const headers = () =>
  table()
    .findColumnHeaders()
    .map((h) => h.getElement().textContent);

describe('History › Field Edits', () => {
  it('is an empty table, not an alert, when nothing was edited', () => {
    render(<FieldEditsTable sectionData={{ inference_result: {} }} />);
    expect(table().findEmptySlot()!.getElement().textContent).toBe('No field has been edited in this section yet.');
    expect(createWrapper(document.body).findAlert()).toBeNull();
    expect(headers()).toEqual(['When', 'By', 'Field', 'From', 'To']);
  });

  it('lists each field a save on the edit screen changed, as it records them', () => {
    render(<FieldEditsTable sectionData={savedWith([clientEntry('2026-10-22T10:00:00Z')])} />);
    const rows = table().findRows();
    expect(rows).toHaveLength(2);
    const text = rows.map((r) => r.getElement().textContent);
    expect(text.find((t) => t?.includes('Name'))).toMatch(/alice.*Name.*Ann.*Bob/);
    expect(text.find((t) => t?.includes('Total'))).toMatch(/Total.*10.*12/);
  });

  it('lists a Test Studio label edit from the review API, by email, as a baseline edit', () => {
    render(<FieldEditsTable sectionData={savedWith([serverEntry])} />);
    expect(headers()).toEqual(['When', 'By', 'Field', 'From', 'To', 'Applies to']);
    expect(table().findRows()[0].getElement().textContent).toMatch(/ann@example\.com.*total.*100\.00.*105\.00.*Baseline/);
  });

  it('lists the newest edit first, and an entry in both files once', () => {
    const older = clientEntry('2026-10-22T09:00:00Z');
    const newer = clientEntry('2026-10-22T11:00:00Z');
    const rows = fieldEditRows(savedWith([older, newer]), savedWith([older]));
    expect(rows).toHaveLength(4);
    expect(rows[0].at).toBe('2026-10-22T11:00:00Z');
    expect(rows[3].at).toBe('2026-10-22T09:00:00Z');
  });

  it('adds no row for an entry that changed no field', () => {
    const confirmed = { timestamp: '2026-10-22T10:00:00Z', editedBy: 'ann', source: 'annotation-review' };
    expect(fieldEditRows(savedWith([confirmed]))).toEqual([]);
  });

  it('is the Field Edits tab of History, beside Review Activity', () => {
    render(<EditorHistoryModal visible onDismiss={() => undefined} sectionData={savedWith([clientEntry('2026-10-22T10:00:00Z')])} />);
    const tabs = createWrapper(document.body).findTabs()!;
    expect(tabs.findTabLinkByIndex(1)!.getElement().textContent).toBe('Field Edits');
    expect(tabs.findTabContent()!.findTable()!.findRows()).toHaveLength(2);
  });
});
