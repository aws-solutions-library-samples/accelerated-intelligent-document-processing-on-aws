// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Reads what a run will do (the server's planned run) while a dialog is open, for
// PlannedSteps to show: the plan, whether it is being read, why the read failed, and a
// retry. A failed read is reported, never thrown: the dialog it serves stays usable.
import { useCallback, useEffect, useRef, useState } from 'react';
import { ConsoleLogger } from 'aws-amplify/utils';

import type { PlannedRun } from '../../graphql/generated/operation-types';
import { extractGraphQLErrorMessage } from '../../hooks/utils/graphql-error';

const logger = new ConsoleLogger('PlannedRun');

export interface PlannedRunRead<T = PlannedRun> {
  /** What the server answered; null until it has, and after a failed read. */
  plan: T | null;
  loading: boolean;
  /** Why the last read failed, in the server's words. */
  error: string | null;
  /** Read again. */
  retry: () => void;
}

interface ReadState<T> {
  plan: T | null;
  loading: boolean;
  error: string | null;
}

/** No read: the dialog is closed. */
const IDLE = { plan: null, loading: false, error: null };

/**
 * `load` reads the plan and is called while `active` is true: once when it becomes
 * true, again on `retry`, and again when `load` itself changes, so pass a memoized
 * function (`useCallback`). `T` is what `load` resolves: one planned run, or an answer
 * that holds several (the two endings of a review).
 */
const usePlannedRun = <T = PlannedRun>(load: () => Promise<T>, active: boolean): PlannedRunRead<T> => {
  const [state, setState] = useState<ReadState<T>>({ plan: null, loading: active, error: null });
  const [attempt, setAttempt] = useState(0);
  // The latest read wins: a retry, or closing the dialog, supersedes a read in flight.
  const readId = useRef(0);

  useEffect(() => {
    const id = ++readId.current;
    if (!active) {
      // Nothing is kept from one opening to the next; an idle read is left as it is.
      setState((current) => (current.plan === null && !current.loading && current.error === null ? current : IDLE));
      return undefined;
    }
    setState({ plan: null, loading: true, error: null });
    load().then(
      (plan) => {
        if (id === readId.current) setState({ plan, loading: false, error: null });
      },
      (err: unknown) => {
        logger.warn('the planned run could not be read', err);
        if (id === readId.current) setState({ plan: null, loading: false, error: extractGraphQLErrorMessage(err) });
      },
    );
    return () => {
      readId.current += 1;
    };
  }, [load, active, attempt]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  return { ...state, retry };
};

export default usePlannedRun;
