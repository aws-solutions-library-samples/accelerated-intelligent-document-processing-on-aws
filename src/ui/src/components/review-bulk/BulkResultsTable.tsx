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

const ALL: SelectProps.Option = { label: 'All outcomes', value: '' };

const BulkResultsTable = ({
  results,
  loading = false,
  exportName,
  header = 'Documents',
  footer,
}: BulkResultsTableProps): React.JSX.Element => {
  const [outcome, setOutcome] = useState<SelectProps.Option>(ALL);
  const filtered = useMemo(() => (outcome.value ? results.filter((r) => r.outcome === outcome.value) : results), [results, outcome]);
  const present = useMemo(() => new Set(results.map((r) => r.outcome)), [results]);
  const options = [
    ALL,
    ...Object.keys(BULK_OUTCOMES)
      .filter((o) => present.has(o))
      .map((o) => ({ label: outcomeView(o).label, value: o })),
  ];
  const { items, filterProps, paginationProps, collectionProps, filteredItemsCount } = useCollection(filtered, {
    filtering: {
      filteringFunction: (item, text) => item.objectKey.toLowerCase().includes(text.toLowerCase()),
      empty: (
        <Box textAlign="center" color="inherit">
          <b>No documents</b>
          <Box variant="p" color="inherit">
            Results appear here as documents are processed.
          </Box>
        </Box>
      ),
      noMatch: (
        <Box textAlign="center" color="inherit">
          <b>No matches</b>
          <Box variant="p" color="inherit">
            No document matches the filter.
          </Box>
        </Box>
      ),
    },
    pagination: { pageSize: 25 },
    sorting: {},
  });

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
      loadingText="Loading results"
      variant="embedded"
      header={
        <Header
          counter={`(${results.length})`}
          actions={
            <Button iconName="download" onClick={download} disabled={filtered.length === 0}>
              Export to Excel
            </Button>
          }
        >
          {header}
        </Header>
      }
      filter={
        <SpaceBetween direction="horizontal" size="xs">
          <TextFilter
            {...filterProps}
            filteringPlaceholder="Find a document"
            filteringAriaLabel="Find a document"
            countText={`${filteredItemsCount ?? 0} matches`}
          />
          <Select
            selectedOption={outcome}
            onChange={({ detail }) => setOutcome(detail.selectedOption)}
            options={options}
            ariaLabel="Outcome"
          />
        </SpaceBetween>
      }
      pagination={<Pagination {...paginationProps} />}
      footer={footer}
      columnDefinitions={[
        {
          id: 'document',
          header: 'Document',
          cell: (r) => <Link href={documentDetailHref(r.objectKey)}>{r.objectKey}</Link>,
          sortingField: 'objectKey',
        },
        {
          id: 'outcome',
          header: 'Outcome',
          cell: (r) => {
            const view = outcomeView(r.outcome);
            return <StatusIndicator type={view.type}>{view.label}</StatusIndicator>;
          },
          sortingField: 'outcome',
        },
        { id: 'detail', header: 'Detail', cell: (r) => detailLabel(r.detail) || '-' },
      ]}
    />
  );
};

export default BulkResultsTable;
