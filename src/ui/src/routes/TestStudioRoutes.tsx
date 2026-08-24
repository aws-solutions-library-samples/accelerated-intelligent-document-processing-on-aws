// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { Routes, Route } from 'react-router-dom';

import TestStudioLayout from '../components/test-studio/TestStudioLayout';
import TestSetDetail from '../components/test-studio/TestSetDetail';
import TestSetDocumentDetail from '../components/test-studio/TestSetDocumentDetail';
import AnnotationWorkspace from '../components/test-studio/AnnotationWorkspace';
import AnnotationQueueLanding from '../components/test-studio/AnnotationQueueLanding';
import GenAIIDPTopNavigation from '../components/genai-idp-top-navigation';

const TestStudioRoutes = (): React.JSX.Element => {
  return (
    <Routes>
      {/* objectKey may contain slashes (nested input names) — wildcard segment */}
      <Route
        path="sets/:testSetId/doc/*"
        element={
          <div>
            <GenAIIDPTopNavigation />
            <TestSetDocumentDetail />
          </div>
        }
      />
      {/* The scoped annotation queue — the landing page for an Annotator, and
          reachable by an owner from the test set's detail page. Declared before
          the bare sets/:testSetId route so "annotate" isn't swallowed as an id. */}
      <Route
        path="sets/:testSetId/annotate"
        element={
          <div>
            <GenAIIDPTopNavigation />
            <AnnotationWorkspace />
          </div>
        }
      />
      <Route
        path="annotate"
        element={
          <div>
            <GenAIIDPTopNavigation />
            <AnnotationQueueLanding />
          </div>
        }
      />
      <Route
        path="sets/:testSetId"
        element={
          <div>
            <GenAIIDPTopNavigation />
            <TestSetDetail />
          </div>
        }
      />
      <Route
        path="*"
        element={
          <div>
            <GenAIIDPTopNavigation />
            <TestStudioLayout />
          </div>
        }
      />
    </Routes>
  );
};

export default TestStudioRoutes;
