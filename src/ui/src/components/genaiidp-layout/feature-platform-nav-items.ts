// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Snippets to splice into `src/ui/src/components/genaiidp-layout/navigation.tsx`.
 *
 * The feature platform menu is **always visible** (per the locked plan) even
 * when no features are installed — the section always starts with a "Browse
 * catalog" link to /features (no id), which renders the catalog browser. It's
 * styled (italic + search icon) so it reads as the catalog entry point rather
 * than another extension. Each feature adds a sub-link below it.
 *
 * Which features get nav links:
 *   - every **installed** feature (always, even if removed from the catalog —
 *     so the user can still reach the page to uninstall an orphaned feature);
 *   - catalog-only (published, not yet installed) features whose catalog entry
 *     has `showInNav !== false`. The bundled reference samples publish
 *     `showInNav: false` (from their feature.yaml), so they're discoverable
 *     via "Browse catalog" only until installed.
 */

import React from 'react';
import Badge from '@cloudscape-design/components/badge';
import Popover from '@cloudscape-design/components/popover';
import Box from '@cloudscape-design/components/box';
import type { SideNavigationProps } from '@cloudscape-design/components';
import type { CatalogFeature, InstalledFeature } from '../../types/feature-platform';
import { FEATURES_PATH_PREFIX, featureDetailHref } from '../../routes/constants';

export const FEATURES_SECTION_ID = 'idp-feature-platform';

export const COMING_SOON_HREF = '#extension-coming-soon';

const COMING_SOON_EXTENSIONS: { displayName: string; description: string }[] = [];

/**
 * Lifecycle status of a feature, used to choose the nav badge:
 *   - 'unavailable' — a marketplace extension that isn't published for this
 *                     Region, so it cannot be installed here at all. Outranks
 *                     'install', because installing could not succeed.
 *   - 'install'   — not installed. One badge for OSS and marketplace alike; see
 *                   `statusOf` for why the nav does not try to distinguish
 *                   "needs subscribing" from "needs installing".
 *   - 'update'    — installed at an older version than the catalog latest
 *   - 'ready'     — installed and up to date
 */
type FeatureStatus = 'unavailable' | 'install' | 'update' | 'ready';

/**
 * Merged nav entry used internally by the builder. `installed === null` means
 * the feature is catalog-only (not yet installed in this stack).
 */
interface NavEntry {
  featureId: string;
  displayName: string;
  description: string | null;
  source: 'oss' | 'marketplace';
  /** `null` when the feature is catalog-only (not installed). */
  installed: InstalledFeature | null;
  /** True when installed at an older version than the published latest. */
  updateAvailable: boolean;
  /** False only when the catalog says this extension isn't published here. */
  availableInRegion: boolean;
}

function statusOf(entry: NavEntry): FeatureStatus {
  if (entry.installed) return entry.updateAvailable ? 'update' : 'ready';
  // Region availability is checked before subscription: an extension that isn't
  // published for this Region can't be installed even with a valid subscription,
  // so prompting to subscribe would dead-end.
  if (!entry.availableInRegion) return 'unavailable';
  // Not installed — and that is ALL the nav claims, for either source.
  //
  // This used to infer "marketplace + not installed ⇒ not subscribed" and show a
  // 'Subscribe' badge. The inference is simply wrong for a customer who has
  // already paid: they saw "Subscription active" on the feature page and
  // "Subscribe" in the nav at the same time. The nav cannot resolve entitlement
  // — it is built from listInstalledFeatures + listCatalogFeatures, and neither
  // carries an entitlement verdict, by design: the verdict comes from
  // checkFeatureEntitlement, which is per-feature and (for `marketplace-live`)
  // calls the AWS Marketplace Agreement API. Fanning that out across every
  // catalog entry on every page render, to choose one word on a badge, would put
  // a real Marketplace API call on the critical path of the whole application.
  //
  // So the nav states what it actually knows. Whether the next step is
  // subscribing or installing is resolved on the feature page, which has the
  // verdict and is where the user is going anyway. The badge's job — per the
  // note on buildStatusInfo — is to say "this needs action", not to predict
  // which action.
  return 'install';
}

function mergeEntries(installed: InstalledFeature[], catalog: CatalogFeature[]): NavEntry[] {
  const byId = new Map<string, NavEntry>();
  const catalogById = new Map(catalog.map((c) => [c.featureId, c]));

  // Seed with installed features — these always show, even if they've been
  // removed from the catalog (so the user can still navigate to the page
  // to uninstall or see an "orphaned" feature).
  for (const f of installed) {
    const c = catalogById.get(f.featureId);
    byId.set(f.featureId, {
      featureId: f.featureId,
      displayName: f.displayName,
      description: c?.description ?? null,
      source: c?.source === 'marketplace' ? 'marketplace' : 'oss',
      installed: f,
      updateAvailable: f.updateAvailable,
      // An installed feature demonstrably works here, whatever the catalog now
      // says about this Region.
      availableInRegion: true,
    });
  }

  // Overlay catalog: add not-yet-installed features that opted into nav
  // visibility (showInNav !== false; absent means true). Features with
  // showInNav: false — the bundled reference samples — stay off the nav
  // until installed and are discoverable via "Browse catalog".
  for (const c of catalog) {
    if (!byId.has(c.featureId) && c.showInNav !== false) {
      byId.set(c.featureId, {
        featureId: c.featureId,
        displayName: c.displayName,
        description: c.description ?? null,
        source: c.source === 'marketplace' ? 'marketplace' : 'oss',
        installed: null,
        updateAvailable: false,
        // Absent on an older host → treat as available rather than inventing a
        // restriction we can't substantiate.
        availableInRegion: c.availableInRegion !== false,
      });
    }
  }

  return Array.from(byId.values()).sort((a, b) => a.displayName.toLowerCase().localeCompare(b.displayName.toLowerCase()));
}

// Badge text + colour per lifecycle status. 'ready' renders no badge (clean
// nav for the common installed-and-current case); status is implied by the
// absence of a CTA badge plus the hover description.
const STATUS_BADGE: Record<FeatureStatus, { text: string; color: 'blue' | 'grey' | 'red' } | null> = {
  // Not published for this Region — cannot be installed here at all, so don't
  // invite a subscription that couldn't be used.
  unavailable: { text: 'Not in this Region', color: 'red' },
  // "Install" rather than "Deploy" or "Set up" to match the vocabulary used
  // everywhere else for this step: InstallPrompt, "Awaiting installation",
  // listInstalledFeatures, the InstalledFeatures table.
  install: { text: 'Install', color: 'blue' },
  update: { text: 'Update', color: 'blue' },
  ready: null,
};

/**
 * Same two states, worded for someone who cannot act on them. Colour is kept from
 * STATUS_BADGE so the extension still reads as "not current" at a glance — it is the
 * imperative that misleads, not the emphasis.
 */
const NON_ADMIN_BADGE_TEXT: Partial<Record<FeatureStatus, string>> = {
  install: 'Not installed',
  update: 'Update available',
};

/**
 * Build the `info` ReactNode for a nav entry: a status badge (when the feature
 * needs action) wrapped in a Popover that reveals the feature's description on
 * hover/focus. When there's no badge AND no description there's nothing to
 * show, so we return undefined.
 */
function buildStatusInfo(entry: NavEntry, canInstall: boolean): React.ReactNode {
  const status = statusOf(entry);
  const badgeSpec = STATUS_BADGE[status];
  // "Install" and "Update" name an action only an Admin can take (subscribeFeature is
  // Admin-only, and the feature page routes everyone else to AwaitingAdminInstall). For
  // the rest, the same lifecycle state is said as a state rather than an invitation —
  // which is all this badge claims to do anyway, per the note above.
  const badgeText = !canInstall && (status === 'install' || status === 'update') ? NON_ADMIN_BADGE_TEXT[status] : badgeSpec?.text;
  const badge = badgeSpec && badgeText ? React.createElement(Badge, { color: badgeSpec.color }, badgeText) : null;

  // A not-yet-installed marketplace extension needs a subscription at some point,
  // and the badge no longer says so. Note it in the hover text instead: the nav
  // can honestly say a subscription is REQUIRED (a property of the extension,
  // straight from the catalog) without claiming whether this customer HAS one,
  // which is the verdict it cannot see.
  const needsSubscriptionNote = status === 'install' && entry.source === 'marketplace';
  const hoverText = needsSubscriptionNote
    ? [entry.description, 'Requires an AWS Marketplace subscription.'].filter(Boolean).join(' ')
    : entry.description;

  if (!hoverText) {
    return badge ?? undefined;
  }

  // Popover trigger: the badge if present, else a small "info" affordance so
  // a description is always discoverable even for 'ready' features.
  const trigger = badge ?? React.createElement(Box, { color: 'text-status-info', fontSize: 'body-s' }, 'ⓘ');
  return React.createElement(
    Popover,
    {
      header: entry.displayName,
      content: hoverText,
      triggerType: 'text',
      dismissButton: false,
      position: 'right',
      size: 'small',
    },
    trigger,
  );
}

/**
 * Returns a SideNavigation section starting with a "Browse catalog" link to
 * /features (the catalog browser that lists everything), then — separated by
 * a divider — installed features plus nav-visible (showInNav !== false)
 * catalog features. Always returns a non-empty section (even when both lists
 * are empty) so the menu entry is visible to all roles.
 *
 * Use in navigation.tsx like:
 *
 *   const { features: installed } = useInstalledFeatures();
 *   const { features: catalog } = useCatalogFeatures();
 *   const featureSection = useMemo(
 *     () => buildFeaturesNavSection(installed, catalog),
 *     [installed, catalog],
 *   );
 */
function comingSoonItems(installed: InstalledFeature[]): SideNavigationProps.Link[] {
  const installedIds = new Set(installed.map((f) => f.featureId));
  return COMING_SOON_EXTENSIONS.filter((c) => !installedIds.has(c.displayName)).map(
    (c) =>
      ({
        type: 'link',
        text: c.displayName,
        href: COMING_SOON_HREF,
        info: React.createElement(
          Popover,
          {
            header: c.displayName,
            content: c.description,
            triggerType: 'text',
            dismissButton: false,
            position: 'right',
            size: 'small',
          },
          React.createElement(Badge, { color: 'grey' }, 'Coming soon'),
        ),
      }) as SideNavigationProps.Link,
  );
}

export function buildFeaturesNavSection(
  installed: InstalledFeature[],
  catalog: CatalogFeature[] = [],
  /* Defaults to true so existing callers and tests keep the admin wording; only the
     nav, which knows the role, narrows it. */
  canInstall = true,
): SideNavigationProps.Section {
  const entries = mergeEntries(installed, catalog);

  const items: SideNavigationProps.Item[] = entries.map(
    (e) =>
      ({
        type: 'link',
        text: e.displayName,
        href: featureDetailHref(e.featureId),
        // `info` is a ReactNode (NOT a descriptor object — passing
        // { type: 'badge', ... } crashes React with error #31). We render a
        // status badge, wrapped in a Popover so hovering shows the feature's
        // description. The actual action (Subscribe / Install / Update) lives
        // on the feature page; the nav badge only communicates status.
        info: buildStatusInfo(e, canInstall),
      }) as SideNavigationProps.Link,
  );

  // The catalog entry point always comes first: /features (no id) renders the
  // catalog browser, which lists every extension — including reference samples
  // with showInNav: false that have no nav links of their own. The italic
  // treatment (so it reads as the catalog browser, not just another extension
  // in the list) is applied in navigation.css via an `a[href='#/features']`
  // selector (Cloudscape nav links expose no per-item className hook).
  const browseCatalog: SideNavigationProps.Link = {
    type: 'link',
    text: 'Browse catalog',
    href: `#${FEATURES_PATH_PREFIX}`,
  };

  const extensionItems = [...items, ...comingSoonItems(installed)];

  return {
    type: 'section',
    // "(Preview)" signals that the extension framework is still being built out —
    // there are no production extensions to install yet beyond the bundled demo.
    text: 'Extensions',
    items: extensionItems.length > 0 ? [browseCatalog, ...extensionItems] : [browseCatalog],
  };
}
