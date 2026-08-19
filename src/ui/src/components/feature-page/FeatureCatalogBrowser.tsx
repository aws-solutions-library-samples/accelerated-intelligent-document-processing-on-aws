// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useMemo } from 'react';
import { Badge, Box, Container, Header, Link, SpaceBetween, Spinner } from '@cloudscape-design/components';

import useCatalogFeatures from '../../hooks/use-catalog-features';
import useInstalledFeatures from '../../hooks/use-installed-features';
import type { CatalogFeature, InstalledFeature } from '../../types/feature-platform';
import { featureDetailHref } from '../../routes/constants';

interface BrowserEntry {
  featureId: string;
  displayName: string;
  description: string | null;
  source: 'oss' | 'marketplace';
  installed: InstalledFeature | null;
  /** False only when the catalog says this extension isn't published here. */
  availableInRegion: boolean;
  /** Regions the extension IS published to; empty when not region-scoped. */
  availableRegions: string[];
}

function mergeCatalog(installed: InstalledFeature[], catalog: CatalogFeature[]): BrowserEntry[] {
  const byId = new Map<string, BrowserEntry>();

  for (const f of installed) {
    byId.set(f.featureId, {
      featureId: f.featureId,
      displayName: f.displayName,
      description: null,
      source: 'oss',
      installed: f,
      availableInRegion: true,
      availableRegions: [],
    });
  }

  for (const c of catalog) {
    // An older host doesn't send availableInRegion at all; absent means "no
    // region restriction known", so default to available rather than hiding a
    // working extension behind a warning we can't substantiate.
    const availableInRegion = c.availableInRegion !== false;
    const availableRegions = c.availableRegions ?? [];
    const existing = byId.get(c.featureId);
    if (existing) {
      existing.description = c.description ?? null;
      existing.source = c.source === 'marketplace' ? 'marketplace' : 'oss';
      existing.availableRegions = availableRegions;
      // An already-installed extension keeps working even if the catalog no
      // longer lists this region, so don't flag an install we can see running.
      existing.availableInRegion = existing.installed ? true : availableInRegion;
    } else {
      byId.set(c.featureId, {
        featureId: c.featureId,
        displayName: c.displayName,
        description: c.description ?? null,
        source: c.source === 'marketplace' ? 'marketplace' : 'oss',
        installed: null,
        availableInRegion,
        availableRegions,
      });
    }
  }

  return Array.from(byId.values()).sort((a, b) => a.displayName.toLowerCase().localeCompare(b.displayName.toLowerCase()));
}

function statusBadge(entry: BrowserEntry): React.ReactNode {
  if (entry.installed) {
    return entry.installed.updateAvailable ? <Badge color="blue">Update available</Badge> : <Badge color="green">Installed</Badge>;
  }
  if (!entry.availableInRegion) {
    return <Badge color="red">Not available in this Region</Badge>;
  }
  return entry.source === 'marketplace' ? (
    <Badge color="grey">Subscription required</Badge>
  ) : (
    <Badge color="blue">Available to install</Badge>
  );
}

/**
 * The `/features` (no featureId) page: lists every extension — installed and
 * available — with a link to each feature's own page, where an admin can
 * install / update / subscribe. Unlike the side nav, this page ignores
 * `showInNav`, so it's the discovery surface for reference samples and other
 * catalog features that keep themselves off the nav until installed.
 */
const FeatureCatalogBrowser = (): React.JSX.Element => {
  const { features: catalog, loading: catalogLoading } = useCatalogFeatures();
  const { features: installed, loading: installedLoading } = useInstalledFeatures();

  const entries = useMemo(() => mergeCatalog(installed, catalog), [installed, catalog]);

  if (catalogLoading || installedLoading) {
    return (
      <Box padding="xxl" textAlign="center">
        <Spinner size="large" />
      </Box>
    );
  }

  return (
    <Container
      header={
        <Header variant="h1" description="Installable add-ons that extend the IDP Accelerator. Open an extension to install or update it.">
          Extensions catalog
        </Header>
      }
    >
      {entries.length === 0 ? (
        <Box color="text-body-secondary">No extensions are available in this deployment.</Box>
      ) : (
        <SpaceBetween size="l">
          {entries.map((e) => (
            <Box key={e.featureId}>
              <SpaceBetween size="xs">
                <SpaceBetween direction="horizontal" size="xs">
                  <Link href={featureDetailHref(e.featureId)}>
                    <b>{e.displayName}</b>
                  </Link>
                  {statusBadge(e)}
                </SpaceBetween>
                {e.description && <Box color="text-body-secondary">{e.description}</Box>}
                {!e.availableInRegion && (
                  <Box color="text-status-inactive" fontSize="body-s">
                    {e.availableRegions.length > 0
                      ? `Available in ${e.availableRegions.join(', ')}. Deploy the accelerator in one of those Regions to install it.`
                      : 'This extension is not published for installation in this Region.'}
                  </Box>
                )}
              </SpaceBetween>
            </Box>
          ))}
        </SpaceBetween>
      )}
    </Container>
  );
};

export default FeatureCatalogBrowser;
