// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// A queue page's Queue Progress panel, the same on a Production queue and a Test Studio
// queue: one bar, about half the panel's width, with nearly square corners. The caller
// says what the bar counts (Test Studio: documents completed; Production: documents
// assigned or in progress, since a finished item leaves its queue).
import React from 'react';
import { Container, Header, ProgressBar } from '@cloudscape-design/components';

/** The bar's look: a little rounding, not a pill. */
const BAR_STYLE = { progressBar: { height: '16px', borderRadius: '2px' } };

interface QueueProgressProps {
  /** Counted documents, of `total`. */
  count: number;
  total: number;
  /** What `count` counts, as it reads after "<count> of <total> documents": "completed". */
  countLabel: string;
  /** Under the bar's label: what the bar is about. */
  description: string;
}

/** The percentage of `total` that `count` is, rounded; 0 for an empty queue. */
export const progressPercent = (count: number, total: number): number => (total > 0 ? Math.round((count / total) * 100) : 0);

const QueueProgress = ({ count, total, countLabel, description }: QueueProgressProps): React.JSX.Element => (
  <Container header={<Header variant="h2">Queue Progress</Header>}>
    {/* Half the panel, so the bar reads as a gauge rather than a page-wide rule. */}
    <div style={{ width: '50%', minWidth: '280px' }}>
      <ProgressBar
        value={progressPercent(count, total)}
        description={description}
        additionalInfo={`${count} of ${total} documents ${countLabel}`}
        style={BAR_STYLE}
      />
    </div>
  </Container>
);

export default QueueProgress;
