// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/** The fields of an abortTestRuns result the Test Studio list displays. */
export interface AbortTestRunsResult {
  success: boolean;
  message?: string | null;
  abortedCount: number;
  failedCount: number;
  errors?: (string | null)[] | null;
}

/**
 * What to show after a successful abort: a success line, plus a warning when the
 * abort could not confirm that every document stopped (#1609). The run is still
 * marked ABORTED in that case, so it is a success, but the documents named in
 * `errors` may still be processing and the user must not be told otherwise.
 */
export const abortSuccessMessages = (result: AbortTestRunsResult): { success: string; warning: string | null } => {
  const { abortedCount, failedCount } = result;
  let success = `Successfully aborted ${abortedCount} test run${abortedCount > 1 ? 's' : ''}`;
  if (failedCount > 0) {
    success += `, ${failedCount} failed`;
  }
  const errors = (result.errors ?? []).filter((e): e is string => Boolean(e));
  const warning = errors.length > 0 ? `${result.message ?? 'Abort incomplete'}. ${errors.join(' ')}` : null;
  return { success, warning };
};
