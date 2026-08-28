// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';

import { Authenticator } from '@aws-amplify/ui-react';

import { SessionLoading } from './SessionStates';
import { signInWithRedirect } from 'aws-amplify/auth';
import Button from '@cloudscape-design/components/button';

import { LOGIN_PATH, LOGOUT_PATH, REDIRECT_URL_PARAM } from './constants';

// this is set at build time depending on the AllowedSignUpEmailDomain CloudFormation parameter
const VITE_SHOULD_HIDE_SIGN_UP = import.meta.env.VITE_SHOULD_HIDE_SIGN_UP ?? 'true';
const VITE_EXTERNAL_IDP_NAME = import.meta.env.VITE_EXTERNAL_IDP_NAME ?? '';
const VITE_EXTERNAL_IDP_AUTO_LOGIN = import.meta.env.VITE_EXTERNAL_IDP_AUTO_LOGIN ?? 'false';

// Track whether auto-login redirect has been attempted to prevent loops.
// Uses sessionStorage (not a module-level variable) so it survives page refreshes
// and is consistent with the idp_signed_out flag pattern.
const getAutoLoginAttempted = () => sessionStorage.getItem('idp_auto_login_attempted') === 'true';
const setAutoLoginAttempted = () => sessionStorage.setItem('idp_auto_login_attempted', 'true');

const AuthHeader = (): React.JSX.Element => (
  <h1 style={{ textAlign: 'center', margin: '2rem 0' }}>Welcome to GenAI Intelligent Document Processing!</h1>
);

const FederatedSignInButton = (): React.JSX.Element | null => {
  if (!VITE_EXTERNAL_IDP_NAME) return null;

  return (
    <div style={{ textAlign: 'center', margin: '1rem 0' }}>
      <Button variant="primary" fullWidth onClick={() => signInWithRedirect({ provider: { custom: VITE_EXTERNAL_IDP_NAME } })}>
        Sign in with {VITE_EXTERNAL_IDP_NAME}
      </Button>
      <div style={{ margin: '1rem 0', color: '#666', fontSize: '0.875rem' }}>— or sign in with username —</div>
    </div>
  );
};

interface UnauthRoutesProps {
  location: {
    pathname: string;
    search: string;
  };
}

const AutoLoginOrAuthenticator = (): React.JSX.Element => {
  // Check if we returned from a failed federated login attempt or user explicitly signed out
  const hasError = window.location.href.includes('error=') || window.location.href.includes('errorMessage=');
  const userSignedOut = sessionStorage.getItem('idp_signed_out') === 'true';

  // Auto-redirect to external IdP if configured, not already attempted, no error, and user didn't just sign out
  if (VITE_EXTERNAL_IDP_NAME && VITE_EXTERNAL_IDP_AUTO_LOGIN === 'true' && !getAutoLoginAttempted() && !hasError && !userSignedOut) {
    setAutoLoginAttempted();
    signInWithRedirect({ provider: { custom: VITE_EXTERNAL_IDP_NAME } });
    return <div style={{ textAlign: 'center', margin: '4rem 0' }}>Redirecting to {VITE_EXTERNAL_IDP_NAME}...</div>;
  }

  // Clear the signed-out flag once we show the login form (user can manually click the IdP button)
  if (userSignedOut) {
    sessionStorage.removeItem('idp_signed_out');
  }

  return (
    <Authenticator
      initialState="signIn"
      components={{
        Header: () => (
          <>
            <AuthHeader />
            <FederatedSignInButton />
          </>
        ),
      }}
      services={{
        async validateCustomSignUp(formData) {
          if (formData.email) {
            return undefined;
          }
          return {
            email: 'Email is required',
          };
        },
      }}
      signUpAttributes={['email']}
      hideSignUp={VITE_SHOULD_HIDE_SIGN_UP === 'true'}
    >
      {/* Amplify renders these children once authStatus is 'authenticated', so an
          <Authenticator /> with NO children renders nothing at all in exactly
          that state — the mechanism behind the blank page after a valid sign-in.
          Reaching here while already authenticated means the app is mounting
          (e.g. `user` has not been populated yet on a reload with a live
          session), so show that rather than an empty page. */}
      {() => <SessionLoading />}
    </Authenticator>
  );
};

const UnauthRoutes = ({ location }: UnauthRoutesProps): React.JSX.Element => (
  <Routes>
    <Route path={LOGIN_PATH} element={<AutoLoginOrAuthenticator />} />
    <Route path={LOGOUT_PATH} element={<Navigate to={LOGIN_PATH} replace />} />
    <Route
      path="*"
      element={
        <Navigate
          to={{
            pathname: LOGIN_PATH,
            search: `?${REDIRECT_URL_PARAM}=${location.pathname}${location.search}`,
          }}
          replace
        />
      }
    />
  </Routes>
);

export default UnauthRoutes;
