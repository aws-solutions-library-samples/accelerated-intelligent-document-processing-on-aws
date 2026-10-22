// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * While a document is in a review queue, the Sections panel offers no way to change it.
 *
 * Edit Mode and Edit Page Grouping are off for every role (here an Admin, whom the
 * legacy gating always let through), and Skip All Reviews is not offered, because the
 * queue item is where a queued document is reviewed, skipped and completed.
 */

import { render, screen } from '@testing-library/react';
import React from 'react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
vi.mock('../../../graphql/generated', () => ({
  updateDocumentSections: 'updateDocumentSections',
  processChanges: 'processChanges',
  skipAllSectionsReview: 'skipAllSectionsReview',
}));
vi.mock('../../../hooks/use-page-thumbnails', () => ({
  default: () => ({ urls: {}, failure: null }),
  THUMBNAIL_FAILURE_MESSAGES: { 'access-denied': 'access denied', failed: 'failed' },
}));
vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({ canWrite: true, canReview: true, isReviewerOnly: false, canWorkReviewQueues: true }),
}));
vi.mock('../../../contexts/document-version', () => ({
  useDocumentVersion: () => ({ versionIdForUri: () => undefined, runId: null, isHistorical: false }),
}));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: { IDPPattern: 'Pattern 2' } }) }));
vi.mock('../../../contexts/app', () => ({ default: () => ({ currentCredentials: {} }) }));

import SectionsPanel from '../SectionsPanel';

const renderPanel = (hitlStatus: string) =>
  render(
    <SectionsPanel
      sections={[{ Id: '1', Class: 'Invoice', PageIds: [1] }] as never}
      pages={[{ Id: 1, ImageUri: 's3://out/1.jpg' }] as never}
      documentItem={{ objectKey: 'packet.pdf', objectStatus: 'COMPLETED', hitlTriggered: true, hitlStatus } as never}
      mergedConfig={{ classes: [{ $id: 'Invoice' }] }}
    />,
  );

// The confirm modal's own "Skip All Reviews" button is always in the DOM; only the
// header action counts.
const skipAllAction = () =>
  screen.queryAllByRole('button', { name: /Skip All Reviews/i }).filter((button) => !button.closest('[role="dialog"]'));

const isOff = (button: HTMLElement) => button.hasAttribute('disabled') || button.getAttribute('aria-disabled') === 'true';

describe('SectionsPanel review-queue gating', () => {
  it('turns Edit Mode and Edit Page Grouping off while the document is in a review queue', () => {
    renderPanel('in_review');
    expect(isOff(screen.getByRole('button', { name: /^Edit Mode/i }))).toBe(true);
    expect(isOff(screen.getByRole('button', { name: /Edit Page Grouping/i }))).toBe(true);
  });

  it('does not offer Skip All Reviews for a queued document', () => {
    renderPanel('in_review');
    expect(skipAllAction()).toHaveLength(0);
  });

  it('keeps Skip All Reviews for a legacy pending review', () => {
    renderPanel('Review Pending');
    expect(skipAllAction()).toHaveLength(1);
    expect(isOff(screen.getByRole('button', { name: /^Edit Mode/i }))).toBe(false);
  });
});
