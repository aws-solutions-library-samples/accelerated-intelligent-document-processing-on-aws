// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Local mirror of the host's FeatureContext type. Keep this file in sync with
 *   src/ui/src/types/feature-platform.ts (in the main IDP UI).
 * The host passes an object matching this shape to the feature's Component
 * as its sole prop.
 */
export interface FeatureContext {
  featureId: string;
  installedVersion: string;
  featureApiEndpoint: string | null;
  getAuthToken: () => Promise<string>;
  mainStackName: string;
  /**
   * UX affordance ONLY — not a licence gate.
   *
   * A host-computed boolean delivered to code running in the end user's browser,
   * in the customer's own AWS account. It is `true` whenever the host is in
   * `auto` mode, whenever a marketplace simulator is configured, and whenever
   * the live subscription check was unreachable (`advisory`) — all of which an
   * account admin controls. Use it to disable buttons and render read-only
   * fallbacks; never to decide whether to serve paid functionality.
   */
  subscriptionActive: boolean;
  /** How the host arrived at that state. `auto` / `advisory` mean nothing was verified. */
  entitlementSource?: 'marketplace' | 'marketplace-live' | 'advisory' | 'simulator' | 'auto' | 'oss' | 'none';
  /**
   * True only when the host actually confirmed an entitlement against a
   * Marketplace API. Unlike `subscriptionActive` it does not read `true` when
   * checks are disabled or unreachable — but it is still host-computed and
   * browser-delivered, so it is a signal to *warn* on, not to gate on.
   *
   * If you are building a PAID extension: enforce in your own backend against
   * your own seller-side check. See
   * docs/feature-platform-developer-guide.md -> "Entitlement enforcement is the
   * extension's job".
   */
  entitlementVerified?: boolean;
}

export interface FeatureRegistration {
  Component: React.ComponentType<FeatureContext>;
  version: string;
  displayName: string;
}

declare global {
  interface Window {
    IdpFeatures?: {
      register: (featureId: string, registration: FeatureRegistration) => void;
    };
  }
}
