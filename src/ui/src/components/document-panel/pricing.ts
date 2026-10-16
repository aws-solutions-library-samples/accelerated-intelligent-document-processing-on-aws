// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Pricing lookup for the document cost table.
 *
 * The cost this panel shows and the cost written to the `metering` reporting
 * table are not allowed to disagree, and the two reach that agreement by the
 * panel not deciding anything the backend has already decided. It resolves a
 * metering key to a pricing entry the same way the backend does — exact key,
 * longest `/`-delimited suffix wins — and for the one genuinely contestable case,
 * a matched entry that does not list the metered unit, it reads the backend's
 * verdict out of `nonChargeableUnits` rather than forming its own.
 *
 * That field comes from the `getPricing` response, where the resolver builds it
 * from `idp_common.metering_units.declared_free_units`: the single place the two
 * free-unit registers and their differing scopes are known. The browser cannot
 * import them, and a hand-maintained TypeScript copy of the rule is what made
 * this file answer `0` for every omitted unit — so a row the reporting table
 * recorded as an explicit NULL, meaning nobody has priced it, was displayed here
 * as a confident $0.00 contribution (GitHub issues #1212, #1206, #1406).
 *
 * It lives in its own module, rather than inside DocumentPanel.tsx, so it can be
 * unit-tested without mounting the component.
 */

import type { NonChargeableUnits } from '../../graphql/awsjson-types';

export type { NonChargeableUnits };

export interface PricingLookup {
  [serviceName: string]: {
    [unitName: string]: number;
  };
}

/**
 * What `lookupUnitPrice` needs beyond the pricing table to answer the
 * absent-unit case, and the direction each default leans.
 *
 * Both are optional and both default to the STRICT answer, which is the point:
 * a caller that forgets one gets 'unpriced' for an omitted unit, never 'free'.
 * The convenient default is the bug this file had.
 *
 * - `count` — the metered count for this row. A count of 0 contributes 0 at any
 *   rate, so it is free by arithmetic rather than by anybody's declaration; this
 *   is the one branch of the backend's rule the panel applies itself, because it
 *   is about a number only the caller holds. It mirrors the `value` parameter of
 *   `_get_unit_cost`, and like that parameter it defaults to a non-zero value so
 *   that an omitted argument gets the strict answer.
 * - `nonChargeableUnits` — the backend's declarations, keyed by pricing key.
 *   Absent means no declarations reached the browser (an older deployment's
 *   resolver, a pricing fetch that failed), and an omitted unit then reads as
 *   unpriced.
 */
export interface UnitPriceContext {
  count?: number;
  nonChargeableUnits?: NonChargeableUnits;
}

/**
 * Resolve the unit price for a `serviceApi`/`unit` pair.
 *
 * Resolution is by EXACT pricing key: `serviceApi` is tried first, then
 * progressively shorter `/`-delimited suffixes of it, and the longest match
 * wins. Within a matched entry the unit name must match exactly.
 *
 * Returns:
 * - the price, when the matched entry lists the unit;
 * - `0`, when an entry matches, does not list the unit, and either the backend
 *   declares that unit non-chargeable for that entry or the metered count is 0;
 * - `null` — unpriced, rendered 'None'/'N/A' and left out of every total — when
 *   an entry matches, does not list the unit, and nothing above makes it free.
 *   This is the same answer `_get_unit_cost` gives, where it is recorded as a SQL
 *   NULL with a warning naming the entry;
 * - `null`, when no entry matches at all. A related entry's price is never
 *   substituted.
 *
 * There is deliberately NO substring matching. This function used to match
 * case-insensitively and bidirectionally on both the service key and the unit
 * name, so `cacheReadInputTokens` bound to the matched entry's `inputTokens`
 * price — the first unit every Bedrock row lists. Cache reads cost about a tenth
 * of fresh input, so they were displayed at up to ~8x their real rate, while
 * `cacheWriteInputTokens` was understated by ~20%. See GitHub issue #926 and
 * PR #952.
 *
 * Lambda-hook rows resolve because the metering key carries the bare function
 * name (`lambda_hook/GENAIIDP-mistral-ocr-hook`) rather than the configured ARN.
 * An ARN delimits the function name with `:`, which this walk cannot split, so
 * an ARN-keyed metering row would be unpriceable. See
 * `bedrock.client.lambda_hook_metering_name`.
 */
export const lookupUnitPrice = (
  pricingData: PricingLookup,
  serviceApi: string,
  unit: string,
  context: UnitPriceContext = {},
): number | null => {
  const { count = 1, nonChargeableUnits = {} } = context;
  const parts = serviceApi.split('/');
  for (let start = 0; start < parts.length; start += 1) {
    const candidate = parts.slice(start).join('/');
    const serviceCosts = pricingData[candidate];
    if (serviceCosts !== undefined && serviceCosts !== null) {
      const price = serviceCosts[unit];
      if (price !== undefined) return Number(price);

      // The entry matched but lists no rate for this unit. Whose question that is
      // matters more than the answer: the backend has one rule for it, sourced to
      // AWS's published price list per service and per model, and this panel is a
      // reader of that rule's output. Anything it cannot read there is unpriced.
      const declaredFree = nonChargeableUnits[candidate];
      if (Array.isArray(declaredFree) && declaredFree.includes(unit)) return 0;

      // Arithmetic, not a judgement: nothing metered costs nothing at any rate.
      if (count === 0) return 0;

      return null;
    }
  }

  return null;
};
