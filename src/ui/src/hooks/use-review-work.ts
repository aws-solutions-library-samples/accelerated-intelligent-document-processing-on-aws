// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useContext } from 'react';

import { ReviewWorkContext } from '../contexts/review-work';
import type { ReviewWorkContextValue } from '../contexts/review-work';

/** The shared My Tasks state. Throws outside ReviewWorkProvider. */
const useReviewWork = (): ReviewWorkContextValue => {
  const ctx = useContext(ReviewWorkContext);
  if (!ctx) throw new Error('useReviewWork must be used inside ReviewWorkProvider');
  return ctx;
};

/** Same, but null outside the provider (navigation for roles without review queues). */
export const useOptionalReviewWork = (): ReviewWorkContextValue | null => useContext(ReviewWorkContext);

export default useReviewWork;
