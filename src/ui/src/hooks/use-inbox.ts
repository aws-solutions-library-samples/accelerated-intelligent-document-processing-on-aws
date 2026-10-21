// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useContext } from 'react';

import { InboxContext } from '../contexts/inbox';
import type { InboxContextValue } from '../contexts/inbox';

/** The shared Inbox state and actions. Throws outside InboxProvider. */
const useInbox = (): InboxContextValue => {
  const ctx = useContext(InboxContext);
  if (!ctx) throw new Error('useInbox must be used inside InboxProvider');
  return ctx;
};

export default useInbox;
