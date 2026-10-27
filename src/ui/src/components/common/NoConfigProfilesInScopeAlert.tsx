// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { Alert } from '@cloudscape-design/components';

/** Shown in place of a configuration when the caller's scope holds no existing profile. */
const NoConfigProfilesInScopeAlert = (): React.JSX.Element => (
  <Alert type="info" header="No configuration profiles in your scope">
    Your account can use only the configuration profiles in its scope, and none of this deployment&apos;s profiles are in it. Ask an
    administrator to add one to your scope.
  </Alert>
);

export default NoConfigProfilesInScopeAlert;
