// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Page edits are not offered for a document whose configuration sets `use_bda`.
 *
 * A page class reset or text edit clears the extracted values of the sections it
 * touches, and a BDA reprocess keeps the existing sections, so nothing regenerates
 * them. BDA mode is read from the document's own configuration; the stack's
 * `IDPPattern` is mocked as `Unified`, the constant every stack carries.
 */

import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
vi.mock('../../../api/presign-batch', () => ({
  presignBatch: vi.fn(async () => ({ get: () => undefined, size: 0, failed: false, accessDenied: false })),
}));
vi.mock('../../../contexts/app', () => ({ default: () => ({ currentCredentials: {} }) }));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: { IDPPattern: 'Unified' } }) }));
vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({ isReviewerOnly: false, canWrite: true, canReview: true }),
}));

import PagesPanel from '../PagesPanel';
import { DocumentVersionProvider } from '../../../contexts/document-version';

const PAGES = [{ Id: '1', Class: 'Invoice', ImageUri: 's3://b/doc/pages/1/image.jpg' }];
const DOC = { objectKey: 'doc', objectStatus: 'COMPLETED' };

const renderPanel = (mergedConfig: Record<string, unknown> | null) =>
  render(
    <DocumentVersionProvider runId={null} files={[]}>
      <PagesPanel {...({ pages: PAGES, documentItem: DOC, mergedConfig } as Record<string, unknown>)} />
    </DocumentVersionProvider>,
  );

// Cloudscape renders a modal's content while it is hidden, so whether the panel
// ENTERED page Edit Mode is read from its header: the Edit Mode button is replaced by
// Cancel / Process Changes once it does.
const inPageEditMode = () => !screen.queryByRole('button', { name: /^Edit Mode$/ });

describe('PagesPanel in BDA mode', () => {
  it('explains instead of entering page Edit Mode', async () => {
    renderPanel({ use_bda: true });

    await userEvent.click(screen.getByRole('button', { name: /^Edit Mode$/ }));

    expect(inPageEditMode()).toBe(false);
    expect(screen.getByText(/Page edits are not available in BDA mode/i)).toBeInTheDocument();
  });

  it.each([{ use_bda: false }, { use_bda: 'false' }, null])('enters page Edit Mode in pipeline mode (%j)', async (config) => {
    renderPanel(config);

    await userEvent.click(screen.getByRole('button', { name: /^Edit Mode$/ }));

    expect(inPageEditMode()).toBe(true);
  });
});
