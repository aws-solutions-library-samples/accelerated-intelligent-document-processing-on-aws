// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Redaction Report — a metadata-only audit view of documents the preprocessing
 * hook has redacted. NO PII is shown (the audit table stores none): per-document
 * PII count, mode, source/redacted keys, companion profile, timestamp.
 *
 * Table UX mirrors the host Document List: resizable columns, pagination,
 * column selection + page-size preferences (persisted in localStorage), text
 * filter, and sorting. The "PII mapping" column is kept near the front so the
 * RBAC-gated "View mapping" action is visible without horizontal scrolling.
 */

import React, { useEffect, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  CollectionPreferences,
  Container,
  Header,
  Modal,
  Pagination,
  Select,
  SpaceBetween,
  Spinner,
  StatusIndicator,
  Table,
  TextFilter,
} from '@cloudscape-design/components';
import { useCollection } from '@cloudscape-design/collection-hooks';

import type { ApiClient } from './api';
import type { RedactionRow } from './types';

// Mirrors the host Document List "Load:" time-period dropdown
// (TIME_PERIOD_DROPDOWN_CONFIG in src/ui/.../documents-table-config.tsx),
// expressed as feature-API window strings (^(\d+)([hdw])$). The host default
// is 2 hours, so it is listed first and used as the initial selection.
const WINDOW_OPTIONS = [
  { value: '2h', label: '2 hrs' },
  { value: '4h', label: '4 hrs' },
  { value: '8h', label: '8 hrs' },
  { value: '24h', label: '1 day' },
  { value: '2d', label: '2 days' },
  { value: '1w', label: '1 week' },
  { value: '2w', label: '2 weeks' },
  { value: '30d', label: '30 days' },
];

const COLUMN_DEFINITIONS = [
  {
    id: 'documentId',
    header: 'Document',
    cell: (r: RedactionRow) => r.documentId,
    sortingField: 'documentId',
    minWidth: 160,
  },
  {
    id: 'mapping',
    header: 'PII mapping',
    // Real cell is injected in the component (needs the viewMapping closure).
    cell: (_r: RedactionRow): React.ReactNode => null,
    minWidth: 130,
  },
  {
    id: 'piiCount',
    header: 'PII redacted',
    cell: (r: RedactionRow) => r.piiCount ?? 0,
    sortingField: 'piiCount',
    minWidth: 110,
  },
  {
    id: 'mode',
    header: 'Mode',
    cell: (r: RedactionRow) =>
      r.mode === 'redactcopy_and_stop' ? (
        <StatusIndicator type="info">Redact &amp; stop</StatusIndicator>
      ) : (
        <StatusIndicator type="success">Redact &amp; continue</StatusIndicator>
      ),
    minWidth: 150,
  },
  {
    id: 'createdAt',
    header: 'When',
    cell: (r: RedactionRow) => r.createdAt,
    sortingField: 'createdAt',
    minWidth: 170,
  },
  {
    id: 'companionConfigVersion',
    header: 'Processed as',
    cell: (r: RedactionRow) => r.companionConfigVersion || '—',
    minWidth: 140,
  },
  {
    id: 'redactedKey',
    header: 'Redacted copy',
    cell: (r: RedactionRow) => r.redactedKey || '—',
    minWidth: 180,
  },
];

// "Redacted copy" (usually `<document>(REDACTED).<ext>`, derivable from the
// Document column) is hidden by default to keep the table narrow enough that
// every visible column — especially "PII mapping" — fits without scrolling.
const DEFAULT_PREFERENCES = {
  pageSize: 10,
  wrapLines: true,
  visibleContent: [
    'documentId',
    'mapping',
    'piiCount',
    'mode',
    'createdAt',
    'companionConfigVersion',
  ],
};

const PREFERENCES_STORAGE_KEY = 'pii-anonymizer-report-preferences';

type Preferences = typeof DEFAULT_PREFERENCES;

function loadPreferences(): Preferences {
  try {
    const raw = localStorage.getItem(PREFERENCES_STORAGE_KEY);
    if (raw) return { ...DEFAULT_PREFERENCES, ...JSON.parse(raw) };
  } catch {
    /* fall through to defaults */
  }
  return DEFAULT_PREFERENCES;
}

const RedactionReportView: React.FC<{ api: ApiClient; enabled: boolean }> = ({
  api,
  enabled,
}) => {
  const [rows, setRows] = useState<RedactionRow[]>([]);
  const [totalPii, setTotalPii] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [timeWindow, setTimeWindow] = useState(WINDOW_OPTIONS[0]);
  const [preferences, setPreferences] = useState<Preferences>(loadPreferences);
  // Mapping modal state
  const [mapDoc, setMapDoc] = useState<string | null>(null);
  const [mapRows, setMapRows] = useState<Array<{ original: string; synthetic: string }>>([]);
  const [mapLoading, setMapLoading] = useState(false);
  const [mapError, setMapError] = useState<string | null>(null);

  function savePreferences(p: Preferences) {
    setPreferences(p);
    try {
      localStorage.setItem(PREFERENCES_STORAGE_KEY, JSON.stringify(p));
    } catch {
      /* preferences just won't persist */
    }
  }

  function viewMapping(docId: string) {
    setMapDoc(docId);
    setMapRows([]);
    setMapError(null);
    setMapLoading(true);
    api
      .getMapping(docId)
      .then((m) => {
        const entries = Object.entries(m.mapping || {}).map(([original, synthetic]) => ({
          original,
          synthetic: String(synthetic),
        }));
        setMapRows(entries);
      })
      .catch((e: unknown) => setMapError(e instanceof Error ? e.message : String(e)))
      .finally(() => setMapLoading(false));
  }

  const load = React.useCallback(() => {
    if (!enabled) return;
    setLoading(true);
    setError(null);
    api
      .listReport({ window: timeWindow.value || undefined })
      .then((r) => {
        setRows(r.rows);
        setTotalPii(r.totalPiiRedacted);
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, [api, enabled, timeWindow]);

  useEffect(load, [load]);

  const columnDefinitions = COLUMN_DEFINITIONS.map((c) =>
    c.id === 'mapping'
      ? {
          ...c,
          cell: (r: RedactionRow) =>
            r.mappingStored ? (
              <Button variant="inline-link" onClick={() => viewMapping(r.documentId)}>
                View mapping
              </Button>
            ) : (
              <Box color="text-status-inactive">not stored</Box>
            ),
        }
      : c,
  );

  const { items, collectionProps, filterProps, paginationProps, filteredItemsCount } =
    useCollection(rows, {
      filtering: { empty: 'No documents redacted yet.' },
      pagination: { pageSize: preferences.pageSize },
      sorting: {
        defaultState: {
          sortingColumn: { sortingField: 'createdAt' },
          isDescending: true,
        },
      },
    });

  if (!enabled) {
    return (
      <Alert type="info" header="Subscription not active">
        Activate the subscription to view redaction activity.
      </Alert>
    );
  }

  return (
    <Container
      header={
        <Header
          variant="h2"
          counter={`(${rows.length})`}
          description={`Metadata-only audit of redacted documents — no PII is stored or shown. ${totalPii} PII items redacted in view.`}
          actions={
            <SpaceBetween size="xs" direction="horizontal">
              <Select
                selectedOption={timeWindow}
                onChange={({ detail }) =>
                  setTimeWindow(detail.selectedOption as { value: string; label: string })
                }
                options={WINDOW_OPTIONS}
                ariaLabel="Time window filter"
              />
              <Button
                iconName="refresh"
                onClick={load}
                loading={loading}
                ariaLabel="Refresh redaction report"
              />
            </SpaceBetween>
          }
        >
          Redaction Report
        </Header>
      }
    >
      {error && (
        <Alert type="error" header="Could not load report" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}
      {loading ? (
        <Spinner />
      ) : (
        <Table
          {...collectionProps}
          variant="embedded"
          items={items}
          columnDefinitions={columnDefinitions}
          visibleColumns={preferences.visibleContent}
          resizableColumns
          wrapLines={preferences.wrapLines}
          empty={
            <Box textAlign="center" color="inherit">
              No documents redacted yet.
            </Box>
          }
          filter={
            <TextFilter
              {...filterProps}
              filteringAriaLabel="Filter redacted documents"
              filteringPlaceholder="Find documents"
              countText={`${filteredItemsCount ?? 0} match${(filteredItemsCount ?? 0) === 1 ? '' : 'es'}`}
            />
          }
          pagination={<Pagination {...paginationProps} />}
          preferences={
            <CollectionPreferences
              title="Preferences"
              confirmLabel="Confirm"
              cancelLabel="Cancel"
              preferences={preferences}
              onConfirm={({ detail }) => savePreferences(detail as Preferences)}
              pageSizePreference={{
                title: 'Page size',
                options: [
                  { value: 10, label: '10 documents' },
                  { value: 25, label: '25 documents' },
                  { value: 50, label: '50 documents' },
                ],
              }}
              wrapLinesPreference={{
                label: 'Wrap lines',
                description: 'Wrap long values (e.g. document keys) onto multiple lines',
              }}
              visibleContentPreference={{
                title: 'Select visible columns',
                options: [
                  {
                    label: 'Report columns',
                    options: COLUMN_DEFINITIONS.map((c) => ({
                      id: c.id,
                      label: typeof c.header === 'string' ? c.header : c.id,
                      editable: c.id !== 'documentId',
                    })),
                  },
                ],
              }}
            />
          }
        />
      )}

      <Modal
        visible={mapDoc !== null}
        onDismiss={() => setMapDoc(null)}
        header="PII mapping (sensitive)"
        footer={
          <Box float="right">
            <Button variant="primary" onClick={() => setMapDoc(null)}>
              Close
            </Button>
          </Box>
        }
      >
        <SpaceBetween size="s">
          <Alert type="warning">
            This is the original→synthetic mapping — it contains the real PII. You
            can see it because you have access to the configuration profile that processed
            the original document.
          </Alert>
          {mapError && <Alert type="error">{mapError}</Alert>}
          {mapLoading ? (
            <Spinner />
          ) : (
            <Table
              variant="embedded"
              items={mapRows}
              empty={<Box textAlign="center">No mapping entries.</Box>}
              columnDefinitions={[
                { id: 'original', header: 'Original', cell: (m) => m.original },
                { id: 'synthetic', header: 'Synthetic replacement', cell: (m) => m.synthetic },
              ]}
            />
          )}
        </SpaceBetween>
      </Modal>
    </Container>
  );
};

export default RedactionReportView;
