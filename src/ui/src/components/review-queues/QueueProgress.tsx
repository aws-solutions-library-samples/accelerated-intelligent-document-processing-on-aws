// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// A queue page's Queue Progress panel, the same on a Production queue and a Test Studio
// queue: one bar, about half the panel's width, with nearly square corners. The caller
// says what the bar counts (Test Studio: documents completed; Production: documents
// assigned or in progress, since a finished item leaves its queue).
import React from 'react';
import { Container, Header, ProgressBar } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';

/** The bar's look: a little rounding, not a pill. */
const BAR_STYLE = { progressBar: { height: '16px', borderRadius: '2px' } };

type Progress = { readonly count: number; readonly total: number };

const messages = defineMessages<{ title: NoMessageValues; underway: Progress; completed: Progress }>({
  title: {
    id: 'reviewQueues.queueProgress.title',
    defaultMessage: 'Queue Progress',
    description: "Heading of the panel with a bar showing how far a review queue's documents have got, on a queue's page.",
  },
  underway: {
    id: 'reviewQueues.queueProgress.underway',
    defaultMessage: '{count} of {total, plural, one {# document} other {# documents}} assigned or in progress',
    description:
      "Text beside the Queue Progress bar of a production review queue. {count} is how many of the queue's {total} documents an Annotator holds, started or not.",
  },
  completed: {
    id: 'reviewQueues.queueProgress.completed',
    defaultMessage: '{count} of {total, plural, one {# document} other {# documents}} completed',
    description:
      "Text beside the Queue Progress bar of a Test Studio queue. {count} is how many of the queue's {total} documents are completed.",
  },
});

interface QueueProgressProps {
  /** Counted documents, of `total`. */
  count: number;
  total: number;
  /** What `count` counts: documents assigned or in progress (`underway`) or `completed`. */
  countLabel: 'underway' | 'completed';
  /** Under the bar's label: what the bar is about. */
  description: string;
}

/** The percentage of `total` that `count` is, rounded; 0 for an empty queue. */
export const progressPercent = (count: number, total: number): number => (total > 0 ? Math.round((count / total) * 100) : 0);

const QueueProgress = ({ count, total, countLabel, description }: QueueProgressProps): React.JSX.Element => {
  const intl = useIntl();
  return (
    <Container
      header={
        <Header variant="h2">
          <FormattedMessage {...messages.title} />
        </Header>
      }
    >
      {/* Half the panel, so the bar reads as a gauge rather than a page-wide rule. */}
      <div style={{ width: '50%', minWidth: '280px' }}>
        <ProgressBar
          value={progressPercent(count, total)}
          description={description}
          additionalInfo={intl.formatMessage(messages[countLabel], { count, total })}
          style={BAR_STYLE}
        />
      </div>
    </Container>
  );
};

export default QueueProgress;
