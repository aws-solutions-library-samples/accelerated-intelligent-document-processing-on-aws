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
