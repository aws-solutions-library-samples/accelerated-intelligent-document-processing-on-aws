// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * A queued document's page offers one action: Review, to its queue item.
 *
 * Asserted against the source, as VisualEditorModal.classGating.test.ts does: the
 * decision is a condition and a label, and rendering DocumentPanel means mocking most
 * of the application.
 */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

const SOURCE = readFileSync(join(__dirname, '..', 'DocumentPanel.tsx'), 'utf-8');

describe('DocumentPanel Review button', () => {
  it('shows Review to every role that can open a queue item, not only Admins', () => {
    expect(SOURCE).toMatch(/showReviewInQueue = isInReviewQueue\(localItem\?\.hitlStatus\) && canWorkReviewQueues;/);
  });

  it('labels it Review and points it at the queue item', () => {
    const at = SOURCE.indexOf('{showReviewInQueue && (');
    expect(at).toBeGreaterThan(-1);
    const button = SOURCE.slice(at, SOURCE.indexOf('</Button>', at));
    expect(button).toMatch(/href=\{reviewItemHref\(localItem\.objectKey\)\}/);
    expect(button).toMatch(/>\s*Review\s*$/);
  });

  it('does not offer the legacy Start Review on a queued document', () => {
    expect(SOURCE).toMatch(/hasPendingHITL = [^;]*!hitlView\.queueManaged/);
    expect(SOURCE).not.toMatch(/Open in review queue/);
  });
});
