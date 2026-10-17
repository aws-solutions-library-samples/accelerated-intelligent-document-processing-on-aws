// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Regression test: editing must be disabled while viewing a historical
 * document version. The panels write to the *current* output objects, so a
 * live edit during a read-only historical view would silently mutate the
 * current document. PagesPanel gates its "Edit Mode" button on
 * useDocumentVersion().isHistorical.
 */

import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({ graphql: vi.fn() }),
}));
// Thumbnail presigning is irrelevant here; stub the batch resolver call.
vi.mock('../../../api/presign-batch', () => ({
  presignBatch: vi.fn(async (requests: Array<{ s3Uri: string }>) => ({
    get: (s3Uri: string) => (requests.some((r) => r.s3Uri === s3Uri) ? 'https://example/thumb.jpg' : undefined),
    size: requests.length,
    failed: false,
    accessDenied: false,
  })),
}));
vi.mock('../../../contexts/app', () => ({ default: () => ({ currentCredentials: {} }) }));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
// Admin — always allowed to edit absent any version/HITL gating.
vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({ isReviewerOnly: false, canWrite: true, canReview: true }),
}));

import PagesPanel from '../PagesPanel';
import { DocumentVersionProvider } from '../../../contexts/document-version';

const PAGES = [{ Id: '1', Class: 'Invoice', ImageUri: 's3://b/doc/pages/1/image.jpg' }];
const DOC = { objectKey: 'doc', objectStatus: 'COMPLETED' };

const renderPanel = (runId: string | null) =>
  render(
    <DocumentVersionProvider runId={runId} files={[]}>
      <PagesPanel {...({ pages: PAGES, documentItem: DOC } as Record<string, unknown>)} />
    </DocumentVersionProvider>,
  );

describe('PagesPanel historical read-only gating', () => {
  it('enables Edit Mode when viewing the current version', () => {
    renderPanel(null);
    const editButton = screen.getByRole('button', { name: /Edit Mode/i });
    expect(editButton).toBeEnabled();
  });

  it('disables Edit Mode when viewing a historical version', () => {
    renderPanel('20250101T090000Z-run-a');
    const editButton = screen.getByRole('button', { name: /Edit Mode/i });
    expect(editButton).toBeDisabled();
  });
});
