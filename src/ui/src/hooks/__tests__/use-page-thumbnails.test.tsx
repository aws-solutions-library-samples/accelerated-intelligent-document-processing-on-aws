// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Thumbnail signing for a document's pages.
 *
 * The hook used to sign each page locally, which cost nothing, so a surface could
 * call it on mount whether or not it rendered the result. Signing is now a
 * resolver round trip (issue #1033), which makes two things properties worth
 * pinning rather than implementation details:
 *
 * - **One call for the whole page list.** A regression to one call per page is
 *   invisible in the rendered output and turns a 200-page document view into 200
 *   authorised round trips.
 * - **`{ enabled: false }` signs nothing.** SectionsPanel's thumbnails feed only
 *   the page-regrouping board, which is closed by default; a gate that still
 *   issues the call is indistinguishable from no gate in anything but a network
 *   trace.
 *
 * - **A refusal is reported as a reason, not as absent URLs.** The resolver fails
 *   the whole batch on a scope refusal, so every page of a document the caller may
 *   not read goes missing at once; without `failure` the surface cannot tell that
 *   from a document that has no page images, and it showed a spinner for ever.
 *
 * Two behaviours predate the batching and are kept because a caller depends on
 * each: a page whose URL could not be signed maps to `null` rather than being
 * absent (so the board can draw a placeholder instead of a broken image), and the
 * per-URI `versionId` pinning that makes a historical view show the pages as they
 * were.
 */

import { renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const { mockGraphql } = vi.hoisted(() => ({ mockGraphql: vi.fn() }));

vi.mock('../../api/client-shim', () => ({ generateClient: () => ({ graphql: mockGraphql }) }));
vi.mock('../../graphql/generated', () => ({ getFilePresignedUrls: 'query GetFilePresignedUrls' }));

const VERSION_CONTEXT = {
  isHistorical: false,
  runId: null as string | null,
  versionIdForUri: (_uri?: string | null): string | undefined => undefined,
};
vi.mock('../../contexts/document-version', () => ({ useDocumentVersion: () => VERSION_CONTEXT }));

import { THUMBNAIL_FAILURE_MESSAGES, usePageThumbnails } from '../use-page-thumbnails';
import { FILE_ACCESS_DENIED_MESSAGE } from '../utils/graphql-error';

interface PresignCall {
  query: string;
  variables: { files: Array<{ s3Uri: string; versionId?: string }> };
}

const filesOf = (call: unknown): Array<{ s3Uri: string; versionId?: string }> => (call as PresignCall).variables.files;

const PAGES = [1, 2, 3, 4, 5].map((Id) => ({ Id, ImageUri: `s3://out/doc/pages/${Id}/image.jpg` }));

const signEverything = async (args: PresignCall) => ({
  data: {
    getFilePresignedUrls: {
      results: args.variables.files.map((f) => ({
        s3Uri: f.s3Uri,
        presignedUrl: `https://signed.example/${f.s3Uri.slice('s3://'.length)}`,
        contentType: 'image/jpeg',
      })),
    },
  },
});

beforeEach(() => {
  mockGraphql.mockReset().mockImplementation(signEverything);
  VERSION_CONTEXT.runId = null;
  VERSION_CONTEXT.isHistorical = false;
  VERSION_CONTEXT.versionIdForUri = () => undefined;
});

describe('usePageThumbnails', () => {
  it('signs N pages in one call, not N calls', async () => {
    const { result } = renderHook(() => usePageThumbnails(PAGES));

    await waitFor(() => expect(Object.keys(result.current.urls)).toHaveLength(PAGES.length));
    expect(mockGraphql).toHaveBeenCalledTimes(1);
    expect(filesOf(mockGraphql.mock.calls[0][0]).map((f) => f.s3Uri)).toEqual(PAGES.map((p) => p.ImageUri));
    expect(result.current.urls['3']).toBe('https://signed.example/out/doc/pages/3/image.jpg');
    expect(result.current.failure).toBeNull();
  });

  it('signs nothing and returns an empty map when disabled', async () => {
    const { result } = renderHook(() => usePageThumbnails(PAGES, { enabled: false }));

    // Settle the effect before asserting an absence, so this is not satisfied by
    // the call simply not having been issued yet.
    await waitFor(() => expect(result.current.urls).toEqual({}));
    expect(mockGraphql).not.toHaveBeenCalled();
    // Not attempted is not a failure: a disabled surface must not show an error.
    expect(result.current.failure).toBeNull();

    // The same pages DO get signed when the gate opens, which is what makes the
    // assertion above about the gate rather than about the page list.
    const enabled = renderHook(() => usePageThumbnails(PAGES, { enabled: true }));
    await waitFor(() => expect(Object.keys(enabled.result.current.urls)).toHaveLength(PAGES.length));
    expect(mockGraphql).toHaveBeenCalledTimes(1);
  });

  it('defaults to enabled when no options are passed', async () => {
    const { result } = renderHook(() => usePageThumbnails(PAGES));
    await waitFor(() => expect(Object.keys(result.current.urls)).toHaveLength(PAGES.length));
  });

  it('maps a page the server returned no URL for to null, and leaves a page with no ImageUri absent', async () => {
    mockGraphql.mockImplementation(async (args: PresignCall) => ({
      data: {
        getFilePresignedUrls: {
          results: args.variables.files
            .filter((f) => !f.s3Uri.endsWith('/2/image.jpg'))
            .map((f) => ({ s3Uri: f.s3Uri, presignedUrl: 'https://signed.example/ok', contentType: 'image/jpeg' })),
        },
      },
    }));

    const pages = [...PAGES, { Id: 99 }];
    const { result } = renderHook(() => usePageThumbnails(pages));

    await waitFor(() => expect(result.current.urls['1']).toBe('https://signed.example/ok'));
    expect(result.current.urls['2']).toBeNull();
    expect('2' in result.current.urls).toBe(true);
    // A page with no ImageUri stays ABSENT, so it is still distinguishable from
    // the page above whose URL the server declined to mint.
    expect('99' in result.current.urls).toBe(false);
  });

  it('maps every page to null when the whole call fails', async () => {
    mockGraphql.mockRejectedValue(new Error('network down'));

    const { result } = renderHook(() => usePageThumbnails(PAGES));

    await waitFor(() => expect(Object.keys(result.current.urls)).toHaveLength(PAGES.length));
    expect(Object.values(result.current.urls)).toEqual(PAGES.map(() => null));
    expect(result.current.failure).toBe('failed');
  });

  it('reports an authorization refusal as access-denied, so the panels can say why', async () => {
    mockGraphql.mockRejectedValue({ errors: [{ errorType: 'Unauthorized', message: 'Unauthorized' }] });

    const { result } = renderHook(() => usePageThumbnails(PAGES));

    await waitFor(() => expect(result.current.failure).toBe('access-denied'));
    expect(THUMBNAIL_FAILURE_MESSAGES['access-denied']).toBe(FILE_ACCESS_DENIED_MESSAGE);
    // The urls map is still fully populated with nulls rather than empty: the
    // caller needs "asked and refused" to stay distinct from "never asked".
    expect(Object.keys(result.current.urls)).toHaveLength(PAGES.length);
  });

  it('carries the versionId from versionIdForUri per entry', async () => {
    VERSION_CONTEXT.runId = 'run-7';
    VERSION_CONTEXT.isHistorical = true;
    VERSION_CONTEXT.versionIdForUri = (uri) => (uri?.endsWith('/2/image.jpg') ? 'ver-2' : undefined);

    const { result } = renderHook(() => usePageThumbnails(PAGES.slice(0, 3)));

    await waitFor(() => expect(Object.keys(result.current.urls)).toHaveLength(3));
    expect(filesOf(mockGraphql.mock.calls[0][0])).toEqual([
      { s3Uri: 's3://out/doc/pages/1/image.jpg' },
      { s3Uri: 's3://out/doc/pages/2/image.jpg', versionId: 'ver-2' },
      { s3Uri: 's3://out/doc/pages/3/image.jpg' },
    ]);
  });
});
