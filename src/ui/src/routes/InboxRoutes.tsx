// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React from 'react';
import { Route, Routes } from 'react-router-dom';

import GenAIIDPTopNavigation from '../components/genai-idp-top-navigation';
import InboxLayout from '../components/inbox/InboxLayout';

const InboxRoutes = (): React.JSX.Element => (
  <Routes>
    <Route
      path="*"
      element={
        <div>
          <GenAIIDPTopNavigation />
          <InboxLayout />
        </div>
      }
    />
  </Routes>
);

export default InboxRoutes;
