// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Where the annotation screen goes once an item is completed or rejected: an
// Annotator returns to My Work › Tasks; an Admin (or an Author on a test set)
// returns to the queue the item belongs to.
import { useCallback } from 'react';
import { useNavigate } from 'react-router-dom';

import useAppContext from '../../contexts/app';
import { useOptionalReviewWork } from '../../hooks/use-review-work';
import useUserRole from '../../hooks/use-user-role';
import { MY_TASKS_PATH } from '../../routes/constants';

/**
 * Returns `exit(queuePath, message, workItemId)`: show the message, drop the finished
 * item from My Work (so the Tasks badge updates now, not on a later poll), refresh
 * My Work and navigate. `workItemId` is the item's WorkItem.id.
 */
const useAnnotationExit = (): ((queuePath: string, message: string, workItemId: string) => void) => {
  const navigate = useNavigate();
  const { setSuccessMessage } = useAppContext();
  const reviewWork = useOptionalReviewWork();
  const { isAdmin, isAnnotator } = useUserRole();

  return useCallback(
    (queuePath: string, message: string, workItemId: string) => {
      setSuccessMessage(message);
      reviewWork?.settle(workItemId);
      void reviewWork?.refresh();
      navigate(isAnnotator && !isAdmin ? MY_TASKS_PATH : queuePath);
    },
    [isAdmin, isAnnotator, navigate, reviewWork, setSuccessMessage],
  );
};

export default useAnnotationExit;
