// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { test, expect } from '../fixtures/test-base';
import { statePath, WELCOME_DISMISSED_KEY } from '../fixtures/roles';
import { gotoDocumentList } from '../helpers/documents';

test.use({ storageState: statePath('Admin') });

/**
 * First-run onboarding. auth.setup.ts seeds WELCOME_DISMISSED_KEY so the task
 * scenarios aren't blocked by an interstitial they're not testing — this scenario
 * exists so that bypass hides nothing: it clears the key, proving a brand-new
 * user can get from the welcome screen into the console.
 */
test.describe('First-run onboarding', () => {
  test('a new user can get from the welcome screen into the console', async ({ page }) => {
    await page.goto('./');
    // Clear the dismissal, then reload so AuthRoutes re-evaluates the gate.
    await page.evaluate((key) => localStorage.removeItem(key), WELCOME_DISMISSED_KEY);
    await page.reload();

    await expect(
      page.getByRole('heading', { name: /welcome to genai idp/i }),
    ).toBeVisible({ timeout: 60_000 });

    // This button is the ONLY way past the interstitial to the app shell, so a
    // regression here strands every new user on the landing page.
    await page.getByRole('button', { name: /enter idp console/i }).click();

    await expect(
      page.getByRole('link', { name: 'Document List' }),
      'clicking "Enter IDP Console" did not reveal the app navigation',
    ).toBeVisible({ timeout: 30_000 });
  });
});

/**
 * The cheapest possible regression net: an authenticated Admin can load the app
 * shell and read the document list. Sign-in itself is covered by auth.setup.ts.
 */
test.describe('Shell and document list', () => {
  test('Admin can open the document list and read its columns', async ({ page }) => {
    await gotoDocumentList(page);

    // Column headers come from documents-table-config.tsx and are the most stable
    // handles in the app: config-driven, user-visible, and semantic <th> elements.
    await expect(page.getByRole('columnheader', { name: 'Document ID', exact: true })).toBeVisible();
    await expect(page.getByRole('columnheader', { name: 'Status', exact: true })).toBeVisible();
    await expect(page.getByRole('columnheader', { name: 'Submitted', exact: true })).toBeVisible();
  });

  test('primary navigation destinations are all reachable', async ({ page }) => {
    await gotoDocumentList(page);

    // A dead nav entry is a whole feature that cannot be reached — exactly the
    // class of defect the unit tests cannot see, since they never render the shell.
    const destinations: Array<{ link: string; expect: RegExp }> = [
      { link: 'Upload Document(s)', expect: /upload documents/i },
      { link: 'View/Edit Configuration', expect: /configuration/i },
    ];

    for (const d of destinations) {
      await page.getByRole('link', { name: d.link }).click();
      await expect(
        page.getByRole('heading', { name: d.expect }).first(),
        `nav entry "${d.link}" did not lead to a recognisable page`,
      ).toBeVisible({ timeout: 30_000 });
      await page.goBack();
    }
  });
});
