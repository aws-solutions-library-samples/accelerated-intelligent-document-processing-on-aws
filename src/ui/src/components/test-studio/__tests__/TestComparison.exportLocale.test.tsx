// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The comparison's cost cells follow the UI locale; the CSV it downloads does not.
 *
 * On screen, Total Cost is prose: under `fr-CA` it reads `0,5000 US$`, the shape `Intl` gives
 * a US-dollar amount in that locale, with the decimal comma and a (no-break) space before the
 * currency. The CSV is read by other software, a spreadsheet or a script that parses each cell
 * back, so its cells keep `$` and the `.` decimal in every locale: `"$0.5000"` round-trips,
 * `0,5000 US$` would not; and a token count is written as plain digits, `"1234567"`, never
 * grouped.
 */

import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';

import { renderWithIntl } from '../../../test-utils/renderWithIntl';

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
          'run-b': run({
            filesCount: 4,
            completedFiles: 3,
            failedFiles: 1,
            // One usage line: `value` is a token count the CSV must write as plain digits.
            costBreakdown: { ctx: { 'bedrock/claude_inputTokens': { value: 1234567, estimated_cost: 1.23 } } },
          }),
          'run-c': run({}),
        }),
      },
    },
  });

  renderWithIntl(<TestComparison preSelectedTestRunIds={['run-a', 'run-b', 'run-c']} />, {
    locale: 'fr-CA',
    enabledLocales: ['en', 'fr-CA'],
    wrapper: MemoryRouter,
  });

  // The component pauses 500 ms after the response to show 100% progress.
  await screen.findAllByText('Files Completed', { selector: 'td, td *' }, { timeout: 3000 });
};

describe('TestComparison costs under a locale', () => {
  it('shows the total cost in the fr-CA shape on screen', async () => {
    await renderComparison();

    // Awaited, as the helper's notes ask of the first assertion on a locale: the provider's
    // catalog load settles inside the wait instead of after the test body.
    await waitFor(() => {
      const cells = rowCells('Total Cost').slice(1);
      expect(cells).toHaveLength(3);
      for (const cell of cells) expect(cell).toMatch(/^0,5000\sUS\$$/);
    });
  });

  it('keeps the downloaded CSV locale-neutral', async () => {
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
    expect(row('Total Cost')).toBe('"Total Cost","$0.5000","$0.5000","$0.5000"');

    // The usage section follows the cost section, whose own "ctx" line reads "$1.2300".
    const usage = lines.slice(lines.indexOf('"=== USAGE BREAKDOWN ==="'));
    expect(usage.find((l) => l.startsWith('"ctx",'))).toBe('"ctx","bedrock/claude","inputTokens","N/A","1234567","N/A"');
  });
});
