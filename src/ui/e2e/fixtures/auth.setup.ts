// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { test as setup, expect } from '@playwright/test';
import fs from 'node:fs';
import {
  ROLES,
  credentials,
  statePath,
  STATE_DIR,
  WELCOME_DISMISSED_KEY,
  type Role,
} from './roles';

/**
 * Signs in through the real Amplify Authenticator form and saves storageState
 * for reuse. Deliberately form-based rather than seeding tokens: form login
 * needs no AWS credentials at all (only a username/password), which keeps the
 * whole tier runnable from a stock CI runner with no IAM role.
 *
 * This doubles as scenario 1. If sign-in regresses, setup fails and every
 * dependent scenario is reported SKIPPED rather than silently passing.
 */
for (const role of ROLES) {
  setup(`authenticate as ${role}`, async ({ page }) => {
    const { username, password } = credentials(role as Role);
    fs.mkdirSync(STATE_DIR, { recursive: true });

    const pageErrors: string[] = [];
    page.on('pageerror', (e) => pageErrors.push(e.message));

    await page.goto('./');

    // Amplify's Authenticator labels these "Username" and "Password". Matched by
    // accessible label rather than a CSS class so an Amplify style refactor
    // doesn't break auth for the whole suite.
    await page.getByLabel(/username|email/i).first().fill(username);
    await page.getByLabel(/^password/i).first().fill(password);
    await page.getByRole('button', { name: /^sign in$/i }).click();

    // Proof of authentication is the identity chip in the banner, which renders
    // "<username> (<Group>)". Deliberately NOT the 'Document List' nav link: a
    // freshly created user lands on the "Welcome to GenAI IDP" onboarding
    // interstitial, where no nav exists yet, so asserting on nav here reports a
    // perfectly successful login as a failure. This assertion also proves the
    // user landed in the RIGHT Cognito group.
    await expect(
      page.getByRole('banner').getByRole('button').filter({ hasText: username }),
      `${role} did not authenticate. If this times out on a freshly created user, ` +
        'the account may be in FORCE_CHANGE_PASSWORD — run_uat.py sets a permanent ' +
        'password to avoid that.',
    ).toBeVisible({ timeout: 60_000 });

    await expect(
      page.getByRole('banner').getByRole('button').filter({ hasText: `(${role})` }),
      `${role} authenticated but the banner does not show the ${role} group`,
    ).toBeVisible();

    expect(pageErrors, `uncaught page errors during sign-in: ${pageErrors.join('; ')}`)
      .toHaveLength(0);

    // Suppress the onboarding interstitial for the task scenarios, which are not
    // testing onboarding and would otherwise each have to click past it.
    // storageState captures localStorage, so this carries into every spec.
    // The first-run journey itself is covered explicitly in 01-shell.spec.ts,
    // which starts from a clean context — so this bypass hides nothing.
    await page.evaluate((key) => localStorage.setItem(key, 'true'), WELCOME_DISMISSED_KEY);

    await page.context().storageState({ path: statePath(role as Role) });
  });
}
