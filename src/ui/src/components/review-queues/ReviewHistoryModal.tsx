// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The review item's history for this cycle, opened from the annotation screen's
// History button: who claimed, skipped, completed or rejected it, and why. A skip or
// reject reason and its note are shown with the event, so the next reviewer can see
// why the item came back. Kept off the screen itself, which stays on the review.
import React from 'react';
import { Box, Button, Modal, Table } from '@cloudscape-design/components';

import type { ReviewItemDetail } from '../../graphql/generated/operation-types';
import { formatDateTime } from '../../utils/date-time';

type HistoryEntry = ReviewItemDetail['history'][number];

const EVENT_LABELS: Record<string, string> = {
  claimed: 'Claimed',
  reclaimed_expired: 'Claimed (previous claim expired)',
  released: 'Skipped',
  force_released: 'Unassigned by an Admin',
  taken_over: 'Taken over',
  assigned: 'Assigned',
  reassigned: 'Reassigned',
  draft_saved: 'Edits saved',
  completed: 'Completed',
  rejected: 'Rejected',
  removed: 'Removed from review',
  moved: 'Moved',
  withdrawn: 'Withdrawn',
  superseded: 'Superseded',
};

export const eventLabel = (event: string): string => EVENT_LABELS[event] ?? event.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase());

interface EntryDetails {
  reason?: string;
  note?: string;
}

export const entryDetails = (entry: HistoryEntry): EntryDetails => {
  if (!entry.details) return {};
  try {
    const parsed = JSON.parse(entry.details) as Record<string, unknown>;
    return {
      reason: typeof parsed.reason === 'string' ? parsed.reason : undefined,
      note: typeof parsed.note === 'string' ? parsed.note : undefined,
    };
  } catch {
    return {};
  }
};

interface ReviewHistoryModalProps {
  history: ReviewItemDetail['history'];
  onDismiss: () => void;
}

const ReviewHistoryModal = ({ history, onDismiss }: ReviewHistoryModalProps): React.JSX.Element => (
  <Modal
    visible
    size="large"
    onDismiss={onDismiss}
    header={`Review history (${history.length})`}
    footer={
      <Box float="right">
        <Button variant="primary" onClick={onDismiss}>
          Close
        </Button>
      </Box>
    }
  >
    <Table
      variant="embedded"
      items={[...history].reverse()}
      trackBy={(entry) => `${entry.at}-${entry.event}`}
      columnDefinitions={[
        { id: 'at', header: 'When', cell: (entry) => formatDateTime(entry.at) },
        { id: 'event', header: 'Event', cell: (entry) => eventLabel(entry.event) },
        { id: 'actor', header: 'By', cell: (entry) => entry.actor || '-' },
        {
          id: 'reason',
          header: 'Reason',
          cell: (entry) => {
            const { reason, note } = entryDetails(entry);
            if (!reason && !note) return '-';
            return (
              <Box>
                {reason}
                {note && (
                  <Box variant="small" color="text-body-secondary">
                    {note}
                  </Box>
                )}
              </Box>
            );
          },
        },
      ]}
      empty={<Box color="text-body-secondary">Nothing has happened to this item yet.</Box>}
    />
  </Modal>
);

export default ReviewHistoryModal;
