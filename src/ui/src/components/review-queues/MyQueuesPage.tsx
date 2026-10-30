// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Home › Queues (Annotators): the queues that list the caller as an Annotator —
// production review queues (getMyReviewQueues) and Test Studio queues
// (listTestSetQueues, which returns an Annotator only the sets that list them).
// Opening a queue lists its items; any item opens read-only, and Claim assigns one
// to the caller so it shows in Home › Tasks.
import React, { useCallback, useMemo, useState } from 'react';
import { ConsoleLogger } from 'aws-amplify/utils';
import { useCollection } from '@cloudscape-design/collection-hooks';
import type { TableProps } from '@cloudscape-design/components';
import { Alert, Badge, Box, Button, ContentLayout, Header, Link, Pagination, Table, TextFilter } from '@cloudscape-design/components';

import usePolling from '../../hooks/use-polling';
import { useOptionalReviewWork } from '../../hooks/use-review-work';
import { describeApiError } from '../../hooks/utils/graphql-error';
import { formatDateTime, formatRelativeTime } from '../../utils/date-time';
import { productionQueuePath, testSetQueuePath } from '../../routes/constants';
import type { ReviewQueueSummary, TestSetQueueSummary } from '../../graphql/generated/operation-types';
import { paginationLabels } from '../common/labels';
import { TableNoMatchState, getFilterCounterText } from '../common/table';
import { QUEUES_POLL_INTERVAL_MS } from './QueuesPage';
import { fetchMyReviewQueues } from './review-api';
import { listTestSetQueues } from './reviewer-assignment';
import { SOURCE_LABELS } from './work-items';
import type { WorkSource } from './work-items';
import useUserRole from '../../hooks/use-user-role';

const logger = new ConsoleLogger('MyQueuesPage');

export interface MyQueueRow {
  id: string;
  source: WorkSource;
  name: string;
  href: string;
  description: string | null;
  /** Items in the queue (production: still to review; Test Studio: documents in the set). */
  items: number | null;
  /** Items no one is assigned to yet: the ones that can be claimed. */
  pending: number | null;
  /** Items assigned to the caller. */
  mine: number | null;
  priority: number | null;
  oldestEnqueuedAt: string | null;
}

export const fromProductionQueue = (q: ReviewQueueSummary): MyQueueRow => ({
  id: `p:${q.name}`,
  source: 'production',
  name: q.displayName || q.name,
  href: `#${productionQueuePath(q.name)}`,
  description: q.description ?? null,
  items: q.depth,
  pending: q.unclaimed,
  mine: q.claimedByMe,
  priority: q.priority,
  oldestEnqueuedAt: q.oldestEnqueuedAt ?? null,
});

export const fromTestSetQueue = (q: TestSetQueueSummary, mine: number | null): MyQueueRow => ({
  id: `t:${q.id}`,
  source: 'test-set',
  name: q.name || q.id,
  href: `#${testSetQueuePath(q.id)}`,
  description: q.description ?? null,
  items: q.fileCount ?? null,
  pending: q.pendingDocs ?? null,
  mine,
  priority: q.priority ?? null,
  oldestEnqueuedAt: null,
});

const count = (n: number | null): React.ReactNode => (n == null ? '—' : n);
const byNumber = (pick: (r: MyQueueRow) => number | null) => (a: MyQueueRow, b: MyQueueRow) => (pick(a) ?? -1) - (pick(b) ?? -1);

const COLUMNS: TableProps.ColumnDefinition<MyQueueRow>[] = [
  {
    id: 'name',
    header: 'Queue',
    isRowHeader: true,
    sortingField: 'name',
    cell: (row) => (
      <Link href={row.href} variant="primary">
        {row.name}
      </Link>
    ),
  },
  {
    id: 'source',
    header: 'Source',
    width: 140,
    sortingField: 'source',
    cell: (row) => <Badge color={row.source === 'production' ? 'blue' : 'grey'}>{SOURCE_LABELS[row.source]}</Badge>,
  },
  { id: 'items', header: 'Items', cell: (row) => count(row.items), sortingComparator: byNumber((r) => r.items) },
  { id: 'pending', header: 'Pending', cell: (row) => count(row.pending), sortingComparator: byNumber((r) => r.pending) },
  { id: 'mine', header: 'Assigned to you', cell: (row) => count(row.mine), sortingComparator: byNumber((r) => r.mine) },
  {
    id: 'oldest',
    header: 'Oldest waiting',
    cell: (row) =>
      row.oldestEnqueuedAt ? <span title={formatDateTime(row.oldestEnqueuedAt)}>{formatRelativeTime(row.oldestEnqueuedAt)}</span> : '—',
  },
  { id: 'priority', header: 'Priority', cell: (row) => row.priority ?? '—', sortingComparator: byNumber((r) => r.priority) },
];

const MyQueuesPage = (): React.JSX.Element => {
  const reviewWork = useOptionalReviewWork();
  const { isAnnotator } = useUserRole();
  const [production, setProduction] = useState<ReviewQueueSummary[] | null>(null);
  const [testSets, setTestSets] = useState<TestSetQueueSummary[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    // Test Studio queues are Annotators'; a Reviewer works the Production queues only.
    const [mine, sets] = await Promise.allSettled([fetchMyReviewQueues(1), isAnnotator ? listTestSetQueues() : Promise.resolve([])]);
    if (mine.status === 'fulfilled') setProduction(mine.value.queues);
    if (sets.status === 'fulfilled') setTestSets(sets.value.filter((t) => (t.fileCount ?? 0) > 0));
    const failure = mine.status === 'rejected' ? mine.reason : sets.status === 'rejected' ? sets.reason : null;
    if (failure) logger.error('loading my queues failed', failure);
    setError(failure);
  }, [isAnnotator]);

  usePolling(load, { enabled: true, intervalMs: QUEUES_POLL_INTERVAL_MS, immediate: true });

  const rows = useMemo(() => {
    const tasksPerSet = new Map<string, number>();
    for (const t of reviewWork?.testSetTasks ?? []) {
      if (t.testSetId) tasksPerSet.set(t.testSetId, (tasksPerSet.get(t.testSetId) ?? 0) + 1);
    }
    const mineIn = (id: string): number | null => (reviewWork?.testSetTasks ? (tasksPerSet.get(id) ?? 0) : null);
    return [...(production ?? []).map(fromProductionQueue), ...(testSets ?? []).map((t) => fromTestSetQueue(t, mineIn(t.id)))].sort(
      (a, b) => (b.priority ?? 0) - (a.priority ?? 0) || a.name.localeCompare(b.name),
    );
  }, [production, testSets, reviewWork?.testSetTasks]);

  const {
    items,
    actions: collectionActions,
    filteredItemsCount,
    collectionProps,
    filterProps,
    paginationProps,
  } = useCollection(rows, {
    filtering: {
      empty: (
        <Box textAlign="center" color="inherit" padding="s">
          No queues list you yet. An Admin adds you to a queue&apos;s Eligible Annotators in Review Management; its queue then appears here.
        </Box>
      ),
      noMatch: <TableNoMatchState onClearFilter={() => collectionActions.setFiltering('')} />,
      filteringFunction: (row, text) =>
        [row.name, row.description ?? '', SOURCE_LABELS[row.source]].some((v) => v.toLowerCase().includes(text.toLowerCase())),
    },
    pagination: { pageSize: 25 },
    sorting: {},
  });

  const refresh = async () => {
    setRefreshing(true);
    try {
      await load();
    } finally {
      setRefreshing(false);
    }
  };

  const loaded = production != null || testSets != null;
  const initial = !loaded && error == null;

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description="The queues you are listed on. Open a queue to see its items; open any item to view it, and claim one to work it in Tasks."
          actions={
            <Button iconName="refresh" ariaLabel="Refresh queues" loading={refreshing} onClick={() => void refresh()}>
              Refresh
            </Button>
          }
        >
          Queues
        </Header>
      }
    >
      {error != null && (
        <Box margin={{ bottom: 'l' }}>
          <Alert
            type={loaded ? 'warning' : 'error'}
            header={loaded ? 'Showing the last loaded queues' : "Couldn't load your queues"}
            action={<Button onClick={() => void refresh()}>Retry</Button>}
          >
            {describeApiError(error, 'load your queues')}
          </Alert>
        </Box>
      )}
      <Table<MyQueueRow>
        {...collectionProps}
        variant="container"
        items={items}
        trackBy="id"
        columnDefinitions={COLUMNS}
        loading={initial || (refreshing && rows.length === 0)}
        loadingText="Loading queues"
        header={<Header counter={initial ? undefined : `(${rows.length})`}>Your queues</Header>}
        filter={
          <TextFilter
            {...filterProps}
            filteringPlaceholder="Find queues"
            filteringAriaLabel="Find queues"
            countText={getFilterCounterText(filteredItemsCount ?? 0)}
          />
        }
        pagination={<Pagination {...paginationProps} ariaLabels={paginationLabels} />}
      />
    </ContentLayout>
  );
};

export default MyQueuesPage;
