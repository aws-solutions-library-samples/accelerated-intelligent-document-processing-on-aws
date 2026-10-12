// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * A guard that lets only the newest in-flight request render its result.
 *
 * Needed wherever typing issues one request per keystroke against a server-side
 * list: the replies do not arrive in the order they were sent, and the naive
 * `setRows(reply)` renders whichever lands last rather than whichever was asked
 * for last.
 *
 * ⚠️ **The slow request is usually the broad one**, which is what makes this a
 * near-certainty rather than a rare race. On the test set documents table, the
 * resolver builds each returned row by reading that document's baseline sections,
 * so a search matching 500 documents does ~50 reads for its page while a search
 * matching one does one read. Typing `0063` sends `0`, `00`, `006`, `0063`; the
 * last is the fastest, paints the single correct match, and is then overwritten by
 * `0`'s reply arriving late with page 1 of the whole set — the search appearing to
 * reset itself a moment after working (GitHub #1323).
 *
 * Kept as a module rather than inline so the ordering can be tested with staged
 * out-of-order replies. A race cannot be caught by asserting on source text.
 *
 * Usage — claim before awaiting, check after:
 *
 * ```ts
 * const isCurrent = guard.claim();
 * const reply = await fetch();
 * if (!isCurrent()) return;   // a later claim superseded this one
 * setRows(reply);
 * ```
 */
export interface LatestResponseGuard {
  /**
   * Register a new request as the current one, returning a predicate that reports
   * whether it still is. Call this *before* awaiting, so the ordering is decided
   * by when each request started rather than by when it finished.
   */
  claim: () => () => boolean;
}

export const createLatestResponseGuard = (): LatestResponseGuard => {
  let current = 0;
  return {
    claim: () => {
      const ticket = ++current;
      return () => ticket === current;
    },
  };
};
