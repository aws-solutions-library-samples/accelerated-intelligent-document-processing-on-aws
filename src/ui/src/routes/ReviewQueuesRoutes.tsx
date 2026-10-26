// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// /review-queues: the edit screen for one item (Admin and Annotator; Inbox
// notification links point here), each queue's page (Admin, and the Annotators listed
// on it, who reach it from My Work › Queues) and HITL › Queue Management (Admin only).
import React, { useCallback, useState } from 'react';
import { Navigate, Route, Routes, useParams } from 'react-router-dom';
import type { BreadcrumbGroupProps } from '@cloudscape-design/components';

import GenAIIDPTopNavigation from '../components/genai-idp-top-navigation';
import ReviewQueuesLayout from '../components/review-queues/ReviewQueuesLayout';
import QueuesPage from '../components/review-queues/QueuesPage';
import ReviewQueuePage from '../components/review-queues/ReviewQueuePage';
import { documentName } from '../components/review-queues/review-api';
import ProductionItemEdit from '../components/edit-screen/ProductionItemEdit';
import type { ItemQueue } from '../components/edit-screen/ProductionItemEdit';
import TestSetItemEdit from '../components/edit-screen/TestSetItemEdit';
import type { ItemTestSet } from '../components/edit-screen/TestSetItemEdit';
import AnnotationWorkspace from '../components/test-studio/AnnotationWorkspace';
import useUserRole from '../hooks/use-user-role';
import RequireRole from './RequireRole';
import { MY_QUEUES_PATH, MY_TASKS_PATH, REVIEW_QUEUES_PATH, productionQueuePath, testSetQueuePath } from './constants';

const MY_WORK_ROOT: BreadcrumbGroupProps.Item = { text: 'My Work', href: `#${MY_TASKS_PATH}` };
const MY_QUEUES_CRUMB: BreadcrumbGroupProps.Item = { text: 'Queues', href: `#${MY_QUEUES_PATH}` };

/** A queue's page: Queue Management › <queue> for an Admin, My Work › Queues › <queue> for an Annotator. */
const useQueueCrumbs = (queueCrumb: BreadcrumbGroupProps.Item) => {
  const { isAdmin } = useUserRole();
  if (isAdmin) return { root: undefined, crumbs: [queueCrumb] };
  return { root: MY_WORK_ROOT, crumbs: [MY_QUEUES_CRUMB, queueCrumb] };
};

/**
 * An item's breadcrumb has the same shape for every role: Home › <queue or test set> ›
 * <document>, the queue crumb linking to that queue's page. Until the queue is known (a
 * production item's loads with it) the item sits under Queue Management for an Admin
 * and under My Work for an Annotator.
 */
const useItemCrumbs = (queueCrumb: BreadcrumbGroupProps.Item | null, objectKey: string) => {
  const { isAdmin } = useUserRole();
  const item = { text: documentName(decodeURIComponent(objectKey)), href: '#' };
  return { root: queueCrumb ?? (isAdmin ? undefined : MY_WORK_ROOT), crumbs: [item] };
};

export const ProductionItemPage = (): React.JSX.Element => {
  const { objectKey = '' } = useParams();
  // Keyed on the item, so moving to another item never shows the previous one's queue.
  const [loaded, setLoaded] = useState<{ objectKey: string; queue: ItemQueue } | null>(null);
  const onItemQueue = useCallback((queue: ItemQueue) => setLoaded({ objectKey, queue }), [objectKey]);
  const queue = loaded?.objectKey === objectKey ? loaded.queue : null;
  const { root, crumbs } = useItemCrumbs(queue && { text: queue.name, href: `#${productionQueuePath(queue.id)}` }, objectKey);
  return (
    <ReviewQueuesLayout root={root} crumbs={crumbs}>
      {/* Keyed on the item, so per-item state (the sections saved so far) never carries over. */}
      <ProductionItemEdit key={objectKey} objectKey={decodeURIComponent(objectKey)} onItemQueue={onItemQueue} />
    </ReviewQueuesLayout>
  );
};

export const TestSetItemPage = (): React.JSX.Element => {
  const { testSetId = '', objectKey = '' } = useParams();
  // The set's name arrives with its queue; its id stands in until then.
  const [loaded, setLoaded] = useState<ItemTestSet | null>(null);
  const onTestSet = useCallback((testSet: ItemTestSet) => setLoaded(testSet), []);
  const name = loaded?.id === testSetId ? loaded.name : null;
  const { root, crumbs } = useItemCrumbs({ text: name ?? testSetId, href: `#${testSetQueuePath(testSetId)}` }, objectKey);
  return (
    <ReviewQueuesLayout root={root} crumbs={crumbs}>
      {/* Keyed on the item, so per-item state (the opened version transition) never carries over. */}
      <TestSetItemEdit
        key={`${testSetId}/${objectKey}`}
        testSetId={testSetId}
        objectKey={decodeURIComponent(objectKey)}
        onTestSet={onTestSet}
      />
    </ReviewQueuesLayout>
  );
};

/**
 * A test set's queue page (Admin, and the Annotators listed on it). Keyed on the set
 * so moving between sets remounts the workspace: its per-set state — above all the
 * opened version transition — must never carry over to another set.
 */
const TestSetQueuePage = (): React.JSX.Element => {
  const { testSetId = '' } = useParams();
  const { root, crumbs } = useQueueCrumbs({ text: `Test set: ${testSetId}`, href: '#' });
  return (
    <ReviewQueuesLayout root={root} crumbs={crumbs}>
      <AnnotationWorkspace key={testSetId} />
    </ReviewQueuesLayout>
  );
};

const ProductionQueuePage = (): React.JSX.Element => {
  const { queue = '' } = useParams();
  const { root, crumbs } = useQueueCrumbs({ text: decodeURIComponent(queue), href: '#' });
  return (
    <ReviewQueuesLayout root={root} crumbs={crumbs}>
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
      {/* A queue's page: Admins, and Annotators from My Work › Queues (the API refuses an unlisted queue). */}
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
          <RequireRole roles={['Admin', 'Annotator']}>
            <TestSetQueuePage />
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
