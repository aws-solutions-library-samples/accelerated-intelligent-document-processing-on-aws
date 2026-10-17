// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Batched server-side presigning.
 *
 * Four properties here are the ones a reimplementation loses, and each is a
 * silent failure rather than a visible one:
 *
 * - **Chunking at `PRESIGN_BATCH_SIZE`.** The server refuses a list longer than
 *   `MAX_BATCH_OBJECTS` with a 400 instead of truncating it, so a document whose
 *   page count crosses the cap would show no pages at all, not some of them.
 * - **Joining on the echoed `s3Uri`.** The resolver returns entries in request
 *   order today, and the field echoes the URI precisely so callers need not rely
 *   on that. Joining by index looks correct against an in-order server and
 *   mislabels every page against one that reorders.
 * - **Deduplication on the (s3Uri, versionId) PAIR.** The same pair twice in one
 *   call wastes a signing and makes the request longer than the page count, which
 *   is what the cap is measured against. But two *versions* of one key are
 *   different bytes, so collapsing them to the first — which keying on the URI
 *   alone does — serves one version's bytes under both labels with nothing
 *   reporting it. That is the only failure here that produces a wrong answer
 *   rather than a worse experience.
 * - **A failed call is reported, not just logged.** An authorization refusal fails
 *   the whole batch by server design, so a surface that only sees missing URLs
 *   cannot tell a refusal from a document with no images — and showed a spinner
 *   for ever.
 * - **A failed chunk omits only its own URLs.** One refused chunk must not throw
 *   away a whole document view, and `presignBatch` reports failure by absence —
 *   so a caller can still tell "asked and failed" from "never asked".
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';

const { mockGraphql } = vi.hoisted(() => ({ mockGraphql: vi.fn() }));

vi.mock('../client-shim', () => ({ generateClient: () => ({ graphql: mockGraphql }) }));
vi.mock('../../graphql/generated', () => ({ getFilePresignedUrls: 'query GetFilePresignedUrls' }));

import { PRESIGN_BATCH_SIZE, PRESIGN_REFRESH_AFTER_MS, presignBatch, presignedUrlsNeedRefresh } from '../presign-batch';

interface PresignCall {
  query: string;
  variables: { files: Array<{ s3Uri: string; versionId?: string }> };
}

/** Echo every requested URI back, in request order, as the real resolver does. */
/**
 * The URL the fake server mints. It varies with the requested `versionId`, which
 * is what makes "two versions of one key" distinguishable at all — a stub that
 * ignored `versionId` would pass the pair test with the URI-keyed implementation.
 */
const signedUrlFor = (file: { s3Uri: string; versionId?: string }): string =>
  `https://signed.example/${encodeURIComponent(file.s3Uri)}${file.versionId ? `#${file.versionId}` : ''}`;

const echoInOrder = async (args: PresignCall) => ({
  data: {
    getFilePresignedUrls: {
      results: args.variables.files.map((f) => ({
        s3Uri: f.s3Uri,
        presignedUrl: signedUrlFor(f),
        contentType: 'image/jpeg',
      })),
    },
  },
});

const filesOf = (call: unknown): Array<{ s3Uri: string; versionId?: string }> => (call as PresignCall).variables.files;

beforeEach(() => {
  mockGraphql.mockReset().mockImplementation(echoInOrder);
});

describe('presignBatch chunking', () => {
  it('splits at the batch boundary rather than sending one over-long list', async () => {
    const requests = Array.from({ length: PRESIGN_BATCH_SIZE + 1 }, (_, i) => ({ s3Uri: `s3://b/page-${i}.jpg` }));

    const urls = await presignBatch(requests);

    expect(mockGraphql).toHaveBeenCalledTimes(2);
    expect(filesOf(mockGraphql.mock.calls[0][0])).toHaveLength(PRESIGN_BATCH_SIZE);
    expect(filesOf(mockGraphql.mock.calls[1][0])).toHaveLength(1);
    expect(urls.size).toBe(PRESIGN_BATCH_SIZE + 1);
    // The boundary entries specifically: an off-by-one in the slice drops one.
    expect(urls.get(`s3://b/page-${PRESIGN_BATCH_SIZE - 1}.jpg`)).toBeDefined();
    expect(urls.get(`s3://b/page-${PRESIGN_BATCH_SIZE}.jpg`)).toBeDefined();
    expect(urls.failed).toBe(false);
  });

  it('sends exactly one call for a list that fits', async () => {
    await presignBatch(Array.from({ length: PRESIGN_BATCH_SIZE }, (_, i) => ({ s3Uri: `s3://b/${i}.jpg` })));
    expect(mockGraphql).toHaveBeenCalledTimes(1);
  });

  it('stays at or below the server cap', () => {
    // The authoritative check is scripts/tests/test_presign_batch_cap.py, which
    // reads MAX_BATCH_OBJECTS out of the resolver. This is the cheap local echo.
    expect(PRESIGN_BATCH_SIZE).toBeLessThanOrEqual(1000);
  });
});

describe('presignBatch request construction', () => {
  it('sends a repeated (s3Uri, versionId) pair once', async () => {
    const urls = await presignBatch([
      { s3Uri: 's3://b/a.jpg', versionId: 'v1' },
      { s3Uri: 's3://b/a.jpg', versionId: 'v1' },
      { s3Uri: 's3://b/b.jpg' },
      { s3Uri: 's3://b/b.jpg' },
    ]);

    expect(mockGraphql).toHaveBeenCalledTimes(1);
    expect(filesOf(mockGraphql.mock.calls[0][0])).toEqual([{ s3Uri: 's3://b/a.jpg', versionId: 'v1' }, { s3Uri: 's3://b/b.jpg' }]);
    expect(urls.size).toBe(2);
  });

  it('keeps two versions of one key apart instead of collapsing them to the first', async () => {
    // The wrong-answer case. Keyed on the URI alone, the second pair is dropped as
    // a duplicate and `get` hands v1's URL back for v2 — a side-by-side version
    // comparison would then show one version's bytes under both labels, with no
    // error anywhere. Keyed on the pair, both are signed and each resolves to its
    // own URL.
    const urls = await presignBatch([
      { s3Uri: 's3://b/a.jpg', versionId: 'v1' },
      { s3Uri: 's3://b/a.jpg', versionId: 'v2' },
    ]);

    // Asserted first on purpose: this is the one that reads as wrong DATA rather
    // than as a missing entry, so it should be the line a regression quotes.
    expect(urls.get('s3://b/a.jpg', 'v1')).not.toBe(urls.get('s3://b/a.jpg', 'v2'));
    expect(urls.get('s3://b/a.jpg', 'v1')).toBe(`https://signed.example/${encodeURIComponent('s3://b/a.jpg')}#v1`);
    expect(urls.get('s3://b/a.jpg', 'v2')).toBe(`https://signed.example/${encodeURIComponent('s3://b/a.jpg')}#v2`);
    expect(urls.size).toBe(2);
    // And the unversioned lookup is a third identity, not an alias for either.
    expect(urls.get('s3://b/a.jpg')).toBeUndefined();
  });

  it('splits a repeated URI across calls, because one response cannot echo it twice unambiguously', async () => {
    await presignBatch([{ s3Uri: 's3://b/a.jpg', versionId: 'v1' }, { s3Uri: 's3://b/a.jpg', versionId: 'v2' }, { s3Uri: 's3://b/b.jpg' }]);

    expect(mockGraphql).toHaveBeenCalledTimes(2);
    // Each call carries the URI at most once, so the echoed s3Uri identifies
    // exactly one requested entry within its own response.
    for (const call of mockGraphql.mock.calls) {
      const uris = filesOf(call[0]).map((f) => f.s3Uri);
      expect(new Set(uris).size).toBe(uris.length);
    }
  });

  it('chunks a lane that is longer than the cap, instead of chunking across lanes', async () => {
    // Where the two mechanisms meet, and neither of the tests above reaches it:
    // the chunking cases use distinct URIs (one lane) and the lane cases use three
    // entries (two lanes far below the cap), so a lane LONGER than the cap has
    // never been built. The hazard is specific — a 251-entry lane sent whole is a
    // 251-object call, which the server refuses with a 400 rather than truncating,
    // so the surface that asked would show nothing at all.
    const uris = Array.from({ length: PRESIGN_BATCH_SIZE + 1 }, (_, i) => `s3://b/page-${i}.jpg`);
    const requests = [...uris.map((s3Uri) => ({ s3Uri, versionId: 'v1' })), ...uris.map((s3Uri) => ({ s3Uri, versionId: 'v2' }))];

    const urls = await presignBatch(requests);

    // Two lanes, each chunked 250 + 1 — not one 502-entry list chunked 250/250/2,
    // and not two 251-entry calls.
    expect(mockGraphql.mock.calls.map((call) => filesOf(call[0]).length)).toEqual([PRESIGN_BATCH_SIZE, 1, PRESIGN_BATCH_SIZE, 1]);
    for (const call of mockGraphql.mock.calls) {
      const sent = filesOf(call[0]).map((f) => f.s3Uri);
      // No call may exceed the cap, and no call may carry a URI twice: the echo is
      // the join key, so a duplicate inside one response is ambiguous.
      expect(sent.length).toBeLessThanOrEqual(PRESIGN_BATCH_SIZE);
      expect(new Set(sent).size).toBe(sent.length);
    }

    // And every pair resolves to its own URL, including the two that land in the
    // one-entry tail chunks, which is where an off-by-one in the slice shows up.
    expect(urls.size).toBe(requests.length);
    for (const s3Uri of uris) {
      expect(urls.get(s3Uri, 'v1')).toBe(`https://signed.example/${encodeURIComponent(s3Uri)}#v1`);
      expect(urls.get(s3Uri, 'v2')).toBe(`https://signed.example/${encodeURIComponent(s3Uri)}#v2`);
    }
    expect(urls.failed).toBe(false);
  });

  it('keeps a URI out of its own call when a short second lane overlaps the first chunk', async () => {
    // Unequal lanes, which is a different arithmetic from the equal ones above: a
    // short second lane must not be absorbed into the first lane's last window.
    //
    // ⚠️ Per-call uniqueness is asserted here but cannot FAIL here, and the reason
    // is worth stating because it is a property rather than a coincidence. A chunk is
    // a slice of a single lane and a lane holds each `s3Uri` at most once, so no
    // chunk can carry a duplicate for ANY input at any scale. What the assertion
    // guards is a regression to slicing the FLATTENED lane list — and against that,
    // the two entries for one URI sit `laneZeroLength` apart while the window is
    // `PRESIGN_BATCH_SIZE` wide, so they co-occur only when lane 0 is shorter than
    // the cap, which is the three-entry test's shape and not this one. So no single
    // input exercises both the chunk arithmetic over a long lane and the
    // duplicate-echo ambiguity: here the call SIZES catch flat slicing, and
    // 'keeps two versions of one key apart' catches the ambiguity. Both tests are
    // needed; neither subsumes the other.
    const uris = Array.from({ length: PRESIGN_BATCH_SIZE + 1 }, (_, i) => `s3://b/page-${i}.jpg`);
    const requests = [
      ...uris.map((s3Uri) => ({ s3Uri, versionId: 'v1' })),
      ...uris.slice(0, 10).map((s3Uri) => ({ s3Uri, versionId: 'v2' })),
    ];

    const urls = await presignBatch(requests);

    // Lane 0 is 251 (chunked 250 + 1); lane 1 is 10, which is below the cap and
    // would be absorbed into lane 0's first window by flat slicing.
    expect(mockGraphql.mock.calls.map((call) => filesOf(call[0]).length)).toEqual([PRESIGN_BATCH_SIZE, 1, 10]);
    for (const call of mockGraphql.mock.calls) {
      const sent = filesOf(call[0]).map((f) => f.s3Uri);
      expect(new Set(sent).size).toBe(sent.length);
    }
    expect(urls.size).toBe(requests.length);
    for (const s3Uri of uris.slice(0, 10)) {
      expect(urls.get(s3Uri, 'v1')).toBe(`https://signed.example/${encodeURIComponent(s3Uri)}#v1`);
      expect(urls.get(s3Uri, 'v2')).toBe(`https://signed.example/${encodeURIComponent(s3Uri)}#v2`);
    }
  });

  it('does not send an empty or non-s3 URI', async () => {
    const urls = await presignBatch([
      { s3Uri: '' },
      { s3Uri: 'blob:http://localhost/abc' },
      { s3Uri: 'data:image/png;base64,AAA' },
      { s3Uri: 'https://example.test/x.jpg' },
      { s3Uri: 's3://b/real.jpg' },
    ]);

    expect(filesOf(mockGraphql.mock.calls[0][0])).toEqual([{ s3Uri: 's3://b/real.jpg' }]);
    expect(urls.get('blob:http://localhost/abc')).toBeUndefined();
  });

  it('issues no call at all when nothing is signable', async () => {
    for (const input of [[{ s3Uri: 'blob:x' }], [], undefined]) {
      const urls = await presignBatch(input);
      expect(urls.size).toBe(0);
      // Nothing was attempted, so nothing failed — a surface must not show an
      // error for a document that simply has no page images.
      expect(urls.failed).toBe(false);
      expect(urls.accessDenied).toBe(false);
    }
    expect(mockGraphql).not.toHaveBeenCalled();
  });

  it('omits a versionId of the literal string "null", which S3 reports for a pre-versioning object', async () => {
    await presignBatch([{ s3Uri: 's3://b/a.jpg', versionId: 'null' }]);
    expect(filesOf(mockGraphql.mock.calls[0][0])).toEqual([{ s3Uri: 's3://b/a.jpg' }]);
  });
});

describe('presignBatch result joining', () => {
  it('joins on the echoed s3Uri, not on array position', async () => {
    mockGraphql.mockImplementation(async (args: PresignCall) => {
      const results = args.variables.files.map((f) => ({
        s3Uri: f.s3Uri,
        presignedUrl: signedUrlFor(f),
        contentType: 'image/jpeg',
      }));
      // Deliberately NOT in request order. An index join pairs page 1's URL with
      // page 3's URI here, which renders the wrong page under the right label.
      return { data: { getFilePresignedUrls: { results: results.reverse() } } };
    });

    const urls = await presignBatch([{ s3Uri: 's3://b/1.jpg' }, { s3Uri: 's3://b/2.jpg' }, { s3Uri: 's3://b/3.jpg' }]);

    expect(urls.get('s3://b/1.jpg')).toBe(`https://signed.example/${encodeURIComponent('s3://b/1.jpg')}`);
    expect(urls.get('s3://b/2.jpg')).toBe(`https://signed.example/${encodeURIComponent('s3://b/2.jpg')}`);
    expect(urls.get('s3://b/3.jpg')).toBe(`https://signed.example/${encodeURIComponent('s3://b/3.jpg')}`);
  });

  it('omits a URI the server answered about with no URL', async () => {
    mockGraphql.mockImplementation(async (args: PresignCall) => ({
      data: {
        getFilePresignedUrls: {
          results: args.variables.files
            .filter((f) => f.s3Uri !== 's3://b/2.jpg')
            .map((f) => ({ s3Uri: f.s3Uri, presignedUrl: 'https://signed.example/x', contentType: 'image/jpeg' })),
        },
      },
    }));

    const urls = await presignBatch([{ s3Uri: 's3://b/1.jpg' }, { s3Uri: 's3://b/2.jpg' }]);

    expect(urls.get('s3://b/1.jpg')).toBeDefined();
    expect(urls.get('s3://b/2.jpg')).toBeUndefined();
    // The call itself succeeded, so this is NOT reported as a failure: the single
    // form can still be asked about that one URI and give a precise reason.
    expect(urls.failed).toBe(false);
  });
});

describe('presignBatch failure handling', () => {
  it('omits only the failed chunk and still resolves', async () => {
    const requests = Array.from({ length: PRESIGN_BATCH_SIZE + 2 }, (_, i) => ({ s3Uri: `s3://b/page-${i}.jpg` }));
    mockGraphql.mockImplementation(async (args: PresignCall) => {
      // The short tail chunk is the one that fails.
      if (args.variables.files.length < PRESIGN_BATCH_SIZE) throw new Error('403 Unauthorized');
      return echoInOrder(args);
    });

    const urls = await presignBatch(requests);

    expect(mockGraphql).toHaveBeenCalledTimes(2);
    expect(urls.size).toBe(PRESIGN_BATCH_SIZE);
    expect(urls.get('s3://b/page-0.jpg')).toBeDefined();
    expect(urls.get(`s3://b/page-${PRESIGN_BATCH_SIZE}.jpg`)).toBeUndefined();
    expect(urls.get(`s3://b/page-${PRESIGN_BATCH_SIZE + 1}.jpg`)).toBeUndefined();
    expect(urls.failed).toBe(true);
  });

  it('resolves rather than rejecting when every chunk fails, and says so', async () => {
    mockGraphql.mockRejectedValue(new Error('network down'));
    const urls = await presignBatch([{ s3Uri: 's3://b/a.jpg' }]);
    expect(urls.size).toBe(0);
    expect(urls.failed).toBe(true);
    expect(urls.accessDenied).toBe(false);
  });

  it('reports an authorization refusal as such, so a surface can say why', async () => {
    // The dispatcher's 403 envelope. Reported rather than logged, because the
    // server refuses the WHOLE batch: every page of a document outside the
    // caller's scope goes missing at once, and the surfaces used to show a
    // spinner for it indefinitely.
    mockGraphql.mockRejectedValue({ errors: [{ errorType: 'Unauthorized', message: 'Unauthorized' }] });
    const urls = await presignBatch([{ s3Uri: 's3://b/a.jpg' }]);
    expect(urls.failed).toBe(true);
    expect(urls.accessDenied).toBe(true);
  });
});

describe('presignedUrlsNeedRefresh', () => {
  /**
   * The predicate a surface consults before displaying a **newly needed** image.
   * Pure and clock-injected, which is what lets the viewer's own test tell the
   * fix from its absence by advancing time and changing nothing else.
   *
   * The threshold has to stay under the resolver's `_PRESIGNED_URL_EXPIRY_SECONDS`
   * (300 s); `scripts/tests/test_presign_batch_cap.py` compares the two literals
   * across the language boundary. Asserted here only as "shorter than the
   * server's lifetime with a margin", not restated as a number.
   */
  const signedAt = 1_700_000_000_000;
  const freshResult = { size: 3, signedAt };

  it('leaves a margin under the resolver lifetime it has to beat', () => {
    expect(PRESIGN_REFRESH_AFTER_MS).toBeGreaterThan(0);
    expect(PRESIGN_REFRESH_AFTER_MS).toBeLessThan(300_000);
  });

  it('says no just inside the threshold', () => {
    expect(presignedUrlsNeedRefresh(freshResult, signedAt)).toBe(false);
    expect(presignedUrlsNeedRefresh(freshResult, signedAt + PRESIGN_REFRESH_AFTER_MS - 1)).toBe(false);
  });

  it('says yes at the threshold and beyond', () => {
    // Inclusive on purpose: the margin below the server's expiry is the whole margin.
    expect(presignedUrlsNeedRefresh(freshResult, signedAt + PRESIGN_REFRESH_AFTER_MS)).toBe(true);
    expect(presignedUrlsNeedRefresh(freshResult, signedAt + PRESIGN_REFRESH_AFTER_MS + 60_000)).toBe(true);
  });

  it('says no for a result that signed nothing, however old', () => {
    // A view whose pages are all blob:/data:/https: — there is no capability to
    // expire, and reporting it stale would make every such view call the resolver
    // with nothing to ask for.
    expect(presignedUrlsNeedRefresh({ size: 0, signedAt: undefined }, signedAt + PRESIGN_REFRESH_AFTER_MS * 100)).toBe(false);
    // And a result that somehow holds URLs without a timestamp: unknowable age is
    // not a reason to spend a round trip.
    expect(presignedUrlsNeedRefresh({ size: 3, signedAt: undefined }, signedAt + PRESIGN_REFRESH_AFTER_MS * 100)).toBe(false);
    expect(presignedUrlsNeedRefresh(null, signedAt)).toBe(false);
    expect(presignedUrlsNeedRefresh(undefined, signedAt)).toBe(false);
  });

  it('says no for a result whose every call failed', () => {
    // Size 0 with a timestamp: refusal, not expiry. Re-signing would turn each
    // page turn on a refused document into another refused round trip, and the
    // surface already reports the refusal.
    expect(presignedUrlsNeedRefresh({ size: 0, signedAt }, signedAt + PRESIGN_REFRESH_AFTER_MS * 10)).toBe(false);
  });

  it('is what presignBatch stamps, for both shapes', async () => {
    // Not a restatement of the implementation: the predicate is only useful if the
    // real result objects carry a usable `signedAt`, and the empty one does not.
    mockGraphql.mockImplementation(echoInOrder);
    const signed = await presignBatch([{ s3Uri: 's3://b/a.jpg' }]);
    expect(typeof signed.signedAt).toBe('number');
    expect(presignedUrlsNeedRefresh(signed, (signed.signedAt as number) + PRESIGN_REFRESH_AFTER_MS)).toBe(true);
    expect(presignedUrlsNeedRefresh(signed, signed.signedAt as number)).toBe(false);

    const nothing = await presignBatch([{ s3Uri: 'blob:http://localhost/x' }]);
    expect(nothing.signedAt).toBeUndefined();
    expect(presignedUrlsNeedRefresh(nothing)).toBe(false);
  });
});
