// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The routing rules that send documents to one review queue, for an Admin: what the
// delete confirmation and the queue's page show. The lookup reads every configuration
// profile, so it runs once per queue and again only on `reload`; it is not polled.
import { useCallback, useEffect, useRef, useState } from 'react';
import { ConsoleLogger } from 'aws-amplify/utils';

import type { QueueReference } from '../../graphql/generated/operation-types';
import { getReviewQueueReferences } from './reviewer-assignment';

const logger = new ConsoleLogger('useQueueReferences');

export interface QueueReferencesState {
  /** Null until a lookup for this queue has answered. */
  references: QueueReference[] | null;
  loading: boolean;
  /** What the last lookup for this queue failed with, or null. */
  error: unknown;
  reload: () => void;
}

interface Loaded {
  queueId: string;
  references: QueueReference[] | null;
  loading: boolean;
  error: unknown;
}

/** The references to `queueId`; pass null (no queue chosen, or not an Admin) to look nothing up. */
const useQueueReferences = (queueId: string | null): QueueReferencesState => {
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  // Only the newest lookup may answer: a slow one for another queue must not.
  const newest = useRef(0);

  const load = useCallback(async () => {
    newest.current += 1;
    const mine = newest.current;
    if (!queueId) {
      setLoaded(null);
      return;
    }
    setLoaded((previous) => ({
      queueId,
      references: previous?.queueId === queueId ? previous.references : null,
      loading: true,
      error: null,
    }));
    try {
      const references = await getReviewQueueReferences(queueId);
      if (mine === newest.current) setLoaded({ queueId, references, loading: false, error: null });
    } catch (err) {
      logger.error('getReviewQueueReferences failed', err);
      if (mine === newest.current) setLoaded({ queueId, references: null, loading: false, error: err });
    }
  }, [queueId]);

  useEffect(() => {
    void load();
  }, [load]);

  const reload = useCallback(() => {
    void load();
  }, [load]);

  // The render after the queue changes still holds the answer for the one before.
  if (!queueId) return { references: null, loading: false, error: null, reload };
  if (loaded?.queueId !== queueId) return { references: null, loading: true, error: null, reload };
  return { references: loaded.references, loading: loaded.loading, error: loaded.error, reload };
};

export default useQueueReferences;
