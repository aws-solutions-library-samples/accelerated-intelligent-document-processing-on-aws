// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The one Inbox poll for the whole app. Mounted once in the authenticated shell
// (AuthRoutes), so the bell, the navigation badge and the Inbox page share a single
// 60-second `listInboxMessages` request instead of each polling on its own.
//
// Arriving messages never raise a flash message: the bell badge and the navigation
// badge are the only signal. Flash messages are reserved for the user's own actions.
import React, { createContext, useCallback, useMemo, useRef, useState } from 'react';
import { ConsoleLogger } from 'aws-amplify/utils';

import { generateClient } from '../api/client-shim';
import {
  clearReadInboxMessages as clearReadInboxMessagesMutation,
  deleteInboxMessages as deleteInboxMessagesMutation,
  listInboxMessages as listInboxMessagesQuery,
  markAllInboxMessagesRead as markAllInboxMessagesReadMutation,
  markInboxMessages as markInboxMessagesMutation,
} from '../graphql/generated';
import type { InboxMessage, InboxMutationResult } from '../graphql/generated/operation-types';
import useAppContext from './app';
import usePolling from '../hooks/use-polling';
import { describeApiError } from '../hooks/utils/graphql-error';

const client = generateClient();
const logger = new ConsoleLogger('InboxProvider');

export const INBOX_POLL_INTERVAL_MS = 60_000;
export const BELL_RECENT_LIMIT = 5;
export type InboxSeverity = 'info' | 'success' | 'warning' | 'error';
export interface InboxMutationOptions {
  silent?: boolean;
}
export interface InboxContextValue {
  /** null until the first successful load. */
  unreadCount: number | null;
  /** Newest BELL_RECENT_LIMIT messages, read and unread. */
  recent: readonly InboxMessage[];
  loaded: boolean;
  pollError: unknown;
  /** Bumped when the newest id or unreadCount changes between polls, and after own mutations. */
  changeToken: number;
  /** Waits for an in-flight poll, then polls again. */
  refresh: () => Promise<void>;
  syncUnreadCount: (n: number) => void;
  markMessages: (msgs: readonly InboxMessage[], read: boolean, o?: InboxMutationOptions) => Promise<InboxMutationResult>;
  markAllRead: (o?: InboxMutationOptions) => Promise<InboxMutationResult>;
  deleteMessages: (msgs: readonly InboxMessage[]) => Promise<InboxMutationResult>;
  clearRead: () => Promise<InboxMutationResult>;
}

export const InboxContext = createContext<InboxContextValue | null>(null);

interface InboxState {
  unreadCount: number | null;
  recent: readonly InboxMessage[];
  loaded: boolean;
  pollError: unknown;
  changeToken: number;
}

interface Snapshot {
  unreadCount: number | null;
  recent: readonly InboxMessage[];
}

const INITIAL_STATE: InboxState = { unreadCount: null, recent: [], loaded: false, pollError: null, changeToken: 0 };

const plural = (n: number): string => (n === 1 ? '1 message' : `${n} messages`);

// The API returns null only when the resolver failed without an error envelope.
const requireResult = (value: InboxMutationResult | null | undefined, field: string): InboxMutationResult => {
  if (value == null) throw new Error(`${field} returned no result`);
  return value;
};

const unreadAmong = (msgs: readonly InboxMessage[]): number => msgs.filter((m) => !m.read).length;

interface InboxProviderProps {
  children: React.ReactNode;
  /** Only Annotators have an Inbox; for everyone else nothing is fetched. */
  enabled?: boolean;
}

export const InboxProvider = ({ children, enabled = true }: InboxProviderProps): React.JSX.Element => {
  const { setSuccessMessage, setErrorMessage } = useAppContext();
  const [state, setState] = useState<InboxState>(INITIAL_STATE);

  // Latest committed state, read by the poll and by mutation snapshots.
  const stateRef = useRef<InboxState>(INITIAL_STATE);
  stateRef.current = state;
  // The poll currently on the wire; an overlapping tick reuses it.
  const inFlightRef = useRef<Promise<void> | null>(null);
  // Bumped at the start and end of every mutation; a poll whose snapshot is stale is discarded.
  const mutationEpochRef = useRef(0);

  const poll = useCallback((): Promise<void> => {
    if (inFlightRef.current) return inFlightRef.current;
    const epoch = mutationEpochRef.current;
    const request = (async () => {
      try {
        const result = await client.graphql({ query: listInboxMessagesQuery, variables: { limit: BELL_RECENT_LIMIT } });
        if (epoch !== mutationEpochRef.current) return;
        const page = result.data?.listInboxMessages;
        const items: readonly InboxMessage[] = page?.items ?? [];
        const count = page?.unreadCount ?? 0;
        setState((prev) => {
          const changed = prev.loaded && (prev.unreadCount !== count || prev.recent[0]?.id !== items[0]?.id);
          return {
            unreadCount: count,
            recent: items,
            loaded: true,
            pollError: null,
            changeToken: changed ? prev.changeToken + 1 : prev.changeToken,
          };
        });
      } catch (err) {
        logger.error('listInboxMessages poll failed', err);
        if (epoch !== mutationEpochRef.current) return;
        setState((prev) => ({ ...prev, pollError: err }));
      } finally {
        inFlightRef.current = null;
      }
    })();
    inFlightRef.current = request;
    return request;
  }, []);

  usePolling(poll, { enabled, intervalMs: INBOX_POLL_INTERVAL_MS, immediate: true });

  const refresh = useCallback(async (): Promise<void> => {
    if (inFlightRef.current) await inFlightRef.current;
    await poll();
  }, [poll]);

  const syncUnreadCount = useCallback((n: number) => {
    setState((prev) => (prev.unreadCount === n ? prev : { ...prev, unreadCount: n }));
  }, []);

  const runMutation = useCallback(
    async (
      optimistic: (s: Snapshot) => Snapshot,
      call: () => Promise<InboxMutationResult>,
      action: string,
      successText: (r: InboxMutationResult) => string,
      silent: boolean,
    ): Promise<InboxMutationResult> => {
      mutationEpochRef.current += 1;
      const snapshot: Snapshot = { unreadCount: stateRef.current.unreadCount, recent: stateRef.current.recent };
      setState((prev) => ({ ...prev, ...optimistic({ unreadCount: prev.unreadCount, recent: prev.recent }) }));
      try {
        const result = await call();
        mutationEpochRef.current += 1;
        setState((prev) => ({ ...prev, unreadCount: result.unreadCount, changeToken: prev.changeToken + 1 }));
        void refresh();
        if (!silent) setSuccessMessage(successText(result));
        return result;
      } catch (err) {
        mutationEpochRef.current += 1;
        logger.error(`Inbox mutation failed: ${action}`, err);
        setState((prev) => ({ ...prev, unreadCount: snapshot.unreadCount, recent: snapshot.recent }));
        setErrorMessage(describeApiError(err, action));
        throw err;
      }
    },
    [refresh, setErrorMessage, setSuccessMessage],
  );

  const markMessages = useCallback(
    (msgs: readonly InboxMessage[], read: boolean, o?: InboxMutationOptions): Promise<InboxMutationResult> => {
      const ids = msgs.map((m) => m.id);
      const idSet = new Set(ids);
      const delta = msgs.filter((m) => m.read !== read).length;
      const readAt = read ? new Date().toISOString() : null;
      return runMutation(
        (s) => ({
          unreadCount: s.unreadCount == null ? null : Math.max(0, s.unreadCount + (read ? -delta : delta)),
          recent: s.recent.map((m) => (idSet.has(m.id) ? { ...m, read, readAt } : m)),
        }),
        async () => {
          const res = await client.graphql({ query: markInboxMessagesMutation, variables: { ids, read } });
          return requireResult(res.data?.markInboxMessages, 'markInboxMessages');
        },
        read ? 'mark messages as read' : 'mark messages as unread',
        (r) => `Marked ${plural(r.updated)} as ${read ? 'read' : 'unread'}`,
        o?.silent ?? false,
      );
    },
    [runMutation],
  );

  const markAllRead = useCallback(
    (o?: InboxMutationOptions): Promise<InboxMutationResult> => {
      const readAt = new Date().toISOString();
      return runMutation(
        (s) => ({
          unreadCount: s.unreadCount == null ? null : 0,
          recent: s.recent.map((m) => (m.read ? m : { ...m, read: true, readAt })),
        }),
        async () => {
          const res = await client.graphql({ query: markAllInboxMessagesReadMutation });
          return requireResult(res.data?.markAllInboxMessagesRead, 'markAllInboxMessagesRead');
        },
        'mark all messages as read',
        () => 'Marked all messages as read',
        o?.silent ?? false,
      );
    },
    [runMutation],
  );

  const deleteMessages = useCallback(
    (msgs: readonly InboxMessage[]): Promise<InboxMutationResult> => {
      const ids = msgs.map((m) => m.id);
      const idSet = new Set(ids);
      const unread = unreadAmong(msgs);
      return runMutation(
        (s) => ({
          unreadCount: s.unreadCount == null ? null : Math.max(0, s.unreadCount - unread),
          recent: s.recent.filter((m) => !idSet.has(m.id)),
        }),
        async () => {
          const res = await client.graphql({ query: deleteInboxMessagesMutation, variables: { ids } });
          return requireResult(res.data?.deleteInboxMessages, 'deleteInboxMessages');
        },
        'delete messages',
        (r) => `Deleted ${plural(r.updated)}`,
        false,
      );
    },
    [runMutation],
  );

  const clearRead = useCallback(
    (): Promise<InboxMutationResult> =>
      runMutation(
        (s) => ({ unreadCount: s.unreadCount, recent: s.recent.filter((m) => !m.read) }),
        async () => {
          const res = await client.graphql({ query: clearReadInboxMessagesMutation });
          return requireResult(res.data?.clearReadInboxMessages, 'clearReadInboxMessages');
        },
        'clear read messages',
        () => 'Cleared read messages',
        false,
      ),
    [runMutation],
  );

  const value = useMemo<InboxContextValue>(
    () => ({
      unreadCount: state.unreadCount,
      recent: state.recent,
      loaded: state.loaded,
      pollError: state.pollError,
      changeToken: state.changeToken,
      refresh,
      syncUnreadCount,
      markMessages,
      markAllRead,
      deleteMessages,
      clearRead,
    }),
    [state, refresh, syncUnreadCount, markMessages, markAllRead, deleteMessages, clearRead],
  );

  return <InboxContext.Provider value={value}>{children}</InboxContext.Provider>;
};

export default InboxProvider;
