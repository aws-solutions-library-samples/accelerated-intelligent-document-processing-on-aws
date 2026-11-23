// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The HITL work table: production review items and Test Studio documents in one
// list, filterable by source and by assignment.
//
// The table shows the columns its page names as essential and offers the others in its
// settings (column preferences and page size), kept per page in the browser. The reason
// column shows why the item is in review, shortened, with the whole text in a popover;
// it wraps in its column, so the table does not grow wider than its page.
import React, { useMemo, useState } from 'react';
import { useCollection } from '@cloudscape-design/collection-hooks';
import type { ButtonDropdownProps, SelectProps, TableProps } from '@cloudscape-design/components';
import {
  Badge,
  Box,
  ButtonDropdown,
  CollectionPreferences,
  Link,
  Pagination,
  Popover,
  Select,
  SpaceBetween,
  Table,
  TextFilter,
} from '@cloudscape-design/components';
import { defineMessages, useIntl, type IntlShape, type NoMessageValues } from 'react-intl';

import { distinguishingNames } from '../common/distinguishing-names';
import { usePaginationLabels } from '../common/labels';
import useLocalStorage from '../common/local-storage';
import TruncatedText from '../common/TruncatedText';
import { NoDataState, TableHeader, TableNoMatchState, filterCounterText } from '../common/table';
import { useFormatters } from '../../i18n/format';
import { formatDateTime, formatRelativeTime } from '../../utils/date-time';
import { routedByText, shortened } from './RoutedBy';
import { SOURCE_LABELS } from './work-items';
import type { WorkItem, WorkSource } from './work-items';
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
  moreReasons: { readonly count: number };
  failedPolicyRules: { readonly count: number };
  preferences: NoMessageValues;
  confirm: NoMessageValues;
  cancelPreferences: NoMessageValues;
  pageSize: NoMessageValues;
  pageSizeOption: { readonly count: number };
  columnPreferences: NoMessageValues;
}>({
  columnDocument: {
    id: 'reviewQueues.workItems.columnDocument',
    defaultMessage: 'Document',
    description: 'Column header (and column preference) of the work items table: the document, as a link that opens it.',
  },
  columnSource: {
    id: 'reviewQueues.workItems.columnSource',
    defaultMessage: 'Source',
    description:
      'Column header (and column preference) of the work items table: whether the item is production work or a Test Studio document.',
  },
  columnQueue: {
    id: 'reviewQueues.workItems.columnQueue',
    defaultMessage: 'Queue',
    description: 'Column header (and column preference) of the work items table: the review queue the item is in.',
  },
  columnPriority: {
    id: 'reviewQueues.workItems.columnPriority',
    defaultMessage: 'Priority',
    description: 'Column header (and column preference) of the work items table: the priority of the item.',
  },
  columnAge: {
    id: 'reviewQueues.workItems.columnAge',
    defaultMessage: 'Age',
    description: 'Column header (and column preference) of the work items table: how long the item has been in its queue.',
  },
  columnReason: {
    id: 'reviewQueues.workItems.columnReason',
    defaultMessage: 'Reason',
    description:
      'Column header (and column preference) of the work items table: why the item is in review (the reason of its routing rule, or its first alert).',
  },
  columnState: {
    id: 'reviewQueues.workItems.columnState',
    defaultMessage: 'State',
    description: 'Column header (and column preference) of the work items table: whether the item is waiting, assigned or in progress.',
  },
  columnOwner: {
    id: 'reviewQueues.workItems.columnOwner',
    defaultMessage: 'Annotator',
    description: 'Column header (and column preference) of the work items table: the Annotator the item is assigned to.',
  },
  moreReasons: {
    id: 'reviewQueues.workItems.moreReasons',
    defaultMessage: '+{count, number} more',
    description: 'After the first alert of a work item, in its reason cell: how many more alerts the item has. {count} is that number.',
  },
  failedPolicyRules: {
    id: 'reviewQueues.workItems.failedPolicyRules',
    defaultMessage: '{count, plural, one {# failed policy rule} other {# failed policy rules}}',
    description: 'After the reason of a work item, in its reason cell: how many policy rules its document failed. {count} is that number.',
  },
  preferences: {
    id: 'reviewQueues.workItems.preferences',
    defaultMessage: 'Preferences',
    description: 'Title of the settings dialog of the work items table.',
  },
  confirm: {
    id: 'reviewQueues.workItems.confirm',
    defaultMessage: 'Confirm',
    description: 'Confirm button of the settings dialog of the work items table.',
  },
  cancelPreferences: {
    id: 'reviewQueues.workItems.cancelPreferences',
    defaultMessage: 'Cancel',
    description: 'Cancel button of the settings dialog of the work items table.',
  },
  pageSize: {
    id: 'reviewQueues.workItems.pageSize',
    defaultMessage: 'Page size',
    description: 'Title of the page size choice in the settings of the work items table.',
  },
  pageSizeOption: {
    id: 'reviewQueues.workItems.pageSizeOption',
    defaultMessage: '{count, plural, one {# item} other {# items}}',
    description: 'A page size option in the settings of the work items table. {count} is the number of items per page.',
  },
  columnPreferences: {
    id: 'reviewQueues.workItems.columnPreferences',
    defaultMessage: 'Column preferences',
    description: 'Title of the column choice in the settings of the work items table.',
  },
});

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

/** An item's age in the UI locale; a column's `cell` cannot call a hook, so this is a component. */
const Age = ({ iso }: { iso: string | null }): React.JSX.Element => {
  const { locale } = useFormatters();
  return iso ? (
    <span title={formatDateTime(iso, locale)}>{formatRelativeTime(iso, locale)}</span>
  ) : (
    <Box color="text-status-inactive">—</Box>
  );
};

/** The queue as people know it: its name, or its id when the API reports no name. */
const queueLabel = (item: WorkItem): string => item.queueName ?? item.queue;

/** Characters of the document name shown before its middle is cut. */
const DOCUMENT_NAME_CHARS = 40;

/** Characters of a reason shown in its cell; the whole reason is in the cell's popover. */
export const REASON_CELL_CHARS = 60;

/** The id of the row menu's column, which is always shown, last. */
const ROW_ACTIONS_COLUMN = 'row-actions';

/** The base columns: the id, the message naming it (header and column preference), and whether it is always shown. */
export const WORK_ITEM_COLUMNS = [
  { id: 'document', label: messages.columnDocument, alwaysVisible: true },
  { id: 'source', label: messages.columnSource },
  { id: 'queue', label: messages.columnQueue },
  { id: 'priority', label: messages.columnPriority },
  { id: 'age', label: messages.columnAge },
  { id: 'reason', label: messages.columnReason },
  { id: 'state', label: messages.columnState },
  { id: 'owner', label: messages.columnOwner },
] as const;

const isAlwaysVisible = (id: string): boolean => WORK_ITEM_COLUMNS.some((column) => column.id === id && 'alwaysVisible' in column);

/**
 * Why the item is in review, in a few words: the reason of its routing rule when it has
 * one, else its first alert (and how many more there are), else the routing rule that
 * placed it; then how many policy rules its document failed. A text longer than
 * REASON_CELL_CHARS is cut, and the whole of it opens in a popover.
 */
const ReasonCell = ({ item }: { item: WorkItem }): React.JSX.Element => {
  const intl = useIntl();
  const alert = item.routingReason ? null : item.reason;
  const text = item.routingReason || alert || (item.routedBy ? routedByText(intl, item.routedBy) : '');
  if (!text) return <>—</>;
  const short = shortened(text, REASON_CELL_CHARS);
  return (
    <span>
      {short === text ? (
        text
      ) : (
        <Popover triggerType="text-inline" dismissButton={false} size="large" content={text}>
          {short}
        </Popover>
      )}
      {alert && item.extraReasons > 0 && (
        <>
          {' '}
          <Box variant="small" color="text-body-secondary">
            {intl.formatMessage(messages.moreReasons, { count: item.extraReasons })}
          </Box>
        </>
      )}
      {item.policyFailureCount > 0 && (
        <>
          {' · '}
          <Box variant="small" color="text-body-secondary">
            {intl.formatMessage(messages.failedPolicyRules, { count: item.policyFailureCount })}
          </Box>
        </>
      )}
    </span>
  );
};

/** The base columns; `displayName` is the row's distinguishing document name. */
const baseColumns = (intl: IntlShape, displayName: (item: WorkItem) => string): TableProps.ColumnDefinition<WorkItem>[] => [
  {
    id: 'document',
    header: intl.formatMessage(messages.columnDocument),
    minWidth: 240,
    cell: (item) => (
      <Link href={item.href} ariaLabel={`Open ${item.objectKey}`}>
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
    cell: (item) => <Badge color={item.source === 'production' ? 'blue' : 'grey'}>{SOURCE_LABELS[item.source]}</Badge>,
    sortingField: 'source',
  },
  {
    id: 'queue',
    header: intl.formatMessage(messages.columnQueue),
    // A width, not a least width: a short queue name leaves its room to the reason.
    width: 200,
    cell: (item) => <TruncatedText maxChars={32}>{queueLabel(item)}</TruncatedText>,
    sortingComparator: (a, b) => queueLabel(a).localeCompare(queueLabel(b)),
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
  { id: 'reason', header: intl.formatMessage(messages.columnReason), cell: (item) => <ReasonCell item={item} /> },
  {
    id: 'state',
    header: intl.formatMessage(messages.columnState),
    width: 120,
    cell: (item) => item.state.label,
  },
  {
    id: 'owner',
    header: intl.formatMessage(messages.columnOwner),
    width: 210,
    cell: (item) => (item.assignedTo ? <TruncatedText maxChars={24}>{item.assignedTo}</TruncatedText> : '—'),
    sortingField: 'assignedTo',
  },
];

interface ColumnChoice {
  id: string;
  visible: boolean;
}

/** What the table's settings keep: the page size, and each column's place and whether it is shown. */
interface WorkItemsPreferences {
  pageSize: number;
  contentDisplay: ColumnChoice[];
}

/** The page sizes offered in the table's settings. */
const PAGE_SIZES = [10, 25, 50];
const DEFAULT_PAGE_SIZE = 25;

/** The column choices among what the browser kept, which can be of another shape; none when it kept none. */
const keptChoices = (kept: unknown): ColumnChoice[] =>
  Array.isArray(kept)
    ? (kept as unknown[]).flatMap((entry) => {
        const { id, visible } = (entry ?? {}) as Partial<ColumnChoice>;
        return typeof id === 'string' ? [{ id, visible: visible === true }] : [];
      })
    : [];

/**
 * The columns in the order the settings keep, each with whether it is shown. The kept
 * choices are brought in line with the columns the table has now: a column the settings
 * do not know yet is added after the others, shown when it is one of `defaultVisible`; a
 * kept column the table no longer has is dropped; a column that is always shown stays shown.
 */
export const columnDisplayOf = (kept: unknown, columns: readonly string[], defaultVisible: readonly string[]): ColumnChoice[] => {
  const choices = keptChoices(kept);
  const known = choices.filter(
    (entry, index) => columns.includes(entry.id) && choices.findIndex((other) => other.id === entry.id) === index,
  );
  const added = columns.filter((id) => !known.some((entry) => entry.id === id));
  return [
    ...known.map((entry) => ({ id: entry.id, visible: entry.visible || isAlwaysVisible(entry.id) })),
    ...added.map((id) => ({ id, visible: defaultVisible.includes(id) || isAlwaysVisible(id) })),
  ];
};

interface WorkItemsTableProps {
  items: readonly WorkItem[];
  loading?: boolean;
  title: string;
  description?: string;
  actions?: React.ReactNode;
  /** Offer only these base columns, in this order (ids: document, source, queue, priority, age, reason, state, owner). */
  columns?: readonly string[];
  /** The columns shown until the user changes them in the table's settings; all of `columns` when left out. */
  defaultVisible?: readonly string[];
  /** The browser storage key the table's settings are kept under: one per page that shows the table. */
  preferencesKey: string;
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

const ALL_COLUMNS: readonly string[] = WORK_ITEM_COLUMNS.map((column) => column.id);

const WorkItemsTable = ({
  items,
  loading = false,
  title,
  description,
  actions,
  columns = ALL_COLUMNS,
  defaultVisible = columns,
  preferencesKey,
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
  const [source, setSource] = useState<SelectProps.Option>(ALL_SOURCES);
  const [state, setState] = useState<SelectProps.Option>(ALL_STATES);
  const [kept, setKept] = useLocalStorage<Partial<WorkItemsPreferences> | null>(preferencesKey, null);
  const displayNames = useMemo(() => distinguishingNames(items.map((i) => i.objectKey)), [items]);
  const visible = useMemo(
    () => items.filter((i) => (!source.value || i.source === (source.value as WorkSource)) && matchesState(i, state.value ?? '')),
    [items, source, state],
  );
  // What the browser kept can be of another shape, or name other columns: it is read defensively.
  const keptPageSize = kept?.pageSize;
  const preferences: WorkItemsPreferences = {
    pageSize: typeof keptPageSize === 'number' && PAGE_SIZES.includes(keptPageSize) ? keptPageSize : DEFAULT_PAGE_SIZE,
    contentDisplay: columnDisplayOf(kept?.contentDisplay, columns, defaultVisible),
  };

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
        // A queue is found by the name shown and by its id.
        const rule = [item.routedBy?.ruleLabel ?? '', item.routedBy?.ruleId ?? ''];
        return [
          item.objectKey,
          item.queue,
          queueLabel(item),
          item.assignedTo ?? '',
          item.routingReason ?? '',
          item.reason ?? '',
          ...rule,
        ].some((v) => v.toLowerCase().includes(t));
      },
    },
    pagination: { pageSize: preferences.pageSize },
    sorting: {},
  });

  const columnsOffered = useMemo(() => {
    const all = baseColumns(intl, (item) => displayNames.get(item.objectKey) ?? item.name);
    return columns.flatMap((id) => all.filter((c) => c.id === id));
  }, [intl, displayNames, columns]);

  return (
    <Table
      {...collectionProps}
      items={shown}
      columnDefinitions={[
        ...columnsOffered,
        ...(onRowAction
          ? [
              {
                id: ROW_ACTIONS_COLUMN,
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
      // The row menu is not a choice: it follows the columns the settings show.
      columnDisplay={[...preferences.contentDisplay, ...(onRowAction ? [{ id: ROW_ACTIONS_COLUMN, visible: true }] : [])]}
      trackBy="id"
      // A cell that does not wrap is as wide as its text, and a reason would push the table
      // past its page. The names in the other columns are cut to one line by themselves.
      wrapLines
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
            countText={filterCounterText(intl, filteredItemsCount ?? 0)}
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
      preferences={
        <CollectionPreferences
          title={intl.formatMessage(messages.preferences)}
          confirmLabel={intl.formatMessage(messages.confirm)}
          cancelLabel={intl.formatMessage(messages.cancelPreferences)}
          preferences={preferences}
          onConfirm={({ detail }) =>
            setKept({
              pageSize: detail.pageSize ?? preferences.pageSize,
              contentDisplay: [...(detail.contentDisplay ?? preferences.contentDisplay)],
            })
          }
          pageSizePreference={{
            title: intl.formatMessage(messages.pageSize),
            options: PAGE_SIZES.map((count) => ({ value: count, label: intl.formatMessage(messages.pageSizeOption, { count }) })),
          }}
          contentDisplayPreference={{
            title: intl.formatMessage(messages.columnPreferences),
            options: WORK_ITEM_COLUMNS.filter((column) => columns.includes(column.id)).map((column) => ({
              id: column.id,
              label: intl.formatMessage(column.label),
              alwaysVisible: isAlwaysVisible(column.id),
            })),
          }}
        />
      }
      footer={footer}
    />
  );
};

export default WorkItemsTable;
