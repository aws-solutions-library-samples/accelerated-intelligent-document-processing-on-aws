// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import type { ButtonDropdownProps } from '@cloudscape-design/components';
import { buildDownloadMenuItems, type MappedDocument } from '../documents-table-config';

const row = (overrides: Partial<MappedDocument> = {}): MappedDocument =>
  ({
    objectKey: 'tenant/one/lending.pdf',
    objectStatus: 'COMPLETED',
    evaluationStatus: 'NOT_EVALUATED',
    ...overrides,
  }) as MappedDocument;

const group = (items: ButtonDropdownProps.ItemOrGroup[], index: number): ButtonDropdownProps.ItemGroup =>
  items[index] as ButtonDropdownProps.ItemGroup;

const itemById = (items: ButtonDropdownProps.ItemOrGroup[], id: string): ButtonDropdownProps.Item => {
  const all = items.flatMap((entry) => ('items' in entry ? entry.items : [entry]));
  const found = all.find((entry) => 'id' in entry && entry.id === id);
  if (!found) throw new Error(`No menu item with id ${id}`);
  return found as ButtonDropdownProps.Item;
};

describe('buildDownloadMenuItems', () => {
  it('separates the list export from the selection exports, with counts in the group labels', () => {
    const items = buildDownloadMenuItems([row(), row({ objectKey: 'b.pdf' })], 142);

    expect(group(items, 0).text).toBe('Document list');
    expect(group(items, 1).text).toBe('Selected documents (2)');
    expect(itemById(items, 'excel').text).toBe('Table as Excel (142 rows)');
    expect(group(items, 1).items.map((i) => ('id' in i ? i.id : ''))).toEqual(['all', 'predictions', 'baselines']);
  });

  it('singularises the filtered row count', () => {
    expect(itemById(buildDownloadMenuItems([], 1), 'excel').text).toBe('Table as Excel (1 row)');
  });

  it('disables the ZIP scopes with a reason when nothing is selected, but keeps Excel available', () => {
    const items = buildDownloadMenuItems([], 10);

    expect(group(items, 1).text).toBe('Selected documents (0)');
    for (const id of ['all', 'predictions', 'baselines']) {
      expect(itemById(items, id).disabled).toBe(true);
      expect(itemById(items, id).disabledReason).toMatch(/Select one or more documents/);
    }
    expect(itemById(items, 'excel').disabled).toBeFalsy();
  });

  it('enables baselines when at least one selected document has a baseline', () => {
    const items = buildDownloadMenuItems([row(), row({ objectKey: 'b.pdf', evaluationStatus: 'BASELINE_AVAILABLE' })], 2);
    expect(itemById(items, 'baselines').disabled).toBe(false);
    expect(itemById(items, 'baselines').disabledReason).toBeUndefined();
  });

  it('disables baselines with an explanatory reason when no selected document has one', () => {
    const items = buildDownloadMenuItems([row(), row({ objectKey: 'b.pdf', evaluationStatus: 'RUNNING' })], 2);

    expect(itemById(items, 'baselines').disabled).toBe(true);
    expect(itemById(items, 'baselines').disabledReason).toMatch(/no selected document has an evaluation baseline/i);
    // The other scopes stay available
    expect(itemById(items, 'all').disabled).toBe(false);
    expect(itemById(items, 'predictions').disabled).toBe(false);
  });

  it('treats a COMPLETED evaluation as having a baseline', () => {
    const items = buildDownloadMenuItems([row({ evaluationStatus: 'COMPLETED' })], 1);
    expect(itemById(items, 'baselines').disabled).toBe(false);
  });

  it('holds only the ZIP scopes while a download runs, leaving the Excel export usable', () => {
    const items = buildDownloadMenuItems([row({ evaluationStatus: 'COMPLETED' })], 3, true, true);

    for (const id of ['all', 'predictions', 'baselines']) {
      expect(itemById(items, id).disabled).toBe(true);
      expect(itemById(items, id).disabledReason).toMatch(/already in progress/);
    }
    expect(itemById(items, 'excel').disabled).toBeFalsy();
  });

  it('omits the selection group entirely when bulk export is unavailable', () => {
    const items = buildDownloadMenuItems([row()], 5, false);
    expect(items).toHaveLength(1);
    expect(group(items, 0).text).toBe('Document list');
  });
});
