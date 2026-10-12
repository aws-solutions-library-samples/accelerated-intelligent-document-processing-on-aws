// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { describe, it, expect } from 'vitest';

import { calculateTotalCosts, meteringCount, priceMetering } from '../metering-cost';
import type { PricingLookup } from '../pricing';

const SONNET = 'us.anthropic.claude-sonnet-4-5-20250929-v1:0';
const NOVA = 'us.amazon.nova-2-lite-v1:0';

const PRICING: PricingLookup = {
  [`bedrock/${SONNET}`]: {
    inputTokens: 3e-6,
    outputTokens: 1.5e-5,
    cacheReadInputTokens: 3e-7,
    cacheWriteInputTokens: 3.75e-6,
  },
  [`bedrock/${NOVA}`]: {
    inputTokens: 3e-7,
    outputTokens: 2.5e-6,
  },
  'textract/detect_document_text': {
    pages: 0.0015,
  },
};

const STORED_WITH_CACHE_DETAILS: Record<string, Record<string, unknown>> = {
  'OCR/textract/detect_document_text': { pages: 4 },
  [`Extraction/bedrock/${SONNET}`]: {
    inputTokens: 40000,
    outputTokens: 4000,
    totalTokens: 44000,
    cacheReadInputTokens: 30000,
    cacheWriteInputTokens: 8000,
    cacheDetails: [{ ttl: '5m', inputTokens: 8000 }],
    requests: 4,
  },
  [`Extraction/bedrock/${NOVA}`]: {
    inputTokens: 10000,
    outputTokens: 600,
    requests: 1,
  },
};

const EXPECTED_ROW_COSTS: Array<[string, string, number]> = [
  ['OCR', 'pages', 0.006],
  ['Extraction', 'inputTokens', 0.12],
  ['Extraction', 'outputTokens', 0.06],
  ['Extraction', 'totalTokens', 0],
  ['Extraction', 'cacheReadInputTokens', 0.009],
  ['Extraction', 'cacheWriteInputTokens', 0.03],
  ['Extraction', 'requests', 0],
  ['Extraction', 'inputTokens', 0.003],
  ['Extraction', 'outputTokens', 0.0015],
  ['Extraction', 'requests', 0],
];

describe('meteringCount', () => {
  it('reads a finite number, or a string holding one, as a count', () => {
    expect(meteringCount(0)).toBe(0);
    expect(meteringCount(4)).toBe(4);
    expect(meteringCount(0.5)).toBe(0.5);
    expect(meteringCount('1200')).toBe(1200);
    expect(meteringCount(' 7 ')).toBe(7);
  });

  it('reads nothing else as a count', () => {
    for (const notACount of [
      [{ ttl: '5m', inputTokens: 8000 }],
      [8000],
      [],
      { ttl: '5m', inputTokens: 8000 },
      {},
      true,
      false,
      null,
      undefined,
      '',
      '   ',
      'n/a',
      Number.NaN,
      Number.POSITIVE_INFINITY,
      Number.NEGATIVE_INFINITY,
    ]) {
      expect(meteringCount(notACount)).toBeNull();
    }
  });
});

describe('priceMetering', () => {
  it('gives a structured member such as cacheDetails no row', () => {
    const { rows } = priceMetering(STORED_WITH_CACHE_DETAILS, PRICING);
    expect(rows.map((row) => row.unit)).not.toContain('cacheDetails');
    expect(rows.map((row) => [row.context, row.unit])).toEqual(EXPECTED_ROW_COSTS.map(([context, unit]) => [context, unit]));
  });

  it('keeps every figure finite when a stored member is not a count', () => {
    const { rows, contextTotals, totalCost } = priceMetering(STORED_WITH_CACHE_DETAILS, PRICING);
    for (const row of rows) {
      expect(Number.isFinite(row.value)).toBe(true);
      expect(Number.isFinite(row.cost)).toBe(true);
      expect(row.unitPrice === null || Number.isFinite(row.unitPrice)).toBe(true);
    }
    expect(Object.values(contextTotals).every(Number.isFinite)).toBe(true);
    expect(Number.isFinite(totalCost)).toBe(true);
  });

  it('prices each numeric member at its own rate', () => {
    const { rows } = priceMetering(STORED_WITH_CACHE_DETAILS, PRICING);
    expect(rows).toHaveLength(EXPECTED_ROW_COSTS.length);
    rows.forEach((row, index) => {
      expect(row.cost).toBeCloseTo(EXPECTED_ROW_COSTS[index][2], 12);
    });
  });

  it('sums every numeric row of a context into its subtotal, including the rows before a member that is not a count', () => {
    const { contextTotals } = priceMetering(STORED_WITH_CACHE_DETAILS, PRICING);
    expect(contextTotals.OCR).toBeCloseTo(0.006, 12);
    expect(contextTotals.Extraction).toBeCloseTo(0.2235, 12);
    expect(contextTotals.Extraction).not.toBeCloseTo(0.0045, 6);
  });

  it('totals the subtotals', () => {
    const { contextTotals, totalCost } = priceMetering(STORED_WITH_CACHE_DETAILS, PRICING);
    expect(totalCost).toBeCloseTo(0.2295, 12);
    expect(totalCost).toBeCloseTo(contextTotals.OCR + contextTotals.Extraction, 12);
  });

  it('keeps a member no pricing entry covers as an unpriced row that adds nothing', () => {
    const { rows, contextTotals, totalCost } = priceMetering(
      { 'Summarization/bedrock/not.in.the.table': { inputTokens: 500 }, 'OCR/textract/detect_document_text': { pages: 2 } },
      PRICING,
    );
    expect(rows).toEqual([
      { context: 'Summarization', serviceApi: 'bedrock/not.in.the.table', unit: 'inputTokens', value: 500, unitPrice: null, cost: 0 },
      { context: 'OCR', serviceApi: 'textract/detect_document_text', unit: 'pages', value: 2, unitPrice: 0.0015, cost: 0.003 },
    ]);
    expect(contextTotals).toEqual({ OCR: 0.003 });
    expect(totalCost).toBe(0.003);
  });

  it('treats a unit price that is not a finite number as unpriced', () => {
    const { rows, totalCost } = priceMetering(
      { 'OCR/textract/analyze_document': { pages: 3 }, 'OCR/textract/analyze_expense': { pages: 3 } },
      { 'textract/analyze_document': { pages: Number.NaN }, 'textract/analyze_expense': { pages: Number.POSITIVE_INFINITY } },
    );
    expect(rows.map((row) => row.unitPrice)).toEqual([null, null]);
    expect(totalCost).toBe(0);
  });

  it('skips an entry that is not a map of unit to count', () => {
    const { rows, totalCost } = priceMetering(
      {
        'OCR/textract/a': null,
        'OCR/textract/b': 12,
        'OCR/textract/c': 'pages',
        'OCR/textract/d': [4],
        'OCR/textract/detect_document_text': { pages: 2 },
      },
      PRICING,
    );
    expect(rows.map((row) => row.serviceApi)).toEqual(['textract/detect_document_text']);
    expect(totalCost).toBeCloseTo(0.003, 12);
  });

  it('reads a key with fewer than three parts as having no context', () => {
    const { rows } = priceMetering({ 'textract/detect_document_text': { pages: 1 } }, PRICING);
    expect(rows).toEqual([
      { context: '', serviceApi: 'textract/detect_document_text', unit: 'pages', value: 1, unitPrice: 0.0015, cost: 0.0015 },
    ]);
  });
});

describe('calculateTotalCosts', () => {
  it('reports the table total and divides it by the page count', () => {
    const { totalCost, costPerPage } = calculateTotalCosts(STORED_WITH_CACHE_DETAILS, 4, PRICING);
    expect(totalCost).toBeCloseTo(0.2295, 12);
    expect(totalCost).toBe(priceMetering(STORED_WITH_CACHE_DETAILS, PRICING).totalCost);
    expect(costPerPage).toBeCloseTo(0.057375, 12);
  });

  it('is zero without metering or pricing, and divides by one without a page count', () => {
    expect(calculateTotalCosts(null, 4, PRICING)).toEqual({ totalCost: 0, costPerPage: 0 });
    expect(calculateTotalCosts(STORED_WITH_CACHE_DETAILS, 4, null)).toEqual({ totalCost: 0, costPerPage: 0 });
    expect(calculateTotalCosts(STORED_WITH_CACHE_DETAILS, undefined, PRICING).costPerPage).toBeCloseTo(0.2295, 12);
    expect(calculateTotalCosts(STORED_WITH_CACHE_DETAILS, 0, PRICING).costPerPage).toBeCloseTo(0.2295, 12);
  });
});
