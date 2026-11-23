// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Hides a page from users without one of `roles`. Not a security boundary: the API
// enforces access; this only avoids rendering a page of refusals.
import React from 'react';
import { Alert, Box, Link, Spinner } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';

import useUserRole from '../hooks/use-user-role';

const messages = defineMessages<{ header: NoMessageValues; body: NoMessageValues; homeLink: NoMessageValues }>({
  header: {
    id: 'routes.requireRole.header',
    defaultMessage: "You don't have access to this page",
    description: 'Header of the notice shown in place of a page the user’s role does not include.',
  },
  body: {
    id: 'routes.requireRole.body',
    defaultMessage: "Your role doesn't include this page.",
    description: 'Body of the notice shown in place of a page the user’s role does not include; followed by the link to the home page.',
  },
  homeLink: {
    id: 'routes.requireRole.homeLink',
    defaultMessage: 'Go to your home page',
    description: 'Link in the notice shown in place of a page the user’s role does not include; opens the user’s home page.',
  },
});

interface RequireRoleProps {
  roles: readonly string[];
  children: React.ReactNode;
}

/** The notice of the denied branch. Its own component, so only that branch reads the intl context. */
const NoAccess = (): React.JSX.Element => {
  const intl = useIntl();
  return (
    <Box padding="l">
      <Alert type="warning" header={intl.formatMessage(messages.header)}>
        <FormattedMessage {...messages.body} />{' '}
        <Link href="#/">
          <FormattedMessage {...messages.homeLink} />
        </Link>
      </Alert>
    </Box>
  );
};

const RequireRole = ({ roles, children }: RequireRoleProps): React.JSX.Element => {
  const { groups, loading } = useUserRole();

  if (loading) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" />
      </Box>
    );
  }
  if (!groups.some((g) => roles.includes(g))) {
    return <NoAccess />;
  }
  return <>{children}</>;
};

export default RequireRole;
