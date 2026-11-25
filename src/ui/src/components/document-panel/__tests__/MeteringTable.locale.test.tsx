// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Estimated Cost table shows every amount as `US$`, in the UI locale's own digits.
 *
 * A reader in a locale with another dollar must not have to guess the currency, so
 * the symbol is explicit in every language: `US$0.0020` under `en`, `0,0020 US$` under
 * `fr-CA`. The unit price follows the same adaptive rule as Test Studio's cost table,
 * so one price renders one way on both screens.
 */

import { render, screen, within } from '@testing-library/react';
import React from 'react';
import { IntlProvider } from 'react-intl';
import { describe, expect, it, vi } from 'vitest';

import { renderWithIntl } from '../../../test-utils/renderWithIntl';
import { formatUnitCostUsd } from '../../test-studio/formatCost';

// One object for every render: the table re-derives its lookup when `pricing` changes identity.
const PRICING = vi.hoisted(() => ({
  pricing: { pricing: [{ name: 'bedrock/m', units: [{ name: 'outputTokens', price: '2.0E-6' }] }] },
  nonChargeableUnits: {},
  loading: false,
}));
vi.mock('../../../hooks/use-pricing', () => ({ default: () => PRICING }));

import MeteringTable from '../MeteringTable';

// 1000 output tokens at 2.0E-6 each: a cost of 0.0020.
const METERING = { 'Extraction/bedrock/m': { inputTokens: 100, outputTokens: 1000 } };

const English = ({ children }: { children: React.ReactNode }) => (
  <IntlProvider locale="en" defaultLocale="en">
    {children}
  </IntlProvider>
);

const rowFor = (unit: string): string => {
  const row = within(screen.getByRole('table'))
    .getAllByRole('row')
    .find((r) => within(r).queryByText(unit) !== null);
  expect(row).toBeDefined();
  return row!.textContent ?? '';
};

describe('Estimated Cost table amounts', () => {
  it('reads US$ under en, with no bare $ anywhere in the table', () => {
    render(<MeteringTable meteringData={METERING} />, { wrapper: English });
    const output = rowFor('outputTokens');
    expect(output).toContain('US$0.0020');
    // The unit price is Test Studio's rendering of the same price.
    expect(output).toContain(formatUnitCostUsd(2e-6, 'en'));
    expect(screen.getByRole('table').textContent ?? '').not.toMatch(/(?<!US)\$/);
  });

  it('reads the French digits with a trailing US$ under fr-CA', () => {
    // The space before the symbol is a no-break space whose codepoint varies between ICU releases.
    renderWithIntl(<MeteringTable meteringData={METERING} />, { locale: 'fr-CA', enabledLocales: ['en', 'fr-CA'] });
    expect(rowFor('outputTokens')).toMatch(/0,0020\sUS\$/);
  });
});
