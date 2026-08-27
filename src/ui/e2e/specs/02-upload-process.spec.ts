// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import path from 'node:path';
import fs from 'node:fs';
import { test, expect } from '../fixtures/test-base';
import { statePath } from '../fixtures/roles';
import { gotoDocumentList, uploadDocument, waitForDocumentTerminal } from '../helpers/documents';

test.use({ storageState: statePath('Admin') });

// Repo-root-relative: e2e lives at src/ui/e2e, so up three levels.
const REPO_ROOT = path.resolve(import.meta.dirname, '../../../..');
const SAMPLE = process.env.UAT_SAMPLE_DOC
  ?? path.join(REPO_ROOT, 'samples', 'lending_package.pdf');

/**
 * The flagship scenario: the accelerator's entire reason for existing is
 * "put a document in, get structured data out". If this cannot be done through
 * the UI, nothing else matters.
 *
 * This is also the slow one (real Textract + Bedrock), so it carries the
 * generous per-document budget rather than the default expect timeout.
 */
test.describe('Document processing end to end', () => {
  test('Admin can upload a document and it reaches a terminal state', async ({ page }) => {
    test.skip(!fs.existsSync(SAMPLE), `sample document not found at ${SAMPLE}`);

    const uploaded = await uploadDocument(page, SAMPLE);

    await gotoDocumentList(page);

    // Not asserting COMPLETED-only here on purpose: FAILED is a legitimate,
    // *informative* outcome that still proves the pipeline responded. What we
    // refuse to accept is a document that never concludes at all.
    const terminal = await waitForDocumentTerminal(page, uploaded);

    expect(
      terminal,
      `document finished as ${terminal}; expected COMPLETED for the standard sample`,
    ).toBe('COMPLETED');
  });

  test('a completed document opens to a detail view', async ({ page }) => {
    await gotoDocumentList(page);

    const completed = page.getByRole('row').filter({ hasText: 'COMPLETED' }).first();
    test.skip(
      (await completed.count()) === 0,
      'no COMPLETED document present on this stack to inspect',
    );

    // The first cell links to the detail page; clicking the row's link is how a
    // user gets there.
    await completed.getByRole('link').first().click();

    // Detail pages render section/page panels. Assert on something structural
    // rather than on extracted values, which vary by document and model.
    await expect(
      page.getByRole('heading', { level: 2 }).first(),
      'document detail view did not render a section heading',
    ).toBeVisible({ timeout: 60_000 });
  });
});
