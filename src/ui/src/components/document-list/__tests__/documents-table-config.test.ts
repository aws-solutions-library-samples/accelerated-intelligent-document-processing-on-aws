// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import type { ButtonDropdownProps } from '@cloudscape-design/components';
import {
  COLUMN_DEFINITIONS_MAIN,
  DOCUMENT_LIST_SHARDS_PER_DAY,
  KEY_COLUMN_ID,
  LATEST_PERIODS,
  TEST_RUN_COLUMN_ID,
  buildDownloadMenuItems,
  resolveInitialPeriodsToLoad,
  testRunIdFromObjectKey,
  type MappedDocument,
} from '../documents-table-config';
import { resolveDateRange } from '../../../hooks/use-graphql-api';

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

describe('testRunIdFromObjectKey', () => {
  it('recovers the run id from the prefix the test file copier writes', () => {
    expect(testRunIdFromObjectKey('W2-TestSet-20260417-125337/doc-1.pdf')).toBe('W2-TestSet-20260417-125337');
  });

  it('keeps hyphens and spaces that are part of the test set name', () => {
    expect(testRunIdFromObjectKey('ConfBench (full)-20260521-163329/a.pdf')).toBe('ConfBench (full)-20260521-163329');
    expect(testRunIdFromObjectKey('DocSplit-Poly-Seq-20260521-163329/nested/a.pdf')).toBe('DocSplit-Poly-Seq-20260521-163329');
  });

  it('returns null for keys without the run prefix, so an ordinary upload is never mislabelled', () => {
    expect(testRunIdFromObjectKey('lending_package.pdf')).toBeNull();
    expect(testRunIdFromObjectKey('tenant/one/lending.pdf')).toBeNull();
    // Right shape but no trailing slash: the prefix names a document, not a folder.
    expect(testRunIdFromObjectKey('set-20260417-125337')).toBeNull();
    // Timestamp must be the full YYYYMMDD-HHMMSS.
    expect(testRunIdFromObjectKey('set-2026041-12533/a.pdf')).toBeNull();
  });

  it('tolerates a missing key', () => {
    expect(testRunIdFromObjectKey(undefined)).toBeNull();
  });
});

describe('COLUMN_DEFINITIONS_MAIN', () => {
  const ids = (view?: 'PRODUCTION' | 'TEST') => COLUMN_DEFINITIONS_MAIN([], view).map((c) => c.id);

  it('adds the Test Run column only in the Test Studio view', () => {
    expect(ids('TEST')).toContain(TEST_RUN_COLUMN_ID);
    expect(ids('PRODUCTION')).not.toContain(TEST_RUN_COLUMN_ID);
  });

  it('defaults to the production view', () => {
    expect(ids()).toEqual(ids('PRODUCTION'));
  });

  it('keeps Document ID leftmost, with Test Run immediately after it', () => {
    // Column order comes from this array, not from the table's visibleColumns.
    expect(ids('TEST').slice(0, 2)).toEqual([KEY_COLUMN_ID, TEST_RUN_COLUMN_ID]);
    expect(ids('PRODUCTION')[0]).toBe(KEY_COLUMN_ID);
  });

  it('changes nothing else about the production columns', () => {
    expect(ids('TEST').filter((id) => id !== TEST_RUN_COLUMN_ID)).toEqual(ids('PRODUCTION'));
  });
});

describe('resolveDateRange', () => {
  it('returns no bounds for Latest, so the query runs newest-first over the whole partition', () => {
    expect(resolveDateRange(LATEST_PERIODS, null)).toBeNull();
  });

  it('converts a shard count into a window ending now', () => {
    const range = resolveDateRange(DOCUMENT_LIST_SHARDS_PER_DAY, null);
    expect(range).not.toBeNull();
    const hours = (Date.parse(range!.endDateTime) - Date.parse(range!.startDateTime)) / 3_600_000;
    expect(hours).toBeCloseTo(24, 3);
  });

  it('never produces a start after the end — the Latest sentinel is negative, and would if treated as a period', () => {
    for (const periods of [0.5, 1, DOCUMENT_LIST_SHARDS_PER_DAY * 30]) {
      const range = resolveDateRange(periods, null);
      expect(Date.parse(range!.startDateTime)).toBeLessThan(Date.parse(range!.endDateTime));
    }
  });

  it('lets a custom range win over both, matching the dropdown clearing one for the other', () => {
    const custom = { startDateTime: '2026-01-01T00:00:00.000Z', endDateTime: '2026-01-02T00:00:00.000Z' };
    expect(resolveDateRange(DOCUMENT_LIST_SHARDS_PER_DAY, custom)).toBe(custom);
    expect(resolveDateRange(LATEST_PERIODS, custom)).toBe(custom);
  });
});

describe('resolveInitialPeriodsToLoad', () => {
  it('defaults to Latest when nothing is stored, so a quiet stack never opens on an empty table', () => {
    // 0 is what the caller passes for an absent localStorage key.
    expect(resolveInitialPeriodsToLoad(0)).toBe(LATEST_PERIODS);
  });

  it('round-trips the Latest sentinel instead of reading it back as a window', () => {
    // Math.abs(-2) is 2, a perfectly plausible shard count — the sentinel has to be
    // recognised before that normalisation runs.
    expect(resolveInitialPeriodsToLoad(LATEST_PERIODS)).toBe(LATEST_PERIODS);
    expect(resolveInitialPeriodsToLoad(LATEST_PERIODS)).not.toBe(Math.abs(LATEST_PERIODS));
  });

  it('keeps a stored window, so an existing preference survives the default change', () => {
    expect(resolveInitialPeriodsToLoad(0.5)).toBe(0.5);
    expect(resolveInitialPeriodsToLoad(DOCUMENT_LIST_SHARDS_PER_DAY)).toBe(DOCUMENT_LIST_SHARDS_PER_DAY);
    expect(resolveInitialPeriodsToLoad(DOCUMENT_LIST_SHARDS_PER_DAY * 30)).toBe(DOCUMENT_LIST_SHARDS_PER_DAY * 30);
  });

  it('normalises a negative window to its magnitude, as before', () => {
    expect(resolveInitialPeriodsToLoad(-6)).toBe(6);
  });

  it('falls back to Latest for values it cannot use', () => {
    expect(resolveInitialPeriodsToLoad(DOCUMENT_LIST_SHARDS_PER_DAY * 30 + 1)).toBe(LATEST_PERIODS);
    expect(resolveInitialPeriodsToLoad('nonsense')).toBe(LATEST_PERIODS);
    expect(resolveInitialPeriodsToLoad(null)).toBe(LATEST_PERIODS);
    expect(resolveInitialPeriodsToLoad(undefined)).toBe(LATEST_PERIODS);
    expect(resolveInitialPeriodsToLoad(Number.NaN)).toBe(LATEST_PERIODS);
    expect(resolveInitialPeriodsToLoad(Number.POSITIVE_INFINITY)).toBe(LATEST_PERIODS);
  });
});
