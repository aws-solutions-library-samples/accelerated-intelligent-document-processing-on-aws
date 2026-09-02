// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  CollectionPreferences,
  Header,
  Link,
  Modal,
  Pagination,
  Select,
  SpaceBetween,
  StatusIndicator,
  Table,
  TextFilter,
} from '@cloudscape-design/components';
import type { CollectionPreferencesProps } from '@cloudscape-design/components';
import { useCollection } from '@cloudscape-design/collection-hooks';

import type { ApiClient } from './api';
import type { ClaimRow } from './types';
import ClaimDetail from './ClaimDetail';
import { STATUS_META } from './statusMeta';

const STATUS_OPTIONS = [
  { label: 'All statuses', value: '' },
  { label: 'Clean claim', value: 'CLEAN_CLAIM' },
  { label: 'Review required', value: 'REVIEW_REQUIRED' },
  { label: 'Insufficient documentation', value: 'INSUFFICIENT_DOCUMENTATION' },
];

const WINDOW_OPTIONS = [
  { label: 'All time', value: '' },
  { label: 'Last 24 hours', value: '24h' },
  { label: 'Last 7 days', value: '7d' },
  { label: 'Last 28 days', value: '28d' },
];

// Mirrors the Document List table conventions: id/header pairs drive both the
// column definitions and the visible-content preference options.
const VISIBLE_CONTENT_OPTIONS: CollectionPreferencesProps.VisibleContentOptionsGroup[] = [
  {
    label: 'Claim properties',
    options: [
      { id: 'documentId', label: 'Document', editable: false },
      { id: 'status', label: 'Status' },
      { id: 'counts', label: 'Pass / Fail / Not found' },
      { id: 'totalRules', label: 'Total rules' },
      { id: 'policyTypes', label: 'Policy types' },
      { id: 'updatedAt', label: 'Updated' },
    ],
  },
];

const PAGE_SIZE_OPTIONS: CollectionPreferencesProps.PageSizePreference['options'] = [
  { value: 10, label: '10 claims' },
  { value: 25, label: '25 claims' },
  { value: 50, label: '50 claims' },
];

const DEFAULT_PREFERENCES: CollectionPreferencesProps.Preferences = {
  pageSize: 25,
  wrapLines: false,
  visibleContent: ['status', 'counts', 'totalRules', 'policyTypes', 'updatedAt'],
};

interface ClaimsDashboardViewProps {
  api: ApiClient;
  enabled: boolean;
}

const ClaimsDashboardView: React.FC<ClaimsDashboardViewProps> = ({ api, enabled }) => {
  const [claims, setClaims] = useState<ClaimRow[]>([]);
  const [statusFilter, setStatusFilter] = useState('');
  const [windowFilter, setWindowFilter] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [selectedDocId, setSelectedDocId] = useState<string | null>(null);
  const [preferences, setPreferences] =
    useState<CollectionPreferencesProps.Preferences>(DEFAULT_PREFERENCES);
  const [deleteModalVisible, setDeleteModalVisible] = useState(false);
  const [deleteLoading, setDeleteLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await api.listClaims({
        status: statusFilter || undefined,
        window: windowFilter || undefined,
      });
      setClaims(resp.claims);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [api, statusFilter, windowFilter]);

  useEffect(() => {
    if (enabled) refresh();
  }, [refresh, enabled]);

  const emptyState = (
    <Box textAlign="center" padding="l">
      <SpaceBetween size="s">
        <b>No claims processed yet</b>
        <Box variant="small">
          Activate the <code>sample-health-insurance-review</code> configuration
          profile, then upload the sample prior-auth packet{' '}
          <code>samples/rule-validation/medicare_respiratory_pa_packet.pdf</code>{' '}
          to the input bucket. After rule validation runs, the
          postRuleValidation hook records the claim here.
        </Box>
      </SpaceBetween>
    </Box>
  );

  const {
    items,
    actions,
    filteredItemsCount,
    collectionProps,
    filterProps,
    paginationProps,
  } = useCollection(claims, {
    filtering: {
      empty: emptyState,
      noMatch: (
        <Box textAlign="center" padding="l">
          <SpaceBetween size="s">
            <b>No matches</b>
            <Button onClick={() => actions.setFiltering('')}>Clear filter</Button>
          </SpaceBetween>
        </Box>
      ),
      // Match on document id, status, and policy types.
      filteringFunction: (item, filteringText) => {
        const t = filteringText.toLowerCase();
        return (
          item.documentId.toLowerCase().includes(t) ||
          (item.status || '').toLowerCase().includes(t) ||
          (item.policyTypes || []).join(',').toLowerCase().includes(t)
        );
      },
    },
    pagination: { pageSize: preferences.pageSize },
    sorting: {
      defaultState: {
        sortingColumn: { sortingField: 'updatedAt' },
        isDescending: true,
      },
    },
    selection: { keepSelection: false, trackBy: 'documentId' },
  });

  const selectedItems = (collectionProps.selectedItems ?? []) as ClaimRow[];

  const handleDeleteConfirm = async () => {
    setDeleteLoading(true);
    setError(null);
    const failures: string[] = [];
    for (const item of selectedItems) {
      try {
        await api.deleteClaim(item.documentId);
      } catch (e) {
        failures.push(
          `${item.documentId}: ${e instanceof Error ? e.message : String(e)}`,
        );
      }
    }
    setDeleteLoading(false);
    setDeleteModalVisible(false);
    actions.setSelectedItems([]);
    if (failures.length) {
      setError(`Failed to delete: ${failures.join('; ')}`);
    }
    await refresh();
  };

  const columnDefinitions = useMemo(
    () => [
      {
        id: 'documentId',
        header: 'Document',
        cell: (c: ClaimRow) => (
          <Link onFollow={() => setSelectedDocId(c.documentId)}>
            {c.documentId}
          </Link>
        ),
        sortingField: 'documentId',
        width: 320,
        minWidth: 180,
      },
      {
        id: 'status',
        header: 'Status',
        cell: (c: ClaimRow) => {
          const meta = STATUS_META[c.status];
          return (
            <StatusIndicator type={meta?.indicator ?? 'info'}>
              {meta?.label ?? c.status}
            </StatusIndicator>
          );
        },
        sortingField: 'status',
        width: 220,
        minWidth: 140,
      },
      {
        id: 'counts',
        header: 'Pass / Fail / Not found',
        cell: (c: ClaimRow) => `${c.passCount} / ${c.failCount} / ${c.notFoundCount}`,
        width: 190,
        minWidth: 120,
      },
      {
        id: 'totalRules',
        header: 'Total rules',
        cell: (c: ClaimRow) => c.totalRules ?? '—',
        sortingField: 'totalRules',
        width: 120,
        minWidth: 90,
      },
      {
        id: 'policyTypes',
        header: 'Policy types',
        cell: (c: ClaimRow) => (c.policyTypes || []).join(', ') || '—',
        width: 280,
        minWidth: 140,
      },
      {
        id: 'updatedAt',
        header: 'Updated',
        cell: (c: ClaimRow) =>
          c.updatedAt ? new Date(c.updatedAt).toLocaleString() : '—',
        sortingField: 'updatedAt',
        width: 200,
        minWidth: 140,
      },
    ],
    [],
  );

  if (selectedDocId) {
    return (
      <ClaimDetail
        api={api}
        docId={selectedDocId}
        onBack={() => setSelectedDocId(null)}
      />
    );
  }

  return (
    <SpaceBetween size="l">
      {error && <Alert type="error">{error}</Alert>}
      <Table<ClaimRow>
        {...collectionProps}
        items={items}
        loading={loading}
        loadingText="Loading claims…"
        columnDefinitions={columnDefinitions}
        trackBy="documentId"
        selectionType="multi"
        resizableColumns
        wrapLines={preferences.wrapLines}
        visibleColumns={['documentId', ...(preferences.visibleContent ?? [])]}
        ariaLabels={{
          selectionGroupLabel: 'Claims selection',
          allItemsSelectionLabel: ({ selectedItems: sel }) =>
            `${sel.length} ${sel.length === 1 ? 'claim' : 'claims'} selected`,
          itemSelectionLabel: (_data, row) => `Select ${row.documentId}`,
        }}
        header={
          <Header
            counter={
              selectedItems.length
                ? `(${selectedItems.length}/${claims.length})`
                : `(${claims.length})`
            }
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Select
                  selectedOption={
                    STATUS_OPTIONS.find((o) => o.value === statusFilter) ??
                    STATUS_OPTIONS[0]
                  }
                  onChange={({ detail }) =>
                    setStatusFilter(detail.selectedOption.value ?? '')
                  }
                  options={STATUS_OPTIONS}
                />
                <Select
                  selectedOption={
                    WINDOW_OPTIONS.find((o) => o.value === windowFilter) ??
                    WINDOW_OPTIONS[0]
                  }
                  onChange={({ detail }) =>
                    setWindowFilter(detail.selectedOption.value ?? '')
                  }
                  options={WINDOW_OPTIONS}
                />
                <Button iconName="refresh" onClick={refresh} loading={loading}>
                  Refresh
                </Button>
                <Button
                  disabled={selectedItems.length === 0}
                  onClick={() => setDeleteModalVisible(true)}
                >
                  Delete
                </Button>
              </SpaceBetween>
            }
          >
            Processed claims
          </Header>
        }
        filter={
          <TextFilter
            {...filterProps}
            filteringAriaLabel="Filter claims"
            filteringPlaceholder="Find claims"
            countText={
              filteredItemsCount === 1
                ? '1 match'
                : `${filteredItemsCount ?? 0} matches`
            }
          />
        }
        pagination={<Pagination {...paginationProps} />}
        preferences={
          <CollectionPreferences
            title="Preferences"
            confirmLabel="Confirm"
            cancelLabel="Cancel"
            preferences={preferences}
            onConfirm={({ detail }) => setPreferences(detail)}
            pageSizePreference={{ title: 'Page size', options: PAGE_SIZE_OPTIONS }}
            wrapLinesPreference={{
              label: 'Wrap lines',
              description: 'Wrap long cell text onto multiple lines',
            }}
            visibleContentPreference={{
              title: 'Select visible columns',
              options: VISIBLE_CONTENT_OPTIONS,
            }}
          />
        }
      />

      <Modal
        visible={deleteModalVisible}
        onDismiss={() => setDeleteModalVisible(false)}
        header="Delete claims"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                onClick={() => setDeleteModalVisible(false)}
                disabled={deleteLoading}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleDeleteConfirm}
                loading={deleteLoading}
              >
                Delete
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="s">
          <Box>
            Delete{' '}
            <b>
              {selectedItems.length} {selectedItems.length === 1 ? 'claim' : 'claims'}
            </b>{' '}
            from the dashboard?
          </Box>
          <Box variant="small">
            This removes only the claim record from this dashboard — the
            document and its rule-validation outputs are untouched, and
            reprocessing the document recreates the claim. Requires the Admin
            or Author role.
          </Box>
          <ul>
            {selectedItems.map((i) => (
              <li key={i.documentId}>
                <code>{i.documentId}</code>
              </li>
            ))}
          </ul>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
};

export default ClaimsDashboardView;
