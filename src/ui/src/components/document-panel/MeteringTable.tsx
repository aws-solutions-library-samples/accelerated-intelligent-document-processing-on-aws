// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React, { useEffect, useState } from 'react';
import { Box, StatusIndicator, Table } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';

import usePricing from '../../hooks/use-pricing';
import { describePromptCache, summarizeCacheUsage } from '../common/promptCacheModel';
import type { PromptCacheDescription } from '../common/promptCacheModel';
import { priceMetering } from './metering-cost';
import type { PricingLookup } from './pricing';

const logger = new ConsoleLogger('MeteringTable');

interface MeteringRowItem {
  context: string;
  serviceApi: string;
  unit: string;
  value: string;
  unitCost: string;
  cost: string;
  costValue: number;
  isTotal: boolean;
  isSubtotal: boolean;
  note?: string;
  // Per-phase prompt-cache verdict, shown on the phase's subtotal row (#780).
  cacheNote?: PromptCacheDescription;
}

interface PricingUnit {
  name: string;
  price: number;
}

interface PricingItem {
  name: string;
  units: PricingUnit[];
}

export interface PricingData {
  pricing: PricingItem[];
}

interface MeteringTableProps {
  meteringData: Record<string, Record<string, unknown>> | null;
  preCalculatedTotals?: { totalCost: number; costPerPage: number };
}

// Helper function to format cost cells
const formatCostCell = (rowItem: MeteringRowItem): React.JSX.Element | string => {
  if (rowItem.isTotal) {
    return <Box fontWeight="bold">{`${rowItem.note}: ${rowItem.cost}`}</Box>;
  }
  if (rowItem.isSubtotal) {
    return <Box fontWeight="bold" color="text-body-secondary">{`${rowItem.note}: ${rowItem.cost}`}</Box>;
  }
  return rowItem.cost;
};

// Component to display metering information in a table
// Floors for the metering table's short-valued columns, in px. With none, a narrow
// window shrank them below their own one-word contents and Cloudscape wrapped inside
// the word ("Contex t", "Classifi cation", "gb_second s"); the table now scrolls
// horizontally instead. Service/Api, the long column, keeps the remaining width.
const METERING_COLUMN_MIN_WIDTH = { context: 130, unit: 150, value: 100, unitCost: 130, cost: 130 } as const;

const MeteringTable = ({ meteringData, preCalculatedTotals }: MeteringTableProps): React.JSX.Element | null => {
  // Use usePricing hook to get pricing data from the new separate pricing config
  const { pricing, nonChargeableUnits, loading } = usePricing();
  const [pricingData, setPricingData] = useState<PricingLookup>({});
  // We no longer use a default unit cost, showing "None" instead

  useEffect(() => {
    if (pricing && (pricing as PricingData).pricing) {
      // Convert pricing array to lookup object for easier access
      const pricingLookup: PricingLookup = {};
      (pricing as PricingData).pricing.forEach((item) => {
        if (item.name && item.units) {
          pricingLookup[item.name] = {};
          item.units.forEach((unitItem) => {
            if (unitItem.name && unitItem.price !== undefined) {
              // Ensure price is stored as a number
              pricingLookup[item.name][unitItem.name] = Number(unitItem.price);
            }
          });
        }
      });
      setPricingData(pricingLookup);
      logger.debug('Pricing data initialized:', pricingLookup);
    }
  }, [pricing]);

  if (!meteringData) {
    return null;
  }

  if (loading) {
    return <Box>Loading pricing data...</Box>;
  }

  // Transform metering data into table rows with context parsing
  const { rows, contextTotals, totalCost } = priceMetering(meteringData, pricingData, nonChargeableUnits);
  const rawTableItems: MeteringRowItem[] = rows.map(({ context, serviceApi, unit, value, unitPrice, cost }) => ({
    context,
    serviceApi,
    unit,
    value: String(value),
    unitCost: unitPrice !== null ? `$${unitPrice}` : 'None',
    cost: unitPrice !== null ? `$${cost.toFixed(4)}` : 'N/A',
    costValue: cost,
    isTotal: false,
    isSubtotal: false,
  }));

  // Group items by context and add subtotals
  const tableItems: MeteringRowItem[] = [];
  const contextGroups: Record<string, MeteringRowItem[]> = {};

  // Group raw items by context
  rawTableItems.forEach((item) => {
    if (!contextGroups[item.context]) {
      contextGroups[item.context] = [];
    }
    contextGroups[item.context].push(item);
  });

  // Sort contexts in pipeline order. 'Splitting' and 'SectionClassification' are the two
  // halves of the split-then-classify strategy (classification.strategy: split_then_classify) and
  // appear in place of 'Classification', never alongside it — they sit where it
  // sits so the cost table reads in pipeline order either way.
  const contextOrder = ['BDAProject', 'OCR', 'Classification', 'Splitting', 'SectionClassification', 'Extraction', 'Summarization'];
  const sortedContexts = Object.keys(contextGroups).sort((a, b) => {
    const aIndex = contextOrder.indexOf(a);
    const bIndex = contextOrder.indexOf(b);

    // If both contexts are in the predefined order, sort by their position
    if (aIndex !== -1 && bIndex !== -1) {
      return aIndex - bIndex;
    }

    // If only one context is in the predefined order, it comes first
    if (aIndex !== -1) return -1;
    if (bIndex !== -1) return 1;

    // If neither context is in the predefined order, sort alphabetically
    return a.localeCompare(b);
  });

  sortedContexts.forEach((context) => {
    // Add all items for this context
    tableItems.push(...contextGroups[context]);

    // Add subtotal row for this context, carrying the phase's prompt-cache
    // verdict: the cache units are priced above, but a row of numbers does not
    // say whether the cache point did anything (#780).
    const contextTotal = contextTotals[context] || 0;
    const cacheSummary = context ? summarizeCacheUsage(meteringData as Record<string, unknown>, context, { exact: true }) : null;
    tableItems.push({
      context: '',
      serviceApi: '',
      unit: '',
      value: '',
      unitCost: '',
      cost: `$${contextTotal.toFixed(4)}`,
      costValue: contextTotal,
      isTotal: false,
      isSubtotal: true,
      note: `${context} Subtotal`,
      cacheNote:
        cacheSummary && cacheSummary.state !== 'no-cache-data'
          ? describePromptCache(cacheSummary, { phaseOnly: true, context })
          : undefined,
    });
  });

  // Use preCalculatedTotals if provided, otherwise calculate locally
  const finalTotalCost = preCalculatedTotals ? preCalculatedTotals.totalCost : totalCost;

  // Add overall total row
  tableItems.push({
    context: '',
    serviceApi: '',
    unit: '',
    value: '',
    unitCost: '',
    cost: `$${finalTotalCost.toFixed(4)}`,
    costValue: finalTotalCost,
    isTotal: true,
    isSubtotal: false,
    note: 'Total',
  });

  return (
    <Table
      columnDefinitions={[
        {
          id: 'context',
          header: 'Context',
          minWidth: METERING_COLUMN_MIN_WIDTH.context,
          cell: (rowItem: MeteringRowItem) => rowItem.context,
        },
        {
          id: 'serviceApi',
          header: 'Service/Api',
          cell: (rowItem: MeteringRowItem) =>
            rowItem.cacheNote ? (
              <Box fontSize="body-s">
                <StatusIndicator type={rowItem.cacheNote.indicator}>{rowItem.cacheNote.headline}</StatusIndicator>
                <Box fontSize="body-s" color="text-body-secondary">
                  {rowItem.cacheNote.detail}
                </Box>
              </Box>
            ) : (
              rowItem.serviceApi
            ),
        },
        {
          id: 'unit',
          header: 'Unit',
          minWidth: METERING_COLUMN_MIN_WIDTH.unit,
          cell: (rowItem: MeteringRowItem) => rowItem.unit,
        },
        {
          id: 'value',
          header: 'Value',
          minWidth: METERING_COLUMN_MIN_WIDTH.value,
          cell: (rowItem: MeteringRowItem) => rowItem.value,
        },
        {
          id: 'unitCost',
          header: 'Unit Cost',
          minWidth: METERING_COLUMN_MIN_WIDTH.unitCost,
          cell: (rowItem: MeteringRowItem) => rowItem.unitCost,
        },
        {
          id: 'cost',
          header: 'Estimated Cost',
          minWidth: METERING_COLUMN_MIN_WIDTH.cost,
          cell: formatCostCell,
        },
      ]}
      items={tableItems}
      loadingText="Loading resources"
      sortingDisabled
      wrapLines
      stripedRows
      empty={
        <Box textAlign="center" color="inherit">
          <b>No metering data</b>
          <Box padding={{ bottom: 's' }} variant="p" color="inherit">
            No metering data is available for this document.
          </Box>
        </Box>
      }
    />
  );
};

export default MeteringTable;
