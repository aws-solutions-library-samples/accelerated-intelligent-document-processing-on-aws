// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The common row action of a HITL work table (the "⋮" menu): Annotate, which opens
// the item's annotation screen. Opening an item never changes who it is assigned to.
import { useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import type { ButtonDropdownProps } from '@cloudscape-design/components';

import type { WorkItem } from './work-items';

export type WorkItemAction = 'annotate';

const hrefPath = (href: string): string => href.replace(/^#/, '');

/** The row menu entries every HITL work table offers. */
export const workItemActions = (): ButtonDropdownProps.Item[] => [{ id: 'annotate', text: 'Annotate' }];

/** Runs a common row action; returns false for an id it doesn't handle. */
const useWorkItemActions = () => {
  const navigate = useNavigate();

  const run = useCallback(
    (item: WorkItem, action: string): boolean => {
      if (action !== 'annotate') return false;
      navigate(hrefPath(item.href));
      return true;
    },
    [navigate],
  );

  return { run };
};

export default useWorkItemActions;
