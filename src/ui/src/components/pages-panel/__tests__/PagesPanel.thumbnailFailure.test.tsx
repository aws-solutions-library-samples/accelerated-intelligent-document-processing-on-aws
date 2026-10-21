// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * What the Pages table says when its thumbnails could not be signed.
 *
 * Thumbnails are presigned by the resolver now, and its per-document scope check
 * refuses the **whole** batch (issue #1033) — so a caller outside a document's
 * scope gets no URL for any page, all at once. The Thumbnail column renders "No
 * image" per cell, which is correct for a page that has no image and silent about
 * a refusal: a reader sees a table that looks complete except for the pictures,
 * with nothing saying why or that anything went wrong.
 *
 * The table therefore carries one alert for the surface, with the reason. These
 * tests pin the reason reaching it — an authorization refusal gets the shared
 * access-denied copy, any other failure gets the load-failed copy — and pin that a
 * successful signing shows no alert at all, because an alert that is always there
 * is one nobody reads.
 */

import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

const { mockPresignBatch } = vi.hoisted(() => ({ mockPresignBatch: vi.fn() }));

vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
vi.mock('../../../api/presign-batch', () => ({ presignBatch: mockPresignBatch }));
vi.mock('../../../contexts/app', () => ({ default: () => ({ currentCredentials: {} }) }));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({ isReviewerOnly: false, canWrite: true, canReview: true }),
}));

import PagesPanel from '../PagesPanel';
import { DocumentVersionProvider } from '../../../contexts/document-version';
import { FILE_ACCESS_DENIED_MESSAGE } from '../../../hooks/utils/graphql-error';

const PAGES = [{ Id: '1', Class: 'Invoice', ImageUri: 's3://b/doc/pages/1/image.jpg' }];
const DOC = { objectKey: 'doc', objectStatus: 'COMPLETED' };

/** A {@link import('../../../api/presign-batch').PresignBatchResult} that signed nothing. */
const refused = (accessDenied: boolean) => ({
  get: () => undefined,
  size: 0,
  failed: true,
  accessDenied,
});

const renderPanel = () =>
  render(
    <DocumentVersionProvider runId={null} files={[]}>
      <PagesPanel {...({ pages: PAGES, documentItem: DOC } as Record<string, unknown>)} />
    </DocumentVersionProvider>,
  );

describe('PagesPanel thumbnail failure reporting', () => {
  it('shows the shared access-denied copy when the resolver refuses the document', async () => {
    mockPresignBatch.mockResolvedValue(refused(true));

    renderPanel();

    await waitFor(() => expect(screen.getByText(FILE_ACCESS_DENIED_MESSAGE)).toBeDefined());
    expect(screen.getByText('Page thumbnails unavailable')).toBeDefined();
  });

  it('shows the load-failed copy for any other failure, not the permission copy', async () => {
    mockPresignBatch.mockResolvedValue(refused(false));

    renderPanel();

    await waitFor(() => expect(screen.getByText('Page thumbnails could not be loaded.')).toBeDefined());
    // Telling a user to ask for a role when the call simply failed sends them to
    // an administrator who can do nothing about it.
    expect(screen.queryByText(FILE_ACCESS_DENIED_MESSAGE)).toBeNull();
  });

  it('shows no alert when signing succeeds', async () => {
    mockPresignBatch.mockResolvedValue({
      get: () => 'https://signed.example/thumb.jpg',
      size: 1,
      failed: false,
      accessDenied: false,
    });

    renderPanel();

    // Wait for the thumbnail itself, so the absence below is a settled state
    // rather than the effect not having run.
    await waitFor(() => expect(screen.getByAltText('Page thumbnail')).toBeDefined());
    expect(screen.queryByText('Page thumbnails unavailable')).toBeNull();
  });
});
