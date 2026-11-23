// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Whether the pricing table in the editor differs from the stack's defaults.
 *
 * This decides whether "Restore default (All)" is enabled, so it has to see every
 * difference a restore would undo:
 *
 * - **A changed price, however small.** Prices are per unit — a Bedrock token
 *   rate is around `1e-6` to `1e-5` dollars — so the absolute tolerance of `1e-6`
 *   this replaced treated any change below $1 per million tokens as no change and
 *   left the button disabled. Prices are compared as numbers, exactly: the same
 *   decimal value written two ways (`2.2E-6`, `0.0000022`, `2.2e-06`) parses to
 *   the same double, so exact equality does not report a notation difference.
 * - **A unit added that the defaults lack.**
 * - **A default unit that is no longer in the table.** Deleting a row is a
 *   customisation too, and restoring brings the row back.
 *
 * The table on the page is the effective one: the backend lays the saved table
 * over the defaults per model and unit. So after a reload it holds every default
 * unit, and the deletion case can only be an unsaved edit — a deleted default row
 * comes back once the table is saved and read again.
 */

import { parsePrice } from './price-format';

export interface PricingUnitLike {
  name: string;
  price: string | number;
}

export interface PricingEntryLike {
  name: string;
  units: PricingUnitLike[];
}

export interface PricingTableLike {
  pricing: PricingEntryLike[];
}

// An internal map key, never shown: a separator that cannot occur in a name.
const KEY_SEPARATOR = '\u0000';
const unitKey = (entryName: string, unitName: string): string => [entryName, unitName].join(KEY_SEPARATOR);

const priceMap = (table: PricingTableLike): Map<string, string> => {
  const map = new Map<string, string>();
  for (const entry of table.pricing) {
    // A row with no name is kept, not skipped: it is not in the defaults, so it
    // is a difference, and skipping it would leave the button disabled.
    if (!entry) continue;
    for (const unit of Array.isArray(entry.units) ? entry.units : []) {
      if (!unit) continue;
      map.set(unitKey(String(entry.name), String(unit.name)), String(unit.price ?? ''));
    }
  }
  return map;
};

/** True when two price strings denote different values. */
export const pricesDiffer = (a: string, b: string): boolean => {
  const x = a.trim();
  const y = b.trim();
  // Number('') is 0, so an emptied field would otherwise equal a default of 0.
  if (x === '' || y === '') return x !== y;
  // A value that is not a valid price (`0x10`, `Infinity`) is compared as text:
  // `Number('0x10') === 16` would otherwise read it as unchanged from `16`.
  const nx = parsePrice(x);
  const ny = parsePrice(y);
  if (nx === null || ny === null) return x !== y;
  return nx !== ny;
};

export const hasPricingCustomizations = (current: unknown, defaults: unknown): boolean => {
  const cur = current as PricingTableLike | null | undefined;
  const def = defaults as PricingTableLike | null | undefined;
  if (!cur || !def || !Array.isArray(cur.pricing) || !Array.isArray(def.pricing)) return false;

  const currentPrices = priceMap(cur);
  const defaultPrices = priceMap(def);

  for (const [key, price] of currentPrices) {
    const defaultPrice = defaultPrices.get(key);
    if (defaultPrice === undefined || pricesDiffer(price, defaultPrice)) return true;
  }
  for (const key of defaultPrices.keys()) {
    if (!currentPrices.has(key)) return true;
  }
  return false;
};

/**
 * Looks up a unit's price in the shipped default table, or `undefined` when the
 * defaults have no such unit.
 *
 * This is what tells a default row from a custom one on the Pricing page: a row
 * is a default row when its entry and unit name are in the defaults, whatever its
 * current price, so a default the administrator has overridden is still a default
 * row. A default row cannot usefully be deleted — the backend lays the saved
 * table over the defaults per unit, so the unit comes back after a save and
 * reload — and the price returned here is what "Restore default price" writes.
 * Of two default rows with the same names the last one is used, as the cost
 * readers do.
 */
export const defaultUnitPriceLookup = (defaults: unknown): ((entryName: string, unitName: string) => string | undefined) => {
  const def = defaults as PricingTableLike | null | undefined;
  const prices = def && Array.isArray(def.pricing) ? priceMap(def) : new Map<string, string>();
  return (entryName, unitName) => prices.get(unitKey(String(entryName), String(unitName)));
};
