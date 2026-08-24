// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { ConsoleLogger } from 'aws-amplify/utils';
import { Navigate, Route, Routes } from 'react-router-dom';

import { Button, useAuthenticator } from '@aws-amplify/ui-react';
import { Spinner } from '@cloudscape-design/components';

import { SettingsContext } from '../contexts/settings';
import useParameterStore from '../hooks/use-parameter-store';
import useAppContext from '../contexts/app';
import useUserRole from '../hooks/use-user-role';

import DocumentsRoutes from './DocumentsRoutes';
import DocumentsQueryRoutes from './DocumentsQueryRoutes';
import DocumentsAnalyticsRoutes from './DocumentsAnalyticsRoutes';
import TestStudioRoutes from './TestStudioRoutes';
import AgentChatRoutes from './AgentChatRoutes';
import QuickStartWidget from '../components/agent-chat/QuickStartWidget';
import FeaturesRoutes from './FeaturesRoutes';
import WelcomePage from '../pages/WelcomePage';

import {
  DOCUMENTS_PATH,
  DEFAULT_PATH,
  LOGIN_PATH,
  LOGOUT_PATH,
  DOCUMENTS_KB_QUERY_PATH,
  DOCUMENTS_ANALYTICS_PATH,
  TEST_STUDIO_PATH,
  AGENT_CHAT_PATH,
  FEATURES_PATH_PREFIX,
  WELCOME_PATH,
  WELCOME_DISMISSED_KEY,
  ANNOTATE_LANDING_PATH,
} from './constants';

const logger = new ConsoleLogger('AuthRoutes');

interface AuthRoutesProps {
  redirectParam: string;
}

const AuthRoutes = ({ redirectParam }: AuthRoutesProps): React.JSX.Element => {
  const { currentCredentials } = useAppContext();
  const { isAnnotatorOnly } = useUserRole();
  const settings = useParameterStore(currentCredentials);
  const { signOut } = useAuthenticator();

  const settingsContextValue = {
    settings,
  };
  logger.debug('settingsContextValue', settingsContextValue);

  // Vertical-product packs (Claims, Loans, etc.) set DefaultFeatureId in
  // the SSM Settings parameter so a customer's first navigation goes to
  // /features/<id> instead of /documents. Empty string preserves the
  // standard accelerator landing.
  //
  // IMPORTANT: SSM settings load asynchronously. If we render Routes
  // before settings are populated, the catch-all <Route path="*"> redirects
  // to /documents immediately and the URL is "stuck" there once settings
  // arrive. Wait until the parameter has been fetched (settings has
  // any keys) before rendering. This adds a brief loading flash but
  // guarantees the first navigation honours DefaultFeatureId.
  const settingsLoaded = settings && Object.keys(settings).length > 0;
  const defaultFeatureId = (settings as Record<string, unknown>)?.DefaultFeatureId as string | undefined;
  // Landing precedence: a configured vertical-product feature wins; otherwise show
  // the welcome page unless the user has dismissed it; otherwise the documents list.
  let welcomeDismissed = false;
  try {
    welcomeDismissed = localStorage.getItem(WELCOME_DISMISSED_KEY) === 'true';
  } catch {
    /* ignore */
  }
  // An Annotator lands in their queue. The welcome page offers Quick Start, the
  // tour (which ends at Quick Start), Configuration and Upload Document — none of
  // which they are scoped to, so it would be a page of dead ends.
  const landingPath = isAnnotatorOnly
    ? ANNOTATE_LANDING_PATH
    : defaultFeatureId
      ? `${FEATURES_PATH_PREFIX}/${defaultFeatureId}`
      : welcomeDismissed
        ? DEFAULT_PATH
        : WELCOME_PATH;

  if (!settingsLoaded) {
    return (
      <SettingsContext.Provider value={settingsContextValue}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '50vh' }}>
          <Spinner size="large" />
        </div>
      </SettingsContext.Provider>
    );
  }

  return (
    <SettingsContext.Provider value={settingsContextValue}>
      <Routes>
        <Route path={WELCOME_PATH} element={<WelcomePage />} />
        <Route path={`${AGENT_CHAT_PATH}/*`} element={<AgentChatRoutes />} />
        <Route path={`${FEATURES_PATH_PREFIX}/*`} element={<FeaturesRoutes />} />
        <Route path={`${DOCUMENTS_KB_QUERY_PATH}/*`} element={<DocumentsQueryRoutes />} />
        <Route path={`${DOCUMENTS_ANALYTICS_PATH}/*`} element={<DocumentsAnalyticsRoutes />} />
        <Route path={`${TEST_STUDIO_PATH}/*`} element={<TestStudioRoutes />} />
        <Route path={`${DOCUMENTS_PATH}/*`} element={<DocumentsRoutes />} />
        <Route
          path={LOGIN_PATH}
          element={<Navigate to={!redirectParam || redirectParam === LOGIN_PATH ? landingPath : `${redirectParam}`} replace />}
        />
        <Route path={LOGOUT_PATH} element={<Button onClick={signOut}>Sign Out</Button>} />
        <Route path="*" element={<Navigate to={landingPath} replace />} />
      </Routes>
      <QuickStartWidget />
    </SettingsContext.Provider>
  );
};

export default AuthRoutes;
