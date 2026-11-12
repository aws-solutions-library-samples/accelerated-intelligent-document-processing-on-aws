// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The results of an inline bulk Skip All Reviews run or of Send to review.
import React from 'react';
import { Box, Button, Modal, SpaceBetween } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, type NoMessageValues } from 'react-intl';

import BulkResultsTable from './BulkResultsTable';
import type { BulkResultRow } from './BulkResultsTable';
import OutcomeCounts from './OutcomeCounts';

const messages = defineMessages<{ close: NoMessageValues }>({
  close: {
    id: 'reviewBulk.bulkResultsModal.close',
    defaultMessage: 'Close',
    description: 'Button that closes the results dialog of a bulk Skip All Reviews run or of Send to review.',
  },
});

interface BulkResultsModalProps {
  header: string;
  counts: { outcome: string; count: number }[];
  results: BulkResultRow[];
  exportName: string;
  onDismiss: () => void;
}

const BulkResultsModal = ({ header, counts, results, exportName, onDismiss }: BulkResultsModalProps): React.JSX.Element => (
  <Modal
    visible
    size="large"
    header={header}
    onDismiss={onDismiss}
    footer={
      <Box float="right">
        <Button variant="primary" onClick={onDismiss}>
          <FormattedMessage {...messages.close} />
        </Button>
      </Box>
    }
  >
    <SpaceBetween size="m">
      <OutcomeCounts counts={counts} />
      <BulkResultsTable results={results} exportName={exportName} />
    </SpaceBetween>
  </Modal>
);

export default BulkResultsModal;
