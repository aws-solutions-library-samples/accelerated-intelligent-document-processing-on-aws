// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Reports › Estimated Cost on the document page: the document's metering, priced with the
// stack's pricing, in a dialog titled with the cost per page. It is mounted only while
// open, so the pricing is read only when it is asked for.
import React, { useMemo } from 'react';
import { Box, Button, Modal } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl } from 'react-intl';

import { REPORT_MODAL_SIZE, reportModalMessages } from '../common/ReportModal';
import usePricing from '../../hooks/use-pricing';
import MeteringTable from './MeteringTable';
import { calculateTotalCosts } from './metering-cost';
import { toPricingLookup } from '../pricing-layout/price-format';
import { useFormatters } from '../../i18n/format';

interface EstimatedCostModalProps {
  meteringData: Record<string, Record<string, unknown>>;
  pageCount?: number | null;
  onDismiss: () => void;
}

const messages = defineMessages<{ title: { readonly cost: string } }>({
  title: {
    id: 'documents.estimatedCost.title',
    defaultMessage: 'Estimated Cost (per page: {cost})',
    description:
      "Title of the dialog Reports › Estimated Cost opens on the document page. {cost} is the document's estimated cost per page, in US dollars formatted for the UI language.",
  },
});

export { toPricingLookup };

const EstimatedCostModal = ({ meteringData, pageCount, onDismiss }: EstimatedCostModalProps): React.JSX.Element => {
  const { pricing } = usePricing();
  const { formatUsDollars } = useFormatters();
  const intl = useIntl();
  const pricingData = useMemo(() => toPricingLookup(pricing), [pricing]);
  const { totalCost, costPerPage } = calculateTotalCosts(meteringData, pageCount ?? undefined, pricingData);

  return (
    <Modal
      visible
      size={REPORT_MODAL_SIZE}
      onDismiss={onDismiss}
      header={intl.formatMessage(messages.title, { cost: formatUsDollars(costPerPage, { fractionDigits: 4 }) })}
      footer={
        <Box float="right">
          <Button variant="primary" onClick={onDismiss}>
            <FormattedMessage {...reportModalMessages.close} />
          </Button>
        </Box>
      }
    >
      <MeteringTable meteringData={meteringData} preCalculatedTotals={{ totalCost, costPerPage }} />
    </Modal>
  );
};

export default EstimatedCostModal;
