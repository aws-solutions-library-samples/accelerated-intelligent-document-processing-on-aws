// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The figures in the document cost table: one priced row per metering member, a
 * subtotal per context, and the document total the panel header divides by the
 * page count.
 *
 * Only a member holding a finite number is metering, the rule the backend has
 * applied since #852 (`bedrock.client.numeric_usage`). Documents processed before
 * it can still store Bedrock's `cacheDetails`, a list of per-TTL cache-write
 * breakdowns whose tokens are also metered as `cacheWriteInputTokens`; it gets no
 * row and reaches no figure, where it used to turn the total into $NaN.
 *
 * Kept out of DocumentPanel.tsx so the table and its header share one computation
 * that can be unit-tested without mounting the component.
 */

import { ConsoleLogger } from 'aws-amplify/utils';

import { lookupUnitPrice } from './pricing';
import type { PricingLookup } from './pricing';

const logger = new ConsoleLogger('metering-cost');

export interface PricedMeteringRow {
  context: string;
  serviceApi: string;
  unit: string;
  value: number;
  unitPrice: number | null;
  cost: number;
}

export interface PricedMetering {
  rows: PricedMeteringRow[];
  contextTotals: Record<string, number>;
  totalCost: number;
}

/**
 * The count a stored metering member holds, or `null` when it holds none. A
 * numeric string counts, as it does for the backend readers of stored metering;
 * a boolean, a list, an object or a non-finite number does not.
 */
export const meteringCount = (value: unknown): number | null => {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
};

/** Split a `<context>/<service>/<api>` metering key; a key of fewer than three parts has no context. */
const parseServiceApiKey = (serviceApiKey: string): { context: string; serviceApi: string } => {
  const parts = serviceApiKey.split('/');
  if (parts.length >= 3) {
    const context = parts[0];
    const serviceApi = parts.slice(1).join('/');
    return { context, serviceApi };
  }
  return { context: '', serviceApi: serviceApiKey };
};

/**
 * Price every member of a metering map that holds a count.
 *
 * `unitPrice` follows `lookupUnitPrice`: `null` when no pricing entry covers the
 * member (rendered 'None' / 'N/A', and left out of every total), `0` when its
 * entry does not list the unit (metered but not chargeable). An entry that is not
 * a map of unit to count is skipped.
 */
export const priceMetering = (meteringData: Record<string, unknown>, pricingData: PricingLookup): PricedMetering => {
  const rows: PricedMeteringRow[] = [];
  const contextTotals: Record<string, number> = {};
  let totalCost = 0;

  Object.entries(meteringData).forEach(([originalServiceApiKey, metrics]) => {
    if (!metrics || typeof metrics !== 'object' || Array.isArray(metrics)) return;
    const { context, serviceApi } = parseServiceApiKey(originalServiceApiKey);

    Object.entries(metrics as Record<string, unknown>).forEach(([unit, member]) => {
      const value = meteringCount(member);
      if (value === null) return;

      const price = lookupUnitPrice(pricingData, serviceApi, unit);
      const unitPrice = price !== null && Number.isFinite(price) ? price : null;
      let cost = 0;
      if (unitPrice !== null) {
        cost = value * unitPrice;
        totalCost += cost;
        contextTotals[context] = (contextTotals[context] ?? 0) + cost;
        logger.debug(`Found price for ${serviceApi}/${unit}: $${unitPrice}`);
      } else {
        logger.debug(`No price found for ${serviceApi}/${unit}, using None`);
      }

      rows.push({ context, serviceApi, unit, value, unitPrice, cost });
    });
  });

  return { rows, contextTotals, totalCost };
};

/** The document total and its cost per page, from the same rows as the table. */
export const calculateTotalCosts = (
  meteringData: Record<string, unknown> | null,
  pageCount: number | undefined,
  pricingData: PricingLookup | null,
): { totalCost: number; costPerPage: number } => {
  if (!meteringData) return { totalCost: 0, costPerPage: 0 };

  const totalCost = pricingData ? priceMetering(meteringData, pricingData).totalCost : 0;
  const numPages = pageCount || 1;

  return { totalCost, costPerPage: totalCost / numPages };
};
