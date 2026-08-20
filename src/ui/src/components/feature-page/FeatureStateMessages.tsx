// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from 'react';
import { Alert, Box, Button, Container, Header, Link, SpaceBetween, Spinner } from '@cloudscape-design/components';

import type { FeatureEntitlement } from '../../types/feature-platform';

/** "Learn more" external doc link, rendered when a docsUrl is available. */
export const LearnMore: React.FC<{ docsUrl?: string | null }> = ({ docsUrl }) =>
  docsUrl ? (
    <Box>
      <Link href={docsUrl} external externalIconAriaLabel="Opens in a new tab">
        Learn more
      </Link>
    </Box>
  ) : null;

/**
 * Region-unavailable state — the extension isn't published for this Region.
 *
 * Takes priority over `SubscriptionRequired`: offering Subscribe here would
 * dead-end, because even a valid subscription can't be installed. Marketplace
 * extensions are Region-scoped because `sam package` bakes an absolute,
 * Region-specific s3:// CodeUri into the published template, and a Lambda's code
 * bucket must live in the function's own Region.
 */
export const NotAvailableInRegion: React.FC<{
  featureDisplayName: string;
  description?: string | null;
  docsUrl?: string | null;
  /** Regions the extension IS published to. Empty when unknown. */
  availableRegions?: string[];
  /** Region this stack runs in, when the UI knows it. */
  currentRegion?: string | null;
  /** Public listing page, so the admin can still read about it. */
  marketplaceUrl?: string;
}> = ({ featureDisplayName, description, docsUrl, availableRegions, currentRegion, marketplaceUrl }) => (
  <Container
    header={
      <Header variant="h1" description={description || undefined}>
        {featureDisplayName}
      </Header>
    }
  >
    <SpaceBetween size="l">
      <Alert type="warning" header="Not available in this Region" statusIconAriaLabel="Warning">
        <b>{featureDisplayName}</b> isn&apos;t available {currentRegion ? <>in {currentRegion}</> : 'in this Region'}.{' '}
        {availableRegions && availableRegions.length > 0 ? (
          <>
            It can be installed in <b>{availableRegions.join(', ')}</b>. To use it, deploy the IDP Accelerator in one of those Regions.
          </>
        ) : (
          <>No Regions are currently published for this extension.</>
        )}
      </Alert>
      {marketplaceUrl && (
        <Box>
          <Button iconName="external" href={marketplaceUrl} target="_blank">
            View on AWS Marketplace
          </Button>
        </Box>
      )}
      <LearnMore docsUrl={docsUrl} />
    </SpaceBetween>
  </Container>
);

/** NONE state — no entitlement (marketplace features only). Admin sees an in-UI Subscribe button. */
export const SubscriptionRequired: React.FC<{
  featureDisplayName: string;
  /** Short feature description shown under the title. */
  description?: string | null;
  /** "Learn more" doc link (docs-site or marketplace listing). */
  docsUrl?: string | null;
  marketplaceUrl?: string;
  /** Admin-only: if true, render the in-UI Subscribe button. Non-admins see the marketplace link only. */
  canSubscribe?: boolean;
  /** Click handler for the in-UI Subscribe button (wired to useSubscribeFeature). */
  onSubscribe?: () => void;
  /** Loading indicator for the Subscribe button. */
  subscribing?: boolean;
  /** Error string from the last subscribe attempt (if any). */
  subscribeError?: string | null;
}> = ({ featureDisplayName, description, docsUrl, marketplaceUrl, canSubscribe, onSubscribe, subscribing, subscribeError }) => (
  <Container
    header={
      <Header variant="h1" description={description || 'This feature requires an active AWS Marketplace subscription.'}>
        {featureDisplayName}
      </Header>
    }
  >
    <SpaceBetween size="l">
      <Alert type="info" header="Subscription required" statusIconAriaLabel="Info">
        You don&apos;t have an active subscription for <b>{featureDisplayName}</b> yet. Click <b>Subscribe</b> to open the AWS Marketplace
        listing in a new tab, where you accept pricing, the seller EULA, and the AWS Customer Agreement. Once the subscription is active, an
        admin can install the extension into this IDP stack.
      </Alert>
      {subscribeError && (
        <Alert type="error" header="Failed to subscribe">
          {subscribeError}
        </Alert>
      )}
      <Box>
        <SpaceBetween direction="horizontal" size="s">
          {canSubscribe && onSubscribe && (
            <Button variant="primary" iconName="external" loading={subscribing} onClick={onSubscribe}>
              Subscribe
            </Button>
          )}
          {marketplaceUrl && (
            <Button variant={canSubscribe && onSubscribe ? 'normal' : 'primary'} iconName="external" href={marketplaceUrl} target="_blank">
              View on AWS Marketplace
            </Button>
          )}
        </SpaceBetween>
      </Box>
      <LearnMore docsUrl={docsUrl} />
    </SpaceBetween>
  </Container>
);

/** Installable (not yet installed) — admin sees this.
 *
 * For OSS features there is no subscription concept, so the wording is purely
 * about installing. For marketplace features (active subscription) the wording
 * notes the active subscription.
 */
export const InstallPrompt: React.FC<{
  featureDisplayName: string;
  description?: string | null;
  /** "Learn more" doc link. */
  docsUrl?: string | null;
  /** True for open-source features (no AWS Marketplace subscription). */
  isOss?: boolean;
  loading: boolean;
  onInstall: () => void;
  errorMessage: string | null;
}> = ({ featureDisplayName, description, docsUrl, isOss, loading, onInstall, errorMessage }) => (
  <Container
    header={
      <Header
        variant="h1"
        description={
          description ||
          (isOss
            ? 'Install this extension to add it to your IDP stack.'
            : 'Your subscription is active. Install the feature stack to unlock it.')
        }
      >
        {featureDisplayName}
      </Header>
    }
  >
    <SpaceBetween size="l">
      <Alert type={isOss ? 'info' : 'success'} header={isOss ? 'Ready to install' : 'Subscription active'}>
        {isOss ? (
          <>
            <b>{featureDisplayName}</b> is available to install. Install the feature stack into this account to start using it.
          </>
        ) : (
          <>
            Your AWS Marketplace subscription for <b>{featureDisplayName}</b> is active. Install the feature stack into this account to
            start using it.
          </>
        )}
      </Alert>
      {errorMessage && (
        <Alert type="error" header="Failed to build launch URL">
          {errorMessage}
        </Alert>
      )}
      <Box>
        <Button variant="primary" iconName="external" loading={loading} onClick={onInstall}>
          Launch stack in CloudFormation Console
        </Button>
      </Box>
      <Box color="text-body-secondary">
        The button opens the CloudFormation Console pre-filled with the feature&apos;s template and parameters. Review the parameters and
        click <b>Create stack</b> — the feature will register itself back to this UI once deployed (typically 2–3 minutes).
      </Box>
      <LearnMore docsUrl={docsUrl} />
    </SpaceBetween>
  </Container>
);

/** Installable but not yet installed — non-admin sees this. */
export const AwaitingAdminInstall: React.FC<{ featureDisplayName: string; docsUrl?: string | null; isOss?: boolean }> = ({
  featureDisplayName,
  docsUrl,
  isOss,
}) => (
  <Container
    header={
      <Header variant="h1" description="This feature has not been installed yet.">
        {featureDisplayName}
      </Header>
    }
  >
    <SpaceBetween size="l">
      <Alert type="warning" header="Awaiting installation">
        {isOss ? (
          <>
            <b>{featureDisplayName}</b> is available but has not been installed into this IDP stack yet. Ask an IDP administrator to install
            it.
          </>
        ) : (
          <>
            Your AWS Marketplace subscription for <b>{featureDisplayName}</b> is active, but the feature stack has not been installed into
            this IDP stack yet. Ask an IDP administrator to install it.
          </>
        )}
      </Alert>
      <LearnMore docsUrl={docsUrl} />
    </SpaceBetween>
  </Container>
);

/** Installed, version matches latest.
 *
 * For OSS / auto-subscribe features (source 'auto') there is no Marketplace
 * subscription, so we show a plain "up to date" with no source suffix. For
 * marketplace/simulator subscriptions we annotate the source.
 */
export const UpToDateBanner: React.FC<{ version: string; source: string }> = ({ version, source }) => {
  const showSource = source !== 'auto';
  return (
    <Alert type="success" statusIconAriaLabel="Active" dismissible={false}>
      v{version} — up to date{showSource ? ` (${source})` : ''}
    </Alert>
  );
};

/** ACTIVE + installed, newer version available. */
export const UpdateAvailableBanner: React.FC<{
  installedVersion: string;
  latestVersion: string;
  isAdmin: boolean;
  onUpdate?: () => void;
  loading?: boolean;
}> = ({ installedVersion, latestVersion, isAdmin, onUpdate, loading }) => (
  <Alert
    type="info"
    header={`Update available: v${latestVersion}`}
    action={
      isAdmin && onUpdate ? (
        <Button loading={loading} onClick={onUpdate}>
          Update
        </Button>
      ) : undefined
    }
  >
    You are running <b>v{installedVersion}</b>. Version <b>v{latestVersion}</b> is available.
    {!isAdmin && ' Ask your admin to install the update.'}
  </Alert>
);

/** EXPIRED entitlement — feature UI is shown but wrapped in a dimming overlay. */
export const ExpiredBanner: React.FC<{
  featureDisplayName: string;
  marketplaceUrl?: string;
}> = ({ featureDisplayName, marketplaceUrl }) => (
  <Alert
    type="error"
    header="Subscription expired"
    action={
      marketplaceUrl ? (
        <Button iconName="external" href={marketplaceUrl} target="_blank" variant="primary">
          Renew
        </Button>
      ) : undefined
    }
  >
    Your AWS Marketplace subscription for <b>{featureDisplayName}</b> has expired. The feature is shown in read-only mode. Renew on AWS
    Marketplace to restore full access.
  </Alert>
);

/** Human-friendly rendering of an ISO-8601 timestamp (falls back to raw string on parse failure). */
function formatDate(iso: string | null): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

/**
 * ACTIVE + installed status banner — renders above the feature UI.
 *
 * Shows the subscription source (marketplace | simulator) and expiry, plus an
 * admin-only "Cancel Subscription" button that invokes `unsubscribeFeature`
 * server-side (flips entitlement to EXPIRED).
 */
export const ActiveSubscriptionBanner: React.FC<{
  entitlement: FeatureEntitlement;
  /** Admin-only: if true, render the Cancel Subscription button. */
  canCancel?: boolean;
  /** Click handler wired to useUnsubscribeFeature. */
  onCancel?: () => void;
  /** Loading indicator for the Cancel button. */
  cancelling?: boolean;
  /** Error string from the last cancel attempt (if any). */
  cancelError?: string | null;
}> = ({ entitlement, canCancel, onCancel, cancelling, cancelError }) => {
  const expires = formatDate(entitlement.expiresAt);
  const source = entitlement.source ?? 'marketplace';
  const header = expires ? `Subscription active · expires ${expires}` : 'Subscription active';
  return (
    <Alert
      type="success"
      header={header}
      statusIconAriaLabel="Subscription active"
      action={
        canCancel && onCancel ? (
          <Button loading={cancelling} onClick={onCancel}>
            Cancel Subscription
          </Button>
        ) : undefined
      }
    >
      Source: <b>{source}</b>
      {cancelError && (
        <Box margin={{ top: 's' }}>
          <Alert type="error" header="Failed to cancel subscription">
            {cancelError}
          </Alert>
        </Box>
      )}
    </Alert>
  );
};

/** Generic loading block (used while entitlement/install state resolve). */
export const LoadingBlock: React.FC = () => (
  <Box textAlign="center" padding="xxl">
    <Spinner size="large" />
    <Box padding="s" color="text-body-secondary">
      Checking subscription…
    </Box>
  </Box>
);
