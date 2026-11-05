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

import type { ReviewItemDetail } from '../../graphql/generated/operation-types';
import { NEXT_ALERT_LABEL } from '../common/labels';
import { MISSING_REQUIRED_KIND, reasonText } from './review-api';

export const TOP_REASONS = 5;

const alerts = (n: number): string => `${n} more alert${n === 1 ? '' : 's'}`;

export type ReviewReason = ReviewItemDetail['item']['reasons'][number];

interface ReviewTaskContextProps {
  detail: ReviewItemDetail;
  /** Move to the reason's field: its chip was chosen, or Next alert reached it. */
  onReasonClick?: (reason: ReviewReason) => void;
}

const ReviewTaskContext = ({ detail, onReasonClick }: ReviewTaskContextProps): React.JSX.Element => {
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
                  {reason.kind === MISSING_REQUIRED_KIND ? (
                    <Box>No extracted value; the class schema requires this field.</Box>
                  ) : (
                    <Box>
                      Confidence {reason.score.toFixed(2)}, threshold {reason.threshold.toFixed(2)}
                    </Box>
                  )}
                </SpaceBetween>
              }
            >
              <Button variant="inline-link" ariaLabel={reasonText(reason)} onClick={() => onReasonClick?.(reason)}>
                <Badge color="severity-medium">{reasonText(reason)}</Badge>
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
