// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Container,
  Header,
  KeyValuePairs,
  PieChart,
  Select,
  SpaceBetween,
  Spinner,
} from '@cloudscape-design/components';

import type { FeatureContext } from './types';

interface CountsResponse {
  counts: Record<string, number>;
  total: number;
  window: string;
  asOf: string;
}

const WINDOW_OPTIONS = [
  { label: 'All time', value: '' },
  { label: 'Last 24 hours', value: '24h' },
  { label: 'Last 7 days', value: '7d' },
  { label: 'Last 28 days', value: '28d' },
];

/**
 * Docs-by-status sample feature. Calls its own HTTP API (provided by
 * template.yaml), which queries the main stack's TrackingTable (via the
 * TypeDateIndex GSI, ItemType='document') and returns a {status -> count}
 * map. Rendered as a Cloudscape PieChart.
 */
const App: React.FC<FeatureContext> = ({
  featureApiEndpoint,
  getAuthToken,
  uiAccessAllowed,
  installedVersion,
}) => {
  const [window, setWindow] = useState<string>('');
  const [data, setData] = useState<CountsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    if (!featureApiEndpoint) {
      setError('No feature API endpoint configured.');
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const token = await getAuthToken();
      const qs = window ? `?window=${window}` : '';
      const resp = await fetch(`${featureApiEndpoint}/counts${qs}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!resp.ok) {
        throw new Error(`${resp.status} ${resp.statusText}`);
      }
      setData((await resp.json()) as CountsResponse);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [featureApiEndpoint, getAuthToken, window]);

  useEffect(() => {
    if (uiAccessAllowed) refresh();
  }, [refresh, uiAccessAllowed]);

  const pieData = useMemo(() => {
    if (!data) return [];
    return Object.entries(data.counts)
      .filter(([, n]) => n > 0)
      .map(([status, count]) => ({ title: status, value: count }));
  }, [data]);

  return (
    <Container
      header={
        <Header
          variant="h1"
          description={`Sample feature add-on · v${installedVersion} — live counts from the IDP tracking table`}
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Select
                selectedOption={WINDOW_OPTIONS.find((o) => o.value === window) ?? WINDOW_OPTIONS[0]}
                onChange={({ detail }) => setWindow(detail.selectedOption.value ?? '')}
                options={WINDOW_OPTIONS}
              />
              <Button onClick={refresh} loading={loading} disabled={!uiAccessAllowed}>
                Refresh
              </Button>
            </SpaceBetween>
          }
        >
          Sample: Document Status
        </Header>
      }
    >
      <SpaceBetween size="l">
        {loading && !data && <Spinner />}
        {error && <Alert type="error">{error}</Alert>}
        {data && (
          <>
            <KeyValuePairs
              columns={3}
              items={[
                { label: 'Total', value: String(data.total) },
                { label: 'Window', value: data.window === 'all' ? 'All time' : data.window },
                { label: 'As of', value: new Date(data.asOf).toLocaleString() },
              ]}
            />
            <Box padding="m">
              <PieChart
                data={pieData}
                detailPopoverContent={(datum, sum) => [
                  { key: 'Count', value: datum.value },
                  { key: 'Share', value: `${((datum.value / sum) * 100).toFixed(1)}%` },
                ]}
                segmentDescription={(datum, sum) =>
                  `${datum.value} (${((datum.value / sum) * 100).toFixed(0)}%)`
                }
                ariaLabel="Document status breakdown"
                hideFilter
                empty={<Box>No documents</Box>}
              />
            </Box>
          </>
        )}
      </SpaceBetween>
    </Container>
  );
};

export default App;
