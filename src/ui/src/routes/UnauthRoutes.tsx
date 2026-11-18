// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';

import { Authenticator } from '@aws-amplify/ui-react';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';

import { SessionLoading } from './SessionStates';
import { signInWithRedirect } from 'aws-amplify/auth';
import Button from '@cloudscape-design/components/button';

import { LOGIN_PATH, LOGOUT_PATH, REDIRECT_URL_PARAM } from './constants';

/** The app's own sign-in strings; the form itself is Amplify's, with Amplify's own dictionaries. */
const messages = defineMessages<{
  header: NoMessageValues;
  federatedSignIn: { readonly provider: string };
  orUsername: NoMessageValues;
  redirecting: { readonly provider: string };
  emailRequired: NoMessageValues;
}>({
  header: {
    id: 'routes.unauth.header',
    defaultMessage: 'Welcome to GenAI Intelligent Document Processing!',
    description:
      'Heading above the sign-in form. "GenAI Intelligent Document Processing" is the product name and may stay as it is in the translation.',
  },
  federatedSignIn: {
    id: 'routes.unauth.federatedSignIn',
    defaultMessage: 'Sign in with {provider}',
    description: 'Button above the sign-in form that signs in through the external identity provider. {provider} is its configured name.',
  },
  orUsername: {
    id: 'routes.unauth.orUsername',
    defaultMessage: '— or sign in with username —',
    description: 'Separator between the external identity provider button and the username and password form below it.',
  },
  redirecting: {
    id: 'routes.unauth.redirecting',
    defaultMessage: 'Redirecting to {provider}...',
    description:
      'Text shown instead of the sign-in form while the browser is sent to the external identity provider. {provider} is its configured name.',
  },
  emailRequired: {
    id: 'routes.unauth.emailRequired',
    defaultMessage: 'Email is required',
    description: 'Validation error of the sign-up form when the e-mail field is left empty.',
  },
});

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
  <h1 style={{ textAlign: 'center', margin: '2rem 0' }}>
    <FormattedMessage {...messages.header} />
  </h1>
);

const FederatedSignInButton = (): React.JSX.Element | null => {
  if (!VITE_EXTERNAL_IDP_NAME) return null;

  return (
    <div style={{ textAlign: 'center', margin: '1rem 0' }}>
      <Button variant="primary" fullWidth onClick={() => signInWithRedirect({ provider: { custom: VITE_EXTERNAL_IDP_NAME } })}>
        <FormattedMessage {...messages.federatedSignIn} values={{ provider: VITE_EXTERNAL_IDP_NAME }} />
      </Button>
      <div style={{ margin: '1rem 0', color: '#666', fontSize: '0.875rem' }}>
        <FormattedMessage {...messages.orUsername} />
      </div>
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
  const intl = useIntl();
  // Check if we returned from a failed federated login attempt or user explicitly signed out
  const hasError = window.location.href.includes('error=') || window.location.href.includes('errorMessage=');
  const userSignedOut = sessionStorage.getItem('idp_signed_out') === 'true';

  // Auto-redirect to external IdP if configured, not already attempted, no error, and user didn't just sign out
  if (VITE_EXTERNAL_IDP_NAME && VITE_EXTERNAL_IDP_AUTO_LOGIN === 'true' && !getAutoLoginAttempted() && !hasError && !userSignedOut) {
    setAutoLoginAttempted();
    signInWithRedirect({ provider: { custom: VITE_EXTERNAL_IDP_NAME } });
    return (
      <div style={{ textAlign: 'center', margin: '4rem 0' }}>
        {intl.formatMessage(messages.redirecting, { provider: VITE_EXTERNAL_IDP_NAME })}
      </div>
    );
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
            email: intl.formatMessage(messages.emailRequired),
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

/**
 * The `?redirect=` query that sends a signed-out visitor back where they were.
 *
 * The value is encoded, because it is read back with `URLSearchParams.get`, which
 * decodes it once. Interpolated raw, a document id's `%2F` came back as `/`, so the
 * route no longer matched and the page rendered blank; a `&` in the original query
 * also cut the value short.
 */
export const redirectSearch = (location: { pathname: string; search: string }): string =>
  `?${new URLSearchParams({ [REDIRECT_URL_PARAM]: `${location.pathname}${location.search}` }).toString()}`;

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
            search: redirectSearch(location),
          }}
          replace
        />
      }
    />
  </Routes>
);

export default UnauthRoutes;
