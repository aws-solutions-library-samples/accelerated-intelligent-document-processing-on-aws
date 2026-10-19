// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Tests for how export assets are signed.
 *
 * Every bucket is signed by the backend `getFilePresignedUrl` resolver, the Input
 * and Output buckets included. The browser's Cognito identity role granted
 * `s3:GetObject` on those two and on nothing else, so the exporter signed them
 * locally and routed the rest — notably the EvaluationBaselineBucket — through the
 * resolver. That role now grants no bucket at all (issue #1033), which makes a
 * client-signed URL a 403 at fetch time everywhere and leaves the resolver as the
 * only working route.
 *
 * These tests are written so that reinstating the client branch fails them: each
 * asserts both that the resolver was asked for the URI *and* that the URL handed
 * back is the resolver's, so a second signing route cannot satisfy them by
 * returning something plausible.
 */

import JSZip from 'jszip';
import { afterEach, describe, expect, it, vi, beforeEach } from 'vitest';

const { mockClientSign } = vi.hoisted(() => ({ mockClientSign: vi.fn() }));

vi.mock('../../../graphql/generated', () => ({
  getFilePresignedUrl: 'query GetFilePresignedUrl',
  getFilePresignedUrls: 'query GetFilePresignedUrls',
}));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: mockClientSign }) }));

import { exportDocuments, presignForExport, type ExportableDocument, type ExportSettings } from '../document-export';
import { FILE_ACCESS_DENIED_MESSAGE } from '../../../hooks/utils/graphql-error';

const SETTINGS: ExportSettings = {
  InputBucket: 'input-bkt',
  OutputBucket: 'output-bkt',
  EvaluationBaselineBucket: 'baseline-bkt',
};

const CREDENTIALS = { accessKeyId: 'AKIA', secretAccessKey: 'shh' };

const RESOLVER_URL = 'https://resolver-signed.example/';

/** The URL the batch field mints, distinct per URI so a fetch can be traced back. */
const batchUrl = (s3Uri: string): string => `https://batch-signed.example/${encodeURIComponent(s3Uri)}`;

interface GraphqlCall {
  query: string;
  variables?: { s3Uri?: string; files?: Array<{ s3Uri: string; versionId?: string }> };
}

/** Calls to the batch field, and to the single-object field, separately. */
const batchCalls = (): GraphqlCall[] => mockClientSign.mock.calls.map((c) => c[0] as GraphqlCall).filter((c) => c.query.endsWith('Urls'));
const singleCalls = (): GraphqlCall[] =>
  mockClientSign.mock.calls.map((c) => c[0] as GraphqlCall).filter((c) => c.query === 'query GetFilePresignedUrl');

/** Sign everything asked for, by either field. `omit` drops a URI from the batch answer. */
const serverSigns =
  (omit: string[] = []) =>
  async (args: GraphqlCall) => {
    if (args.query.endsWith('Urls')) {
      return {
        data: {
          getFilePresignedUrls: {
            results: (args.variables?.files ?? [])
              .filter((f) => !omit.includes(f.s3Uri))
              .map((f) => ({ s3Uri: f.s3Uri, presignedUrl: batchUrl(f.s3Uri), contentType: 'application/json' })),
          },
        },
      };
    }
    return { data: { getFilePresignedUrl: { presignedUrl: RESOLVER_URL } } };
  };

beforeEach(() => {
  mockClientSign.mockReset().mockImplementation(serverSigns());
});

describe('presignForExport', () => {
  it('signs OutputBucket and InputBucket objects through the resolver, not in the browser', async () => {
    await expect(presignForExport('s3://output-bkt/a/b.json', CREDENTIALS, SETTINGS)).resolves.toBe(RESOLVER_URL);
    await expect(presignForExport('s3://input-bkt/a.pdf', CREDENTIALS, SETTINGS)).resolves.toBe(RESOLVER_URL);
    expect(mockClientSign.mock.calls.map((c) => c[0].variables.s3Uri)).toEqual(['s3://output-bkt/a/b.json', 's3://input-bkt/a.pdf']);
  });

  it('signs baseline-bucket objects through the backend resolver', async () => {
    await expect(presignForExport('s3://baseline-bkt/a/b.json', CREDENTIALS, SETTINGS)).resolves.toBe(RESOLVER_URL);
    expect(mockClientSign).toHaveBeenCalledWith({
      query: 'query GetFilePresignedUrl',
      variables: { s3Uri: 's3://baseline-bkt/a/b.json' },
    });
  });

  it('routes unknown buckets and unloaded settings through the resolver rather than guessing', async () => {
    await expect(presignForExport('s3://surprise/a.json', CREDENTIALS, SETTINGS)).resolves.toBe(RESOLVER_URL);
    await expect(presignForExport('s3://output-bkt/a.json', CREDENTIALS, null)).resolves.toBe(RESOLVER_URL);
    expect(mockClientSign).toHaveBeenCalledTimes(2);
  });

  it('fails loudly when the resolver returns no URL', async () => {
    mockClientSign.mockResolvedValue({ data: { getFilePresignedUrl: null } });
    await expect(presignForExport('s3://baseline-bkt/a.json', CREDENTIALS, SETTINGS)).rejects.toThrow(/returned no URL/);
  });
});

const okFetch = () =>
  vi.fn(async (_url: string) => ({
    ok: true,
    status: 200,
    statusText: 'OK',
    arrayBuffer: async () => new ArrayBuffer(2),
  }));

/** One document with four assets, two of them in the baseline bucket. */
const docWithAssets = (key: string): ExportableDocument => ({
  objectKey: key,
  evaluationStatus: 'COMPLETED',
  sections: [
    { Id: 's1', OutputJSONUri: `s3://output-bkt/${key}/sections/s1/result.json` },
    { Id: 's2', OutputJSONUri: `s3://output-bkt/${key}/sections/s2/result.json` },
  ],
});

describe('exportDocuments signing', () => {
  /**
   * The round-trip count is the point. The single-object field also issues a
   * `head_object` per call, so a 27-page "Download All (ZIP)" was ~115 authorised
   * round trips where the retired client-signed path made none. One call per
   * document replaces all of them.
   */
  it('pre-signs a document\u2019s assets in one batch call and fetches from it', async () => {
    const fetchFn = okFetch();

    const result = await exportDocuments([docWithAssets('doc.pdf')], SETTINGS, {
      scope: 'all',
      credentials: CREDENTIALS,
      fetchFn,
    });

    expect(result.errors).toEqual([]);
    expect(batchCalls()).toHaveLength(1);
    // Predictions (OutputBucket) and baselines alike, in the one call.
    expect((batchCalls()[0].variables?.files ?? []).map((f) => f.s3Uri).sort()).toEqual([
      's3://baseline-bkt/doc.pdf/sections/s1/result.json',
      's3://baseline-bkt/doc.pdf/sections/s2/result.json',
      's3://output-bkt/doc.pdf/sections/s1/result.json',
      's3://output-bkt/doc.pdf/sections/s2/result.json',
    ]);
    // Nothing fell back to the single form, and every fetch used a batch URL.
    expect(singleCalls()).toHaveLength(0);
    expect(fetchFn.mock.calls.map((c) => c[0]).every((url) => url.startsWith('https://batch-signed.example/'))).toBe(true);
    expect(fetchFn).toHaveBeenCalledTimes(4);
  });

  it('batches per document, not across the selection', async () => {
    // ⚠️ The resolver bounds the tracking-table round trips one call may spend
    // (`document_scope.MAX_DOCUMENT_LOOKUP_BATCHES`) and refuses past it, so one
    // call for a whole bulk selection would fail on a full page of results.
    const result = await exportDocuments([docWithAssets('a.pdf'), docWithAssets('b.pdf'), docWithAssets('c.pdf')], SETTINGS, {
      scope: 'predictions',
      credentials: CREDENTIALS,
      fetchFn: okFetch(),
    });

    expect(result.errors).toEqual([]);
    expect(batchCalls()).toHaveLength(3);
    for (const call of batchCalls()) {
      const docKeys = new Set((call.variables?.files ?? []).map((f) => f.s3Uri.split('/')[3]));
      expect(docKeys.size).toBe(1);
    }
  });

  it('falls back to the single form for a URI the batch did not return', async () => {
    const missing = 's3://output-bkt/doc.pdf/sections/s2/result.json';
    mockClientSign.mockImplementation(serverSigns([missing]));
    const fetchFn = okFetch();

    const result = await exportDocuments([docWithAssets('doc.pdf')], SETTINGS, {
      scope: 'predictions',
      credentials: CREDENTIALS,
      fetchFn,
    });

    expect(result.errors).toEqual([]);
    // Exactly one fallback, for exactly the URI the batch omitted — the call
    // itself succeeded, so the single form can still give a precise answer.
    expect(singleCalls().map((c) => c.variables?.s3Uri)).toEqual([missing]);
    expect(fetchFn.mock.calls.map((c) => c[0])).toContain(RESOLVER_URL);
  });

  it('does not retry each asset singly when the batch call itself was refused', async () => {
    // A refusal applies to every object in the batch, so re-asking per asset would
    // spend exactly the round trips this batching removes and get the same answer.
    mockClientSign.mockRejectedValue({ errors: [{ errorType: 'Unauthorized', message: 'Unauthorized' }] });
    const fetchFn = okFetch();

    const result = await exportDocuments([docWithAssets('doc.pdf')], SETTINGS, {
      scope: 'predictions',
      credentials: CREDENTIALS,
      fetchFn,
    });

    expect(batchCalls()).toHaveLength(1);
    expect(singleCalls()).toHaveLength(0);
    expect(fetchFn).not.toHaveBeenCalled();
    // The archive still says what is missing and why, per asset.
    expect(result.errors).toHaveLength(2);
    expect(result.errors.every((e) => e.message === FILE_ACCESS_DENIED_MESSAGE)).toBe(true);
    expect(result.errors.map((e) => e.uri).sort()).toEqual([
      's3://output-bkt/doc.pdf/sections/s1/result.json',
      's3://output-bkt/doc.pdf/sections/s2/result.json',
    ]);
  });

  it('leaves an injected presignFn as the only signing route', async () => {
    // The override IS the signing seam; pre-signing behind it would sign through a
    // route the caller did not ask for.
    const presignFn = vi.fn(async (uri: string) => `https://injected.example/${encodeURIComponent(uri)}`);

    await exportDocuments([docWithAssets('doc.pdf')], SETTINGS, {
      scope: 'predictions',
      credentials: CREDENTIALS,
      presignFn,
      fetchFn: okFetch(),
    });

    expect(mockClientSign).not.toHaveBeenCalled();
    expect(presignFn).toHaveBeenCalledTimes(2);
  });
});

/**
 * Re-signing asset URLs that expire part-way through an export.
 *
 * The pre-sign pass mints every URL for every selected document and finishes before
 * the first byte is fetched, so the last asset of a bulk export is fetched however
 * long the whole run takes — and a presigned URL lives 300 s. Before the batch
 * pre-sign existed each URL was minted inside its own fetch task, immediately before
 * its own fetch, so there was no window at all; batching opened one with no upper
 * bound, since neither the asset count nor the byte count is bounded. A page of
 * results is tens of documents, a 27-page document is ~115 assets and source PDFs are
 * multi-megabyte.
 *
 * The failure it produces is the expensive kind to diagnose: the archive completes,
 * its `manifest.json` carries `HTTP 403 Forbidden` for the later objects, and nothing
 * anywhere says the URL had expired.
 *
 * The fake server below mints URLs that record **when** they were signed, and the
 * fake fetch refuses one older than the server's 300 s — evaluated at the moment of
 * the request, as S3 does. So these tests are distinguished from a broken
 * implementation by elapsed time and nothing else: same documents, same assets, same
 * concurrency.
 */
describe('exportDocuments re-signs expiring asset URLs', () => {
  /** The resolver's `_PRESIGNED_URL_EXPIRY_SECONDS`, enforced by `expiringFetch`. */
  const SERVER_EXPIRY_MS = 300_000;

  /** A document whose only assets are `count` section results, all in one bucket. */
  const docWithSections = (key: string, count: number): ExportableDocument => ({
    objectKey: key,
    sections: Array.from({ length: count }, (_, i) => ({
      Id: `s${i + 1}`,
      OutputJSONUri: `s3://output-bkt/${key}/sections/s${i + 1}/result.json`,
    })),
  });

  /** A signed URL that carries its own minting time, so a fetch can expire it. */
  const timedUrl = (s3Uri: string, mintedAt: number): string => `https://batch-signed.example/${encodeURIComponent(s3Uri)}#${mintedAt}`;

  /**
   * A signing server. `advanceAfterFirstCall` moves the clock on once the pre-sign
   * pass has been answered, which is how a test puts the whole fetch pass past the
   * threshold; `refuseAfterFirstCall` makes every *re*-sign a 403.
   */
  const signingServer = (opts: { advanceAfterFirstCall?: number; refuseAfterFirstCall?: boolean } = {}) => {
    let batchCallCount = 0;
    return async (args: GraphqlCall) => {
      if (!args.query.endsWith('Urls')) return { data: { getFilePresignedUrl: { presignedUrl: RESOLVER_URL } } };
      batchCallCount += 1;
      if (batchCallCount > 1 && opts.refuseAfterFirstCall) {
        throw { errors: [{ errorType: 'Unauthorized', message: 'Unauthorized' }] };
      }
      const mintedAt = Date.now();
      const response = {
        data: {
          getFilePresignedUrls: {
            results: (args.variables?.files ?? []).map((f) => ({
              s3Uri: f.s3Uri,
              presignedUrl: timedUrl(f.s3Uri, mintedAt),
              contentType: 'application/json',
            })),
          },
        },
      };
      if (batchCallCount === 1 && opts.advanceAfterFirstCall) vi.setSystemTime(Date.now() + opts.advanceAfterFirstCall);
      return response;
    };
  };

  /**
   * Fetch that refuses an expired URL with the 403 S3 would give, then advances the
   * clock by `advanceMsPerFetch` to model the download taking time. Expiry is judged
   * before the clock moves, since that is when the request is made.
   */
  const expiringFetch = (advanceMsPerFetch = 0) =>
    vi.fn(async (url: string) => {
      const mintedAt = Number(url.split('#')[1]);
      const expired = Number.isFinite(mintedAt) && Date.now() - mintedAt > SERVER_EXPIRY_MS;
      if (advanceMsPerFetch) vi.setSystemTime(Date.now() + advanceMsPerFetch);
      if (expired) return { ok: false, status: 403, statusText: 'Forbidden', arrayBuffer: async () => new ArrayBuffer(0) };
      return { ok: true, status: 200, statusText: 'OK', arrayBuffer: async () => new ArrayBuffer(2) };
    });

  /** Captures the archive so the assets actually written to it can be counted. */
  const capturingZipFactory = () => {
    const captured: { zip?: JSZip } = {};
    return {
      captured,
      factory: () => {
        captured.zip = new JSZip();
        return captured.zip;
      },
    };
  };

  const assetsInArchive = (zip?: JSZip): string[] => Object.keys(zip?.files ?? {}).filter((name) => name.endsWith('/result.json'));

  /** Distinct minting times across every fetched URL — one per signing call used. */
  const mintTimesFetched = (fetchFn: { mock: { calls: unknown[][] } }): number[] => [
    ...new Set(fetchFn.mock.calls.map((c) => Number(String(c[0]).split('#')[1])).filter((n) => Number.isFinite(n))),
  ];

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-01-01T12:00:00Z'));
    mockClientSign.mockReset().mockImplementation(signingServer());
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('re-signs mid-export when the fetch pass straddles the refresh threshold', async () => {
    // Four assets fetched serially, each download taking 160 s: the requests land at
    // 0 s, 160 s, 320 s and 480 s after signing. The third is the first past the
    // 240 s threshold, so one re-sign happens there and nothing is ever fetched with
    // a URL older than the server's 300 s. Without the check, the third and fourth
    // requests carry a dead URL.
    const fetchFn = expiringFetch(160_000);
    const { captured, factory } = capturingZipFactory();

    const result = await exportDocuments([docWithSections('doc.pdf', 4)], SETTINGS, {
      scope: 'predictions',
      credentials: CREDENTIALS,
      fetchFn,
      concurrency: 1,
      zipFactory: factory,
    });

    // The user-visible property first, so a regression reads as the damage rather
    // than as a call count: every asset is in the archive and the manifest records
    // nothing against any of them.
    expect(result.errors).toEqual([]);
    expect(assetsInArchive(captured.zip)).toHaveLength(4);
    expect(fetchFn).toHaveBeenCalledTimes(4);
    // Then the mechanism: one re-sign, and the late assets fetched with the SECOND
    // call's URLs rather than the first's.
    expect(batchCalls()).toHaveLength(2);
    expect(mintTimesFetched(fetchFn)).toHaveLength(2);
    expect(singleCalls()).toHaveLength(0);
  });

  it('issues exactly one signing call for an export that finishes inside the threshold', async () => {
    // The companion. A fix that re-signs on every asset rather than only on a stale
    // map passes the test above and reintroduces the per-asset signing loop that the
    // batch pre-sign exists to remove, so this is the half that refuses it.
    const fetchFn = expiringFetch(10_000);

    const result = await exportDocuments([docWithSections('doc.pdf', 8)], SETTINGS, {
      scope: 'predictions',
      credentials: CREDENTIALS,
      fetchFn,
      concurrency: 1,
    });

    expect(result.errors).toEqual([]);
    expect(fetchFn).toHaveBeenCalledTimes(8);
    expect(batchCalls()).toHaveLength(1);
    expect(mintTimesFetched(fetchFn)).toHaveLength(1);
  });

  it('collapses the concurrent re-signs of one document into a single call', async () => {
    // The clock jumps past the threshold between the pre-sign pass and the first
    // fetch, so EVERY task starts out holding a stale map. With eight assets in a
    // pool of four, a check-and-re-sign per task would issue four extra calls; one
    // shared in-flight re-sign issues one.
    mockClientSign.mockImplementation(signingServer({ advanceAfterFirstCall: 250_000 }));
    const fetchFn = expiringFetch();

    const result = await exportDocuments([docWithSections('doc.pdf', 8)], SETTINGS, {
      scope: 'predictions',
      credentials: CREDENTIALS,
      fetchFn,
      concurrency: 4,
    });

    expect(result.errors).toEqual([]);
    expect(batchCalls()).toHaveLength(2);
    expect(fetchFn).toHaveBeenCalledTimes(8);
    expect(mintTimesFetched(fetchFn)).toHaveLength(1);
  });

  it('re-signs each document separately, never across the selection', async () => {
    // Same bound as the pre-sign pass: the resolver refuses a batch that spends
    // more than `document_scope.MAX_DOCUMENT_LOOKUP_BATCHES` tracking-table round
    // trips.
    mockClientSign.mockImplementation(signingServer({ advanceAfterFirstCall: 250_000 }));

    const result = await exportDocuments([docWithSections('a.pdf', 2), docWithSections('b.pdf', 2)], SETTINGS, {
      scope: 'predictions',
      credentials: CREDENTIALS,
      fetchFn: expiringFetch(),
      concurrency: 4,
    });

    expect(result.errors).toEqual([]);
    for (const call of batchCalls()) {
      const docKeys = new Set((call.variables?.files ?? []).map((f) => f.s3Uri.split('/')[3]));
      expect(docKeys.size).toBe(1);
    }
  });

  it('reports a refused re-sign per asset instead of throwing the export away', async () => {
    // A re-sign can be refused — the caller's document scope may have changed, or
    // the stack may be mid-deploy. That must land in the manifest as the
    // access-denied copy for each remaining asset, and the archive must still be
    // produced.
    mockClientSign.mockImplementation(signingServer({ advanceAfterFirstCall: 250_000, refuseAfterFirstCall: true }));

    const result = await exportDocuments([docWithSections('doc.pdf', 3)], SETTINGS, {
      scope: 'predictions',
      credentials: CREDENTIALS,
      fetchFn: expiringFetch(),
      concurrency: 1,
    });

    expect(result.blob).toBeDefined();
    expect(result.errors).toHaveLength(3);
    expect(result.errors.every((e) => e.message === FILE_ACCESS_DENIED_MESSAGE)).toBe(true);
    // One refused attempt, not one per asset: a refused result is not "stale", so
    // the remaining tasks do not each retry it.
    expect(batchCalls()).toHaveLength(2);
    // And no per-asset fallback to the single form, which a refusal never justifies.
    expect(singleCalls()).toHaveLength(0);
  });

  it('never re-signs behind an injected presignFn', async () => {
    // That override is the signing seam, and it mints per fetch, so it has no window
    // to close. Re-signing behind it would sign through a route the caller declined.
    const presignFn = vi.fn(async (uri: string) => `https://injected.example/${encodeURIComponent(uri)}`);

    await exportDocuments([docWithSections('doc.pdf', 4)], SETTINGS, {
      scope: 'predictions',
      credentials: CREDENTIALS,
      presignFn,
      fetchFn: expiringFetch(160_000),
      concurrency: 1,
    });

    expect(mockClientSign).not.toHaveBeenCalled();
    expect(presignFn).toHaveBeenCalledTimes(4);
  });
});
