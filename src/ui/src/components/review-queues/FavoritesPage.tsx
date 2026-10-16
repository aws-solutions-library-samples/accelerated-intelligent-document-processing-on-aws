// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// My Work › Favorites: the documents (and, for Admins, queues) the caller starred.
// Kept in this browser for the signed-in user. Queues are Admin-only pages, so an
// Annotator sees only their starred documents.
import React, { useMemo } from 'react';
import { useCollection } from '@cloudscape-design/collection-hooks';
import type { TableProps } from '@cloudscape-design/components';
import { Badge, Box, Button, ContentLayout, Header, Link, Pagination, Table, TextFilter } from '@cloudscape-design/components';

import useFavorites from '../../hooks/use-favorites';
import useUserRole from '../../hooks/use-user-role';
import type { Favorite, FavoriteKind } from '../../hooks/use-favorites';
import { formatDateTime, formatRelativeTime } from '../../utils/date-time';
import { paginationLabels } from '../common/labels';
import { TableNoMatchState, getFilterCounterText } from '../common/table';

const KIND_LABELS: Record<FavoriteKind, string> = {
  'review-queue': 'Production queue',
  'test-set': 'Test Studio queue',
  'review-item': 'Production document',
  'test-set-item': 'Test Studio document',
};

const QUEUE_KINDS: ReadonlySet<FavoriteKind> = new Set(['review-queue', 'test-set']);

const FavoritesPage = (): React.JSX.Element => {
  const { favorites: all, remove } = useFavorites();
  const { isAdmin } = useUserRole();
  const favorites = useMemo(() => (isAdmin ? all : all.filter((f) => !QUEUE_KINDS.has(f.kind))), [all, isAdmin]);
  const {
    items,
    actions: collectionActions,
    filteredItemsCount,
    collectionProps,
    filterProps,
    paginationProps,
  } = useCollection([...favorites], {
    filtering: {
      empty: (
        <Box textAlign="center" color="inherit" padding="s">
          No favorites yet. Star a document on its annotation screen to find it here.
        </Box>
      ),
      noMatch: <TableNoMatchState onClearFilter={() => collectionActions.setFiltering('')} />,
      filteringFunction: (f, text) =>
        [f.name, f.context ?? '', KIND_LABELS[f.kind]].some((v) => v.toLowerCase().includes(text.toLowerCase())),
    },
    pagination: { pageSize: 25 },
    sorting: { defaultState: { sortingColumn: { sortingField: 'addedAt' }, isDescending: true } },
  });

  const columns: TableProps.ColumnDefinition<Favorite>[] = [
    {
      id: 'name',
      header: 'Name',
      isRowHeader: true,
      sortingField: 'name',
      cell: (f) => <Link href={f.href}>{f.name}</Link>,
    },
    { id: 'kind', header: 'Type', sortingField: 'kind', cell: (f) => <Badge color="grey">{KIND_LABELS[f.kind]}</Badge> },
    { id: 'context', header: 'Where', cell: (f) => f.context ?? '—' },
    {
      id: 'addedAt',
      header: 'Added',
      sortingField: 'addedAt',
      cell: (f) => <span title={formatDateTime(f.addedAt)}>{formatRelativeTime(f.addedAt)}</span>,
    },
    {
      id: 'remove',
      header: <span className="idp-visually-hidden">Remove</span>,
      width: 60,
      cell: (f) => (
        <Button variant="icon" iconName="star-filled" ariaLabel={`Remove ${f.name} from favorites`} onClick={() => remove(f.id)} />
      ),
    },
  ];

  return (
    <ContentLayout
      header={
        <Header variant="h1" description="Documents you starred, kept in this browser.">
          Favorites
        </Header>
      }
    >
      <Table<Favorite>
        {...collectionProps}
        variant="container"
        items={items}
        trackBy="id"
        columnDefinitions={columns}
        header={<Header counter={`(${favorites.length})`}>Favorites</Header>}
        filter={
          <TextFilter
            {...filterProps}
            filteringPlaceholder="Find favorites"
            filteringAriaLabel="Find favorites"
            countText={getFilterCounterText(filteredItemsCount ?? 0)}
          />
        }
        pagination={<Pagination {...paginationProps} ariaLabels={paginationLabels} />}
      />
    </ContentLayout>
  );
};

export default FavoritesPage;
