// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// /review-queues: the annotation screens for one item (Admin and Annotator; Inbox
// notification links point here) and HITL › Queue Management (Admin only).
import React from 'react';
import { Navigate, Route, Routes, useParams } from 'react-router-dom';
import type { BreadcrumbGroupProps } from '@cloudscape-design/components';

import GenAIIDPTopNavigation from '../components/genai-idp-top-navigation';
import ReviewQueuesLayout from '../components/review-queues/ReviewQueuesLayout';
import QueuesPage from '../components/review-queues/QueuesPage';
import ReviewQueuePage from '../components/review-queues/ReviewQueuePage';
import { documentName } from '../components/review-queues/review-api';
import AnnotationScreen from '../components/review-workspace/AnnotationScreen';
import AnnotationWorkspace from '../components/test-studio/AnnotationWorkspace';
import useUserRole from '../hooks/use-user-role';
import RequireRole from './RequireRole';
import { MY_TASKS_PATH, REVIEW_QUEUES_PATH, productionQueuePath, testSetQueuePath } from './constants';

const MY_WORK_ROOT: BreadcrumbGroupProps.Item = { text: 'My Work', href: `#${MY_TASKS_PATH}` };

/**
 * An Annotator reaches the annotation screens from My Work › Tasks, an Admin from
 * Queue Management, so the breadcrumbs follow the caller's role.
 */
const useItemCrumbs = (queueCrumb: BreadcrumbGroupProps.Item | null, objectKey: string) => {
  const { isAdmin } = useUserRole();
  const item = { text: documentName(decodeURIComponent(objectKey)), href: '#' };
  if (isAdmin) return { root: undefined, crumbs: queueCrumb ? [queueCrumb, item] : [item] };
  return { root: MY_WORK_ROOT, crumbs: [{ text: 'Tasks', href: `#${MY_TASKS_PATH}` }, item] };
};

const ProductionItemPage = (): React.JSX.Element => {
  const { objectKey = '' } = useParams();
  const { root, crumbs } = useItemCrumbs(null, objectKey);
  return (
    <ReviewQueuesLayout root={root} crumbs={crumbs}>
      <AnnotationScreen key={objectKey} kind="production" />
    </ReviewQueuesLayout>
  );
};

const TestSetItemPage = (): React.JSX.Element => {
  const { testSetId = '', objectKey = '' } = useParams();
  const { root, crumbs } = useItemCrumbs({ text: `Test set: ${testSetId}`, href: `#${testSetQueuePath(testSetId)}` }, objectKey);
  return (
    <ReviewQueuesLayout root={root} crumbs={crumbs}>
      <AnnotationScreen key={`${testSetId}/${objectKey}`} kind="test-set" />
    </ReviewQueuesLayout>
  );
};

/**
 * A test set's queue page (Admin). Keyed on the set so moving between sets remounts
 * the workspace: its per-set state — above all the opened version transition — must
 * never carry over to another set.
 */
const TestSetQueuePage = (): React.JSX.Element => {
  const { testSetId = '' } = useParams();
  return (
    <ReviewQueuesLayout crumbs={[{ text: `Test set: ${testSetId}`, href: '#' }]}>
      <AnnotationWorkspace key={testSetId} mode="queue" />
    </ReviewQueuesLayout>
  );
};

const ProductionQueuePage = (): React.JSX.Element => {
  const { queue = '' } = useParams();
  return (
    <ReviewQueuesLayout crumbs={[{ text: decodeURIComponent(queue), href: '#' }]}>
      <ReviewQueuePage />
    </ReviewQueuesLayout>
  );
};

/** The two-segment queues/:profile/:queue link: queues are system-wide, so the profile is dropped. */
const LegacyProductionQueueRedirect = (): React.JSX.Element => {
  const { queue = '' } = useParams();
  return <Navigate to={productionQueuePath(decodeURIComponent(queue))} replace />;
};

/** Queue Management is Admin-only; an Annotator who lands here goes to their Tasks. */
const AdminOnly = ({ children }: { children: React.ReactNode }): React.JSX.Element => {
  const { isAdmin, isAnnotator, loading } = useUserRole();
  if (!loading && !isAdmin && isAnnotator) return <Navigate to={MY_TASKS_PATH} replace />;
  return <RequireRole roles={['Admin']}>{children}</RequireRole>;
};

const QueueManagementRoutes = (): React.JSX.Element => (
  <AdminOnly>
    <Routes>
      <Route
        index
        element={
          <ReviewQueuesLayout>
            <QueuesPage />
          </ReviewQueuesLayout>
        }
      />
      <Route path="queues/:queue" element={<ProductionQueuePage />} />
      <Route path="queues/:profile/:queue" element={<LegacyProductionQueueRedirect />} />
      <Route path="test-sets/:testSetId" element={<TestSetQueuePage />} />
      {/* Queue health is part of Queue Management and each queue's page. */}
      <Route path="supervise" element={<Navigate to={REVIEW_QUEUES_PATH} replace />} />
      <Route path="*" element={<Navigate to={REVIEW_QUEUES_PATH} replace />} />
    </Routes>
  </AdminOnly>
);

const ReviewQueuesRoutes = (): React.JSX.Element => (
  <div>
    <GenAIIDPTopNavigation />
    <Routes>
      {/* Inbox notification links open these, so both paths must keep working. */}
      <Route
        path="items/:objectKey"
        element={
          <RequireRole roles={['Admin', 'Annotator']}>
            <ProductionItemPage />
          </RequireRole>
        }
      />
      <Route
        path="test-sets/:testSetId/items/:objectKey"
        element={
          <RequireRole roles={['Admin', 'Annotator']}>
            <TestSetItemPage />
          </RequireRole>
        }
      />
      {/* My Tasks moved to My Work › Tasks. */}
      <Route path="my-tasks" element={<Navigate to={MY_TASKS_PATH} replace />} />
      <Route path="*" element={<QueueManagementRoutes />} />
    </Routes>
  </div>
);

export default ReviewQueuesRoutes;
