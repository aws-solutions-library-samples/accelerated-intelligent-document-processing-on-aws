// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The comparison's file counts show a genuine zero as 0, not as N/A.
 *
 * Files Processed, Files Completed and Files Failed are counts, and 0 is a real
 * value for each of them — Files Failed most of all, since it is 0 on every clean
 * run. A falsy fallback rendered all of those as "N/A", which reads as "this run
 * did not report the number". N/A is for a count that is genuinely absent.
 */

import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }) }));
vi.mock('../../../hooks/use-configuration-versions', () => ({ default: () => ({ versions: [] }) }));

import TestComparison from '../TestComparison';

const run = (overrides: Record<string, unknown>) => ({
  status: 'COMPLETE',
  testSetName: 'set',
  context: 'ctx',
  totalCost: 0.5,
  ...overrides,
});

const rowCells = (label: string): string[] => {
  const row = screen.getAllByText(label, { selector: 'td, td *' })[0].closest('tr');
  if (!row) throw new Error(`no row for ${label}`);
  return within(row)
    .getAllByRole('cell')
    .map((cell) => cell.textContent ?? '');
};

const renderComparison = async () => {
  graphql.mockResolvedValue({
    data: {
      compareTestRuns: {
        configs: [],
        metrics: JSON.stringify({
          'run-a': run({ filesCount: 0, completedFiles: 0, failedFiles: 0 }),
          'run-b': run({ filesCount: 4, completedFiles: 3, failedFiles: 1 }),
          'run-c': run({}),
        }),
      },
    },
  });

  render(
    <MemoryRouter>
      <TestComparison preSelectedTestRunIds={['run-a', 'run-b', 'run-c']} />
    </MemoryRouter>,
  );

  // The component pauses 500 ms after the response to show 100% progress.
  await screen.findAllByText('Files Completed', { selector: 'td, td *' }, { timeout: 3000 });
};

describe('TestComparison file counts', () => {
  it('renders a zero count as 0 and an absent count as N/A', async () => {
    await renderComparison();

    expect(rowCells('Files Processed').slice(1)).toEqual(['0', '4', 'N/A']);
    expect(rowCells('Files Completed').slice(1)).toEqual(['0', '3', 'N/A']);
    expect(rowCells('Files Failed').slice(1)).toEqual(['0', '1', 'N/A']);
  });

  it('exports a zero count as 0 and an absent count as N/A in the CSV', async () => {
    await renderComparison();
    const blobs: Blob[] = [];
    URL.createObjectURL = vi.fn((blob: Blob) => {
      blobs.push(blob);
      return 'blob:csv';
    });
    URL.revokeObjectURL = vi.fn();
    const user = userEvent.setup();

    // The page's download dropdown; its items are rendered only once it is open.
    const triggers = screen.getAllByRole('button').filter((b) => b.getAttribute('aria-haspopup'));
    for (const trigger of triggers) {
      await user.click(trigger);
      const csv = screen.queryByText('CSV');
      if (csv) {
        await user.click(csv);
        break;
      }
    }
    await waitFor(() => expect(blobs).toHaveLength(1));

    const lines = (await blobs[0].text()).split('\n');
    const row = (label: string) => lines.find((l) => l.startsWith(`"${label}",`));
    expect(row('Files Processed')).toBe('"Files Processed","0","4","N/A"');
    expect(row('Files Completed')).toBe('"Files Completed","0","3","N/A"');
    expect(row('Files Failed')).toBe('"Files Failed","0","1","N/A"');
  });
});
