// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Reports › Estimated Cost on the document page: the document's metering, priced with the
// stack's pricing, in a dialog titled with the cost per page. It is mounted only while
// open, so the pricing is read only when it is asked for.
import React, { useMemo } from 'react';
import { Box, Button, Modal } from '@cloudscape-design/components';

import { REPORT_MODAL_SIZE } from '../common/ReportModal';
import usePricing from '../../hooks/use-pricing';
import MeteringTable from './MeteringTable';
import { calculateTotalCosts } from './metering-cost';
import { toPricingLookup } from '../pricing-layout/price-format';

interface EstimatedCostModalProps {
  meteringData: Record<string, Record<string, unknown>>;
  pageCount?: number | null;
  onDismiss: () => void;
}

export { toPricingLookup };

const EstimatedCostModal = ({ meteringData, pageCount, onDismiss }: EstimatedCostModalProps): React.JSX.Element => {
  const { pricing } = usePricing();
  const pricingData = useMemo(() => toPricingLookup(pricing), [pricing]);
  const { totalCost, costPerPage } = calculateTotalCosts(meteringData, pageCount ?? undefined, pricingData);

  return (
    <Modal
      visible
      size={REPORT_MODAL_SIZE}
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
