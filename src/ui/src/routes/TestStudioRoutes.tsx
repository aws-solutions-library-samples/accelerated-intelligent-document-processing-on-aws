// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { Routes, Route, useParams } from 'react-router-dom';

import TestStudioLayout from '../components/test-studio/TestStudioLayout';
import TestSetDetail from '../components/test-studio/TestSetDetail';
import TestSetDocumentDetail from '../components/test-studio/TestSetDocumentDetail';
import AnnotationWorkspace from '../components/test-studio/AnnotationWorkspace';
import AnnotationQueueLanding from '../components/test-studio/AnnotationQueueLanding';
import GenAIIDPTopNavigation from '../components/genai-idp-top-navigation';

/**
 * Keyed on the set id so moving between sets remounts the workspace instead of
 * reusing it. Its per-set state would otherwise carry over — above all the opened
 * version transition: starting one on set A and navigating in-app to set B showed
 * B with A's `v1 → v2` badge, a `?v=2` link and an unlocked editor, while B had no
 * draft and no snapshot. That is the silent commitment the transition exists to
 * prevent, reachable through the ordinary path of an annotator working two sets.
 * A remount also drops A's queue, so B's load failure cannot leave A's documents
 * on screen under B's heading.
 */
const AnnotationWorkspaceForSet = (): React.JSX.Element => {
  const { testSetId } = useParams<{ testSetId: string }>();
  return <AnnotationWorkspace key={testSetId} />;
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
      {/* The scoped annotation queue — the landing page for an Annotator, and
          reachable by an owner from the test set's detail page. Declared before
          the bare sets/:testSetId route so "annotate" isn't swallowed as an id. */}
      <Route
        path="sets/:testSetId/annotate"
        element={
          <div>
            <GenAIIDPTopNavigation />
            <AnnotationWorkspaceForSet />
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
