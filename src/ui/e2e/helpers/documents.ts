// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { expect, type Page } from '@playwright/test';

/**
 * Statuses that mean "still working". Mirrors ABORTABLE_STATUSES in
 * src/ui/src/components/document-list/documents-table-config.tsx — if that list
 * gains a status and this one doesn't, a document in the new state is wrongly
 * treated as terminal, so keep them in step.
 */
export const IN_FLIGHT_STATUSES = [
  'QUEUED',
  'RUNNING',
  'PREPROCESSING',
  'OCR',
  'CLASSIFYING',
  'EXTRACTING',
  'ASSESSING',
  'POSTPROCESSING',
  'HITL_IN_PROGRESS',
  'SUMMARIZING',
  'EVALUATING',
] as const;

/**
 * Hash routes, used RELATIVE (no leading slash) on purpose.
 *
 * `page.goto('/#/documents')` is root-absolute and discards baseURL's path, which
 * breaks the APIGateway hosting variant (WebUIHosting=APIGateway) where the SPA is
 * served under the REST stage prefix, e.g. https://<id>.execute-api.../api/.
 * `page.goto('#/documents')` resolves against the full baseURL and works for both
 * CloudFront and APIGateway hosting.
 */
export const ROUTES = {
  documents: '#/documents',
  upload: '#/documents/upload',
  configuration: '#/documents/config',
  testStudio: '#/test-studio',
} as const;

/**
 * Waits for the document list table to be rendered and readable.
 *
 * NOTE: column headers are matched with `exact: true`. The table renders both
 * "Status" and "Review Status", so a substring match resolves to two elements and
 * Playwright raises a strict-mode violation instead of asserting anything useful.
 */
export async function gotoDocumentList(page: Page): Promise<void> {
  await page.goto(ROUTES.documents);
  await expect(page.getByRole('columnheader', { name: 'Document ID', exact: true })).toBeVisible();
}

/**
 * Polls the document's Status cell until it leaves the in-flight set.
 *
 * THIS IS THE CORE UAT ASSERTION. It never sleeps a fixed interval and never
 * asserts on appearance — it asks only "did this task reach a conclusion?".
 * A document that never leaves IN_FLIGHT is precisely the failure mode behind
 * accelerator_issues.md #4 (run wedged in EVALUATING) and #6 (HITL silently
 * blocking an unattended run), so those become named failures here.
 *
 * @returns the terminal status text (e.g. 'COMPLETED', 'FAILED')
 */
export async function waitForDocumentTerminal(
  page: Page,
  documentId: string,
  budgetMs = Number(process.env.UAT_DOC_BUDGET_MS ?? 300_000),
): Promise<string> {
  const deadline = Date.now() + budgetMs;
  const pollMs = 10_000;
  let last = '(never observed)';

  while (Date.now() < deadline) {
    const row = page.getByRole('row').filter({ hasText: documentId }).first();

    if (await row.count()) {
      // Read the whole row and match a status token: column order is config-driven
      // (documents-table-config.tsx), so indexing a cell position would be brittle.
      const text = (await row.innerText()).toUpperCase();
      const inFlight = IN_FLIGHT_STATUSES.find((s) => text.includes(s));
      if (inFlight) {
        last = inFlight;
      } else {
        const terminal = ['COMPLETED', 'FAILED', 'ABORTED', 'TIMED_OUT'].find((s) =>
          text.includes(s),
        );
        if (terminal) return terminal;
        last = `unrecognised (row: ${text.slice(0, 120)})`;
      }
    } else {
      last = '(row not present)';
    }

    await page.waitForTimeout(pollMs);
    await page.reload();
    await expect(page.getByRole('columnheader', { name: 'Document ID', exact: true })).toBeVisible();
  }

  throw new Error(
    `Document "${documentId}" did not reach a terminal status within ${budgetMs}ms. ` +
      `Last observed: ${last}. The task was not completable — this is the ` +
      'signature of a wedged pipeline (see accelerator_issues.md #4/#6).',
  );
}

/**
 * Uploads a local file through the Upload Documents panel.
 * @returns the filename, which is how the document is identified in the list
 */
export async function uploadDocument(page: Page, filePath: string): Promise<string> {
  await page.goto(ROUTES.upload);
  await expect(page.getByRole('heading', { name: 'Upload Documents' })).toBeVisible();

  // Cloudscape FileUpload renders a real <input type=file>, which Playwright can
  // set directly — more robust than driving the visible "Choose files" button.
  await page.locator('input[type="file"]').first().setInputFiles(filePath);

  await page.getByRole('button', { name: /^upload/i }).first().click();

  const name = filePath.split('/').pop()!;
  // The panel reports per-file status; wait for the name to appear as confirmation
  // the POST was issued rather than assuming the click worked.
  await expect(page.getByText(name, { exact: false }).first()).toBeVisible({
    timeout: 120_000,
  });
  return name;
}
