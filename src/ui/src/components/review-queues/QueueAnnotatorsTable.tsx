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

import { paginationLabels } from '../common/labels';
import { TableNoMatchState, getFilterCounterText } from '../common/table';

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
  const sorted = useMemo(() => rows, [rows]);
  const { items, actions, filteredItemsCount, collectionProps, filterProps, paginationProps } = useCollection(sorted, {
    filtering: {
      empty: (
        <Box textAlign="center" color="inherit" padding="s">
          No Annotators are listed on this queue. Add them in Settings.
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
      loadingText="Loading Annotators"
      header={
        <Header
          counter={loading ? undefined : `(${rows.length})`}
          description="The Annotators who work this queue, and the documents assigned to each."
        >
          Annotators
        </Header>
      }
      columnDefinitions={[
        {
          id: 'annotator',
          header: 'Annotator',
          sortingField: 'email',
          cell: (row) =>
            row.listed ? (
              row.email
            ) : (
              <span title="No longer listed in this queue's Eligible Annotators; still holds its items">{`${row.email} (not listed)`}</span>
            ),
        },
        { id: 'assigned', header: 'Assigned documents', sortingField: 'assigned', cell: (row) => row.assigned },
      ]}
      filter={
        <TextFilter
          {...filterProps}
          filteringPlaceholder="Find Annotators"
          filteringAriaLabel="Find Annotators"
          countText={getFilterCounterText(filteredItemsCount ?? 0)}
        />
      }
      pagination={<Pagination {...paginationProps} ariaLabels={paginationLabels} />}
    />
  );
};

export default QueueAnnotatorsTable;
