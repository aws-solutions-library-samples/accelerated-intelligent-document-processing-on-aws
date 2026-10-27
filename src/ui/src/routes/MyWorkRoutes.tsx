// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Home › Tasks and Home › Queues (Annotators): the caller's Tasks (items assigned to
// them) and Queues (the queues that list them). Home › Inbox, which every user has,
// keeps its own route.
import React from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';

import GenAIIDPTopNavigation from '../components/genai-idp-top-navigation';
import ReviewQueuesLayout from '../components/review-queues/ReviewQueuesLayout';
import MyTasksPage from '../components/review-queues/MyTasksPage';
import MyQueuesPage from '../components/review-queues/MyQueuesPage';
import RequireRole from './RequireRole';
import { MY_QUEUES_PATH, MY_TASKS_PATH } from './constants';

const TASKS = { text: 'Tasks', href: `#${MY_TASKS_PATH}` };
const QUEUES = { text: 'Queues', href: `#${MY_QUEUES_PATH}` };

const MyWorkRoutes = (): React.JSX.Element => (
  <div>
    <GenAIIDPTopNavigation />
    <RequireRole roles={['Annotator']}>
      <Routes>
        <Route
          path="tasks"
          element={
            <ReviewQueuesLayout root={TASKS}>
              <MyTasksPage />
            </ReviewQueuesLayout>
          }
        />
        <Route
          path="queues"
          element={
            <ReviewQueuesLayout root={QUEUES}>
              <MyQueuesPage />
            </ReviewQueuesLayout>
          }
        />
        <Route path="*" element={<Navigate to={MY_TASKS_PATH} replace />} />
      </Routes>
    </RequireRole>
  </div>
);

export default MyWorkRoutes;
