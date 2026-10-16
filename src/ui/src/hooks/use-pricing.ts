// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useState, useEffect } from 'react';
import { generateClient } from '../api/client-shim';
import { ConsoleLogger } from 'aws-amplify/utils';
import { getPricing, updatePricing as updatePricingOp, restoreDefaultPricing as restoreDefaultPricingOp } from '../graphql/generated';
import { parseNonChargeableUnits, parsePricingData } from '../graphql/awsjson-parsers';
import type { NonChargeableUnits } from '../graphql/awsjson-types';

interface UsePricingReturn {
  pricing: unknown;
  defaultPricing: unknown;
  /**
   * Per pricing key, the metering units `idp_common.metering_units` declares
   * free, as this deployment's resolver reports them.
   *
   * The document cost table needs these to tell a unit that is free from one
   * nobody has priced; it cannot import the declarations and must not guess.
   * `{}` means none reached us — an older resolver, or a fetch that failed — and
   * resolves an omitted unit to unpriced rather than to $0.00. See
   * `components/document-panel/pricing.ts`.
   */
  nonChargeableUnits: NonChargeableUnits;
  loading: boolean;
  refreshing: boolean;
  error: string | null;
  fetchPricing: (silent?: boolean) => Promise<void>;
  updatePricing: (newPricing: unknown) => Promise<boolean>;
  restoreDefaultPricing: () => Promise<boolean>;
}

const client = generateClient();
const logger = new ConsoleLogger('usePricing');

const usePricing = (): UsePricingReturn => {
  const [pricing, setPricing] = useState<unknown>(null);
  const [nonChargeableUnits, setNonChargeableUnits] = useState<NonChargeableUnits>({});
  const [defaultPricing, setDefaultPricing] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchPricing = async (silent: boolean = false): Promise<void> => {
    if (silent) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }
    setError(null);

    try {
      logger.debug('Fetching pricing...');
      const result = await client.graphql({ query: getPricing });
      logger.debug('API response:', result);

      const response = result.data.getPricing;

      if (!response?.success) {
        const errorMsg = response?.error?.message || 'Failed to load pricing';
        throw new Error(errorMsg);
      }

      // pricing and defaultPricing come as AWSJSON (JSON strings) - parse with typed parser
      const pricingData = parsePricingData(response.pricing as string);
      const defaultPricingData = parsePricingData(response.defaultPricing as string);

      if (pricingData == null || defaultPricingData == null) {
        throw new Error('Failed to parse pricing data from server response');
      }

      logger.debug('Parsed pricing:', pricingData);
      logger.debug('Parsed default pricing:', defaultPricingData);
      setPricing(pricingData);
      setDefaultPricing(defaultPricingData);
      setNonChargeableUnits(parseNonChargeableUnits(response.nonChargeableUnits));
    } catch (err: unknown) {
      logger.error('Error fetching pricing', err);
      const message = err instanceof Error ? err.message : String(err);
      setError(`Failed to load pricing: ${message}`);
    } finally {
      if (silent) {
        setRefreshing(false);
      } else {
        setLoading(false);
      }
    }
  };

  const updatePricing = async (newPricing: unknown): Promise<boolean> => {
    setError(null);
    try {
      logger.debug('Updating pricing with:', newPricing);

      // Send the entire pricing object as AWSJSON (stringify if not already a string)
      const pricingConfig = typeof newPricing === 'string' ? newPricing : JSON.stringify(newPricing);

      logger.debug('Sending pricingConfig:', pricingConfig);

      const result = await client.graphql({
        query: updatePricingOp,
        variables: { pricingConfig },
      });

      const response = result.data.updatePricing;

      if (!response?.success) {
        const errorMsg = response?.error?.message || 'Failed to update pricing';
        throw new Error(errorMsg);
      }

      // Refetch silently to ensure backend and frontend are in sync
      await fetchPricing(true);

      return true;
    } catch (err: unknown) {
      logger.error('Error updating pricing', err);
      const message = err instanceof Error ? err.message : String(err);
      setError(`Failed to update pricing: ${message}`);
      return false;
    }
  };

  const restoreDefaultPricing = async (): Promise<boolean> => {
    setError(null);
    try {
      logger.debug('Restoring default pricing...');

      const result = await client.graphql({
        query: restoreDefaultPricingOp,
      });

      const response = result.data.restoreDefaultPricing;

      if (!response?.success) {
        const errorMsg = response?.error?.message || 'Failed to restore default pricing';
        throw new Error(errorMsg);
      }

      // Refetch to get the restored defaults
      await fetchPricing(true);

      return true;
    } catch (err: unknown) {
      logger.error('Error restoring default pricing', err);
      const message = err instanceof Error ? err.message : String(err);
      setError(`Failed to restore default pricing: ${message}`);
      return false;
    }
  };

  useEffect(() => {
    fetchPricing();
  }, []);

  return {
    pricing,
    defaultPricing,
    nonChargeableUnits,
    loading,
    refreshing,
    error,
    fetchPricing,
    updatePricing,
    restoreDefaultPricing,
  };
};

export default usePricing;
