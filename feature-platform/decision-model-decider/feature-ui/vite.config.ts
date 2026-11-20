// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * IDP Feature UI — Vite config.
 *
 * Produces a single UMD bundle at `dist/ui-bundle.js`. React, ReactDOM,
 * Cloudscape, aws-amplify, and react-router-dom are ALL externalised so the
 * feature bundle is small and shares the host's library instances (avoiding
 * the classic "two copies of React" crashes).
 *
 * When the bundle is loaded in the host, these externals are resolved via
 * `window.*` globals set up by the main IDP UI's build.
 *
 * --- Single source of truth: feature.yaml ---
 *
 * `featureId`, `displayName`, and `version` live in `feature.yaml` (one
 * directory up from feature-ui/). At build time we read them and inject
 * them as compile-time constants (`__FEATURE_ID__`, `__FEATURE_DISPLAY_NAME__`,
 * `__FEATURE_VERSION__`) via Vite's `define:` option, which performs
 * verbatim string-substitution before parsing.
 *
 * `entry.tsx` then references those constants — meaning **a version bump is
 * a one-line edit in `feature.yaml`** and never an entry.tsx edit. The
 * publisher's bundle validator (`idp-feature-cli build`) still verifies the
 * resulting bundle contains the quoted version literal it expects, but with
 * `define:` the output bundle naturally contains exactly what feature.yaml
 * said, so the validator can no longer surface "out-of-sync version"
 * errors.
 */

/** Read top-level scalars from feature.yaml. Tiny regex parser — fine
 *  because feature.yaml has a fixed top-level shape and we only need three
 *  string scalars. Adding js-yaml as a dep would be heavier than this. */
function readFeatureManifest(): {
  featureId: string;
  displayName: string;
  version: string;
} {
  const manifestPath = resolve(__dirname, '../feature.yaml');
  const text = readFileSync(manifestPath, 'utf-8');
  const pick = (key: string): string => {
    // Match `^<key>: <value>` at start-of-line (multiline). `(.*)` greedy
    // up to end-of-line; we strip surrounding quotes ourselves below.
    const re = new RegExp(`^${key}:\\s*(.*)$`, 'm');
    const m = text.match(re);
    if (!m) {
      throw new Error(
        `[vite.config.ts] feature.yaml is missing top-level '${key}:' ` +
          `(${manifestPath})`,
      );
    }
    let val = m[1].trim();
    // Strip a single matched pair of surrounding quotes.
    if (
      (val.startsWith('"') && val.endsWith('"')) ||
      (val.startsWith("'") && val.endsWith("'"))
    ) {
      val = val.slice(1, -1);
    }
    if (!val) {
      throw new Error(
        `[vite.config.ts] feature.yaml '${key}' is empty (${manifestPath})`,
      );
    }
    return val;
  };
  return {
    featureId: pick('featureId'),
    displayName: pick('displayName'),
    version: pick('version'),
  };
}

const manifest = readFeatureManifest();

export default defineConfig({
  // Use the classic JSX runtime (React.createElement) so the bundle does not
  // import from `react/jsx-runtime`. The automatic runtime inlines
  // react-jsx-runtime.production.min.js into the bundle (containing
  // `ReactCurrentDispatcher`), which the publisher's safety check rejects.
  plugins: [react({ jsxRuntime: 'classic' })],
  define: {
    // Vite's `define:` performs verbatim text substitution of these
    // identifiers in TypeScript source. JSON.stringify(...) gives us the
    // quoted-literal form ("1.2.3") so the substituted token is a valid
    // string literal, not a bare identifier.
    __FEATURE_ID__: JSON.stringify(manifest.featureId),
    __FEATURE_DISPLAY_NAME__: JSON.stringify(manifest.displayName),
    __FEATURE_VERSION__: JSON.stringify(manifest.version),
  },
  build: {
    lib: {
      entry: 'src/entry.tsx',
      formats: ['umd'],
      name: 'IdpFeatureBundle',
      fileName: () => 'ui-bundle.js',
    },
    rollupOptions: {
      external: [
        'react',
        'react-dom',
        'react-dom/client',
        'react-router-dom',
        /^@cloudscape-design\/.*/,
        /^aws-amplify(\/.*)?$/,
      ],
      output: {
        globals: {
          react: 'React',
          'react-dom': 'ReactDOM',
          'react-dom/client': 'ReactDOM',
          'react-router-dom': 'ReactRouterDOM',
          'aws-amplify': 'awsAmplify',
          '@cloudscape-design/components': 'CloudscapeComponents',
          '@cloudscape-design/design-tokens': 'CloudscapeDesignTokens',
        },
      },
    },
  },
});
