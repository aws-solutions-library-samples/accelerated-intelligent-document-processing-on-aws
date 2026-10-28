// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The HITL work table: production review items and Test Studio documents in one
// list, filterable by source and by assignment.
import React, { useMemo, useState } from 'react';
import { useCollection } from '@cloudscape-design/collection-hooks';
import type { ButtonDropdownProps, SelectProps, TableProps } from '@cloudscape-design/components';
import { Badge, Box, ButtonDropdown, Link, Pagination, Select, SpaceBetween, Table, TextFilter } from '@cloudscape-design/components';

import { paginationLabels } from '../common/labels';
import TruncatedText from '../common/TruncatedText';
import { NoDataState, TableHeader, TableNoMatchState, getFilterCounterText } from '../common/table';
import { formatDateTime, formatRelativeTime } from '../../utils/date-time';
import { SOURCE_LABELS } from './work-items';
import type { WorkItem, WorkSource } from './work-items';
import { workItemActions } from './use-work-item-actions';

const ALL_STATES: SelectProps.Option = { value: '', label: 'All items' };
const STATE_OPTIONS: SelectProps.Option[] = [
  ALL_STATES,
  { value: 'unassigned', label: 'Unassigned' },
  { value: 'assigned', label: 'Assigned' },
];

const matchesState = (item: WorkItem, state: string): boolean => {
  switch (state) {
    case 'unassigned':
      return !item.assignedTo;
    case 'assigned':
      return !!item.assignedTo;
    default:
      return true;
  }
};

const ALL_SOURCES: SelectProps.Option = { value: '', label: 'All sources' };
const SOURCE_OPTIONS: SelectProps.Option[] = [
  ALL_SOURCES,
  { value: 'production', label: SOURCE_LABELS.production },
  { value: 'test-set', label: SOURCE_LABELS['test-set'] },
];

const age = (iso: string | null): React.ReactNode =>
  iso ? <span title={formatDateTime(iso)}>{formatRelativeTime(iso)}</span> : <Box color="text-status-inactive">—</Box>;

const BASE_COLUMNS: TableProps.ColumnDefinition<WorkItem>[] = [
  {
    id: 'document',
    header: 'Document',
    minWidth: 240,
    cell: (item) => (
      <Link href={item.href} ariaLabel={`Open ${item.objectKey}`}>
        <TruncatedText maxChars={32}>{item.name}</TruncatedText>
      </Link>
    ),
    sortingField: 'name',
    isRowHeader: true,
  },
  {
    id: 'source',
    header: 'Source',
    width: 140,
    cell: (item) => <Badge color={item.source === 'production' ? 'blue' : 'grey'}>{SOURCE_LABELS[item.source]}</Badge>,
    sortingField: 'source',
  },
  {
    id: 'queue',
    header: 'Queue',
    minWidth: 240,
    cell: (item) => <TruncatedText maxChars={32}>{item.queue}</TruncatedText>,
    sortingField: 'queue',
  },
  { id: 'priority', header: 'Priority', width: 100, cell: (item) => item.priority ?? '—', sortingField: 'priority' },
  { id: 'age', header: 'Age', width: 110, cell: (item) => age(item.enqueuedAt), sortingField: 'enqueuedAt' },
  {
    id: 'reason',
    header: 'Top reason',
    cell: (item) =>
      item.reason ? (
        <span>
          {item.reason}
          {item.extraReasons > 0 && <Box variant="small" color="text-body-secondary">{` +${item.extraReasons} more`}</Box>}
        </span>
      ) : (
        '—'
      ),
  },
  {
    id: 'state',
    header: 'State',
    width: 120,
    cell: (item) => item.state.label,
  },
  {
    id: 'owner',
    header: 'Annotator',
    width: 210,
    cell: (item) => (item.assignedTo ? <TruncatedText maxChars={24}>{item.assignedTo}</TruncatedText> : '—'),
    sortingField: 'assignedTo',
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
  const [source, setSource] = useState<SelectProps.Option>(ALL_SOURCES);
  const [state, setState] = useState<SelectProps.Option>(ALL_STATES);
  const visible = useMemo(
    () => items.filter((i) => (!source.value || i.source === (source.value as WorkSource)) && matchesState(i, state.value ?? '')),
    [items, source, state],
  );

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
            setSource(ALL_SOURCES);
            setState(ALL_STATES);
          }}
        />
      ),
      filteringFunction: (item, text) => {
        const t = text.toLowerCase();
        return [item.objectKey, item.queue, item.assignedTo ?? '', item.reason ?? ''].some((v) => v.toLowerCase().includes(t));
      },
    },
    pagination: { pageSize: 25 },
    sorting: {},
  });

  return (
    <Table
      {...collectionProps}
      items={shown}
      columnDefinitions={[
        ...(columns ? columns.flatMap((id) => BASE_COLUMNS.filter((c) => c.id === id)) : BASE_COLUMNS),
        ...extraColumns,
        ...(onRowAction
          ? [
              {
                id: 'row-actions',
                header: 'Actions',
                cell: (item: WorkItem) => (
                  <ButtonDropdown
                    variant="inline-icon"
                    ariaLabel={`Actions for ${item.name}`}
                    expandToViewport
                    loading={busyRowId === item.id}
                    items={[...workItemActions(), ...(extraRowActions?.(item) ?? [])]}
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
      loadingText="Loading items"
      variant="container"
      selectionType={selectionType}
      ariaLabels={
        selectionType
          ? {
              selectionGroupLabel: 'Item selection',
              allItemsSelectionLabel: () => 'Select all items on this page',
              itemSelectionLabel: (_, item) => `Select ${item.name}`,
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
            filteringPlaceholder="Find items"
            filteringAriaLabel="Find items"
            countText={getFilterCounterText(filteredItemsCount ?? 0)}
          />
          {showStatusFilter && (
            <Select
              selectedOption={state}
              onChange={({ detail }) => setState(detail.selectedOption)}
              options={STATE_OPTIONS}
              ariaLabel="Filter by assignment"
            />
          )}
          {showSourceFilter && (
            <Select
              selectedOption={source}
              onChange={({ detail }) => setSource(detail.selectedOption)}
              options={SOURCE_OPTIONS}
              ariaLabel="Filter by source"
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
