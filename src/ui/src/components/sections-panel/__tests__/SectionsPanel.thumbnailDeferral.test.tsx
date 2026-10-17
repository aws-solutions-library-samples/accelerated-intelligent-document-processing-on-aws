// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * SectionsPanel does not sign page thumbnails until the regrouping board is open.
 *
 * Its thumbnails feed only `PageGroupingEditor`, which is rendered behind
 * `isRegrouping` and starts closed. While signing was a local HMAC that cost
 * nothing, calling the hook on mount was harmless. It is now a resolver round
 * trip for every page of the document (issue #1033), and it was half the signing
 * work of opening a document view — spent on images the panel renders none of.
 *
 * The deferral is asserted here against the real `use-page-thumbnails` and the
 * real `api/presign-batch`, with only the transport mocked, because a test that
 * stubs the hook cannot see whether the panel asked it for anything. Opening the
 * board is asserted in the same test: an absence on its own is equally satisfied
 * by a panel that never signs at all, or by a board that cannot open.
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const { graphql } = vi.hoisted(() => ({ graphql: vi.fn() }));

vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }) }));
vi.mock('../../../graphql/generated', () => ({
  updateDocumentSections: 'updateDocumentSections',
  processChanges: 'processChanges',
  skipAllSectionsReview: 'skipAllSectionsReview',
  getFilePresignedUrls: 'getFilePresignedUrls',
}));

vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({ canWrite: true, canReview: true, isReviewerOnly: false }),
}));

// One stable object, not a fresh one per render: `use-page-thumbnails` keys its
// signing effect on `versionIdForUri`, so a new identity on every render would
// re-run it on every render — which the real provider never does, because it
// memoizes the context value.
const VERSION_CONTEXT = { versionIdForUri: () => undefined, runId: null, isHistorical: false };
vi.mock('../../../contexts/document-version', () => ({ useDocumentVersion: () => VERSION_CONTEXT }));

vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: { IDPPattern: 'Pattern 2' } }) }));
vi.mock('../../../contexts/app', () => ({ default: () => ({ currentCredentials: {} }) }));

import SectionsPanel from '../SectionsPanel';
import { FILE_ACCESS_DENIED_MESSAGE } from '../../../hooks/utils/graphql-error';

const SECTIONS = [
  { Id: '1', Class: 'FieldTicket', PageIds: [1, 2] },
  { Id: '2', Class: 'Invoice', PageIds: [3, 4] },
];

const PAGES = [1, 2, 3, 4].map((Id) => ({ Id, ImageUri: `s3://out/${Id}.jpg` }));

const CONFIG = { classes: [{ $id: 'FieldTicket' }, { $id: 'Invoice' }, { $id: 'W2' }] };

/** Calls that asked the batch presign resolver for URLs. */
const presignCalls = () => graphql.mock.calls.filter((c) => (c[0] as { query: string }).query === 'getFilePresignedUrls');

beforeEach(() => {
  graphql.mockReset();
  graphql.mockImplementation((args: { query: string; variables?: { files?: Array<{ s3Uri: string }> } }) => {
    if (args.query === 'getFilePresignedUrls') {
      return Promise.resolve({
        data: {
          getFilePresignedUrls: {
            results: (args.variables?.files ?? []).map((f) => ({
              s3Uri: f.s3Uri,
              presignedUrl: `https://signed.example/${f.s3Uri.slice('s3://'.length)}`,
              contentType: 'image/jpeg',
            })),
          },
        },
      });
    }
    return Promise.resolve({ data: {} });
  });
});

describe('SectionsPanel thumbnail deferral', () => {
  it('signs nothing on mount, and signs the pages once the board is opened', async () => {
    render(
      <SectionsPanel
        sections={SECTIONS as never}
        pages={PAGES as never}
        documentItem={{ objectKey: 'packet.pdf' } as never}
        mergedConfig={CONFIG}
      />,
    );

    // The panel has rendered (so the absence below is not "nothing happened yet")
    // and asked for no presigned URL.
    await waitFor(() => expect(screen.getByRole('button', { name: /Edit page grouping/i })).toBeDefined());
    expect(presignCalls()).toHaveLength(0);

    await userEvent.click(screen.getByRole('button', { name: /Edit page grouping/i }));

    await waitFor(() => expect(presignCalls()).toHaveLength(1));
    expect((presignCalls()[0][0] as { variables: { files: Array<{ s3Uri: string }> } }).variables.files.map((f) => f.s3Uri)).toEqual(
      PAGES.map((p) => p.ImageUri),
    );
  });

  it('says why the board has no page images when the resolver refuses them', async () => {
    // The board's page cards ARE the thumbnails, so a refusal leaves it showing
    // unlabelled empty cards with nothing explaining them. The resolver refuses
    // the whole batch, so this is the shape every refused document takes.
    graphql.mockImplementation((args: { query: string }) =>
      args.query === 'getFilePresignedUrls'
        ? Promise.reject({ errors: [{ errorType: 'Unauthorized', message: 'Unauthorized' }] })
        : Promise.resolve({ data: {} }),
    );

    render(
      <SectionsPanel
        sections={SECTIONS as never}
        pages={PAGES as never}
        documentItem={{ objectKey: 'packet.pdf' } as never}
        mergedConfig={CONFIG}
      />,
    );

    await userEvent.click(screen.getByRole('button', { name: /Edit page grouping/i }));

    await waitFor(() => expect(screen.getByText(FILE_ACCESS_DENIED_MESSAGE)).toBeDefined());
    expect(screen.getByText('Page thumbnails unavailable')).toBeDefined();
  });
});
