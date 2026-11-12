// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Whether the Default review queue is on, and the bulk Skip All Reviews inline limit,
// for Admins and Reviewers (from getMyReviewQueues, which they may read). Cached for
// 60 s across the app; null while unknown, so an action is not refused on a guess.
import { useEffect, useState } from 'react';
import { ConsoleLogger } from 'aws-amplify/utils';

import useUserRole from '../../hooks/use-user-role';
import { fetchMyReviewQueues } from '../review-queues/review-api';

const logger = new ConsoleLogger('useReviewActionSettings');
const TTL_MS = 60_000;

export interface ReviewActionSettings {
  defaultQueueEnabled: boolean | null;
  bulkSkipSyncLimit: number | null;
  loading: boolean;
}

let cache: { at: number; value: { defaultQueueEnabled: boolean; bulkSkipSyncLimit: number } } | null = null;
let inFlight: Promise<{ defaultQueueEnabled: boolean; bulkSkipSyncLimit: number }> | null = null;

/** For tests: forget the cached settings. */
export const resetReviewActionSettingsCache = (): void => {
  cache = null;
  inFlight = null;
};

const load = (): Promise<{ defaultQueueEnabled: boolean; bulkSkipSyncLimit: number }> => {
  if (cache && Date.now() - cache.at < TTL_MS) return Promise.resolve(cache.value);
  if (!inFlight) {
    inFlight = fetchMyReviewQueues(0)
      .then((r) => {
        const value = { defaultQueueEnabled: !!r.defaultQueueEnabled, bulkSkipSyncLimit: r.bulkSkipSyncLimit ?? 50 };
        cache = { at: Date.now(), value };
        return value;
      })
      .finally(() => {
        inFlight = null;
      });
  }
  return inFlight;
};

const useReviewActionSettings = (): ReviewActionSettings => {
  const { canReview } = useUserRole();
  const [state, setState] = useState<ReviewActionSettings>({
    defaultQueueEnabled: cache?.value.defaultQueueEnabled ?? null,
    bulkSkipSyncLimit: cache?.value.bulkSkipSyncLimit ?? null,
    loading: !!canReview && !cache,
  });

  useEffect(() => {
    if (!canReview) return undefined;
    let live = true;
    load()
      .then((value) => {
        if (live) setState({ ...value, loading: false });
      })
      .catch((error) => {
        logger.warn('Review action settings unavailable', error);
        if (live) setState((s) => ({ ...s, loading: false }));
      });
    return () => {
      live = false;
    };
  }, [canReview]);

  return state;
};

export default useReviewActionSettings;
