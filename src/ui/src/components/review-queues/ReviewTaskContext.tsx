// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Why the document is in review (reason chips) and any escalation note: the one panel
// above the editor, kept to what the reviewer needs to review.
//
// Reasons arrive worst first (largest gap below the threshold), so the first few are
// the ones to look at. Only TOP_REASONS are shown until the reviewer asks for the
// rest; the server stores at most its own cap, so the total beyond that is a count.
// Each reason is a yellow (severity-medium) badge inside an inline-link button, so it
// stays a highlighted chip yet moves focus to its field (`onReasonClick`) by mouse or
// keyboard. Its details are on the popover, and Next alert steps through the stored
// reasons in order.
import React, { useState } from 'react';
import { Alert, Badge, Box, Button, Container, Header, Popover, SpaceBetween } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';

import type { ReviewItemDetail } from '../../graphql/generated/operation-types';
import { NEXT_ALERT_LABEL } from '../common/labels';
import { EXTRACTION_FAILED_KIND, MISSING_REQUIRED_KIND, reasonText } from './review-api';
import { useFormatters } from '../../i18n/format';

const messages = defineMessages<{
  sentToReview: { readonly reason: string };
  sentBy: { readonly requestedBy: string };
  sentByWithNote: { readonly requestedBy: string; readonly note: string };
  extractionFailed: NoMessageValues;
}>({
  extractionFailed: {
    id: 'reviewQueues.taskContext.extractionFailed',
    defaultMessage: 'Extraction failed for this section, so it has no extracted data to check.',
    description:
      'Popover text on an "Extraction failed" review reason: the section was held for review because its extraction did not succeed (for example, a content filter withheld the model reply), not because of a field confidence.',
  },
  sentToReview: {
    id: 'reviewQueues.taskContext.sentToReview',
    defaultMessage: 'Sent to review: {reason}',
    description:
      'Header of the info alert on a review task whose document was sent to review by hand. {reason} is the reason chosen when sending it.',
  },
  sentBy: {
    id: 'reviewQueues.taskContext.sentBy',
    defaultMessage: 'By {requestedBy}',
    description: 'Body of the Sent to review alert. {requestedBy} is the user (e-mail) who sent the document to review.',
  },
  sentByWithNote: {
    id: 'reviewQueues.taskContext.sentByWithNote',
    defaultMessage: 'By {requestedBy} — {note}',
    description:
      'Body of the Sent to review alert when the sender left a note. {requestedBy} is the user (e-mail) who sent it; {note} is their free-text note.',
  },
});

export const TOP_REASONS = 5;

const alerts = (n: number): string => `${n} more alert${n === 1 ? '' : 's'}`;

export type ReviewReason = ReviewItemDetail['item']['reasons'][number];

interface ReviewTaskContextProps {
  detail: ReviewItemDetail;
  /** Move to the reason's field: its chip was chosen, or Next alert reached it. */
  onReasonClick?: (reason: ReviewReason) => void;
}

const ReviewTaskContext = ({ detail, onReasonClick }: ReviewTaskContextProps): React.JSX.Element => {
  const intl = useIntl();
  const { formatFixed } = useFormatters();
  const { item } = detail;
  const [showAll, setShowAll] = useState(false);
  // The reason Next alert moves to next.
  const [nextIndex, setNextIndex] = useState(0);
  const shown = showAll ? item.reasons : item.reasons.slice(0, TOP_REASONS);
  const collapsed = item.reasons.length - shown.length;
  const unlisted = Math.max(item.reasonCount - item.reasons.length, 0);

  const handleNextAlert = () => {
    if (!onReasonClick || item.reasons.length === 0) return;
    const at = nextIndex % item.reasons.length;
    onReasonClick(item.reasons[at]);
    setNextIndex(at + 1);
  };

  return (
    <Container
      header={
        <Header
          variant="h2"
          counter={`(${item.reasonCount})`}
          actions={onReasonClick && item.reasons.length > 0 ? <Button onClick={handleNextAlert}>{NEXT_ALERT_LABEL}</Button> : undefined}
        >
          Review Reason
        </Header>
      }
    >
      <SpaceBetween size="m">
        {item.escalated && item.escalationNote && (
          <Alert type="info" header="Escalated">
            {item.escalationNote}
          </Alert>
        )}
        {item.sentToReview && (
          <Alert type="info" header={intl.formatMessage(messages.sentToReview, { reason: item.sentToReview.reason })}>
            {item.sentToReview.note ? (
              <FormattedMessage
                {...messages.sentByWithNote}
                values={{ requestedBy: item.sentToReview.requestedBy, note: item.sentToReview.note }}
              />
            ) : (
              <FormattedMessage {...messages.sentBy} values={{ requestedBy: item.sentToReview.requestedBy }} />
            )}
          </Alert>
        )}
        <SpaceBetween direction="horizontal" size="xs">
          {shown.map((reason) => (
            <Popover
              key={`${reason.sectionId}-${reason.kind}-${reason.field ?? ''}-${reason.score}`}
              triggerType="custom"
              dismissButton={false}
              size="small"
              content={
                <SpaceBetween size="xxs">
                  <Box>Section {reason.sectionId}</Box>
                  {reason.field && <Box>Field: {reason.field}</Box>}
                  {reason.kind === EXTRACTION_FAILED_KIND ? (
                    <>
                      <Box>
                        <FormattedMessage {...messages.extractionFailed} />
                      </Box>
                      {reason.label && <Box>{reason.label}</Box>}
                    </>
                  ) : reason.kind === MISSING_REQUIRED_KIND ? (
                    <Box>No extracted value; the class schema requires this field.</Box>
                  ) : (
                    <Box>
                      Confidence {formatFixed(reason.score, 2)}, threshold {formatFixed(reason.threshold, 2)}
                    </Box>
                  )}
                </SpaceBetween>
              }
            >
              <Button variant="inline-link" ariaLabel={reasonText(reason, intl)} onClick={() => onReasonClick?.(reason)}>
                <Badge color="severity-medium">{reasonText(reason, intl)}</Badge>
              </Button>
            </Popover>
          ))}
          {collapsed > 0 && (
            <Button variant="inline-link" onClick={() => setShowAll(true)} ariaLabel={`Show ${alerts(collapsed + unlisted)}`}>
              {`+${alerts(collapsed + unlisted)}`}
            </Button>
          )}
          {collapsed === 0 && unlisted > 0 && (
            <Popover
              triggerType="custom"
              dismissButton={false}
              size="small"
              content={`Only the ${item.reasons.length} lowest-confidence alerts are listed.`}
            >
              <Badge color="grey">{`+${alerts(unlisted)}`}</Badge>
            </Popover>
          )}
          {showAll && item.reasons.length > TOP_REASONS && (
            <Button variant="inline-link" onClick={() => setShowAll(false)}>
              Show fewer
            </Button>
          )}
        </SpaceBetween>
      </SpaceBetween>
    </Container>
  );
};

export default ReviewTaskContext;
