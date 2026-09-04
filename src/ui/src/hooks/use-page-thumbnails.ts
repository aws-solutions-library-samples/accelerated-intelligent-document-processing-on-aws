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
 * Pins to the viewed run's object versions via `useDocumentVersion`, so a historical
 * view shows the pages as they were rather than as they are now. That behaviour came
 * with the original and is easy to lose by reimplementing.
 *
 * A page whose URL could not be signed maps to `null` rather than being absent, so a
 * caller can tell "failed" from "not attempted yet" and render a placeholder instead of
 * a broken image.
 */

import { useEffect, useState } from 'react';
import { ConsoleLogger } from 'aws-amplify/utils';

import generateS3PresignedUrl from '../components/common/generate-s3-presigned-url';
import { useDocumentVersion } from '../contexts/document-version';
import useAppContext from '../contexts/app';

const logger = new ConsoleLogger('use-page-thumbnails');

export interface ThumbnailPage {
  Id: number | string;
  ImageUri?: string;
}

export const usePageThumbnails = (pages?: ThumbnailPage[]): Record<string, string | null> => {
  const [thumbnailUrls, setThumbnailUrls] = useState<Record<string, string | null>>({});
  const { currentCredentials } = useAppContext();
  const { versionIdForUri, runId: viewingRunId } = useDocumentVersion();

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      if (!pages) return;
      const urls: Record<string, string | null> = {};
      await Promise.all(
        pages.map(async (page) => {
          if (!page.ImageUri) return;
          try {
            urls[String(page.Id)] = await generateS3PresignedUrl(page.ImageUri, currentCredentials as Record<string, unknown>, {
              versionId: versionIdForUri(page.ImageUri),
            });
          } catch (err) {
            logger.error('Error generating presigned URL for thumbnail:', err);
            urls[String(page.Id)] = null;
          }
        }),
      );
      // Guarded because a signing round trip can outlive the pages it was for; without
      // this, switching documents quickly can show the previous one's thumbnails.
      if (!cancelled) setThumbnailUrls(urls);
    };

    load();
    return () => {
      cancelled = true;
    };
  }, [pages, viewingRunId, currentCredentials, versionIdForUri]);

  return thumbnailUrls;
};

export default usePageThumbnails;
