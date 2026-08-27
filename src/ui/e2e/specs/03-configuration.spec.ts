// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { test, expect } from '../fixtures/test-base';
import { statePath } from '../fixtures/roles';
import { ROUTES } from '../helpers/documents';

test.use({ storageState: statePath('Admin') });

/**
 * Configuration is where accelerator_issues.md concentrates (#1, #2, #5, #7, #8).
 * These scenarios stay read-only/non-destructive on purpose: a UAT tier that
 * rewrites the shared stack's configuration would corrupt the environment for
 * every later scenario and for anyone else using it.
 */
test.describe('Configuration', () => {
  test('Admin can open Configuration and it renders without error', async ({ page }) => {
    await page.goto(ROUTES.configuration);

    await expect(
      page.getByRole('heading', { name: /configuration/i }).first(),
    ).toBeVisible({ timeout: 60_000 });
  });

  test('Configuration exposes a document-classes surface', async ({ page }) => {
    await page.goto(ROUTES.configuration);
    await expect(
      page.getByRole('heading', { name: /configuration/i }).first(),
    ).toBeVisible({ timeout: 60_000 });

    // The config editor is tabbed/sectioned; assert that a classes-related control
    // is reachable, which is the entry point for the class/field editing flows.
    const classesSurface = page
      .getByRole('tab', { name: /class|schema|document/i })
      .or(page.getByRole('button', { name: /class|schema/i }))
      .or(page.getByText(/document class/i))
      .first();

    await expect(
      classesSurface,
      'no document-classes surface found in Configuration — the class/field ' +
        'editing flow may be unreachable',
    ).toBeVisible({ timeout: 30_000 });
  });
});
