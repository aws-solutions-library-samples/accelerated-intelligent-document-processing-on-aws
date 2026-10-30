// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Where the edit screen goes once the work is finished, skipped or cancelled: from a
// queue item, an Annotator returns to Home › Tasks and an Admin (or an Author on a test
// set) to the queue the item belongs to.
import { useCallback } from 'react';
import { useNavigate } from 'react-router-dom';

import useAppContext from '../../contexts/app';
import { useOptionalReviewWork } from '../../hooks/use-review-work';
import useUserRole from '../../hooks/use-user-role';
import { MY_TASKS_PATH } from '../../routes/constants';
import type { EditExit } from './edit-model';

/** Returns `leavePath(queuePath)`: where a queue item's edit screen leaves to for the caller. */
export const useLeavePath = (): ((queuePath: string) => string) => {
  const { isAdmin, isAnnotator } = useUserRole();
  return useCallback((queuePath: string) => (isAnnotator && !isAdmin ? MY_TASKS_PATH : queuePath), [isAdmin, isAnnotator]);
};

/**
 * Returns `exit({ to, message, workItemId })`: show the message, drop the finished item
 * from Home › Tasks when it is one (so the Tasks badge updates now, not on a later poll),
 * refresh Tasks and navigate to `to`, already resolved for the caller (useLeavePath).
 */
const useEditExit = (): ((exit: EditExit) => void) => {
  const navigate = useNavigate();
  const { setSuccessMessage } = useAppContext();
  const reviewWork = useOptionalReviewWork();

  return useCallback(
    ({ to, message, workItemId }: EditExit) => {
      setSuccessMessage(message);
      if (workItemId) reviewWork?.settle(workItemId);
      void reviewWork?.refresh();
      navigate(to);
    },
    [navigate, reviewWork, setSuccessMessage],
  );
};

export default useEditExit;
