// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The HITL work table: production review items and Test Studio documents in one
// list, filterable by source and by assignment.
import React, { useMemo, useState } from 'react';
import { useCollection } from '@cloudscape-design/collection-hooks';
import type { ButtonDropdownProps, SelectProps, TableProps } from '@cloudscape-design/components';
import { Badge, Box, ButtonDropdown, Link, Pagination, Select, SpaceBetween, Table, TextFilter } from '@cloudscape-design/components';
import { defineMessages, useIntl, type IntlShape, type NoMessageValues } from 'react-intl';

import { distinguishingNames } from '../common/distinguishing-names';
import { usePaginationLabels } from '../common/labels';
import TruncatedText from '../common/TruncatedText';
import { NoDataState, TableHeader, TableNoMatchState, filterCounterText } from '../common/table';
import { useFormatters } from '../../i18n/format';
import { formatDateTime, formatRelativeTime, relativeTimeMessages } from '../../utils/date-time';
import { workItemView, workSourceLabel } from './work-items';
import type { WorkItem, WorkItemView, WorkSource } from './work-items';
import { workItemActions } from './use-work-item-actions';

const messages = defineMessages<{
  columnDocument: NoMessageValues;
  columnSource: NoMessageValues;
  columnQueue: NoMessageValues;
  columnPriority: NoMessageValues;
  columnAge: NoMessageValues;
  columnReason: NoMessageValues;
  columnState: NoMessageValues;
  columnOwner: NoMessageValues;
  columnActions: NoMessageValues;
  openDocument: { readonly objectKey: string };
  moreReasons: { readonly count: number };
  rowActions: { readonly name: string };
  loading: NoMessageValues;
  selectionGroup: NoMessageValues;
  selectAll: NoMessageValues;
  selectItem: { readonly name: string };
  find: NoMessageValues;
  assignmentFilter: NoMessageValues;
  allItems: NoMessageValues;
  unassigned: NoMessageValues;
  assigned: NoMessageValues;
  sourceFilter: NoMessageValues;
  allSources: NoMessageValues;
}>({
  columnDocument: {
    id: 'reviewQueues.workItemsTable.column.document',
    defaultMessage: 'Document',
    description: 'Column header of a review work table (Home › Tasks, a queue page): the document, as a link to its edit page.',
  },
  columnSource: {
    id: 'reviewQueues.workItemsTable.column.source',
    defaultMessage: 'Source',
    description: 'Column header of a review work table: whether the item comes from a production review queue or from Test Studio.',
  },
  columnQueue: {
    id: 'reviewQueues.workItemsTable.column.queue',
    defaultMessage: 'Queue',
    description: 'Column header of a review work table: the review queue (or Test Studio test set) the item is in.',
  },
  columnPriority: {
    id: 'reviewQueues.workItemsTable.column.priority',
    defaultMessage: 'Priority',
    description: "Column header of a review work table: the item's priority, a number.",
  },
  columnAge: {
    id: 'reviewQueues.workItemsTable.column.age',
    defaultMessage: 'Age',
    description: 'Column header of a review work table: how long ago the item entered its queue or was assigned.',
  },
  columnReason: {
    id: 'reviewQueues.workItemsTable.column.reason',
    defaultMessage: 'Top reason',
    description: "Column header of a review work table: the main reason the item needs review, such as a field's low confidence.",
  },
  columnState: {
    id: 'reviewQueues.workItemsTable.column.state',
    defaultMessage: 'State',
    description: "Column header of a review work table: the item's review state, such as Waiting or Assigned.",
  },
  columnOwner: {
    id: 'reviewQueues.workItemsTable.column.owner',
    defaultMessage: 'Annotator',
    description: 'Column header of a review work table: who the item is assigned to. Annotator is the name of a user role.',
  },
  columnActions: {
    id: 'reviewQueues.workItemsTable.column.actions',
    defaultMessage: 'Actions',
    description: "Column header of a review work table's row menus.",
  },
  openDocument: {
    id: 'reviewQueues.workItemsTable.openDocument',
    defaultMessage: 'Open {objectKey}',
    description:
      "Screen-reader label of a review work table's document link, which opens its edit page. {objectKey} is the document's full name.",
  },
  moreReasons: {
    id: 'reviewQueues.workItemsTable.moreReasons',
    defaultMessage: '+{count} more',
    description: "After an item's top reason in a review work table: how many more reasons it has. {count} is at least 1.",
  },
  rowActions: {
    id: 'reviewQueues.workItemsTable.rowActions',
    defaultMessage: 'Actions for {name}',
    description: "Screen-reader label of a review work table row's menu button. {name} is the document's file name.",
  },
  loading: {
    id: 'reviewQueues.workItemsTable.loading',
    defaultMessage: 'Loading items',
    description: 'Shown in a review work table while its items load.',
  },
  selectionGroup: {
    id: 'reviewQueues.workItemsTable.selection.group',
    defaultMessage: 'Item selection',
    description: "Screen-reader label of a review work table's selection checkboxes, as a group.",
  },
  selectAll: {
    id: 'reviewQueues.workItemsTable.selection.all',
    defaultMessage: 'Select all items on this page',
    description: "Screen-reader label of a review work table's select-all checkbox.",
  },
  selectItem: {
    id: 'reviewQueues.workItemsTable.selection.item',
    defaultMessage: 'Select {name}',
    description: "Screen-reader label of a review work table row's checkbox. {name} is the document's file name.",
  },
  find: {
    id: 'reviewQueues.workItemsTable.find',
    defaultMessage: 'Find items',
    description: "Placeholder and screen-reader label of a review work table's text filter.",
  },
  assignmentFilter: {
    id: 'reviewQueues.workItemsTable.assignment.filter',
    defaultMessage: 'Filter by assignment',
    description: "Screen-reader label of a review work table's filter on whether items are assigned.",
  },
  allItems: {
    id: 'reviewQueues.workItemsTable.assignment.all',
    defaultMessage: 'All items',
    description: "Option of a review work table's assignment filter: every item, assigned or not.",
  },
  unassigned: {
    id: 'reviewQueues.workItemsTable.assignment.unassigned',
    defaultMessage: 'Unassigned',
    description: "Option of a review work table's assignment filter: items no one is assigned to.",
  },
  assigned: {
    id: 'reviewQueues.workItemsTable.assignment.assigned',
    defaultMessage: 'Assigned',
    description: "Option of a review work table's assignment filter: items assigned to an Annotator.",
  },
  sourceFilter: {
    id: 'reviewQueues.workItemsTable.source.filter',
    defaultMessage: 'Filter by source',
    description: "Screen-reader label of a review work table's filter on the items' source (production or Test Studio).",
  },
  allSources: {
    id: 'reviewQueues.workItemsTable.source.all',
    defaultMessage: 'All sources',
    description: "Option of a review work table's source filter: items from production queues and from Test Studio.",
  },
});

const matchesState = (view: WorkItemView, state: string): boolean => {
  switch (state) {
    case 'unassigned':
      return !view.assignee;
    case 'assigned':
      return !!view.assignee;
    default:
      return true;
  }
};

/** An item's age in the UI locale; a column's `cell` cannot call a hook, so this is a component. */
const Age = ({ iso }: { iso: string | null }): React.JSX.Element => {
  const intl = useIntl();
  const { locale } = useFormatters();
  return iso ? (
    <span title={formatDateTime(iso, locale)}>
      {formatRelativeTime(iso, locale, undefined, intl.formatMessage(relativeTimeMessages.justNow))}
    </span>
  ) : (
    <Box color="text-status-inactive">—</Box>
  );
};

/** Characters of the document name shown before its middle is cut. */
const DOCUMENT_NAME_CHARS = 40;

type Comparator = (a: WorkItem, b: WorkItem) => number;

/** The base columns; `displayName` is the row's distinguishing document name, `view` its labels. */
const baseColumns = (
  intl: IntlShape,
  displayName: (item: WorkItem) => string,
  view: (item: WorkItem) => WorkItemView,
  sortBy: { queue: Comparator; owner: Comparator },
): TableProps.ColumnDefinition<WorkItem>[] => [
  {
    id: 'document',
    header: intl.formatMessage(messages.columnDocument),
    minWidth: 240,
    cell: (item) => (
      <Link href={item.href} ariaLabel={intl.formatMessage(messages.openDocument, { objectKey: item.objectKey })}>
        <TruncatedText maxChars={DOCUMENT_NAME_CHARS} ellipsis="middle" title={item.objectKey}>
          {displayName(item)}
        </TruncatedText>
      </Link>
    ),
    sortingComparator: (a, b) => displayName(a).localeCompare(displayName(b)),
    isRowHeader: true,
  },
  {
    id: 'source',
    header: intl.formatMessage(messages.columnSource),
    width: 140,
    cell: (item) => <Badge color={item.source === 'production' ? 'blue' : 'grey'}>{workSourceLabel(item.source, intl)}</Badge>,
    sortingField: 'source',
  },
  {
    id: 'queue',
    header: intl.formatMessage(messages.columnQueue),
    minWidth: 240,
    cell: (item) => <TruncatedText maxChars={32}>{view(item).queue}</TruncatedText>,
    sortingComparator: sortBy.queue,
  },
  {
    id: 'priority',
    header: intl.formatMessage(messages.columnPriority),
    width: 100,
    cell: (item) => item.priority ?? '—',
    sortingField: 'priority',
  },
  {
    id: 'age',
    header: intl.formatMessage(messages.columnAge),
    width: 110,
    cell: (item) => <Age iso={item.enqueuedAt} />,
    sortingField: 'enqueuedAt',
  },
  {
    id: 'reason',
    header: intl.formatMessage(messages.columnReason),
    cell: (item) => {
      const { reason } = view(item);
      return reason ? (
        <span>
          {reason}
          {item.extraReasons > 0 && (
            <Box variant="small" color="text-body-secondary">
              {` ${intl.formatMessage(messages.moreReasons, { count: item.extraReasons })}`}
            </Box>
          )}
        </span>
      ) : (
        '—'
      );
    },
  },
  {
    id: 'state',
    header: intl.formatMessage(messages.columnState),
    width: 120,
    cell: (item) => view(item).state,
  },
  {
    id: 'owner',
    header: intl.formatMessage(messages.columnOwner),
    width: 210,
    cell: (item) => {
      const { assignee } = view(item);
      return assignee ? <TruncatedText maxChars={24}>{assignee}</TruncatedText> : '—';
    },
    sortingComparator: sortBy.owner,
  },
];

interface WorkItemsTableProps {
  items: readonly WorkItem[];
  loading?: boolean;
  title: string;
  description?: string;
  actions?: React.ReactNode;
  extraColumns?: TableProps.ColumnDefinition<WorkItem>[];
  /** Show only these base columns, in this order (ids: document, source, queue, priority, age, reason, state, owner). */
  columns?: readonly string[];
  /** Offer the source filter (only when both sources can appear). */
  showSourceFilter?: boolean;
  selectionType?: TableProps.SelectionType;
  selectedItems?: WorkItem[];
  onSelectionChange?: (items: WorkItem[]) => void;
  isItemDisabled?: (item: WorkItem) => boolean;
  footer?: React.ReactNode;
  /** Offer the assignment filter (unassigned, assigned). */
  showStatusFilter?: boolean;
  /** Adds the row "⋮" menu (Edit, plus `extraRowActions`). */
  onRowAction?: (item: WorkItem, action: string) => void;
  /** More row menu entries, e.g. the Admin actions. */
  extraRowActions?: (item: WorkItem) => ButtonDropdownProps.ItemOrGroup[];
  /** The row whose action is running (its menu shows a spinner). */
  busyRowId?: string | null;
  /** What an empty table says (what is missing and what to do next). */
  empty?: React.ReactNode;
}

const WorkItemsTable = ({
  items,
  loading = false,
  title,
  description,
  actions,
  extraColumns = [],
  columns,
  showSourceFilter = false,
  selectionType,
  selectedItems,
  onSelectionChange,
  isItemDisabled,
  footer,
  onRowAction,
  extraRowActions,
  busyRowId,
  showStatusFilter = false,
  empty,
}: WorkItemsTableProps): React.JSX.Element => {
  const intl = useIntl();
  const paginationLabels = usePaginationLabels();
  // The filters hold values; their options, and so their labels, are built per render.
  const [source, setSource] = useState<WorkSource | ''>('');
  const [state, setState] = useState('');
  // Each row worded once per language; the cells, the filters and the sorting share it.
  const view = useMemo(() => {
    const views = new WeakMap<WorkItem, WorkItemView>();
    return (item: WorkItem): WorkItemView => {
      let worded = views.get(item);
      if (!worded) {
        worded = workItemView(item, intl);
        views.set(item, worded);
      }
      return worded;
    };
  }, [intl]);
  // Kept across polls, so a table sorted by one of these columns stays sorted by it.
  const sortBy = useMemo(
    () => ({
      queue: (a: WorkItem, b: WorkItem) => view(a).queue.localeCompare(view(b).queue),
      owner: (a: WorkItem, b: WorkItem) => (view(a).assignee ?? '').localeCompare(view(b).assignee ?? ''),
    }),
    [view],
  );
  const displayNames = useMemo(() => distinguishingNames(items.map((i) => i.objectKey)), [items]);
  const visible = useMemo(
    () => items.filter((i) => (!source || i.source === source) && matchesState(view(i), state)),
    [items, source, state, view],
  );

  const stateOptions: SelectProps.Option[] = [
    { value: '', label: intl.formatMessage(messages.allItems) },
    { value: 'unassigned', label: intl.formatMessage(messages.unassigned) },
    { value: 'assigned', label: intl.formatMessage(messages.assigned) },
  ];
  const sourceOptions: SelectProps.Option[] = [
    { value: '', label: intl.formatMessage(messages.allSources) },
    { value: 'production', label: workSourceLabel('production', intl) },
    { value: 'test-set', label: workSourceLabel('test-set', intl) },
  ];

  const {
    items: shown,
    actions: collectionActions,
    filteredItemsCount,
    collectionProps,
    filterProps,
    paginationProps,
  } = useCollection(visible, {
    filtering: {
      empty: empty ? (
        <Box textAlign="center" color="inherit" padding="s">
          {empty}
        </Box>
      ) : (
        <NoDataState />
      ),
      noMatch: (
        <TableNoMatchState
          onClearFilter={() => {
            collectionActions.setFiltering('');
            setSource('');
            setState('');
          }}
        />
      ),
      filteringFunction: (item, text) => {
        const t = text.toLowerCase();
        const { queue, assignee, reason } = view(item);
        return [item.objectKey, queue, assignee ?? '', reason ?? ''].some((v) => v.toLowerCase().includes(t));
      },
    },
    pagination: { pageSize: 25 },
    sorting: {},
  });

  const columnsShown = useMemo(() => {
    const all = baseColumns(intl, (item) => displayNames.get(item.objectKey) ?? item.name, view, sortBy);
    return columns ? columns.flatMap((id) => all.filter((c) => c.id === id)) : all;
  }, [intl, displayNames, view, sortBy, columns]);

  return (
    <Table
      {...collectionProps}
      items={shown}
      columnDefinitions={[
        ...columnsShown,
        ...extraColumns,
        ...(onRowAction
          ? [
              {
                id: 'row-actions',
                header: intl.formatMessage(messages.columnActions),
                cell: (item: WorkItem) => (
                  <ButtonDropdown
                    variant="inline-icon"
                    ariaLabel={intl.formatMessage(messages.rowActions, { name: item.name })}
                    expandToViewport
                    loading={busyRowId === item.id}
                    items={[...workItemActions(intl), ...(extraRowActions?.(item) ?? [])]}
                    onItemClick={({ detail }) => onRowAction(item, detail.id)}
                  />
                ),
                width: 60,
              } as TableProps.ColumnDefinition<WorkItem>,
            ]
          : []),
      ]}
      trackBy="id"
      loading={loading}
      loadingText={intl.formatMessage(messages.loading)}
      variant="container"
      selectionType={selectionType}
      ariaLabels={
        selectionType
          ? {
              selectionGroupLabel: intl.formatMessage(messages.selectionGroup),
              allItemsSelectionLabel: () => intl.formatMessage(messages.selectAll),
              itemSelectionLabel: (_, item) => intl.formatMessage(messages.selectItem, { name: item.name }),
            }
          : undefined
      }
      selectedItems={selectedItems}
      isItemDisabled={isItemDisabled}
      onSelectionChange={onSelectionChange ? ({ detail }) => onSelectionChange(detail.selectedItems) : undefined}
      header={<TableHeader title={title} description={description} totalItems={visible} actionButtons={actions} />}
      filter={
        <SpaceBetween direction="horizontal" size="xs" alignItems="center">
          <TextFilter
            {...filterProps}
            filteringPlaceholder={intl.formatMessage(messages.find)}
            filteringAriaLabel={intl.formatMessage(messages.find)}
            countText={filterCounterText(intl, filteredItemsCount ?? 0)}
          />
          {showStatusFilter && (
            <Select
              selectedOption={stateOptions.find((o) => o.value === state) ?? stateOptions[0]}
              onChange={({ detail }) => setState(detail.selectedOption.value ?? '')}
              options={stateOptions}
              ariaLabel={intl.formatMessage(messages.assignmentFilter)}
            />
          )}
          {showSourceFilter && (
            <Select
              selectedOption={sourceOptions.find((o) => o.value === source) ?? sourceOptions[0]}
              onChange={({ detail }) => setSource((detail.selectedOption.value ?? '') as WorkSource | '')}
              options={sourceOptions}
              ariaLabel={intl.formatMessage(messages.sourceFilter)}
            />
          )}
        </SpaceBetween>
      }
      pagination={<Pagination {...paginationProps} ariaLabels={paginationLabels} />}
      footer={footer}
    />
  );
};

export default WorkItemsTable;
