// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The non-zero outcome counts of a dry run, a run or a job, as StatusIndicators.
import React from 'react';
import { Box, SpaceBetween, StatusIndicator } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';

import { outcomeView } from './bulk-outcomes';

const messages = defineMessages<{
  noDocuments: NoMessageValues;
  willBeSkipped: NoMessageValues;
  count: { readonly label: string; readonly count: number };
}>({
  noDocuments: {
    id: 'reviewBulk.outcomeCounts.noDocuments',
    defaultMessage: 'No documents.',
    description: 'Shown instead of the outcome counts of a bulk Skip All Reviews run or Send to review when no document has an outcome.',
  },
  willBeSkipped: {
    id: 'reviewBulk.outcomeCounts.willBeSkipped',
    defaultMessage: 'Will be skipped',
    description: 'Outcome label in the dry-run preview of bulk Skip All Reviews: the reviews that the run will skip.',
  },
  count: {
    id: 'reviewBulk.outcomeCounts.count',
    defaultMessage: '{label}: {count, number}',
    description:
      'One outcome count of a bulk Skip All Reviews run or Send to review. {label} is the outcome (e.g. "Skipped"), {count} the number of documents.',
  },
});

interface OutcomeCountsProps {
  counts: { outcome: string; count: number }[];
  /** Label the SKIPPED count as what will happen (dry run) rather than what did. */
  preview?: boolean;
}

const OutcomeCounts = ({ counts, preview = false }: OutcomeCountsProps): React.JSX.Element => {
  const intl = useIntl();
  const shown = counts.filter((c) => c.count > 0);
  if (shown.length === 0)
    return (
      <Box color="text-body-secondary">
        <FormattedMessage {...messages.noDocuments} />
      </Box>
    );
  return (
    <SpaceBetween size="xxs">
      {shown.map((c) => {
        const view = outcomeView(c.outcome);
        const label = preview && c.outcome === 'SKIPPED' ? intl.formatMessage(messages.willBeSkipped) : view.label;
        return (
          <StatusIndicator key={c.outcome} type={view.type}>
            {intl.formatMessage(messages.count, { label, count: c.count })}
          </StatusIndicator>
        );
      })}
    </SpaceBetween>
  );
};

export default OutcomeCounts;
