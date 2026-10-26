// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Where the edit screen goes once a queue item is finished, skipped or cancelled: an
// Annotator returns to Home › Tasks; an Admin (or an Author on a test set) returns
// to the queue the item belongs to.
import { useCallback } from 'react';
import { useNavigate } from 'react-router-dom';

import useAppContext from '../../contexts/app';
import { useOptionalReviewWork } from '../../hooks/use-review-work';
import useUserRole from '../../hooks/use-user-role';
import { MY_TASKS_PATH } from '../../routes/constants';

/** Returns `leavePath(queuePath)`: where a queue item's edit screen leaves to for the caller. */
export const useLeavePath = (): ((queuePath: string) => string) => {
  const { isAdmin, isAnnotator } = useUserRole();
  return useCallback((queuePath: string) => (isAnnotator && !isAdmin ? MY_TASKS_PATH : queuePath), [isAdmin, isAnnotator]);
};

/**
 * Returns `exit(queuePath, message, workItemId)`: show the message, drop the finished
 * item from Home › Tasks (so the Tasks badge updates now, not on a later poll), refresh
 * Tasks and navigate. `workItemId` is the item's WorkItem.id.
 */
const useEditExit = (): ((queuePath: string, message: string, workItemId: string) => void) => {
  const navigate = useNavigate();
  const { setSuccessMessage } = useAppContext();
  const reviewWork = useOptionalReviewWork();
  const leavePath = useLeavePath();

  return useCallback(
    (queuePath: string, message: string, workItemId: string) => {
      setSuccessMessage(message);
      reviewWork?.settle(workItemId);
      void reviewWork?.refresh();
      navigate(leavePath(queuePath));
    },
    [leavePath, navigate, reviewWork, setSuccessMessage],
  );
};

export default useEditExit;
