// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * A just-saved queue definition stays on screen. What has to hold: after Save, a read
 * that still returns the old definition (another API instance's 30-second cache) shows
 * the saved one; a read that returns the saved one, or one after the hold, is believed
 * again; a just-created queue shows before the list has it; and a deleted queue is not
 * brought back.
 */

import { renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { ReviewQueueDefinition } from '../../../graphql/generated/operation-types';
import useSavedQueueDefinitions, { SAVED_DEFINITION_HOLD_MS } from '../use-saved-queue-definitions';

const def = (over: Partial<ReviewQueueDefinition> = {}): ReviewQueueDefinition =>
  ({
    id: 'default',
    name: 'Default',
    description: '',
    priority: 50,
    checklist: [],
    rejectReasons: [],
    skipReasons: [],
    timeLimitMinutes: 60,
    eligibleAnnotators: [],
    isDefaultQueue: true,
    enabled: true,
    ...over,
  }) as ReviewQueueDefinition;

const setup = () => {
  const clock = { now: 1_000 };
  const { result } = renderHook(() => useSavedQueueDefinitions(() => clock.now));
  return { saved: () => result.current, clock };
};

describe('useSavedQueueDefinitions', () => {
  it('shows the saved definition over a stale read', () => {
    const { saved } = setup();
    saved().remember(def({ name: 'Renamed' }));
    expect(saved().apply([def()])[0].name).toBe('Renamed');
  });

  it('believes the list again once it returns the saved definition', () => {
    const { saved } = setup();
    saved().remember(def({ name: 'Renamed' }));
    expect(saved().apply([def({ name: 'Renamed' })])[0].name).toBe('Renamed');
    // No longer held: a later, genuine change made elsewhere shows.
    expect(saved().apply([def({ name: 'Changed by someone else' })])[0].name).toBe('Changed by someone else');
  });

  it('believes the list again after the hold, whatever it says', () => {
    const { saved, clock } = setup();
    saved().remember(def({ name: 'Renamed' }));
    clock.now += SAVED_DEFINITION_HOLD_MS;
    expect(saved().apply([def()])[0].name).toBe('Default');
  });

  it('shows a just-created queue before the list has it, and not a deleted one', () => {
    const { saved } = setup();
    saved().remember(def({ id: 'claims', name: 'Claims', isDefaultQueue: false }));
    expect(
      saved()
        .apply([def()])
        .map((d) => d.id),
    ).toEqual(['default', 'claims']);
    saved().forget('claims');
    expect(
      saved()
        .apply([def()])
        .map((d) => d.id),
    ).toEqual(['default']);
  });

  it('is one object for the component’s life, so callers can depend on it', () => {
    const clock = { now: 1_000 };
    const { result, rerender } = renderHook(() => useSavedQueueDefinitions(clockFn));
    function clockFn() {
      return clock.now;
    }
    const first = result.current;
    rerender();
    expect(result.current).toBe(first);
  });
});
