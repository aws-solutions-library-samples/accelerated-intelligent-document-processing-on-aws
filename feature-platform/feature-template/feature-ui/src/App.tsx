// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useEffect, useState, useCallback } from 'react';
import { Alert, Box, Button, Container, Header, SpaceBetween, Spinner } from '@cloudscape-design/components';

import type { FeatureContext } from './types';

interface ApiResponse {
  message: string;
  mainStackName: string;
}

/**
 * Example feature root component. Receives FeatureContext as a prop from the
 * host. Customise everything below — this is just a demo that calls the
 * feature's own API (described in ../template.yaml).
 */
const App: React.FC<FeatureContext> = ({
  featureId,
  installedVersion,
  featureApiEndpoint,
  getAuthToken,
  uiAccessAllowed,
}) => {
  const [data, setData] = useState<ApiResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const callApi = useCallback(async () => {
    if (!featureApiEndpoint) {
      setError('No feature API endpoint configured.');
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const token = await getAuthToken();
      const resp = await fetch(`${featureApiEndpoint}/hello`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!resp.ok) {
        throw new Error(`${resp.status} ${resp.statusText}`);
      }
      setData((await resp.json()) as ApiResponse);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [featureApiEndpoint, getAuthToken]);

  // `uiAccessAllowed` gates the UI so we don't fire requests the host has
  // already dimmed. That is ALL it is for.
  //
  // If you are building a PAID extension, do NOT treat this as your licence
  // check. It is a host-computed boolean handed to a browser in the customer's
  // own AWS account, and it reads `true` whenever subscription checks are
  // disabled (`auto`), simulated, or unreachable (`advisory`). Enforce in your
  // backend against your own seller-side check — see
  // docs/feature-platform-developer-guide.md → "Entitlement enforcement is the
  // extension's job".
  useEffect(() => {
    if (uiAccessAllowed) callApi();
  }, [callApi, uiAccessAllowed]);

  return (
    <Container
      header={
        <Header variant="h1" description={`Feature v${installedVersion} — ${featureId}`}>
          My Feature
        </Header>
      }
    >
      <SpaceBetween size="m">
        {loading && <Spinner />}
        {error && <Alert type="error">{error}</Alert>}
        {data && (
          <Box>
            <Box variant="awsui-key-label">Message</Box>
            <Box variant="p">{data.message}</Box>
            <Box variant="awsui-key-label">Main stack</Box>
            <Box variant="p">{data.mainStackName}</Box>
          </Box>
        )}
        <Button onClick={callApi} loading={loading} disabled={!uiAccessAllowed}>
          Refresh
        </Button>
      </SpaceBetween>
    </Container>
  );
};

export default App;
