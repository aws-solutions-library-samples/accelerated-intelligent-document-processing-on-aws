// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useState } from 'react';
import { Modal, Box, SpaceBetween, Button, Alert } from '@cloudscape-design/components';
import { useIntl } from 'react-intl';
import DateRangeFields, { lastDaysRange, toIsoRange, validateDateRange } from './DateRangeFields';
import type { DateRangeValue } from './DateRangeFields';

interface DateRangeModalProps {
  visible: boolean;
  onDismiss: () => void;
  onApply: (range: { startDateTime: string; endDateTime: string }) => void;
}

const DateRangeModal = ({ visible, onDismiss, onApply }: DateRangeModalProps): React.JSX.Element => {
  const intl = useIntl();
  // Default to last 7 days
  const [range, setRange] = useState<DateRangeValue>(() => lastDaysRange(7));
  const [error, setError] = useState<string | null>(null);

  const handleApply = () => {
    const problem = validateDateRange(range, intl);
    setError(problem);
    if (problem) return;
    onApply(toIsoRange(range));
  };

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header="Select custom date range"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss}>
              Cancel
            </Button>
            <Button variant="primary" onClick={handleApply}>
              Apply
            </Button>
          </SpaceBetween>
        </Box>
      }
      size="medium"
    >
      <SpaceBetween size="l">
        {error && <Alert type="error">{error}</Alert>}
        <DateRangeFields value={range} onChange={setRange} />
        <Box variant="small" color="text-body-secondary">
          Documents are queried server-side for custom date ranges. Results are paginated for performance. All times are in UTC.
        </Box>
      </SpaceBetween>
    </Modal>
  );
};

export default DateRangeModal;
