// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Edit Mode is off for every role while the document is in a review queue.
 *
 * An Admin's or Author's Save on the document page writes the result file directly:
 * outside the item's assignment, and overwritten by the reviewer's corrections when
 * the queue review completes. The role here is Admin, which the legacy gating always
 * let through, so this pins the queue term rather than a role term.
 */

import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({ graphql: vi.fn() }),
}));
vi.mock('../../../api/presign-batch', () => ({
  presignBatch: vi.fn(async () => ({ get: () => undefined, size: 0, failed: false, accessDenied: false })),
}));
vi.mock('../../../contexts/app', () => ({ default: () => ({ currentCredentials: {} }) }));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({ isReviewerOnly: false, canWrite: true, canReview: true, canWorkReviewQueues: true }),
}));

import PagesPanel from '../PagesPanel';
import { DocumentVersionProvider } from '../../../contexts/document-version';

const PAGES = [{ Id: '1', Class: 'Invoice', ImageUri: 's3://b/doc/pages/1/image.jpg' }];

const editButton = (hitlStatus: string) => {
  render(
    <DocumentVersionProvider runId={null} files={[]}>
      <PagesPanel
        {...({ pages: PAGES, documentItem: { objectKey: 'doc', objectStatus: 'COMPLETED', hitlStatus } } as Record<string, unknown>)}
      />
    </DocumentVersionProvider>,
  );
  return screen.getByRole('button', { name: /Edit Mode/i });
};

// Cloudscape renders a button with a disabledReason as aria-disabled (still focusable,
// so the reason can be read), not with the disabled attribute.
const isOff = (button: HTMLElement) => button.hasAttribute('disabled') || button.getAttribute('aria-disabled') === 'true';

describe('PagesPanel review-queue gating', () => {
  it('turns Edit Mode off for an Admin while the document is in a review queue', () => {
    expect(isOff(editButton('in_review'))).toBe(true);
  });

  it('leaves Edit Mode on once the queue review has finished', () => {
    expect(isOff(editButton('complete'))).toBe(false);
  });
});
