// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The one My Work › Tasks poll for the whole app. Mounted once in the authenticated
// shell for Annotators, so the navigation badge and the Tasks page share a single
// 30-second poll: `listMyReviewWork` (assigned production items) and
// `listMyTestSetTasks` (assigned Test Studio documents), in parallel.
import React, { createContext, useCallback, useMemo, useRef, useState } from 'react';
import { ConsoleLogger } from 'aws-amplify/utils';

import { generateClient } from '../api/client-shim';
import { listMyReviewWork as listMyReviewWorkQuery } from '../graphql/generated';
import type { ReviewItem } from '../graphql/generated/operation-types';
import usePolling from '../hooks/use-polling';
import { listMyTestSetTasks } from '../components/review-queues/reviewer-assignment';
import { fromTestSetTask } from '../components/review-queues/work-items';
import type { WorkItem } from '../components/review-queues/work-items';

const client = generateClient();
const logger = new ConsoleLogger('ReviewWorkProvider');

export const REVIEW_WORK_POLL_INTERVAL_MS = 30_000;

export interface ReviewWorkContextValue {
  /** Production review items assigned to the caller. */
  myItems: readonly ReviewItem[];
  /** Test Studio documents assigned to the caller (null until loaded). */
  testSetTasks: readonly WorkItem[] | null;
  /** Production items plus Test Studio documents; null until the first load. */
  count: number | null;
  loaded: boolean;
  pollError: unknown;
  /** Waits for an in-flight poll, then polls again. Call after completing or rejecting an item. */
  refresh: () => Promise<void>;
  /** Same as `refresh`: both kinds of work are loaded by the one poll. */
  refreshTestSetTasks: () => Promise<void>;
  /** Discard polls started before now (call before a mutation). */
  beginMutation: () => void;
}

export const ReviewWorkContext = createContext<ReviewWorkContextValue | null>(null);

interface State {
  myItems: readonly ReviewItem[];
  testSetTasks: readonly WorkItem[] | null;
  count: number | null;
  loaded: boolean;
  pollError: unknown;
}

const INITIAL: State = { myItems: [], testSetTasks: null, count: null, loaded: false, pollError: null };

export const ReviewWorkProvider = ({ children }: { children: React.ReactNode }): React.JSX.Element => {
  const [state, setState] = useState<State>(INITIAL);
  const inFlightRef = useRef<Promise<void> | null>(null);
  const epochRef = useRef(0);

  const poll = useCallback((): Promise<void> => {
    if (inFlightRef.current) return inFlightRef.current;
    const epoch = epochRef.current;
    const request = (async () => {
      try {
        const [production, testSets] = await Promise.allSettled([
          client.graphql({ query: listMyReviewWorkQuery, variables: { limit: 100 } }),
          listMyTestSetTasks(),
        ]);
        if (epoch !== epochRef.current) return;
        setState((prev) => {
          const myItems = production.status === 'fulfilled' ? (production.value.data?.listMyReviewWork?.items ?? []) : prev.myItems;
          const testSetTasks = testSets.status === 'fulfilled' ? testSets.value.map(fromTestSetTask) : prev.testSetTasks;
          const failure = production.status === 'rejected' ? production.reason : testSets.status === 'rejected' ? testSets.reason : null;
          if (failure) logger.error('My Work poll failed', failure);
          return {
            myItems,
            testSetTasks: testSetTasks ?? (failure ? [] : null),
            count: myItems.length + (testSetTasks?.length ?? 0),
            loaded: true,
            pollError: failure,
          };
        });
      } finally {
        inFlightRef.current = null;
      }
    })();
    inFlightRef.current = request;
    return request;
  }, []);

  usePolling(poll, { enabled: true, intervalMs: REVIEW_WORK_POLL_INTERVAL_MS, immediate: true });

  const refresh = useCallback(async (): Promise<void> => {
    epochRef.current += 1;
    if (inFlightRef.current) await inFlightRef.current.catch(() => undefined);
    await poll();
  }, [poll]);

  const beginMutation = useCallback(() => {
    epochRef.current += 1;
  }, []);

  const value = useMemo<ReviewWorkContextValue>(
    () => ({
      ...state,
      refresh,
      refreshTestSetTasks: refresh,
      beginMutation,
    }),
    [state, refresh, beginMutation],
  );

  return <ReviewWorkContext.Provider value={value}>{children}</ReviewWorkContext.Provider>;
};
