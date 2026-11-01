// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Keeps a queue definition the caller has just saved on screen, whatever the next reads
// return.
//
// A read straight after Save can still return the old definition: the API's queue
// listing reads past its per-process cache (`QueueDefStore`), but other readers of the
// definitions keep it for up to 30 seconds, and the listing itself is an eventually
// consistent read. A saved definition therefore stands in for the listed one until a
// read returns a later version (someone else saved since), or that window has passed;
// with an API that reports no version, until a read returns it.
import { useCallback, useMemo, useRef } from 'react';

import type { ReviewQueueDefinition } from '../../graphql/generated/operation-types';

/** Longer than the API's definition cache, so a stale read cannot outlast it. */
export const SAVED_DEFINITION_HOLD_MS = 35_000;

const sameDefinition = (a: ReviewQueueDefinition, b: ReviewQueueDefinition): boolean =>
  JSON.stringify({ ...a, __typename: undefined }) === JSON.stringify({ ...b, __typename: undefined });

/**
 * The list is to be believed over the held definition. Every write bumps the version,
 * so when both carry one only a later version (someone else saved since) replaces the
 * held one: the same version is the same definition, and holding it keeps a later
 * stale read from showing the old one again. An API that reports no version is
 * compared field by field.
 */
const caughtUp = (listed: ReviewQueueDefinition, held: ReviewQueueDefinition): boolean =>
  typeof listed.version === 'number' && typeof held.version === 'number' ? listed.version > held.version : sameDefinition(listed, held);

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
        if (caughtUp(d, held.definition) || at >= held.until) {
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
