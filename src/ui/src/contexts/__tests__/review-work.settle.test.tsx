// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The Tasks badge after finishing an item. The task lists read a GSI that lags the
 * write ending the assignment, so a poll straight after Complete can still return the
 * item; `settle` must keep it out of the count until the server stops returning it.
 */

import React from 'react';
import { act, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ReviewWorkProvider, SETTLED_HIDE_MS } from '../review-work';
import type { ReviewWorkContextValue } from '../review-work';
import useReviewWork from '../../hooks/use-review-work';

const { graphqlMock, role } = vi.hoisted(() => ({
  graphqlMock: vi.fn(),
  role: { canWorkTestSetQueues: true, isAuthor: false, loading: false },
}));
vi.mock('../../api/client-shim', () => ({
  generateClient: () => ({ graphql: graphqlMock }),
}));
vi.mock('../../hooks/use-user-role', () => ({ default: () => role }));

const task = (objectKey: string) => ({ testSetId: 'ts1', objectKey, assignedTo: 'a@example.com', documentClasses: [] });

/** Server answers: production items and Test Studio tasks, by object key. */
let server: { production: string[]; testSet: string[] };
const answer = ({ query }: { query: string }) => {
  if (query.includes('listMyReviewWork(')) {
    return Promise.resolve({ data: { listMyReviewWork: { items: server.production.map((objectKey) => ({ objectKey })) } } });
  }
  if (query.includes('listMyTestSetTasks')) {
    return Promise.resolve({ data: { listMyTestSetTasks: server.testSet.map(task) } });
  }
  return Promise.reject(new Error(`unexpected query ${query.slice(0, 40)}`));
};

let latest: ReviewWorkContextValue;
const Consumer = (): React.JSX.Element => {
  latest = useReviewWork();
  return <span>{latest.count ?? 'none'}</span>;
};

const flush = async (ms = 0) => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
};

describe('ReviewWorkProvider.settle', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    graphqlMock.mockReset().mockImplementation(answer);
    server = { production: ['doc-p.pdf'], testSet: ['doc-a.pdf', 'doc-b.pdf'] };
    Object.assign(role, { canWorkTestSetQueues: true, isAuthor: false, loading: false });
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
    render(
      <ReviewWorkProvider>
        <Consumer />
      </ReviewWorkProvider>,
    );
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('drops a finished item at once and keeps it out while the server lags', async () => {
    await flush();
    expect(latest.count).toBe(3);

    act(() => latest.settle('t:ts1:doc-a.pdf'));
    expect(latest.count).toBe(2);

    // The refresh after Complete reads the lagging index, which still returns it.
    await act(async () => {
      await latest.refresh();
    });
    expect(latest.count).toBe(2);
    expect(latest.testSetTasks?.map((t) => t.objectKey)).toEqual(['doc-b.pdf']);
  });

  it('drops a finished production item the same way', async () => {
    await flush();
    act(() => latest.settle('p:doc-p.pdf'));
    await act(async () => {
      await latest.refresh();
    });
    expect(latest.myItems).toEqual([]);
    expect(latest.count).toBe(2);
  });

  it('shows the item again if it is still assigned after the hide window', async () => {
    await flush();
    act(() => latest.settle('t:ts1:doc-a.pdf'));
    await flush(SETTLED_HIDE_MS + 1);
    await act(async () => {
      await latest.refresh();
    });
    expect(latest.count).toBe(3);
  });

  it('forgets the item once the server stops returning it, so a re-assignment shows', async () => {
    await flush();
    act(() => latest.settle('t:ts1:doc-a.pdf'));
    server.testSet = ['doc-b.pdf'];
    await act(async () => {
      await latest.refresh();
    });
    expect(latest.count).toBe(2);

    // Assigned to the caller again: it must count, not stay hidden.
    server.testSet = ['doc-a.pdf', 'doc-b.pdf'];
    await act(async () => {
      await latest.refresh();
    });
    expect(latest.count).toBe(3);
  });
});

/**
 * A Reviewer-only user works Production queues only, and the server refuses them
 * `listMyTestSetTasks`. The poll must not ask, and must still settle: the Test Studio
 * source is answered with an empty list rather than reported as a failure.
 */
describe('ReviewWorkProvider for a Reviewer-only user', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    graphqlMock.mockReset().mockImplementation(answer);
    server = { production: ['doc-p.pdf'], testSet: ['doc-a.pdf'] };
    Object.assign(role, { canWorkTestSetQueues: false, isAuthor: false, loading: false });
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
    render(
      <ReviewWorkProvider>
        <Consumer />
      </ReviewWorkProvider>,
    );
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  const testSetCalls = () => graphqlMock.mock.calls.filter(([arg]) => (arg as { query: string }).query.includes('listMyTestSetTasks'));

  it('makes no listMyTestSetTasks call and reports no error', async () => {
    await flush();
    expect(testSetCalls()).toHaveLength(0);
    expect(latest.loaded).toBe(true);
    expect(latest.pollError).toBeNull();
    expect(latest.testSetTasks).toEqual([]);
    expect(latest.count).toBe(1);
  });

  it('still settles a finished production item', async () => {
    await flush();
    act(() => latest.settle('p:doc-p.pdf'));
    server.production = [];
    await act(async () => {
      await latest.refresh();
    });
    expect(latest.count).toBe(0);
    expect(testSetCalls()).toHaveLength(0);
  });
});

describe('ReviewWorkProvider for an Author', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('reads Test Studio tasks', async () => {
    vi.useFakeTimers();
    graphqlMock.mockReset().mockImplementation(answer);
    server = { production: [], testSet: ['doc-a.pdf'] };
    Object.assign(role, { canWorkTestSetQueues: false, isAuthor: true, loading: false });
    render(
      <ReviewWorkProvider>
        <Consumer />
      </ReviewWorkProvider>,
    );
    await flush();
    expect(latest.count).toBe(1);
  });
});
