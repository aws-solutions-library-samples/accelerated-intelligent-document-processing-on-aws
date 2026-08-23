// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { useParams } from 'react-router-dom';
import { Badge, Box, HelpPanel, Icon, SpaceBetween, StatusIndicator } from '@cloudscape-design/components';

import useCatalogFeatures from '../../hooks/use-catalog-features';
import useInstalledFeatures from '../../hooks/use-installed-features';
import { resolveFeatureDocsUrl } from './feature-docs-url';

const DOCS_BASE_URL = 'https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws';

/**
 * Info (Tools) panel for the Extensions section — the standard right-side
 * `(i)` panel. Always shows the Extensions overview; when the user is on a
 * specific feature page (`/features/:featureId`) it additionally shows a
 * "Selected extension" section with that feature's name, status, description,
 * and a Learn more link.
 */
const FeaturesToolsPanel = (): React.JSX.Element => {
  const { featureId } = useParams<{ featureId?: string }>();
  const { byId: catalogById } = useCatalogFeatures();
  const { byId: installedById } = useInstalledFeatures();

  const catalog = featureId ? catalogById(featureId) : undefined;
  const installed = featureId ? installedById(featureId) : undefined;

  let selected: React.ReactNode = null;
  if (featureId) {
    const displayName = installed?.displayName ?? catalog?.displayName ?? featureId;
    const isMarketplace = catalog?.source === 'marketplace';
    const description = catalog?.description ?? null;
    const docsUrl = resolveFeatureDocsUrl(catalog ?? null);

    // Lifecycle status, mirroring the nav badges — and, like the nav, saying only
    // what this panel actually knows.
    //
    // It used to read "Subscribe to install" for any uninstalled marketplace
    // extension. That inference is wrong for exactly the customer it matters to:
    // someone who has already paid, and only needs to launch the stack, was told
    // to go and subscribe. The panel cannot know — it is built from
    // listCatalogFeatures + listInstalledFeatures, and neither carries an
    // entitlement verdict. That verdict comes from `checkFeatureEntitlement`,
    // which for a `marketplace-live` extension calls the AWS Marketplace
    // Agreement API; the feature page next to this panel has already made that
    // call and shows the answer. Mounting `useFeatureEntitlement` here as well
    // would duplicate a real Marketplace API call just to word one status line.
    //
    // So the status states installed-or-not, and the subscription requirement is
    // stated separately below as the property of the *extension* that it is —
    // same split, and same wording, as the nav's hover text.
    let status: { label: string; type: 'success' | 'info' | 'pending' | 'warning' };
    if (installed) {
      status = installed.updateAvailable ? { label: 'Update available', type: 'info' } : { label: 'Ready', type: 'success' };
    } else if (catalog?.availableInRegion === false) {
      // Not published for this Region: it cannot be installed here at all, which
      // outranks anything else the panel might say about installing it.
      status = { label: 'Not available in this Region', type: 'warning' };
    } else {
      status = { label: 'Not installed', type: 'pending' };
    }

    selected = (
      <div>
        <h3>Selected extension</h3>
        <SpaceBetween size="s">
          <Box>
            <SpaceBetween direction="horizontal" size="xs">
              <b>{displayName}</b>
              <Badge color={isMarketplace ? 'grey' : 'blue'}>{isMarketplace ? 'AWS Marketplace' : 'Open source'}</Badge>
            </SpaceBetween>
          </Box>
          <StatusIndicator type={status.type}>{status.label}</StatusIndicator>
          {installed && <Box color="text-body-secondary">Installed v{installed.installedVersion}</Box>}
          {/* A property of the extension, straight from the catalog — NOT a claim
              about whether this customer has a subscription, which the panel
              can't see. Dropped once installed: by then it's noise. */}
          {isMarketplace && !installed && <Box color="text-body-secondary">Requires an AWS Marketplace subscription.</Box>}
          {description && <Box>{description}</Box>}
          {docsUrl && (
            <Box>
              <a href={docsUrl} target="_blank" rel="noopener noreferrer">
                Learn more <Icon name="external" />
              </a>
            </Box>
          )}
        </SpaceBetween>
        <hr />
      </div>
    );
  }

  return (
    <HelpPanel
      header={<h2>Extensions</h2>}
      footer={
        <div>
          <h3>
            Learn more <Icon name="external" />
          </h3>
          <ul>
            <li>
              <a href={`${DOCS_BASE_URL}/feature-platform/`} target="_blank" rel="noopener noreferrer">
                Feature Platform
              </a>
            </li>
            <li>
              <a href={`${DOCS_BASE_URL}/feature-platform-developer-guide/`} target="_blank" rel="noopener noreferrer">
                Developer Guide — build an extension
              </a>
            </li>
          </ul>
        </div>
      }
    >
      {selected}
      <div>
        <p>
          Extensions are installable add-ons that extend the IDP Accelerator. Each runs as its own CloudFormation stack in this account and
          appears here once installed.
        </p>
        <h3>Two kinds</h3>
        <ul>
          <li>
            <b>Open source</b> — bundled with the accelerator and installable directly, at no additional cost beyond the AWS resources they
            create.
          </li>
          <li>
            <b>AWS Marketplace</b> — paid extensions delivered via an AWS Marketplace subscription. Subscribe on the listing, then install
            the extension into this stack.
          </li>
        </ul>
        <h3>Lifecycle</h3>
        <ul>
          <li>
            <b>Subscribe</b> — for a Marketplace extension, start an AWS Marketplace subscription before installing. You accept pricing, the
            seller licence terms, and the AWS Customer Agreement on the listing.
          </li>
          <li>
            <b>Install</b> — launch the extension&apos;s CloudFormation stack into this account. It registers itself back to this UI once
            deployed.
          </li>
          <li>
            <b>Ready</b> — installed and up to date; the extension&apos;s own page is live.
          </li>
          <li>
            <b>Update</b> — a newer version is available. Updating relaunches the same CloudFormation stack, so the extension is upgraded in
            place rather than installed a second time.
          </li>
        </ul>
        <p>
          Most extensions appear in the navigation; open <b>Browse catalog</b> to see the full catalog, including reference samples that
          don&apos;t get a navigation entry until installed. From an extension&apos;s page an admin can subscribe, install or update it, or
          open its documentation. Marketplace extensions are published per Region — one that isn&apos;t available in this stack&apos;s
          Region says so on its page instead of offering an install.
        </p>
      </div>
    </HelpPanel>
  );
};

export default FeaturesToolsPanel;
