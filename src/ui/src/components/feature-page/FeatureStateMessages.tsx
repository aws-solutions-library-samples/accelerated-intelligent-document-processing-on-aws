// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React from 'react';
import { Alert, Box, Button, Container, Header, Link, SpaceBetween, Spinner } from '@cloudscape-design/components';

import type { FeatureEntitlement, FeatureLicenseMode } from '../../types/feature-platform';
import { sourceDisplayLabel } from './entitlement-source';

/**
 * The buyer's own AWS Marketplace subscription management console.
 *
 * Deliberately NOT the product listing page: a listing sells a subscription,
 * whereas "manage" means view the agreement, change the offer, or cancel — all of
 * which live in the buyer account's Marketplace console. A real AWS Marketplace
 * subscription cannot be cancelled from this UI at all (the in-UI Cancel button
 * drives the simulator's admin API), so this link is the only honest path to it.
 *
 * Region-free on purpose: AWS Marketplace is a us-east-1 service and the console
 * redirects there from wherever the buyer is signed in. Same URL the
 * "After subscribing on AWS Marketplace" doc page points at, so the two agree.
 */
export const MARKETPLACE_SUBSCRIPTIONS_CONSOLE_URL = 'https://console.aws.amazon.com/marketplace/home#/subscriptions';

/**
 * Link out to the buyer's AWS Marketplace subscriptions console.
 *
 * Rendered on every state that has established the extension is PAID and the
 * customer has (or may have) a subscription — active-and-installed,
 * active-but-not-installed, and awaiting-admin-install alike. A customer who has
 * paid needs the same one thing from all of them: somewhere to go and look at,
 * change, or cancel what they are paying for.
 *
 * Deliberately not gated on the IDP `Admin` group. Viewing and cancelling a
 * Marketplace subscription happens in the AWS console under the caller's own IAM
 * permissions — an IDP viewer may well be the account's billing owner, and the
 * link grants nothing by existing.
 *
 * NOT shown on the NONE state, which needs the *listing* (where a subscription
 * is created) rather than the management console.
 */
export const ManageSubscriptionButton: React.FC = () => (
  <Button iconName="external" href={MARKETPLACE_SUBSCRIPTIONS_CONSOLE_URL} target="_blank">
    Manage subscription
  </Button>
);

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
  /**
   * The authority this extension is checked against. When it is
   * `marketplace-live` the in-UI Subscribe button is suppressed: that button
   * drives `subscribeFeature`, which on a simulator-configured stack redirects to
   * the simulator's buyer console — a subscription the extension will ignore,
   * because it only honours real AWS Marketplace. The listing link below is the
   * only real path, and a real subscription can only be created there anyway.
   */
  licenseMode?: FeatureLicenseMode | null;
  /** Click handler for the in-UI Subscribe button (wired to useSubscribeFeature). */
  onSubscribe?: () => void;
  /** Loading indicator for the Subscribe button. */
  subscribing?: boolean;
  /** Error string from the last subscribe attempt (if any). */
  subscribeError?: string | null;
}> = ({
  featureDisplayName,
  description,
  docsUrl,
  marketplaceUrl,
  canSubscribe,
  licenseMode,
  onSubscribe,
  subscribing,
  subscribeError,
}) => {
  const offerInUiSubscribe = canSubscribe && !!onSubscribe && licenseMode !== 'marketplace-live';
  return (
    <Container
      header={
        <Header variant="h1" description={description || 'This feature requires an active AWS Marketplace subscription.'}>
          {featureDisplayName}
        </Header>
      }
    >
      <SpaceBetween size="l">
        <Alert type="info" header="Subscription required" statusIconAriaLabel="Info">
          You don&apos;t have an active subscription for <b>{featureDisplayName}</b> yet.{' '}
          {offerInUiSubscribe ? (
            <>
              Click <b>Subscribe</b> to open the AWS Marketplace listing in a new tab, where you accept pricing, the seller EULA, and the
              AWS Customer Agreement.
            </>
          ) : marketplaceUrl ? (
            <>
              Click <b>View on AWS Marketplace</b> to accept pricing, the seller EULA, and the AWS Customer Agreement. A real AWS
              Marketplace subscription can only be created there.
            </>
          ) : (
            // No in-UI Subscribe and no listing URL means there is no way forward
            // from this screen. Say so rather than rendering a bare alert with no
            // buttons and leaving the admin to guess — which is exactly what
            // happened while the listing URL wasn't being read from the catalog.
            <>
              This stack has no AWS Marketplace listing URL registered for <b>{featureDisplayName}</b>, so there is no link to offer here.
              Add <b>marketplaceListingUrl</b> to its entry in <b>config_library/extensions-marketplace.yaml</b> and republish.
            </>
          )}{' '}
          Once the subscription is active, an admin can install the extension into this IDP stack.
        </Alert>
        {subscribeError && (
          <Alert type="error" header="Failed to subscribe">
            {subscribeError}
          </Alert>
        )}
        <Box>
          <SpaceBetween direction="horizontal" size="s">
            {offerInUiSubscribe && (
              <Button variant="primary" iconName="external" loading={subscribing} onClick={onSubscribe}>
                Subscribe
              </Button>
            )}
            {marketplaceUrl && (
              <Button variant={offerInUiSubscribe ? 'normal' : 'primary'} iconName="external" href={marketplaceUrl} target="_blank">
                View on AWS Marketplace
              </Button>
            )}
          </SpaceBetween>
        </Box>
        <LearnMore docsUrl={docsUrl} />
      </SpaceBetween>
    </Container>
  );
};

/** Installable (not yet installed) — admin sees this.
 *
 * For OSS features there is no subscription concept, so the wording is purely
 * about installing. For marketplace features the wording follows whether the
 * subscription was actually verified — claiming "your subscription is active"
 * off the back of an `auto` / `advisory` / `simulated` grant is the same lie the
 * installed page used to tell, one screen earlier.
 */
export const InstallPrompt: React.FC<{
  featureDisplayName: string;
  description?: string | null;
  /** "Learn more" doc link. */
  docsUrl?: string | null;
  /** True for open-source features (no AWS Marketplace subscription). */
  isOss?: boolean;
  /** True when access is being allowed without a confirmed subscription. */
  unverified?: boolean;
  loading: boolean;
  onInstall: () => void;
  errorMessage: string | null;
}> = ({ featureDisplayName, description, docsUrl, isOss, unverified, loading, onInstall, errorMessage }) => (
  <Container
    header={
      <Header
        variant="h1"
        description={
          description ||
          (isOss
            ? 'Install this extension to add it to your IDP stack.'
            : unverified
              ? 'Your subscription could not be confirmed. You can still install the feature stack.'
              : 'Your subscription is active. Install the feature stack to unlock it.')
        }
      >
        {featureDisplayName}
      </Header>
    }
  >
    <SpaceBetween size="l">
      <Alert
        type={isOss ? 'info' : unverified ? 'warning' : 'success'}
        header={isOss ? 'Ready to install' : unverified ? 'Subscription not verified' : 'Subscription active'}
        // Same action as the installed-and-active banner. This screen states the
        // subscription is active, so it owes the customer the same route to
        // managing it — not installing the stack yet doesn't make the
        // subscription any less real, or any less billable. OSS has none.
        action={isOss ? undefined : <ManageSubscriptionButton />}
      >
        {isOss ? (
          <>
            <b>{featureDisplayName}</b> is available to install. Install the feature stack into this account to start using it.
          </>
        ) : unverified ? (
          <>
            The host could not confirm an AWS Marketplace subscription for <b>{featureDisplayName}</b>, and is allowing installation anyway.
            The extension performs its own subscription check at runtime, so it may still refuse to work until you subscribe.
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
export const AwaitingAdminInstall: React.FC<{
  featureDisplayName: string;
  docsUrl?: string | null;
  isOss?: boolean;
  /** True when access is being allowed without a confirmed subscription. */
  unverified?: boolean;
}> = ({ featureDisplayName, docsUrl, isOss, unverified }) => (
  <Container
    header={
      <Header variant="h1" description="This feature has not been installed yet.">
        {featureDisplayName}
      </Header>
    }
  >
    <SpaceBetween size="l">
      <Alert
        type="warning"
        header="Awaiting installation"
        // Offered to non-admins too: the subscription is an account-level thing,
        // billed whether or not an IDP admin has got round to installing the
        // stack, and the AWS console enforces its own permissions.
        action={isOss ? undefined : <ManageSubscriptionButton />}
      >
        {isOss ? (
          <>
            <b>{featureDisplayName}</b> is available but has not been installed into this IDP stack yet. Ask an IDP administrator to install
            it.
          </>
        ) : unverified ? (
          <>
            <b>{featureDisplayName}</b> has not been installed into this IDP stack yet, and the host could not confirm an AWS Marketplace
            subscription for it. Ask an IDP administrator to install it.
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
 * Deliberately says nothing about the subscription. It used to append the
 * entitlement source — rendering "v0.5.1 — up to date (advisory)" as a third
 * banner in a stack that already disagreed with itself about whether the
 * subscription was active. The version and the subscription are separate facts;
 * the subscription is stated exactly once, by the banner above this one.
 */
export const UpToDateBanner: React.FC<{ version: string }> = ({ version }) => (
  <Alert type="success" statusIconAriaLabel="Active" dismissible={false}>
    v{version} — up to date
  </Alert>
);

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
/**
 * Shown when the host is granting access to a PAID extension without having
 * verified a subscription — `auto` (checks off), `advisory` (check unreachable,
 * allowed rather than locked out), or `simulated` (aimed at a simulator).
 *
 * This state is otherwise invisible: the page looks identical to a real
 * subscription. Making it visible is the point — it removes the plausible
 * deniability of running a paid extension unsubscribed by accident, and it tells
 * an admin who *is* paying that their entitlement isn't being confirmed (usually
 * a missing `aws-marketplace:SearchAgreements` permission) so they can fix it.
 *
 * This is the ONLY subscription banner an unverified state renders. It used to
 * appear directly above a green `ActiveSubscriptionBanner` reading "Subscription
 * active", so the page asserted both "not verified" and "active" at once — worse
 * than either alone, and it made the extension's own honest "no subscription
 * found" panel read as a third opinion rather than the answer. Hence the header
 * states what actually happened and names the source, and
 * `FeaturePage` renders the two banners mutually exclusively.
 *
 * Deliberately a warning, not an error, and it blocks nothing: the host gate is
 * advisory by design, and blocking here would lock out a paying customer whenever
 * the Marketplace API had a bad day.
 */
export const UnverifiedSubscriptionBanner: React.FC<{
  featureDisplayName: string;
  /** The reported source, shown verbatim so the page agrees with `entitlementSource`. */
  source: FeatureEntitlement['source'];
  /** Why it's unverified — from `unverifiedReason(source)`. */
  reason: string;
  /** Optional operator note from `licenseModeMismatchNote()`. */
  mismatchNote?: string | null;
  /** The listing, so an admin can go and subscribe properly. */
  marketplaceUrl?: string;
  /**
   * Admin-only, simulator-backed subscriptions only. The Cancel action lives
   * HERE rather than on a second banner: this is the only subscription banner an
   * unverified state renders, so the action has to travel with it or the
   * simulator dev loop loses its off switch.
   */
  canCancel?: boolean;
  onCancel?: () => void;
  cancelling?: boolean;
  cancelError?: string | null;
}> = ({ featureDisplayName, source, reason, mismatchNote, marketplaceUrl, canCancel, onCancel, cancelling, cancelError }) => (
  <Alert type="warning" header={`Access allowed without a verified subscription · source: ${source}`} statusIconAriaLabel="Warning">
    <SpaceBetween size="s">
      <Box variant="span">
        Access to <b>{featureDisplayName}</b> is being allowed without a confirmed AWS Marketplace subscription. {reason}
      </Box>
      {mismatchNote && <Box variant="span">{mismatchNote}</Box>}
      {(marketplaceUrl || (canCancel && onCancel)) && (
        <Box>
          <SpaceBetween direction="horizontal" size="s">
            {marketplaceUrl && (
              <Button iconName="external" href={marketplaceUrl} target="_blank">
                View subscription on AWS Marketplace
              </Button>
            )}
            {canCancel && onCancel && (
              <Button loading={cancelling} onClick={onCancel}>
                Cancel Subscription
              </Button>
            )}
          </SpaceBetween>
        </Box>
      )}
      {cancelError && (
        <Alert type="error" header="Failed to cancel subscription">
          {cancelError}
        </Alert>
      )}
    </SpaceBetween>
  </Alert>
);

export const ActiveSubscriptionBanner: React.FC<{
  entitlement: FeatureEntitlement;
  /**
   * Optional operator note from `licenseModeMismatchNote()`. A stale catalog entry
   * is worth saying even when the subscription itself verified fine — otherwise
   * the only place it appears is the resolver's logs.
   */
  mismatchNote?: string | null;
  /** Admin-only: if true, render the Cancel Subscription button. */
  canCancel?: boolean;
  /** Click handler wired to useUnsubscribeFeature. */
  onCancel?: () => void;
  /** Loading indicator for the Cancel button. */
  cancelling?: boolean;
  /** Error string from the last cancel attempt (if any). */
  cancelError?: string | null;
}> = ({ entitlement, mismatchNote, canCancel, onCancel, cancelling, cancelError }) => {
  const expires = formatDate(entitlement.expiresAt);
  // The authority, named the way the customer knows it. The raw
  // `marketplace-live` identifier belongs in the *unverified* banner, where
  // naming the exact mode is the whole point; a confirmed subscription should say
  // which product it came from.
  const source = sourceDisplayLabel(entitlement.source ?? undefined);
  // One line. The source used to occupy a second row as "Source:
  // marketplace-live"; parenthesising it into the header says the same thing in
  // the space the status already took.
  const header = expires ? `Subscription active (${source}) · expires ${expires}` : `Subscription active (${source})`;
  const showBody = !!mismatchNote || !!cancelError;
  return (
    <Alert
      type="success"
      header={header}
      statusIconAriaLabel="Subscription active"
      action={
        // "Manage subscription" points at the buyer's own AWS Marketplace
        // console, the only place a real subscription can be inspected, changed
        // or cancelled. It lives in the action slot so the banner stays one line.
        // The in-UI Cancel button drives `unsubscribeFeature`, which only works
        // against the simulator, so it is still offered for simulated grants
        // only (see `canCancelSubscription` in FeaturePage).
        <SpaceBetween direction="horizontal" size="xs">
          <ManageSubscriptionButton />
          {canCancel && onCancel && (
            <Button loading={cancelling} onClick={onCancel}>
              Cancel Subscription
            </Button>
          )}
        </SpaceBetween>
      }
    >
      {/* Normally renders nothing — these are the exceptional cases they name. */}
      {showBody && (
        <SpaceBetween size="s">
          {mismatchNote && <Box variant="span">{mismatchNote}</Box>}
          {cancelError && (
            <Alert type="error" header="Failed to cancel subscription">
              {cancelError}
            </Alert>
          )}
        </SpaceBetween>
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
