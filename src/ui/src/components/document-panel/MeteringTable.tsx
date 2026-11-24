// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React, { useEffect, useState } from 'react';
import { Box, StatusIndicator, Table } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';

import usePricing from '../../hooks/use-pricing';
import { describePromptCache, summarizeCacheUsage } from '../common/promptCacheModel';
import type { PromptCacheDescription } from '../common/promptCacheModel';
import { priceMetering } from './metering-cost';
import type { PricingLookup } from './pricing';
import { toPricingLookup } from '../pricing-layout/price-format';
import { formatUnitCostUsd } from '../test-studio/formatCost';
import { useFormatters } from '../../i18n/format';

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

const messages = defineMessages<{
  loadingPricing: NoMessageValues;
  columnContext: NoMessageValues;
  columnServiceApi: NoMessageValues;
  columnUnit: NoMessageValues;
  columnValue: NoMessageValues;
  columnUnitCost: NoMessageValues;
  columnCost: NoMessageValues;
  unpricedUnitCost: NoMessageValues;
  unpricedCost: NoMessageValues;
  subtotal: { readonly context: string };
  total: NoMessageValues;
  costCell: { readonly label: string; readonly cost: string };
}>({
  loadingPricing: {
    id: 'documents.estimatedCost.loadingPricing',
    defaultMessage: 'Loading pricing data...',
    description: "Shown in Reports › Estimated Cost on the document page while the stack's pricing loads.",
  },
  columnContext: {
    id: 'documents.estimatedCost.column.context',
    defaultMessage: 'Context',
    description:
      'Column header of the cost table in Reports › Estimated Cost on the document page: the processing phase a line was metered in, such as Extraction.',
  },
  columnServiceApi: {
    id: 'documents.estimatedCost.column.serviceApi',
    defaultMessage: 'Service/Api',
    description:
      "Column header of the cost table in Reports › Estimated Cost on the document page: the service a line was metered for, with its API or model, such as textract/analyze_document. A phase's subtotal row shows its prompt-cache note in this column.",
  },
  columnUnit: {
    id: 'documents.estimatedCost.column.unit',
    defaultMessage: 'Unit',
    description:
      'Column header of the cost table in Reports › Estimated Cost on the document page: what a line counts, such as inputTokens.',
  },
  columnValue: {
    id: 'documents.estimatedCost.column.value',
    defaultMessage: 'Value',
    description: 'Column header of the cost table in Reports › Estimated Cost on the document page: how many units a line metered.',
  },
  columnUnitCost: {
    id: 'documents.estimatedCost.column.unitCost',
    defaultMessage: 'Unit Cost',
    description: "Column header of the cost table in Reports › Estimated Cost on the document page: the stack's price for one unit.",
  },
  columnCost: {
    id: 'documents.estimatedCost.column.cost',
    defaultMessage: 'Estimated Cost',
    description:
      "Column header of the cost table in Reports › Estimated Cost on the document page: a line's cost, and the subtotal and total rows' amounts.",
  },
  unpricedUnitCost: {
    id: 'documents.estimatedCost.unpricedUnitCost',
    defaultMessage: 'None',
    description:
      "Unit Cost cell of a line in Reports › Estimated Cost on the document page when the stack's pricing has no price for its unit. The line is left out of every total.",
  },
  unpricedCost: {
    id: 'documents.estimatedCost.unpricedCost',
    defaultMessage: 'N/A',
    description:
      "Estimated Cost cell of a line in Reports › Estimated Cost on the document page when the stack's pricing has no price for its unit. The line is left out of every total.",
  },
  subtotal: {
    id: 'documents.estimatedCost.subtotal',
    defaultMessage: '{context} Subtotal',
    description:
      "Label of a phase's subtotal row in the Estimated Cost column of Reports › Estimated Cost on the document page, before the amount. {context} is the phase as metered, such as Extraction, shown as written.",
  },
  total: {
    id: 'documents.estimatedCost.total',
    defaultMessage: 'Total',
    description:
      "Label of the document's total row in the Estimated Cost column of Reports › Estimated Cost on the document page, before the amount.",
  },
  costCell: {
    id: 'documents.estimatedCost.costCell',
    defaultMessage: '{label}: {cost}',
    description:
      'A subtotal or total cell of the Estimated Cost column in Reports › Estimated Cost on the document page. {label} is the row\'s label, such as "Extraction Subtotal" or "Total", and {cost} its amount in US dollars, formatted for the UI language.',
  },
});

// Helper function to format cost cells
const formatCostCell = (rowItem: MeteringRowItem): React.JSX.Element | string => {
  if (rowItem.isTotal) {
    return (
      <Box fontWeight="bold">
        <FormattedMessage {...messages.costCell} values={{ label: String(rowItem.note), cost: rowItem.cost }} />
      </Box>
    );
  }
  if (rowItem.isSubtotal) {
    return (
      <Box fontWeight="bold" color="text-body-secondary">
        <FormattedMessage {...messages.costCell} values={{ label: String(rowItem.note), cost: rowItem.cost }} />
      </Box>
    );
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
  const { locale, formatUsDollars } = useFormatters();
  const intl = useIntl();
  const [pricingData, setPricingData] = useState<PricingLookup>({});
  // We no longer use a default unit cost, showing "None" instead

  useEffect(() => {
    // The one lookup the cost panels share (see price-format.ts): a price the cost
    // reports cannot read is left out, so the unit reads as unpriced here too.
    const pricingLookup = toPricingLookup(pricing);
    if (pricingLookup) {
      setPricingData(pricingLookup);
      logger.debug('Pricing data initialized:', pricingLookup);
    }
  }, [pricing]);

  if (!meteringData) {
    return null;
  }

  if (loading) {
    return (
      <Box>
        <FormattedMessage {...messages.loadingPricing} />
      </Box>
    );
  }

  // Transform metering data into table rows with context parsing
  const { rows, contextTotals, totalCost } = priceMetering(meteringData, pricingData, nonChargeableUnits);
  const rawTableItems: MeteringRowItem[] = rows.map(({ context, serviceApi, unit, value, unitPrice, cost }) => ({
    context,
    serviceApi,
    unit,
    value: String(value),
    // The same adaptive rule as Test Studio's cost table, so one price renders one way on both screens.
    unitCost: unitPrice !== null ? formatUnitCostUsd(unitPrice, locale) : intl.formatMessage(messages.unpricedUnitCost),
    cost: unitPrice !== null ? formatUsDollars(cost, { fractionDigits: 4 }) : intl.formatMessage(messages.unpricedCost),
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
      cost: formatUsDollars(contextTotal, { fractionDigits: 4 }),
      costValue: contextTotal,
      isTotal: false,
      isSubtotal: true,
      note: intl.formatMessage(messages.subtotal, { context }),
      cacheNote:
        cacheSummary && cacheSummary.state !== 'no-cache-data'
          ? describePromptCache(cacheSummary, { phaseOnly: true, context }, intl)
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
    cost: formatUsDollars(finalTotalCost, { fractionDigits: 4 }),
    costValue: finalTotalCost,
    isTotal: true,
    isSubtotal: false,
    note: intl.formatMessage(messages.total),
  });

  return (
    <Table
      columnDefinitions={[
        {
          id: 'context',
          header: intl.formatMessage(messages.columnContext),
          minWidth: METERING_COLUMN_MIN_WIDTH.context,
          cell: (rowItem: MeteringRowItem) => rowItem.context,
        },
        {
          id: 'serviceApi',
          header: intl.formatMessage(messages.columnServiceApi),
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
          header: intl.formatMessage(messages.columnUnit),
          minWidth: METERING_COLUMN_MIN_WIDTH.unit,
          cell: (rowItem: MeteringRowItem) => rowItem.unit,
        },
        {
          id: 'value',
          header: intl.formatMessage(messages.columnValue),
          minWidth: METERING_COLUMN_MIN_WIDTH.value,
          cell: (rowItem: MeteringRowItem) => rowItem.value,
        },
        {
          id: 'unitCost',
          header: intl.formatMessage(messages.columnUnitCost),
          minWidth: METERING_COLUMN_MIN_WIDTH.unitCost,
          cell: (rowItem: MeteringRowItem) => rowItem.unitCost,
        },
        {
          id: 'cost',
          header: intl.formatMessage(messages.columnCost),
          minWidth: METERING_COLUMN_MIN_WIDTH.cost,
          cell: formatCostCell,
        },
      ]}
      // Always at least the total row, and never in a loading state (the pricing's
      // loading text above replaces the table), so it needs no empty or loading text.
      items={tableItems}
      sortingDisabled
      wrapLines
      stripedRows
    />
  );
};

export default MeteringTable;
