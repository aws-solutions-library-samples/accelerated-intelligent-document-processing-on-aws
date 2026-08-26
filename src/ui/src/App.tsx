// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { useState } from 'react';
import { HashRouter } from 'react-router-dom';
import { Authenticator, ThemeProvider, useAuthenticator } from '@aws-amplify/ui-react';
import { ConsoleLogger } from 'aws-amplify/utils';
import '@aws-amplify/ui-react/styles.css';

import { AppContext, type AppActiveTestRun } from './contexts/app';
import { AnalyticsProvider } from './contexts/analytics';
import { AgentChatProvider } from './contexts/agentChat';
import { GuidedTourProvider } from './contexts/guidedTour';
import useAwsConfig from './hooks/use-aws-config';
import useCurrentSessionCreds from './hooks/use-current-session-creds';

import Routes from './routes/Routes';

import './App.css';

const logger = new ConsoleLogger('App', import.meta.env.DEV ? 'DEBUG' : 'WARN');

const AppContent = (): React.JSX.Element => {
  const awsConfig = useAwsConfig();
  const { authStatus: authState, user } = useAuthenticator((context) => [context.authStatus, context.user]);
  const { currentSession, currentCredentials, credentialsStatus, retryCredentials } = useCurrentSessionCreds({});
  const [errorMessage, setErrorMessage] = useState<string | undefined>();
  const [successMessage, setSuccessMessage] = useState<string | undefined>();
  const [navigationOpen, setNavigationOpen] = useState<boolean>(true);
  const [activeTestRuns, setActiveTestRuns] = useState<AppActiveTestRun[]>([]);

  const addTestRun = (testRunId: string, testSetName: string, context: string, filesCount: number, configVersion: string): void => {
    setActiveTestRuns((prev) => [...prev, { testRunId, testSetName, context, filesCount, configVersion, startTime: new Date() }]);
  };

  const removeTestRun = (testRunId: string): void => {
    setActiveTestRuns((prev) => prev.filter((run) => run.testRunId !== testRunId));
  };

  const appContextValue = {
    authState,
    awsConfig,
    errorMessage,
    successMessage,
    currentCredentials,
    currentSession,
    credentialsStatus,
    retryCredentials,
    setErrorMessage,
    setSuccessMessage,
    user,
    navigationOpen,
    setNavigationOpen,
    activeTestRuns,
    addTestRun,
    removeTestRun,
  };
  logger.debug('appContextValue', appContextValue);
  // TODO: Remove the AnalyticsProvider once we migrate full to Agent Chat
  return (
    <div className="App">
      <AppContext.Provider value={appContextValue}>
        <AnalyticsProvider>
          <AgentChatProvider>
            <HashRouter>
              <GuidedTourProvider>
                <Routes />
              </GuidedTourProvider>
            </HashRouter>
          </AgentChatProvider>
        </AnalyticsProvider>
      </AppContext.Provider>
    </div>
  );
};

const App = (): React.JSX.Element => {
  return (
    <ThemeProvider>
      <Authenticator.Provider>
        <AppContent />
      </Authenticator.Provider>
    </ThemeProvider>
  );
};

export default App;
