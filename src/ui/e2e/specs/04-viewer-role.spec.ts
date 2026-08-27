// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { test, expect } from '../fixtures/test-base';
import { statePath } from '../fixtures/roles';
import { gotoDocumentList, ROUTES } from '../helpers/documents';

test.use({ storageState: statePath('Viewer') });

/**
 * The UI half of RBAC. `make api-test` already proves the REST API enforces
 * group membership; it cannot prove the UI doesn't OFFER an action the API will
 * then refuse. A Viewer shown a Delete button that 403s is a real usability
 * defect that the API test passes cleanly.
 */
test.describe('Viewer role', () => {
  test('Viewer can read the document list', async ({ page }) => {
    await gotoDocumentList(page);
    await expect(page.getByRole('columnheader', { name: 'Document ID', exact: true })).toBeVisible();
  });

  test('Viewer is not offered destructive document actions', async ({ page }) => {
    await gotoDocumentList(page);

    // Absence assertions need the page settled first, otherwise they pass simply
    // because the toolbar has not rendered yet.
    await expect(page.getByRole('columnheader', { name: 'Status', exact: true })).toBeVisible();

    for (const label of [/^delete$/i, /^reprocess$/i]) {
      await expect(
        page.getByRole('button', { name: label }),
        `Viewer was offered a "${label}" control it is not authorised to use`,
      ).toHaveCount(0);
    }
  });

  test('Viewer is not offered configuration editing', async ({ page }) => {
    await page.goto(ROUTES.configuration);
    // Either the route is refused outright, or it renders read-only. Both are
    // acceptable; what is not acceptable is an enabled Save control.
    await page.waitForLoadState('networkidle');

    await expect(
      page.getByRole('button', { name: /^save/i }).and(page.locator(':not([disabled])')),
      'Viewer was offered an enabled Save control in Configuration',
    ).toHaveCount(0);
  });
});
