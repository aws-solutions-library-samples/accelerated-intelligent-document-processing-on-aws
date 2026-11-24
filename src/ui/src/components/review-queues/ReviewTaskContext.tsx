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
import { sharedLabelMessages } from '../common/labels';
import { EXTRACTION_FAILED_KIND, MISSING_REQUIRED_KIND, reasonText } from './review-api';
import { useFormatters } from '../../i18n/format';

const messages = defineMessages<{
  sentToReview: { readonly reason: string };
  sentBy: { readonly requestedBy: string };
  sentByWithNote: { readonly requestedBy: string; readonly note: string };
  extractionFailed: NoMessageValues;
  title: NoMessageValues;
  escalated: NoMessageValues;
  section: { readonly sectionId: string };
  field: { readonly field: string };
  missingValue: NoMessageValues;
  confidence: { readonly score: string; readonly threshold: string };
  moreAlerts: { readonly count: number };
  showMoreAlerts: { readonly count: number };
  onlyLowest: { readonly count: number };
  showFewer: NoMessageValues;
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
  title: {
    id: 'reviewQueues.taskContext.title',
    defaultMessage: 'Review Reason',
    description: 'Heading of the panel above the editor of a review item that lists why the document is in review, one chip per reason.',
  },
  escalated: {
    id: 'reviewQueues.taskContext.escalated',
    defaultMessage: 'Escalated',
    description: "Header of the info alert on an escalated review item; the alert's text is the escalation note, as written.",
  },
  section: {
    id: 'reviewQueues.taskContext.section',
    defaultMessage: 'Section {sectionId}',
    description:
      "Popover line on a review reason chip: the document section it belongs to. {sectionId} is the section's id, usually a number.",
  },
  field: {
    id: 'reviewQueues.taskContext.field',
    defaultMessage: 'Field: {field}',
    description: "Popover line on a review reason chip: the field it is about. {field} is the field's name or path in the extracted data.",
  },
  missingValue: {
    id: 'reviewQueues.taskContext.missingValue',
    defaultMessage: 'No extracted value; the class schema requires this field.',
    description: 'Popover text on a Missing required review reason: extraction left a field empty that the document class schema requires.',
  },
  confidence: {
    id: 'reviewQueues.taskContext.confidence',
    defaultMessage: 'Confidence {score}, threshold {threshold}',
    description:
      "Popover text on a low-confidence review reason. {score} is the extracted value's confidence and {threshold} the threshold it fell below, both already formatted with two decimals.",
  },
  moreAlerts: {
    id: 'reviewQueues.taskContext.moreAlerts',
    defaultMessage: '+{count, plural, one {# more alert} other {# more alerts}}',
    description:
      'After the first review reason chips: how many more reasons the item has. A button that shows the rest, or a grey badge when the rest are not listed. {count} is at least 1.',
  },
  showMoreAlerts: {
    id: 'reviewQueues.taskContext.showMoreAlerts',
    defaultMessage: 'Show {count, plural, one {# more alert} other {# more alerts}}',
    description: 'Screen-reader label of the button that shows the rest of the review reason chips. {count} is at least 1.',
  },
  onlyLowest: {
    id: 'reviewQueues.taskContext.onlyLowest',
    defaultMessage: 'Only the {count} lowest-confidence alerts are listed.',
    description:
      'Popover on the grey badge counting the review reasons that are not listed: only the worst ones are kept. {count} is how many are listed.',
  },
  showFewer: {
    id: 'reviewQueues.taskContext.showFewer',
    defaultMessage: 'Show fewer',
    description: 'Button that collapses the review reason chips back to the first few.',
  },
});

export const TOP_REASONS = 5;

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
          actions={
            onReasonClick && item.reasons.length > 0 ? (
              <Button onClick={handleNextAlert}>
                <FormattedMessage {...sharedLabelMessages.nextAlert} />
              </Button>
            ) : undefined
          }
        >
          <FormattedMessage {...messages.title} />
        </Header>
      }
    >
      <SpaceBetween size="m">
        {item.escalated && item.escalationNote && (
          <Alert type="info" header={intl.formatMessage(messages.escalated)}>
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
                  <Box>
                    <FormattedMessage {...messages.section} values={{ sectionId: reason.sectionId }} />
                  </Box>
                  {reason.field && (
                    <Box>
                      <FormattedMessage {...messages.field} values={{ field: reason.field }} />
                    </Box>
                  )}
                  {reason.kind === EXTRACTION_FAILED_KIND ? (
                    <>
                      <Box>
                        <FormattedMessage {...messages.extractionFailed} />
                      </Box>
                      {reason.label && <Box>{reason.label}</Box>}
                    </>
                  ) : reason.kind === MISSING_REQUIRED_KIND ? (
                    <Box>
                      <FormattedMessage {...messages.missingValue} />
                    </Box>
                  ) : (
                    <Box>
                      <FormattedMessage
                        {...messages.confidence}
                        values={{ score: formatFixed(reason.score, 2), threshold: formatFixed(reason.threshold, 2) }}
                      />
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
            <Button
              variant="inline-link"
              onClick={() => setShowAll(true)}
              ariaLabel={intl.formatMessage(messages.showMoreAlerts, { count: collapsed + unlisted })}
            >
              <FormattedMessage {...messages.moreAlerts} values={{ count: collapsed + unlisted }} />
            </Button>
          )}
          {collapsed === 0 && unlisted > 0 && (
            <Popover
              triggerType="custom"
              dismissButton={false}
              size="small"
              content={intl.formatMessage(messages.onlyLowest, { count: item.reasons.length })}
            >
              <Badge color="grey">
                <FormattedMessage {...messages.moreAlerts} values={{ count: unlisted }} />
              </Badge>
            </Popover>
          )}
          {showAll && item.reasons.length > TOP_REASONS && (
            <Button variant="inline-link" onClick={() => setShowAll(false)}>
              <FormattedMessage {...messages.showFewer} />
            </Button>
          )}
        </SpaceBetween>
      </SpaceBetween>
    </Container>
  );
};

export default ReviewTaskContext;
