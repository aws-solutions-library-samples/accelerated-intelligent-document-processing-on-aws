// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useEffect, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { ConsoleLogger } from 'aws-amplify/utils';
import { useAuthenticator } from '@aws-amplify/ui-react';
import { Alert, Box, Button, SpaceBetween, Spinner } from '@cloudscape-design/components';

import UnauthRoutes from './UnauthRoutes';

import useAppContext from '../contexts/app';
import AuthRoutes from './AuthRoutes';

import { REDIRECT_URL_PARAM } from './constants';

const logger = new ConsoleLogger('Routes');

/** Signed in, credentials still arriving. Brief, and better than a blank page. */
const SessionLoading = (): React.JSX.Element => (
  <Box padding="xxl" textAlign="center">
    <SpaceBetween size="s" alignItems="center">
      <Spinner size="large" />
      <Box variant="p" color="text-body-secondary">
        Establishing your session…
      </Box>
    </SpaceBetween>
  </Box>
);

/**
 * Credentials could not be obtained despite a valid sign-in.
 *
 * Always offers a way out. The failure this replaces was recoverable by a reload
 * the whole time — the user just had no way to know that.
 */
const SessionError = ({ onRetry }: { onRetry?: () => void }): React.JSX.Element => (
  <Box padding="xxl">
    <Alert
      type="error"
      header="Could not establish your session"
      action={
        <SpaceBetween direction="horizontal" size="xs">
          {onRetry && <Button onClick={onRetry}>Retry</Button>}
          <Button onClick={() => window.location.reload()}>Reload the page</Button>
        </SpaceBetween>
      }
    >
      You are signed in, but the app could not obtain AWS credentials for your session. This is usually temporary — retrying or reloading
      normally resolves it. If it persists, sign out and sign in again.
    </Alert>
  </Box>
);

const Routes = (): React.JSX.Element => {
  const { user, currentCredentials, credentialsStatus, retryCredentials } = useAppContext();
  const { authStatus } = useAuthenticator((context) => [context.authStatus]);
  const location = useLocation();
  const [urlSearchParams, setUrlSearchParams] = useState(new URLSearchParams({}));
  const [redirectParam, setRedirectParam] = useState('');

  useEffect(() => {
    if (!location?.search) {
      return;
    }
    const searchParams = new URLSearchParams(location.search);
    logger.debug('searchParams:', searchParams);
    setUrlSearchParams(searchParams);
  }, [location]);

  useEffect(() => {
    const redirect = urlSearchParams?.get(REDIRECT_URL_PARAM);
    if (!redirect) {
      return;
    }
    logger.debug('redirect:', redirect);
    setRedirectParam(redirect);
  }, [urlSearchParams]);

  // Authenticated but without credentials yet is NOT the same as unauthenticated,
  // and treating it as such is what produced a blank page after a valid sign-in:
  // UnauthRoutes sends /login to Amplify's <Authenticator>, which renders its
  // children once authStatus is 'authenticated' — and it is given none, so it
  // renders nothing at all. Authenticated so no sign-in form, no credentials so
  // no app: an empty shell with no way out but a reload nobody suggested.
  const authenticatedWithoutCredentials = authStatus === 'authenticated' && user && !currentCredentials;

  if (authenticatedWithoutCredentials) {
    return credentialsStatus === 'error' ? <SessionError onRetry={retryCredentials} /> : <SessionLoading />;
  }

  return !(authStatus === 'authenticated' && user && currentCredentials) ? (
    <UnauthRoutes location={location} />
  ) : (
    <AuthRoutes redirectParam={redirectParam} />
  );
};

export default Routes;
