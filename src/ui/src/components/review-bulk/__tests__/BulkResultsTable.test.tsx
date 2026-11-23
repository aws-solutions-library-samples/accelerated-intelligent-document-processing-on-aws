// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/** The results table: outcome labels, the outcome filter and the Excel export. */

import React from 'react';
import { act } from '@testing-library/react';
import { renderWithIntl } from '../../../test-utils/renderWithIntl';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import { describe, expect, it, vi } from 'vitest';

const { exportToExcel } = vi.hoisted(() => ({ exportToExcel: vi.fn() }));
vi.mock('../../common/download-func', () => ({ exportToExcel }));

import BulkResultsTable from '../BulkResultsTable';

const ROWS = [
  { objectKey: 'a.pdf', outcome: 'SKIPPED', kind: 'legacy', detail: null },
  { objectKey: 'b.pdf', outcome: 'HELD_BY_OTHER', kind: 'queue', detail: null },
  { objectKey: 'c.pdf', outcome: 'SKIPPED', kind: 'queue', detail: 'finalize_retry_pending' },
];

describe('BulkResultsTable', () => {
  it('labels outcomes and details, filters by outcome and exports what is shown', () => {
    renderWithIntl(<BulkResultsTable results={ROWS} exportName="results" />);
    const table = createWrapper().findTable()!;
    expect(table.findRows()).toHaveLength(3);
    expect(table.getElement().textContent).toContain('Held by another reviewer');
    expect(table.getElement().textContent).toContain('Finishing run will be retried');
    const select = createWrapper().findSelect()!;
    act(() => select.openDropdown());
    act(() => select.selectOptionByValue('SKIPPED'));
    expect(table.findRows()).toHaveLength(2);
    act(() => table.findHeaderSlot()!.findButton()!.click());
    expect(exportToExcel).toHaveBeenCalledWith(
      [
        { Document: 'a.pdf', Outcome: 'Skipped', Detail: '' },
        { Document: 'c.pdf', Outcome: 'Skipped', Detail: 'Finishing run will be retried' },
      ],
      'results',
    );
  });
});
