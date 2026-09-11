// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useCallback, useEffect, useMemo, useRef } from 'react';
import { useParams } from 'react-router-dom';
import { SpaceBetween } from '@cloudscape-design/components';
import { fetchSharedAuthSession } from '../../api/auth-session';

import useInstalledFeatures from '../../hooks/use-installed-features';
import useCatalogFeatures from '../../hooks/use-catalog-features';
import useFeatureEntitlement from '../../hooks/use-feature-entitlement';
import useFeatureLaunchUrl from '../../hooks/use-feature-launch-url';
import useSubscribeFeature from '../../hooks/use-subscribe-feature';
import useUnsubscribeFeature from '../../hooks/use-unsubscribe-feature';
import type { FeatureContext } from '../../types/feature-platform';
// Region this stack runs in — used only to name the Region in the
// "not available in <region>" message. The server-side catalog resolver is
// authoritative about availability; this is display text.
import { awsRegion } from '../../aws-exports';

import FeatureLoader from './FeatureLoader';
import FeatureCatalogBrowser from './FeatureCatalogBrowser';
import { resolveFeatureDocsUrl } from './feature-docs-url';
import { isUnverifiedGrant, isVerifiedEntitlement, licenseModeMismatchNote, unverifiedReason } from './entitlement-source';
import {
  ActiveSubscriptionBanner,
  AwaitingAdminInstall,
  ExpiredBanner,
  InstallPrompt,
  LearnMore,
  LoadingBlock,
  NotAvailableInRegion,
  SubscriptionRequired,
  UnverifiedSubscriptionBanner,
  UpToDateBanner,
  UpdateAvailableBanner,
} from './FeatureStateMessages';

export interface FeaturePageProps {
  /**
   * Override the featureId from the URL. Useful for embedding FeaturePage in
   * other layouts or for tests. Defaults to `useParams().featureId`.
   */
  featureIdOverride?: string;
  /** Cognito groups of the current user (from useUserRole).  Empty = anonymous. */
  groups: string[];
  /** Name of the main IDP stack (passed to features via FeatureContext). */
  mainStackName: string;
  /**
   * Optional map of featureId -> marketplace listing URL. An OVERRIDE only —
   * the catalog entry's `marketplaceListingUrl` is the real source, so leaving
   * this unset is the normal case (no route passes it).
   */
  marketplaceUrls?: Record<string, string>;
}

async function getAuthToken(): Promise<string> {
  const session = await fetchSharedAuthSession();
  const jwt = session.tokens?.idToken?.toString();
  if (!jwt) throw new Error('No Cognito idToken available');
  return jwt;
}

/**
 * The 7-state FeaturePage renderer — implements the state machine documented
 * in subscription-features/feature-platform/ui-extensions/README.md.
 *
 * State table:
 *
 *   | Entitlement | Installed | Role    | UI                          |
 *   |-------------|-----------|---------|-----------------------------|
 *   | NONE        | any       | any     | SubscriptionRequired        |
 *   | ACTIVE      | no        | admin   | InstallPrompt               |
 *   | ACTIVE      | no        | non-adm | AwaitingAdminInstall        |
 *   | ACTIVE      | yes, =v   | any     | Feature UI + UpToDate       |
 *   | ACTIVE      | yes, <v   | admin   | Feature UI + UpdateAvailable|
 *   | ACTIVE      | yes, <v   | non-adm | Feature UI + UpdateAvailable (no btn) |
 *   | EXPIRED     | yes       | any     | Feature UI blurred + Renew  |
 */
const FeaturePage: React.FC<FeaturePageProps> = ({ featureIdOverride, groups, mainStackName, marketplaceUrls }) => {
  const params = useParams<{ featureId?: string }>();
  const featureId = featureIdOverride ?? params.featureId ?? '';
  const isAdmin = groups.includes('Admin');

  const { loading: installedLoading, byId, refresh: refreshInstalled } = useInstalledFeatures();
  const { byId: catalogById } = useCatalogFeatures();
  const { entitlement, loading: entitlementLoading, refresh: refreshEntitlement } = useFeatureEntitlement(featureId);
  const { fetch: fetchLaunchUrl, loading: launchLoading, error: launchError } = useFeatureLaunchUrl();
  const { subscribe, loading: subscribing, error: subscribeError } = useSubscribeFeature();
  const { unsubscribe, loading: cancelling, error: cancelError } = useUnsubscribeFeature();

  const installed = useMemo(() => byId(featureId), [byId, featureId]);
  const catalogEntry = useMemo(() => catalogById(featureId), [catalogById, featureId]);
  // Resolve ONCE, with the catalog as the fallback. The prop-only form was
  // always undefined in production — no route passes `marketplaceUrls` — and
  // only two of the five call sites below had a `?? catalogEntry` fallback, so
  // the "View on AWS Marketplace" button never rendered in the NONE, EXPIRED or
  // not-installed states. On a `marketplace-live` extension, where the in-UI
  // Subscribe button is deliberately suppressed because it drives the simulator,
  // that left an admin who genuinely needed to subscribe with no button at all.
  const marketplaceUrl = marketplaceUrls?.[featureId] ?? catalogEntry?.marketplaceListingUrl ?? undefined;

  const handleInstall = useCallback(async () => {
    try {
      const urlInfo = await fetchLaunchUrl(featureId);
      // Open in a new tab so the user can come back to the IDP UI after Create stack.
      window.open(urlInfo.launchUrl, '_blank', 'noopener,noreferrer');
    } catch {
      // error is surfaced via the hook's `error` state
    }
  }, [fetchLaunchUrl, featureId]);

  const handleUpdate = useCallback(async () => {
    // Update == Launch Stack with the latest version. Same CFN quick-create
    // URL mechanism; because the stackName is preserved by the server-side
    // resolver, this performs an Update Stack.
    try {
      const urlInfo = await fetchLaunchUrl(featureId);
      window.open(urlInfo.launchUrl, '_blank', 'noopener,noreferrer');
    } catch {
      // error surfaced via hook
    }
  }, [fetchLaunchUrl, featureId]);

  // Tracks whether we're waiting for the admin to return from the
  // Marketplace / simulator tab. When true, the next `window.focus` event
  // triggers a one-shot entitlement refresh so the UI transitions from
  // NONE → ACTIVE without the admin having to manually reload.
  const awaitingMarketplaceReturn = useRef(false);

  const handleSubscribe = useCallback(async () => {
    try {
      const currentUrl = typeof window !== 'undefined' ? window.location.href : undefined;
      const subResult = await subscribe(featureId, { returnUrl: currentUrl });
      if (subResult.marketplaceUrl) {
        // Real AWS Marketplace does not expose subscribe as a silent RPC — the
        // buyer is redirected to a Marketplace-hosted page to accept pricing,
        // EULA, and the AWS Customer Agreement. We do the same via the
        // simulator's /marketplace/* HTML buyer console, then refresh the
        // entitlement state when the admin returns to this tab.
        awaitingMarketplaceReturn.current = true;
        window.open(subResult.marketplaceUrl, '_blank', 'noopener,noreferrer');
      } else {
        // No redirect URL (shouldn't happen — hook throws otherwise). Fall
        // back to the old behaviour of just refreshing caches.
        await Promise.all([refreshEntitlement(), refreshInstalled()]);
      }
    } catch {
      // error surfaced via hook's `subscribeError`
    }
  }, [subscribe, featureId, refreshEntitlement, refreshInstalled]);

  // Refresh entitlement + installed state when the admin returns to this tab
  // after completing (or cancelling) the Marketplace / simulator flow.
  useEffect(() => {
    const onFocus = () => {
      if (!awaitingMarketplaceReturn.current) return;
      awaitingMarketplaceReturn.current = false;
      // Fire-and-forget — consumers don't await these.
      void Promise.all([refreshEntitlement(), refreshInstalled()]);
    };
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
  }, [refreshEntitlement, refreshInstalled]);

  const handleCancel = useCallback(async () => {
    try {
      await unsubscribe(featureId);
      // Invalidate both caches so the UI transitions to EXPIRED.
      await Promise.all([refreshEntitlement(), refreshInstalled()]);
    } catch {
      // error surfaced via hook's `cancelError`
    }
  }, [unsubscribe, featureId, refreshEntitlement, refreshInstalled]);

  if (!featureId) {
    // /features with no id: the catalog browser — the discovery surface for
    // available (not-yet-installed) extensions, which don't get nav links.
    return <FeatureCatalogBrowser />;
  }
  if (installedLoading || entitlementLoading) {
    return <LoadingBlock />;
  }

  const state = entitlement?.state ?? 'NONE';
  // Prefer installed.displayName (most accurate — what the feature's own
  // RegisterFeature custom resource wrote when it deployed). Fall back to
  // catalog.displayName (what the UI's hardcoded feature registry lists).
  // Fall back to raw featureId as a last resort so the page still renders.
  const featureDisplayName = installed?.displayName ?? catalogEntry?.displayName ?? featureId;
  // OSS features have no AWS Marketplace contract — drive the subscription
  // wording off the catalog `source` (auto-subscribe mode also reports
  // source='auto', covered separately for the installed banner). When the
  // catalog entry is missing we fall back to NOT treating it as OSS so the
  // marketplace-safe wording is the default.
  const isOss = catalogEntry?.source === 'oss';
  const featureDescription = catalogEntry?.description ?? null;
  // "Learn more" link: docs-site slug/URL from the catalog, else the
  // marketplace listing. Null when neither is available.
  const docsUrl = resolveFeatureDocsUrl(catalogEntry);

  // --- Region-unavailable -------------------------------------------------
  // Checked BEFORE the entitlement states: if the extension isn't published for
  // this Region, a Subscribe button would dead-end, because even a valid
  // subscription can't be installed here. An already-installed feature is
  // exempt — it demonstrably works, whatever the catalog now says.
  if (!installed && catalogEntry?.availableInRegion === false) {
    return (
      <NotAvailableInRegion
        featureDisplayName={featureDisplayName}
        description={featureDescription}
        docsUrl={docsUrl}
        availableRegions={catalogEntry.availableRegions ?? []}
        currentRegion={awsRegion}
        marketplaceUrl={marketplaceUrl}
      />
    );
  }

  // --- NONE ---------------------------------------------------------------
  if (state === 'NONE') {
    return (
      <SubscriptionRequired
        featureDisplayName={featureDisplayName}
        description={featureDescription}
        docsUrl={docsUrl}
        marketplaceUrl={marketplaceUrl}
        canSubscribe={isAdmin}
        licenseMode={entitlement?.licenseMode}
        onSubscribe={isAdmin ? handleSubscribe : undefined}
        subscribing={subscribing}
        subscribeError={subscribeError?.message ?? null}
      />
    );
  }

  // Paid extension being served without a confirmed subscription — `auto`
  // (checks off), `advisory` (check unreachable) or `simulated` (aimed at a
  // simulator). Only for marketplace features: an OSS extension has no
  // subscription to verify, so warning about it would be noise.
  //
  // Computed here rather than in the installed-only block below because the
  // not-yet-installed screens make the same claim ("Your subscription is
  // active") and were making it just as wrongly.
  const unverifiedGrant = !isOss && isUnverifiedGrant(state, entitlement?.source);

  // --- ACTIVE + not installed ---------------------------------------------
  if (state === 'ACTIVE' && !installed) {
    return isAdmin ? (
      <InstallPrompt
        featureDisplayName={featureDisplayName}
        description={featureDescription}
        docsUrl={docsUrl}
        isOss={isOss}
        unverified={unverifiedGrant}
        loading={launchLoading}
        onInstall={handleInstall}
        errorMessage={launchError?.message ?? null}
      />
    ) : (
      <AwaitingAdminInstall featureDisplayName={featureDisplayName} docsUrl={docsUrl} isOss={isOss} unverified={unverifiedGrant} />
    );
  }

  // From this point on, `installed` is non-null.
  if (!installed) {
    // Safety: EXPIRED + not installed falls here. Treat as NONE.
    return (
      <SubscriptionRequired
        featureDisplayName={featureDisplayName}
        marketplaceUrl={marketplaceUrl}
        canSubscribe={isAdmin}
        onSubscribe={isAdmin ? handleSubscribe : undefined}
        subscribing={subscribing}
        subscribeError={subscribeError?.message ?? null}
      />
    );
  }

  const entitlementSource = entitlement?.source ?? 'none';
  const context: FeatureContext = {
    featureId,
    installedVersion: installed.installedVersion,
    featureApiEndpoint: installed.featureApiEndpoint,
    getAuthToken,
    mainStackName,
    uiAccessAllowed: state === 'ACTIVE',
    entitlementSource,
    // ACTIVE *and* actually checked against a Marketplace API. `auto` means
    // checks are switched off; `advisory` means the check was unreachable and we
    // allowed rather than locking out a possibly-paying customer. Neither is a
    // verified subscription, and collapsing them into `uiAccessAllowed: true`
    // is what makes that flag unusable as a licence gate.
    entitlementVerified: isVerifiedEntitlement(state, entitlementSource),
  };

  const featureContent = (
    <FeatureLoader
      featureId={featureId}
      uiBundlePath={installed.uiBundlePath}
      expectedVersion={installed.installedVersion}
      context={context}
    />
  );

  // --- EXPIRED + installed ------------------------------------------------
  if (state === 'EXPIRED') {
    return (
      <SpaceBetween size="l">
        <ExpiredBanner featureDisplayName={featureDisplayName} marketplaceUrl={marketplaceUrl} />
        {/* Dimmed wrapper — pointer-events:none makes the read-only nature obvious. */}
        <div aria-disabled="true" style={{ opacity: 0.55, pointerEvents: 'none', filter: 'grayscale(0.3)' }}>
          {featureContent}
        </div>
      </SpaceBetween>
    );
  }

  // Cancelling goes through `unsubscribeFeature`, which drives the simulator's
  // admin API. A REAL AWS Marketplace subscription can only be cancelled in the
  // buyer's Marketplace console, so don't offer a button that would fail — and
  // never offer it for an `advisory` state, where we never confirmed a
  // subscription in the first place.
  // `simulated` covers both seller-side modes (bundled simulator and
  // admin-supplied endpoint) — they were separate source values, and both are
  // cancellable through the simulator's admin API.
  const canCancelSubscription = isAdmin && entitlement?.source === 'simulated';

  // --- ACTIVE + installed --------------------------------------------------
  // Trust the server's `updateAvailable`, which compares SemVer properly and
  // only reports an update when latest is strictly NEWER. Recomputing it here
  // as `latest !== installed` resurrected the downgrade prompt that the
  // resolver already fixed: a feature installed with `--from-code` can be
  // AHEAD of the published latest, and plain inequality then invited the admin
  // to "update" backwards.
  const hasUpdate = installed.updateAvailable && !!installed.latestVersion;

  return (
    <SpaceBetween size="l">
      {/* EXACTLY ONE subscription banner. These two used to render together, so
          an advisory grant produced a yellow "Subscription not verified"
          immediately above a green "Subscription active · Source: advisory" —
          the page contradicting itself, and making the extension's own honest
          "no subscription found" panel look like a third opinion.

          Unverified wins, because it is the true statement of the pair, and it
          carries the Cancel action so the simulator dev loop keeps its off
          switch. `ActiveSubscriptionBanner` now renders only when the
          subscription really was confirmed. Excluded from both:
            - `oss` — an open-source extension has no subscription at all;
              check_feature_entitlement short-circuits it to ACTIVE. Showing
              "Subscription active / Source: oss" with a Cancel button offered
              admins an action that cannot work.
            - `auto` — checks are switched off, so there is no contract to cancel
              (and `auto` is an unverified source, so it lands on the banner
              above). */}
      {unverifiedGrant ? (
        <UnverifiedSubscriptionBanner
          featureDisplayName={featureDisplayName}
          source={entitlementSource}
          reason={unverifiedReason(entitlementSource)}
          // Appended to the existing banner rather than added as another one —
          // the contradictory-banner stack is what the previous change removed.
          mismatchNote={licenseModeMismatchNote(entitlement)}
          marketplaceUrl={marketplaceUrl}
          canCancel={canCancelSubscription}
          onCancel={canCancelSubscription ? handleCancel : undefined}
          cancelling={cancelling}
          cancelError={cancelError?.message ?? null}
        />
      ) : (
        entitlement &&
        !isOss &&
        entitlement.source !== 'auto' && (
          <ActiveSubscriptionBanner
            entitlement={entitlement}
            mismatchNote={licenseModeMismatchNote(entitlement)}
            canCancel={canCancelSubscription}
            onCancel={canCancelSubscription ? handleCancel : undefined}
            cancelling={cancelling}
            cancelError={cancelError?.message ?? null}
          />
        )
      )}
      {hasUpdate ? (
        <UpdateAvailableBanner
          installedVersion={installed.installedVersion}
          latestVersion={installed.latestVersion as string}
          isAdmin={isAdmin}
          onUpdate={isAdmin ? handleUpdate : undefined}
          loading={launchLoading}
        />
      ) : (
        // Version only — the subscription is stated once, by the banner above.
        <UpToDateBanner version={installed.installedVersion} />
      )}
      <LearnMore docsUrl={docsUrl} />
      {featureContent}
    </SpaceBetween>
  );
};

export default FeaturePage;
