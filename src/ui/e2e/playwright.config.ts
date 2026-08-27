// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { defineConfig, devices } from '@playwright/test';

/**
 * UI acceptance-test (UAT) configuration.
 *
 * Points at a DEPLOYED stack, never a local dev server: the deployed SPA already
 * has its VITE_* configuration baked in at build time, so the runner needs only
 * a URL. UAT_BASE_URL is normally the stack's `ApplicationWebURL` output, which
 * scripts/uat/run_uat.py resolves and exports.
 */
const baseURL = process.env.UAT_BASE_URL;

if (!baseURL) {
  throw new Error(
    'UAT_BASE_URL is not set. Run via `make uat-testing`, or export it yourself:\n' +
      "  export UAT_BASE_URL=$(aws cloudformation describe-stacks --stack-name <stack> \\\n" +
      "    --query \"Stacks[0].Outputs[?OutputKey=='ApplicationWebURL'].OutputValue\" --output text)",
  );
}

// Scenarios 4-7 wait on real Bedrock/Textract processing. The budget is generous
// on purpose: a false failure from an impatient timeout is worse than a slow pass,
// and "did not finish in budget" is itself the assertion we care about.
const ACTION_TIMEOUT = Number(process.env.UAT_ACTION_TIMEOUT_MS ?? 30_000);
const TEST_TIMEOUT = Number(process.env.UAT_TEST_TIMEOUT_MS ?? 420_000);

export default defineConfig({
  testDir: './specs',
  outputDir: './test-results',
  // A shared persistent stack is not safe to hammer in parallel: several
  // scenarios mutate configuration or test-set state. Serial by default.
  workers: Number(process.env.UAT_WORKERS ?? 1),
  fullyParallel: false,
  // One retry absorbs genuine network flake without hiding a real regression:
  // a scenario that only ever passes on retry still shows as "flaky" in the report.
  retries: Number(process.env.UAT_RETRIES ?? 1),
  timeout: TEST_TIMEOUT,
  expect: { timeout: 15_000 },
  forbidOnly: !!process.env.CI,
  reporter: [
    ['list'],
    ['html', { outputFolder: './playwright-report', open: 'never' }],
    ['junit', { outputFile: './test-results/junit.xml' }],
    // Emits uat-report.md + uat-results.json, including the interaction metrics
    // (clicks, keystrokes, navigations) collected per scenario.
    ['./reporters/uat-reporter.ts'],
    ...(process.env.CI ? [['github'] as const] : []),
  ],
  use: {
    baseURL,
    actionTimeout: ACTION_TIMEOUT,
    navigationTimeout: 60_000,
    trace: 'on-first-retry',
    video: 'retain-on-failure',
    screenshot: 'only-on-failure',
    // CloudFront serves a valid cert; APIGateway-hosting variants may not.
    ignoreHTTPSErrors: !!process.env.UAT_IGNORE_HTTPS_ERRORS,
  },
  projects: [
    // Signs in through the real Amplify form once per role and saves storageState.
    // This is also scenario 1's coverage: if the form login breaks, setup fails
    // and every dependent scenario is reported as skipped rather than passing.
    {
      name: 'setup',
      testDir: './fixtures',
      testMatch: /auth\.setup\.ts/,
    },
    {
      name: 'uat',
      dependencies: ['setup'],
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
