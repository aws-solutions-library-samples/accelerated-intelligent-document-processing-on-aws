// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// /review-queues: the edit page for one Test Studio document and a test set's queue
// page (Admin, Annotator, and Author, who edits authored ground truth there), each
// Production queue's page (Admin, and the Annotators listed on it, who reach it from Home › Queues)
// and HITL › Queue Management (Admin only). A Production item's former route,
// items/:objectKey, redirects to the document's edit page (/documents/:objectKey/edit),
// which opens the item while the document is in review.
import React, { useCallback, useState } from 'react';
import { Navigate, Route, Routes, useLocation, useParams } from 'react-router-dom';
import type { BreadcrumbGroupProps } from '@cloudscape-design/components';

import GenAIIDPTopNavigation from '../components/genai-idp-top-navigation';
import ReviewQueuesLayout from '../components/review-queues/ReviewQueuesLayout';
import QueuesPage from '../components/review-queues/QueuesPage';
import ReviewQueuePage from '../components/review-queues/ReviewQueuePage';
import TestSetItemEdit from '../components/edit-screen/TestSetItemEdit';
import AnnotationWorkspace from '../components/test-studio/AnnotationWorkspace';
import { QueueManagementToolsPanel, QueueToolsPanel } from '../components/review-queues/tools-panel';
import useUserRole from '../hooks/use-user-role';
import RequireRole from './RequireRole';
import { MY_QUEUES_PATH, MY_TASKS_PATH, REVIEW_QUEUES_PATH, documentEditPath, productionQueuePath } from './constants';

const MY_QUEUES_CRUMB: BreadcrumbGroupProps.Item = { text: 'Queues', href: `#${MY_QUEUES_PATH}` };

/** A queue's page: Queue Management › <queue> for an Admin, Queues › <queue> for an Annotator (both under Home). */
const useQueueCrumbs = (queueCrumb: BreadcrumbGroupProps.Item) => {
  const { isAdmin } = useUserRole();
  if (isAdmin) return { root: undefined, crumbs: [queueCrumb] };
  return { root: MY_QUEUES_CRUMB, crumbs: [queueCrumb] };
};

/**
 * A Production item's former route, which Inbox messages (kept up to a year) and copied
 * field links still point to: permanently the document's edit page, with the whole query
 * (`?field=`, `?view=`, `?section=`). The key is decoded once by the router and encoded
 * once here. No role gate: the edit page decides what the caller sees.
 */
export const ReviewItemRedirect = (): React.JSX.Element => {
  const { objectKey = '' } = useParams();
  const { search } = useLocation();
  return <Navigate replace to={`${documentEditPath(objectKey)}${search}`} />;
};

export const TestSetItemPage = (): React.JSX.Element => {
  // The router has already decoded the params once.
  const { testSetId = '', objectKey = '' } = useParams();
  // Keyed on the item, so per-item state (the opened version transition) never carries over.
  return <TestSetItemEdit key={`${testSetId}/${objectKey}`} testSetId={testSetId} objectKey={objectKey} />;
};

/**
 * A test set's queue page (Admin, and the Annotators listed on it). Keyed on the set
 * so moving between sets remounts the workspace: its per-set state — above all the
 * opened version transition — must never carry over to another set.
 */
const TestSetQueuePage = (): React.JSX.Element => {
  const { testSetId = '' } = useParams();
  // The set's name arrives with its queue, keyed on the set; its id stands in until then.
  const [loaded, setLoaded] = useState<{ id: string; name: string } | null>(null);
  const onName = useCallback((name: string) => setLoaded({ id: testSetId, name }), [testSetId]);
  const name = loaded?.id === testSetId ? loaded.name : testSetId;
  const { root, crumbs } = useQueueCrumbs({ text: `Test set: ${name}`, href: '#' });
  return (
    <ReviewQueuesLayout root={root} crumbs={crumbs} tools={<QueueToolsPanel />}>
      <AnnotationWorkspace key={testSetId} onName={onName} />
    </ReviewQueuesLayout>
  );
};

const ProductionQueuePage = (): React.JSX.Element => {
  const { queue = '' } = useParams();
  // The queue's name, as loaded or as just saved, keyed on the queue; its id stands in until then.
  const [loaded, setLoaded] = useState<{ id: string; name: string } | null>(null);
  const onName = useCallback((name: string) => setLoaded({ id: queue, name }), [queue]);
  const name = loaded?.id === queue ? loaded.name : queue;
  const { root, crumbs } = useQueueCrumbs({ text: name, href: '#' });
  return (
    <ReviewQueuesLayout root={root} crumbs={crumbs} tools={<QueueToolsPanel />}>
      <ReviewQueuePage onName={onName} />
    </ReviewQueuesLayout>
  );
};

/** The two-segment queues/:profile/:queue link: queues are system-wide, so the profile is dropped. */
const LegacyProductionQueueRedirect = (): React.JSX.Element => {
  const { queue = '' } = useParams();
  return <Navigate to={productionQueuePath(queue)} replace />;
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
          <ReviewQueuesLayout tools={<QueueManagementToolsPanel />}>
            <QueuesPage />
          </ReviewQueuesLayout>
        }
      />
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
      <Route path="items/:objectKey" element={<ReviewItemRedirect />} />
      <Route
        path="test-sets/:testSetId/items/:objectKey"
        element={
          <RequireRole roles={['Admin', 'Annotator', 'Author']}>
            <TestSetItemPage />
          </RequireRole>
        }
      />
      {/* A queue's page: Admins, and Annotators from Home › Queues (the API refuses an unlisted queue). */}
      <Route
        path="queues/:queue"
        element={
          <RequireRole roles={['Admin', 'Annotator']}>
            <ProductionQueuePage />
          </RequireRole>
        }
      />
      <Route
        path="queues/:profile/:queue"
        element={
          <RequireRole roles={['Admin', 'Annotator']}>
            <LegacyProductionQueueRedirect />
          </RequireRole>
        }
      />
      <Route
        path="test-sets/:testSetId"
        element={
          <RequireRole roles={['Admin', 'Annotator', 'Author']}>
            <TestSetQueuePage />
          </RequireRole>
        }
      />
      {/* My Tasks moved to Home › Tasks. */}
      <Route path="my-tasks" element={<Navigate to={MY_TASKS_PATH} replace />} />
      <Route path="*" element={<QueueManagementRoutes />} />
    </Routes>
  </div>
);

export default ReviewQueuesRoutes;
