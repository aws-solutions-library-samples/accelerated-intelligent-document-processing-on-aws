// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The review item's history for this cycle, opened from the edit screen's
// History button: who claimed, skipped, completed or rejected it, and why. A skip or
// reject reason and its note are shown with the event, so the next reviewer can see
// why the item came back. The routing events say which routing rule placed the document
// in which queue and the reason it gave; a completed review the step the next run starts
// at and the sections it extracts again; and a send-back the step the document is
// processed again from. Kept off the screen itself, which stays on the review.
import React from 'react';
import { Box, Button, Modal, Table } from '@cloudscape-design/components';
import {
  defineMessages,
  FormattedMessage,
  useIntl,
  type IntlShape,
  type MessageDescriptor,
  type NoMessageValues,
  type TypedMessageDescriptor,
} from 'react-intl';

import type { ReviewItemDetail } from '../../graphql/generated/operation-types';
import { useFormatters } from '../../i18n/format';
import { formatDateTime } from '../../utils/date-time';
import { stepMessage } from '../run-plan/StartStepPicker';
import { routedByFallbackText } from './RoutedBy';

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

/** The events routing and a send-back write, by event name in lower camel case. */
const routingEventMessages = defineMessages<{
  routed: NoMessageValues;
  rerouted: NoMessageValues;
  sentBack: NoMessageValues;
  placementLimitReached: NoMessageValues;
  routingEvaluationFailed: NoMessageValues;
}>({
  routed: {
    id: 'reviewQueues.reviewHistory.event.routed',
    defaultMessage: 'Routed to a queue',
    description: 'Review history event: a routing rule placed the document in a review queue.',
  },
  rerouted: {
    id: 'reviewQueues.reviewHistory.event.rerouted',
    defaultMessage: 'Routed to a queue again after review',
    description: 'Review history event: after a completed review, a routing rule sent the document to a review queue again.',
  },
  sentBack: {
    id: 'reviewQueues.reviewHistory.event.sentBack',
    defaultMessage: 'Sent back for reprocessing',
    description: 'Review history event: the reviewer sent the document back to be processed again from a step.',
  },
  placementLimitReached: {
    id: 'reviewQueues.reviewHistory.event.placementLimitReached',
    defaultMessage: 'Not routed again: placement limit reached',
    description:
      'Review history event: a rule would have sent the document to a queue again, but it had been placed as many times in a row as the profile allows, so processing continued.',
  },
  routingEvaluationFailed: {
    id: 'reviewQueues.reviewHistory.event.routingEvaluationFailed',
    defaultMessage: 'Routing rules could not be evaluated',
    description: 'Review history event: the routing table failed to evaluate after a completed review, so processing continued.',
  },
});

const detailMessages = defineMessages<{
  queue: { readonly queue: string };
  rule: { readonly rule: string };
  defaultOutcome: NoMessageValues;
  routingReason: { readonly reason: string };
  restartStep: { readonly step: string };
  restartSections: { readonly sections: string };
  startStep: { readonly step: string };
  limit: { readonly limit: number };
}>({
  queue: {
    id: 'reviewQueues.reviewHistory.detail.queue',
    defaultMessage: 'Queue: {queue}',
    description: 'Review history detail of a routing event: the id of the review queue the document was placed in.',
  },
  rule: {
    id: 'reviewQueues.reviewHistory.detail.rule',
    defaultMessage: 'Routing rule: {rule}',
    description: 'Review history detail of a routing event: the routing rule that decided, by its label or its id.',
  },
  defaultOutcome: {
    id: 'reviewQueues.reviewHistory.detail.defaultOutcome',
    defaultMessage: 'Default outcome',
    description: 'Review history detail of a routing event: no routing rule matched, and the default outcome of the routing table decided.',
  },
  routingReason: {
    id: 'reviewQueues.reviewHistory.detail.routingReason',
    defaultMessage: 'Routing reason: {reason}',
    description:
      'Review history detail of a routing event: the reason the routing rule gave for placing the document in the queue, with the values the document had. {reason} is that text.',
  },
  restartStep: {
    id: 'reviewQueues.reviewHistory.detail.restartStep',
    defaultMessage: 'Run starts at: {step}',
    description:
      'Review history detail of a completed review: the processing step the run that follows the review starts at. {step} is the name of the step.',
  },
  restartSections: {
    id: 'reviewQueues.reviewHistory.detail.restartSections',
    defaultMessage: 'Sections extracted again: {sections}',
    description:
      'Review history detail of a completed review whose next run extracts some sections again, because the reviewer regrouped their pages. {sections} is the list of their ids.',
  },
  startStep: {
    id: 'reviewQueues.reviewHistory.detail.startStep',
    defaultMessage: 'Start step: {step}',
    description: 'Review history detail of a send-back: the processing step the document is processed again from.',
  },
  limit: {
    id: 'reviewQueues.reviewHistory.detail.limit',
    defaultMessage: 'Limit: {limit, number} in a row',
    description: 'Review history detail: how many times in a row a document may be placed in a review queue.',
  },
});

const camel = (value: string): string => value.replace(/_([a-z0-9])/g, (_match, letter: string) => letter.toUpperCase());

/** The label of an event, in the UI's language: a routing event's message, or `eventLabel`. */
export const eventText = (intl: IntlShape, event: string): string => {
  const message = (routingEventMessages as Record<string, MessageDescriptor>)[camel(event)];
  return message ? intl.formatMessage(message) : eventLabel(event, intl);
};

/** The events whose `reason` detail is the event's own name, not something a person chose. */
const EVENTS_WITH_NO_CHOSEN_REASON = new Set(['sent_back']);

interface EntryDetails {
  reason?: string;
  note?: string;
  /** A routing event: the queue the document was placed in. */
  queue?: string;
  /** A routing event: the rule that decided; empty for the table's default outcome. */
  ruleId?: string;
  ruleLabel?: string;
  /** A routing event: why the document is in the Default queue and not the rule's. */
  fallbackReason?: string;
  /** A routing event: the reason the routing rule gave, with the document's values. */
  routingReason?: string;
  /** A completed review: the step the run that follows starts at, and the sections it extracts again. */
  restartStep?: string;
  restartSections?: string[];
  /** A send-back: the step processing starts at again. */
  startStep?: string;
  /** A placement limit: how many placements in a row the profile allows. */
  limit?: number;
}

const text = (value: unknown): string | undefined => (typeof value === 'string' ? value : undefined);

/** The texts of a list; undefined when it is not a list, or holds none. */
const texts = (value: unknown): string[] | undefined => {
  const items = Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string' && item !== '') : [];
  return items.length > 0 ? items : undefined;
};

export const entryDetails = (entry: HistoryEntry): EntryDetails => {
  if (!entry.details) return {};
  try {
    const parsed = JSON.parse(entry.details) as Record<string, unknown>;
    return {
      // A send-back stores its own name as the reason; nobody chose it.
      reason: EVENTS_WITH_NO_CHOSEN_REASON.has(entry.event) ? undefined : text(parsed.reason),
      note: text(parsed.note) || undefined,
      queue: text(parsed.queue),
      ruleId: text(parsed.rule_id),
      ruleLabel: text(parsed.rule_label) || undefined,
      fallbackReason: text(parsed.fallback_reason) || undefined,
      // Its own key: `reason` is what a person chose.
      routingReason: text(parsed.routing_reason)?.trim() || undefined,
      restartStep: text(parsed.restart_step) || undefined,
      restartSections: texts(parsed.restart_sections),
      startStep: text(parsed.start_step),
      limit: typeof parsed.limit === 'number' ? parsed.limit : undefined,
    };
  } catch {
    return {};
  }
};

/** The name of a processing step; a step this UI has no name for is shown as the server sent it. */
const stepName = (intl: IntlShape, stepId: string): string => {
  const step = stepMessage(stepId);
  return step ? intl.formatMessage(step) : stepId;
};

/** What an entry's details say, one line each: the routing lines first, then the steps of the next run, then the chosen reason. */
export const entryDetailLines = (intl: IntlShape, entry: HistoryEntry): string[] => {
  const details = entryDetails(entry);
  const lines: string[] = [];
  if (details.queue) lines.push(intl.formatMessage(detailMessages.queue, { queue: details.queue }));
  if (details.ruleId !== undefined) {
    lines.push(
      details.ruleId
        ? intl.formatMessage(detailMessages.rule, { rule: details.ruleLabel || details.ruleId })
        : intl.formatMessage(detailMessages.defaultOutcome),
    );
  }
  const fallback = routedByFallbackText(intl, { fallbackReason: details.fallbackReason ?? '' });
  if (fallback) lines.push(fallback);
  if (details.routingReason) lines.push(intl.formatMessage(detailMessages.routingReason, { reason: details.routingReason }));
  if (details.restartStep) lines.push(intl.formatMessage(detailMessages.restartStep, { step: stepName(intl, details.restartStep) }));
  if (details.restartSections) {
    lines.push(
      intl.formatMessage(detailMessages.restartSections, {
        sections: intl.formatList(details.restartSections, { type: 'conjunction' }),
      }),
    );
  }
  if (details.startStep) lines.push(intl.formatMessage(detailMessages.startStep, { step: stepName(intl, details.startStep) }));
  if (details.limit !== undefined) lines.push(intl.formatMessage(detailMessages.limit, { limit: details.limit }));
  if (details.reason) lines.push(details.reason);
  return lines;
};

/** The review activity of one cycle, newest first. Shared with the unified editor's History. */
export const ReviewHistoryTable = ({ history }: { history: ReviewItemDetail['history'] }): React.JSX.Element => {
  const intl = useIntl();
  const { locale } = useFormatters();
  return (
    <Table
      variant="embedded"
      // A routing reason is a sentence or two: it is shown whole, on as many lines as it takes.
      wrapLines
      items={[...history].reverse()}
      trackBy={(entry) => `${entry.at}-${entry.event}`}
      columnDefinitions={[
        { id: 'at', header: intl.formatMessage(messages.columnWhen), cell: (entry) => formatDateTime(entry.at, locale) },
        { id: 'event', header: intl.formatMessage(messages.columnEvent), cell: (entry) => eventText(intl, entry.event) },
        { id: 'actor', header: intl.formatMessage(messages.columnBy), cell: (entry) => entry.actor || '-' },
        {
          id: 'reason',
          header: intl.formatMessage(messages.columnReason),
          cell: (entry) => {
            const lines = entryDetailLines(intl, entry);
            const { note } = entryDetails(entry);
            if (lines.length === 0 && !note) return '-';
            return (
              <Box>
                {lines.map((line) => (
                  <div key={line}>{line}</div>
                ))}
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
