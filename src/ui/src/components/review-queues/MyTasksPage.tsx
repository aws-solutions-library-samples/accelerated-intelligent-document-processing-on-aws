// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Home › Tasks (Annotators): the items assigned to the caller (by an Admin, or
// claimed from Home › Queues) —
// production review items and Test Studio documents, both from the shared
// ReviewWorkProvider poll. Opening one goes to its edit screen.
import React, { useMemo, useState } from 'react';
import { Alert, Button, ContentLayout, Header, SpaceBetween } from '@cloudscape-design/components';

import { useOptionalReviewWork } from '../../hooks/use-review-work';
import { describeApiError } from '../../hooks/utils/graphql-error';
import WorkItemsTable from './WorkItemsTable';
import useWorkItemActions from './use-work-item-actions';
import { fromReviewItem } from './work-items';

/** The columns the Tasks table offers, in order; the row menu follows them. */
const TASK_COLUMNS = ['document', 'source', 'queue', 'priority', 'age', 'reason'] as const;
/** The columns shown until the user changes them in the table's settings; the others are offered there. */
const DEFAULT_VISIBLE_TASK_COLUMNS = ['document', 'queue', 'age', 'reason'] as const;
/** The browser storage key of the Tasks table's settings. */
const TASK_PREFERENCES_KEY = 'my-tasks-preferences';

const MyTasksPage = (): React.JSX.Element => {
  const reviewWork = useOptionalReviewWork();
  const { run: runRowAction } = useWorkItemActions();
  const [refreshing, setRefreshing] = useState(false);

  const items = useMemo(
    () => [...(reviewWork?.myItems ?? []).map(fromReviewItem('tasks')), ...(reviewWork?.testSetTasks ?? [])],
    [reviewWork?.myItems, reviewWork?.testSetTasks],
  );

  const refresh = async () => {
    setRefreshing(true);
    try {
      await reviewWork?.refresh();
    } finally {
      setRefreshing(false);
    }
  };

  const loaded = !reviewWork || reviewWork.loaded;
  const pollError = reviewWork?.pollError ?? null;

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          counter={reviewWork?.count != null ? `(${reviewWork.count})` : undefined}
          description="Items assigned to you, by an Admin or claimed from your queues. Open an item to annotate it."
        >
          Tasks
        </Header>
      }
    >
      <SpaceBetween size="l">
        {pollError != null && (
          <Alert
            type="warning"
            header={items.length > 0 ? 'Showing the last loaded tasks' : "Couldn't load your tasks"}
            action={<Button onClick={() => void refresh()}>Retry</Button>}
          >
            {describeApiError(pollError, 'load your tasks')}
          </Alert>
        )}
        <WorkItemsTable
          items={items}
          loading={!loaded || (refreshing && items.length === 0)}
          title="Items assigned to you"
          onRowAction={runRowAction}
          columns={TASK_COLUMNS}
          defaultVisible={DEFAULT_VISIBLE_TASK_COLUMNS}
          preferencesKey={TASK_PREFERENCES_KEY}
          showSourceFilter
          empty="Nothing is assigned to you. Claim an item from Home › Queues, or wait for an Admin to assign you one (you're told in your Inbox)."
          actions={
            <Button iconName="refresh" ariaLabel="Refresh" loading={refreshing} onClick={() => void refresh()}>
              Refresh
            </Button>
          }
        />
      </SpaceBetween>
    </ContentLayout>
  );
};

export default MyTasksPage;
