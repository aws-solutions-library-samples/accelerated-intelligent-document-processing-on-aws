// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// What the reviewer is asked to do: why the document is here (reason chips), the
// queue's checklist (sent with Complete) and any escalation note.
import React from 'react';
import { Alert, Badge, Box, Checkbox, Container, Header, Popover, SpaceBetween } from '@cloudscape-design/components';

import type { ReviewItemDetail } from '../../graphql/generated/operation-types';
import { reasonText } from './review-api';

interface ReviewTaskContextProps {
  detail: ReviewItemDetail;
  checklist: boolean[];
  onChecklistChange: (values: boolean[]) => void;
  disabled: boolean;
}

const ReviewTaskContext = ({ detail, checklist, onChecklistChange, disabled }: ReviewTaskContextProps): React.JSX.Element => {
  const { item } = detail;
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
          {item.reasons.map((reason) => (
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
          {item.reasonCount > item.reasons.length && <Badge color="grey">{`+${item.reasonCount - item.reasons.length} more`}</Badge>}
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
