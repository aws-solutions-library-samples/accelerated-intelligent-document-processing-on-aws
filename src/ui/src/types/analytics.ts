// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import type { NoMessageValues, TypedMessageDescriptor } from 'react-intl';

/**
 * An error on the agents page: text from the server or from a thrown error, kept as it arrives, or
 * one of the page's messages, formatted at render so it follows a language switch. `detail` is the
 * thrown error's own text, for the message's `{error}`.
 */
export type AnalyticsError =
  | string
  | { message: TypedMessageDescriptor<NoMessageValues> }
  | { message: TypedMessageDescriptor<{ readonly error: string }>; detail: string };

export interface AnalyticsState {
  queryText: string;
  currentInputText: string;
  jobId: string | null;
  jobStatus: string | null;
  jobResult: unknown;
  agentMessages: unknown;
  error: AnalyticsError | null;
  isSubmitting: boolean;
  subscription: unknown;
}

export interface AnalyticsContextValue {
  analyticsState: AnalyticsState;
  updateAnalyticsState: (updates: Partial<AnalyticsState>) => void;
  resetAnalyticsState: () => void;
  clearAnalyticsResults: () => void;
}
