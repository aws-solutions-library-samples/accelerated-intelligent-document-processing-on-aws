// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { ConsoleLogger } from 'aws-amplify/utils';
import { Navigate, Route, Routes } from 'react-router-dom';

import { Button, useAuthenticator } from '@aws-amplify/ui-react';
import { Spinner } from '@cloudscape-design/components';

import { SettingsContext } from '../contexts/settings';
import { InboxProvider } from '../contexts/inbox';
import { ReviewWorkProvider } from '../contexts/review-work';
import useParameterStore from '../hooks/use-parameter-store';
import useAppContext from '../contexts/app';
import useUserRole from '../hooks/use-user-role';

import DocumentEditRoute from './DocumentEditRoute';
import DocumentsRoutes from './DocumentsRoutes';
import DocumentsQueryRoutes from './DocumentsQueryRoutes';
import DocumentsAnalyticsRoutes from './DocumentsAnalyticsRoutes';
import TestStudioRoutes from './TestStudioRoutes';
import AgentChatRoutes from './AgentChatRoutes';
import QuickStartWidget from '../components/agent-chat/QuickStartWidget';
import FeaturesRoutes from './FeaturesRoutes';
import InboxRoutes from './InboxRoutes';
import ReviewQueuesRoutes from './ReviewQueuesRoutes';
import MyWorkRoutes from './MyWorkRoutes';
import WelcomePage from '../pages/WelcomePage';
import { NoRoleAssigned, SessionError } from './SessionStates';

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
  INBOX_PATH,
  REVIEW_QUEUES_PATH,
  MY_WORK_PATH,
  MY_TASKS_PATH,
} from './constants';

const logger = new ConsoleLogger('AuthRoutes');

interface AuthRoutesProps {
  redirectParam: string;
}

const AuthRoutes = ({ redirectParam }: AuthRoutesProps): React.JSX.Element => {
  const { currentCredentials } = useAppContext();
  const { isQueueWorker, isAnnotatorOnly, isReviewerOnly, hasNoRole, sessionError, retrySession, loading: roleLoading } = useUserRole();
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
  // An Annotator-only user, and a Reviewer with no Admin, Author or Viewer role, lands on
  // Home › Tasks: their work is reached from Home. The welcome page offers Quick Start,
  // the tour, Configuration and Upload Document — none of which they are scoped to, so
  // it would be a page of dead ends — and the Document List is not in their navigation.
  const landingPath =
    isAnnotatorOnly || isReviewerOnly
      ? MY_TASKS_PATH
      : defaultFeatureId
        ? `${FEATURES_PATH_PREFIX}/${defaultFeatureId}`
        : welcomeDismissed
          ? DEFAULT_PATH
          : WELCOME_PATH;

  // No route renders before the role is known, so no user sees another role's page flash.
  if (!settingsLoaded || roleLoading) {
    return (
      <SettingsContext.Provider value={settingsContextValue}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '50vh' }}>
          <Spinner size="large" />
        </div>
      </SettingsContext.Provider>
    );
  }

  // The caller's groups could not be read at all, which is a different thing from
  // their having none — and a much more common one: `api/auth-session.ts` records
  // a live `400 NotAuthorizedException` on a valid token, shared across every
  // consumer of the one in-flight promise. `SessionError` says what that needs
  // ("usually temporary"). It was previously unreachable from here, because
  // `Routes` only renders it when credentials are ABSENT and they exist by the
  // time this component mounts, so the failure fell through to the no-role screen
  // and sent an entitled user to an administrator with nothing to fix.
  if (sessionError) {
    return (
      <SettingsContext.Provider value={settingsContextValue}>
        <SessionError onRetry={retrySession} />
      </SettingsContext.Provider>
    );
  }

  // An account in no Cognito group is refused every document read by the API, so
  // mounting the app would hand it the Viewer navigation (see the fall-through in
  // navigation.tsx) and fail on each page in turn — which reads as a broken
  // deployment rather than an account nobody has finished setting up. Say it once
  // instead. `hasNoRole` requires a SUCCESSFUL session read and is false while one
  // is in flight, so this cannot fire on a user whose groups have not arrived yet.
  if (hasNoRole) {
    return (
      <SettingsContext.Provider value={settingsContextValue}>
        <NoRoleAssigned onSignOut={signOut} />
      </SettingsContext.Provider>
    );
  }

  const loginRoute = (
    <Route
      path={LOGIN_PATH}
      element={<Navigate to={!redirectParam || redirectParam === LOGIN_PATH ? landingPath : `${redirectParam}`} replace />}
    />
  );
  const logoutRoute = <Route path={LOGOUT_PATH} element={<Button onClick={signOut}>Sign Out</Button>} />;

  // An Annotator-only user's shell: Home (Inbox, Tasks, Queues), the queue pages and
  // Test Studio items (under /review-queues), and a document's edit page (where Inbox
  // notification links and Tasks point). Nothing else is routed, so the rest of
  // /documents is not theirs.
  const routes = isAnnotatorOnly ? (
    <Routes>
      <Route path={`${DOCUMENTS_PATH}/:objectKey/edit`} element={<DocumentEditRoute />} />
      <Route path={`${MY_WORK_PATH}/*`} element={<MyWorkRoutes />} />
      <Route path={`${INBOX_PATH}/*`} element={<InboxRoutes />} />
      <Route path={`${REVIEW_QUEUES_PATH}/*`} element={<ReviewQueuesRoutes />} />
      {loginRoute}
      {logoutRoute}
      <Route path="*" element={<Navigate to={landingPath} replace />} />
    </Routes>
  ) : (
    <Routes>
      <Route path={WELCOME_PATH} element={<WelcomePage />} />
      <Route path={`${AGENT_CHAT_PATH}/*`} element={<AgentChatRoutes />} />
      <Route path={`${FEATURES_PATH_PREFIX}/*`} element={<FeaturesRoutes />} />
      <Route path={`${DOCUMENTS_KB_QUERY_PATH}/*`} element={<DocumentsQueryRoutes />} />
      <Route path={`${DOCUMENTS_ANALYTICS_PATH}/*`} element={<DocumentsAnalyticsRoutes />} />
      <Route path={`${TEST_STUDIO_PATH}/*`} element={<TestStudioRoutes />} />
      <Route path={`${DOCUMENTS_PATH}/*`} element={<DocumentsRoutes />} />
      {/* Home › Inbox, which every user has. */}
      <Route path={`${INBOX_PATH}/*`} element={<InboxRoutes />} />
      <Route path={`${REVIEW_QUEUES_PATH}/*`} element={<ReviewQueuesRoutes />} />
      <Route path={`${MY_WORK_PATH}/*`} element={<MyWorkRoutes />} />
      {loginRoute}
      {logoutRoute}
      <Route path="*" element={<Navigate to={landingPath} replace />} />
    </Routes>
  );

  const shell = (
    <>
      {routes}
      {!isAnnotatorOnly && <QuickStartWidget />}
    </>
  );

  return (
    <SettingsContext.Provider value={settingsContextValue}>
      {/* Every user has an Inbox, so it is polled for every signed-in user. */}
      <InboxProvider>
        {/* Home › Tasks and its badge: the items assigned to an Annotator. */}
        {isQueueWorker ? <ReviewWorkProvider>{shell}</ReviewWorkProvider> : shell}
      </InboxProvider>
    </SettingsContext.Provider>
  );
};

export default AuthRoutes;
