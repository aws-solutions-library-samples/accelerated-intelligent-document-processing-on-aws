// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The Inbox messages table. It loads every message (the per-user cap bounds them)
// so the text filter and the type and severity filters work client-side, and it
// reloads whenever the shared poll reports a change — unless the user has rows
// selected, in which case it only flags the list as out of date.
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useCollection } from '@cloudscape-design/collection-hooks';
import type { SelectProps, TableProps } from '@cloudscape-design/components';
import {
  Alert,
  Box,
  Button,
  ButtonDropdown,
  CollectionPreferences,
  Modal,
  Pagination,
  SegmentedControl,
  Select,
  SpaceBetween,
  Table,
  TextFilter,
} from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';

import { generateClient } from '../../api/client-shim';
import { getInboxPreferences as getInboxPreferencesQuery, listInboxMessages as listInboxMessagesQuery } from '../../graphql/generated';
import type { InboxMessage, InboxMessageType, ListInboxMessagesQuery } from '../../graphql/generated/operation-types';
import useInbox from '../../hooks/use-inbox';
import { describeApiError } from '../../hooks/utils/graphql-error';
import { useFormatters } from '../../i18n/format';
import { usePaginationLabels } from '../common/labels';
import useLocalStorage from '../common/local-storage';
import { TableEmptyState, TableHeader, TableNoMatchState, filterCounterText } from '../common/table';
import TruncatedText from '../common/TruncatedText';
import { formatDateTime, formatRelativeTime, relativeTimeMessages } from '../../utils/date-time';
import DeleteInboxMessagesModal from './DeleteInboxMessagesModal';
import InboxMessageModal from './InboxMessageModal';
import { messageCategory } from './message-category';
import { INBOX_SEVERITIES, severityLabel, severityRank } from './severity';

const client = generateClient();
const logger = new ConsoleLogger('InboxMessagesTable');

const PAGE_LIMIT = 100;
const MAX_PAGES = 10;

const messages = defineMessages<{
  emptyTitle: NoMessageValues;
  emptyBody: NoMessageValues;
  allTypes: NoMessageValues;
  allSeverities: NoMessageValues;
  noSelection: NoMessageValues;
  columnReceived: NoMessageValues;
  columnTitle: NoMessageValues;
  columnType: NoMessageValues;
  columnSeverity: NoMessageValues;
  columnAction: NoMessageValues;
  rowActions: { readonly title: string };
  rowOpen: NoMessageValues;
  rowMarkUnread: NoMessageValues;
  rowMarkRead: NoMessageValues;
  rowDelete: NoMessageValues;
  loadFailed: NoMessageValues;
  loadMessagesAction: NoMessageValues;
  retry: NoMessageValues;
  stale: NoMessageValues;
  refresh: NoMessageValues;
  loading: NoMessageValues;
  selectionGroup: NoMessageValues;
  selectAll: NoMessageValues;
  selectItem: { readonly title: string };
  heading: NoMessageValues;
  markSelectedRead: NoMessageValues;
  markSelectedReadHint: NoMessageValues;
  markSelectedUnread: NoMessageValues;
  markSelectedUnreadHint: NoMessageValues;
  deleteSelected: NoMessageValues;
  deleteSelectedHint: NoMessageValues;
  moreMarkAllRead: NoMessageValues;
  moreNoUnread: NoMessageValues;
  moreClearRead: NoMessageValues;
  moreNoRead: NoMessageValues;
  moreActions: NoMessageValues;
  refreshMessages: NoMessageValues;
  find: NoMessageValues;
  readStateLabel: NoMessageValues;
  readStateAll: NoMessageValues;
  readStateUnread: NoMessageValues;
  filterByType: NoMessageValues;
  filterBySeverity: NoMessageValues;
  preferencesTitle: NoMessageValues;
  preferencesConfirm: NoMessageValues;
  preferencesCancel: NoMessageValues;
  preferencesPageSize: NoMessageValues;
  preferencesPageSizeOption: { readonly count: number };
  clearReadHeader: NoMessageValues;
  clearReadCancel: NoMessageValues;
  clearReadConfirm: NoMessageValues;
  clearReadBody: NoMessageValues;
}>({
  emptyTitle: {
    id: 'inbox.messagesTable.empty.title',
    defaultMessage: 'No messages',
    description: 'Heading the Inbox table shows when the user has no message.',
  },
  emptyBody: {
    id: 'inbox.messagesTable.empty.body',
    defaultMessage: "You'll see messages here when work is assigned to you or something needs your attention.",
    description: 'Text under the heading the Inbox table shows when the user has no message.',
  },
  allTypes: {
    id: 'inbox.messagesTable.allTypes',
    defaultMessage: 'All types',
    description: "First option of the Inbox table's message type filter: no filtering by type.",
  },
  allSeverities: {
    id: 'inbox.messagesTable.allSeverities',
    defaultMessage: 'All severities',
    description: "First option of the Inbox table's severity filter: no filtering by severity.",
  },
  noSelection: {
    id: 'inbox.messagesTable.noSelection',
    defaultMessage: 'Select one or more messages',
    description: "Tooltip of the Inbox table's Mark as read, Mark as unread and Delete buttons while no message is selected.",
  },
  columnReceived: {
    id: 'inbox.messagesTable.column.received',
    defaultMessage: 'Received',
    description: 'Column header of the Inbox table: when each message arrived.',
  },
  columnTitle: {
    id: 'inbox.messagesTable.column.title',
    defaultMessage: 'Title',
    description: 'Column header of the Inbox table: the title of each message.',
  },
  columnType: {
    id: 'inbox.messagesTable.column.type',
    defaultMessage: 'Type',
    description: 'Column header of the Inbox table: the kind of message (Task, Queue, or the type name).',
  },
  columnSeverity: {
    id: 'inbox.messagesTable.column.severity',
    defaultMessage: 'Severity',
    description: 'Column header of the Inbox table: the severity of each message (Info, Warning, ...).',
  },
  columnAction: {
    id: 'inbox.messagesTable.column.action',
    defaultMessage: 'Action',
    description: "Column header of the Inbox table: each row's menu of actions.",
  },
  rowActions: {
    id: 'inbox.messagesTable.rowActions',
    defaultMessage: 'Actions for {title}',
    description: "Screen-reader label of a row's action menu in the Inbox table. {title} is the message title, as the platform writes it.",
  },
  rowOpen: {
    id: 'inbox.messagesTable.row.open',
    defaultMessage: 'Open',
    description: "Item of a row's action menu in the Inbox table that opens the message.",
  },
  rowMarkUnread: {
    id: 'inbox.messagesTable.row.markUnread',
    defaultMessage: 'Mark as unread',
    description: "Item of a read message's action menu in the Inbox table.",
  },
  rowMarkRead: {
    id: 'inbox.messagesTable.row.markRead',
    defaultMessage: 'Mark as read',
    description: "Item of an unread message's action menu in the Inbox table.",
  },
  rowDelete: {
    id: 'inbox.messagesTable.row.delete',
    defaultMessage: 'Delete',
    description: "Item of a row's action menu in the Inbox table that deletes the message, after a confirmation.",
  },
  loadFailed: {
    id: 'inbox.messagesTable.loadFailed',
    defaultMessage: 'Could not load messages',
    description: 'Title of the error shown above the Inbox table when the messages could not be loaded.',
  },
  loadMessagesAction: {
    id: 'inbox.messagesTable.loadMessagesAction',
    defaultMessage: 'load your messages',
    description:
      'Action phrase completing an API error sentence ("Failed to …: <error>" / "You do not have permission to …") when the Inbox messages cannot be loaded.',
  },
  retry: {
    id: 'inbox.messagesTable.retry',
    defaultMessage: 'Retry',
    description: 'Button of the load error above the Inbox table that loads the messages again.',
  },
  stale: {
    id: 'inbox.messagesTable.stale',
    defaultMessage: 'New activity since this list was loaded.',
    description: 'Notice above the Inbox table when messages changed while rows were selected, so the list was not reloaded.',
  },
  refresh: {
    id: 'inbox.messagesTable.refresh',
    defaultMessage: 'Refresh',
    description: 'Button of the notice above the Inbox table that reloads the list.',
  },
  loading: {
    id: 'inbox.messagesTable.loading',
    defaultMessage: 'Loading messages',
    description: 'Shown in the Inbox table while the messages load.',
  },
  selectionGroup: {
    id: 'inbox.messagesTable.selection.group',
    defaultMessage: 'Message selection',
    description: 'Screen-reader name of the selection checkboxes of the Inbox table.',
  },
  selectAll: {
    id: 'inbox.messagesTable.selection.all',
    defaultMessage: 'Select all messages',
    description: 'Screen-reader label of the checkbox in the header of the Inbox table that selects every message on the page.',
  },
  selectItem: {
    id: 'inbox.messagesTable.selection.item',
    defaultMessage: 'Select {title}',
    description: 'Screen-reader label of the checkbox of one Inbox table row. {title} is the message title, as the platform writes it.',
  },
  heading: {
    id: 'inbox.messagesTable.heading',
    defaultMessage: 'Messages',
    description: 'Heading of the Inbox table, followed by the message count.',
  },
  markSelectedRead: {
    id: 'inbox.messagesTable.markSelectedRead',
    defaultMessage: 'Mark as read',
    description: 'Button in the header of the Inbox table that marks the selected messages as read.',
  },
  markSelectedReadHint: {
    id: 'inbox.messagesTable.markSelectedReadHint',
    defaultMessage: 'Mark the selected messages as read',
    description: 'Tooltip of the Mark as read button in the header of the Inbox table, while messages are selected.',
  },
  markSelectedUnread: {
    id: 'inbox.messagesTable.markSelectedUnread',
    defaultMessage: 'Mark as unread',
    description: 'Button in the header of the Inbox table that marks the selected messages as unread.',
  },
  markSelectedUnreadHint: {
    id: 'inbox.messagesTable.markSelectedUnreadHint',
    defaultMessage: 'Mark the selected messages as unread',
    description: 'Tooltip of the Mark as unread button in the header of the Inbox table, while messages are selected.',
  },
  deleteSelected: {
    id: 'inbox.messagesTable.deleteSelected',
    defaultMessage: 'Delete',
    description: 'Button in the header of the Inbox table that deletes the selected messages, after a confirmation.',
  },
  deleteSelectedHint: {
    id: 'inbox.messagesTable.deleteSelectedHint',
    defaultMessage: 'Delete the selected messages',
    description: 'Tooltip of the Delete button in the header of the Inbox table, while messages are selected.',
  },
  moreMarkAllRead: {
    id: 'inbox.messagesTable.more.markAllRead',
    defaultMessage: 'Mark all as read',
    description: "Item of the Inbox table's More actions menu that marks every message as read.",
  },
  moreNoUnread: {
    id: 'inbox.messagesTable.more.noUnread',
    defaultMessage: 'No unread messages',
    description: 'Why Mark all as read is disabled in the More actions menu of the Inbox table: nothing is unread.',
  },
  moreClearRead: {
    id: 'inbox.messagesTable.more.clearRead',
    defaultMessage: 'Clear read',
    description: "Item of the Inbox table's More actions menu that deletes every read message, after a confirmation.",
  },
  moreNoRead: {
    id: 'inbox.messagesTable.more.noRead',
    defaultMessage: 'No read messages',
    description: 'Why Clear read is disabled in the More actions menu of the Inbox table: no message is read.',
  },
  moreActions: {
    id: 'inbox.messagesTable.more.button',
    defaultMessage: 'More actions',
    description: 'Menu button in the header of the Inbox table that holds the actions on the whole list.',
  },
  refreshMessages: {
    id: 'inbox.messagesTable.more.refresh',
    defaultMessage: 'Refresh messages',
    description: 'Screen-reader label of the refresh (circular arrow) button in the header of the Inbox table.',
  },
  find: {
    id: 'inbox.messagesTable.find',
    defaultMessage: 'Find messages',
    description: "Placeholder and screen-reader label of the Inbox table's text filter.",
  },
  readStateLabel: {
    id: 'inbox.messagesTable.readState.label',
    defaultMessage: 'Read state',
    description: 'Screen-reader label of the Inbox table filter that shows all messages or only the unread ones.',
  },
  readStateAll: {
    id: 'inbox.messagesTable.readState.all',
    defaultMessage: 'All',
    description: 'Option of the read-state filter of the Inbox table: every message.',
  },
  readStateUnread: {
    id: 'inbox.messagesTable.readState.unread',
    defaultMessage: 'Unread',
    description: 'Option of the read-state filter of the Inbox table: only the unread messages.',
  },
  filterByType: {
    id: 'inbox.messagesTable.filterByType',
    defaultMessage: 'Filter by type',
    description: 'Screen-reader label of the message type filter of the Inbox table.',
  },
  filterBySeverity: {
    id: 'inbox.messagesTable.filterBySeverity',
    defaultMessage: 'Filter by severity',
    description: 'Screen-reader label of the severity filter of the Inbox table.',
  },
  preferencesTitle: {
    id: 'inbox.messagesTable.preferences.title',
    defaultMessage: 'Preferences',
    description: 'Title of the Inbox table preferences dialog (page size), opened from the gear button by the pagination.',
  },
  preferencesConfirm: {
    id: 'inbox.messagesTable.preferences.confirm',
    defaultMessage: 'Confirm',
    description: 'Button that applies the Inbox table preferences.',
  },
  preferencesCancel: {
    id: 'inbox.messagesTable.preferences.cancel',
    defaultMessage: 'Cancel',
    description: 'Button that closes the Inbox table preferences without applying them.',
  },
  preferencesPageSize: {
    id: 'inbox.messagesTable.preferences.pageSize',
    defaultMessage: 'Page size',
    description: 'Heading of the page size choices in the Inbox table preferences.',
  },
  preferencesPageSizeOption: {
    id: 'inbox.messagesTable.preferences.pageSizeOption',
    defaultMessage: '{count, plural, one {# message} other {# messages}}',
    description: 'One page size choice in the Inbox table preferences. {count} is the number of messages per page (10, 20 or 50).',
  },
  clearReadHeader: {
    id: 'inbox.messagesTable.clearRead.header',
    defaultMessage: 'Clear read messages',
    description: 'Title of the dialog that confirms deleting every read Inbox message.',
  },
  clearReadCancel: {
    id: 'inbox.messagesTable.clearRead.cancel',
    defaultMessage: 'Cancel',
    description: 'Button that closes the Clear read messages dialog without deleting anything.',
  },
  clearReadConfirm: {
    id: 'inbox.messagesTable.clearRead.confirm',
    defaultMessage: 'Clear read',
    description: 'Button that confirms deleting every read Inbox message.',
  },
  clearReadBody: {
    id: 'inbox.messagesTable.clearRead.body',
    defaultMessage: 'Delete every message you have already read? This action cannot be undone.',
    description: 'Question of the dialog that confirms deleting every read Inbox message.',
  },
});

/** The empty state's English, written once in `messages`. */
export const EMPTY_TITLE = String(messages.emptyTitle.defaultMessage);
export const EMPTY_DESCRIPTION = String(messages.emptyBody.defaultMessage);

type ReadFilter = 'all' | 'unread';

interface TablePreferences {
  pageSize: number;
}

/**
 * Starting column widths (px), chosen so the table and its selection column fit a
 * standard page (about 850 px) without scrolling: Received 143, Title a fixed 340
 * (not its content's width), Type 120, Severity 110, and Action at least 80, filling
 * the rest.
 */
export const INBOX_COLUMN_WIDTHS = { createdAt: 143, title: 340, type: 120, severity: 110, actions: 80 } as const;
const DEFAULT_PREFERENCES: TablePreferences = { pageSize: 20 };

const markRead = (m: InboxMessage, read: boolean, readAt: string | null): InboxMessage => ({ ...m, read, readAt: read ? readAt : null });

const InboxMessagesTable = (): React.JSX.Element => {
  // 1. Hooks
  const intl = useIntl();
  const paginationLabels = usePaginationLabels();
  const { unreadCount, changeToken, beginUnreadSync, markMessages, markAllRead, deleteMessages, clearRead } = useInbox();
  const { locale } = useFormatters();
  const [items, setItems] = useState<InboxMessage[]>([]);
  const [loading, setLoading] = useState(true);
  // What the last read threw, worded at render: loadAll never reads `intl`.
  const [loadError, setLoadError] = useState<{ readonly cause: unknown } | null>(null);
  const [selectedItems, setSelectedItems] = useState<InboxMessage[]>([]);
  const [stale, setStale] = useState(false);
  const [types, setTypes] = useState<readonly InboxMessageType[]>([]);
  const [readFilter, setReadFilter] = useState<ReadFilter>('all');
  // The chosen type and severity, '' for all; each Select's option is built from it at render.
  const [typeFilter, setTypeFilter] = useState('');
  const [severityFilter, setSeverityFilter] = useState('');
  const [openMessage, setOpenMessage] = useState<InboxMessage | null>(null);
  // The messages the delete modal is confirming: the selection, or one row's menu.
  const [deleteTargets, setDeleteTargets] = useState<readonly InboxMessage[] | null>(null);
  const [clearVisible, setClearVisible] = useState(false);
  const [busy, setBusy] = useState(false);
  const [preferences, setPreferences] = useLocalStorage<TablePreferences>('inbox-messages-preferences', DEFAULT_PREFERENCES);
  const headingRef = useRef<HTMLSpanElement>(null);
  const selectedRef = useRef<InboxMessage[]>([]);
  selectedRef.current = selectedItems;
  // Set by this table's own actions, which update their rows themselves, so the
  // provider's change signal for that action needs no reload.
  const ownActionRef = useRef(false);

  const loadAll = async (): Promise<void> => {
    setLoading(true);
    // A mark, delete or clear that starts while this read is on the wire makes it
    // stale: its rows and count predate the action. Read again instead of applying it.
    const sync = beginUnreadSync();
    try {
      const all: InboxMessage[] = [];
      let nextToken: string | null | undefined = null;
      let count: number | null = null;
      for (let pageIndex = 0; pageIndex < MAX_PAGES; pageIndex += 1) {
        const result: { data: ListInboxMessagesQuery } = await client.graphql({
          query: listInboxMessagesQuery,
          variables: { limit: PAGE_LIMIT, nextToken },
        });
        const page: ListInboxMessagesQuery['listInboxMessages'] = result.data?.listInboxMessages;
        all.push(...(page?.items ?? []));
        if (count == null && page) count = page.unreadCount;
        nextToken = page?.nextToken;
        if (!nextToken) break;
      }
      if (!sync(count)) {
        await loadAll();
        return;
      }
      setItems(all);
      setLoadError(null);
      setStale(false);
    } catch (err) {
      logger.error('listInboxMessages failed', err);
      setLoadError({ cause: err });
    } finally {
      setLoading(false);
    }
  };

  const loadTypes = async (): Promise<void> => {
    try {
      const result = await client.graphql({ query: getInboxPreferencesQuery });
      setTypes(result.data?.getInboxPreferences?.types ?? []);
    } catch (err) {
      // Labels fall back to the raw type id; the preferences tab reports the error.
      logger.warn('getInboxPreferences failed; type filter shows raw ids', err);
    }
  };

  // 2. Effects
  useEffect(() => {
    void loadTypes();
  }, []);

  useEffect(() => {
    if (ownActionRef.current) {
      ownActionRef.current = false;
      return;
    }
    if (selectedRef.current.length > 0) {
      setStale(true);
      return;
    }
    void loadAll();
  }, [changeToken]);

  // 3. Memos
  const typeNames = useMemo(() => new Map(types.map((t) => [t.id, t.displayName])), [types]);
  const typeLabel = (id: string): string => typeNames.get(id) ?? id;

  const allTypesOption: SelectProps.Option = { value: '', label: intl.formatMessage(messages.allTypes) };
  const typeOptions = useMemo<SelectProps.Option[]>(() => {
    const ids = new Set<string>(types.map((t) => t.id));
    items.forEach((m) => ids.add(m.type));
    return [...ids].map((id) => ({ value: id, label: typeNames.get(id) ?? id }));
  }, [types, items, typeNames]);
  const typeOption: SelectProps.Option = typeFilter ? { value: typeFilter, label: typeLabel(typeFilter) } : allTypesOption;

  const severityOptions: SelectProps.Option[] = [
    { value: '', label: intl.formatMessage(messages.allSeverities) },
    ...INBOX_SEVERITIES.map((s) => ({ value: s, label: severityLabel(s, intl) })),
  ];
  const severityOption = severityOptions.find((o) => o.value === severityFilter) ?? severityOptions[0];

  const filtered = useMemo(
    () =>
      items.filter(
        (m) =>
          (readFilter === 'all' || !m.read) && (!typeFilter || m.type === typeFilter) && (!severityFilter || m.severity === severityFilter),
      ),
    [items, readFilter, typeFilter, severityFilter],
  );

  const clearFilters = () => {
    setReadFilter('all');
    setTypeFilter('');
    setSeverityFilter('');
    actions.setFiltering('');
  };

  const {
    items: pageItems,
    actions,
    filteredItemsCount,
    collectionProps,
    filterProps,
    paginationProps,
  } = useCollection(filtered, {
    filtering: {
      empty: <TableEmptyState title={intl.formatMessage(messages.emptyTitle)} description={intl.formatMessage(messages.emptyBody)} />,
      noMatch: <TableNoMatchState onClearFilter={clearFilters} />,
    },
    pagination: { pageSize: preferences.pageSize },
    sorting: { defaultState: { sortingColumn: { sortingField: 'createdAt' }, isDescending: true } },
    selection: { keepSelection: false, trackBy: 'id' },
  });

  const hasRead = items.some((m) => m.read);

  // 4. Handlers
  const focusHeading = () => headingRef.current?.focus();

  const runAction = async (action: () => Promise<void>): Promise<void> => {
    // A successful action always bumps the provider's changeToken once; the effect
    // consumes this flag for that bump instead of reloading rows updated here.
    ownActionRef.current = true;
    setBusy(true);
    try {
      await action();
    } catch {
      // No bump follows a failure. The provider has already flashed the error;
      // reload so the rows match the server.
      ownActionRef.current = false;
      void loadAll();
    } finally {
      setBusy(false);
      setSelectedItems([]);
      focusHeading();
    }
  };

  const onMark = (targets: readonly InboxMessage[], read: boolean) =>
    runAction(async () => {
      const ids = new Set(targets.map((m) => m.id));
      await markMessages([...targets], read);
      const now = new Date().toISOString();
      setItems((prev) => prev.map((m) => (ids.has(m.id) ? markRead(m, read, now) : m)));
    });

  const onMarkAllRead = () =>
    runAction(async () => {
      await markAllRead();
      const now = new Date().toISOString();
      setItems((prev) => prev.map((m) => (m.read ? m : markRead(m, true, now))));
    });

  const onConfirmDelete = () =>
    runAction(async () => {
      const targets = deleteTargets ?? [];
      const ids = new Set(targets.map((m) => m.id));
      try {
        await deleteMessages([...targets]);
        setItems((prev) => prev.filter((m) => !ids.has(m.id)));
      } finally {
        setDeleteTargets(null);
      }
    });

  const onConfirmClearRead = () =>
    runAction(async () => {
      try {
        await clearRead();
        setItems((prev) => prev.filter((m) => !m.read));
      } finally {
        setClearVisible(false);
      }
    });

  const onMessageRead = (message: InboxMessage) => {
    const now = new Date().toISOString();
    setItems((prev) => prev.map((m) => (m.id === message.id ? markRead(m, true, now) : m)));
  };

  const closeMessage = () => {
    setOpenMessage(null);
    focusHeading();
  };

  const onRowAction = (m: InboxMessage, id: string) => {
    if (id === 'open') setOpenMessage(m);
    else if (id === 'read') void onMark([m], true);
    else if (id === 'unread') void onMark([m], false);
    else if (id === 'delete') setDeleteTargets([m]);
  };

  const onHeaderAction = (id: string) => {
    if (id === 'mark-all-read') void onMarkAllRead();
    else if (id === 'clear-read') setClearVisible(true);
  };

  const onRefresh = () => {
    setSelectedItems([]);
    void loadAll();
  };

  // 5. Render
  const noSelectionReason = intl.formatMessage(messages.noSelection);
  // Received | Title | Type | Severity | Action, sized to fit the page without
  // horizontal scrolling or resizing. Every column but the last has a width: a
  // resizable table reads a column with none from its content, so a long title would
  // widen the table. The title truncates (full title on hover), and Action, the last
  // column, takes whatever width is left. Plain text in every cell (no status icons
  // or badges); an unread message's title is bold, a read title is normal weight.
  const columnDefinitions: TableProps.ColumnDefinition<InboxMessage>[] = [
    {
      id: 'createdAt',
      header: intl.formatMessage(messages.columnReceived),
      width: INBOX_COLUMN_WIDTHS.createdAt,
      cell: (m) => (
        <span title={formatDateTime(m.createdAt, locale)}>
          {formatRelativeTime(m.createdAt, locale, undefined, intl.formatMessage(relativeTimeMessages.justNow))}
        </span>
      ),
      sortingField: 'createdAt',
    },
    {
      id: 'title',
      header: intl.formatMessage(messages.columnTitle),
      width: INBOX_COLUMN_WIDTHS.title,
      minWidth: 160,
      cell: (m) => (
        <Button variant="inline-link" onClick={() => setOpenMessage(m)}>
          <TruncatedText bold={!m.read}>{m.title}</TruncatedText>
        </Button>
      ),
      sortingField: 'title',
    },
    {
      id: 'type',
      header: intl.formatMessage(messages.columnType),
      width: INBOX_COLUMN_WIDTHS.type,
      cell: (m) => <span title={typeLabel(m.type)}>{messageCategory(m.type, typeLabel(m.type), intl)}</span>,
      sortingField: 'type',
      sortingComparator: (a, b) =>
        messageCategory(a.type, typeLabel(a.type), intl).localeCompare(messageCategory(b.type, typeLabel(b.type), intl)),
    },
    {
      id: 'severity',
      header: intl.formatMessage(messages.columnSeverity),
      width: INBOX_COLUMN_WIDTHS.severity,
      cell: (m) => severityLabel(m.severity, intl),
      sortingField: 'severity',
      sortingComparator: (a, b) => severityRank(a.severity) - severityRank(b.severity),
    },
    {
      id: 'actions',
      header: intl.formatMessage(messages.columnAction),
      minWidth: INBOX_COLUMN_WIDTHS.actions,
      cell: (m) => (
        <ButtonDropdown
          variant="inline-icon"
          ariaLabel={intl.formatMessage(messages.rowActions, { title: m.title })}
          expandToViewport
          disabled={busy}
          onItemClick={({ detail }) => onRowAction(m, detail.id)}
          items={[
            { id: 'open', text: intl.formatMessage(messages.rowOpen) },
            m.read
              ? { id: 'unread', text: intl.formatMessage(messages.rowMarkUnread) }
              : { id: 'read', text: intl.formatMessage(messages.rowMarkRead) },
            { id: 'delete', text: intl.formatMessage(messages.rowDelete) },
          ]}
        />
      ),
    },
  ];

  const nothingSelected = selectedItems.length === 0;

  return (
    <SpaceBetween size="s">
      {loadError && (
        <Alert
          type="error"
          header={intl.formatMessage(messages.loadFailed)}
          action={
            <Button onClick={() => void loadAll()} loading={loading}>
              <FormattedMessage {...messages.retry} />
            </Button>
          }
        >
          {describeApiError(loadError.cause, messages.loadMessagesAction, intl)}
        </Alert>
      )}
      {stale && (
        <Alert
          type="info"
          action={
            <Button onClick={onRefresh}>
              <FormattedMessage {...messages.refresh} />
            </Button>
          }
        >
          <FormattedMessage {...messages.stale} />
        </Alert>
      )}
      <Table
        {...collectionProps}
        selectedItems={selectedItems}
        onSelectionChange={({ detail }) => setSelectedItems(detail.selectedItems)}
        selectionType="multi"
        trackBy="id"
        columnDefinitions={columnDefinitions}
        items={pageItems}
        loading={loading && items.length === 0}
        loadingText={intl.formatMessage(messages.loading)}
        resizableColumns
        ariaLabels={{
          selectionGroupLabel: intl.formatMessage(messages.selectionGroup),
          allItemsSelectionLabel: () => intl.formatMessage(messages.selectAll),
          itemSelectionLabel: (_s, m) => intl.formatMessage(messages.selectItem, { title: m.title }),
        }}
        header={
          <TableHeader
            title={
              <span ref={headingRef} tabIndex={-1}>
                <FormattedMessage {...messages.heading} />
              </span>
            }
            totalItems={filtered}
            selectedItems={selectedItems}
            actionButtons={
              <SpaceBetween direction="horizontal" size="xs">
                <span title={nothingSelected ? noSelectionReason : intl.formatMessage(messages.markSelectedReadHint)}>
                  <Button disabled={nothingSelected || busy} onClick={() => void onMark(selectedItems, true)}>
                    <FormattedMessage {...messages.markSelectedRead} />
                  </Button>
                </span>
                <span title={nothingSelected ? noSelectionReason : intl.formatMessage(messages.markSelectedUnreadHint)}>
                  <Button disabled={nothingSelected || busy} onClick={() => void onMark(selectedItems, false)}>
                    <FormattedMessage {...messages.markSelectedUnread} />
                  </Button>
                </span>
                <span title={nothingSelected ? noSelectionReason : intl.formatMessage(messages.deleteSelectedHint)}>
                  <Button disabled={nothingSelected || busy} onClick={() => setDeleteTargets(selectedItems)}>
                    <FormattedMessage {...messages.deleteSelected} />
                  </Button>
                </span>
                <ButtonDropdown
                  disabled={busy}
                  onItemClick={({ detail }) => onHeaderAction(detail.id)}
                  items={[
                    {
                      id: 'mark-all-read',
                      text: intl.formatMessage(messages.moreMarkAllRead),
                      disabled: unreadCount === 0,
                      disabledReason: intl.formatMessage(messages.moreNoUnread),
                    },
                    {
                      id: 'clear-read',
                      text: intl.formatMessage(messages.moreClearRead),
                      disabled: !hasRead,
                      disabledReason: intl.formatMessage(messages.moreNoRead),
                    },
                  ]}
                >
                  <FormattedMessage {...messages.moreActions} />
                </ButtonDropdown>
                <Button iconName="refresh" ariaLabel={intl.formatMessage(messages.refreshMessages)} onClick={onRefresh} loading={loading} />
              </SpaceBetween>
            }
          />
        }
        filter={
          <SpaceBetween direction="horizontal" size="s" alignItems="center">
            <TextFilter
              {...filterProps}
              filteringPlaceholder={intl.formatMessage(messages.find)}
              filteringAriaLabel={intl.formatMessage(messages.find)}
              countText={filterCounterText(intl, filteredItemsCount ?? 0)}
            />
            <SegmentedControl
              label={intl.formatMessage(messages.readStateLabel)}
              selectedId={readFilter}
              onChange={({ detail }) => setReadFilter(detail.selectedId as ReadFilter)}
              options={[
                { id: 'all', text: intl.formatMessage(messages.readStateAll) },
                { id: 'unread', text: intl.formatMessage(messages.readStateUnread) },
              ]}
            />
            <Select
              selectedOption={typeOption}
              onChange={({ detail }) => setTypeFilter(detail.selectedOption.value ?? '')}
              options={[allTypesOption, ...typeOptions]}
              ariaLabel={intl.formatMessage(messages.filterByType)}
            />
            <Select
              selectedOption={severityOption}
              onChange={({ detail }) => setSeverityFilter(detail.selectedOption.value ?? '')}
              options={severityOptions}
              ariaLabel={intl.formatMessage(messages.filterBySeverity)}
            />
          </SpaceBetween>
        }
        pagination={<Pagination {...paginationProps} ariaLabels={paginationLabels} />}
        preferences={
          <CollectionPreferences
            title={intl.formatMessage(messages.preferencesTitle)}
            confirmLabel={intl.formatMessage(messages.preferencesConfirm)}
            cancelLabel={intl.formatMessage(messages.preferencesCancel)}
            preferences={preferences}
            onConfirm={({ detail }) => setPreferences({ pageSize: detail.pageSize ?? DEFAULT_PREFERENCES.pageSize })}
            pageSizePreference={{
              title: intl.formatMessage(messages.preferencesPageSize),
              options: [10, 20, 50].map((value) => ({
                value,
                label: intl.formatMessage(messages.preferencesPageSizeOption, { count: value }),
              })),
            }}
          />
        }
      />
      <InboxMessageModal message={openMessage} onDismiss={closeMessage} onMarkedRead={onMessageRead} />
      <DeleteInboxMessagesModal
        visible={deleteTargets !== null}
        messages={deleteTargets ?? []}
        isLoading={busy}
        onDismiss={() => {
          setDeleteTargets(null);
          focusHeading();
        }}
        onConfirm={() => void onConfirmDelete()}
      />
      <Modal
        visible={clearVisible}
        onDismiss={
          busy
            ? undefined
            : () => {
                setClearVisible(false);
                focusHeading();
              }
        }
        header={intl.formatMessage(messages.clearReadHeader)}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" disabled={busy} onClick={() => setClearVisible(false)}>
                <FormattedMessage {...messages.clearReadCancel} />
              </Button>
              <Button variant="primary" loading={busy} onClick={() => void onConfirmClearRead()}>
                <FormattedMessage {...messages.clearReadConfirm} />
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <FormattedMessage {...messages.clearReadBody} />
      </Modal>
    </SpaceBetween>
  );
};

export default InboxMessagesTable;
