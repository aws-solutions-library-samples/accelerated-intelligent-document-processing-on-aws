// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useState } from 'react';
import { Modal, Box, SpaceBetween, Button, Alert } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';
import DateRangeFields, { lastDaysRange, toIsoRange, validateDateRange } from './DateRangeFields';
import type { DateRangeValue } from './DateRangeFields';

const messages = defineMessages<{
  title: NoMessageValues;
  cancel: NoMessageValues;
  apply: NoMessageValues;
  note: NoMessageValues;
}>({
  title: {
    id: 'common.dateRangeModal.title',
    defaultMessage: 'Select custom date range',
    description: 'Title of the dialog that picks a custom date range for a list.',
  },
  cancel: {
    id: 'common.dateRangeModal.cancel',
    defaultMessage: 'Cancel',
    description: 'Button that closes the custom date range dialog without applying a range.',
  },
  apply: {
    id: 'common.dateRangeModal.apply',
    defaultMessage: 'Apply',
    description: 'Button that applies the custom date range.',
  },
  note: {
    id: 'common.dateRangeModal.note',
    defaultMessage:
      'Documents are queried server-side for custom date ranges. Results are paginated for performance. All times are in UTC.',
    description: 'Note under the date and time fields of the custom date range dialog.',
  },
});

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
      header={intl.formatMessage(messages.title)}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss}>
              <FormattedMessage {...messages.cancel} />
            </Button>
            <Button variant="primary" onClick={handleApply}>
              <FormattedMessage {...messages.apply} />
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
          <FormattedMessage {...messages.note} />
        </Box>
      </SpaceBetween>
    </Modal>
  );
};

export default DateRangeModal;
