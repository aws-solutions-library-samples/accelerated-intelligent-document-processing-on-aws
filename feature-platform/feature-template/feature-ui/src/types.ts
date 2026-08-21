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
  uiAccessAllowed: boolean;
  /**
   * How the host arrived at that state. `auto` / `advisory` / `simulated` all
   * mean nothing was verified. `marketplace-live` means the buyer-side
   * Agreement API answered **against real AWS** — the host derives it from
   * whether a Marketplace endpoint override is in effect, not from its
   * SubscriptionMode parameter, so a simulator-backed check reports
   * `simulated` and cannot forge this value.
   */
  entitlementSource?: 'marketplace-live' | 'simulated' | 'advisory' | 'auto' | 'oss' | 'none';
  /**
   * True only when the host actually confirmed an entitlement against real
   * AWS Marketplace. Unlike `uiAccessAllowed` it does not read `true` when
   * checks are disabled, unreachable, or simulator-backed — but it is still host-computed and
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
