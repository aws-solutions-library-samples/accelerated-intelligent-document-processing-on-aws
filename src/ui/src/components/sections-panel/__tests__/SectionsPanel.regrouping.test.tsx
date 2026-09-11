// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Re-grouping a processed document's pages from the document view.
 *
 * The property this file exists for: page ids pass through **unconverted**. Document
 * page ids are 1-based, except BDA / Pattern-1 which is 0-based, which is exactly why
 * `section-grouping` is base-agnostic — a conversion here would put an off-by-one into
 * the one surface with two numbering conventions. The test-set path converts because it
 * stores 0-based `page_indices`; this one must not.
 *
 * Also pins the distinction from Edit Mode, whose save regenerates the extracted values.
 * Two routes to a similar-looking outcome is only defensible if the difference is stated,
 * so it is asserted here rather than left to the reader.
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
  default: () => ({ '1': 'blob:1', '2': 'blob:2', '3': 'blob:3', '4': 'blob:4' }),
}));

vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({ canWrite: true, canReview: true, isReviewerOnly: false }),
}));

vi.mock('../../../contexts/document-version', () => ({
  useDocumentVersion: () => ({ versionIdForUri: () => undefined, runId: null, isHistorical: false }),
}));

vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: { IDPPattern: 'Pattern 2' } }) }));
vi.mock('../../../contexts/app', () => ({ default: () => ({ currentCredentials: {} }) }));

import SectionsPanel from '../SectionsPanel';

const SECTIONS = [
  { Id: '1', Class: 'FieldTicket', PageIds: [1, 2] },
  { Id: '2', Class: 'Invoice', PageIds: [3, 4] },
];

const PAGES = [1, 2, 3, 4].map((Id) => ({ Id, ImageUri: `s3://out/${Id}.jpg` }));

const CONFIG = { classes: [{ $id: 'FieldTicket' }, { $id: 'Invoice' }, { $id: 'W2' }] };

beforeEach(() => {
  graphql.mockReset();
  graphql.mockImplementation((args: { query: string }) => {
    if (args.query === 'updateDocumentSections') {
      return Promise.resolve({
        data: { updateDocumentSections: { success: true, message: 'Page grouping saved as 2 section(s).', processingJobId: null } },
      });
    }
    return Promise.resolve({ data: {} });
  });
});

const renderPanel = () =>
  render(
    <SectionsPanel
      sections={SECTIONS as never}
      pages={PAGES as never}
      documentItem={{ objectKey: 'packet.pdf' } as never}
      mergedConfig={CONFIG}
    />,
  );

const openBoard = async () => {
  await userEvent.click(screen.getByRole('button', { name: /Edit page grouping/i }));
};

const movePageTo = async (pageId: number, sectionId: string) => {
  await userEvent.click(screen.getByRole('button', { name: new RegExp(`^Move page ${pageId}\\b`) }));
  await userEvent.click(await screen.findByRole('menuitem', { name: new RegExp(`^Section ${sectionId}\\b`) }));
};

describe('SectionsPanel page regrouping', () => {
  it('offers the board alongside Edit Mode, not instead of it', () => {
    renderPanel();

    // Both routes stay available; their labels carry the difference in consequence.
    expect(screen.getByRole('button', { name: /Edit page grouping/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Edit Mode/i })).toBeInTheDocument();
  });

  it('sends page ids unconverted, in the document own numbering', async () => {
    renderPanel();
    await openBoard();
    await movePageTo(3, '1');
    await userEvent.click(screen.getByRole('button', { name: /Save page grouping/i }));

    await waitFor(() => {
      const call = graphql.mock.calls.find((c) => c[0].query === 'updateDocumentSections');
      expect(call).toBeDefined();
      // 1-based here, and NOT shifted: the test-set path converts because it stores
      // 0-based page_indices, this one stores page ids as they are.
      expect(call![0].variables.sections).toEqual([
        { sectionId: '1', classification: 'FieldTicket', pageIds: ['1', '2', '3'] },
        { sectionId: '2', classification: 'Invoice', pageIds: ['4'] },
      ]);
    });
  });

  it('sends a reordered section in the reviewer order, not sorted', async () => {
    // Page order within a section is scored (`split_accuracy_with_order` compares the
    // lists with `==`), so the document view must persist it as authored. This surface
    // shares the board with the test-set view, so the gesture is the same one.
    renderPanel();
    await openBoard();

    await userEvent.click(screen.getByRole('button', { name: /^Move page 1\b/ }));
    await userEvent.click(await screen.findByRole('menuitem', { name: 'Move later' }));
    await userEvent.click(screen.getByRole('button', { name: /Save page grouping/i }));

    await waitFor(() => {
      const call = graphql.mock.calls.find((c) => c[0].query === 'updateDocumentSections');
      expect(call).toBeDefined();
      expect(call![0].variables.sections[0].pageIds).toEqual(['2', '1']);
    });
  });

  it('treats a pure reorder as a change, so Save is reachable', async () => {
    renderPanel();
    await openBoard();

    expect(screen.getByRole('button', { name: /Save page grouping/i })).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: /^Move page 1\b/ }));
    await userEvent.click(await screen.findByRole('menuitem', { name: 'Move later' }));

    expect(screen.getByRole('button', { name: /Save page grouping/i })).toBeEnabled();
  });

  it('says the values are kept and the document is not reprocessed', async () => {
    renderPanel();
    await openBoard();

    expect(screen.getByText(/including any a reviewer corrected/)).toBeInTheDocument();
    expect(screen.getByText(/reprocessed, so they may no longer match their pages/)).toBeInTheDocument();
    // Points at the other route for the reviewer who does want a reprocess.
    expect(screen.getByText(/Process Changes/)).toBeInTheDocument();
  });

  it('does not call processChanges, which would regenerate the values', async () => {
    renderPanel();
    await openBoard();
    await movePageTo(3, '1');
    await userEvent.click(screen.getByRole('button', { name: /Save page grouping/i }));

    await waitFor(() => expect(graphql.mock.calls.some((c) => c[0].query === 'updateDocumentSections')).toBe(true));
    expect(graphql.mock.calls.some((c) => c[0].query === 'processChanges')).toBe(false);
  });

  it('surfaces a refusal rather than swallowing it', async () => {
    // The resolver returns a reasoned refusal for the cases a reviewer can act on —
    // a document mid-pipeline most of all — so it has to reach the screen.
    graphql.mockImplementation((args: { query: string }) => {
      if (args.query === 'updateDocumentSections') {
        return Promise.resolve({
          data: {
            updateDocumentSections: {
              success: false,
              message: 'packet.pdf is currently running. Wait for processing to finish.',
              processingJobId: null,
            },
          },
        });
      }
      return Promise.resolve({ data: {} });
    });

    renderPanel();
    await openBoard();
    await movePageTo(3, '1');
    await userEvent.click(screen.getByRole('button', { name: /Save page grouping/i }));

    expect(await screen.findByText(/currently running/)).toBeInTheDocument();
    // Still open, so the reviewer's work is not lost to a refusal.
    expect(screen.getByRole('button', { name: /Save page grouping/i })).toBeInTheDocument();
  });

  it('closes the board and reports success on a good save', async () => {
    renderPanel();
    await openBoard();
    await movePageTo(3, '1');
    await userEvent.click(screen.getByRole('button', { name: /Save page grouping/i }));

    expect(await screen.findByText(/Page grouping saved as 2 section/)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('button', { name: /Save page grouping/i })).not.toBeInTheDocument());
  });
});
