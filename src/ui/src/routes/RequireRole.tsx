// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Hides a page from users without one of `roles`. Not a security boundary: the API
// enforces access; this only avoids rendering a page of refusals.
import React from 'react';
import { Alert, Box, Link, Spinner } from '@cloudscape-design/components';

import useUserRole from '../hooks/use-user-role';

interface RequireRoleProps {
  roles: readonly string[];
  children: React.ReactNode;
}

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
    return (
      <Box padding="l">
        <Alert type="warning" header="You don't have access to this page">
          Your role doesn&apos;t include this page. <Link href="#/">Go to your home page</Link>
        </Alert>
      </Box>
    );
  }
  return <>{children}</>;
};

export default RequireRole;
