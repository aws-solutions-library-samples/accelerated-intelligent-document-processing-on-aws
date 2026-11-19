// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import React, { useState } from 'react';
import { Table, Box, Container, Header, Pagination, CollectionPreferences } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';

const messages = defineMessages<{
  title: NoMessageValues;
  loading: NoMessageValues;
  emptyTitle: NoMessageValues;
  emptyBody: NoMessageValues;
  preferencesTitle: NoMessageValues;
  preferencesConfirm: NoMessageValues;
  preferencesCancel: NoMessageValues;
  pageSize: NoMessageValues;
  pageSizeOption: { readonly count: number };
  visibleContent: NoMessageValues;
  columnsGroup: NoMessageValues;
}>({
  title: {
    id: 'agents.table.title',
    defaultMessage: 'Table Results',
    description: "Heading of an agent's table result.",
  },
  loading: {
    id: 'agents.table.loading',
    defaultMessage: 'Loading table data',
    description: "Shown in an agent's result table while its rows load.",
  },
  emptyTitle: {
    id: 'agents.table.empty.title',
    defaultMessage: 'No data available',
    description: "Bold heading in an agent's result table when it has no rows.",
  },
  emptyBody: {
    id: 'agents.table.empty.body',
    defaultMessage: 'No table data to display.',
    description: "Text under the heading in an agent's result table when it has no rows.",
  },
  preferencesTitle: {
    id: 'agents.table.preferences.title',
    defaultMessage: 'Preferences',
    description: "Title of the settings dialog of an agent's result table, and the label of the button that opens it.",
  },
  preferencesConfirm: {
    id: 'agents.table.preferences.confirm',
    defaultMessage: 'Confirm',
    description: "Button that applies the settings of an agent's result table.",
  },
  preferencesCancel: {
    id: 'agents.table.preferences.cancel',
    defaultMessage: 'Cancel',
    description: "Button that closes the settings of an agent's result table without applying them.",
  },
  pageSize: {
    id: 'agents.table.preferences.pageSize',
    defaultMessage: 'Page size',
    description: "Title of the page size choice in the settings of an agent's result table.",
  },
  pageSizeOption: {
    id: 'agents.table.preferences.pageSizeOption',
    defaultMessage: '{count, plural, one {# row} other {# rows}}',
    description: "A page size option in the settings of an agent's result table. {count} is the number of rows per page.",
  },
  visibleContent: {
    id: 'agents.table.preferences.visibleContent',
    defaultMessage: 'Select visible content',
    description: "Title of the column choice in the settings of an agent's result table.",
  },
  columnsGroup: {
    id: 'agents.table.preferences.columnsGroup',
    defaultMessage: 'Main table properties',
    description: "Label of the group that lists the table's columns in the column choice of an agent's result table.",
  },
});

/** The page sizes offered in the table's settings. */
const PAGE_SIZES = [5, 10, 20, 50];

interface TableHeader {
  id: string;
  label: string;
  sortable?: boolean;
}

interface TableRow {
  id: string;
  data: Record<string, unknown>;
}

interface TableDataType {
  headers: TableHeader[];
  rows: TableRow[];
}

interface TableDisplayProps {
  tableData?: TableDataType | Record<string, unknown> | null;
}

interface TablePreferences {
  pageSize: number;
  visibleContent: string[];
}

const TableDisplay = ({ tableData = null }: TableDisplayProps): React.JSX.Element | null => {
  const intl = useIntl();
  const [preferences, setPreferences] = useState<TablePreferences>({
    pageSize: 10,
    visibleContent: ['all'],
  });
  const [currentPageIndex, setCurrentPageIndex] = useState(1);

  if (!tableData) {
    return null;
  }

  const typedTableData = tableData as TableDataType;
  const { headers, rows } = typedTableData;

  // Convert headers to AWS UI table format
  const columnDefinitions = headers.map((header) => ({
    id: header.id,
    header: header.label,
    cell: (item: TableRow) => item.data[header.id] as React.ReactNode,
    sortingField: header.sortable ? header.id : undefined,
  }));

  // Paginate the data
  const startIndex = (currentPageIndex - 1) * preferences.pageSize;
  const endIndex = startIndex + preferences.pageSize;
  const paginatedItems = rows.slice(startIndex, endIndex);

  return (
    <Container
      header={
        <Header variant="h3">
          <FormattedMessage {...messages.title} />
        </Header>
      }
    >
      <Table
        columnDefinitions={columnDefinitions}
        items={paginatedItems}
        loadingText={intl.formatMessage(messages.loading)}
        sortingDisabled={false}
        empty={
          <Box textAlign="center" color="inherit">
            <b>
              <FormattedMessage {...messages.emptyTitle} />
            </b>
            <Box padding={{ bottom: 's' }} variant="p" color="inherit">
              <FormattedMessage {...messages.emptyBody} />
            </Box>
          </Box>
        }
        pagination={
          <Pagination
            currentPageIndex={currentPageIndex}
            onChange={({ detail }) => setCurrentPageIndex(detail.currentPageIndex)}
            pagesCount={Math.ceil(rows.length / preferences.pageSize)}
          />
        }
        preferences={
          <CollectionPreferences
            title={intl.formatMessage(messages.preferencesTitle)}
            confirmLabel={intl.formatMessage(messages.preferencesConfirm)}
            cancelLabel={intl.formatMessage(messages.preferencesCancel)}
            preferences={preferences}
            onConfirm={({ detail }) => setPreferences(detail as TablePreferences)}
            pageSizePreference={{
              title: intl.formatMessage(messages.pageSize),
              options: PAGE_SIZES.map((count) => ({ value: count, label: intl.formatMessage(messages.pageSizeOption, { count }) })),
            }}
            visibleContentPreference={{
              title: intl.formatMessage(messages.visibleContent),
              options: [
                {
                  label: intl.formatMessage(messages.columnsGroup),
                  options: columnDefinitions.map(({ id, header }) => ({
                    id,
                    label: header,
                  })),
                },
              ],
            }}
          />
        }
      />
    </Container>
  );
};

export default TableDisplay;
