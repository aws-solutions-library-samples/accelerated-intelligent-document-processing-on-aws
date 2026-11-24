// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import type { LinkProps } from '@cloudscape-design/components';
import { Box, Button, Header, SpaceBetween } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, type IntlShape, type NoMessageValues } from 'react-intl';

import { InfoLink } from './info-link';

const messages = defineMessages<{
  filterCount: { readonly count: number };
  noMatchTitle: NoMessageValues;
  noMatchBody: NoMessageValues;
  clearFilter: NoMessageValues;
  noData: NoMessageValues;
}>({
  filterCount: {
    id: 'common.table.filterCount',
    defaultMessage: '{count, plural, one {# match} other {# matches}}',
    description: "Count next to a table's text filter. {count} is the number of rows the filter matches.",
  },
  noMatchTitle: {
    id: 'common.table.noMatch.title',
    defaultMessage: 'No matches',
    description: 'Heading a table shows when its filter matches no row.',
  },
  noMatchBody: {
    id: 'common.table.noMatch.body',
    defaultMessage: "We can't find a match.",
    description: 'Text under the heading a table shows when its filter matches no row.',
  },
  clearFilter: {
    id: 'common.table.noMatch.clear',
    defaultMessage: 'Clear filter',
    description: 'Button under the no-match text of a table that clears its filter.',
  },
  noData: {
    id: 'common.table.noData',
    defaultMessage: 'No data!',
    description:
      "Shown in a table that has no rows once loading has finished, where the table has no empty text of its own (a queue's Documents table, Review Management's Test Studio queues).",
  },
});

/** Empty state for a table with no rows (after loading has finished). */
export const NoDataState = (): React.JSX.Element => (
  <Box textAlign="center" color="inherit" padding="s">
    <FormattedMessage {...messages.noData} />
  </Box>
);

/** The count next to a table's text filter, in the UI's language. */
export const filterCounterText = (intl: IntlShape, count: number): string => intl.formatMessage(messages.filterCount, { count });
/* prettier-ignore */
const getHeaderCounterText = (items: unknown[] = [], selectedItems: unknown[] = []): string => (
  selectedItems && selectedItems.length > 0
    ? `(${selectedItems.length}/${items.length})`
    : `(${items.length})`
);

interface TableHeaderProps {
  counter?: string;
  totalItems?: unknown[];
  selectedItems?: unknown[];
  updateTools?: LinkProps['onFollow'];
  description?: string;
  actionButtons?: React.ReactNode;
  title?: React.ReactNode;
}

const getCounter = (props: TableHeaderProps): string | null => {
  if (props.counter) {
    return props.counter;
  }
  if (!props.totalItems) {
    return null;
  }
  return getHeaderCounterText(props.totalItems, props.selectedItems);
};

export const TableHeader = (props: TableHeaderProps): React.JSX.Element => (
  <Header
    counter={getCounter(props)}
    info={props.updateTools && <InfoLink onFollow={props.updateTools} />}
    description={props.description}
    actions={props.actionButtons}
  >
    {props.title}
  </Header>
);

interface TableEmptyStateProps {
  /** The heading, already in the UI's language ("No documents"). */
  title: string;
  /** The text under the heading, already in the UI's language; say what to do next. */
  description: string;
  /** An optional call to action under the text. */
  action?: React.ReactNode;
}

export const TableEmptyState = ({ title, description, action }: TableEmptyStateProps): React.JSX.Element => (
  <Box margin={{ vertical: 'xs' }} textAlign="center" color="inherit">
    <SpaceBetween size="xxs">
      <div>
        <b>{title}</b>
        <Box variant="p" color="inherit">
          {description}
        </Box>
      </div>
      {action}
    </SpaceBetween>
  </Box>
);

interface TableNoMatchStateProps {
  onClearFilter?: () => void;
}

export const TableNoMatchState = (props: TableNoMatchStateProps): React.JSX.Element => (
  <Box margin={{ vertical: 'xs' }} textAlign="center" color="inherit">
    <SpaceBetween size="xxs">
      <div>
        <b>
          <FormattedMessage {...messages.noMatchTitle} />
        </b>
        <Box variant="p" color="inherit">
          <FormattedMessage {...messages.noMatchBody} />
        </Box>
      </div>
      <Button onClick={props.onClearFilter}>
        <FormattedMessage {...messages.clearFilter} />
      </Button>
    </SpaceBetween>
  </Box>
);
