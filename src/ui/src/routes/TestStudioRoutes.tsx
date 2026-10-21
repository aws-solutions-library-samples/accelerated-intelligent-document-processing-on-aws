// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { Navigate, Routes, Route, useParams, useSearchParams } from 'react-router-dom';

import TestStudioLayout from '../components/test-studio/TestStudioLayout';
import TestSetDetail from '../components/test-studio/TestSetDetail';
import TestSetDocumentDetail from '../components/test-studio/TestSetDocumentDetail';
import GenAIIDPTopNavigation from '../components/genai-idp-top-navigation';
import { REVIEW_QUEUES_PATH, testSetItemPath, testSetQueuePath } from './constants';

/**
 * Annotation moved to Human Review: an old `sets/<id>/annotate[?doc=...]` link opens
 * the test set's queue page, or the document's workspace when it names one.
 */
const AnnotateRedirect = (): React.JSX.Element => {
  const { testSetId = '' } = useParams<{ testSetId: string }>();
  const [searchParams] = useSearchParams();
  const doc = searchParams.get('doc');
  const rest = new URLSearchParams(searchParams);
  rest.delete('doc');
  const query = rest.toString();
  const target = doc ? testSetItemPath(testSetId, doc) : testSetQueuePath(testSetId);
  return <Navigate to={`${target}${query ? `?${query}` : ''}`} replace />;
};

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
      {/* Annotation lives under Human Review; these keep old links working.
          Declared before the bare sets/:testSetId route so "annotate" isn't
          swallowed as an id. */}
      <Route path="sets/:testSetId/annotate" element={<AnnotateRedirect />} />
      <Route path="annotate" element={<Navigate to={REVIEW_QUEUES_PATH} replace />} />
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
