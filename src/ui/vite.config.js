// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import svgr from 'vite-plugin-svgr';
import { resolve } from 'path';

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => ({
  // Public base path. Defaults to '/' (CloudFront hosting / local dev). When the
  // Web UI is served from API Gateway (WebUIHosting=APIGateway) the SPA lives
  // under the REST API stage prefix (/api), so CodeBuild sets
  // VITE_UI_BASE_PATH=/api/ and Vite rewrites all asset URLs and the manifest to
  // that base. Must be an absolute directory path ending in '/'.
  base: process.env.VITE_UI_BASE_PATH || '/',

  plugins: [
    react({
      // Use automatic JSX runtime (React 17+)
      jsxRuntime: 'automatic',
      // Include all JavaScript files for JSX transformation
      include: '**/*.{js,jsx,ts,tsx}',
    }),

    // Enable SVG import as React components
    svgr(),
  ],

  // Ensure all .js and .jsx files are treated as JSX
  esbuild: {
    jsx: 'automatic',
  },

  // Development server configuration
  server: {
    port: 3000,
    open: true,
    // Enable CORS for AWS Amplify
    cors: true,
  },

  // Build configuration
  build: {
    outDir: 'build',
    sourcemap: mode === 'development' ? 'inline' : false,
    // Increase chunk size warning limit (suppressed for enterprise internal tool)
    chunkSizeWarningLimit: 3000,
    rollupOptions: {
      output: {
        // Manual chunking for better code splitting
        manualChunks: {
          'aws-amplify': ['aws-amplify', '@aws-amplify/ui-react'],
          'aws-sdk': ['@aws-sdk/client-ssm', '@aws-sdk/client-cognito-identity', '@aws-sdk/s3-request-presigner'],
          cloudscape: ['@cloudscape-design/components', '@cloudscape-design/global-styles'],
          chart: ['chart.js', 'react-chartjs-2'],
          'react-vendor': ['react', 'react-dom', 'react-router-dom'],
        },
      },
    },
    // Configure target to ensure JSX is handled
    target: 'esnext',
  },

  // Resolve configuration
  resolve: {
    alias: {
      '@': resolve(__dirname, './src'),
    },
    // Ensure proper module resolution
    extensions: ['.mjs', '.ts', '.tsx', '.js', '.jsx', '.json'],
  },

  // Define global constants
  define: {
    // Ensure process.env is available for compatibility
    'process.env': {},
  },

  // Optimize dependencies
  optimizeDeps: {
    include: [
      'react',
      'react-dom',
      'react-router-dom',
      'aws-amplify',
      '@aws-amplify/ui-react',
      '@cloudscape-design/components',
      '@cloudscape-design/global-styles',
    ],
    exclude: ['@aws-sdk/signature-v4-multi-region'],
    esbuildOptions: {
      loader: {
        '.js': 'jsx',
      },
      // Suppress source map warnings for dependencies
      sourcemap: false,
    },
  },

  // Suppress source map warnings in development
  ...(mode === 'development' && {
    logLevel: 'info',
    clearScreen: false,
  }),

  // Test configuration (vitest)
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: './src/setupTests.ts',
    // Every extension Vitest's own `defaultInclude` accepts
    // (`**/*.{test,spec}.?(c|m)[jt]s?(x)`), written as braces because that is
    // the syntax `scripts/tests/test_ui_test_discovery.py` can resolve without
    // Node. This was `src/**/*.test.{ts,tsx}`: two extensions, so a test written
    // as `Foo.test.jsx` — a file the eslint script lints and tsc reads — was
    // never collected, and `npx vitest run` exited 0 without mentioning it. That
    // gate asserts every test-named file under src/ui is matched by what is
    // configured here, so narrowing this list fails it rather than quietly
    // dropping suites.
    include: [
      'src/**/*.{test,spec}.{js,jsx,ts,tsx,cjs,cjsx,cts,ctsx,mjs,mjsx,mts,mtsx}',
    ],
    // Above vitest's 5s default, for CI runner variance rather than to license
    // slow tests. Rendering a Cloudscape table in jsdom costs hundreds of
    // milliseconds, and the shared CI runner has been observed ~5x slower than a
    // developer machine — enough that a test passing locally in 1s timed out
    // there. That failure mode is invisible until the pipeline runs, so it costs a
    // round-trip every time. The fix for a genuinely slow test is still to make it
    // cheaper; a real hang still fails, just later.
    testTimeout: 10000,
  },

  // CSS configuration
  css: {
    modules: {
      localsConvention: 'camelCase',
    },
  },
}));
