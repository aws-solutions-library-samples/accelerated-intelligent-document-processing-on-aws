// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Review Studio › Review Management (Admin): Admins edit the Default queue's settings and see
// every queue here, or, with the Production | Test Studio switch, the Test Studio queues,
// one per test set. One table shows at a time, and only it is polled; the choice is in
// the URL (`?view=test`), so Back and the queue pages' breadcrumbs return to it.
// Open a queue to assign its items to Annotators, unassign, move or remove them.
// Deleting a queue moves its items to the Default queue.
import React, { useCallback, useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { ConsoleLogger } from 'aws-amplify/utils';
import { createIntl, defineMessages, FormattedMessage, useIntl, type IntlShape, type NoMessageValues } from 'react-intl';
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
import { productionQueuePath, testSetQueuePath } from '../../routes/constants';
import type { ReviewManagementView } from '../../routes/constants';
import { usePaginationLabels } from '../common/labels';
import { NoDataState, TableNoMatchState, filterCounterText } from '../common/table';
import QueueDefinitionModal from './QueueDefinitionModal';
import type { TestSetQueueTarget } from './QueueDefinitionModal';
import { fetchQueuesOverview, reviewErrorCode } from './review-api';
import { deleteReviewQueue, listAnnotatorEmails, listReviewQueues, listTestSetQueues } from './reviewer-assignment';
import useSavedQueueDefinitions from './use-saved-queue-definitions';

const logger = new ConsoleLogger('QueuesPage');

export const QUEUES_POLL_INTERVAL_MS = 20_000;

type QueueName = { readonly name: string };

const messages = defineMessages<{
  notDefined: NoMessageValues;
  defaultNotDeletable: NoMessageValues;
  confirmDelete: { readonly name: string; readonly id: string };
  confirmDeleteMoving: { readonly name: string; readonly id: string; readonly count: number };
  noAnnotators: NoMessageValues;
  empty: NoMessageValues;
  deleted: QueueName;
  deletedMoved: QueueName;
  finalizing: NoMessageValues;
  menuView: NoMessageValues;
  menuSettings: NoMessageValues;
  menuDelete: NoMessageValues;
  columnQueue: NoMessageValues;
  columnItems: NoMessageValues;
  columnPending: NoMessageValues;
  columnAssigned: NoMessageValues;
  columnAnnotators: NoMessageValues;
  columnActions: NoMessageValues;
  notDefinedNote: NoMessageValues;
  rowActions: QueueName;
  countedAt: { readonly time: string };
  preparing: NoMessageValues;
  description: NoMessageValues;
  viewLabel: NoMessageValues;
  viewProduction: NoMessageValues;
  viewTestStudio: NoMessageValues;
  refreshAriaLabel: NoMessageValues;
  refresh: NoMessageValues;
  title: NoMessageValues;
  productionStale: NoMessageValues;
  productionLoadError: NoMessageValues;
  retry: NoMessageValues;
  productionLoading: NoMessageValues;
  productionDescription: NoMessageValues;
  productionTitle: NoMessageValues;
  productionFind: NoMessageValues;
  productionFindAriaLabel: NoMessageValues;
  testSetsStale: NoMessageValues;
  testSetsLoadError: NoMessageValues;
  testSetsLoading: NoMessageValues;
  testSetsDescription: NoMessageValues;
  preparingSets: { readonly count: number };
  testSetsTitle: NoMessageValues;
  testSetsFind: NoMessageValues;
  testSetsFindAriaLabel: NoMessageValues;
  saved: QueueName;
  savedTestSet: QueueName;
  savedTestSetFallback: NoMessageValues;
  deleteTitle: NoMessageValues;
  cancel: NoMessageValues;
  delete: NoMessageValues;
}>({
  notDefined: {
    id: 'reviewQueues.queuesPage.notDefined',
    defaultMessage: 'This queue is not defined',
    description:
      "Why the Settings or Delete item of a queue's menu in Review Management is disabled: the queue still holds items but has no definition.",
  },
  defaultNotDeletable: {
    id: 'reviewQueues.queuesPage.defaultNotDeletable',
    defaultMessage: "The default queue can't be deleted",
    description: "Why the Delete item of the Default queue's menu in Review Management is disabled.",
  },
  confirmDelete: {
    id: 'reviewQueues.queuesPage.confirmDelete',
    defaultMessage: "Delete the queue {name} ({id})? This can't be undone.",
    description: "Body of the Delete queue dialog for a queue that holds no items. {name} is the queue's name and {id} its id, as stored.",
  },
  confirmDeleteMoving: {
    id: 'reviewQueues.queuesPage.confirmDeleteMoving',
    defaultMessage:
      "Delete the queue {name} ({id})? This can't be undone. {count, plural, one {# item} other {# items}} will move to the Default queue.",
    description:
      "Body of the Delete queue dialog for a queue that holds items. {name} is the queue's name and {id} its id, as stored; {count} is how many items it holds. Default is the name of the queue they move to.",
  },
  noAnnotators: {
    id: 'reviewQueues.queuesPage.noAnnotators',
    defaultMessage: 'No Annotators listed',
    description:
      'Tooltip of the Annotators count in a Review Management table when the queue lists no Annotator. Annotator is a user role.',
  },
  empty: {
    id: 'reviewQueues.queuesPage.empty',
    defaultMessage: 'No queues found. The Default queue is created automatically; reload the page.',
    description: 'Shown in the Production Queues table of Review Management when no queue is listed. Default is the name of a queue.',
  },
  deleted: {
    id: 'reviewQueues.queuesPage.deleted',
    defaultMessage: 'Deleted the queue {name}.',
    description: "Notice after an Admin deleted a queue that held no items. {name} is the queue's name, as stored.",
  },
  deletedMoved: {
    id: 'reviewQueues.queuesPage.deletedMoved',
    defaultMessage: 'Deleted the queue {name}; its items moved to the Default queue.',
    description:
      "Notice after an Admin deleted a queue that held items. {name} is the queue's name, as stored; Default is the name of the queue its items moved to.",
  },
  finalizing: {
    id: 'reviewQueues.queuesPage.finalizing',
    defaultMessage: 'An item in this queue is being completed. Try again in a minute.',
    description: "Error in the Delete queue dialog when the server refused the delete because one of the queue's items is being completed.",
  },
  menuView: {
    id: 'reviewQueues.queuesPage.menu.view',
    defaultMessage: 'View',
    description: "Item of a queue's menu in Review Management that opens the queue's page.",
  },
  menuSettings: {
    id: 'reviewQueues.queuesPage.menu.settings',
    defaultMessage: 'Settings',
    description: "Item of a queue's menu in Review Management that opens the queue's Settings dialog.",
  },
  menuDelete: {
    id: 'reviewQueues.queuesPage.menu.delete',
    defaultMessage: 'Delete',
    description: "Item of a production queue's menu in Review Management that deletes the queue.",
  },
  columnQueue: {
    id: 'reviewQueues.queuesPage.column.queue',
    defaultMessage: 'Queue',
    description: "Column header of the Review Management queue tables: the queue's name, as a link to its page.",
  },
  columnItems: {
    id: 'reviewQueues.queuesPage.column.items',
    defaultMessage: 'Items',
    description: 'Column header of the Review Management queue tables: how many items the queue holds.',
  },
  columnPending: {
    id: 'reviewQueues.queuesPage.column.pending',
    defaultMessage: 'Pending',
    description: 'Column header of the Review Management queue tables: how many of its items no one is assigned to yet.',
  },
  columnAssigned: {
    id: 'reviewQueues.queuesPage.column.assigned',
    defaultMessage: 'Assigned',
    description: 'Column header of the Review Management queue tables: how many of its items are assigned, started or not.',
  },
  columnAnnotators: {
    id: 'reviewQueues.queuesPage.column.annotators',
    defaultMessage: 'Annotators',
    description: 'Column header of the Review Management queue tables: how many Annotators the queue lists. Annotator is a user role.',
  },
  columnActions: {
    id: 'reviewQueues.queuesPage.column.actions',
    defaultMessage: 'Actions',
    description: "Header of the Review Management queue tables' last column, read only by screen readers: each row's menu.",
  },
  notDefinedNote: {
    id: 'reviewQueues.queuesPage.notDefinedNote',
    defaultMessage: 'Not defined — move its items to a defined queue',
    description: "Note under a queue's name in the Production Queues table when the queue holds items but has no definition.",
  },
  rowActions: {
    id: 'reviewQueues.queuesPage.rowActions',
    defaultMessage: 'Actions for {name}',
    description: "Screen-reader label of a queue's menu button in Review Management. {name} is the queue's name, as stored.",
  },
  countedAt: {
    id: 'reviewQueues.queuesPage.countedAt',
    defaultMessage: 'Counted {time}',
    description:
      "Tooltip of a Test Studio queue's count in Review Management: when it was counted. {time} is a date and time, already formatted.",
  },
  preparing: {
    id: 'reviewQueues.queuesPage.preparing',
    defaultMessage: 'Preparing…',
    description: "Shown in a Test Studio queue's count cell in Review Management while the test set's summaries are built.",
  },
  description: {
    id: 'reviewQueues.queuesPage.description',
    defaultMessage: 'Set up the Default review queue and list the Annotators who work it.',
    description: 'Description under the Review Management heading. Default is the name of a queue; Annotator is a user role.',
  },
  viewLabel: {
    id: 'reviewQueues.queuesPage.view.label',
    defaultMessage: 'Queues to show',
    description: 'Screen-reader label of the Review Management switch between the Production and the Test Studio queue tables.',
  },
  viewProduction: {
    id: 'reviewQueues.queuesPage.view.production',
    defaultMessage: 'Production',
    description: 'Option of the Review Management switch that shows the production queues, where processed documents are routed.',
  },
  viewTestStudio: {
    id: 'reviewQueues.queuesPage.view.testStudio',
    defaultMessage: 'Test Studio',
    description:
      'Option of the Review Management switch that shows the Test Studio queues, one per test set. Test Studio is a product area name.',
  },
  refreshAriaLabel: {
    id: 'reviewQueues.queuesPage.refreshAriaLabel',
    defaultMessage: 'Refresh queues',
    description: 'Screen-reader label of the Refresh button in Review Management.',
  },
  refresh: {
    id: 'reviewQueues.queuesPage.refresh',
    defaultMessage: 'Refresh',
    description: 'Button in Review Management that reloads the queue table shown.',
  },
  title: {
    id: 'reviewQueues.queuesPage.title',
    defaultMessage: 'Review Management',
    description: 'Heading of the Review Management page of Review Studio, where Admins set up and watch the review queues.',
  },
  productionStale: {
    id: 'reviewQueues.queuesPage.production.stale',
    defaultMessage: 'Showing the last loaded queues',
    description:
      'Header of the warning in Review Management when refreshing the production queues failed but earlier ones are still shown.',
  },
  productionLoadError: {
    id: 'reviewQueues.queuesPage.production.loadError',
    defaultMessage: "Couldn't load the review queues",
    description: 'Header of the error in Review Management when the production queues could not be loaded at all.',
  },
  retry: {
    id: 'reviewQueues.queuesPage.retry',
    defaultMessage: 'Retry',
    description: 'Button of a Review Management load warning or error that loads the queues again.',
  },
  productionLoading: {
    id: 'reviewQueues.queuesPage.production.loading',
    defaultMessage: 'Loading queues',
    description: 'Shown in the Production Queues table while the queues load.',
  },
  productionDescription: {
    id: 'reviewQueues.queuesPage.production.description',
    defaultMessage:
      "System-wide queues for processed documents. When HITL is enabled in a document's configuration and the Default queue is enabled, a document that raised confidence alerts arrives in the Default queue.",
    description:
      'Description of the Production Queues table. HITL (human in the loop) is the name of a configuration setting; Default is the name of a queue.',
  },
  productionTitle: {
    id: 'reviewQueues.queuesPage.production.title',
    defaultMessage: 'Production Queues',
    description: 'Title of the Review Management table of production queues, where processed documents are routed.',
  },
  productionFind: {
    id: 'reviewQueues.queuesPage.production.find',
    defaultMessage: 'Find queues',
    description: "Placeholder of the Production Queues table's text filter.",
  },
  productionFindAriaLabel: {
    id: 'reviewQueues.queuesPage.production.findAriaLabel',
    defaultMessage: 'Find production queues',
    description: "Screen-reader label of the Production Queues table's text filter.",
  },
  testSetsStale: {
    id: 'reviewQueues.queuesPage.testSets.stale',
    defaultMessage: 'Showing the last loaded test sets',
    description:
      'Header of the warning in Review Management when refreshing the Test Studio queues failed but earlier ones are still shown.',
  },
  testSetsLoadError: {
    id: 'reviewQueues.queuesPage.testSets.loadError',
    defaultMessage: "Couldn't load the test sets",
    description: 'Header of the error in Review Management when the Test Studio queues (one per test set) could not be loaded at all.',
  },
  testSetsLoading: {
    id: 'reviewQueues.queuesPage.testSets.loading',
    defaultMessage: 'Loading Test Studio queues',
    description: 'Shown in the Test Studio Queues table while the queues load. Test Studio is a product area name.',
  },
  testSetsDescription: {
    id: 'reviewQueues.queuesPage.testSets.description',
    defaultMessage: 'Each test set is a queue of documents whose ground truth needs confirming. Open one to assign its documents.',
    description: 'Description of the Test Studio Queues table in Review Management.',
  },
  preparingSets: {
    id: 'reviewQueues.queuesPage.testSets.preparing',
    defaultMessage: 'Preparing {count, plural, one {# test set} other {# test sets}}…',
    description: "Status beside the Test Studio Queues table's title while test sets' summaries are built. {count} is how many test sets.",
  },
  testSetsTitle: {
    id: 'reviewQueues.queuesPage.testSets.title',
    defaultMessage: 'Test Studio Queues',
    description: 'Title of the Review Management table of Test Studio queues, one per test set. Test Studio is a product area name.',
  },
  testSetsFind: {
    id: 'reviewQueues.queuesPage.testSets.find',
    defaultMessage: 'Find test sets',
    description: "Placeholder of the Test Studio Queues table's text filter.",
  },
  testSetsFindAriaLabel: {
    id: 'reviewQueues.queuesPage.testSets.findAriaLabel',
    defaultMessage: 'Find Test Studio queues',
    description: "Screen-reader label of the Test Studio Queues table's text filter.",
  },
  saved: {
    id: 'reviewQueues.queuesPage.saved',
    defaultMessage: 'Saved the queue {name}.',
    description: "Notice after an Admin saved a production queue's settings in Review Management. {name} is the queue's name, as stored.",
  },
  savedTestSet: {
    id: 'reviewQueues.queuesPage.savedTestSet',
    defaultMessage: 'Saved the queue settings of {name}.',
    description:
      "Notice after an Admin saved a Test Studio queue's settings in Review Management. {name} is the test set's name, as stored.",
  },
  savedTestSetFallback: {
    id: 'reviewQueues.queuesPage.savedTestSetFallback',
    defaultMessage: 'Saved the queue settings of the test set.',
    description: "Notice after an Admin saved a Test Studio queue's settings in Review Management, when the test set's name is not known.",
  },
  deleteTitle: {
    id: 'reviewQueues.queuesPage.deleteDialog.title',
    defaultMessage: 'Delete queue',
    description: 'Title of the dialog that confirms deleting a production queue.',
  },
  cancel: {
    id: 'reviewQueues.queuesPage.deleteDialog.cancel',
    defaultMessage: 'Cancel',
    description: 'Button of the Delete queue dialog that closes it without deleting.',
  },
  delete: {
    id: 'reviewQueues.queuesPage.deleteDialog.delete',
    defaultMessage: 'Delete',
    description: 'Button of the Delete queue dialog that deletes the queue.',
  },
});

/** Formats the English source text for callers that pass no `intl`. */
const englishIntl = createIntl({ locale: 'en', defaultLocale: 'en' });

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

/**
 * Why a queue can't be deleted, or undefined when it can. A queue that holds items can: they move to Default.
 * So can a queue that is no longer defined but still holds items — what a delete that stopped part-way
 * leaves, since the definition goes first — and deleting it again moves the rest.
 * In the UI's language; without `intl`, the English source text.
 */
export const deleteDisabledReason = (definition: ReviewQueueDefinition | null, depth = 0, intl?: IntlShape): string | undefined =>
  !definition
    ? depth > 0
      ? undefined
      : (intl ?? englishIntl).formatMessage(messages.notDefined)
    : definition.isDefaultQueue
      ? (intl ?? englishIntl).formatMessage(messages.defaultNotDeletable)
      : undefined;

/**
 * The delete confirmation text, naming where a non-empty queue's items go. In the UI's
 * language; without `intl`, the English source text.
 */
export const deleteConfirmText = (queue: { id: string; name: string }, depth: number, intl?: IntlShape): string => {
  const format = intl ?? englishIntl;
  return depth > 0
    ? format.formatMessage(messages.confirmDeleteMoving, { name: queue.name, id: queue.id, count: depth })
    : format.formatMessage(messages.confirmDelete, { name: queue.name, id: queue.id });
};

/**
 * How many Annotators are listed on a queue (its Eligible Annotators, from its
 * Settings), with their names on hover. Only they work the queue; none listed means
 * only Admins do.
 */
export const AnnotatorsCell = ({ eligible }: { eligible: readonly string[] | null | undefined }): React.JSX.Element => {
  const intl = useIntl();
  const names = eligible ?? [];
  return <span title={names.length ? names.join(', ') : intl.formatMessage(messages.noAnnotators)}>{names.length}</span>;
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
  const [editing, setEditing] = useState<{ queue: ReviewQueueDefinition } | null>(null);
  const [deleting, setDeleting] = useState<{ queue: { id: string; name: string }; depth: number } | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [settingsTarget, setSettingsTarget] = useState<TestSetQueueTarget | null>(null);

  const savedDefinitions = useSavedQueueDefinitions();

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
          <FormattedMessage {...messages.empty} />
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
      savedDefinitions.forget(deleting.queue.id);
      setSuccessMessage(intl.formatMessage(deleting.depth > 0 ? messages.deletedMoved : messages.deleted, { name: deleting.queue.name }));
      setDeleting(null);
      await loadProduction();
    } catch (err) {
      setDeleteError(
        reviewErrorCode(err) === 'QUEUE_FINALIZING' ? intl.formatMessage(messages.finalizing) : describeApiError(err, 'delete the queue'),
      );
    } finally {
      setDeleteBusy(false);
    }
  };

  const productionMenu = (row: ProductionRow): ButtonDropdownProps.Item[] => {
    const d = row.definition;
    const deleteReason = deleteDisabledReason(d, row.health?.summary.depth ?? 0, intl);
    return [
      { id: 'view', text: intl.formatMessage(messages.menuView) },
      {
        id: 'edit',
        text: intl.formatMessage(messages.menuSettings),
        disabled: !d,
        disabledReason: d ? undefined : intl.formatMessage(messages.notDefined),
      },
      { id: 'delete', text: intl.formatMessage(messages.menuDelete), disabled: !!deleteReason, disabledReason: deleteReason },
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
      header: intl.formatMessage(messages.columnQueue),
      isRowHeader: true,
      sortingField: 'name',
      cell: (row) => (
        <SpaceBetween size="xxxs">
          <Link href={row.href} variant="primary">
            {row.name}
          </Link>
          {!row.definition && (
            <Box variant="small" color="text-status-warning">
              <FormattedMessage {...messages.notDefinedNote} />
            </Box>
          )}
        </SpaceBetween>
      ),
    },
    {
      id: 'items',
      header: intl.formatMessage(messages.columnItems),
      cell: (row) => countCell(row.health?.summary.depth),
      sortingComparator: (a, b) => (a.health?.summary.depth ?? -1) - (b.health?.summary.depth ?? -1),
    },
    {
      id: 'pending',
      header: intl.formatMessage(messages.columnPending),
      cell: (row) => countCell(row.health?.summary.unclaimed),
      sortingComparator: (a, b) => (a.health?.summary.unclaimed ?? -1) - (b.health?.summary.unclaimed ?? -1),
    },
    {
      id: 'assigned',
      header: intl.formatMessage(messages.columnAssigned),
      cell: (row) => countCell(row.health?.summary.claimed),
      sortingComparator: (a, b) => (a.health?.summary.claimed ?? -1) - (b.health?.summary.claimed ?? -1),
    },
    {
      id: 'annotators',
      header: intl.formatMessage(messages.columnAnnotators),
      cell: (row) => <AnnotatorsCell eligible={row.definition?.eligibleAnnotators} />,
      sortingComparator: (a, b) => eligibleCount(a.definition?.eligibleAnnotators) - eligibleCount(b.definition?.eligibleAnnotators),
    },
    {
      id: 'actions',
      header: (
        <span className="idp-visually-hidden">
          <FormattedMessage {...messages.columnActions} />
        </span>
      ),
      width: 60,
      cell: (row) => (
        <ButtonDropdown
          variant="inline-icon"
          ariaLabel={intl.formatMessage(messages.rowActions, { name: row.name })}
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
      <span
        title={
          row.ref.queueCountsAt
            ? intl.formatMessage(messages.countedAt, { time: formatDateTime(row.ref.queueCountsAt, locale) })
            : undefined
        }
      >
        {n}
      </span>
    ) : preparing(row) ? (
      intl.formatMessage(messages.preparing)
    ) : (
      '—'
    );

  /** Every assigned document, started or not, so Pending + Assigned covers the open work. */
  const testSetAssigned = (row: TestSetRow): number | null =>
    row.ref.assignedDocs == null && row.ref.inProgressDocs == null ? null : (row.ref.assignedDocs ?? 0) + (row.ref.inProgressDocs ?? 0);

  const testSetColumns: TableProps.ColumnDefinition<TestSetRow>[] = [
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
      id: 'items',
      header: intl.formatMessage(messages.columnItems),
      cell: (row) => testSetCount(row, row.ref.fileCount),
      sortingComparator: (a, b) => (a.ref.fileCount ?? -1) - (b.ref.fileCount ?? -1),
    },
    {
      id: 'pending',
      header: intl.formatMessage(messages.columnPending),
      cell: (row) => testSetCount(row, row.ref.pendingDocs),
      sortingComparator: (a, b) => (a.ref.pendingDocs ?? -1) - (b.ref.pendingDocs ?? -1),
    },
    {
      id: 'assigned',
      header: intl.formatMessage(messages.columnAssigned),
      cell: (row) => testSetCount(row, testSetAssigned(row)),
      sortingComparator: (a, b) => (testSetAssigned(a) ?? -1) - (testSetAssigned(b) ?? -1),
    },
    {
      id: 'annotators',
      header: intl.formatMessage(messages.columnAnnotators),
      cell: (row) => <AnnotatorsCell eligible={row.ref.eligibleAnnotators} />,
      sortingComparator: (a, b) => eligibleCount(a.ref.eligibleAnnotators) - eligibleCount(b.ref.eligibleAnnotators),
    },
    {
      id: 'actions',
      header: (
        <span className="idp-visually-hidden">
          <FormattedMessage {...messages.columnActions} />
        </span>
      ),
      width: 60,
      cell: (row) => (
        <ButtonDropdown
          variant="inline-icon"
          ariaLabel={intl.formatMessage(messages.rowActions, { name: row.name })}
          expandToViewport
          items={[
            { id: 'view', text: intl.formatMessage(messages.menuView) },
            { id: 'settings', text: intl.formatMessage(messages.menuSettings) },
          ]}
          onItemClick={({ detail }) => onTestSetAction(row, detail.id)}
        />
      ),
    },
  ];

  const productionInitial = definitions == null && productionError == null;
  const testSetsInitial = testSetRefs == null && testSetError == null;
  const preparingSets = (testSetRefs ?? []).filter((t) => t.summaryStatus === 'BUILDING').length;
  const shownLoading = view === 'test' ? listingTestSets : productionLoading;

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description={intl.formatMessage(messages.description)}
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <SegmentedControl
                selectedId={view}
                onChange={({ detail }) => setView(detail.selectedId as ReviewManagementView)}
                label={intl.formatMessage(messages.viewLabel)}
                options={[
                  { id: 'production', text: intl.formatMessage(messages.viewProduction) },
                  { id: 'test', text: intl.formatMessage(messages.viewTestStudio) },
                ]}
              />
              <Button
                iconName="refresh"
                loading={shownLoading}
                onClick={() => void refresh()}
                ariaLabel={intl.formatMessage(messages.refreshAriaLabel)}
              >
                <FormattedMessage {...messages.refresh} />
              </Button>
            </SpaceBetween>
          }
        >
          <FormattedMessage {...messages.title} />
        </Header>
      }
    >
      <SpaceBetween size="l">
        {view === 'production' && (
          <>
            {productionError != null && (
              <Alert
                type={definitions ? 'warning' : 'error'}
                header={intl.formatMessage(definitions ? messages.productionStale : messages.productionLoadError)}
                action={
                  <Button onClick={() => void loadProduction()}>
                    <FormattedMessage {...messages.retry} />
                  </Button>
                }
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
              loadingText={intl.formatMessage(messages.productionLoading)}
              header={
                <Header
                  counter={productionInitial ? undefined : `(${productionRows.length})`}
                  description={intl.formatMessage(messages.productionDescription)}
                >
                  <FormattedMessage {...messages.productionTitle} />
                </Header>
              }
              filter={
                <TextFilter
                  {...production.filterProps}
                  filteringPlaceholder={intl.formatMessage(messages.productionFind)}
                  filteringAriaLabel={intl.formatMessage(messages.productionFindAriaLabel)}
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
                header={intl.formatMessage(testSetRefs ? messages.testSetsStale : messages.testSetsLoadError)}
                action={
                  <Button onClick={() => void loadTestSets()}>
                    <FormattedMessage {...messages.retry} />
                  </Button>
                }
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
              loadingText={intl.formatMessage(messages.testSetsLoading)}
              header={
                <Header
                  counter={testSetsInitial ? undefined : `(${testSetRows.length})`}
                  description={intl.formatMessage(messages.testSetsDescription)}
                  actions={
                    preparingSets > 0 ? (
                      <StatusIndicator type="loading">
                        <FormattedMessage {...messages.preparingSets} values={{ count: preparingSets }} />
                      </StatusIndicator>
                    ) : undefined
                  }
                >
                  <FormattedMessage {...messages.testSetsTitle} />
                </Header>
              }
              filter={
                <TextFilter
                  {...testStudio.filterProps}
                  filteringPlaceholder={intl.formatMessage(messages.testSetsFind)}
                  filteringAriaLabel={intl.formatMessage(messages.testSetsFindAriaLabel)}
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
            setSuccessMessage(intl.formatMessage(messages.saved, { name: q.name }));
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
        header={intl.formatMessage(messages.deleteTitle)}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setDeleting(null)}>
                <FormattedMessage {...messages.cancel} />
              </Button>
              <Button variant="primary" loading={deleteBusy} onClick={() => void confirmDelete()}>
                <FormattedMessage {...messages.delete} />
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          {deleteError && <Alert type="error">{deleteError}</Alert>}
          <Box>{deleting ? deleteConfirmText(deleting.queue, deleting.depth, intl) : null}</Box>
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
          const setName = settingsTarget?.name;
          setSuccessMessage(
            setName != null
              ? intl.formatMessage(messages.savedTestSet, { name: setName })
              : intl.formatMessage(messages.savedTestSetFallback),
          );
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
