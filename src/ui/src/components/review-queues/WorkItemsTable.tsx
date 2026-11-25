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
import { formatDateTime, formatRelativeTime, relativeTimeMessages } from '../../utils/date-time';
import { routedByText, shortened } from './RoutedBy';
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
  failedPolicyRules: { readonly count: number };
  preferences: NoMessageValues;
  confirmPreferences: NoMessageValues;
  cancelPreferences: NoMessageValues;
  pageSize: NoMessageValues;
  pageSizeOption: { readonly count: number };
  columnPreferences: NoMessageValues;
}>({
  columnDocument: {
    id: 'reviewQueues.workItemsTable.column.document',
    defaultMessage: 'Document',
    description:
      "Column header of a review work table (Home › Tasks, a queue page), and the name of that column in the table's settings: the document, as a link to its edit page.",
  },
  columnSource: {
    id: 'reviewQueues.workItemsTable.column.source',
    defaultMessage: 'Source',
    description:
      "Column header of a review work table, and the name of that column in the table's settings: whether the item comes from a production review queue or from Test Studio.",
  },
  columnQueue: {
    id: 'reviewQueues.workItemsTable.column.queue',
    defaultMessage: 'Queue',
    description:
      "Column header of a review work table, and the name of that column in the table's settings: the review queue (or Test Studio test set) the item is in.",
  },
  columnPriority: {
    id: 'reviewQueues.workItemsTable.column.priority',
    defaultMessage: 'Priority',
    description:
      "Column header of a review work table, and the name of that column in the table's settings: the item's priority, a number.",
  },
  columnAge: {
    id: 'reviewQueues.workItemsTable.column.age',
    defaultMessage: 'Age',
    description:
      "Column header of a review work table, and the name of that column in the table's settings: how long ago the item entered its queue or was assigned.",
  },
  columnReason: {
    id: 'reviewQueues.workItemsTable.column.reason',
    defaultMessage: 'Reason',
    description:
      "Column header of a review work table, and the name of that column in the table's settings: why the item is in review (the reason its routing rule gives, or its first alert, such as a field's low confidence).",
  },
  columnState: {
    id: 'reviewQueues.workItemsTable.column.state',
    defaultMessage: 'State',
    description:
      "Column header of a review work table, and the name of that column in the table's settings: the item's review state, such as Waiting or Assigned.",
  },
  columnOwner: {
    id: 'reviewQueues.workItemsTable.column.owner',
    defaultMessage: 'Annotator',
    description:
      "Column header of a review work table, and the name of that column in the table's settings: who the item is assigned to. Annotator is the name of a user role.",
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
    description:
      "After an item's first alert in the Reason column of a review work table: how many more alerts it has. {count} is at least 1.",
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
  failedPolicyRules: {
    id: 'reviewQueues.workItemsTable.failedPolicyRules',
    defaultMessage: '{count, plural, one {# failed policy rule} other {# failed policy rules}}',
    description:
      "After an item's reason in the Reason column of a review work table: how many policy rules its document failed. {count} is that number, at least 1.",
  },
  preferences: {
    id: 'reviewQueues.workItemsTable.preferences.title',
    defaultMessage: 'Preferences',
    description: "Title of a review work table's settings dialog, where the reader picks the page size and the columns shown.",
  },
  confirmPreferences: {
    id: 'reviewQueues.workItemsTable.preferences.confirm',
    defaultMessage: 'Confirm',
    description: "Button of a review work table's settings dialog that applies and keeps the choices.",
  },
  cancelPreferences: {
    id: 'reviewQueues.workItemsTable.preferences.cancel',
    defaultMessage: 'Cancel',
    description: "Button of a review work table's settings dialog that closes it and changes nothing.",
  },
  pageSize: {
    id: 'reviewQueues.workItemsTable.preferences.pageSize',
    defaultMessage: 'Page size',
    description: "Title of the page size choice in a review work table's settings dialog.",
  },
  pageSizeOption: {
    id: 'reviewQueues.workItemsTable.preferences.pageSizeOption',
    defaultMessage: '{count, plural, one {# item} other {# items}}',
    description: "A page size option in a review work table's settings dialog. {count} is the number of items shown per page.",
  },
  columnPreferences: {
    id: 'reviewQueues.workItemsTable.preferences.columns',
    defaultMessage: 'Column preferences',
    description: "Title of the choice of columns, and their order, in a review work table's settings dialog.",
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
 * REASON_CELL_CHARS is cut, and the whole of it opens in a popover. `firstAlert` is the
 * item's first alert in words, from its view.
 */
const ReasonCell = ({ item, firstAlert }: { item: WorkItem; firstAlert: string | null }): React.JSX.Element => {
  const intl = useIntl();
  const alert = item.routingReason ? null : firstAlert;
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
    // A width, not a least width: a short queue name leaves its room to the reason.
    width: 200,
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
    cell: (item) => <ReasonCell item={item} firstAlert={view(item).reason} />,
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
  const [kept, setKept] = useLocalStorage<Partial<WorkItemsPreferences> | null>(preferencesKey, null);
  const displayNames = useMemo(() => distinguishingNames(items.map((i) => i.objectKey)), [items]);
  const visible = useMemo(
    () => items.filter((i) => (!source || i.source === source) && matchesState(view(i), state)),
    [items, source, state, view],
  );
  // What the browser kept can be of another shape, or name other columns: it is read defensively.
  const keptPageSize = kept?.pageSize;
  const preferences: WorkItemsPreferences = {
    pageSize: typeof keptPageSize === 'number' && PAGE_SIZES.includes(keptPageSize) ? keptPageSize : DEFAULT_PAGE_SIZE,
    contentDisplay: columnDisplayOf(kept?.contentDisplay, columns, defaultVisible),
  };

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
        const rule = [item.routedBy?.ruleLabel ?? '', item.routedBy?.ruleId ?? ''];
        // A queue is found by the name shown and by its id; a reason by the routing reason, the first alert or the routing rule.
        return [item.objectKey, item.queue, queue, assignee ?? '', item.routingReason ?? '', reason ?? '', ...rule].some((v) =>
          v.toLowerCase().includes(t),
        );
      },
    },
    pagination: { pageSize: preferences.pageSize },
    sorting: {},
  });

  const columnsOffered = useMemo(() => {
    const all = baseColumns(intl, (item) => displayNames.get(item.objectKey) ?? item.name, view, sortBy);
    return columns.flatMap((id) => all.filter((c) => c.id === id));
  }, [intl, displayNames, view, sortBy, columns]);

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
      // The row menu is not a choice: it follows the columns the settings show.
      columnDisplay={[...preferences.contentDisplay, ...(onRowAction ? [{ id: ROW_ACTIONS_COLUMN, visible: true }] : [])]}
      trackBy="id"
      // A cell that does not wrap is as wide as its text, and a reason would push the table
      // past its page. The names in the other columns are cut to one line by themselves.
      wrapLines
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
      preferences={
        <CollectionPreferences
          title={intl.formatMessage(messages.preferences)}
          confirmLabel={intl.formatMessage(messages.confirmPreferences)}
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
