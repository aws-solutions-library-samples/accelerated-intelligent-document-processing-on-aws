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
import { defineMessages, useIntl, type IntlShape, type MessageDescriptor, type NoMessageValues } from 'react-intl';

import type { ReviewItemDetail } from '../../graphql/generated/operation-types';
import { useFormatters } from '../../i18n/format';
import { formatDateTime } from '../../utils/date-time';
import { stepMessage } from '../run-plan/StartStepPicker';
import { routedByFallbackText } from './RoutedBy';

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
  skipped: 'Skipped (Skip All Reviews)',
  // The legacy review's records (the document's HITLReviewHistory).
  skip_all: 'Skipped (Skip All Reviews)',
  section_reviewed: 'Section review completed',
  finalize_send_failed: 'Finalize pending retry',
  sent_to_review: 'Sent to review',
  moved: 'Moved',
  withdrawn: 'Withdrawn',
  superseded: 'Superseded',
};

export const eventLabel = (event: string): string => EVENT_LABELS[event] ?? event.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase());

/** The events routing and a send-back write, by event name in lower camel case. */
const routingEventMessages = defineMessages<{
  routed: NoMessageValues;
  rerouted: NoMessageValues;
  sentBack: NoMessageValues;
  placementLimitReached: NoMessageValues;
  routingEvaluationFailed: NoMessageValues;
}>({
  routed: {
    id: 'reviewQueues.history.routed',
    defaultMessage: 'Routed to a queue',
    description: 'Review history event: a routing rule placed the document in a review queue.',
  },
  rerouted: {
    id: 'reviewQueues.history.rerouted',
    defaultMessage: 'Routed to a queue again after review',
    description: 'Review history event: after a completed review, a routing rule sent the document to a review queue again.',
  },
  sentBack: {
    id: 'reviewQueues.history.sentBack',
    defaultMessage: 'Sent back for reprocessing',
    description: 'Review history event: the reviewer sent the document back to be processed again from a step.',
  },
  placementLimitReached: {
    id: 'reviewQueues.history.placementLimitReached',
    defaultMessage: 'Not routed again: placement limit reached',
    description:
      'Review history event: a rule would have sent the document to a queue again, but it had been placed as many times in a row as the profile allows, so processing continued.',
  },
  routingEvaluationFailed: {
    id: 'reviewQueues.history.routingEvaluationFailed',
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
    id: 'reviewQueues.history.detailQueue',
    defaultMessage: 'Queue: {queue}',
    description: 'Review history detail of a routing event: the id of the review queue the document was placed in.',
  },
  rule: {
    id: 'reviewQueues.history.detailRule',
    defaultMessage: 'Routing rule: {rule}',
    description: 'Review history detail of a routing event: the routing rule that decided, by its label or its id.',
  },
  defaultOutcome: {
    id: 'reviewQueues.history.detailDefaultOutcome',
    defaultMessage: 'Default outcome',
    description: 'Review history detail of a routing event: no routing rule matched, and the default outcome of the routing table decided.',
  },
  routingReason: {
    id: 'reviewQueues.history.detailRoutingReason',
    defaultMessage: 'Routing reason: {reason}',
    description:
      'Review history detail of a routing event: the reason the routing rule gave for placing the document in the queue, with the values the document had. {reason} is that text.',
  },
  restartStep: {
    id: 'reviewQueues.history.detailRestartStep',
    defaultMessage: 'Run starts at: {step}',
    description:
      'Review history detail of a completed review: the processing step the run that follows the review starts at. {step} is the name of the step.',
  },
  restartSections: {
    id: 'reviewQueues.history.detailRestartSections',
    defaultMessage: 'Sections extracted again: {sections}',
    description:
      'Review history detail of a completed review whose next run extracts some sections again, because the reviewer regrouped their pages. {sections} is the list of their ids.',
  },
  startStep: {
    id: 'reviewQueues.history.detailStartStep',
    defaultMessage: 'Start step: {step}',
    description: 'Review history detail of a send-back: the processing step the document is processed again from.',
  },
  limit: {
    id: 'reviewQueues.history.detailLimit',
    defaultMessage: 'Limit: {limit, number} in a row',
    description: 'Review history detail: how many times in a row a document may be placed in a review queue.',
  },
});

const camel = (value: string): string => value.replace(/_([a-z0-9])/g, (_match, letter: string) => letter.toUpperCase());

/** The label of an event: a routing event's message, or `eventLabel`. */
export const eventText = (intl: IntlShape, event: string): string => {
  const message = (routingEventMessages as Record<string, MessageDescriptor>)[camel(event)];
  return message ? intl.formatMessage(message) : eventLabel(event);
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
        { id: 'at', header: 'When', cell: (entry) => formatDateTime(entry.at, locale) },
        { id: 'event', header: 'Event', cell: (entry) => eventText(intl, entry.event) },
        { id: 'actor', header: 'By', cell: (entry) => entry.actor || '-' },
        {
          id: 'reason',
          header: 'Reason',
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
      empty={<Box color="text-body-secondary">Nothing has happened to this item yet.</Box>}
    />
  );
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
    <ReviewHistoryTable history={history} />
  </Modal>
);

export default ReviewHistoryModal;
