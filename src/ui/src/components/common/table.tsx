// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import type { LinkProps } from '@cloudscape-design/components';
import { Box, Button, Header, SpaceBetween } from '@cloudscape-design/components';

import { InfoLink } from './info-link';

/** The one empty-table text for Human Review and My Work tables. */
export const NO_DATA_TEXT = 'No data!';

/** Empty state for a table with no rows (after loading has finished). */
export const NoDataState = (): React.JSX.Element => (
  <Box textAlign="center" color="inherit" padding="s">
    {NO_DATA_TEXT}
  </Box>
);

export const getFilterCounterText = (count: number): string => `${count} ${count === 1 ? 'match' : 'matches'}`;
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
  resourceName: string;
  /** Replaces the default "No <resource>s" heading. */
  title?: string;
  /** Replaces the default "No <resource>s found." text; say what to do next. */
  description?: string;
  /** An optional call to action under the text. */
  action?: React.ReactNode;
}

export const TableEmptyState = ({ resourceName, title, description, action }: TableEmptyStateProps): React.JSX.Element => (
  <Box margin={{ vertical: 'xs' }} textAlign="center" color="inherit">
    <SpaceBetween size="xxs">
      <div>
        <b>{title ?? ` No ${resourceName.toLowerCase()}s`}</b>
        <Box variant="p" color="inherit">
          {description ?? `No ${resourceName.toLowerCase()}s found.`}
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
        <b>No matches</b>
        <Box variant="p" color="inherit">
          We can&apos;t find a match.
        </Box>
      </div>
      <Button onClick={props.onClearFilter}>Clear filter</Button>
    </SpaceBetween>
  </Box>
);
