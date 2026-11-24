// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// How a routing decision reads to a reviewer: the rule that sent the document to its
// queue (its label, or its id when it has none), or the table's default outcome when
// no rule matched; when the queue the rule named could not take the document, why it is
// in the Default queue instead; the reason the routing rule gives, with the values the
// document has; and the policy rules the document failed. Shown above the editor
// (ReviewTaskContext), as the reason of a work item (WorkItemsTable) and on the document
// page (DocumentPanel).
//
// The reason and the explanations of the failed policy rules can hold values taken
// from the document. They are rendered as text, never as HTML or markdown. A long text
// is shortened by its number of characters, with the whole of it one click away.
import React, { useState } from 'react';
import { Box, Button, Modal, Popover, SpaceBetween, Table } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type IntlShape, type MessageTag, type NoMessageValues } from 'react-intl';

import type { ReviewItem } from '../../graphql/generated/operation-types';
import { fallbackMessage } from '../config-routing/RoutingTestPanel';

/** The routing decision of a review item, as the API returns it. */
export type RoutedBy = NonNullable<ReviewItem['routedBy']>;
/** What the displays read of it. */
export type RoutedByLike = Pick<RoutedBy, 'ruleId' | 'ruleLabel' | 'fallbackReason' | 'reason' | 'policyFailures' | 'policyFailureCount'>;
/** A policy rule the document failed, with the reason policy rule validation gave. */
export type PolicyFailure = NonNullable<RoutedBy['policyFailures']>[number];

/** Characters of the reason shown before "Show more". */
export const REASON_PREVIEW_CHARS = 280;
/** Failed policy rules listed before "Show all". */
export const POLICY_FAILURE_PREVIEW = 3;
/** Characters of a failed policy rule's reason shown in the list. */
export const POLICY_FAILURE_REASON_CHARS = 120;

const messages = defineMessages<{
  rule: { readonly rule: string };
  defaultOutcome: NoMessageValues;
  placedInDefault: { readonly why: string };
  showMore: NoMessageValues;
  showLess: NoMessageValues;
}>({
  rule: {
    id: 'reviewQueues.routedBy.rule',
    defaultMessage: 'Routed by rule: {rule}',
    description: 'Why a document is in a review queue: a routing rule sent it there. {rule} is the label of the rule, or its id.',
  },
  defaultOutcome: {
    id: 'reviewQueues.routedBy.defaultOutcome',
    defaultMessage: 'Routed by the default outcome',
    description:
      'Why a document is in a review queue: no routing rule matched, and the default outcome of the routing table sent it there.',
  },
  placedInDefault: {
    id: 'reviewQueues.routedBy.placedInDefault',
    defaultMessage: '{why} The document is in the Default queue instead.',
    description:
      'Shown with the routing decision when the queue it named could not take the document. {why} is a sentence that says why (the queue does not exist, is disabled, or the table could not be evaluated).',
  },
  showMore: {
    id: 'reviewQueues.routedBy.showMore',
    defaultMessage: 'Show more',
    description: 'Button after a shortened routing reason: shows the whole reason.',
  },
  showLess: {
    id: 'reviewQueues.routedBy.showLess',
    defaultMessage: 'Show less',
    description: 'Button after a routing reason shown in full: shortens it again.',
  },
});

const failureMessages = defineMessages<{
  heading: { readonly count: number };
  line: { readonly name: string; readonly policy: string; readonly reason: string; readonly b: MessageTag; readonly detail: MessageTag };
  noReason: NoMessageValues;
  pages: { readonly pages: string };
  showAll: { readonly count: number };
  modalHeader: { readonly count: number };
  columnRule: NoMessageValues;
  columnPolicy: NoMessageValues;
  columnReason: NoMessageValues;
  columnPages: NoMessageValues;
  notAllStored: { readonly listed: number; readonly count: number };
  empty: NoMessageValues;
  close: NoMessageValues;
}>({
  heading: {
    id: 'reviewQueues.policyFailures.heading',
    defaultMessage: 'Failed policy rules ({count, number})',
    description:
      'Heading of the list of policy rules a document in review failed, under the routing reason. {count} is how many it failed in all.',
  },
  line: {
    id: 'reviewQueues.policyFailures.line',
    defaultMessage: '<b>{name}</b> · {policy} — <detail>{reason}</detail>',
    description:
      'One failed policy rule in the list under the routing reason. {name} is the name of the policy rule, {policy} the policy it belongs to, {reason} why policy rule validation failed it (possibly shortened). <b> marks the name; <detail> marks the text that opens the full reason.',
  },
  noReason: {
    id: 'reviewQueues.policyFailures.noReason',
    defaultMessage: 'No reason was given.',
    description: 'Shown in place of the reason of a failed policy rule when policy rule validation gave none.',
  },
  pages: {
    id: 'reviewQueues.policyFailures.pages',
    defaultMessage: 'Pages: {pages}',
    description:
      'Under the full reason of a failed policy rule: the pages of the document the reason cites. {pages} is a list of page numbers.',
  },
  showAll: {
    id: 'reviewQueues.policyFailures.showAll',
    defaultMessage: '{count, plural, one {Show the # failed policy rule} other {Show all # failed policy rules}}',
    description:
      'Button under the first failed policy rules of a document in review: opens a dialog that lists all of them. {count} is how many there are.',
  },
  modalHeader: {
    id: 'reviewQueues.policyFailures.modalHeader',
    defaultMessage: 'Failed policy rules ({count, number})',
    description: 'Title of the dialog that lists the policy rules a document in review failed. {count} is how many it failed in all.',
  },
  columnRule: {
    id: 'reviewQueues.policyFailures.columnRule',
    defaultMessage: 'Policy rule',
    description: 'Column of the failed policy rules dialog: the name of the policy rule.',
  },
  columnPolicy: {
    id: 'reviewQueues.policyFailures.columnPolicy',
    defaultMessage: 'Policy',
    description: 'Column of the failed policy rules dialog: the policy the policy rule belongs to.',
  },
  columnReason: {
    id: 'reviewQueues.policyFailures.columnReason',
    defaultMessage: 'Reason',
    description: 'Column of the failed policy rules dialog: why policy rule validation failed the policy rule.',
  },
  columnPages: {
    id: 'reviewQueues.policyFailures.columnPages',
    defaultMessage: 'Pages',
    description: 'Column of the failed policy rules dialog: the pages of the document the reason cites.',
  },
  notAllStored: {
    id: 'reviewQueues.policyFailures.notAllStored',
    defaultMessage:
      'The document failed {count, plural, one {# policy rule} other {# policy rules}}; {listed, plural, one {# is} other {# are}} listed here. The Policy Rule Validation Summary has all of them.',
    description:
      'Note in the failed policy rules dialog when the document failed more policy rules than are kept with its review. {listed} is how many are listed, {count} how many it failed. "Policy Rule Validation Summary" is the name of a report.',
  },
  empty: {
    id: 'reviewQueues.policyFailures.empty',
    defaultMessage: 'No failed policy rule is listed for this review.',
    description: 'Shown in the failed policy rules dialog when it has no row.',
  },
  close: {
    id: 'reviewQueues.policyFailures.close',
    defaultMessage: 'Close',
    description: 'Button that closes the failed policy rules dialog.',
  },
});

/** "Routed by rule: Claims over 1000", or "Routed by the default outcome". */
export const routedByText = (intl: IntlShape, routedBy: Pick<RoutedByLike, 'ruleId' | 'ruleLabel'>): string =>
  routedBy.ruleId
    ? intl.formatMessage(messages.rule, { rule: routedBy.ruleLabel || routedBy.ruleId })
    : intl.formatMessage(messages.defaultOutcome);

/**
 * Why the document is in the Default queue and not the one its rule named; null when it
 * is where the rule sent it. A reason this UI has no sentence for is shown as the server
 * sent it.
 */
export const routedByFallbackText = (intl: IntlShape, routedBy: Pick<RoutedByLike, 'fallbackReason'>): string | null => {
  if (!routedBy.fallbackReason) return null;
  const message = fallbackMessage(routedBy.fallbackReason);
  return intl.formatMessage(messages.placedInDefault, { why: message ? intl.formatMessage(message) : routedBy.fallbackReason });
};

/** `text` cut to its first `maxChars` characters and ended with an ellipsis; `text` itself when it is no longer than that. */
export const shortened = (text: string, maxChars: number): string => {
  const characters = Array.from(text);
  return characters.length <= maxChars ? text : `${characters.slice(0, maxChars).join('').trimEnd()}…`;
};

/** How many policy rules the document failed: the count the server kept, and never fewer than it lists. */
export const policyFailureTotal = (routedBy: Pick<RoutedByLike, 'policyFailures' | 'policyFailureCount'> | null | undefined): number =>
  Math.max(routedBy?.policyFailureCount ?? 0, routedBy?.policyFailures?.length ?? 0);

/**
 * The reason the document is in review, as the routing rule gives it. A long reason shows
 * its first REASON_PREVIEW_CHARS characters, and "Show more" shows the rest. Nothing for a
 * decision that has no reason (an item placed before routing rules had one).
 */
export const RoutedByReason = ({ routedBy }: { routedBy: Pick<RoutedByLike, 'reason'> }): React.JSX.Element | null => {
  const intl = useIntl();
  const [expanded, setExpanded] = useState(false);
  const reason = (routedBy.reason ?? '').trim();
  if (!reason) return null;
  const preview = shortened(reason, REASON_PREVIEW_CHARS);
  const long = preview !== reason;
  return (
    <Box>
      <span>{expanded ? reason : preview}</span>
      {long && (
        <>
          {' '}
          <Button variant="inline-link" ariaExpanded={expanded} onClick={() => setExpanded((shown) => !shown)}>
            {intl.formatMessage(expanded ? messages.showLess : messages.showMore)}
          </Button>
        </>
      )}
    </Box>
  );
};

const pageList = (failure: PolicyFailure): string => failure.pages.join(', ');

interface KeyedFailure {
  key: string;
  failure: PolicyFailure;
}

/** Each failure with a key of its own: its policy rule id, numbered where an id repeats. */
const keyed = (failures: readonly PolicyFailure[]): KeyedFailure[] => {
  const seen = new Map<string, number>();
  return failures.map((failure) => {
    const earlier = seen.get(failure.ruleId) ?? 0;
    seen.set(failure.ruleId, earlier + 1);
    return { key: earlier ? `${failure.ruleId}#${earlier}` : failure.ruleId, failure };
  });
};

/** One failed policy rule: its name, its policy, and its reason, shortened with the whole of it (and its pages) in a popover. */
const PolicyFailureLine = ({ failure }: { failure: PolicyFailure }): React.JSX.Element => {
  const intl = useIntl();
  const reason = failure.reason.trim() || intl.formatMessage(failureMessages.noReason);
  const preview = shortened(reason, POLICY_FAILURE_REASON_CHARS);
  // The popover holds what the line leaves out: the rest of the reason, and the pages.
  const hasMore = preview !== reason || failure.pages.length > 0;
  return (
    <Box>
      <FormattedMessage
        {...failureMessages.line}
        values={{
          name: failure.name || failure.ruleId,
          policy: failure.policy,
          reason: preview,
          b: (chunks) => <Box variant="strong">{chunks}</Box>,
          detail: (chunks) =>
            hasMore ? (
              <Popover
                triggerType="text-inline"
                dismissButton={false}
                size="large"
                content={
                  <SpaceBetween size="xxs">
                    <Box>{reason}</Box>
                    {failure.pages.length > 0 && (
                      <Box variant="small" color="text-body-secondary">
                        {intl.formatMessage(failureMessages.pages, { pages: pageList(failure) })}
                      </Box>
                    )}
                  </SpaceBetween>
                }
              >
                {chunks}
              </Popover>
            ) : (
              <span>{chunks}</span>
            ),
        }}
      />
    </Box>
  );
};

interface PolicyFailuresProps {
  /** The failed policy rules kept with the review: the first ones the document failed. */
  failures: readonly PolicyFailure[];
  /** How many policy rules the document failed in all. */
  count: number;
}

/** Every failed policy rule kept with the review, in a dialog: the policy rule, its policy, the whole reason and the pages. */
export const PolicyFailuresModal = ({ failures, count, onDismiss }: PolicyFailuresProps & { onDismiss: () => void }): React.JSX.Element => {
  const intl = useIntl();
  const total = Math.max(count, failures.length);
  return (
    <Modal
      visible
      size="large"
      onDismiss={onDismiss}
      header={intl.formatMessage(failureMessages.modalHeader, { count: total })}
      footer={
        <Box float="right">
          <Button variant="primary" onClick={onDismiss}>
            {intl.formatMessage(failureMessages.close)}
          </Button>
        </Box>
      }
    >
      <SpaceBetween size="s">
        {total > failures.length && (
          <Box color="text-body-secondary">
            {intl.formatMessage(failureMessages.notAllStored, { listed: failures.length, count: total })}
          </Box>
        )}
        <Table
          variant="embedded"
          wrapLines
          items={keyed(failures)}
          trackBy="key"
          columnDefinitions={[
            {
              id: 'rule',
              header: intl.formatMessage(failureMessages.columnRule),
              cell: ({ failure }) => failure.name || failure.ruleId,
              isRowHeader: true,
            },
            { id: 'policy', header: intl.formatMessage(failureMessages.columnPolicy), cell: ({ failure }) => failure.policy },
            {
              id: 'reason',
              header: intl.formatMessage(failureMessages.columnReason),
              cell: ({ failure }) => failure.reason.trim() || intl.formatMessage(failureMessages.noReason),
            },
            { id: 'pages', header: intl.formatMessage(failureMessages.columnPages), cell: ({ failure }) => pageList(failure) || '—' },
          ]}
          empty={<Box color="text-body-secondary">{intl.formatMessage(failureMessages.empty)}</Box>}
        />
      </SpaceBetween>
    </Modal>
  );
};

/**
 * The policy rules the document failed, under the routing reason: the first
 * POLICY_FAILURE_PREVIEW of them, one line each, and a button that opens all of them in
 * a dialog when there are more. Nothing for a document that failed none.
 */
export const PolicyFailureList = ({ failures, count }: PolicyFailuresProps): React.JSX.Element | null => {
  const intl = useIntl();
  const [showAll, setShowAll] = useState(false);
  const total = Math.max(count, failures.length);
  if (total === 0) return null;
  const listed = keyed(failures).slice(0, POLICY_FAILURE_PREVIEW);
  return (
    <SpaceBetween size="xxs">
      <Box variant="awsui-key-label">{intl.formatMessage(failureMessages.heading, { count: total })}</Box>
      {listed.map(({ key, failure }) => (
        <PolicyFailureLine key={key} failure={failure} />
      ))}
      {total > listed.length && (
        <Button variant="inline-link" onClick={() => setShowAll(true)}>
          {intl.formatMessage(failureMessages.showAll, { count: total })}
        </Button>
      )}
      {showAll && <PolicyFailuresModal failures={failures} count={total} onDismiss={() => setShowAll(false)} />}
    </SpaceBetween>
  );
};
