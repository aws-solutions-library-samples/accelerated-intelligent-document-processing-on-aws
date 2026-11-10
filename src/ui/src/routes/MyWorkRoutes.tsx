// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Home › Tasks and Home › Queues (Annotators): the caller's Tasks (items assigned to
// them) and Queues (the queues that list them). Home › Jobs (Admins and Reviewers):
// bulk Skip All Reviews runs. Home › Inbox, which every user has, keeps its own route.
import React from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';

import GenAIIDPTopNavigation from '../components/genai-idp-top-navigation';
import ReviewQueuesLayout from '../components/review-queues/ReviewQueuesLayout';
import MyTasksPage from '../components/review-queues/MyTasksPage';
import MyQueuesPage from '../components/review-queues/MyQueuesPage';
import JobsPage from '../components/review-bulk/JobsPage';
import JobDetailPage from '../components/review-bulk/JobDetailPage';
import { JobToolsPanel, JobsToolsPanel, MyQueuesToolsPanel, TasksToolsPanel } from '../components/home/home-tools-panels';
import RequireRole from './RequireRole';
import { HOME_CRUMB, JOBS_CRUMB, MY_QUEUES_CRUMB, TASKS_CRUMB } from '../components/review-queues/breadcrumbs';
import { MY_TASKS_PATH } from './constants';

const JobsRoutes = (): React.JSX.Element => (
  <RequireRole roles={['Admin', 'Reviewer']}>
    <Routes>
      <Route
        index
        element={
          <ReviewQueuesLayout trail={[HOME_CRUMB, JOBS_CRUMB]} tools={<JobsToolsPanel />}>
            <JobsPage />
          </ReviewQueuesLayout>
        }
      />
      <Route
        path=":jobId"
        element={
          <ReviewQueuesLayout trail={[HOME_CRUMB, JOBS_CRUMB, { text: 'Job', href: '#' }]} tools={<JobToolsPanel />}>
            <JobDetailPage />
          </ReviewQueuesLayout>
        }
      />
    </Routes>
  </RequireRole>
);

const MyWorkRoutes = (): React.JSX.Element => (
  <div>
    <GenAIIDPTopNavigation />
    <Routes>
      <Route path="jobs/*" element={<JobsRoutes />} />
      <Route
        path="*"
        element={
          <RequireRole roles={['Annotator', 'Reviewer']}>
            <Routes>
              <Route
                path="tasks"
                element={
                  <ReviewQueuesLayout trail={[HOME_CRUMB, TASKS_CRUMB]} tools={<TasksToolsPanel />}>
                    <MyTasksPage />
                  </ReviewQueuesLayout>
                }
              />
              <Route
                path="queues"
                element={
                  <ReviewQueuesLayout trail={[HOME_CRUMB, MY_QUEUES_CRUMB]} tools={<MyQueuesToolsPanel />}>
                    <MyQueuesPage />
                  </ReviewQueuesLayout>
                }
              />
              <Route path="*" element={<Navigate to={MY_TASKS_PATH} replace />} />
            </Routes>
          </RequireRole>
        }
      />
    </Routes>
  </div>
);

export default MyWorkRoutes;
