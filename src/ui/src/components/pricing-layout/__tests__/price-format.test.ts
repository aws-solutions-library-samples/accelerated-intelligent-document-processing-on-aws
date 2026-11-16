// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The price rule on this side: the same strings the backend refuses (#1903).
 *
 * Every refused form is one `Number()` or Python's `float()` (the cost reports)
 * accepts, so each case is a disagreement between the two readers rather than an
 * arbitrary string. The Python suite
 * `lib/idp_common_pkg/tests/unit/config/test_pricing_price_validation.py` carries
 * the same table and asserts the two `PRICE_PATTERN` sources are identical.
 */

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import yaml from 'js-yaml';
import { describe, expect, it } from 'vitest';

import { findInvalidPrices, parsePrice, readPrice } from '../price-format';
import { pricesDiffer } from '../pricing-customizations';
import sharedCases from './price-cases.json';
import { toPricingLookup } from '../../document-panel/EstimatedCostModal';

const PRICING_YAML = resolve(__dirname, '../../../../../../config_library/pricing.yaml');

describe('parsePrice', () => {
  it.each([
    ['0.0015', 0.0015],
    ['6.0E-8', 6.0e-8],
    ['6.0e-8', 6.0e-8],
    ['2.5E+1', 25],
    ['0', 0],
    ['.5', 0.5],
    ['5.', 5],
    ['1e-400', 0],
  ])('%s is a price worth %s', (text, value) => {
    expect(parsePrice(text)).toBe(value);
  });

  it.each([
    ['0x10', 'Number() reads 16'],
    ['0b1', 'Number() reads 1'],
    ['0o7', 'Number() reads 7'],
    ['Infinity', 'Number() reads Infinity'],
    ['inf', 'float() reads inf'],
    ['nan', 'float() reads NaN'],
    ['1_000', 'float() reads 1000'],
    ['٣', 'float() reads an Arabic-Indic digit'],
    ['', 'Number() reads 0'],
    [' 0.5', 'both trim'],
    ['-1', 'negative'],
    ['+1', 'signed'],
    ['1e999', 'infinite'],
  ])('%s is not a price (%s)', (text) => {
    expect(parsePrice(text)).toBeNull();
  });

  it('reads a number as the string the backend stores for it', () => {
    expect(parsePrice(6e-8)).toBe(6e-8);
    expect(parsePrice(Number.POSITIVE_INFINITY)).toBeNull();
    expect(parsePrice(Number.NaN)).toBeNull();
    expect(parsePrice(-1)).toBeNull();
    expect(parsePrice(null)).toBeNull();
    expect(parsePrice(true)).toBeNull();
  });

  it('accepts every shipped price', () => {
    const raw = yaml.load(readFileSync(PRICING_YAML, 'utf-8')) as { pricing: { units: { price: unknown }[] }[] };
    const prices = raw.pricing.flatMap((e) => e.units.map((u) => u.price));
    expect(prices.length).toBeGreaterThan(300);
    expect(findInvalidPrices(raw)).toEqual([]);
  });
});

describe('readPrice', () => {
  it.each([
    [' 0.0015', 0.0015],
    ['0.0015 ', 0.0015],
    ['\t6.0E-8\n', 6.0e-8],
    ['0.5', 0.5],
  ])('%j is read as %s: both old readers trim ASCII whitespace', (text, value) => {
    expect(readPrice(text)).toBe(value);
  });

  it.each(['-1', '+1', '0x10', ' ', ''])('%j is unpriced', (text) => {
    expect(readPrice(text)).toBeNull();
  });

  it('still refuses surrounding whitespace on save', () => {
    expect(parsePrice(' 0.0015')).toBeNull();
  });
});

interface SharedCase {
  input: string;
  save: number | null;
  read: number | null;
  why: string;
}

const CASES = (sharedCases as { cases: SharedCase[] }).cases;

describe('the shared price case table', () => {
  // The same table the Python suite reads, so the two readers cannot drift apart.
  it('is not empty', () => {
    expect(CASES.length).toBeGreaterThan(50);
    expect(CASES.some((c) => c.read !== null && c.save === null)).toBe(true);
  });

  it.each(CASES.map((c) => [JSON.stringify(c.input), c] as const))('%s follows both rules', (_label, c) => {
    expect(parsePrice(c.input)).toBe(c.save);
    expect(readPrice(c.input)).toBe(c.read);
  });

  it.each(CASES.filter((c) => c.read !== null).map((c) => [JSON.stringify(c.input), c] as const))(
    '%s is valued the same by Number(), the reader the UI used before',
    (_label, c) => {
      expect(Number(c.input)).toBe(c.read);
    },
  );
});

describe('pricesDiffer and an invalid price', () => {
  it('compares a value that is not a price as text, so 0x10 differs from 16', () => {
    expect(pricesDiffer('0x10', '16')).toBe(true);
    expect(pricesDiffer('Infinity', 'Infinity')).toBe(false);
  });
});

describe('findInvalidPrices', () => {
  it('names an invalid price on a unit with no name, which the backend also refuses', () => {
    expect(findInvalidPrices({ pricing: [{ name: 'bedrock/m', units: [{ price: '0x10' }] }] })).toEqual([
      { apiName: 'bedrock/m', unitName: '', price: '0x10' },
    ]);
  });

  it('names every unit whose price the backend would refuse', () => {
    const table = {
      pricing: [
        {
          name: 'bedrock/m',
          units: [
            { name: 'inputTokens', price: '0x10' },
            { name: 'outputTokens', price: '2.0E-6' },
          ],
        },
        { name: 'textract/a', units: [{ name: 'pages', price: 'Infinity' }] },
      ],
    };
    expect(findInvalidPrices(table)).toEqual([
      { apiName: 'bedrock/m', unitName: 'inputTokens', price: '0x10' },
      { apiName: 'textract/a', unitName: 'pages', price: 'Infinity' },
    ]);
  });

  it('does not throw on a malformed table typed into the JSON editor', () => {
    expect(findInvalidPrices(null)).toEqual([]);
    expect(findInvalidPrices({ pricing: 'x' })).toEqual([]);
    expect(findInvalidPrices({ pricing: [null, { units: [null] }] })).toEqual([]);
  });
});

describe('the cost panel price lookup', () => {
  it('keeps a price of zero, which every shipped free unit uses', () => {
    expect(toPricingLookup({ pricing: [{ name: 'bedrock/m', units: [{ name: 'cacheReadInputTokens', price: '0.0' }] }] })).toEqual({
      'bedrock/m': { cacheReadInputTokens: 0 },
    });
  });

  it('values a stored price with surrounding spaces, as the reports do', () => {
    expect(toPricingLookup({ pricing: [{ name: 'textract/a', units: [{ name: 'pages', price: ' 0.0015 ' }] }] })).toEqual({
      'textract/a': { pages: 0.0015 },
    });
  });

  it('leaves a price the reports cannot read unpriced rather than valuing it', () => {
    const lookup = toPricingLookup({
      pricing: [
        {
          name: 'bedrock/m',
          units: [
            { name: 'inputTokens', price: '0x10' },
            { name: 'outputTokens', price: '2.0E-6' },
          ],
        },
      ],
    });
    expect(lookup).toEqual({ 'bedrock/m': { outputTokens: 2.0e-6 } });
  });
});
