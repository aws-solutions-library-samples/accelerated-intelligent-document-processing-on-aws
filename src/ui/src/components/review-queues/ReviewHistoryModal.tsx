// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The review item's history for this cycle, opened from the edit screen's
// History button: who claimed, skipped, completed or rejected it, and why. A skip or
// reject reason and its note are shown with the event, so the next reviewer can see
// why the item came back. Kept off the screen itself, which stays on the review.
import React from 'react';
import { Box, Button, Modal, Table } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type IntlShape, type NoMessageValues, type TypedMessageDescriptor } from 'react-intl';

import type { ReviewItemDetail } from '../../graphql/generated/operation-types';
import { useFormatters } from '../../i18n/format';
import { formatDateTime } from '../../utils/date-time';

type HistoryEntry = ReviewItemDetail['history'][number];

const messages = defineMessages<{
  columnWhen: NoMessageValues;
  columnEvent: NoMessageValues;
  columnBy: NoMessageValues;
  columnReason: NoMessageValues;
  empty: NoMessageValues;
  title: { readonly count: number };
  close: NoMessageValues;
}>({
  columnWhen: {
    id: 'reviewQueues.reviewHistory.column.when',
    defaultMessage: 'When',
    description: "Column header of a review item's history table (History › Review Activity on the edit page): when the event happened.",
  },
  columnEvent: {
    id: 'reviewQueues.reviewHistory.column.event',
    defaultMessage: 'Event',
    description: "Column header of a review item's history table: what happened, such as Claimed or Skipped.",
  },
  columnBy: {
    id: 'reviewQueues.reviewHistory.column.by',
    defaultMessage: 'By',
    description: "Column header of a review item's history table: who did it (an e-mail address).",
  },
  columnReason: {
    id: 'reviewQueues.reviewHistory.column.reason',
    defaultMessage: 'Reason',
    description: "Column header of a review item's history table: the reason and note given with the event, as written.",
  },
  empty: {
    id: 'reviewQueues.reviewHistory.empty',
    defaultMessage: 'Nothing has happened to this item yet.',
    description: "Shown in a review item's history table when it has no events.",
  },
  title: {
    id: 'reviewQueues.reviewHistory.title',
    defaultMessage: 'Review history ({count})',
    description: "Header of the dialog listing a review item's history. {count} is the number of events.",
  },
  close: {
    id: 'reviewQueues.reviewHistory.close',
    defaultMessage: 'Close',
    description: "Button that closes the review item's history dialog.",
  },
});

/** The label of each history event the backend records, in the Event column. */
const eventMessages = defineMessages<{
  claimed: NoMessageValues;
  reclaimedExpired: NoMessageValues;
  released: NoMessageValues;
  forceReleased: NoMessageValues;
  takenOver: NoMessageValues;
  assigned: NoMessageValues;
  reassigned: NoMessageValues;
  draftSaved: NoMessageValues;
  completed: NoMessageValues;
  rejected: NoMessageValues;
  removed: NoMessageValues;
  skippedAll: NoMessageValues;
  sectionReviewed: NoMessageValues;
  finalizeSendFailed: NoMessageValues;
  sentToReview: NoMessageValues;
  moved: NoMessageValues;
  withdrawn: NoMessageValues;
  superseded: NoMessageValues;
}>({
  claimed: {
    id: 'reviewQueues.reviewHistory.event.claimed',
    defaultMessage: 'Claimed',
    description: "Event in a review item's history: an Annotator claimed the item.",
  },
  reclaimedExpired: {
    id: 'reviewQueues.reviewHistory.event.reclaimedExpired',
    defaultMessage: 'Claimed (previous claim expired)',
    description: "Event in a review item's history: an Annotator claimed the item after someone else's claim on it expired.",
  },
  released: {
    id: 'reviewQueues.reviewHistory.event.released',
    defaultMessage: 'Skipped',
    description: "Event in a review item's history: its Annotator gave the item back to its queue unfinished (Skip).",
  },
  forceReleased: {
    id: 'reviewQueues.reviewHistory.event.forceReleased',
    defaultMessage: 'Unassigned by an Admin',
    description: "Event in a review item's history: an Admin took the item away from its Annotator and put it back in its queue.",
  },
  takenOver: {
    id: 'reviewQueues.reviewHistory.event.takenOver',
    defaultMessage: 'Taken over',
    description: "Event in a review item's history: an Admin took over the item from the Annotator it was assigned to.",
  },
  assigned: {
    id: 'reviewQueues.reviewHistory.event.assigned',
    defaultMessage: 'Assigned',
    description: "Event in a review item's history: an Admin assigned the item to an Annotator.",
  },
  reassigned: {
    id: 'reviewQueues.reviewHistory.event.reassigned',
    defaultMessage: 'Reassigned',
    description: "Event in a review item's history: an Admin assigned the item to another Annotator.",
  },
  draftSaved: {
    id: 'reviewQueues.reviewHistory.event.draftSaved',
    defaultMessage: 'Edits saved',
    description: "Event in a review item's history: the reviewer saved their edits as a draft.",
  },
  completed: {
    id: 'reviewQueues.reviewHistory.event.completed',
    defaultMessage: 'Completed',
    description: "Event in a review item's history: the review was completed.",
  },
  rejected: {
    id: 'reviewQueues.reviewHistory.event.rejected',
    defaultMessage: 'Rejected',
    description: "Event in a review item's history: the reviewer rejected the document.",
  },
  removed: {
    id: 'reviewQueues.reviewHistory.event.removed',
    defaultMessage: 'Removed from review',
    description: "Event in a review item's history: an Admin took the item out of review, so the document kept its machine results.",
  },
  skippedAll: {
    id: 'reviewQueues.reviewHistory.event.skippedAll',
    defaultMessage: 'Skipped (Skip All Reviews)',
    description:
      "Event in a review item's history: its review was finished without checking, with Skip All Reviews. Skip All Reviews names an action of the app.",
  },
  sectionReviewed: {
    id: 'reviewQueues.reviewHistory.event.sectionReviewed',
    defaultMessage: 'Section review completed',
    description: "Event in a document's review history: the review of one of its sections was completed.",
  },
  finalizeSendFailed: {
    id: 'reviewQueues.reviewHistory.event.finalizeSendFailed',
    defaultMessage: 'Finalize pending retry',
    description: "Event in a review item's history: applying the review's result could not start and will be tried again.",
  },
  sentToReview: {
    id: 'reviewQueues.reviewHistory.event.sentToReview',
    defaultMessage: 'Sent to review',
    description: "Event in a review item's history: someone sent the finished document to a review queue by hand.",
  },
  moved: {
    id: 'reviewQueues.reviewHistory.event.moved',
    defaultMessage: 'Moved',
    description: "Event in a review item's history: an Admin moved the item to another queue.",
  },
  withdrawn: {
    id: 'reviewQueues.reviewHistory.event.withdrawn',
    defaultMessage: 'Withdrawn',
    description:
      "Event in a review item's history: the item left review without a result, for example because its document was processed again.",
  },
  superseded: {
    id: 'reviewQueues.reviewHistory.event.superseded',
    defaultMessage: 'Superseded',
    description: "Event in a review item's history: a newer review of the same document replaced this one.",
  },
});

/** Each event code the backend records, and its label. */
const eventMessageByCode: Readonly<Record<string, TypedMessageDescriptor<NoMessageValues>>> = {
  claimed: eventMessages.claimed,
  reclaimed_expired: eventMessages.reclaimedExpired,
  released: eventMessages.released,
  force_released: eventMessages.forceReleased,
  taken_over: eventMessages.takenOver,
  assigned: eventMessages.assigned,
  reassigned: eventMessages.reassigned,
  draft_saved: eventMessages.draftSaved,
  completed: eventMessages.completed,
  rejected: eventMessages.rejected,
  removed: eventMessages.removed,
  skipped: eventMessages.skippedAll,
  // The legacy review's records (the document's HITLReviewHistory).
  skip_all: eventMessages.skippedAll,
  section_reviewed: eventMessages.sectionReviewed,
  finalize_send_failed: eventMessages.finalizeSendFailed,
  sent_to_review: eventMessages.sentToReview,
  moved: eventMessages.moved,
  withdrawn: eventMessages.withdrawn,
  superseded: eventMessages.superseded,
};

/**
 * An event's label: in the UI's language with `intl`, else the English source text. An event
 * the UI does not know is shown readably, as stored ("something_new" reads "Something new").
 */
export const eventLabel = (event: string, intl?: IntlShape): string => {
  const message = Object.hasOwn(eventMessageByCode, event) ? eventMessageByCode[event] : undefined;
  if (!message) return event.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase());
  return intl ? intl.formatMessage(message) : String(message.defaultMessage);
};

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

/** The review activity of one cycle, newest first. Shared with the unified editor's History. */
export const ReviewHistoryTable = ({ history }: { history: ReviewItemDetail['history'] }): React.JSX.Element => {
  const intl = useIntl();
  const { locale } = useFormatters();
  return (
    <Table
      variant="embedded"
      items={[...history].reverse()}
      trackBy={(entry) => `${entry.at}-${entry.event}`}
      columnDefinitions={[
        { id: 'at', header: intl.formatMessage(messages.columnWhen), cell: (entry) => formatDateTime(entry.at, locale) },
        { id: 'event', header: intl.formatMessage(messages.columnEvent), cell: (entry) => eventLabel(entry.event, intl) },
        { id: 'actor', header: intl.formatMessage(messages.columnBy), cell: (entry) => entry.actor || '-' },
        {
          id: 'reason',
          header: intl.formatMessage(messages.columnReason),
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
      empty={
        <Box color="text-body-secondary">
          <FormattedMessage {...messages.empty} />
        </Box>
      }
    />
  );
};

interface ReviewHistoryModalProps {
  history: ReviewItemDetail['history'];
  onDismiss: () => void;
}

const ReviewHistoryModal = ({ history, onDismiss }: ReviewHistoryModalProps): React.JSX.Element => {
  const intl = useIntl();
  return (
    <Modal
      visible
      size="large"
      onDismiss={onDismiss}
      header={intl.formatMessage(messages.title, { count: history.length })}
      footer={
        <Box float="right">
          <Button variant="primary" onClick={onDismiss}>
            <FormattedMessage {...messages.close} />
          </Button>
        </Box>
      }
    >
      <ReviewHistoryTable history={history} />
    </Modal>
  );
};

export default ReviewHistoryModal;
