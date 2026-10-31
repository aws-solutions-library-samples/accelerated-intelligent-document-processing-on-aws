// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Reports › Estimated Cost on the document page: the document's metering, priced with the
// stack's pricing, in a dialog titled with the cost per page. It is mounted only while
// open, so the pricing is read only when it is asked for.
import React, { useMemo } from 'react';
import { Box, Button, Modal } from '@cloudscape-design/components';

import usePricing from '../../hooks/use-pricing';
import MeteringTable from './MeteringTable';
import type { PricingData } from './MeteringTable';
import { calculateTotalCosts } from './metering-cost';
import type { PricingLookup } from './pricing';

interface EstimatedCostModalProps {
  meteringData: Record<string, Record<string, unknown>>;
  pageCount?: number | null;
  onDismiss: () => void;
}

/** The pricing as a lookup by service and unit. */
export const toPricingLookup = (pricing: unknown): PricingLookup | null => {
  const rows = (pricing as PricingData | null)?.pricing;
  if (!rows) return null;
  const lookup: PricingLookup = {};
  rows.forEach((item) => {
    if (item.name && item.units) {
      lookup[item.name] = {};
      item.units.forEach((unitItem) => {
        if (unitItem.name && unitItem.price !== undefined) lookup[item.name][unitItem.name] = Number(unitItem.price);
      });
    }
  });
  return lookup;
};

const EstimatedCostModal = ({ meteringData, pageCount, onDismiss }: EstimatedCostModalProps): React.JSX.Element => {
  const { pricing } = usePricing();
  const pricingData = useMemo(() => toPricingLookup(pricing), [pricing]);
  const { totalCost, costPerPage } = calculateTotalCosts(meteringData, pageCount ?? undefined, pricingData);

  return (
    <Modal
      visible
      size="large"
      onDismiss={onDismiss}
      header={`Estimated Cost (per page: $${costPerPage.toFixed(4)})`}
      footer={
        <Box float="right">
          <Button variant="primary" onClick={onDismiss}>
            Close
          </Button>
        </Box>
      }
    >
      <MeteringTable meteringData={meteringData} preCalculatedTotals={{ totalCost, costPerPage }} />
    </Modal>
  );
};

export default EstimatedCostModal;
