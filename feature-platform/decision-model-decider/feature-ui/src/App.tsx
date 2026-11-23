// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from 'react';
import { Box, Container, Header, KeyValuePairs, SpaceBetween } from '@cloudscape-design/components';

import type { FeatureContext } from './types';

const DOCS_URL =
  'https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/blob/develop/docs/extensions/decision-model-decider.md';

/**
 * Decision Model: Strands Decider. The extension has no API of its own: it deploys a
 * Lambda function the pipeline calls, and a configuration profile that selects it.
 * This page says where both are and how to use them.
 */
const App: React.FC<FeatureContext> = ({ featureId, installedVersion, mainStackName }) => (
  <Container
    header={
      <Header variant="h1" description={`v${installedVersion} — a decision model your pipeline calls`}>
        Decision Model: Strands Decider
      </Header>
    }
  >
    <SpaceBetween size="l">
      <Box variant="p">
        This extension built the Strands Decider into a Lambda function in this account, from pinned source and
        model commits. Section classification and extraction confidence can call it in place of an LLM: it returns
        a probability for every class instead of generated text.
      </Box>
      <KeyValuePairs
        columns={2}
        items={[
          { label: 'Configuration profile', value: featureId },
          { label: 'Host stack', value: mainStackName },
          { label: 'Endpoint', value: 'The extension stack output DeciderEndpoint' },
        ]}
      />
      <Box variant="p">
        The installed configuration profile, named <Box variant="code">{featureId}</Box>, sets section
        classification to the decision model under the split-then-classify strategy, with this function as its
        endpoint. Installing it did not change the active configuration: review it on the Configuration page and
        activate it, or copy its section classification settings into the profile you use.
      </Box>
      <Box variant="p">
        <a href={DOCS_URL} target="_blank" rel="noopener noreferrer">
          Read the extension guide
        </a>{' '}
        for the model, its licence, cost, and the settings this stack takes.
      </Box>
    </SpaceBetween>
  </Container>
);

export default App;
