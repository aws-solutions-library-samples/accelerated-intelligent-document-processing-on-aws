// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Which pricing rows can be deleted on the Pricing page.
 *
 * The backend lays the saved table over the shipped defaults per model and unit
 * (#1886), so a default unit deleted in the editor comes back after a save and
 * reload. The page therefore disables "Delete unit" on default rows, with a reason,
 * keeps it on custom rows, and offers "Restore default price" on a default row
 * whose price has been overridden.
 *
 * The page is rendered for real; only `usePricing` and `useUserRole` are mocked.
 * The defaults are `config_library/pricing.yaml`, read from disk, so a default row
 * is one the shipped table actually contains.
 */

import React from 'react';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import yaml from 'js-yaml';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { IntlProvider } from 'react-intl';

import { defaultUnitPriceLookup } from '../pricing-customizations';
import type { PricingTableLike } from '../pricing-customizations';

vi.mock('@monaco-editor/react', () => ({ default: () => null }));

const pricingState = vi.hoisted(() => ({
  pricing: null as unknown,
  defaultPricing: null as unknown,
  isAdmin: true,
}));
vi.mock('../../../hooks/use-pricing', () => ({
  default: () => ({
    pricing: pricingState.pricing,
    defaultPricing: pricingState.defaultPricing,
    nonChargeableUnits: {},
    loading: false,
    refreshing: false,
    error: null,
    fetchPricing: vi.fn(),
    updatePricing: vi.fn(),
    restoreDefaultPricing: vi.fn(),
  }),
}));
vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({ isAdmin: pricingState.isAdmin, loading: false, sessionError: false, retrySession: vi.fn() }),
}));

import PricingLayout from '../PricingLayout';

const PRICING_YAML = resolve(__dirname, '../../../../../../config_library/pricing.yaml');

const loadDefaults = (): PricingTableLike => {
  const raw = yaml.load(readFileSync(PRICING_YAML, 'utf-8')) as PricingTableLike;
  // The resolver sends every price as a string (PricingUnit.price is `str`).
  return {
    pricing: raw.pricing.map((e) => ({ name: e.name, units: e.units.map((u) => ({ name: u.name, price: String(u.price) })) })),
  };
};

const DEFAULTS = loadDefaults();
const copy = (t: PricingTableLike): PricingTableLike => JSON.parse(JSON.stringify(t));

/** A default unit with a non-zero price, whose service the page groups by. */
const firstDefault = (): { entry: number; unit: number; price: string } => {
  for (const [i, e] of DEFAULTS.pricing.entries()) {
    if (!e.name.includes('/')) continue;
    for (const [j, u] of e.units.entries()) {
      if (Number(u.price) > 0) return { entry: i, unit: j, price: String(u.price) };
    }
  }
  throw new Error('pricing.yaml has no priced unit');
};

const CUSTOM_NAME = 'bedrock/example.private-model';
const CUSTOM_PRICE = '1.234E-6';

const renderWith = (pricing: PricingTableLike): void => {
  pricingState.pricing = pricing;
  pricingState.defaultPricing = DEFAULTS;
  render(
    <IntlProvider locale="en" defaultLocale="en">
      <PricingLayout />
    </IntlProvider>,
  );
};

/** The table row whose price field shows `price`; collapsed sections are still in the DOM. */
const rowWithPrice = (price: string): HTMLElement => {
  const inputs = screen.getAllByDisplayValue(price, { exact: true });
  const rows = new Set(inputs.map((i) => i.closest('tr')).filter((r): r is HTMLTableRowElement => r !== null));
  expect(rows.size).toBe(1);
  return [...rows][0];
};

const isDisabled = (button: HTMLElement): boolean =>
  (button as HTMLButtonElement).disabled || button.getAttribute('aria-disabled') === 'true';

const rowButton = (row: HTMLElement, name: string): HTMLElement => within(row).getByRole('button', { name, hidden: true });

const PAGE_RENDER_TIMEOUT_MS = 30_000;

describe('Delete unit and Restore default price on the Pricing page', { timeout: PAGE_RENDER_TIMEOUT_MS }, () => {
  beforeEach(() => {
    pricingState.pricing = null;
    pricingState.defaultPricing = null;
    pricingState.isAdmin = true;
  });

  it('disables Delete unit on a default row and says why', () => {
    const { entry, unit } = firstDefault();
    const edited = copy(DEFAULTS);
    // A unique price, so the row can be found; it is still a default row.
    edited.pricing[entry].units[unit].price = '0.000777';
    renderWith(edited);
    const row = rowWithPrice('0.000777');
    const del = rowButton(row, 'Delete unit');
    expect(isDisabled(del)).toBe(true);
    // Cloudscape renders the disabled reason as the button's accessible description.
    const describedBy = del.getAttribute('aria-describedby');
    expect(describedBy).toBeTruthy();
    const description = describedBy!
      .split(' ')
      .map((id) => document.getElementById(id)?.textContent ?? '')
      .join(' ');
    expect(description).toMatch(/This unit is in the default price list, so it cannot be deleted/);
  });

  it('keeps Delete unit enabled on a custom row, and deleting removes it', () => {
    const edited = copy(DEFAULTS);
    edited.pricing.push({ name: CUSTOM_NAME, units: [{ name: 'inputTokens', price: CUSTOM_PRICE }] });
    renderWith(edited);
    const row = rowWithPrice(CUSTOM_PRICE);
    const del = rowButton(row, 'Delete unit');
    expect(isDisabled(del)).toBe(false);
    expect(within(row).queryByRole('button', { name: 'Restore default price', hidden: true })).toBeNull();
    fireEvent.click(del);
    expect(screen.queryAllByDisplayValue(CUSTOM_PRICE, { exact: true })).toHaveLength(0);
  });

  it('treats a unit the defaults lack as custom even on a default model', () => {
    const { entry } = firstDefault();
    const edited = copy(DEFAULTS);
    edited.pricing[entry].units.push({ name: 'exampleExtraUnit', price: CUSTOM_PRICE });
    renderWith(edited);
    expect(isDisabled(rowButton(rowWithPrice(CUSTOM_PRICE), 'Delete unit'))).toBe(false);
  });

  it('lets an overridden default price be edited and restored to its default', () => {
    const { entry, unit, price } = firstDefault();
    const edited = copy(DEFAULTS);
    edited.pricing[entry].units[unit].price = '0.000777';
    renderWith(edited);
    const row = rowWithPrice('0.000777');
    const priceInput = within(row).getByDisplayValue('0.000777') as HTMLInputElement;
    expect(priceInput.disabled).toBe(false);

    const restore = rowButton(row, 'Restore default price');
    expect(isDisabled(restore)).toBe(false);
    fireEvent.click(restore);
    expect(priceInput.value).toBe(price);
    expect(isDisabled(rowButton(row, 'Restore default price'))).toBe(true);
    expect(isDisabled(rowButton(row, 'Delete unit'))).toBe(true);
  });

  it('disables Restore default price on a default row already at its default price', () => {
    const { entry, unit } = firstDefault();
    const edited = copy(DEFAULTS);
    // The same value in another notation is not a change.
    const original = Number(edited.pricing[entry].units[unit].price);
    edited.pricing[entry].units[unit].price = original.toExponential(12).toUpperCase();
    expect(Number(edited.pricing[entry].units[unit].price)).toBe(original);
    renderWith(edited);
    const row = rowWithPrice(edited.pricing[entry].units[unit].price);
    expect(isDisabled(rowButton(row, 'Restore default price'))).toBe(true);
  });

  it('disables both actions for a non-administrator', () => {
    pricingState.isAdmin = false;
    const edited = copy(DEFAULTS);
    edited.pricing.push({ name: CUSTOM_NAME, units: [{ name: 'inputTokens', price: CUSTOM_PRICE }] });
    renderWith(edited);
    expect(isDisabled(rowButton(rowWithPrice(CUSTOM_PRICE), 'Delete unit'))).toBe(true);
  });
});

describe('defaultUnitPriceLookup', () => {
  it('finds every default unit and no other', () => {
    const lookup = defaultUnitPriceLookup(DEFAULTS);
    let count = 0;
    for (const e of DEFAULTS.pricing) {
      for (const u of e.units) {
        expect(lookup(e.name, u.name)).toBeDefined();
        count += 1;
      }
    }
    expect(count).toBeGreaterThan(0);
    expect(lookup(CUSTOM_NAME, 'inputTokens')).toBeUndefined();
    expect(lookup(DEFAULTS.pricing[0].name, 'exampleExtraUnit')).toBeUndefined();
  });

  it('uses the last of two default rows with the same names, as the cost readers do', () => {
    const lookup = defaultUnitPriceLookup({
      pricing: [
        { name: 'bedrock/x', units: [{ name: 'u', price: '1' }] },
        { name: 'bedrock/x', units: [{ name: 'u', price: '2' }] },
      ],
    });
    expect(lookup('bedrock/x', 'u')).toBe('2');
  });

  it('treats every unit as custom when the defaults are missing', () => {
    expect(defaultUnitPriceLookup(null)('bedrock/x', 'u')).toBeUndefined();
    expect(defaultUnitPriceLookup({})('bedrock/x', 'u')).toBeUndefined();
  });
});
