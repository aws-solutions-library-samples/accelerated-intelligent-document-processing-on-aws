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

import { createApiClient } from './api';
import ConfigPairingView from './ConfigPairingView';
import RedactionReportView from './RedactionReportView';
import type { FeatureContext } from './types';

/**
 * PII Anonymization. Two tabs:
 *   1. Config Pairing — clone an existing config version into a redaction pair
 *      (initiating + companion) and optionally activate it. Uses host GraphQL.
 *   2. Redaction Report — metadata-only audit of redacted documents (feature API).
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
  const [activeTab, setActiveTab] = useState('pairing');
  const [hookArn, setHookArn] = useState<string | null>(null);

  useEffect(() => {
    if (!uiAccessAllowed) return;
    api
      .getConfig()
      .then((c) => setHookArn(c.hookFunctionArn))
      .catch(() => setHookArn(null));
  }, [api, uiAccessAllowed]);

  return (
    <Container
      header={
        <Header
          variant="h1"
          description={`v${installedVersion} — redact PII from documents before the classification/extraction models see them`}
        >
          PII Anonymization
        </Header>
      }
    >
      <SpaceBetween size="l">
        <Alert type="warning" header="Experimental feature">
          PII Anonymization is <b>experimental and unproven</b>. Try it, but
          validate the redacted output yourself and do not rely on it as a sole
          PII control. We want your feedback — what works, what&apos;s missing,
          and the use cases you need — via GitHub issues at{' '}
          <b>github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws</b>.
        </Alert>
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
              id: 'pairing',
              label: 'Config Pairing',
              content: (
                <ConfigPairingView
                  enabled={uiAccessAllowed}
                  hookFunctionArn={hookArn}
                />
              ),
            },
            {
              id: 'report',
              label: 'Redaction Report',
              content: <RedactionReportView api={api} enabled={uiAccessAllowed} />,
            },
          ]}
        />
      </SpaceBetween>
    </Container>
  );
};

export default App;
