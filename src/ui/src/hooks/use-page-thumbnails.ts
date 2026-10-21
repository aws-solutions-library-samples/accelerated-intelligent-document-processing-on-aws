// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Presigned thumbnail URLs for a document's pages, keyed by page id.
 *
 * A page's `ImageUri` is an `s3://` URI, so it cannot be used as an `<img src>`
 * directly — every surface showing page thumbnails has to sign them first. Extracted
 * from PagesPanel when the page-regrouping board became the second such surface, rather
 * than copying the loop.
 *
 * Signing is done by the `getFilePresignedUrls` resolver in **one call for the whole
 * page list** (see `api/presign-batch`). The browser signed these itself until the
 * Identity Pool's authenticated role stopped granting the document buckets (issue
 * #1033); batching is what keeps the replacement's cost constant in page count.
 *
 * Pins to the viewed run's object versions via `useDocumentVersion`, so a historical
 * view shows the pages as they were rather than as they are now. That behaviour came
 * with the original and is easy to lose by reimplementing.
 *
 * A page whose URL could not be signed maps to `null` rather than being absent, so a
 * caller can tell "failed" from "not attempted yet" and render a placeholder instead of
 * a broken image. A page with no `ImageUri` at all stays absent.
 *
 * `failure` carries the reason alongside the urls, and it exists because an
 * authorization refusal is now a routine outcome: the resolver's per-document scope
 * check fails the **whole** batch with a 403, so every page of a document a caller
 * may not read comes back without a URL at once. Reported as a reason, the surface
 * can say so; left as absent URLs, it is indistinguishable from a document with no
 * page images.
 *
 * `{ enabled: false }` signs nothing. Signing now costs a round trip per surface, so a
 * panel whose thumbnails sit behind a collapsed affordance should defer it rather than
 * sign a whole document's pages on mount and render none of them — which is what
 * SectionsPanel did.
 *
 * ⚠️ **No staleness re-sign here, deliberately — the asymmetry with `PageImageViewer`
 * is the point.** A signed URL lives 300 s (`_PRESIGNED_URL_EXPIRY_SECONDS` in the
 * resolver), and an expired one only matters at the moment a **new** image is fetched;
 * bytes already in the browser are not re-fetched. The viewer shows one page at a time,
 * so paging forward on a completed document — whose `pageIds` and run id never change
 * again, so nothing re-fires its effect — is exactly such a moment, and it carries a
 * refresh check at that point (`presignedUrlsNeedRefresh`). Thumbnails are different in
 * kind: every page's `<img>` is rendered at once from one map, so a thumbnail is newly
 * fetched only when the page list, the viewed run or `enabled` changes, and all three
 * are already in this effect's dependency list. Both consumers confirm that shape —
 * `PagesPanel` sorts but does not paginate its table (`useCollection` with sorting
 * only), and `SectionsPanel` maps the whole page array into its board — so there is no
 * path by which a thumbnail URL is used for the first time without a re-sign having
 * happened.
 *
 * The one residual, left unfixed on purpose: if the browser evicts a thumbnail from its
 * HTTP cache and the `<img>` is then remounted after 300 s, that refetch uses an expired
 * URL and shows a broken frame. Closing it would need a timer — a batch call every few
 * minutes per open panel, on a surface that may be idle or collapsed — which costs more
 * than the frame it saves. Nothing *else* here is time-dependent, so if a surface later
 * renders thumbnails lazily (pagination, virtualised rows, an on-demand reveal), that
 * surface needs the viewer's treatment and this comment is the notice.
 */

import { useEffect, useState } from 'react';
import { ConsoleLogger } from 'aws-amplify/utils';

import { presignBatch, type PresignRequest } from '../api/presign-batch';
import { useDocumentVersion } from '../contexts/document-version';
import { FILE_ACCESS_DENIED_MESSAGE } from './utils/graphql-error';

const logger = new ConsoleLogger('use-page-thumbnails');

export interface ThumbnailPage {
  Id: number | string;
  ImageUri?: string;
}

export interface PageThumbnailOptions {
  /** When false, no URL is signed and the returned map stays empty. Defaults to true. */
  enabled?: boolean;
}

/** Why no thumbnail could be signed. `null` when nothing failed. */
export type ThumbnailFailure = 'access-denied' | 'failed';

export interface PageThumbnails {
  /** Presigned URL per page id; `null` for a page whose URL could not be signed. */
  urls: Record<string, string | null>;
  /**
   * Set when a signing call failed, so a surface can say why rather than render
   * an empty frame. An authorization refusal fails the WHOLE batch by server
   * design, so this is a property of the surface, not of one page.
   */
  failure: ThumbnailFailure | null;
}

/**
 * What to show for each failure. One place, because both panels show it and a
 * second copy is how the two would drift.
 */
export const THUMBNAIL_FAILURE_MESSAGES: Record<ThumbnailFailure, string> = {
  'access-denied': FILE_ACCESS_DENIED_MESSAGE,
  failed: 'Page thumbnails could not be loaded.',
};

const NO_THUMBNAILS: PageThumbnails = { urls: {}, failure: null };

export const usePageThumbnails = (pages?: ThumbnailPage[], options: PageThumbnailOptions = {}): PageThumbnails => {
  const [thumbnails, setThumbnails] = useState<PageThumbnails>(NO_THUMBNAILS);
  const { versionIdForUri, runId: viewingRunId } = useDocumentVersion();
  const enabled = options.enabled !== false;

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      if (!enabled) {
        // Clears rather than keeping the last map: a surface that has been
        // disabled is not showing thumbnails, and holding stale ones would hand
        // it URLs signed for a run it may no longer be viewing. Returns the
        // previous value when it is already empty, so a caller whose
        // `versionIdForUri` identity is not stable does not get an effect →
        // setState → re-render loop out of a path that signs nothing.
        if (!cancelled) setThumbnails((prev) => (Object.keys(prev.urls).length === 0 && !prev.failure ? prev : NO_THUMBNAILS));
        return;
      }
      if (!pages) return;

      const requests: PresignRequest[] = [];
      for (const page of pages) {
        if (!page.ImageUri) continue;
        requests.push({ s3Uri: page.ImageUri, versionId: versionIdForUri(page.ImageUri) });
      }

      const signed = await presignBatch(requests);

      const urls: Record<string, string | null> = {};
      let missing = 0;
      for (const page of pages) {
        if (!page.ImageUri) continue;
        const url = signed.get(page.ImageUri, versionIdForUri(page.ImageUri));
        if (!url) missing += 1;
        urls[String(page.Id)] = url ?? null;
      }
      // A refusal is reported by the call, not inferred from missing URLs: the
      // server fails the whole batch, so "nothing came back" and "nothing was
      // asked for" would otherwise look the same to the surface.
      const failure: ThumbnailFailure | null = signed.accessDenied ? 'access-denied' : signed.failed || missing > 0 ? 'failed' : null;
      if (missing > 0) logger.error(`No presigned URL for ${missing} of ${requests.length} page thumbnail(s)`);

      // Guarded because a signing round trip can outlive the pages it was for; without
      // this, switching documents quickly can show the previous one's thumbnails.
      if (!cancelled) setThumbnails({ urls, failure });
    };

    load();
    return () => {
      cancelled = true;
    };
  }, [pages, viewingRunId, versionIdForUri, enabled]);

  return thumbnails;
};

export default usePageThumbnails;
