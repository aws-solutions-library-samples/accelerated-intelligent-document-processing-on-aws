// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Per-document results of a bulk Skip All Reviews run or Send to review: one
// StatusIndicator per outcome, filterable, exportable to Excel.
import React, { useMemo, useState } from 'react';
import {
  Box,
  Button,
  Header,
  Link,
  Pagination,
  Select,
  SpaceBetween,
  StatusIndicator,
  Table,
  TextFilter,
} from '@cloudscape-design/components';
import type { SelectProps } from '@cloudscape-design/components';
import { useCollection } from '@cloudscape-design/collection-hooks';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';

import { exportToExcel } from '../common/download-func';
import { documentDetailHref } from '../../routes/constants';
import { BULK_OUTCOMES, detailLabel, outcomeView } from './bulk-outcomes';

export interface BulkResultRow {
  objectKey: string;
  outcome: string;
  kind?: string | null;
  detail?: string | null;
}

interface BulkResultsTableProps {
  results: BulkResultRow[];
  loading?: boolean;
  exportName: string;
  header?: string;
  footer?: React.ReactNode;
}

const messages = defineMessages<{
  allOutcomes: NoMessageValues;
  documents: NoMessageValues;
  emptyTitle: NoMessageValues;
  emptyDescription: NoMessageValues;
  noMatchTitle: NoMessageValues;
  noMatchDescription: NoMessageValues;
  loading: NoMessageValues;
  exportToExcel: NoMessageValues;
  findDocument: NoMessageValues;
  matches: { readonly count: number };
  outcomeFilter: NoMessageValues;
  documentColumn: NoMessageValues;
  outcomeColumn: NoMessageValues;
  detailColumn: NoMessageValues;
}>({
  allOutcomes: {
    id: 'reviewBulk.bulkResultsTable.allOutcomes',
    defaultMessage: 'All outcomes',
    description: 'Option of the outcome filter of the bulk results table that shows every outcome.',
  },
  documents: {
    id: 'reviewBulk.bulkResultsTable.documents',
    defaultMessage: 'Documents',
    description: 'Default heading of the per-document results table of a bulk Skip All Reviews run or of Send to review.',
  },
  emptyTitle: {
    id: 'reviewBulk.bulkResultsTable.emptyTitle',
    defaultMessage: 'No documents',
    description: 'Title of the empty state of the bulk results table.',
  },
  emptyDescription: {
    id: 'reviewBulk.bulkResultsTable.emptyDescription',
    defaultMessage: 'Results appear here as documents are processed.',
    description: 'Description of the empty state of the bulk results table.',
  },
  noMatchTitle: {
    id: 'reviewBulk.bulkResultsTable.noMatchTitle',
    defaultMessage: 'No matches',
    description: 'Title shown in the bulk results table when no row matches the text filter.',
  },
  noMatchDescription: {
    id: 'reviewBulk.bulkResultsTable.noMatchDescription',
    defaultMessage: 'No document matches the filter.',
    description: 'Description shown in the bulk results table when no row matches the text filter.',
  },
  loading: {
    id: 'reviewBulk.bulkResultsTable.loading',
    defaultMessage: 'Loading results',
    description: 'Loading text of the bulk results table.',
  },
  exportToExcel: {
    id: 'reviewBulk.bulkResultsTable.exportToExcel',
    defaultMessage: 'Export to Excel',
    description: 'Button that downloads the rows shown in the bulk results table as an Excel file.',
  },
  findDocument: {
    id: 'reviewBulk.bulkResultsTable.findDocument',
    defaultMessage: 'Find a document',
    description: 'Placeholder and screen-reader label of the text filter of the bulk results table (matches the document name).',
  },
  matches: {
    id: 'reviewBulk.bulkResultsTable.matches',
    defaultMessage: '{count, plural, one {# match} other {# matches}}',
    description: 'Count next to the text filter of the bulk results table. {count} is the number of matching rows.',
  },
  outcomeFilter: {
    id: 'reviewBulk.bulkResultsTable.outcomeFilter',
    defaultMessage: 'Outcome',
    description: 'Screen-reader label of the outcome filter of the bulk results table.',
  },
  documentColumn: {
    id: 'reviewBulk.bulkResultsTable.documentColumn',
    defaultMessage: 'Document',
    description: 'Column header of the bulk results table: the document name.',
  },
  outcomeColumn: {
    id: 'reviewBulk.bulkResultsTable.outcomeColumn',
    defaultMessage: 'Outcome',
    description: 'Column header of the bulk results table: what happened to the document (e.g. Skipped).',
  },
  detailColumn: {
    id: 'reviewBulk.bulkResultsTable.detailColumn',
    defaultMessage: 'Detail',
    description: 'Column header of the bulk results table: extra detail about the outcome.',
  },
});

/** The outcome filter's "all" option has the empty value. */
const ALL_VALUE = '';

const BulkResultsTable = ({ results, loading = false, exportName, header, footer }: BulkResultsTableProps): React.JSX.Element => {
  const intl = useIntl();
  const all: SelectProps.Option = { label: intl.formatMessage(messages.allOutcomes), value: ALL_VALUE };
  const [outcome, setOutcome] = useState<SelectProps.Option>(all);
  const filtered = useMemo(() => (outcome.value ? results.filter((r) => r.outcome === outcome.value) : results), [results, outcome]);
  const present = useMemo(() => new Set(results.map((r) => r.outcome)), [results]);
  const options = [
    all,
    ...Object.keys(BULK_OUTCOMES)
      .filter((o) => present.has(o))
      .map((o) => ({ label: outcomeView(o).label, value: o })),
  ];
  const { items, filterProps, paginationProps, collectionProps, filteredItemsCount } = useCollection(filtered, {
    filtering: {
      filteringFunction: (item, text) => item.objectKey.toLowerCase().includes(text.toLowerCase()),
      empty: (
        <Box textAlign="center" color="inherit">
          <b>
            <FormattedMessage {...messages.emptyTitle} />
          </b>
          <Box variant="p" color="inherit">
            <FormattedMessage {...messages.emptyDescription} />
          </Box>
        </Box>
      ),
      noMatch: (
        <Box textAlign="center" color="inherit">
          <b>
            <FormattedMessage {...messages.noMatchTitle} />
          </b>
          <Box variant="p" color="inherit">
            <FormattedMessage {...messages.noMatchDescription} />
          </Box>
        </Box>
      ),
    },
    pagination: { pageSize: 25 },
    sorting: {},
  });

  // The Excel column names stay English: export content is English by design.
  const download = () =>
    void exportToExcel(
      filtered.map((r) => ({ Document: r.objectKey, Outcome: outcomeView(r.outcome).label, Detail: detailLabel(r.detail) })),
      exportName,
    );

  return (
    <Table
      {...collectionProps}
      items={items}
      loading={loading}
      loadingText={intl.formatMessage(messages.loading)}
      variant="embedded"
      header={
        <Header
          counter={`(${results.length})`}
          actions={
            <Button iconName="download" onClick={download} disabled={filtered.length === 0}>
              <FormattedMessage {...messages.exportToExcel} />
            </Button>
          }
        >
          {header ?? intl.formatMessage(messages.documents)}
        </Header>
      }
      filter={
        <SpaceBetween direction="horizontal" size="xs">
          <TextFilter
            {...filterProps}
            filteringPlaceholder={intl.formatMessage(messages.findDocument)}
            filteringAriaLabel={intl.formatMessage(messages.findDocument)}
            countText={intl.formatMessage(messages.matches, { count: filteredItemsCount ?? 0 })}
          />
          <Select
            selectedOption={outcome.value === ALL_VALUE ? all : outcome}
            onChange={({ detail }) => setOutcome(detail.selectedOption)}
            options={options}
            ariaLabel={intl.formatMessage(messages.outcomeFilter)}
          />
        </SpaceBetween>
      }
      pagination={<Pagination {...paginationProps} />}
      footer={footer}
      columnDefinitions={[
        {
          id: 'document',
          header: intl.formatMessage(messages.documentColumn),
          cell: (r) => <Link href={documentDetailHref(r.objectKey)}>{r.objectKey}</Link>,
          sortingField: 'objectKey',
        },
        {
          id: 'outcome',
          header: intl.formatMessage(messages.outcomeColumn),
          cell: (r) => {
            const view = outcomeView(r.outcome);
            return <StatusIndicator type={view.type}>{view.label}</StatusIndicator>;
          },
          sortingField: 'outcome',
        },
        { id: 'detail', header: intl.formatMessage(messages.detailColumn), cell: (r) => detailLabel(r.detail) || '-' },
      ]}
    />
  );
};

export default BulkResultsTable;
