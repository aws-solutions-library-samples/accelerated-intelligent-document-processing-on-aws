// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Home › Tasks (Annotators): the items assigned to the caller (by an Admin, or
// claimed from Home › Queues) —
// production review items and Test Studio documents, both from the shared
// ReviewWorkProvider poll. Opening one goes to its edit screen.
import React, { useMemo, useState } from 'react';
import { Alert, Button, ContentLayout, Header, SpaceBetween } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';

import { useOptionalReviewWork } from '../../hooks/use-review-work';
import { describeApiError } from '../../hooks/utils/graphql-error';
import WorkItemsTable from './WorkItemsTable';
import useWorkItemActions from './use-work-item-actions';
import { fromReviewItem } from './work-items';

const messages = defineMessages<{
  title: NoMessageValues;
  description: NoMessageValues;
  loadedWarning: NoMessageValues;
  loadError: NoMessageValues;
  loadTasksAction: NoMessageValues;
  retry: NoMessageValues;
  tableTitle: NoMessageValues;
  empty: NoMessageValues;
  refresh: NoMessageValues;
}>({
  title: {
    id: 'reviewQueues.myTasks.title',
    defaultMessage: 'Tasks',
    description: 'Heading of Home › Tasks, the review items assigned to the reader.',
  },
  description: {
    id: 'reviewQueues.myTasks.description',
    defaultMessage: 'Items assigned to you, by an Admin or claimed from your queues. Open an item to annotate it.',
    description: 'Description under the Home › Tasks heading.',
  },
  loadedWarning: {
    id: 'reviewQueues.myTasks.loadedWarning',
    defaultMessage: 'Showing the last loaded tasks',
    description: 'Header of the warning on Home › Tasks when refreshing the tasks failed but earlier ones are still shown.',
  },
  loadError: {
    id: 'reviewQueues.myTasks.loadError',
    defaultMessage: "Couldn't load your tasks",
    description: 'Header of the warning on Home › Tasks when the tasks could not be loaded at all.',
  },
  loadTasksAction: {
    id: 'reviewQueues.myTasks.loadTasksAction',
    defaultMessage: 'load your tasks',
    description:
      'Action phrase completing an API error sentence ("Failed to …: <error>" / "You do not have permission to …") when the tasks on Home › Tasks cannot be loaded.',
  },
  retry: {
    id: 'reviewQueues.myTasks.retry',
    defaultMessage: 'Retry',
    description: 'Button of the Home › Tasks load warning that loads the tasks again.',
  },
  tableTitle: {
    id: 'reviewQueues.myTasks.tableTitle',
    defaultMessage: 'Items assigned to you',
    description: 'Title of the table on Home › Tasks.',
  },
  empty: {
    id: 'reviewQueues.myTasks.empty',
    defaultMessage:
      "Nothing is assigned to you. Claim an item from Home › Queues, or wait for an Admin to assign you one (you're told in your Inbox).",
    description: 'Shown in the Home › Tasks table when no item is assigned to the reader. Home › Queues and Inbox name pages of the app.',
  },
  refresh: {
    id: 'reviewQueues.myTasks.refresh',
    defaultMessage: 'Refresh',
    description: 'Button, and its screen-reader label, that reloads the Home › Tasks table.',
  },
});

/** The columns the Tasks table offers, in order; the row menu follows them. */
const TASK_COLUMNS = ['document', 'source', 'queue', 'priority', 'age', 'reason'] as const;
/** The columns shown until the user changes them in the table's settings; the others are offered there. */
const DEFAULT_VISIBLE_TASK_COLUMNS = ['document', 'queue', 'age', 'reason'] as const;
/** The browser storage key of the Tasks table's settings. */
const TASK_PREFERENCES_KEY = 'my-tasks-preferences';

const MyTasksPage = (): React.JSX.Element => {
  const intl = useIntl();
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
          description={intl.formatMessage(messages.description)}
        >
          <FormattedMessage {...messages.title} />
        </Header>
      }
    >
      <SpaceBetween size="l">
        {pollError != null && (
          <Alert
            type="warning"
            header={intl.formatMessage(items.length > 0 ? messages.loadedWarning : messages.loadError)}
            action={
              <Button onClick={() => void refresh()}>
                <FormattedMessage {...messages.retry} />
              </Button>
            }
          >
            {describeApiError(pollError, messages.loadTasksAction, intl)}
          </Alert>
        )}
        <WorkItemsTable
          items={items}
          loading={!loaded || (refreshing && items.length === 0)}
          title={intl.formatMessage(messages.tableTitle)}
          onRowAction={runRowAction}
          columns={TASK_COLUMNS}
          defaultVisible={DEFAULT_VISIBLE_TASK_COLUMNS}
          preferencesKey={TASK_PREFERENCES_KEY}
          showSourceFilter
          empty={intl.formatMessage(messages.empty)}
          actions={
            <Button iconName="refresh" ariaLabel={intl.formatMessage(messages.refresh)} loading={refreshing} onClick={() => void refresh()}>
              <FormattedMessage {...messages.refresh} />
            </Button>
          }
        />
      </SpaceBetween>
    </ContentLayout>
  );
};

export default MyTasksPage;
