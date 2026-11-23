// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Whether the Default review queue is on, the bulk Skip All Reviews inline limit, and
// whether Review Routing is on in the active configuration profile, for Admins and
// Reviewers (from getMyReviewQueues, which they may read). Cached for 60 s across the
// app; null while unknown, so an action is not refused, and no warning shown, on a guess.
import { useEffect, useState } from 'react';
import { ConsoleLogger } from 'aws-amplify/utils';

import useUserRole from '../../hooks/use-user-role';
import { fetchMyReviewQueues } from '../review-queues/review-api';

const logger = new ConsoleLogger('useReviewActionSettings');
const TTL_MS = 60_000;

export interface ReviewActionSettings {
  defaultQueueEnabled: boolean | null;
  bulkSkipSyncLimit: number | null;
  /**
   * Review Routing (`hitl.enabled`) of the active configuration profile. Null while
   * unknown, and when the server could not read the profile.
   */
  reviewRoutingEnabled: boolean | null;
  loading: boolean;
}

/** What the server answered. */
interface LoadedSettings {
  defaultQueueEnabled: boolean;
  bulkSkipSyncLimit: number;
  reviewRoutingEnabled: boolean | null;
}

let cache: { at: number; value: LoadedSettings } | null = null;
let inFlight: Promise<LoadedSettings> | null = null;

/** For tests: forget the cached settings. */
export const resetReviewActionSettingsCache = (): void => {
  cache = null;
  inFlight = null;
};

const load = (): Promise<LoadedSettings> => {
  if (cache && Date.now() - cache.at < TTL_MS) return Promise.resolve(cache.value);
  if (!inFlight) {
    inFlight = fetchMyReviewQueues(0)
      .then((r) => {
        const value: LoadedSettings = {
          defaultQueueEnabled: !!r.defaultQueueEnabled,
          bulkSkipSyncLimit: r.bulkSkipSyncLimit ?? 50,
          reviewRoutingEnabled: r.reviewRoutingEnabled ?? null,
        };
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
    reviewRoutingEnabled: cache?.value.reviewRoutingEnabled ?? null,
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
