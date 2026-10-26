// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * BDA mode is read from the document's configuration, and offers data-only editing.
 *
 * A BDA reprocess keeps the sections a document already has, so a structural edit
 * (reclassify, add or remove a section, regroup pages) clears values nothing then
 * regenerates. The panel therefore offers neither the page-grouping board nor Edit
 * Mode's structure controls for a document whose configuration sets `use_bda`, and its
 * save sends no section changes. The stack's `IDPPattern` is mocked as the constant
 * every stack carries, `Unified`, to pin that it no longer decides.
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }) }));
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
  default: () => ({ canWrite: true, canReview: true, isReviewerOnly: false }),
}));
vi.mock('../../../contexts/document-version', () => ({
  useDocumentVersion: () => ({ versionIdForUri: () => undefined, runId: null, isHistorical: false }),
}));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: { IDPPattern: 'Unified' } }) }));
vi.mock('../../../contexts/app', () => ({ default: () => ({ currentCredentials: {} }) }));

import SectionsPanel from '../SectionsPanel';

const SECTIONS = [
  { Id: '1', Class: 'Invoice', PageIds: [0] },
  { Id: '2', Class: 'Receipt', PageIds: [1] },
];
const PAGES = [0, 1].map((Id) => ({ Id, ImageUri: `s3://out/${Id}.jpg` }));
const CLASSES = [{ $id: 'Invoice' }, { $id: 'Receipt' }];

beforeEach(() => {
  graphql.mockReset();
  graphql.mockResolvedValue({ data: { processChanges: { success: true, message: 'ok', processingJobId: 'j' } } });
  vi.spyOn(window, 'alert').mockImplementation(() => undefined);
});

const renderPanel = (useBda: unknown, config?: Record<string, unknown> | null) =>
  render(
    <SectionsPanel
      sections={SECTIONS as never}
      pages={PAGES as never}
      documentItem={{ objectKey: 'packet.pdf', objectStatus: 'COMPLETED' } as never}
      mergedConfig={config === undefined ? { use_bda: useBda, classes: CLASSES } : config}
    />,
  );

describe('SectionsPanel in BDA mode', () => {
  it.each([true, 'true'])('offers no page grouping or structure editing when use_bda is %j', async (useBda) => {
    renderPanel(useBda);

    expect(screen.queryByRole('button', { name: /Edit Page Grouping/i })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /Edit Mode/i }));
    expect(screen.queryByRole('button', { name: /Add Section/i })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Save and Reprocess/i })).toBeInTheDocument();
  });

  it('keeps data-only reprocessing: the save sends no section changes', async () => {
    renderPanel(true);

    await userEvent.click(screen.getByRole('button', { name: /Edit Mode/i }));
    // Cloudscape keeps the hidden confirm dialog in the DOM, so pressing its button
    // alone would not show that Save and Reprocess can be pressed.
    const save = screen.getByRole('button', { name: /Save and Reprocess/i });
    expect(save).toBeEnabled();
    await userEvent.click(save);
    await userEvent.click(await screen.findByRole('button', { name: /Confirm & Process/i }));

    await waitFor(() => {
      const call = graphql.mock.calls.find((c) => c[0].query === 'processChanges');
      expect(call).toBeDefined();
      expect(call![0].variables.modifiedSections).toEqual([]);
    });
  });

  it('offers the structure controls while no configuration has loaded', async () => {
    renderPanel(undefined, null);

    expect(screen.getByRole('button', { name: /Edit Page Grouping/i })).toBeInTheDocument();
  });

  it.each([false, 'false', undefined, null])('offers the structure controls in pipeline mode (use_bda %j)', async (useBda) => {
    renderPanel(useBda);

    expect(screen.getByRole('button', { name: /Edit Page Grouping/i })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /Edit Mode/i }));
    expect(screen.getByRole('button', { name: /Add Section/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Process Changes$/i })).toBeInTheDocument();
  });
});
