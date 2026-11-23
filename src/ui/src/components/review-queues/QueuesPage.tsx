// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Review Studio › Review Management (Admin): Admins create production queues, edit their
// settings and see every queue here, or, with the Production | Test Studio switch, the
// Test Studio queues, one per test set. A configuration profile's routing rules send
// documents to a production queue by its id. One table shows at a time, and only it is polled; the choice is in
// the URL (`?view=test`), so Back and the queue pages' breadcrumbs return to it.
// Routing rules are evaluated only while Review Routing is on and the Default queue is
// enabled: above the production table, a warning says when Review Routing is off in the
// active configuration profile, and another when the Default queue is off, each with
// where it is changed.
// Open a queue to assign its items to Annotators, unassign, move or remove them.
// Deleting a queue moves its items to the Default queue; the confirmation lists the routing
// rules that name the queue, whose documents go to the Default queue from then on.
import React, { useCallback, useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { ConsoleLogger } from 'aws-amplify/utils';
import { defineMessages, FormattedMessage, useIntl, type MessageTag, type NoMessageValues } from 'react-intl';
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
  SegmentedControl,
  SpaceBetween,
  StatusIndicator,
  Table,
  TextFilter,
} from '@cloudscape-design/components';

import type { ReviewQueueDefinition, TestSetQueueSummary, ReviewQueueHealth } from '../../graphql/generated/operation-types';
import useAppContext from '../../contexts/app';
import usePolling from '../../hooks/use-polling';
import { describeApiError } from '../../hooks/utils/graphql-error';
import { useFormatters } from '../../i18n/format';
import { formatDateTime } from '../../utils/date-time';
import { CONFIGURATION_PATH, productionQueuePath, testSetQueuePath } from '../../routes/constants';
import type { ReviewManagementView } from '../../routes/constants';
import { usePaginationLabels } from '../common/labels';
import { NoDataState, TableNoMatchState, filterCounterText } from '../common/table';
import useReviewActionSettings from '../review-bulk/use-review-action-settings';
import QueueDefinitionModal from './QueueDefinitionModal';
import { DeleteQueueReferences } from './QueueReferences';
import type { TestSetQueueTarget } from './QueueDefinitionModal';
import { fetchQueuesOverview, reviewErrorCode } from './review-api';
import { deleteReviewQueue, listAnnotatorEmails, listReviewQueues, listTestSetQueues } from './reviewer-assignment';
import useQueueReferences from './use-queue-references';
import useSavedQueueDefinitions from './use-saved-queue-definitions';

const logger = new ConsoleLogger('QueuesPage');

export const QUEUES_POLL_INTERVAL_MS = 20_000;

const messages = defineMessages<{
  pageDescription: NoMessageValues;
  createQueue: NoMessageValues;
  productionDescription: NoMessageValues;
  reviewRoutingOff: { readonly link: MessageTag };
  defaultQueueOff: NoMessageValues;
  openDefaultQueueSettings: NoMessageValues;
  queueCreated: { readonly name: string };
  queueSaved: { readonly name: string };
}>({
  pageDescription: {
    id: 'reviewQueues.queuesPage.pageDescription',
    defaultMessage: 'Create and set up the production review queues, and list the Annotators who work them.',
    description: 'Text under the heading of the Review Management page.',
  },
  createQueue: {
    id: 'reviewQueues.queuesPage.createQueue',
    defaultMessage: 'Create queue',
    description: 'Button on the Review Management page that opens the dialog for a new production review queue.',
  },
  productionDescription: {
    id: 'reviewQueues.queuesPage.productionDescription',
    defaultMessage:
      "System-wide queues for processed documents. While Review Routing is on in a document's configuration profile and the Default queue is enabled, the routing rules of that profile send the document to the queue a routing rule names. The Default queue receives the documents the built-in routing rules hold, and documents whose queue is missing or disabled.",
    description:
      'Text under the heading of the Production Queues table on the Review Management page. Review Routing is the name of a processing step and of its switch in a configuration profile.',
  },
  reviewRoutingOff: {
    id: 'reviewQueues.queuesPage.reviewRoutingOff',
    defaultMessage:
      "Review Routing is off in the active configuration profile, so no document is held for review. Turn it on in the profile's Processing Settings, on the <link>Configuration</link> page.",
    description:
      'Warning above the Production Queues table on the Review Management page (Admins), when the Review Routing switch of the active configuration profile is off. <link> marks the name of the page it links to; Processing Settings is the name of a tab of that page.',
  },
  defaultQueueOff: {
    id: 'reviewQueues.queuesPage.defaultQueueOff',
    defaultMessage: 'The Default review queue is off, so routing rules are not evaluated.',
    description:
      'Warning above the Production Queues table on the Review Management page (Admins), when the Default review queue is disabled.',
  },
  openDefaultQueueSettings: {
    id: 'reviewQueues.queuesPage.openDefaultQueueSettings',
    defaultMessage: 'Open Default queue settings',
    description:
      'Button in the warning that the Default review queue is off: opens the settings dialog of the Default queue, where it is turned on.',
  },
  queueCreated: {
    id: 'reviewQueues.queuesPage.queueCreated',
    defaultMessage: 'Created the queue {name}.',
    description: 'Success notification after a production review queue is created. {name} is the name of the queue.',
  },
  queueSaved: {
    id: 'reviewQueues.queuesPage.queueSaved',
    defaultMessage: 'Saved the queue {name}.',
    description: 'Success notification after the settings of a production review queue are saved. {name} is the name of the queue.',
  },
});

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

/**
 * Why a queue can't be deleted, or undefined when it can. A queue that holds items can: they move to Default.
 * So can a queue that is no longer defined but still holds items — what a delete that stopped part-way
 * leaves, since the definition goes first — and deleting it again moves the rest.
 */
export const deleteDisabledReason = (definition: ReviewQueueDefinition | null, depth = 0): string | undefined =>
  !definition
    ? depth > 0
      ? undefined
      : 'This queue is not defined'
    : definition.isDefaultQueue
      ? "The default queue can't be deleted"
      : undefined;

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
  const intl = useIntl();
  const paginationLabels = usePaginationLabels();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const view: ReviewManagementView = searchParams.get('view') === 'test' ? 'test' : 'production';
  const setView = (next: ReviewManagementView) => setSearchParams(next === 'test' ? { view: 'test' } : {}, { replace: true });
  const { setSuccessMessage } = useAppContext();
  const { locale } = useFormatters();

  const [definitions, setDefinitions] = useState<ReviewQueueDefinition[] | null>(null);
  const [overview, setOverview] = useState<ReviewQueueHealth[] | null>(null);
  const [productionError, setProductionError] = useState<unknown>(null);
  const [productionLoading, setProductionLoading] = useState(false);
  const [testSetRefs, setTestSetRefs] = useState<TestSetQueueSummary[] | null>(null);
  const [testSetError, setTestSetError] = useState<unknown>(null);
  const [listingTestSets, setListingTestSets] = useState(false);
  const [annotators, setAnnotators] = useState<string[] | null>(null);
  const [annotatorsLoading, setAnnotatorsLoading] = useState(false);
  // The Settings dialog of a queue, or the Create queue dialog (no queue).
  const [editing, setEditing] = useState<{ queue: ReviewQueueDefinition | null } | null>(null);
  const [deleting, setDeleting] = useState<{ queue: { id: string; name: string }; depth: number } | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [settingsTarget, setSettingsTarget] = useState<TestSetQueueTarget | null>(null);

  const savedDefinitions = useSavedQueueDefinitions();
  // Whether Review Routing is on in the active configuration profile; null while unknown.
  const reviewSettings = useReviewActionSettings();
  // The routing rules that name the queue about to be deleted, read when the dialog opens.
  const deletingReferences = useQueueReferences(deleting?.queue.id ?? null);

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
      setDefinitions(savedDefinitions.apply(defs));
      setOverview(ov?.queues ?? null);
      setProductionError(null);
    } catch (err) {
      logger.error('listReviewQueues failed', err);
      setProductionError(err);
    } finally {
      setProductionLoading(false);
    }
  }, [savedDefinitions]);

  usePolling(loadProduction, { enabled: view === 'production', intervalMs: QUEUES_POLL_INTERVAL_MS, immediate: true });

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

  usePolling(loadTestSets, { enabled: view === 'test', intervalMs: QUEUES_POLL_INTERVAL_MS, immediate: true });

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

  // Refresh reads the table on show.
  const refresh = useCallback(async () => {
    await (view === 'test' ? loadTestSets() : loadProduction());
  }, [view, loadProduction, loadTestSets]);

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
  /** Open a queue's Settings, or the Create queue dialog when there is no queue. */
  const openEditor = (queue: ReviewQueueDefinition | null) => {
    setEditing({ queue });
    void loadAnnotators();
  };

  const confirmDelete = async () => {
    if (!deleting) return;
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      await deleteReviewQueue(deleting.queue.id);
      savedDefinitions.forget(deleting.queue.id);
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
    const deleteReason = deleteDisabledReason(d, row.health?.summary.depth ?? 0);
    return [
      { id: 'view', text: 'View' },
      { id: 'edit', text: 'Settings', disabled: !d, disabledReason: d ? undefined : 'This queue is not defined' },
      { id: 'delete', text: 'Delete', disabled: !!deleteReason, disabledReason: deleteReason },
    ];
  };

  const onProductionAction = (row: ProductionRow, action: string) => {
    if (action === 'view') navigate(row.href.replace(/^#/, ''));
    else if (action === 'edit' && row.definition) openEditor(row.definition);
    else if (action === 'delete' && !deleteDisabledReason(row.definition, row.health?.summary.depth ?? 0)) {
      setDeleteError(null);
      setDeleting({
        queue: { id: row.id, name: row.definition?.name || row.name },
        depth: row.health?.summary.depth ?? 0,
      });
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
      <span title={row.ref.queueCountsAt ? `Counted ${formatDateTime(row.ref.queueCountsAt, locale)}` : undefined}>{n}</span>
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

  // Routing rules are evaluated only while Review Routing is on and the Default queue is
  // enabled. Each cause that holds gets its warning; a state that is not known gets none.
  const defaultQueue = (definitions ?? []).find((d) => d.isDefaultQueue) ?? null;
  const reviewRoutingOff = reviewSettings.reviewRoutingEnabled === false;
  const defaultQueueOff = defaultQueue != null && !defaultQueue.enabled;
  const configurationLink = (chunks: React.ReactNode[]): React.JSX.Element => <Link href={`#${CONFIGURATION_PATH}`}>{chunks}</Link>;

  const productionInitial = definitions == null && productionError == null;
  const testSetsInitial = testSetRefs == null && testSetError == null;
  const preparingSets = (testSetRefs ?? []).filter((t) => t.summaryStatus === 'BUILDING').length;
  const shownLoading = view === 'test' ? listingTestSets : productionLoading;

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description={intl.formatMessage(messages.pageDescription)}
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <SegmentedControl
                selectedId={view}
                onChange={({ detail }) => setView(detail.selectedId as ReviewManagementView)}
                label="Queues to show"
                options={[
                  { id: 'production', text: 'Production' },
                  { id: 'test', text: 'Test Studio' },
                ]}
              />
              <Button iconName="refresh" loading={shownLoading} onClick={() => void refresh()} ariaLabel="Refresh queues">
                Refresh
              </Button>
              {/* A new queue is a production queue; a Test Studio queue comes with its test set. */}
              {view === 'production' && (
                <Button variant="primary" onClick={() => openEditor(null)}>
                  {intl.formatMessage(messages.createQueue)}
                </Button>
              )}
            </SpaceBetween>
          }
        >
          Review Management
        </Header>
      }
    >
      <SpaceBetween size="l">
        {view === 'production' && (
          <>
            {(productionError != null || reviewRoutingOff || defaultQueueOff) && (
              <SpaceBetween size="s">
                {productionError != null && (
                  <Alert
                    type={definitions ? 'warning' : 'error'}
                    header={definitions ? 'Showing the last loaded queues' : "Couldn't load the review queues"}
                    action={<Button onClick={() => void loadProduction()}>Retry</Button>}
                  >
                    {describeApiError(productionError, 'load the review queues')}
                  </Alert>
                )}
                {reviewRoutingOff && (
                  <Alert type="warning">
                    <FormattedMessage {...messages.reviewRoutingOff} values={{ link: configurationLink }} />
                  </Alert>
                )}
                {defaultQueueOff && (
                  <Alert
                    type="warning"
                    action={
                      <Button onClick={() => openEditor(defaultQueue)}>{intl.formatMessage(messages.openDefaultQueueSettings)}</Button>
                    }
                  >
                    {intl.formatMessage(messages.defaultQueueOff)}
                  </Alert>
                )}
              </SpaceBetween>
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
                  description={intl.formatMessage(messages.productionDescription)}
                >
                  Production Queues
                </Header>
              }
              filter={
                <TextFilter
                  {...production.filterProps}
                  filteringPlaceholder="Find queues"
                  filteringAriaLabel="Find production queues"
                  countText={filterCounterText(intl, production.filteredItemsCount ?? 0)}
                />
              }
              pagination={<Pagination {...production.paginationProps} ariaLabels={paginationLabels} />}
            />
          </>
        )}
        {view === 'test' && (
          <>
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
                  countText={filterCounterText(intl, testStudio.filteredItemsCount ?? 0)}
                />
              }
              pagination={<Pagination {...testStudio.paginationProps} ariaLabels={paginationLabels} />}
            />
          </>
        )}
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
            setSuccessMessage(intl.formatMessage(editing?.queue ? messages.queueSaved : messages.queueCreated, { name: q.name }));
            // Show the saved definition at once, and keep it through reads that still return the old one.
            savedDefinitions.remember(q);
            setDefinitions((defs) => savedDefinitions.apply(defs ?? []));
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
          <DeleteQueueReferences state={deletingReferences} />
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
