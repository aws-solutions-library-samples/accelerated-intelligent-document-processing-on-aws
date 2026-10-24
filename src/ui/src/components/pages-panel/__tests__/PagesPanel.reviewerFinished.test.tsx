// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * A Reviewer-only user cannot turn on Edit Mode once the document's review has
 * finished, whichever flow finished it.
 *
 * The case that matters is a Reviewer who still holds a legacy claim
 * (`HITLReviewOwner`): without one, a triggered document is already locked until
 * Start Review. A queue review that ended `complete` or `rejected` used to leave Edit
 * Mode on for that Reviewer, because the lock knew only the legacy completed and
 * skipped spellings. The document item goes through the same mapping the document
 * page uses.
 */

import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({ graphql: vi.fn() }),
}));
vi.mock('../../../api/presign-batch', () => ({
  presignBatch: vi.fn(async () => ({ get: () => undefined, size: 0, failed: false, accessDenied: false })),
}));
vi.mock('../../../contexts/app', () => ({ default: () => ({ currentCredentials: {} }) }));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({ isReviewerOnly: true, canWrite: false, canReview: true, canWorkReviewQueues: false }),
}));

import PagesPanel from '../PagesPanel';
import mapDocumentsAttributes from '../../common/map-document-attributes';
import { DocumentVersionProvider } from '../../../contexts/document-version';
import { LegacyHitlStatus, ReviewStatus } from '../../common/review-status';

const PAGES = [{ Id: '1', Class: 'Invoice', ImageUri: 's3://b/doc/pages/1/image.jpg' }];

const panel = (HITLStatus: string) => {
  const [documentItem] = mapDocumentsAttributes([
    { ObjectKey: 'doc', ObjectStatus: 'COMPLETED', HITLStatus, HITLReviewOwner: 'rev' } as never,
  ]);
  return (
    <DocumentVersionProvider runId={null} files={[]}>
      <PagesPanel {...({ pages: PAGES, documentItem } as Record<string, unknown>)} />
    </DocumentVersionProvider>
  );
};

const editButton = (HITLStatus: string) => {
  render(panel(HITLStatus));
  return screen.getByRole('button', { name: /Edit Mode/i });
};

const isOff = (button: HTMLElement) => button.hasAttribute('disabled') || button.getAttribute('aria-disabled') === 'true';

describe('PagesPanel Reviewer-only lock on a finished review', () => {
  it.each([ReviewStatus.COMPLETE, ReviewStatus.REJECTED])('turns Edit Mode off once a queue review ended %s', (status) => {
    expect(isOff(editButton(status))).toBe(true);
  });

  it.each([LegacyHitlStatus.REVIEW_COMPLETED, LegacyHitlStatus.SKIPPED, 'COMPLETED'])(
    'turns Edit Mode off once a legacy review finished (%s)',
    (status) => {
      expect(isOff(editButton(status))).toBe(true);
    },
  );

  it('leaves Edit Mode on for the Reviewer who holds an open legacy review', () => {
    expect(isOff(editButton(LegacyHitlStatus.IN_PROGRESS))).toBe(false);
  });

  it.each([ReviewStatus.COMPLETE, ReviewStatus.REJECTED])('leaves Edit Mode when the review ends %s mid-edit', async (status) => {
    const { rerender } = render(panel(LegacyHitlStatus.IN_PROGRESS));
    await userEvent.click(screen.getByRole('button', { name: /Edit Mode/i }));
    // In Edit Mode the header shows Cancel / Process Changes in place of Edit Mode.
    expect(screen.queryByRole('button', { name: /Edit Mode/i })).not.toBeInTheDocument();

    rerender(panel(status));

    expect(isOff(screen.getByRole('button', { name: /Edit Mode/i }))).toBe(true);
  });
});
