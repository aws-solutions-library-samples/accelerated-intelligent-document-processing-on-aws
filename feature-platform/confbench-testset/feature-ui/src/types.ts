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
  uiAccessAllowed: boolean;
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
