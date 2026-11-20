// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Decision Model: Strands Decider entry — registers with the host.
 *
 * featureId / displayName / version are injected at build time from
 * feature.yaml by vite.config.ts (see __FEATURE_*__ constants below).
 * Bumping the version is a one-line edit in feature.yaml.
 */

import App from './App';

// Compile-time constants populated by Vite from feature.yaml.
declare const __FEATURE_ID__: string;
declare const __FEATURE_DISPLAY_NAME__: string;
declare const __FEATURE_VERSION__: string;

if (typeof window !== 'undefined') {
  if (!window.IdpFeatures?.register) {
    // eslint-disable-next-line no-console
    console.warn('[decision-model-decider] window.IdpFeatures.register not found — running outside host?');
  } else {
    window.IdpFeatures.register(__FEATURE_ID__, {
      Component: App,
      version: __FEATURE_VERSION__,
      displayName: __FEATURE_DISPLAY_NAME__,
    });
  }
}
