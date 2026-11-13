// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The one form a price may take, and its value.
 *
 * The cost reports read a stored price with Python's `float()` and this UI read it
 * with JavaScript's `Number()`, and the two accept different strings: `Number()`
 * takes `0x10` (16), `0b1`, `Infinity` and reads `''` as 0; `float()` takes `1_000`,
 * `nan` and `inf`. A price in one of those forms was priced on one side and
 * unpriced on the other. The backend now refuses to save anything but a
 * non-negative decimal with an optional exponent (`PRICE_PATTERN` and `parse_price`
 * in `lib/idp_common_pkg/idp_common/config/models.py`), and this module is the same
 * rule on this side: the Pricing page refuses the same values inline, and the cost
 * panels treat a price that is not valid as unpriced, as the reports do.
 */

import type { PricingLookup } from '../document-panel/pricing';

/** Must stay identical to `PRICE_PATTERN` in `idp_common/config/models.py`. */
export const PRICE_PATTERN = /^(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$/;

/**
 * A valid price's value, or `null`: the rule a price is SAVED by.
 *
 * A number is read as the string the backend will store for it: an unquoted
 * `price: 6.0e-8` in an imported YAML file arrives here as a number, and the
 * backend stringifies it before checking. `NaN`, `Infinity` and negatives are
 * refused by the pattern, and a match that overflows to infinity (`1e999`) by the
 * finiteness check. Anything else (null, a boolean, surrounding whitespace) is not
 * a price.
 */
export const parsePrice = (value: unknown): number | null => {
  let text: string;
  if (typeof value === 'string') text = value;
  else if (typeof value === 'number') text = String(value);
  else return null;
  if (!PRICE_PATTERN.test(text)) return null;
  const parsed = Number(text);
  return Number.isFinite(parsed) ? parsed : null;
};

// The characters both earlier readers ignored around a number: the intersection
// of what `Number()` and Python's `float()` skip, which is ASCII whitespace plus
// eighteen space separators (U+00A0, as pasted from a web page, among them). Not
// `trim()`, which also strips U+FEFF, which `float()` does not skip. Must stay the
// same list as `PRICE_WHITESPACE` in `idp_common/config/models.py`; the shared
// `__tests__/price-cases.json` pins both readers to one table of cases.
const PRICE_WHITESPACE = ' \t\n\r\f\v\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000';
const SURROUNDING_PRICE_WHITESPACE = new RegExp(`^[${PRICE_WHITESPACE}]+|[${PRICE_WHITESPACE}]+$`, 'g');

/**
 * A stored price's value, or `null` when it is unpriced: the rule a price is READ by.
 *
 * The save rule with surrounding `PRICE_WHITESPACE` tolerated. The Pricing page saved
 * a table cell exactly as typed before prices were checked, so a stored `'0.0015 '`
 * exists; `Number()` and `float()` both read it as 0.0015, and so does this. It is
 * still refused on save. Mirrors `read_price` in `idp_common/config/models.py`.
 */
export const readPrice = (value: unknown): number | null =>
  parsePrice(typeof value === 'string' ? value.replace(SURROUNDING_PRICE_WHITESPACE, '') : value);

/** The pricing as a lookup by service and unit, for the document cost panels. */
export const toPricingLookup = (pricing: unknown): PricingLookup | null => {
  const rows = (pricing as { pricing?: unknown } | null | undefined)?.pricing;
  if (!Array.isArray(rows)) return null;
  const lookup: PricingLookup = {};
  for (const item of rows as (EntryShape | null)[]) {
    if (!item || typeof item.name !== 'string' || !item.name || !Array.isArray(item.units)) continue;
    const units: Record<string, number> = {};
    for (const unit of item.units as (UnitShape | null)[]) {
      if (!unit || typeof unit.name !== 'string' || !unit.name) continue;
      // Not valued the way the cost reports value it: left out, so it is unpriced.
      const price = readPrice(unit.price);
      if (price !== null) units[unit.name] = price;
    }
    lookup[item.name] = units;
  }
  return lookup;
};

export interface InvalidPrice {
  apiName: string;
  unitName: string;
  price: unknown;
}

interface UnitShape {
  name?: unknown;
  price?: unknown;
}

interface EntryShape {
  name?: unknown;
  units?: unknown;
}

/** Every unit in a pricing table whose price the backend would refuse to save. */
export const findInvalidPrices = (table: unknown): InvalidPrice[] => {
  const rows = (table as { pricing?: unknown } | null | undefined)?.pricing;
  if (!Array.isArray(rows)) return [];
  const invalid: InvalidPrice[] = [];
  for (const entry of rows as (EntryShape | null)[]) {
    if (!entry || !Array.isArray(entry.units)) continue;
    for (const unit of entry.units as (UnitShape | null)[]) {
      if (!unit) continue;
      if (parsePrice(unit.price) === null) {
        invalid.push({ apiName: String(entry.name ?? ''), unitName: String(unit.name ?? ''), price: unit.price });
      }
    }
  }
  return invalid;
};
