// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Home › Queues (Annotators): the queues that list the caller as an Annotator —
// production review queues (getMyReviewQueues) and Test Studio queues
// (listTestSetQueues, which returns an Annotator only the sets that list them). A
// Reviewer works Production queues only, so a caller who works no test set makes no
// listTestSetQueues call.
// Opening a queue lists its items; any item opens read-only, and Claim assigns one
// to the caller so it shows in Home › Tasks.
import React, { useCallback, useMemo, useState } from 'react';
import { ConsoleLogger } from 'aws-amplify/utils';
import { useCollection } from '@cloudscape-design/collection-hooks';
import type { TableProps } from '@cloudscape-design/components';
import { Alert, Badge, Box, Button, ContentLayout, Header, Link, Pagination, Table, TextFilter } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';

import usePolling from '../../hooks/use-polling';
import useUserRole from '../../hooks/use-user-role';
import { useOptionalReviewWork } from '../../hooks/use-review-work';
import { describeApiError } from '../../hooks/utils/graphql-error';
import { useFormatters } from '../../i18n/format';
import { formatDateTime, formatRelativeTime, relativeTimeMessages } from '../../utils/date-time';
import { productionQueuePath, testSetQueuePath } from '../../routes/constants';
import type { ReviewQueueSummary, TestSetQueueSummary } from '../../graphql/generated/operation-types';
import { usePaginationLabels } from '../common/labels';
import { TableNoMatchState, filterCounterText } from '../common/table';
import { QUEUES_POLL_INTERVAL_MS } from './QueuesPage';
import { fetchMyReviewQueues } from './review-api';
import { listTestSetQueues } from './reviewer-assignment';
import { workSourceLabel } from './work-items';
import type { WorkSource } from './work-items';

const logger = new ConsoleLogger('MyQueuesPage');

const messages = defineMessages<{
  title: NoMessageValues;
  description: NoMessageValues;
  refreshAriaLabel: NoMessageValues;
  refresh: NoMessageValues;
  loadedWarning: NoMessageValues;
  loadError: NoMessageValues;
  loadQueuesAction: NoMessageValues;
  retry: NoMessageValues;
  loading: NoMessageValues;
  tableTitle: NoMessageValues;
  find: NoMessageValues;
  empty: NoMessageValues;
  columnQueue: NoMessageValues;
  columnSource: NoMessageValues;
  columnItems: NoMessageValues;
  columnPending: NoMessageValues;
  columnMine: NoMessageValues;
  columnOldest: NoMessageValues;
  columnPriority: NoMessageValues;
}>({
  title: {
    id: 'reviewQueues.myQueues.title',
    defaultMessage: 'Queues',
    description: 'Heading of Home › Queues, the review queues that list the reader as an Annotator.',
  },
  description: {
    id: 'reviewQueues.myQueues.description',
    defaultMessage:
      'The queues you are listed on. Open a queue to see its items; open any item to view it, and claim one to work it in Tasks.',
    description: 'Description under the Home › Queues heading. Tasks is the name of the Home › Tasks page.',
  },
  refreshAriaLabel: {
    id: 'reviewQueues.myQueues.refreshAriaLabel',
    defaultMessage: 'Refresh queues',
    description: 'Screen-reader label of the Refresh button on Home › Queues.',
  },
  refresh: {
    id: 'reviewQueues.myQueues.refresh',
    defaultMessage: 'Refresh',
    description: 'Button on Home › Queues that reloads the queues.',
  },
  loadedWarning: {
    id: 'reviewQueues.myQueues.loadedWarning',
    defaultMessage: 'Showing the last loaded queues',
    description: 'Header of the warning on Home › Queues when refreshing the queues failed but earlier ones are still shown.',
  },
  loadError: {
    id: 'reviewQueues.myQueues.loadError',
    defaultMessage: "Couldn't load your queues",
    description: 'Header of the error on Home › Queues when the queues could not be loaded at all.',
  },
  loadQueuesAction: {
    id: 'reviewQueues.myQueues.loadQueuesAction',
    defaultMessage: 'load your queues',
    description:
      'Action phrase completing an API error sentence ("Failed to …: <error>" / "You do not have permission to …") when the queues on Home › Queues cannot be loaded.',
  },
  retry: {
    id: 'reviewQueues.myQueues.retry',
    defaultMessage: 'Retry',
    description: 'Button of the Home › Queues load warning that loads the queues again.',
  },
  loading: {
    id: 'reviewQueues.myQueues.loading',
    defaultMessage: 'Loading queues',
    description: 'Shown in the Home › Queues table while the queues load.',
  },
  tableTitle: {
    id: 'reviewQueues.myQueues.tableTitle',
    defaultMessage: 'Your queues',
    description: 'Title of the table on Home › Queues.',
  },
  find: {
    id: 'reviewQueues.myQueues.find',
    defaultMessage: 'Find queues',
    description: "Placeholder and screen-reader label of the Home › Queues table's text filter.",
  },
  empty: {
    id: 'reviewQueues.myQueues.empty',
    defaultMessage:
      "No queues list you yet. An Admin adds you to a queue's Eligible Annotators in Review Management; its queue then appears here.",
    description:
      'Shown in the Home › Queues table when no queue lists the reader. Eligible Annotators names a setting of a queue, and Review Management a page of the app.',
  },
  columnQueue: {
    id: 'reviewQueues.myQueues.column.queue',
    defaultMessage: 'Queue',
    description: "Column header of the Home › Queues table: the queue's name, as a link to its page.",
  },
  columnSource: {
    id: 'reviewQueues.myQueues.column.source',
    defaultMessage: 'Source',
    description: 'Column header of the Home › Queues table: whether the queue is a production review queue or a Test Studio test set.',
  },
  columnItems: {
    id: 'reviewQueues.myQueues.column.items',
    defaultMessage: 'Items',
    description: 'Column header of the Home › Queues table: how many items the queue holds.',
  },
  columnPending: {
    id: 'reviewQueues.myQueues.column.pending',
    defaultMessage: 'Pending',
    description: 'Column header of the Home › Queues table: how many of its items no one is assigned to yet.',
  },
  columnMine: {
    id: 'reviewQueues.myQueues.column.mine',
    defaultMessage: 'Assigned to you',
    description: 'Column header of the Home › Queues table: how many of its items are assigned to the reader.',
  },
  columnOldest: {
    id: 'reviewQueues.myQueues.column.oldest',
    defaultMessage: 'Oldest waiting',
    description: 'Column header of the Home › Queues table: how long its oldest waiting item has waited.',
  },
  columnPriority: {
    id: 'reviewQueues.myQueues.column.priority',
    defaultMessage: 'Priority',
    description: "Column header of the Home › Queues table: the queue's priority, a number.",
  },
});

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
// One instance each, whatever the language, so a sorted table stays sorted when it changes.
const byItems = byNumber((r) => r.items);
const byPending = byNumber((r) => r.pending);
const byMine = byNumber((r) => r.mine);
const byPriority = byNumber((r) => r.priority);

/** The oldest waiting time in the UI locale; a column's `cell` cannot call a hook, so this is a component. */
const OldestWaitingCell = ({ at }: { at: string }): React.JSX.Element => {
  const intl = useIntl();
  const { locale } = useFormatters();
  return (
    <span title={formatDateTime(at, locale)}>
      {formatRelativeTime(at, locale, undefined, intl.formatMessage(relativeTimeMessages.justNow))}
    </span>
  );
};

const MyQueuesPage = (): React.JSX.Element => {
  const intl = useIntl();
  const paginationLabels = usePaginationLabels();
  const reviewWork = useOptionalReviewWork();
  const { canWorkTestSetQueues, loading: roleLoading } = useUserRole();
  const [production, setProduction] = useState<ReviewQueueSummary[] | null>(null);
  const [testSets, setTestSets] = useState<TestSetQueueSummary[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [refreshing, setRefreshing] = useState(false);

  const columns = useMemo<TableProps.ColumnDefinition<MyQueueRow>[]>(
    () => [
      {
        id: 'name',
        header: intl.formatMessage(messages.columnQueue),
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
        header: intl.formatMessage(messages.columnSource),
        width: 140,
        sortingField: 'source',
        cell: (row) => <Badge color={row.source === 'production' ? 'blue' : 'grey'}>{workSourceLabel(row.source, intl)}</Badge>,
      },
      { id: 'items', header: intl.formatMessage(messages.columnItems), cell: (row) => count(row.items), sortingComparator: byItems },
      {
        id: 'pending',
        header: intl.formatMessage(messages.columnPending),
        cell: (row) => count(row.pending),
        sortingComparator: byPending,
      },
      { id: 'mine', header: intl.formatMessage(messages.columnMine), cell: (row) => count(row.mine), sortingComparator: byMine },
      {
        id: 'oldest',
        header: intl.formatMessage(messages.columnOldest),
        cell: (row) => (row.oldestEnqueuedAt ? <OldestWaitingCell at={row.oldestEnqueuedAt} /> : '—'),
      },
      {
        id: 'priority',
        header: intl.formatMessage(messages.columnPriority),
        cell: (row) => row.priority ?? '—',
        sortingComparator: byPriority,
      },
    ],
    [intl],
  );

  const load = useCallback(async () => {
    const [mine, sets] = await Promise.allSettled([
      fetchMyReviewQueues(1),
      canWorkTestSetQueues ? listTestSetQueues() : Promise.resolve<TestSetQueueSummary[]>([]),
    ]);
    if (mine.status === 'fulfilled') setProduction(mine.value.queues);
    if (sets.status === 'fulfilled') setTestSets(sets.value.filter((t) => (t.fileCount ?? 0) > 0));
    const failure = mine.status === 'rejected' ? mine.reason : sets.status === 'rejected' ? sets.reason : null;
    if (failure) logger.error('loading my queues failed', failure);
    setError(failure);
  }, [canWorkTestSetQueues]);

  // Waits for the role, so the first load already knows whether to list test-set queues.
  usePolling(load, { enabled: !roleLoading, intervalMs: QUEUES_POLL_INTERVAL_MS, immediate: true });

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
          <FormattedMessage {...messages.empty} />
        </Box>
      ),
      noMatch: <TableNoMatchState onClearFilter={() => collectionActions.setFiltering('')} />,
      filteringFunction: (row, text) =>
        [row.name, row.description ?? '', workSourceLabel(row.source, intl)].some((v) => v.toLowerCase().includes(text.toLowerCase())),
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
          description={intl.formatMessage(messages.description)}
          actions={
            <Button
              iconName="refresh"
              ariaLabel={intl.formatMessage(messages.refreshAriaLabel)}
              loading={refreshing}
              onClick={() => void refresh()}
            >
              <FormattedMessage {...messages.refresh} />
            </Button>
          }
        >
          <FormattedMessage {...messages.title} />
        </Header>
      }
    >
      {error != null && (
        <Box margin={{ bottom: 'l' }}>
          <Alert
            type={loaded ? 'warning' : 'error'}
            header={intl.formatMessage(loaded ? messages.loadedWarning : messages.loadError)}
            action={
              <Button onClick={() => void refresh()}>
                <FormattedMessage {...messages.retry} />
              </Button>
            }
          >
            {describeApiError(error, messages.loadQueuesAction, intl)}
          </Alert>
        </Box>
      )}
      <Table<MyQueueRow>
        {...collectionProps}
        variant="container"
        items={items}
        trackBy="id"
        columnDefinitions={columns}
        loading={initial || (refreshing && rows.length === 0)}
        loadingText={intl.formatMessage(messages.loading)}
        header={
          <Header counter={initial ? undefined : `(${rows.length})`}>
            <FormattedMessage {...messages.tableTitle} />
          </Header>
        }
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
    </ContentLayout>
  );
};

export default MyQueuesPage;
