// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * What to show when the user is signed in but the app cannot mount yet.
 *
 * Shared by `Routes` and `UnauthRoutes` on purpose. Amplify's `<Authenticator>`
 * renders **its children** once `authStatus === 'authenticated'`, so an
 * `<Authenticator />` with no children renders nothing at all in exactly that
 * case — which is the mechanism behind the blank page after a valid sign-in.
 * Closing the one route that reached it is not enough; giving the Authenticator
 * a child closes the class.
 *
 * `NoRoleAssigned` is the third member of that family: signed in, credentials
 * fine, but the account is in no Cognito group, so the API refuses the document
 * reads. Same principle — say what happened and offer a way forward, rather than
 * letting each page fail on its own.
 */

import React from 'react';
import { Alert, Box, Button, SpaceBetween, Spinner } from '@cloudscape-design/components';

/** Signed in, credentials still arriving. Brief, and better than a blank page. */
export const SessionLoading = (): React.JSX.Element => (
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
export const SessionError = ({ onRetry }: { onRetry?: () => void }): React.JSX.Element => (
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

/**
 * Signed in, but the account belongs to no application role.
 *
 * Self-service sign-up produces this: where `AllowedSignUpEmailDomain` is set the
 * user pool lets anyone at that domain register themselves, and the new account is
 * in no Cognito group until an administrator assigns one. The API refuses such a
 * caller every document read, so without this screen the app mounts a full
 * navigation whose every page fails — the worst reading of a permissions problem,
 * because it looks like a broken deployment rather than an unfinished account.
 *
 * It is not a security control. The server already denied the request; this only
 * explains the denial once, in the words the user needs, instead of eleven times in
 * the words the dispatcher used.
 */
export const NoRoleAssigned = ({ onSignOut }: { onSignOut?: () => void }): React.JSX.Element => (
  <Box padding="xxl">
    <Alert
      type="info"
      header="Your account has not been granted access yet"
      action={
        <SpaceBetween direction="horizontal" size="xs">
          <Button onClick={() => window.location.reload()}>Reload the page</Button>
          {onSignOut && <Button onClick={onSignOut}>Sign out</Button>}
        </SpaceBetween>
      }
    >
      Your sign-in worked, but an administrator has not assigned your account a role yet, so there is nothing you can view. Ask an
      administrator to assign you one — Admin, Author, Reviewer, Annotator or Viewer — from the User Management page. Reload this page once
      they have.
    </Alert>
  </Box>
);
