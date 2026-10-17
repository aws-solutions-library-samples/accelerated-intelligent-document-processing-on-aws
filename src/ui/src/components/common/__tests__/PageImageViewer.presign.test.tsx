// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * How the page-image viewer gets a displayable URL for each page.
 *
 * Two kinds of `ImageUri` reach this component and they must not be treated the
 * same way. An `s3://` URI has to be signed, and since the browser's Cognito role
 * no longer grants the document buckets (issue #1033) that signing is a resolver
 * call — one call for the whole page list, not one per page. A `blob:`, `data:`
 * or `https:` URI is already fetchable: Test Studio renders pages client-side from
 * a PDF and hands them straight in, so sending one to the resolver would both
 * waste a refusal and, by failing, blank a page that was working.
 *
 * The third property is what a page with **no** URL shows. Signing is finished by
 * the time the viewer renders a page, so a missing URL is a settled outcome — a
 * refusal, a failure, or a page that carries no image — and each says so. It used
 * to fall through to a spinner, which for a document the caller's scope excludes
 * meant every page spinning for ever with no message: the resolver refuses the
 * whole batch, so there is no page that ever arrives.
 *
 * The real `api/presign-batch` is used here with only the transport mocked, so
 * "one call" is asserted at the wire rather than against a stub of the module
 * whose job it is.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { mockGraphql } = vi.hoisted(() => ({ mockGraphql: vi.fn() }));

vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: mockGraphql }) }));
vi.mock('../../../graphql/generated', () => ({ getFilePresignedUrls: 'query GetFilePresignedUrls' }));

// Stable identity on purpose: the viewer's image-load effect is keyed on this.
const VERSION_CONTEXT = { isHistorical: false, runId: null, versionIdForUri: () => undefined };
vi.mock('../../../contexts/document-version', () => ({ useDocumentVersion: () => VERSION_CONTEXT }));

import PageImageViewer from '../PageImageViewer';
import { PRESIGN_REFRESH_AFTER_MS } from '../../../api/presign-batch';
import { FILE_ACCESS_DENIED_MESSAGE } from '../../../hooks/utils/graphql-error';

interface PresignCall {
  query: string;
  variables: { files: Array<{ s3Uri: string; versionId?: string }> };
}

const filesOf = (call: unknown): Array<{ s3Uri: string; versionId?: string }> => (call as PresignCall).variables.files;

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
});

const S3_PAGES = ['1', '2', '3'].map((Id) => ({ Id, ImageUri: `s3://out/doc/pages/${Id}/image.jpg` }));

describe('PageImageViewer presigning', () => {
  it('signs every s3:// page in a single batched call', async () => {
    render(<PageImageViewer pageIds={S3_PAGES.map((p) => p.Id)} documentPages={S3_PAGES} />);

    await waitFor(() => expect(screen.getByAltText('Page 1')).toBeDefined());
    expect(mockGraphql).toHaveBeenCalledTimes(1);
    expect(filesOf(mockGraphql.mock.calls[0][0]).map((f) => f.s3Uri)).toEqual(S3_PAGES.map((p) => p.ImageUri));
    expect(screen.getByAltText('Page 1').getAttribute('src')).toBe('https://signed.example/out/doc/pages/1/image.jpg');
  });

  it('uses a non-s3 ImageUri as-is and does not send it to the resolver', async () => {
    const pages = [
      { Id: '1', ImageUri: 'blob:http://localhost/page-one' },
      { Id: '2', ImageUri: 'data:image/png;base64,AAAA' },
      { Id: '3', ImageUri: 'https://cdn.example.test/page-three.png' },
    ];

    render(<PageImageViewer pageIds={pages.map((p) => p.Id)} documentPages={pages} />);

    await waitFor(() => expect(screen.getByAltText('Page 1')).toBeDefined());
    expect(screen.getByAltText('Page 1').getAttribute('src')).toBe('blob:http://localhost/page-one');
    expect(mockGraphql).not.toHaveBeenCalled();
  });

  it('signs only the s3:// pages of a mixed list, in one call', async () => {
    const pages = [
      { Id: '1', ImageUri: 'blob:http://localhost/page-one' },
      { Id: '2', ImageUri: 's3://out/doc/pages/2/image.jpg' },
    ];

    render(<PageImageViewer pageIds={['1', '2']} documentPages={pages} />);

    await waitFor(() => expect(screen.getByAltText('Page 1')).toBeDefined());
    expect(mockGraphql).toHaveBeenCalledTimes(1);
    expect(filesOf(mockGraphql.mock.calls[0][0])).toEqual([{ s3Uri: 's3://out/doc/pages/2/image.jpg' }]);
  });

  it('says the page images could not be loaded, rather than spinning, when a URL is missing', async () => {
    mockGraphql.mockImplementation(async (args: PresignCall) => ({
      data: {
        getFilePresignedUrls: {
          results: args.variables.files
            .filter((f) => !f.s3Uri.endsWith('/1/image.jpg'))
            .map((f) => ({ s3Uri: f.s3Uri, presignedUrl: 'https://signed.example/ok', contentType: 'image/jpeg' })),
        },
      },
    }));

    render(<PageImageViewer pageIds={S3_PAGES.map((p) => p.Id)} documentPages={S3_PAGES} />);

    await waitFor(() => expect(screen.getByText('The page images for this document could not be loaded.')).toBeDefined());
    expect(screen.queryByAltText('Page 1')).toBeNull();
    // No spinner anywhere: signing has finished, so "still loading" is a lie.
    expect(screen.queryByText('Loading image...')).toBeNull();
    expect(screen.queryByText('Loading page images...')).toBeNull();
  });

  it('shows the access-denied copy when the resolver refuses the document', async () => {
    // The whole batch is refused, so every page is missing at once — which is the
    // shape that produced a permanent spinner before this.
    mockGraphql.mockRejectedValue({ errors: [{ errorType: 'Unauthorized', message: 'Unauthorized' }] });

    render(<PageImageViewer pageIds={S3_PAGES.map((p) => p.Id)} documentPages={S3_PAGES} />);

    await waitFor(() => expect(screen.getByText(FILE_ACCESS_DENIED_MESSAGE)).toBeDefined());
    expect(screen.queryByText('Loading image...')).toBeNull();
  });

  it('says no image is available for a page that carries none, and calls it no failure', async () => {
    const pages = [{ Id: '1' }, { Id: '2', ImageUri: 's3://out/doc/pages/2/image.jpg' }];

    render(<PageImageViewer pageIds={['1', '2']} documentPages={pages} />);

    // Page 1 is current and has no ImageUri at all. That is not an error, so the
    // copy must not accuse the deployment of failing — and must not spin.
    await waitFor(() => expect(screen.getByText('No image is available for this page.')).toBeDefined());
    expect(screen.queryByText('The page images for this document could not be loaded.')).toBeNull();
  });
});

/**
 * Re-signing before the URLs expire.
 *
 * A presigned URL lives 300 s (`_PRESIGNED_URL_EXPIRY_SECONDS` in the resolver).
 * The viewer's signing effect depends on `[pageIds, documentPages, viewingRunId]`
 * and, for a **completed** document, none of those changes again — detail polling
 * has stopped — so nothing re-fires it. Open such a document, wait five minutes,
 * page forward, and the new page's `<img>` fetches an expired URL: S3 answers 403
 * and the fallback shows, which reads as the read path being flaky.
 *
 * These two tests are a pair, and each exists to fail a different wrong fix. The
 * first fails if nothing re-signs; the second fails if *everything* re-signs, which
 * is the cost problem the staleness check exists to avoid. Only the clock differs
 * between them — same mount, same click — so what they measure is the predicate and
 * nothing else.
 *
 * Only `Date` is faked (`toFake: ['Date']`). Faking `setTimeout` as well would stall
 * `waitFor`, which schedules real timers, and the component's own rotation-fit
 * measurement uses them too.
 */
describe('PageImageViewer re-signs expiring page URLs', () => {
  const signWithPrefix = (prefix: string) => async (args: PresignCall) => ({
    data: {
      getFilePresignedUrls: {
        results: args.variables.files.map((f) => ({
          s3Uri: f.s3Uri,
          presignedUrl: `${prefix}${f.s3Uri.slice('s3://'.length)}`,
          contentType: 'image/jpeg',
        })),
      },
    },
  });

  const nextPage = () => screen.getByRole('button', { name: 'Next page' });

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-01-01T12:00:00Z'));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('signs again when the page turn comes after the refresh threshold', async () => {
    mockGraphql.mockImplementation(signWithPrefix('https://first.example/'));
    render(<PageImageViewer pageIds={S3_PAGES.map((p) => p.Id)} documentPages={S3_PAGES} />);

    await waitFor(() => expect(screen.getByAltText('Page 1')).toBeDefined());
    expect(mockGraphql).toHaveBeenCalledTimes(1);

    // The document now sits idle past the threshold. Nothing else happens: no new
    // props, no polling, no re-render.
    mockGraphql.mockImplementation(signWithPrefix('https://second.example/'));
    vi.setSystemTime(Date.now() + PRESIGN_REFRESH_AFTER_MS + 1_000);

    fireEvent.click(nextPage());

    await waitFor(() => expect(mockGraphql).toHaveBeenCalledTimes(2));
    // And the page on screen uses the URL the SECOND call minted — a re-sign that
    // did not reach the `<img>` would fix nothing.
    await waitFor(() => expect(screen.getByAltText('Page 2').getAttribute('src')).toBe('https://second.example/out/doc/pages/2/image.jpg'));
  });

  it('does not sign again when the page turn comes within the threshold', async () => {
    mockGraphql.mockImplementation(signWithPrefix('https://first.example/'));
    render(<PageImageViewer pageIds={S3_PAGES.map((p) => p.Id)} documentPages={S3_PAGES} />);

    await waitFor(() => expect(screen.getByAltText('Page 1')).toBeDefined());
    expect(mockGraphql).toHaveBeenCalledTimes(1);

    vi.setSystemTime(Date.now() + PRESIGN_REFRESH_AFTER_MS - 1_000);

    fireEvent.click(nextPage());

    // The navigation happened, and page 2 is showing the URL the first call minted.
    await waitFor(() => expect(screen.getByAltText('Page 2').getAttribute('src')).toBe('https://first.example/out/doc/pages/2/image.jpg'));
    expect(mockGraphql).toHaveBeenCalledTimes(1);
  });

  it('never signs again for a view whose pages need no signing', async () => {
    // A `blob:` page carries no capability, so there is nothing to expire and a
    // page turn must not produce a resolver call with nothing to ask for.
    const pages = [
      { Id: '1', ImageUri: 'blob:http://localhost/page-one' },
      { Id: '2', ImageUri: 'blob:http://localhost/page-two' },
    ];
    render(<PageImageViewer pageIds={['1', '2']} documentPages={pages} />);

    await waitFor(() => expect(screen.getByAltText('Page 1')).toBeDefined());
    vi.setSystemTime(Date.now() + PRESIGN_REFRESH_AFTER_MS * 10);

    fireEvent.click(nextPage());

    await waitFor(() => expect(screen.getByAltText('Page 2').getAttribute('src')).toBe('blob:http://localhost/page-two'));
    expect(mockGraphql).not.toHaveBeenCalled();
  });
});
