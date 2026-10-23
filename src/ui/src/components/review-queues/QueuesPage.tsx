// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// HITL › Queue Management (Admin): Admins edit the Default queue's settings and see
// every queue here, along with the Test Studio queues, one per test set.
// Open a queue to assign its items to Annotators, unassign, move or remove them.
// Deleting a queue moves its items to the Default queue.
import React, { useCallback, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ConsoleLogger } from 'aws-amplify/utils';
import { useCollection } from '@cloudscape-design/collection-hooks';
import type { ButtonDropdownProps, TableProps } from '@cloudscape-design/components';
import {
  Alert,
  Box,
  Button,
  ButtonDropdown,
  ContentLayout,
  Header,
  Link,
  Modal,
  Pagination,
  SpaceBetween,
  StatusIndicator,
  Table,
  TextFilter,
} from '@cloudscape-design/components';

import type { ReviewQueueDefinition, TestSetQueueSummary, ReviewQueueHealth } from '../../graphql/generated/operation-types';
import useAppContext from '../../contexts/app';
import usePolling from '../../hooks/use-polling';
import { describeApiError } from '../../hooks/utils/graphql-error';
import { formatDateTime } from '../../utils/date-time';
import { productionQueuePath, testSetQueuePath } from '../../routes/constants';
import { paginationLabels } from '../common/labels';
import { NoDataState, TableNoMatchState, getFilterCounterText } from '../common/table';
import QueueDefinitionModal from './QueueDefinitionModal';
import type { TestSetQueueTarget } from './QueueDefinitionModal';
import { fetchQueuesOverview, reviewErrorCode } from './review-api';
import { deleteReviewQueue, listAnnotatorEmails, listReviewQueues, listTestSetQueues } from './reviewer-assignment';

const logger = new ConsoleLogger('QueuesPage');

export const QUEUES_POLL_INTERVAL_MS = 20_000;

interface ProductionRow {
  id: string;
  name: string;
  href: string;
  /** Null for a queue that still holds items but is no longer defined. */
  definition: ReviewQueueDefinition | null;
  health?: ReviewQueueHealth;
}

interface TestSetRow {
  id: string;
  name: string;
  href: string;
  ref: TestSetQueueSummary;
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`;

/** Why a queue can't be deleted, or undefined when it can. A queue that holds items can: they move to Default. */
export const deleteDisabledReason = (definition: ReviewQueueDefinition | null): string | undefined =>
  !definition ? 'This queue is not defined' : definition.isDefaultQueue ? "The default queue can't be deleted" : undefined;

/** The delete confirmation text, naming where a non-empty queue's items go. */
export const deleteConfirmText = (queue: { id: string; name: string }, depth: number): string =>
  `Delete the queue ${queue.name} (${queue.id})? This can't be undone.` +
  (depth > 0 ? ` ${depth === 1 ? '1 item' : `${depth} items`} will move to the Default queue.` : '');

/**
 * How many Annotators are listed on a queue (its Eligible Annotators, from its
 * Settings), with their names on hover. Only they work the queue; none listed means
 * only Admins do.
 */
export const AnnotatorsCell = ({ eligible }: { eligible: readonly string[] | null | undefined }): React.JSX.Element => {
  const names = eligible ?? [];
  return <span title={names.length ? names.join(', ') : 'No Annotators listed'}>{names.length}</span>;
};

const QueuesPage = (): React.JSX.Element => {
  const navigate = useNavigate();
  const { setSuccessMessage } = useAppContext();

  const [definitions, setDefinitions] = useState<ReviewQueueDefinition[] | null>(null);
  const [overview, setOverview] = useState<ReviewQueueHealth[] | null>(null);
  const [productionError, setProductionError] = useState<unknown>(null);
  const [productionLoading, setProductionLoading] = useState(false);
  const [testSetRefs, setTestSetRefs] = useState<TestSetQueueSummary[] | null>(null);
  const [testSetError, setTestSetError] = useState<unknown>(null);
  const [listingTestSets, setListingTestSets] = useState(false);
  const [annotators, setAnnotators] = useState<string[] | null>(null);
  const [annotatorsLoading, setAnnotatorsLoading] = useState(false);
  const [editing, setEditing] = useState<{ queue: ReviewQueueDefinition } | null>(null);
  const [deleting, setDeleting] = useState<{ queue: ReviewQueueDefinition; depth: number } | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [settingsTarget, setSettingsTarget] = useState<TestSetQueueTarget | null>(null);

  // Production queues are cheap (two calls) and polled.
  const loadProduction = useCallback(async () => {
    setProductionLoading(true);
    try {
      const [defs, ov] = await Promise.all([
        listReviewQueues(),
        fetchQueuesOverview().catch((err: unknown) => {
          logger.warn('getReviewQueuesOverview failed', err);
          return null;
        }),
      ]);
      setDefinitions(defs);
      setOverview(ov?.queues ?? null);
      setProductionError(null);
    } catch (err) {
      logger.error('listReviewQueues failed', err);
      setProductionError(err);
    } finally {
      setProductionLoading(false);
    }
  }, []);

  usePolling(loadProduction, { enabled: true, intervalMs: QUEUES_POLL_INTERVAL_MS, immediate: true });

  // Test Studio queues: one DynamoDB-only call returns every set with its stored
  // counts (kept by the background summary build, assignments and reviews), so the
  // list is as cheap as the production one and polled with it.
  const loadTestSets = useCallback(async () => {
    setListingTestSets(true);
    try {
      const sets = await listTestSetQueues();
      setTestSetRefs(sets.filter((t) => (t.fileCount ?? 0) > 0).sort((a, b) => a.name.localeCompare(b.name)));
      setTestSetError(null);
    } catch (err) {
      logger.error('listTestSetQueues failed', err);
      setTestSetError(err);
    } finally {
      setListingTestSets(false);
    }
  }, []);

  usePolling(loadTestSets, { enabled: true, intervalMs: QUEUES_POLL_INTERVAL_MS, immediate: true });

  const loadAnnotators = useCallback(async () => {
    if (annotators != null || annotatorsLoading) return;
    setAnnotatorsLoading(true);
    try {
      setAnnotators(await listAnnotatorEmails());
    } catch (err) {
      logger.warn('listUsers failed', err);
      setAnnotators([]);
    } finally {
      setAnnotatorsLoading(false);
    }
  }, [annotators, annotatorsLoading]);

  const refresh = useCallback(async () => {
    await Promise.all([loadProduction(), loadTestSets()]);
  }, [loadProduction, loadTestSets]);

  // ------------------------------------------------------------- rows
  const productionRows: ProductionRow[] = useMemo(() => {
    const health = new Map((overview ?? []).map((h) => [h.summary.name, h]));
    const rows: ProductionRow[] = (definitions ?? []).map((d) => ({
      id: d.id,
      name: d.name || d.id,
      href: `#${productionQueuePath(d.id)}`,
      definition: d,
      health: health.get(d.id),
    }));
    const defined = new Set(rows.map((r) => r.id));
    for (const h of overview ?? []) {
      if (defined.has(h.summary.name) || h.summary.depth === 0) continue;
      rows.push({
        id: h.summary.name,
        name: h.summary.displayName || h.summary.name,
        href: `#${productionQueuePath(h.summary.name)}`,
        definition: null,
        health: h,
      });
    }
    return rows.sort((a, b) => (b.definition?.priority ?? 0) - (a.definition?.priority ?? 0) || a.name.localeCompare(b.name));
  }, [definitions, overview]);

  const testSetRows: TestSetRow[] = useMemo(
    () =>
      (testSetRefs ?? []).map((ref) => ({
        id: ref.id,
        name: ref.name || ref.id,
        href: `#${testSetQueuePath(ref.id)}`,
        ref,
      })),
    [testSetRefs],
  );

  const production = useCollection(productionRows, {
    filtering: {
      empty: (
        <Box textAlign="center" color="inherit" padding="s">
          No queues found. The Default queue is created automatically; reload the page.
        </Box>
      ),
      noMatch: <TableNoMatchState onClearFilter={() => production.actions.setFiltering('')} />,
      filteringFunction: (row, text) => [row.name, row.id].some((v) => v.toLowerCase().includes(text.toLowerCase())),
    },
    pagination: { pageSize: 25 },
    sorting: {},
  });

  const testStudio = useCollection(testSetRows, {
    filtering: {
      empty: <NoDataState />,
      noMatch: <TableNoMatchState onClearFilter={() => testStudio.actions.setFiltering('')} />,
      filteringFunction: (row, text) => [row.name, row.id].some((v) => v.toLowerCase().includes(text.toLowerCase())),
    },
    pagination: { pageSize: 25 },
    sorting: {},
  });

  // ------------------------------------------------------------- actions
  const openEditor = (queue: ReviewQueueDefinition) => {
    setEditing({ queue });
    void loadAnnotators();
  };

  const confirmDelete = async () => {
    if (!deleting) return;
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      await deleteReviewQueue(deleting.queue.id);
      setSuccessMessage(
        deleting.depth > 0
          ? `Deleted the queue ${deleting.queue.name}; its items moved to the Default queue.`
          : `Deleted the queue ${deleting.queue.name}.`,
      );
      setDeleting(null);
      await loadProduction();
    } catch (err) {
      setDeleteError(
        reviewErrorCode(err) === 'QUEUE_FINALIZING'
          ? 'An item in this queue is being completed. Try again in a minute.'
          : describeApiError(err, 'delete the queue'),
      );
    } finally {
      setDeleteBusy(false);
    }
  };

  const productionMenu = (row: ProductionRow): ButtonDropdownProps.Item[] => {
    const d = row.definition;
    const deleteReason = deleteDisabledReason(d);
    return [
      { id: 'view', text: 'View' },
      { id: 'edit', text: 'Settings', disabled: !d, disabledReason: d ? undefined : 'This queue is not defined' },
      { id: 'delete', text: 'Delete', disabled: !!deleteReason, disabledReason: deleteReason },
    ];
  };

  const onProductionAction = (row: ProductionRow, action: string) => {
    if (action === 'view') navigate(row.href.replace(/^#/, ''));
    else if (action === 'edit' && row.definition) openEditor(row.definition);
    else if (action === 'delete' && row.definition) {
      setDeleteError(null);
      setDeleting({ queue: row.definition, depth: row.health?.summary.depth ?? 0 });
    }
  };

  const onTestSetAction = (row: TestSetRow, action: string) => {
    if (action === 'view') navigate(row.href.replace(/^#/, ''));
    else if (action === 'settings') {
      void loadAnnotators();
      setSettingsTarget({
        testSetId: row.ref.id,
        name: row.name,
        description: row.ref.description,
        priority: row.ref.priority,
        claimLeaseMinutes: row.ref.claimLeaseMinutes,
        eligibleAnnotators: row.ref.eligibleAnnotators,
        checklist: row.ref.checklist,
        skipReasons: row.ref.skipReasons,
      });
    }
  };

  // -------------------------------------------------------------- render
  const countCell = (n: number | undefined) => (n == null ? '—' : n);
  const eligibleCount = (eligible: readonly string[] | null | undefined): number => eligible?.length ?? 0;

  const productionColumns: TableProps.ColumnDefinition<ProductionRow>[] = [
    {
      id: 'name',
      header: 'Queue',
      isRowHeader: true,
      sortingField: 'name',
      cell: (row) => (
        <SpaceBetween size="xxxs">
          <Link href={row.href} variant="primary">
            {row.name}
          </Link>
          {!row.definition && (
            <Box variant="small" color="text-status-warning">
              Not defined — move its items to a defined queue
            </Box>
          )}
        </SpaceBetween>
      ),
    },
    {
      id: 'items',
      header: 'Items',
      cell: (row) => countCell(row.health?.summary.depth),
      sortingComparator: (a, b) => (a.health?.summary.depth ?? -1) - (b.health?.summary.depth ?? -1),
    },
    {
      id: 'pending',
      header: 'Pending',
      cell: (row) => countCell(row.health?.summary.unclaimed),
      sortingComparator: (a, b) => (a.health?.summary.unclaimed ?? -1) - (b.health?.summary.unclaimed ?? -1),
    },
    {
      id: 'assigned',
      header: 'Assigned',
      cell: (row) => countCell(row.health?.summary.claimed),
      sortingComparator: (a, b) => (a.health?.summary.claimed ?? -1) - (b.health?.summary.claimed ?? -1),
    },
    {
      id: 'annotators',
      header: 'Annotators',
      cell: (row) => <AnnotatorsCell eligible={row.definition?.eligibleAnnotators} />,
      sortingComparator: (a, b) => eligibleCount(a.definition?.eligibleAnnotators) - eligibleCount(b.definition?.eligibleAnnotators),
    },
    {
      id: 'actions',
      header: <span className="idp-visually-hidden">Actions</span>,
      width: 60,
      cell: (row) => (
        <ButtonDropdown
          variant="inline-icon"
          ariaLabel={`Actions for ${row.name}`}
          expandToViewport
          items={productionMenu(row)}
          onItemClick={({ detail }) => onProductionAction(row, detail.id)}
        />
      ),
    },
  ];

  const preparing = (row: TestSetRow): boolean => row.ref.summaryStatus === 'BUILDING';
  /** A stored count, or "Preparing…" while the set's summaries are built. */
  const testSetCount = (row: TestSetRow, n: number | null | undefined): React.ReactNode =>
    n != null ? (
      <span title={row.ref.queueCountsAt ? `Counted ${formatDateTime(row.ref.queueCountsAt)}` : undefined}>{n}</span>
    ) : preparing(row) ? (
      'Preparing…'
    ) : (
      '—'
    );

  /** Every assigned document, started or not, so Pending + Assigned covers the open work. */
  const testSetAssigned = (row: TestSetRow): number | null =>
    row.ref.assignedDocs == null && row.ref.inProgressDocs == null ? null : (row.ref.assignedDocs ?? 0) + (row.ref.inProgressDocs ?? 0);

  const testSetColumns: TableProps.ColumnDefinition<TestSetRow>[] = [
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
      id: 'items',
      header: 'Items',
      cell: (row) => testSetCount(row, row.ref.fileCount),
      sortingComparator: (a, b) => (a.ref.fileCount ?? -1) - (b.ref.fileCount ?? -1),
    },
    {
      id: 'pending',
      header: 'Pending',
      cell: (row) => testSetCount(row, row.ref.pendingDocs),
      sortingComparator: (a, b) => (a.ref.pendingDocs ?? -1) - (b.ref.pendingDocs ?? -1),
    },
    {
      id: 'assigned',
      header: 'Assigned',
      cell: (row) => testSetCount(row, testSetAssigned(row)),
      sortingComparator: (a, b) => (testSetAssigned(a) ?? -1) - (testSetAssigned(b) ?? -1),
    },
    {
      id: 'annotators',
      header: 'Annotators',
      cell: (row) => <AnnotatorsCell eligible={row.ref.eligibleAnnotators} />,
      sortingComparator: (a, b) => eligibleCount(a.ref.eligibleAnnotators) - eligibleCount(b.ref.eligibleAnnotators),
    },
    {
      id: 'actions',
      header: <span className="idp-visually-hidden">Actions</span>,
      width: 60,
      cell: (row) => (
        <ButtonDropdown
          variant="inline-icon"
          ariaLabel={`Actions for ${row.name}`}
          expandToViewport
          items={[
            { id: 'view', text: 'View' },
            { id: 'settings', text: 'Settings' },
          ]}
          onItemClick={({ detail }) => onTestSetAction(row, detail.id)}
        />
      ),
    },
  ];

  const productionInitial = definitions == null && productionError == null;
  const testSetsInitial = testSetRefs == null && testSetError == null;
  const preparingSets = (testSetRefs ?? []).filter((t) => t.summaryStatus === 'BUILDING').length;
  const anyLoading = productionLoading || listingTestSets;

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description="Set up the Default review queue and list the Annotators who work it."
          actions={
            <Button iconName="refresh" loading={anyLoading} onClick={() => void refresh()} ariaLabel="Refresh queues">
              Refresh
            </Button>
          }
        >
          Queue Management
        </Header>
      }
    >
      <SpaceBetween size="l">
        {productionError != null && (
          <Alert
            type={definitions ? 'warning' : 'error'}
            header={definitions ? 'Showing the last loaded queues' : "Couldn't load the review queues"}
            action={<Button onClick={() => void loadProduction()}>Retry</Button>}
          >
            {describeApiError(productionError, 'load the review queues')}
          </Alert>
        )}
        <Table<ProductionRow>
          {...production.collectionProps}
          variant="container"
          items={production.items}
          trackBy="id"
          columnDefinitions={productionColumns}
          loading={productionInitial}
          loadingText="Loading queues"
          header={
            <Header
              counter={productionInitial ? undefined : `(${productionRows.length})`}
              description="System-wide queues for processed documents. When HITL is enabled in a document's configuration and the Default queue is enabled, a document that raised confidence alerts arrives in the Default queue."
            >
              Production Queues
            </Header>
          }
          filter={
            <TextFilter
              {...production.filterProps}
              filteringPlaceholder="Find queues"
              filteringAriaLabel="Find production queues"
              countText={getFilterCounterText(production.filteredItemsCount ?? 0)}
            />
          }
          pagination={<Pagination {...production.paginationProps} ariaLabels={paginationLabels} />}
        />
        {testSetError != null && (
          <Alert
            type={testSetRefs ? 'warning' : 'error'}
            header={testSetRefs ? 'Showing the last loaded test sets' : "Couldn't load the test sets"}
            action={<Button onClick={() => void loadTestSets()}>Retry</Button>}
          >
            {describeApiError(testSetError, 'load the test sets')}
          </Alert>
        )}
        <Table<TestSetRow>
          {...testStudio.collectionProps}
          variant="container"
          items={testStudio.items}
          trackBy="id"
          columnDefinitions={testSetColumns}
          loading={testSetsInitial}
          loadingText="Loading Test Studio queues"
          header={
            <Header
              counter={testSetsInitial ? undefined : `(${testSetRows.length})`}
              description="Each test set is a queue of documents whose ground truth needs confirming. Open one to assign its documents."
              actions={
                preparingSets > 0 ? (
                  <StatusIndicator type="loading">{`Preparing ${plural(preparingSets, 'test set')}…`}</StatusIndicator>
                ) : undefined
              }
            >
              Test Studio Queues
            </Header>
          }
          filter={
            <TextFilter
              {...testStudio.filterProps}
              filteringPlaceholder="Find test sets"
              filteringAriaLabel="Find Test Studio queues"
              countText={getFilterCounterText(testStudio.filteredItemsCount ?? 0)}
            />
          }
          pagination={<Pagination {...testStudio.paginationProps} ariaLabels={paginationLabels} />}
        />
      </SpaceBetween>
      <QueueDefinitionModal
        visible={!!editing}
        queue={editing?.queue ?? null}
        existingIds={(definitions ?? []).map((d) => d.id)}
        annotators={annotators ?? []}
        annotatorsLoading={annotatorsLoading}
        onDismiss={() => setEditing(null)}
        onSaved={(q) => {
          if (q) {
            setSuccessMessage(`Saved the queue ${q.name}.`);
            // Show the saved definition at once; the reload below confirms it.
            setDefinitions((defs) =>
              defs && defs.some((d) => d.id === q.id) ? defs.map((d) => (d.id === q.id ? q : d)) : [...(defs ?? []), q],
            );
          }
          setEditing(null);
          void loadProduction();
        }}
      />
      <Modal
        visible={!!deleting}
        onDismiss={() => setDeleting(null)}
        header="Delete queue"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setDeleting(null)}>
                Cancel
              </Button>
              <Button variant="primary" loading={deleteBusy} onClick={() => void confirmDelete()}>
                Delete
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          {deleteError && <Alert type="error">{deleteError}</Alert>}
          <Box>{deleting ? deleteConfirmText(deleting.queue, deleting.depth) : null}</Box>
        </SpaceBetween>
      </Modal>
      <QueueDefinitionModal
        visible={!!settingsTarget}
        queue={null}
        testSet={settingsTarget}
        existingIds={[]}
        annotators={annotators ?? []}
        annotatorsLoading={annotatorsLoading}
        onDismiss={() => setSettingsTarget(null)}
        onSaved={(_, saved) => {
          setSuccessMessage(`Saved the queue settings of ${settingsTarget?.name ?? 'the test set'}.`);
          if (saved) {
            // Show the saved settings at once; the reload below confirms them.
            setTestSetRefs((refs) =>
              (refs ?? []).map((r) =>
                r.id === saved.testSetId
                  ? {
                      ...r,
                      description: saved.description,
                      priority: saved.priority,
                      claimLeaseMinutes: saved.claimLeaseMinutes,
                      eligibleAnnotators: saved.eligibleAnnotators ? [...saved.eligibleAnnotators] : [],
                    }
                  : r,
              ),
            );
          }
          setSettingsTarget(null);
          void loadTestSets();
        }}
      />
    </ContentLayout>
  );
};

export default QueuesPage;
