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
 * the words the dispatcher used. It is reached only after a SUCCESSFUL read of the
 * session — a failed read renders `SessionError` above, because "I could not find
 * out what your groups are" must not be reported as "you have none".
 *
 * The copy does not name the User Management page as the place the role comes from,
 * because on a deployment that federates sign-in the Cognito pre-token trigger maps
 * the external provider's groups into the app's and **overrides** the claim, so a
 * group assigned by hand there is removed again at the next fresh sign-in. Naming
 * "an administrator" covers both, and the second sentence says where to look.
 */
export const NoRoleAssigned = ({ onSignOut }: { onSignOut?: () => void }): React.JSX.Element => (
  <Box padding="xxl">
    <Alert
      type="info"
      statusIconAriaLabel="Info"
      header="Your account has not been granted access yet"
      action={
        <SpaceBetween direction="horizontal" size="xs">
          {onSignOut && <Button onClick={onSignOut}>Sign out</Button>}
          <Button onClick={() => window.location.reload()}>Reload the page</Button>
        </SpaceBetween>
      }
    >
      Your sign-in worked, but your account has not been given a role yet, so there is nothing for you to view. Ask an administrator to
      grant you one — Admin, Author, Reviewer, Annotator or Viewer. If your organization signs you in through its own identity provider, the
      role comes from your group membership there rather than from this application. Reload this page once it has been granted.
    </Alert>
  </Box>
);
