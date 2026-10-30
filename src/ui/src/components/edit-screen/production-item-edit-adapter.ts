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
import { getDocument as getDocumentQuery } from '../../graphql/generated';
import type { ReviewItemDetail } from '../../graphql/generated/operation-types';
import useAppContext from '../../contexts/app';
import usePolling from '../../hooks/use-polling';
import { describeApiError } from '../../hooks/utils/graphql-error';
import { completeItem, fetchReviewItem, reviewErrorCode, saveDraft } from '../review-queues/review-api';
import { fetchSectionResult } from './section-result';
import type { LoadedSection, ReviewPage, ReviewSectionRef } from './types';
import type { RawDocument } from './use-edit-mode';

const client = generateClient();
const logger = new ConsoleLogger('ProductionItemEditAdapter');

export const REVIEW_ITEM_POLL_INTERVAL_MS = 20_000;
export const AUTOSAVE_MS = 2000;

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
  /** Refresh: re-read the item (a failed read throws), then the document. */
  refresh: () => Promise<void>;
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

/** The page images of a document read. */
const pagesOf = (doc: RawDocument | null | undefined): ReviewPage[] =>
  (doc?.Pages ?? [])
    .filter((p): p is NonNullable<typeof p> => !!p && p.Id != null && !!p.ImageUri)
    .map((p) => ({ Id: String(p.Id), ImageUri: p.ImageUri as string }));

/** Every section of a document read. */
const sectionsOf = (doc: RawDocument | null | undefined): ReviewSectionRef[] =>
  (doc?.Sections ?? [])
    .filter((s): s is NonNullable<typeof s> => !!s && s.Id != null)
    .map((s) => ({
      sectionId: String(s.Id),
      documentClass: s.Class ?? null,
      resultUri: s.OutputJSONUri ?? '',
      pageIds: (s.PageIds ?? []).map(String),
    }));

/** The latest draft revision per section of an item read. */
const revisionsOf = (detail: ReviewItemDetail | null | undefined, known: Record<string, number> = {}): Record<string, number> => {
  const revisions = { ...known };
  for (const s of detail?.sections ?? []) revisions[s.sectionId] = Math.max(revisions[s.sectionId] ?? 0, s.draftRevision ?? 0);
  return revisions;
};

/**
 * `seed`: the item and the document as the edit page has just read them (to choose the
 * mode), so neither first read is repeated; the item poll then starts on its interval.
 */
export const useProductionItemEdit = (
  objectKey: string,
  seed?: { detail: ReviewItemDetail; document: RawDocument },
): ProductionItemEditModel => {
  const { setErrorMessage } = useAppContext();
  const [detail, setDetail] = useState<ReviewItemDetail | null>(seed?.detail ?? null);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [pages, setPages] = useState<ReviewPage[]>(() => pagesOf(seed?.document));
  const [pagesLoading, setPagesLoading] = useState(!seed);
  const [documentSections, setDocumentSections] = useState<ReviewSectionRef[]>(() => sectionsOf(seed?.document));
  const [summaryReportUri, setSummaryReportUri] = useState<string | null>(seed?.document.SummaryReportUri || null);
  const [evaluationStatus, setEvaluationStatus] = useState<string | null>(seed?.document.EvaluationStatus || null);
  const [reloadToken, setReloadToken] = useState(0);
  // The latest draft revision per section, so a save names the revision it edits.
  const revisionsRef = useRef<Record<string, number>>(revisionsOf(seed?.detail));
  // The first item and document reads are the seed's; the key they belong to is the first one.
  const seededKey = useRef<string | null>(seed ? objectKey : null);

  const readItem = useCallback(async (): Promise<ReviewItemDetail> => {
    const result = await fetchReviewItem(objectKey);
    setDetail(result);
    setLoadError(null);
    revisionsRef.current = revisionsOf(result, revisionsRef.current);
    return result;
  }, [objectKey]);

  const reload = useCallback(async (): Promise<ReviewItemDetail | null> => {
    if (!objectKey) return null;
    try {
      return await readItem();
    } catch (err) {
      logger.error('getReviewItem failed', err);
      setLoadError(err);
      return null;
    }
  }, [objectKey, readItem]);

  useEffect(() => {
    if (seededKey.current === objectKey) return;
    setDetail(null);
    setLoadError(null);
    revisionsRef.current = {};
  }, [objectKey]);

  const poll = useCallback(async () => {
    await reload();
  }, [reload]);

  usePolling(poll, { enabled: !!objectKey, intervalMs: REVIEW_ITEM_POLL_INTERVAL_MS, immediate: !seed });

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
      setPages(pagesOf(doc));
      setDocumentSections(sectionsOf(doc));
    } catch (err) {
      logger.error('getDocument failed', err);
    } finally {
      if (read === documentRead.current) setPagesLoading(false);
    }
  }, [objectKey]);

  useEffect(() => {
    // A seeded document is already read; a later key reads its own.
    if (seededKey.current === objectKey) {
      seededKey.current = null;
      return undefined;
    }
    setPagesLoading(true);
    setPages([]);
    setDocumentSections([]);
    void refreshDocument();
    return () => {
      documentRead.current += 1;
    };
  }, [refreshDocument]);

  const refresh = useCallback(async (): Promise<void> => {
    await readItem();
    await refreshDocument();
  }, [readItem, refreshDocument]);

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
    refresh,
    summaryReportUri,
    evaluationStatus,
    reloadToken,
    loadSection,
    saveSection,
    complete,
  };
};
