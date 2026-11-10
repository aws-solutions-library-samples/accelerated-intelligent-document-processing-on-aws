// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The non-zero outcome counts of a dry run, a run or a job, as StatusIndicators.
import React from 'react';
import { Box, SpaceBetween, StatusIndicator } from '@cloudscape-design/components';

import { outcomeView } from './bulk-outcomes';

interface OutcomeCountsProps {
  counts: { outcome: string; count: number }[];
  /** Label the SKIPPED count as what will happen (dry run) rather than what did. */
  preview?: boolean;
}

const OutcomeCounts = ({ counts, preview = false }: OutcomeCountsProps): React.JSX.Element => {
  const shown = counts.filter((c) => c.count > 0);
  if (shown.length === 0) return <Box color="text-body-secondary">No documents.</Box>;
  return (
    <SpaceBetween size="xxs">
      {shown.map((c) => {
        const view = outcomeView(c.outcome);
        const label = preview && c.outcome === 'SKIPPED' ? 'Will be skipped' : view.label;
        return (
          <StatusIndicator key={c.outcome} type={view.type}>
            {`${label}: ${c.count.toLocaleString()}`}
          </StatusIndicator>
        );
      })}
    </SpaceBetween>
  );
};

export default OutcomeCounts;
