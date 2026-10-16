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

import { generateClient } from '../../api/client-shim';
import { getInboxPreferences as getInboxPreferencesQuery, listInboxMessages as listInboxMessagesQuery } from '../../graphql/generated';
import type { InboxMessage, InboxMessageType, ListInboxMessagesQuery } from '../../graphql/generated/operation-types';
import useInbox from '../../hooks/use-inbox';
import { describeApiError } from '../../hooks/utils/graphql-error';
import { paginationLabels } from '../common/labels';
import useLocalStorage from '../common/local-storage';
import { NoDataState, TableHeader, TableNoMatchState, getFilterCounterText } from '../common/table';
import TruncatedText from '../common/TruncatedText';
import { formatDateTime, formatRelativeTime } from '../../utils/date-time';
import DeleteInboxMessagesModal from './DeleteInboxMessagesModal';
import InboxMessageModal from './InboxMessageModal';
import { messageCategory } from './message-category';
import { severityLabel, severityRank } from './severity';

const client = generateClient();
const logger = new ConsoleLogger('InboxMessagesTable');

const PAGE_LIMIT = 100;
const MAX_PAGES = 10;

export const EMPTY_TITLE = 'No messages';
export const EMPTY_DESCRIPTION = "You'll see messages here when work is assigned to you or something needs your attention.";

type ReadFilter = 'all' | 'unread';

const ALL_TYPES: SelectProps.Option = { value: '', label: 'All types' };
const SEVERITY_OPTIONS: SelectProps.Option[] = [
  { value: '', label: 'All severities' },
  { value: 'info', label: 'Info' },
  { value: 'success', label: 'Success' },
  { value: 'warning', label: 'Warning' },
  { value: 'error', label: 'Error' },
];

interface TablePreferences {
  pageSize: number;
}
const DEFAULT_PREFERENCES: TablePreferences = { pageSize: 20 };

const markRead = (m: InboxMessage, read: boolean, readAt: string | null): InboxMessage => ({ ...m, read, readAt: read ? readAt : null });

const InboxMessagesTable = (): React.JSX.Element => {
  // 1. Hooks
  const { unreadCount, changeToken, syncUnreadCount, markMessages, markAllRead, deleteMessages, clearRead } = useInbox();
  const [items, setItems] = useState<InboxMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selectedItems, setSelectedItems] = useState<InboxMessage[]>([]);
  const [stale, setStale] = useState(false);
  const [types, setTypes] = useState<readonly InboxMessageType[]>([]);
  const [readFilter, setReadFilter] = useState<ReadFilter>('all');
  const [typeFilter, setTypeFilter] = useState<SelectProps.Option>(ALL_TYPES);
  const [severityFilter, setSeverityFilter] = useState<SelectProps.Option>(SEVERITY_OPTIONS[0]);
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
      setItems(all);
      setLoadError(null);
      setStale(false);
      if (count != null) syncUnreadCount(count);
    } catch (err) {
      logger.error('listInboxMessages failed', err);
      setLoadError(describeApiError(err, 'load your messages'));
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

  const typeOptions = useMemo<SelectProps.Option[]>(() => {
    const ids = new Set<string>(types.map((t) => t.id));
    items.forEach((m) => ids.add(m.type));
    return [ALL_TYPES, ...[...ids].map((id) => ({ value: id, label: typeNames.get(id) ?? id }))];
  }, [types, items, typeNames]);

  const filtered = useMemo(
    () =>
      items.filter(
        (m) =>
          (readFilter === 'all' || !m.read) &&
          (!typeFilter.value || m.type === typeFilter.value) &&
          (!severityFilter.value || m.severity === severityFilter.value),
      ),
    [items, readFilter, typeFilter, severityFilter],
  );

  const clearFilters = () => {
    setReadFilter('all');
    setTypeFilter(ALL_TYPES);
    setSeverityFilter(SEVERITY_OPTIONS[0]);
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
      empty: <NoDataState />,
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
  const noSelectionReason = 'Select one or more messages';
  // Received | Title | Type | Severity | Action, sized to fit without horizontal
  // scrolling: the title takes the remaining width and truncates. Plain text in every
  // cell (no status icons); an unread message's title is bold.
  const columnDefinitions: TableProps.ColumnDefinition<InboxMessage>[] = [
    {
      id: 'createdAt',
      header: 'Received',
      width: 130,
      cell: (m) => <span title={formatDateTime(m.createdAt)}>{formatRelativeTime(m.createdAt)}</span>,
      sortingField: 'createdAt',
    },
    {
      id: 'title',
      header: 'Title',
      minWidth: 260,
      cell: (m) => (
        <Button variant="inline-link" onClick={() => setOpenMessage(m)}>
          <TruncatedText bold={!m.read}>{m.title}</TruncatedText>
        </Button>
      ),
      sortingField: 'title',
    },
    {
      id: 'type',
      header: 'Type',
      width: 110,
      cell: (m) => <span title={typeLabel(m.type)}>{messageCategory(m.type, typeLabel(m.type))}</span>,
      sortingField: 'type',
      sortingComparator: (a, b) => messageCategory(a.type, typeLabel(a.type)).localeCompare(messageCategory(b.type, typeLabel(b.type))),
    },
    {
      id: 'severity',
      header: 'Severity',
      width: 110,
      cell: (m) => severityLabel(m.severity),
      sortingField: 'severity',
      sortingComparator: (a, b) => severityRank(a.severity) - severityRank(b.severity),
    },
    {
      id: 'actions',
      header: 'Action',
      width: 80,
      cell: (m) => (
        <ButtonDropdown
          variant="inline-icon"
          ariaLabel={`Actions for ${m.title}`}
          expandToViewport
          disabled={busy}
          onItemClick={({ detail }) => onRowAction(m, detail.id)}
          items={[
            { id: 'open', text: 'Open' },
            m.read ? { id: 'unread', text: 'Mark as unread' } : { id: 'read', text: 'Mark as read' },
            { id: 'delete', text: 'Delete' },
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
          header="Could not load messages"
          action={
            <Button onClick={() => void loadAll()} loading={loading}>
              Retry
            </Button>
          }
        >
          {loadError}
        </Alert>
      )}
      {stale && (
        <Alert type="info" action={<Button onClick={onRefresh}>Refresh</Button>}>
          New activity since this list was loaded.
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
        loadingText="Loading messages"
        resizableColumns
        ariaLabels={{
          selectionGroupLabel: 'Message selection',
          allItemsSelectionLabel: () => 'Select all messages',
          itemSelectionLabel: (_s, m) => `Select ${m.title}`,
        }}
        header={
          <TableHeader
            title={
              <span ref={headingRef} tabIndex={-1}>
                Messages
              </span>
            }
            totalItems={filtered}
            selectedItems={selectedItems}
            actionButtons={
              <SpaceBetween direction="horizontal" size="xs">
                <span title={nothingSelected ? noSelectionReason : 'Mark the selected messages as read'}>
                  <Button disabled={nothingSelected || busy} onClick={() => void onMark(selectedItems, true)}>
                    Mark as read
                  </Button>
                </span>
                <span title={nothingSelected ? noSelectionReason : 'Mark the selected messages as unread'}>
                  <Button disabled={nothingSelected || busy} onClick={() => void onMark(selectedItems, false)}>
                    Mark as unread
                  </Button>
                </span>
                <span title={nothingSelected ? noSelectionReason : 'Delete the selected messages'}>
                  <Button disabled={nothingSelected || busy} onClick={() => setDeleteTargets(selectedItems)}>
                    Delete
                  </Button>
                </span>
                <ButtonDropdown
                  disabled={busy}
                  onItemClick={({ detail }) => onHeaderAction(detail.id)}
                  items={[
                    {
                      id: 'mark-all-read',
                      text: 'Mark all as read',
                      disabled: unreadCount === 0,
                      disabledReason: 'No unread messages',
                    },
                    { id: 'clear-read', text: 'Clear read', disabled: !hasRead, disabledReason: 'No read messages' },
                  ]}
                >
                  More actions
                </ButtonDropdown>
                <Button iconName="refresh" ariaLabel="Refresh messages" onClick={onRefresh} loading={loading} />
              </SpaceBetween>
            }
          />
        }
        filter={
          <SpaceBetween direction="horizontal" size="s" alignItems="center">
            <TextFilter
              {...filterProps}
              filteringPlaceholder="Find messages"
              filteringAriaLabel="Find messages"
              countText={getFilterCounterText(filteredItemsCount ?? 0)}
            />
            <SegmentedControl
              label="Read state"
              selectedId={readFilter}
              onChange={({ detail }) => setReadFilter(detail.selectedId as ReadFilter)}
              options={[
                { id: 'all', text: 'All' },
                { id: 'unread', text: 'Unread' },
              ]}
            />
            <Select
              selectedOption={typeFilter}
              onChange={({ detail }) => setTypeFilter(detail.selectedOption)}
              options={typeOptions}
              ariaLabel="Filter by type"
            />
            <Select
              selectedOption={severityFilter}
              onChange={({ detail }) => setSeverityFilter(detail.selectedOption)}
              options={SEVERITY_OPTIONS}
              ariaLabel="Filter by severity"
            />
          </SpaceBetween>
        }
        pagination={<Pagination {...paginationProps} ariaLabels={paginationLabels} />}
        preferences={
          <CollectionPreferences
            title="Preferences"
            confirmLabel="Confirm"
            cancelLabel="Cancel"
            preferences={preferences}
            onConfirm={({ detail }) => setPreferences({ pageSize: detail.pageSize ?? DEFAULT_PREFERENCES.pageSize })}
            pageSizePreference={{
              title: 'Page size',
              options: [
                { value: 10, label: '10 messages' },
                { value: 20, label: '20 messages' },
                { value: 50, label: '50 messages' },
              ],
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
        header="Clear read messages"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" disabled={busy} onClick={() => setClearVisible(false)}>
                Cancel
              </Button>
              <Button variant="primary" loading={busy} onClick={() => void onConfirmClearRead()}>
                Clear read
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        Delete every message you have already read? This action cannot be undone.
      </Modal>
    </SpaceBetween>
  );
};

export default InboxMessagesTable;
