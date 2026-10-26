// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { Route, Routes } from 'react-router-dom';
import { ConsoleLogger } from 'aws-amplify/utils';

import GenAIIDPLayout from '../components/genaiidp-layout';
import GenAIIDPTopNavigation from '../components/genai-idp-top-navigation';
import DirectEditPage from '../components/review-workspace/DirectEditPage';

const logger = new ConsoleLogger('DocumentsRoutes');

const DocumentsRoutes = (): React.JSX.Element => {
  logger.info('DocumentsRoutes');

  return (
    <Routes>
      {/* The unified editor's direct edit of any document. */}
      <Route
        path=":objectKey/edit"
        element={
          <div>
            <GenAIIDPTopNavigation />
            <DirectEditPage />
          </div>
        }
      />
      <Route
        path="*"
        element={
          <div>
            <GenAIIDPTopNavigation />
            <GenAIIDPLayout />
          </div>
        }
      />
    </Routes>
  );
};

export default DocumentsRoutes;
