// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The HITL work table's Document column: rows whose keys share a file name show
 * different text (the distinguishing trailing path), long names are cut in the
 * middle, and the full object key is the hover title.
 */

import React from 'react';
import { render } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { describe, expect, it } from 'vitest';

import WorkItemsTable from '../WorkItemsTable';
import type { WorkItem } from '../work-items';

const row = (objectKey: string): WorkItem => ({
  id: objectKey,
  source: 'production',
  objectKey,
  name: objectKey.split('/').pop() ?? objectKey,
  queue: 'default',
  priority: null,
  enqueuedAt: null,
  reason: null,
  extraReasons: 0,
  state: { label: 'Waiting', type: 'pending' },
  assignedTo: null,
  assignedToMe: false,
  href: '#/edit',
});

const documentCells = (keys: string[]): HTMLElement[] => {
  const { container } = render(<WorkItemsTable items={keys.map(row)} title="Items" columns={['document']} />);
  const table = createWrapper(container).findTable()!;
  return keys.map((_, i) =>
    table
      .findBodyCell(i + 1, 1)!
      .find('span[title]')!
      .getElement(),
  );
};

describe('WorkItemsTable document names', () => {
  it('renders different text for two rows with the same file name, with the full key on hover', () => {
    const keys = ['inbound/2026/a/lending_package.pdf', 'inbound/2026/b/lending_package.pdf'];
    const cells = documentCells(keys);
    expect(cells[0].textContent).toBe('a/lending_package.pdf');
    expect(cells[1].textContent).toBe('b/lending_package.pdf');
    expect(cells[0].getAttribute('title')).toBe(keys[0]);
    expect(cells[1].getAttribute('title')).toBe(keys[1]);
  });

  it('cuts a long distinguishing name in the middle, keeping its start and end', () => {
    const keys = ['q3-customer-onboarding-archive/lending_package_final.pdf', 'q4-customer-onboarding-archive/lending_package_final.pdf'];
    const cells = documentCells(keys);
    const [first, second] = cells.map((c) => c.textContent ?? '');
    expect(first).toHaveLength(40);
    expect(first).toBe('q3-customer-onboardi…g_package_final.pdf');
    expect(second.startsWith('q4-')).toBe(true);
    expect(cells[0].getAttribute('title')).toBe(keys[0]);
  });
});
