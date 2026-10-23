// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Batched server-side presigning of S3 objects for display in the browser.
 *
 * Every document byte the UI shows is now signed by the `getFilePresignedUrls`
 * resolver rather than by the browser. The Cognito Identity Pool's authenticated
 * role no longer grants `s3:GetObject` on the Input or Output buckets (issue
 * #1033), so a client-signed URL for a document artefact 403s at fetch time; the
 * resolver is what puts the group floor, the bucket allow-list and the
 * per-document scope check in front of those bytes.
 *
 * Batching is what keeps the cost of that constant in page count rather than
 * linear: a document view used to issue one local signing per page, which cost
 * nothing, and one resolver round trip per page would not be acceptable in its
 * place.
 *
 * Three properties of the server contract this module is written against
 * (`nested/api-resolvers/src/lambda/get_file_contents_resolver/index.py`):
 *
 * - Results are **joined on the echoed `s3Uri`**, never on array index. The
 *   resolver returns entries in request order today, but the field echoes the URI
 *   precisely so a caller need not depend on that.
 * - There are **no per-entry errors**. Any authorization refusal fails the whole
 *   call with a 403, so a chunk either yields all of its URLs or none of them.
 *   That makes a refusal a whole-surface outcome, which is why the result reports
 *   `failed`/`accessDenied` rather than leaving the caller to infer a refusal from
 *   missing URLs — a page with no URL would otherwise be indistinguishable from a
 *   page nobody asked about, and the surfaces showed a permanent spinner for it.
 * - A `files` list longer than the server's cap is **refused**, not truncated.
 *
 * ⚠️ An object is identified by the **pair** `(s3Uri, versionId)`, not by the URI.
 * One key can have several object versions and they are different bytes, so both
 * the deduplication and the result lookup are keyed on the pair. Keying on the URI
 * alone collapsed two versions of one key to whichever was requested first and
 * served those bytes under both labels, with nothing anywhere reporting it — the
 * one failure mode here that produces a wrong answer rather than a worse
 * experience. It is also why a duplicated `s3Uri` is split across **calls** below
 * rather than sent twice in one: the echo is the join key, and two entries echoing
 * the same URI in one response cannot be told apart without falling back to index
 * order.
 */

import { ConsoleLogger } from 'aws-amplify/utils';

import { generateClient } from './client-shim';
import { getFilePresignedUrls } from '../graphql/generated';
import { isAuthorizationError } from '../hooks/utils/graphql-error';

const logger = new ConsoleLogger('presign-batch');

/**
 * Objects per `getFilePresignedUrls` call.
 *
 * The server cap is `MAX_BATCH_OBJECTS = 1000` in
 * `nested/api-resolvers/src/lambda/get_file_contents_resolver/index.py`, and this
 * value must stay at or below it — a longer list is refused with a 400 rather
 * than truncated, so exceeding it would blank a document view outright.
 * `scripts/tests/test_presign_batch_cap.py` reads this literal out of this file
 * and asserts `PRESIGN_BATCH_SIZE <= MAX_BATCH_OBJECTS`, which is why the
 * declaration below is spelled as one greppable line.
 */
export const PRESIGN_BATCH_SIZE = 250;

/**
 * How old a signed URL may get before a surface that is about to need a **new**
 * image re-signs the set.
 *
 * The server's lifetime is `_PRESIGNED_URL_EXPIRY_SECONDS = 300` in
 * `nested/api-resolvers/src/lambda/get_file_contents_resolver/index.py`, and this
 * value must stay **below** it. 240 s against 300 s leaves a 60 s margin, so a URL
 * this predicate has just called fresh cannot expire part-way through the fetch it
 * was checked for. `scripts/tests/test_presign_batch_cap.py` reads both literals
 * out of their own files and asserts the inequality, which is why — like
 * `PRESIGN_BATCH_SIZE` above — the declaration below is spelled as one greppable
 * line. (The digit separator is deliberate and harmless to that gate: Python's
 * `int()` accepts `"240_000"`.)
 *
 * Why a staleness check at the point of use, rather than a longer lifetime or a
 * timer. An expired URL only matters at the moment of a **new** fetch: bytes
 * already in the browser are not re-fetched, so a document view sitting idle
 * breaks nothing however long it sits, and what breaks is the next image requested
 * after expiry — page navigation, in practice. Checking here therefore covers
 * every case where an expired URL would actually be used, including a tab left
 * open for an hour, because the navigation after that hour is what triggers it.
 * The alternatives were each worse: raising the server lifetime to the AWS SDK
 * default of 900 s only moves the cliff, and it lengthens the life of a bearer
 * capability for every consumer of the single-object field too — a security cost
 * paid for a UX bug; a `setTimeout`-driven refresh pays a batch call every few
 * minutes on every idle open document; and re-signing on the navigation *event*
 * with no staleness check pays one on every page turn, which is the same cost
 * problem moved.
 */
export const PRESIGN_REFRESH_AFTER_MS = 240_000;

/** One object to presign. Mirrors the schema's `FilePresignRequest`. */
export interface PresignRequest {
  s3Uri: string;
  /**
   * S3 object VersionId, pinning a past document version's artefact. Per entry
   * rather than per request: a run manifest records a VersionId per key, so one
   * document view signs a different one for each page.
   */
  versionId?: string;
}

/**
 * The outcome of one {@link presignBatch} run.
 *
 * `get` answers for the **pair** that was requested, so a caller asking for two
 * versions of one key gets each version's own URL. `failed` and `accessDenied`
 * describe the calls rather than any object: the server's refusal deliberately
 * says nothing about which object it was for.
 */
export interface PresignBatchResult {
  /** The URL signed for this `(s3Uri, versionId)` pair, or `undefined`. */
  get: (s3Uri: string, versionId?: string | null) => string | undefined;
  /** How many objects were signed. */
  readonly size: number;
  /** True when at least one call failed, so some requested URLs are missing. */
  readonly failed: boolean;
  /** True when at least one failure was an authorization refusal (HTTP 403). */
  readonly accessDenied: boolean;
  /**
   * `Date.now()` from just before the signing calls went out, or `undefined` for a
   * result that signed nothing (no `s3://` entry was asked for at all).
   *
   * Taken *before* the round trip rather than after on purpose: the server's expiry
   * runs from the moment it signed, which is somewhere inside the call, so the
   * earlier timestamp makes {@link presignedUrlsNeedRefresh} report a URL as stale
   * slightly early rather than slightly late.
   */
  readonly signedAt?: number;
}

/**
 * True when a surface about to display a **newly needed** image should re-sign
 * first, because these URLs are older than {@link PRESIGN_REFRESH_AFTER_MS}.
 *
 * Pure, and the clock is a parameter: that is what lets a test tell the fix from
 * its absence by advancing time and changing nothing else.
 *
 * Two shapes answer **false** rather than true, and both matter:
 *
 * - A result that signed **nothing** — `signedAt` is `undefined`, which is every
 *   view whose pages carry no `s3://` image (Test Studio hands in `blob:` URIs).
 *   Reporting those as stale would make each such view issue a batch call that has
 *   nothing to ask for.
 * - A result whose every call **failed**, so `size` is 0. Re-signing there would
 *   turn each page turn on a document the caller may not read into another refused
 *   round trip, and retrying a refusal is a different decision from refreshing an
 *   expiring URL. The surface already reports the refusal.
 *
 * The boundary is inclusive: at exactly `PRESIGN_REFRESH_AFTER_MS` the URLs are
 * re-signed, so the 60 s margin below the server's expiry is the whole margin.
 */
export const presignedUrlsNeedRefresh = (
  result: Pick<PresignBatchResult, 'size' | 'signedAt'> | null | undefined,
  now: number = Date.now(),
): boolean => {
  if (!result || result.signedAt === undefined || result.size === 0) return false;
  return now - result.signedAt >= PRESIGN_REFRESH_AFTER_MS;
};

/**
 * The literal string `'null'` is what S3 reports as the VersionId of an object
 * written before versioning was enabled, and the retired client-side presigner
 * treated it as "no version". Normalising here rather than at the call sites is
 * what keeps a lookup able to find what a request stored.
 */
const normaliseVersionId = (versionId?: string | null): string | undefined => (versionId && versionId !== 'null' ? versionId : undefined);

/** Map key for an object identity. `\u0000` cannot occur in an S3 key or a VersionId. */
const pairKey = (s3Uri: string, versionId?: string | null): string => `${s3Uri}\u0000${normaliseVersionId(versionId) ?? ''}`;

// No `signedAt`: nothing was signed, so there is no minting time and
// `presignedUrlsNeedRefresh` must answer false for it.
const emptyResult = (failed = false, accessDenied = false): PresignBatchResult => ({
  get: () => undefined,
  size: 0,
  failed,
  accessDenied,
});

/**
 * Sign every given `s3://` URI through the batch resolver.
 *
 * The result holds **only the pairs that were successfully signed**, so a caller
 * reads a missing pair as "this one failed" for something it asked about and as
 * "not attempted" for something it did not — which is what lets
 * `use-page-thumbnails` map an unsignable page to `null` while leaving a page with
 * no `ImageUri` absent. `failed` is what tells the two apart at surface level, and
 * `accessDenied` is what lets a surface say *why* instead of showing a spinner.
 *
 * A chunk whose call fails contributes no URLs and is recorded on the result,
 * rather than rejecting: one refused or failed chunk should not throw away the
 * rest of a document view.
 *
 * Entries whose `s3Uri` is empty or not an `s3://` URI are dropped before the
 * call: a `blob:`/`data:`/`https:` source is already fetchable and the resolver
 * would only reject it. Repeated pairs are deduplicated. A URI repeated with a
 * *different* `versionId` is a different object, so it is sent — in a separate
 * call, because one response cannot echo the same URI twice unambiguously.
 */
export const presignBatch = async (requests: readonly PresignRequest[] | undefined | null): Promise<PresignBatchResult> => {
  if (!requests || requests.length === 0) return emptyResult();

  // Lanes, not one list: every lane holds each `s3Uri` at most once, so the
  // echoed URI identifies exactly one entry within a lane's call. The second
  // version of a key goes into lane 1, the third into lane 2, and so on — which
  // for every caller that signs one run's artefacts is a single lane.
  const seen = new Set<string>();
  const laneByUri = new Map<string, number>();
  const lanes: PresignRequest[][] = [];
  for (const request of requests) {
    const s3Uri = request?.s3Uri;
    if (!s3Uri || !s3Uri.startsWith('s3://')) continue;
    const versionId = normaliseVersionId(request.versionId);
    const key = pairKey(s3Uri, versionId);
    if (seen.has(key)) continue;
    seen.add(key);
    const lane = laneByUri.get(s3Uri) ?? 0;
    laneByUri.set(s3Uri, lane + 1);
    if (!lanes[lane]) lanes[lane] = [];
    lanes[lane].push(versionId ? { s3Uri, versionId } : { s3Uri });
  }
  if (lanes.length === 0) return emptyResult();

  const chunks: PresignRequest[][] = [];
  for (const lane of lanes) {
    for (let start = 0; start < lane.length; start += PRESIGN_BATCH_SIZE) {
      chunks.push(lane.slice(start, start + PRESIGN_BATCH_SIZE));
    }
  }

  const client = generateClient();
  const urls = new Map<string, string>();
  let failed = false;
  let accessDenied = false;

  // Before the round trip, not after — see `signedAt` on the result type.
  const signedAt = Date.now();

  await Promise.all(
    chunks.map(async (chunk) => {
      try {
        const response = await client.graphql({ query: getFilePresignedUrls, variables: { files: chunk } });
        const results = response.data?.getFilePresignedUrls?.results ?? [];
        // The versionId this call asked for, per URI. Unique within the chunk by
        // construction, which is what makes the echo a sufficient join key.
        const requestedVersion = new Map(chunk.map((file) => [file.s3Uri, file.versionId]));
        for (const entry of results) {
          if (!entry?.s3Uri || !entry?.presignedUrl) continue;
          // An echo for something this call did not ask about is ignored rather
          // than stored under a pair nobody requested.
          if (!requestedVersion.has(entry.s3Uri)) continue;
          urls.set(pairKey(entry.s3Uri, requestedVersion.get(entry.s3Uri)), entry.presignedUrl);
        }
      } catch (err) {
        failed = true;
        if (isAuthorizationError(err)) accessDenied = true;
        logger.error(`Failed to presign a batch of ${chunk.length} object(s):`, err);
      }
    }),
  );

  return {
    get: (s3Uri, versionId) => urls.get(pairKey(s3Uri, versionId)),
    size: urls.size,
    failed,
    accessDenied,
    signedAt,
  };
};

export default presignBatch;
