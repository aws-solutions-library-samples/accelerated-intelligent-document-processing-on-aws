// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// A queue's Annotators, on the queue's page (Admin): everyone listed in its
// Eligible Annotators, plus anyone still holding its items after being taken off the
// list, with how many of the queue's documents each one is assigned. Shown on a production
// queue's page and a Test Studio queue's page alike.
import React, { useMemo } from 'react';
import { Box, Header, Pagination, Table, TextFilter } from '@cloudscape-design/components';
import { useCollection } from '@cloudscape-design/collection-hooks';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';

import { usePaginationLabels } from '../common/labels';
import { TableNoMatchState, filterCounterText } from '../common/table';

const messages = defineMessages<{
  empty: NoMessageValues;
  loading: NoMessageValues;
  description: NoMessageValues;
  title: NoMessageValues;
  columnAnnotator: NoMessageValues;
  columnAssigned: NoMessageValues;
  notListedHint: NoMessageValues;
  notListed: { readonly email: string };
  find: NoMessageValues;
}>({
  empty: {
    id: 'reviewQueues.annotatorsTable.empty',
    defaultMessage: 'No Annotators are listed on this queue. Add them in Settings.',
    description: "Shown in a queue's Annotators table when the queue lists no Annotator. Settings names the queue page's Settings button.",
  },
  loading: {
    id: 'reviewQueues.annotatorsTable.loading',
    defaultMessage: 'Loading Annotators',
    description: "Shown in a queue's Annotators table while it loads. Annotator is a user role.",
  },
  description: {
    id: 'reviewQueues.annotatorsTable.description',
    defaultMessage: 'The Annotators who work this queue, and the documents assigned to each.',
    description: "Description under the heading of a queue's Annotators table, on the queue's page.",
  },
  title: {
    id: 'reviewQueues.annotatorsTable.title',
    defaultMessage: 'Annotators',
    description: "Heading of a queue's Annotators table, on the queue's page. Annotator is a user role.",
  },
  columnAnnotator: {
    id: 'reviewQueues.annotatorsTable.column.annotator',
    defaultMessage: 'Annotator',
    description: "Column header of a queue's Annotators table: the Annotator's e-mail address.",
  },
  columnAssigned: {
    id: 'reviewQueues.annotatorsTable.column.assigned',
    defaultMessage: 'Assigned documents',
    description: "Column header of a queue's Annotators table: how many of the queue's documents are assigned to the Annotator.",
  },
  notListedHint: {
    id: 'reviewQueues.annotatorsTable.notListedHint',
    defaultMessage: "No longer listed in this queue's Eligible Annotators; still holds its items",
    description:
      "Tooltip of an Annotator in a queue's Annotators table who was taken off the queue's list but still holds some of its items. \"Eligible Annotators\" names a field of the queue's Settings dialog.",
  },
  notListed: {
    id: 'reviewQueues.annotatorsTable.notListed',
    defaultMessage: '{email} (not listed)',
    description:
      "An Annotator in a queue's Annotators table who is no longer listed on the queue but still holds some of its items. {email} is the Annotator's e-mail address.",
  },
  find: {
    id: 'reviewQueues.annotatorsTable.find',
    defaultMessage: 'Find Annotators',
    description: "Placeholder and screen-reader label of the text filter of a queue's Annotators table.",
  },
});

export interface QueueAnnotatorRow {
  email: string;
  assigned: number;
  /** Listed in the queue's Eligible Annotators. */
  listed: boolean;
}

/** One row per Annotator: the listed ones, then any others holding items. */
export const queueAnnotatorRows = (
  eligible: readonly string[],
  reviewers: readonly { email: string; claimed: number }[],
): QueueAnnotatorRow[] => {
  const assigned = new Map(reviewers.map((r) => [r.email.toLowerCase(), r.claimed]));
  const listed = eligible.map((e) => e.toLowerCase());
  const holders = [...assigned.keys()].filter((e) => !listed.includes(e));
  return [
    ...listed.map((email) => ({ email, assigned: assigned.get(email) ?? 0, listed: true })),
    ...holders.map((email) => ({ email, assigned: assigned.get(email) ?? 0, listed: false })),
  ];
};

interface QueueAnnotatorsTableProps {
  rows: QueueAnnotatorRow[];
  loading: boolean;
}

const QueueAnnotatorsTable = ({ rows, loading }: QueueAnnotatorsTableProps): React.JSX.Element => {
  const intl = useIntl();
  const paginationLabels = usePaginationLabels();
  const sorted = useMemo(() => rows, [rows]);
  const { items, actions, filteredItemsCount, collectionProps, filterProps, paginationProps } = useCollection(sorted, {
    filtering: {
      empty: (
        <Box textAlign="center" color="inherit" padding="s">
          <FormattedMessage {...messages.empty} />
        </Box>
      ),
      noMatch: <TableNoMatchState onClearFilter={() => actions.setFiltering('')} />,
      filteringFunction: (row, text) => row.email.includes(text.toLowerCase()),
    },
    pagination: { pageSize: 10 },
    sorting: {},
  });

  return (
    <Table<QueueAnnotatorRow>
      {...collectionProps}
      variant="container"
      items={items}
      trackBy="email"
      loading={loading}
      loadingText={intl.formatMessage(messages.loading)}
      header={
        <Header counter={loading ? undefined : `(${rows.length})`} description={intl.formatMessage(messages.description)}>
          <FormattedMessage {...messages.title} />
        </Header>
      }
      columnDefinitions={[
        {
          id: 'annotator',
          header: intl.formatMessage(messages.columnAnnotator),
          sortingField: 'email',
          cell: (row) =>
            row.listed ? (
              row.email
            ) : (
              <span title={intl.formatMessage(messages.notListedHint)}>{intl.formatMessage(messages.notListed, { email: row.email })}</span>
            ),
        },
        { id: 'assigned', header: intl.formatMessage(messages.columnAssigned), sortingField: 'assigned', cell: (row) => row.assigned },
      ]}
      filter={
        <TextFilter
          {...filterProps}
          filteringPlaceholder={intl.formatMessage(messages.find)}
          filteringAriaLabel={intl.formatMessage(messages.find)}
          countText={filterCounterText(intl, filteredItemsCount ?? 0)}
        />
      }
      pagination={<Pagination {...paginationProps} ariaLabels={paginationLabels} />}
    />
  );
};

export default QueueAnnotatorsTable;
