// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The production adapter of the edit screen: loads a review queue item, its
// page images and section results, autosaves edits as drafts, and completes the
// item. Drafts are applied by the finalize run on Complete.
//
// Only the assignee's verbs are called here. Assigning, unassigning, moving and
// removing are Admin operations on the queue pages: an assignment does not expire.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ConsoleLogger } from 'aws-amplify/utils';

import { generateClient } from '../../api/client-shim';
import { getDocument as getDocumentQuery, getFilePresignedUrl } from '../../graphql/generated';
import type { ReviewItemDetail } from '../../graphql/generated/operation-types';
import useAppContext from '../../contexts/app';
import usePolling from '../../hooks/use-polling';
import { describeApiError } from '../../hooks/utils/graphql-error';
import { completeItem, fetchReviewItem, reviewErrorCode, saveDraft } from '../review-queues/review-api';
import type { LoadedSection, ReviewPage, ReviewSectionRef } from './types';

const client = generateClient();
const logger = new ConsoleLogger('ProductionItemEditAdapter');

export const REVIEW_ITEM_POLL_INTERVAL_MS = 20_000;
export const AUTOSAVE_MS = 2000;

/** Read a section result (or its draft) through a server-issued presigned URL. */
export const fetchSectionResult = async (s3Uri: string): Promise<LoadedSection> => {
  const response = await client.graphql({ query: getFilePresignedUrl, variables: { s3Uri } });
  const presignedUrl = response.data?.getFilePresignedUrl?.presignedUrl;
  if (!presignedUrl) throw new Error('No presigned URL returned by server');
  const s3Response = await fetch(presignedUrl);
  if (!s3Response.ok) throw new Error(`S3 fetch failed: ${s3Response.status}`);
  const text = await s3Response.text();
  return { data: JSON.parse(text) as Record<string, unknown>, raw: text };
};

export interface ProductionItemEditModel {
  detail: ReviewItemDetail | null;
  loadError: unknown;
  /** Re-read the item; resolves to the fresh detail, or null when the read failed. */
  reload: () => Promise<ReviewItemDetail | null>;
  pages: ReviewPage[];
  pagesLoading: boolean;
  /** The sections under review. */
  sections: ReviewSectionRef[];
  /** Every section of the document (the page-grouping board needs them all). */
  documentSections: ReviewSectionRef[];
  /** Re-read the document's pages and sections (after a regroup). */
  refreshDocument: () => Promise<void>;
  /** The document's summary report, once summarization has run (after the review). */
  summaryReportUri: string | null;
  /** The document's evaluation status (BASELINE_COPYING while a baseline copy runs). */
  evaluationStatus: string | null;
  /** Bumped when the open section must be re-read (a newer draft arrived). */
  reloadToken: number;
  loadSection: (sectionId: string) => Promise<LoadedSection>;
  saveSection: (sectionId: string, data: Record<string, unknown>) => Promise<void>;
  complete: (checklist?: boolean[]) => Promise<void>;
}

export const useProductionItemEdit = (objectKey: string): ProductionItemEditModel => {
  const { setErrorMessage } = useAppContext();
  const [detail, setDetail] = useState<ReviewItemDetail | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [pages, setPages] = useState<ReviewPage[]>([]);
  const [pagesLoading, setPagesLoading] = useState(true);
  const [documentSections, setDocumentSections] = useState<ReviewSectionRef[]>([]);
  const [summaryReportUri, setSummaryReportUri] = useState<string | null>(null);
  const [evaluationStatus, setEvaluationStatus] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  // The latest draft revision per section, so a save names the revision it edits.
  const revisionsRef = useRef<Record<string, number>>({});

  const reload = useCallback(async (): Promise<ReviewItemDetail | null> => {
    if (!objectKey) return null;
    try {
      const result = await fetchReviewItem(objectKey);
      setDetail(result);
      setLoadError(null);
      for (const s of result.sections) {
        const known = revisionsRef.current[s.sectionId] ?? 0;
        revisionsRef.current[s.sectionId] = Math.max(known, s.draftRevision ?? 0);
      }
      return result;
    } catch (err) {
      logger.error('getReviewItem failed', err);
      setLoadError(err);
      return null;
    }
  }, [objectKey]);

  useEffect(() => {
    setDetail(null);
    setLoadError(null);
    revisionsRef.current = {};
  }, [objectKey]);

  const poll = useCallback(async () => {
    await reload();
  }, [reload]);

  usePolling(poll, { enabled: !!objectKey, intervalMs: REVIEW_ITEM_POLL_INTERVAL_MS, immediate: true });

  // The latest document read wins: a refresh after a regroup supersedes the first load.
  const documentRead = useRef(0);
  const refreshDocument = useCallback(async (): Promise<void> => {
    const read = ++documentRead.current;
    try {
      const result = await client.graphql({ query: getDocumentQuery, variables: { objectKey } });
      if (read !== documentRead.current) return;
      const doc = result.data?.getDocument;
      setSummaryReportUri(doc?.SummaryReportUri || null);
      setEvaluationStatus(doc?.EvaluationStatus || null);
      setPages(
        (doc?.Pages ?? [])
          .filter((p): p is NonNullable<typeof p> => !!p && p.Id != null && !!p.ImageUri)
          .map((p) => ({ Id: String(p.Id), ImageUri: p.ImageUri as string })),
      );
      setDocumentSections(
        (doc?.Sections ?? [])
          .filter((s): s is NonNullable<typeof s> => !!s && s.Id != null)
          .map((s) => ({
            sectionId: String(s.Id),
            documentClass: s.Class ?? null,
            resultUri: s.OutputJSONUri ?? '',
            pageIds: (s.PageIds ?? []).map(String),
          })),
      );
    } catch (err) {
      logger.error('getDocument failed', err);
    } finally {
      if (read === documentRead.current) setPagesLoading(false);
    }
  }, [objectKey]);

  useEffect(() => {
    setPagesLoading(true);
    setPages([]);
    setDocumentSections([]);
    void refreshDocument();
    return () => {
      documentRead.current += 1;
    };
  }, [refreshDocument]);

  const sections: ReviewSectionRef[] = useMemo(
    () =>
      (detail?.sections ?? []).map((s) => ({
        sectionId: s.sectionId,
        documentClass: s.documentClass,
        resultUri: s.resultUri ?? '',
        draftUri: s.draftUri,
        pageIds: s.pageIds,
      })),
    [detail?.sections],
  );

  const loadSection = useCallback(
    async (sectionId: string): Promise<LoadedSection> => {
      const section = sections.find((s) => s.sectionId === sectionId);
      const s3Uri = section?.draftUri || section?.resultUri;
      if (!s3Uri) throw new Error('This section has no result to review');
      return fetchSectionResult(s3Uri);
    },
    [sections],
  );

  const item = detail?.item ?? null;

  const saveSection = useCallback(
    async (sectionId: string, data: Record<string, unknown>): Promise<void> => {
      if (!item) return;
      try {
        const draft = await saveDraft(item, sectionId, data, revisionsRef.current[sectionId] ?? 0);
        revisionsRef.current[sectionId] = draft.revision;
      } catch (err) {
        if (reviewErrorCode(err) === 'STALE_DRAFT') {
          setErrorMessage('Someone saved a newer draft of this section. It has been reloaded; reapply your last change.');
          await reload();
          setReloadToken((t) => t + 1);
          return;
        }
        // An assignment that moved to someone else shows up on the next read.
        void reload();
        throw new Error(describeApiError(err, 'save your draft'));
      }
    },
    [item, reload, setErrorMessage],
  );

  const complete = useCallback(
    async (checklist?: boolean[]): Promise<void> => {
      if (!item) return;
      await completeItem(item, checklist);
    },
    [item],
  );

  return {
    detail,
    loadError,
    reload,
    pages,
    pagesLoading,
    sections,
    documentSections,
    refreshDocument,
    summaryReportUri,
    evaluationStatus,
    reloadToken,
    loadSection,
    saveSection,
    complete,
  };
};
