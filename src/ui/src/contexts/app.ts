// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useContext, createContext } from 'react';
import type { AuthUser } from 'aws-amplify/auth';

export interface AppActiveTestRun {
  testRunId: string;
  testSetName: string;
  context: string;
  filesCount: number;
  configVersion: string;
  startTime: Date;
}

export interface AppContextValue {
  authState: string;
  awsConfig: Record<string, unknown> | undefined;
  errorMessage: string | undefined;
  successMessage: string | undefined;
  currentCredentials: unknown;
  currentSession: unknown;
  /**
   * Whether credentials are still arriving, ready, or failed. Routes needs this
   * to tell "authenticated but waiting" apart from "not signed in" — conflating
   * them is what rendered a blank page after a valid sign-in.
   */
  // Required, not optional: there is exactly one provider and it always supplies
  // both. Optional would let an undefined status render the loading state
  // forever — a quieter version of the blank page this exists to prevent.
  credentialsStatus: 'idle' | 'pending' | 'ready' | 'error';
  retryCredentials: () => void;
  setErrorMessage: React.Dispatch<React.SetStateAction<string | undefined>>;
  setSuccessMessage: React.Dispatch<React.SetStateAction<string | undefined>>;
  user: AuthUser | undefined;
  navigationOpen: boolean;
  setNavigationOpen: React.Dispatch<React.SetStateAction<boolean>>;
  activeTestRuns: AppActiveTestRun[];
  addTestRun: (testRunId: string, testSetName: string, context: string, filesCount: number, configVersion: string) => void;
  removeTestRun: (testRunId: string) => void;
}

export const AppContext = createContext<AppContextValue | null>(null);

const useAppContext = (): AppContextValue => {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error('useAppContext must be used within AppContext.Provider');
  return ctx;
};

export default useAppContext;
