// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// My Work (Annotators): the caller's Tasks (items assigned to them) and Favorites.
// Inbox, the third page of the section, keeps its own route.
import React from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';

import GenAIIDPTopNavigation from '../components/genai-idp-top-navigation';
import ReviewQueuesLayout from '../components/review-queues/ReviewQueuesLayout';
import MyTasksPage from '../components/review-queues/MyTasksPage';
import FavoritesPage from '../components/review-queues/FavoritesPage';
import RequireRole from './RequireRole';
import { FAVORITES_PATH, MY_TASKS_PATH } from './constants';

const ROOT = { text: 'My Work', href: `#${MY_TASKS_PATH}` };

const MyWorkRoutes = (): React.JSX.Element => (
  <div>
    <GenAIIDPTopNavigation />
    <RequireRole roles={['Annotator']}>
      <Routes>
        <Route
          path="tasks"
          element={
            <ReviewQueuesLayout root={ROOT} crumbs={[{ text: 'Tasks', href: `#${MY_TASKS_PATH}` }]}>
              <MyTasksPage />
            </ReviewQueuesLayout>
          }
        />
        <Route
          path="favorites"
          element={
            <ReviewQueuesLayout root={ROOT} crumbs={[{ text: 'Favorites', href: `#${FAVORITES_PATH}` }]}>
              <FavoritesPage />
            </ReviewQueuesLayout>
          }
        />
        <Route path="*" element={<Navigate to={MY_TASKS_PATH} replace />} />
      </Routes>
    </RequireRole>
  </div>
);

export default MyWorkRoutes;
