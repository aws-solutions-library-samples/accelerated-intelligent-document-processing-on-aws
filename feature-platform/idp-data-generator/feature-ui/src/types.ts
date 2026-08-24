// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/** Props the host passes to a feature's root Component when it mounts the page. */
export interface FeatureContext {
  featureApiEndpoint: string | null;
  getAuthToken: () => Promise<string>;
  uiAccessAllowed: boolean;
  installedVersion: string;
}

declare global {
  interface Window {
    IdpFeatures?: {
      register: (
        featureId: string,
        registration: {
          Component: React.ComponentType<FeatureContext>;
          version: string;
          displayName: string;
        },
      ) => void;
    };
  }
}

export {};
