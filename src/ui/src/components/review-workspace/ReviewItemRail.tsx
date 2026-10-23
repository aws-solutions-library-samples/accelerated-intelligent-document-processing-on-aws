// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The item rail of the review workspace: a collapsible, single-select list of the
 * items an adapter offers (test-set documents, or queue items).
 *
 * Selecting an item only selects it. It never claims: browsing must not lock work
 * away from teammates, so claiming is always a separate, explicit action.
 */

import React from 'react';
import { Box, Button, Cards, Container, Header, SpaceBetween } from '@cloudscape-design/components';

interface ReviewItemRailProps<T> {
  title: string;
  items: T[];
  /** Number shown in the header and, when collapsed, in place of the list. */
  count: number;
  trackBy: Extract<keyof T, string>;
  renderHeader: (item: T) => React.ReactNode;
  renderRow: (item: T) => React.ReactNode;
  selectedId: string | null;
  onSelect: (item: T) => void;
  isItemDisabled?: (item: T) => boolean;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  /** Controls above the list (a text filter, a reveal link). */
  filter?: React.ReactNode;
  empty?: React.ReactNode;
  pagination?: React.ReactNode;
}

const ReviewItemRail = <T,>({
  title,
  items,
  count,
  trackBy,
  renderHeader,
  renderRow,
  selectedId,
  onSelect,
  isItemDisabled,
  collapsed,
  onToggleCollapsed,
  filter,
  empty,
  pagination,
}: ReviewItemRailProps<T>): React.JSX.Element => {
  const selected = items.filter((item) => String(item[trackBy]) === selectedId);

  return (
    <Container
      header={
        <Header
          variant="h3"
          counter={collapsed ? undefined : `(${count})`}
          actions={
            <Button
              variant="inline-icon"
              iconName={collapsed ? 'angle-right' : 'angle-left'}
              ariaLabel={collapsed ? `Expand ${title.toLowerCase()}` : `Collapse ${title.toLowerCase()}`}
              onClick={onToggleCollapsed}
            />
          }
        >
          {collapsed ? '' : title}
        </Header>
      }
    >
      {collapsed ? (
        <Box fontSize="body-s" color="text-body-secondary" textAlign="center">
          {count}
        </Box>
      ) : (
        <SpaceBetween size="s">
          {filter}
          <Cards
            items={items}
            trackBy={trackBy}
            selectionType="single"
            selectedItems={selected}
            onSelectionChange={({ detail }) => {
              const item = detail.selectedItems[0];
              if (item) onSelect(item);
            }}
            isItemDisabled={isItemDisabled}
            cardDefinition={{
              header: renderHeader,
              sections: [{ id: 'row', content: renderRow }],
            }}
            cardsPerRow={[{ cards: 1 }]}
            empty={empty}
          />
          {pagination}
        </SpaceBetween>
      )}
    </Container>
  );
};

export default ReviewItemRail;
