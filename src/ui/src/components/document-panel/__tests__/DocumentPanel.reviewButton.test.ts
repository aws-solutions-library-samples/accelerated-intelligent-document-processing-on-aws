// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * A queued document's page offers one action: Edit, which opens its queue item for
 * every role that can work queue items (there is no separate Review button).
 *
 * Asserted against the source, as VisualEditorModal.classGating.test.ts does: the
 * decision is a condition and a label, and rendering DocumentPanel means mocking most
 * of the application.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const SOURCE = readFileSync(join(__dirname, '..', 'DocumentPanel.tsx'), 'utf-8');

describe('DocumentPanel Edit button on a queued document', () => {
  it('opens the queue item for every role that can work queue items, not only Admins', () => {
    expect(SOURCE).toMatch(/editInQueue = isInReviewQueue\(localItem\?\.hitlStatus\) && canWorkReviewQueues;/);
  });

  it('labels it Edit and points it at the queue item', () => {
    const at = SOURCE.indexOf('{editInQueue ? (');
    expect(at).toBeGreaterThan(-1);
    const button = SOURCE.slice(at, SOURCE.indexOf('</Button>', at));
    expect(button).toMatch(/href=\{reviewItemHref\(localItem\.objectKey\)\}/);
    expect(button).toMatch(/>\s*Edit\s*$/);
  });

  it('has no separate Review button', () => {
    expect(SOURCE).not.toMatch(/>\s*Review\s*<\/Button>/);
  });

  it('does not offer the legacy Start Review on a queued document', () => {
    expect(SOURCE).toMatch(/hasPendingHITL = [^;]*!hitlView\.queueManaged/);
    expect(SOURCE).not.toMatch(/Open in review queue/);
  });
});

describe('DocumentPanel Start Review failure', () => {
  // The REST client rejects with the dispatcher's `{ errors: [...] }` body, not an
  // Error, so `(error as Error).message` is undefined there; getErrorMessage reads
  // the server's message (e.g. REVIEW_FINISHED) out of that body.
  it('shows the message the server refused the claim with', () => {
    expect(SOURCE).toMatch(/setErrorMessage\(`Failed to start review: \$\{getErrorMessage\(error\)\}`\)/);
    expect(SOURCE).not.toMatch(/Failed to start review: \$\{\(error as Error\)\.message/);
  });
});
