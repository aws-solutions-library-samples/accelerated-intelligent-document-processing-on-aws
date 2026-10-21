// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// What the reviewer is asked to do: why the document is here (reason chips), the
// queue's checklist (sent with Complete) and any escalation note.
//
// Reasons arrive worst first (largest gap below the threshold), so the first few are
// the ones to look at. Only TOP_REASONS are shown until the reviewer asks for the
// rest; the server stores at most its own cap, so the total beyond that is a count.
import React, { useState } from 'react';
import { Alert, Badge, Box, Button, Checkbox, Container, Header, Popover, SpaceBetween } from '@cloudscape-design/components';

import type { ReviewItemDetail } from '../../graphql/generated/operation-types';
import { reasonText } from './review-api';

export const TOP_REASONS = 5;

const alerts = (n: number): string => `${n} more alert${n === 1 ? '' : 's'}`;

interface ReviewTaskContextProps {
  detail: ReviewItemDetail;
  checklist: boolean[];
  onChecklistChange: (values: boolean[]) => void;
  disabled: boolean;
}

const ReviewTaskContext = ({ detail, checklist, onChecklistChange, disabled }: ReviewTaskContextProps): React.JSX.Element => {
  const { item } = detail;
  const [showAll, setShowAll] = useState(false);
  const shown = showAll ? item.reasons : item.reasons.slice(0, TOP_REASONS);
  const collapsed = item.reasons.length - shown.length;
  const unlisted = Math.max(item.reasonCount - item.reasons.length, 0);
  return (
    <Container
      header={
        <Header variant="h2" counter={`(${item.reasonCount})`}>
          Why this document needs review
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
                  <Box>
                    Confidence {reason.score.toFixed(2)}, threshold {reason.threshold.toFixed(2)}
                  </Box>
                </SpaceBetween>
              }
            >
              <Badge color="severity-medium">{reasonText(reason)}</Badge>
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
        {detail.checklist.length > 0 && (
          <SpaceBetween size="xxs">
            <Box variant="h4">Checklist</Box>
            {detail.checklist.map((label, i) => (
              <Checkbox
                key={label}
                checked={checklist[i] ?? false}
                disabled={disabled}
                onChange={({ detail: d }) => {
                  const next = [...checklist];
                  next[i] = d.checked;
                  onChecklistChange(next);
                }}
              >
                {label}
              </Checkbox>
            ))}
          </SpaceBetween>
        )}
      </SpaceBetween>
    </Container>
  );
};

export default ReviewTaskContext;
