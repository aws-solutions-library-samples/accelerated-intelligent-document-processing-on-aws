// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Keeps a queue definition the caller has just saved on screen, whatever the next reads
// return.
//
// The API serves queue definitions from a short per-process cache (`QueueDefStore`,
// 30 seconds). A save clears the cache of the instance that handled it, but the next
// read may be served by another instance that still holds the old list, so a reload
// straight after Save can show the old name and settings again. A saved definition
// therefore stands in for the listed one until a read returns it, or the cache window
// has passed.
import { useCallback, useMemo, useRef } from 'react';

import type { ReviewQueueDefinition } from '../../graphql/generated/operation-types';

/** Longer than the API's definition cache, so a stale read cannot outlast it. */
export const SAVED_DEFINITION_HOLD_MS = 35_000;

const sameDefinition = (a: ReviewQueueDefinition, b: ReviewQueueDefinition): boolean =>
  JSON.stringify({ ...a, __typename: undefined }) === JSON.stringify({ ...b, __typename: undefined });

interface SavedQueueDefinitions {
  /** Record a definition the caller has just saved. */
  remember: (definition: ReviewQueueDefinition) => void;
  /** Stop holding a queue's definition (it was deleted). */
  forget: (id: string) => void;
  /** The listed definitions, with every recently saved one in place of what the list says. */
  apply: (listed: ReviewQueueDefinition[]) => ReviewQueueDefinition[];
}

const useSavedQueueDefinitions = (now: () => number = Date.now): SavedQueueDefinitions => {
  const saved = useRef(new Map<string, { definition: ReviewQueueDefinition; until: number }>());

  const remember = useCallback(
    (definition: ReviewQueueDefinition) => saved.current.set(definition.id, { definition, until: now() + SAVED_DEFINITION_HOLD_MS }),
    [now],
  );

  const forget = useCallback((id: string) => {
    saved.current.delete(id);
  }, []);

  const apply = useCallback(
    (listed: ReviewQueueDefinition[]): ReviewQueueDefinition[] => {
      const at = now();
      const out = listed.map((d) => {
        const held = saved.current.get(d.id);
        if (!held) return d;
        // The list has caught up, or the cache window is over: the read is authoritative again.
        if (sameDefinition(d, held.definition) || at >= held.until) {
          saved.current.delete(d.id);
          return d;
        }
        return held.definition;
      });
      // A queue just created may not be listed yet.
      saved.current.forEach(({ definition, until }, id) => {
        if (at < until && !out.some((d) => d.id === id)) out.push(definition);
      });
      return out;
    },
    [now],
  );

  // One object for the component's life, so callers can list it as a dependency.
  return useMemo(() => ({ remember, forget, apply }), [remember, forget, apply]);
};

export default useSavedQueueDefinitions;
