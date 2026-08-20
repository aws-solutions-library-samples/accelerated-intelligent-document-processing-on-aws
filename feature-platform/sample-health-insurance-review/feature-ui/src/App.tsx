// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Container,
  Header,
  SpaceBetween,
  Tabs,
} from '@cloudscape-design/components';

import type { FeatureContext } from './types';
import { createApiClient } from './api';
import ClaimsDashboardView from './ClaimsDashboardView';
import RulesDiscoveryView from './RulesDiscoveryView';

// Compile-time constant injected by Vite from feature.yaml -> version. The
// config preset the feature installs is named `sample-health-insurance-review-v<version>`
// (see apply_feature_config_preset on the host), so the Rules Discovery view
// writes discovered rules into that same version.
declare const __FEATURE_VERSION__: string;

/**
 * Sample: Health Insurance Review. Two tabs:
 *   1. Claims Dashboard — lists processed claims with deterministic status
 *      and per-rule results (its own HTTP API over the ClaimsStatus table).
 *   2. Rules Discovery — drives the host's Rules Discovery flow to extract
 *      validation rules from a payer policy document (host AppSync mutations).
 */
const App: React.FC<FeatureContext> = ({
  featureApiEndpoint,
  getAuthToken,
  uiAccessAllowed,
  installedVersion,
}) => {
  const api = useMemo(
    () => createApiClient(featureApiEndpoint, getAuthToken),
    [featureApiEndpoint, getAuthToken],
  );
  const [discoveryBucket, setDiscoveryBucket] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState('claims');

  // Config version the bundled preset was installed as.
  const configVersion = `sample-health-insurance-review-v${installedVersion || __FEATURE_VERSION__}`;

  useEffect(() => {
    if (!uiAccessAllowed) return;
    api
      .getConfig()
      .then((c) => setDiscoveryBucket(c.discoveryBucket))
      .catch(() => setDiscoveryBucket(null));
  }, [api, uiAccessAllowed]);

  return (
    <Container
      header={
        <Header
          variant="h1"
          description={`Sample use-case add-on · v${installedVersion} — health insurance claims review on the IDP rule-validation pipeline`}
        >
          Sample: Health Insurance Review
        </Header>
      }
    >
      <SpaceBetween size="l">
        {!uiAccessAllowed && (
          <Alert type="info" header="Read-only">
            This feature&apos;s subscription is not active. Views are shown but
            data is not loaded.
          </Alert>
        )}
        <Tabs
          activeTabId={activeTab}
          onChange={({ detail }) => setActiveTab(detail.activeTabId)}
          tabs={[
            {
              id: 'claims',
              label: 'Claims Dashboard',
              content: (
                <ClaimsDashboardView api={api} enabled={uiAccessAllowed} />
              ),
            },
            {
              id: 'discovery',
              label: 'Rules Discovery',
              content: (
                <RulesDiscoveryView
                  discoveryBucket={discoveryBucket}
                  configVersion={configVersion}
                  enabled={uiAccessAllowed}
                />
              ),
            },
          ]}
        />
      </SpaceBetween>
    </Container>
  );
};

export default App;
