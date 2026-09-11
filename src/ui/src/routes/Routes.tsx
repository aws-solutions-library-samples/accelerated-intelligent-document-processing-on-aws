// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useEffect, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { ConsoleLogger } from 'aws-amplify/utils';
import { useAuthenticator } from '@aws-amplify/ui-react';

import UnauthRoutes from './UnauthRoutes';

import useAppContext from '../contexts/app';
import AuthRoutes from './AuthRoutes';

import { REDIRECT_URL_PARAM } from './constants';
import { SessionError, SessionLoading } from './SessionStates';

const logger = new ConsoleLogger('Routes');

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
