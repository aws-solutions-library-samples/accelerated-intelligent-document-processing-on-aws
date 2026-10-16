// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Inbox provider is the one poll behind the bell, the navigation badge and the
 * Inbox page. These tests mount three consumers under one provider and count the
 * `listInboxMessages` requests, so a second poll (or a missing guard) shows up as
 * an extra call rather than as a subtle UI difference.
 */

import React from 'react';
import { act, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AppContext } from '../app';
import type { AppContextValue } from '../app';
import { InboxProvider } from '../inbox';
import type { InboxContextValue } from '../inbox';
import useInbox from '../../hooks/use-inbox';
import type { InboxMessage } from '../../graphql/generated/operation-types';

const { graphqlMock } = vi.hoisted(() => ({ graphqlMock: vi.fn() }));
vi.mock('../../api/client-shim', () => ({
  generateClient: () => ({ graphql: graphqlMock }),
}));

const msg = (id: string, read: boolean): InboxMessage => ({
  id,
  type: 'review.assigned',
  title: `Message ${id}`,
  body: '',
  severity: 'info',
  link: null,
  source: 'test',
  createdAt: '2026-10-10T10:00:00Z',
  read,
  readAt: read ? '2026-10-10T10:01:00Z' : null,
  metadata: null,
});

const page = (items: InboxMessage[], unreadCount: number) => ({ data: { listInboxMessages: { items, nextToken: null, unreadCount } } });

const isList = (args: { query: string }) => args.query.includes('listInboxMessages(');
const listCalls = () => graphqlMock.mock.calls.filter(([args]) => isList(args as { query: string }));

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (v: T) => void;
  reject: (e: unknown) => void;
}
const deferred = <T,>(): Deferred<T> => {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};

const setVisibility = (value: 'visible' | 'hidden') => {
  Object.defineProperty(document, 'visibilityState', { value, configurable: true });
  document.dispatchEvent(new Event('visibilitychange'));
};

const flush = async () => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
};

const advance = async (ms: number) => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
};

let latest: InboxContextValue[] = [];
const Consumer = ({ index }: { index: number }): React.JSX.Element => {
  latest[index] = useInbox();
  return <span>{latest[index].unreadCount ?? 'none'}</span>;
};

const setSuccessMessage = vi.fn();
const setErrorMessage = vi.fn();

const renderProvider = () =>
  render(
    <AppContext.Provider value={{ setSuccessMessage, setErrorMessage } as unknown as AppContextValue}>
      <InboxProvider>
        <Consumer index={0} />
        <Consumer index={1} />
        <Consumer index={2} />
      </InboxProvider>
    </AppContext.Provider>,
  );

describe('InboxProvider', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    graphqlMock.mockReset();
    setSuccessMessage.mockReset();
    setErrorMessage.mockReset();
    latest = [];
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('(a) runs one shared poll for every consumer, once per minute', async () => {
    graphqlMock.mockResolvedValue(page([msg('a', false)], 1));
    renderProvider();
    await flush();

    expect(listCalls()).toHaveLength(1);
    expect(listCalls()[0][0].variables).toEqual({ limit: 5 });
    expect(latest.map((v) => v.unreadCount)).toEqual([1, 1, 1]);

    await advance(60_000);
    expect(listCalls()).toHaveLength(2);
    await advance(60_000);
    expect(listCalls()).toHaveLength(3);
  });

  it('(b) pauses while the tab is hidden and polls once on return', async () => {
    graphqlMock.mockResolvedValue(page([], 0));
    renderProvider();
    await flush();
    expect(listCalls()).toHaveLength(1);

    act(() => setVisibility('hidden'));
    await advance(180_000);
    expect(listCalls()).toHaveLength(1);

    act(() => setVisibility('visible'));
    await flush();
    expect(listCalls()).toHaveLength(2);
  });

  it('(c) does not start a second request while one is in flight', async () => {
    const pending = deferred<ReturnType<typeof page>>();
    graphqlMock.mockReturnValue(pending.promise);
    renderProvider();
    await flush();
    expect(listCalls()).toHaveLength(1);

    await advance(60_000);
    await advance(60_000);
    expect(listCalls()).toHaveLength(1);

    pending.resolve(page([], 0));
    await flush();
    expect(latest[0].loaded).toBe(true);
  });

  it('(d) restores state on a failed mark and applies the server count on success', async () => {
    const recent = [msg('a', false), msg('b', false)];
    graphqlMock.mockResolvedValue(page(recent, 2));
    renderProvider();
    await flush();
    const tokenBefore = latest[0].changeToken;

    graphqlMock.mockImplementation((args: { query: string }) =>
      isList(args) ? Promise.resolve(page(recent, 2)) : Promise.reject({ errors: [{ message: 'boom', errorType: 'InternalError' }] }),
    );
    let failure: unknown;
    await act(async () => {
      failure = await latest[0].markMessages([recent[0]], true).catch((e: unknown) => e);
    });
    expect(failure).toBeTruthy();
    expect(latest[0].unreadCount).toBe(2);
    expect(latest[0].recent.map((m) => m.read)).toEqual([false, false]);
    expect(setErrorMessage).toHaveBeenCalledTimes(1);
    expect(setSuccessMessage).not.toHaveBeenCalled();

    graphqlMock.mockImplementation((args: { query: string }) =>
      isList(args)
        ? Promise.resolve(page([msg('a', true), recent[1]], 1))
        : Promise.resolve({ data: { markInboxMessages: { updated: 1, notFoundIds: [], unreadCount: 1 } } }),
    );
    await act(async () => {
      await latest[0].markMessages([recent[0]], true);
    });
    await flush();
    expect(latest[0].unreadCount).toBe(1);
    expect(latest[0].changeToken).toBeGreaterThan(tokenBefore);
    expect(setSuccessMessage).toHaveBeenCalledWith('Marked 1 message as read');
  });

  it('(e) discards a poll that started before a mutation and resolved after it', async () => {
    graphqlMock.mockResolvedValue(page([msg('a', false)], 1));
    renderProvider();
    await flush();

    const stale = deferred<ReturnType<typeof page>>();
    let listCount = 0;
    graphqlMock.mockImplementation((args: { query: string }) => {
      if (isList(args)) {
        listCount += 1;
        // The first poll after this point is the stale one; later polls see the new state.
        return listCount === 1 ? stale.promise : Promise.resolve(page([msg('a', true)], 0));
      }
      return Promise.resolve({ data: { markAllInboxMessagesRead: { updated: 1, notFoundIds: [], unreadCount: 0 } } });
    });

    await advance(60_000); // the stale poll is now in flight
    expect(listCount).toBe(1);

    await act(async () => {
      await latest[0].markAllRead({ silent: true });
    });
    expect(latest[0].unreadCount).toBe(0);

    // The stale response claims one unread; it must not overwrite the mutation result.
    stale.resolve(page([msg('a', false)], 1));
    await flush();
    expect(latest[0].unreadCount).toBe(0);
    expect(latest[0].recent[0].read).toBe(true);
  });

  it('(f) keeps the previous values on a poll error and flashes nothing', async () => {
    graphqlMock.mockResolvedValue(page([msg('a', false)], 1));
    renderProvider();
    await flush();

    graphqlMock.mockRejectedValue({ errors: [{ message: 'down', errorType: 'NetworkError' }] });
    await advance(60_000);

    expect(latest[0].unreadCount).toBe(1);
    expect(latest[0].recent).toHaveLength(1);
    expect(latest[0].pollError).toBeTruthy();
    expect(setErrorMessage).not.toHaveBeenCalled();
    expect(setSuccessMessage).not.toHaveBeenCalled();
  });

  it('(g) does not flash on arrival', async () => {
    graphqlMock.mockResolvedValue(page([msg('a', false)], 1));
    renderProvider();
    await flush();
    const tokenBefore = latest[0].changeToken;

    graphqlMock.mockResolvedValue(page([msg('b', false), msg('a', false)], 2));
    await advance(60_000);

    expect(latest[0].unreadCount).toBe(2);
    expect(latest[0].changeToken).toBe(tokenBefore + 1);
    expect(setSuccessMessage).not.toHaveBeenCalled();
    expect(setErrorMessage).not.toHaveBeenCalled();
  });
});
