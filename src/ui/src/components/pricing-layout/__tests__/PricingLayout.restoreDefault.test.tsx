// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * When "Restore default (All)" is enabled on the Pricing page.
 *
 * The page is rendered for real; only `usePricing` and `useUserRole` are mocked,
 * at the module boundary. The defaults are the shipped table,
 * `config_library/pricing.yaml`, read from disk, so the price magnitudes are the
 * ones an operator actually edits rather than round test numbers. The button was
 * gated on an absolute tolerance of 1e-6, which is $1 per million tokens — larger
 * than many whole Bedrock token rates — so every case that matters lives below it.
 */

import React from 'react';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import yaml from 'js-yaml';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { IntlProvider } from 'react-intl';

import { hasPricingCustomizations, pricesDiffer } from '../pricing-customizations';
import type { PricingTableLike } from '../pricing-customizations';

vi.mock('@monaco-editor/react', () => ({ default: () => null }));

const pricingState = vi.hoisted(() => ({
  pricing: null as unknown,
  defaultPricing: null as unknown,
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
  default: () => ({ isAdmin: true, loading: false, sessionError: false, retrySession: vi.fn() }),
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

/** The first default unit whose price is below the old 1e-6 tolerance's reach. */
const smallRate = (): { entry: number; unit: number; price: number } => {
  for (const [i, e] of DEFAULTS.pricing.entries()) {
    for (const [j, u] of e.units.entries()) {
      const p = Number(u.price);
      if (p > 0 && p < 1e-5) return { entry: i, unit: j, price: p };
    }
  }
  throw new Error('pricing.yaml has no per-token rate below 1e-5');
};

const restoreButton = (): HTMLButtonElement => screen.getByRole('button', { name: 'Restore default (All)' }) as HTMLButtonElement;

const renderWith = (pricing: PricingTableLike): void => {
  pricingState.pricing = pricing;
  pricingState.defaultPricing = DEFAULTS;
  render(
    <IntlProvider locale="en" defaultLocale="en">
      <PricingLayout />
    </IntlProvider>,
  );
};

describe('Restore default (All) on the Pricing page', () => {
  beforeEach(() => {
    pricingState.pricing = null;
    pricingState.defaultPricing = null;
  });

  it('reads a real table of per-token rates', () => {
    expect(DEFAULTS.pricing.length).toBeGreaterThan(50);
    expect(smallRate().price).toBeLessThan(1e-5);
  });

  it('tells an administrator that a deleted default unit comes back after a save', () => {
    // The backend lays the saved table over the defaults per unit (#1886), so a
    // deletion is not stored; the page says so rather than implying it sticks.
    renderWith(copy(DEFAULTS));
    expect(screen.getByText(/A default unit you delete comes back at its default price after you save and reload/)).toBeTruthy();
  });

  it('is disabled when the table equals the defaults', () => {
    renderWith(copy(DEFAULTS));
    expect(restoreButton().disabled).toBe(true);
  });

  it('is enabled for a token-rate change smaller than $1 per million tokens', () => {
    const { entry, unit, price } = smallRate();
    const edited = copy(DEFAULTS);
    // A 10% negotiated discount: well under 1e-6 in absolute terms.
    edited.pricing[entry].units[unit].price = String(price * 0.9);
    expect(Math.abs(price * 0.1)).toBeLessThan(1e-6);
    renderWith(edited);
    expect(restoreButton().disabled).toBe(false);
  });

  it('is enabled when a default row has been deleted', () => {
    const edited = copy(DEFAULTS);
    edited.pricing[0].units = edited.pricing[0].units.slice(1);
    if (edited.pricing[0].units.length === 0) edited.pricing.shift();
    renderWith(edited);
    expect(restoreButton().disabled).toBe(false);
  });

  it('is enabled for a price one representable step away from its default', () => {
    // Pins exact comparison: any tolerance, absolute or relative, lets this through.
    const { entry, unit, price } = smallRate();
    const next = new Float64Array([price]);
    new BigInt64Array(next.buffer)[0] += 1n;
    expect(next[0]).not.toBe(price);
    const edited = copy(DEFAULTS);
    edited.pricing[entry].units[unit].price = String(next[0]);
    renderWith(edited);
    expect(restoreButton().disabled).toBe(false);
  });

  it('is enabled when one unit is deleted from a model that keeps its other units', () => {
    // What the per-unit remove icon does: the entry survives, one key does not.
    const i = DEFAULTS.pricing.findIndex((e) => e.units.length > 2);
    expect(i).toBeGreaterThanOrEqual(0);
    const edited = copy(DEFAULTS);
    edited.pricing[i].units = edited.pricing[i].units.slice(0, -1);
    renderWith(edited);
    expect(restoreButton().disabled).toBe(false);
  });

  it('detects a deleted default unit even when the table keeps the same number of rows', () => {
    // A duplicated row stands in for the deleted one, so neither a row count nor
    // the added-row check can see the deletion; only the missing key can.
    const i = DEFAULTS.pricing.findIndex((e) => e.units.length > 2);
    const edited = copy(DEFAULTS);
    edited.pricing[i].units.pop();
    edited.pricing[i].units.push({ ...edited.pricing[i].units[0] });
    expect(hasPricingCustomizations(edited, DEFAULTS)).toBe(true);
  });

  it('is enabled when a row the defaults lack has been added', () => {
    const edited = copy(DEFAULTS);
    edited.pricing.push({ name: 'bedrock/example.private-model', units: [{ name: 'inputTokens', price: '1.0E-6' }] });
    renderWith(edited);
    expect(restoreButton().disabled).toBe(false);
  });

  it('stays disabled when a default price is only written in another notation', () => {
    const { entry, unit, price } = smallRate();
    const edited = copy(DEFAULTS);
    edited.pricing[entry].units[unit].price = price.toExponential().toUpperCase();
    expect(edited.pricing[entry].units[unit].price).not.toBe(DEFAULTS.pricing[entry].units[unit].price);
    renderWith(edited);
    expect(restoreButton().disabled).toBe(true);
  });
});

describe('pricesDiffer', () => {
  it.each([
    ['2.2E-6', '0.0000022', false],
    ['2.2e-06', '2.2E-6', false],
    ['2.2E-6', '2.0E-6', true],
    ['0.0', '', true],
    ['abc', 'abc', false],
    ['abc', '0', true],
  ])('%s vs %s -> %s', (a, b, expected) => {
    expect(pricesDiffer(a, b)).toBe(expected);
  });

  it('counts a unit with no name, as typed into the JSON editor, as a difference', () => {
    const edited = copy(DEFAULTS);
    edited.pricing[0].units.push({ price: '1' } as unknown as PricingTableLike['pricing'][0]['units'][0]);
    expect(hasPricingCustomizations(edited, DEFAULTS)).toBe(true);
  });

  it('does not throw on a null unit typed into the JSON editor', () => {
    const edited = copy(DEFAULTS);
    edited.pricing[0].units.push(null as unknown as PricingTableLike['pricing'][0]['units'][0]);
    expect(hasPricingCustomizations(edited, DEFAULTS)).toBe(false);
  });

  it('treats missing tables as no customisation', () => {
    expect(hasPricingCustomizations(null, DEFAULTS)).toBe(false);
    expect(hasPricingCustomizations(DEFAULTS, null)).toBe(false);
  });
});
