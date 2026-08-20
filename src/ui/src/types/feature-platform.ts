// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * TypeScript mirror of the feature-platform GraphQL types
 * (see subscription-features/feature-platform/main-stack-extensions/appsync/feature-platform.graphql).
 *
 * These are intentionally hand-written (not generated) so the UI can compile
 * before the main stack's codegen runs. Once EnableFeaturePlatform is merged
 * into the main schema and codegen runs, these types can move into
 * src/ui/src/graphql/generated/ and this file becomes a re-export shim.
 */

export type FeatureEntitlementState = 'NONE' | 'ACTIVE' | 'EXPIRED';

/** "oss" = open-source bundled feature (install directly); "marketplace" =
 * closed-source extension requiring an AWS Marketplace subscription. */
export type CatalogFeatureSource = 'oss' | 'marketplace';

/**
 * A feature listed in the catalog manifest (catalog.json), whether or not this
 * IDP stack has it installed. Drives the nav section's catalog entries and the
 * FeaturePage Install / Subscribe CTA.
 */
export interface CatalogFeature {
  featureId: string;
  displayName: string;
  latestVersion: string;
  iconUrl: string | null;
  /** Short description shown on nav hover and the not-yet-installed page. */
  description: string | null;
  /** "Learn more" link. OSS: a docs-site slug (e.g. "extensions/sample-document-status")
   * or absolute URL. Empty for marketplace (falls back to marketplaceListingUrl). */
  docsUrl: string | null;
  /** Whether the feature gets its own Extensions nav entry before it's installed
   * (installed features always appear). Null/absent means true; false for
   * reference samples, discoverable via Browse catalog only. */
  showInNav: boolean | null;
  /** "oss" or "marketplace"; defaults to "oss" when absent. */
  source: CatalogFeatureSource | null;
  /** Marketplace-only: product code GetEntitlements is queried against. */
  productCode: string | null;
  /** Marketplace-only: public listing page the Subscribe CTA links to. */
  marketplaceListingUrl: string | null;
  /**
   * Whether this feature can be installed in the host stack's own region.
   * Marketplace extensions are region-scoped: `sam package` bakes an absolute,
   * region-specific s3:// CodeUri into the published template, and a Lambda's
   * code bucket must live in the function's region. When false, show where it
   * IS available instead of a Subscribe button that dead-ends.
   * Always true for OSS features; null/absent from an older host → treat as true.
   */
  availableInRegion: boolean | null;
  /**
   * Regions this feature publishes artifacts to. Empty for OSS features (they
   * ship with the host, so they aren't region-scoped).
   */
  availableRegions: string[] | null;
}

export interface InstalledFeature {
  featureId: string;
  displayName: string;
  installedVersion: string;
  /** Populated from the feature bucket's latest.json; null if unknown. */
  latestVersion: string | null;
  updateAvailable: boolean;
  stackName: string;
  stackRegion: string;
  stackId: string | null;
  uiBundlePath: string;
  featureApiEndpoint: string | null;
  iconUrl: string | null;
  installedAt: string;
  installedBy: string | null;
}

export interface FeatureEntitlement {
  featureId: string;
  state: FeatureEntitlementState;
  expiresAt: string | null;
  customerIdentifier: string | null;
  productCode: string | null;
  /**
   * Which mechanism produced this state:
   * - `marketplace` — GetEntitlements via a configured endpoint (simulator or override)
   * - `simulator` — the local marketplace-simulator
   * - `marketplace-live` — the real buyer-side AWS Marketplace Agreement API
   * - `advisory` — the live check was UNREACHABLE, so ACTIVE was assumed rather
   *   than locking a possibly-paying customer out. Not a confirmed subscription.
   * - `auto` — entitlement checks disabled; everything treated as subscribed
   * - `oss` / `none` — open-source feature, or no product code registered
   */
  source: 'marketplace' | 'marketplace-live' | 'advisory' | 'simulator' | 'auto' | 'oss' | 'none';
  /**
   * URL the UI must redirect the admin to (new tab) in order to accept
   * pricing, EULA, and the AWS Customer Agreement. Populated only by the
   * `subscribeFeature` mutation; null on `checkFeatureEntitlement`.
   */
  marketplaceUrl?: string | null;
}

export interface FeatureLaunchUrl {
  featureId: string;
  version: string;
  launchUrl: string;
  templateUrl: string;
  stackName: string;
  /** JSON-encoded parameters map. */
  parameters: string;
}

/**
 * Contract the feature's UMD bundle must implement when it is loaded into the
 * host. The bundle calls `window.IdpFeatures.register(featureId, registration)`
 * exactly once at script-load time.
 */
export interface FeatureRegistration {
  /** The React component to render inside <FeaturePage>. Receives FeatureContext as a prop. */
  Component: React.ComponentType<FeatureContext>;
  /** The feature's declared version (should match installedVersion). */
  version: string;
  /** Human-readable display name (should match the registered row). */
  displayName: string;
}

/**
 * Props passed to the feature's Component. The host hands the feature
 * everything it needs to call its own API and render with the host's theme.
 */
export interface FeatureContext {
  featureId: string;
  installedVersion: string;
  /** If null, the feature has no backend API (pure client-side feature). */
  featureApiEndpoint: string | null;
  /** Fetches a fresh Cognito JWT to authorize feature-API calls. */
  getAuthToken: () => Promise<string>;
  /** Host stack name (same as main IDP stack name). */
  mainStackName: string;
  /** True when the subscription is ACTIVE; false when EXPIRED. The host wraps
   *  the feature in a disabled overlay when false, but passes the flag through
   *  so features can render sensible read-only fallbacks. */
  subscriptionActive: boolean;
}

declare global {
  interface Window {
    IdpFeatures?: {
      /** Set by the host before any feature script is loaded. */
      register: (featureId: string, registration: FeatureRegistration) => void;
      /** Set by the host so features can log using the host's logger. */
      log?: (level: 'info' | 'warn' | 'error', message: string, meta?: unknown) => void;
    };
  }
}
