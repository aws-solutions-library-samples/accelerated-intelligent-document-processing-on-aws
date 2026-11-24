// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The Test Studio adapter of the edit page: loads one test-set document's queue row,
// opens the set's version transition, saves a section's edits as a draft
// (saveTestSetDocumentSectionDraft: not reviewed, starts nothing), and completes the
// document by applying every section, or skips it (gives it back to its queue,
// unassigned and not reviewed, its drafts kept).
//
// Completing applies each section in turn: a section with a draft is written with the
// draft's content, any other is confirmed as it is. A document with a review record (a
// draft-labeled pipeline copy) applies through completeSectionReview; one without
// (authored ground truth) through saveTestSetDocumentSection. Both tag the label
// reviewed-human, record provenance, feed the confidence curve and delete the section's
// draft server-side, and end the assignment once every section is reviewed.
import { useCallback, useEffect, useRef, useState } from 'react';
import { ConsoleLogger } from 'aws-amplify/utils';

import { generateClient } from '../../api/client-shim';
import {
  completeSectionReview,
  getAnnotationQueueDocument,
  openTestSetAnnotationDraft,
  saveTestSetDocumentSection,
  saveTestSetDocumentSectionDraft,
  skipTestSetDocument,
} from '../../graphql/generated';
import type { GetAnnotationQueueDocumentQuery } from '../../graphql/generated/operation-types';
import { isAuthorizationError } from '../../hooks/utils/graphql-error';
import { reviewErrorCode } from '../review-queues/review-api';
import { currentDraft } from '../test-studio/annotation-edit-flags';
import type { AnnotationDraft } from '../test-studio/annotation-edit-flags';
import { EditPageError, editScreenSharedMessages } from './edit-messages';
import { fetchSectionResult } from './section-result';
import type { LoadedSection } from './types';

const client = generateClient();
const logger = new ConsoleLogger('TestSetItemEditAdapter');

/** A test set's annotation queue, as getAnnotationQueue returns it for one document. */
export type AnnotationQueue = NonNullable<GetAnnotationQueueDocumentQuery['getAnnotationQueue']>;
/** One document of the queue. `claimedByMe` is true when it is assigned to the caller. */
export type QueueItem = AnnotationQueue['documents'][number];

export interface TestSetItemEditModel {
  queue: AnnotationQueue | null;
  /** The document, or null while loading or when it is not in the set. */
  doc: QueueItem | null;
  loading: boolean;
  /**
   * Why the last read failed, for the page to word: `'not-assigned'`, the server refused it
   * (an authorization error, or NOT_ASSIGNED); `'load-failed'`, it returned no queue; otherwise
   * the error it threw, as `cause`.
   */
  loadError: 'not-assigned' | 'load-failed' | { readonly cause: unknown } | null;
  /** Re-read the document; resolves to the fresh row, or null when the read failed. */
  reload: () => Promise<QueueItem | null>;
  /** The same read as `reload`, for Refresh: a failed read throws rather than showing the load error. */
  refresh: () => Promise<void>;
  /** The open version transition; null until annotation has begun on the set. */
  draft: AnnotationDraft | null;
  openDraft: () => Promise<AnnotationDraft | null>;
  /** Save one section's edits as a draft: not reviewed, and nothing else changes. */
  saveDraft: (sectionId: string, data: Record<string, unknown>) => Promise<void>;
  /** Read a section: its draft when it has one, else `fallback` (its label). */
  loadSection: (sectionId: string, fallback: (sectionId: string) => Promise<LoadedSection>) => Promise<LoadedSection>;
  /** Bumped when the open section must be re-read (a newer draft arrived). */
  reloadToken: number;
  /** What a save has to tell the user, for the page to word: `'stale-draft'`, a newer draft was found and reloaded. */
  notice: 'stale-draft' | null;
  /** The page has shown `notice`. */
  clearNotice: () => void;
  /**
   * Apply every section, one at a time: a section's draft when it has one, otherwise
   * its label as it is. Resolves whether the server now counts the document reviewed.
   */
  applyAndComplete: () => Promise<{ reviewed: boolean }>;
  /**
   * Give the document back to its queue: the assignment ends and it is not reviewed.
   * `reason` comes from the set's skip list (`queue.skipReasons`); it and `note` are
   * recorded with the skip.
   */
  skip: (reason: string, note?: string) => Promise<void>;
}

export const useTestSetItemEdit = (testSetId: string, objectKey: string): TestSetItemEditModel => {
  const [queue, setQueue] = useState<AnnotationQueue | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<TestSetItemEditModel['loadError']>(null);
  const [openedDraft, setOpenedDraft] = useState<AnnotationDraft | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const [notice, setNotice] = useState<'stale-draft' | null>(null);
  const clearNotice = useCallback(() => setNotice(null), []);
  // Each section's latest draft, as last read or saved. Refs rather than state, so a
  // save never changes the editor's sections (which would re-read the open one).
  const draftUris = useRef<Record<string, string>>({});
  const revisions = useRef<Record<string, number>>({});

  const read = useCallback(
    async (rethrow: boolean): Promise<QueueItem | null> => {
      if (!testSetId || !objectKey) return null;
      setLoading(true);
      try {
        const r = await client.graphql({ query: getAnnotationQueueDocument, variables: { testSetId, objectKey } });
        const data = r.data?.getAnnotationQueue ?? null;
        if (!data) {
          if (rethrow) throw new EditPageError({ descriptor: editScreenSharedMessages.documentCouldNotLoad });
          setLoadError('load-failed');
          return null;
        }
        const fresh = data.documents.find((d) => d.objectKey === objectKey) ?? null;
        draftUris.current = {};
        revisions.current = {};
        for (const d of fresh?.draftSections ?? []) {
          draftUris.current[d.sectionId] = d.draftUri;
          revisions.current[d.sectionId] = d.revision;
        }
        setQueue(data);
        setLoadError(null);
        return fresh;
      } catch (err) {
        logger.error('getAnnotationQueue failed', err);
        if (rethrow) throw err;
        setLoadError(isAuthorizationError(err) || reviewErrorCode(err) === 'NOT_ASSIGNED' ? 'not-assigned' : { cause: err });
        return null;
      } finally {
        setLoading(false);
      }
    },
    [testSetId, objectKey],
  );
  const reload = useCallback(() => read(false), [read]);
  const refresh = useCallback(async () => {
    await read(true);
  }, [read]);

  useEffect(() => {
    setQueue(null);
    setOpenedDraft(null);
    void reload();
  }, [reload]);

  const doc = queue?.documents.find((d) => d.objectKey === objectKey) ?? null;

  /**
   * Open the version transition this session works in. Asked for rather than done on
   * arrival: annotating a set that already has ground truth produces a new version of
   * it, and that commitment must be visible. Idempotent server-side.
   */
  const openDraft = useCallback(async (): Promise<AnnotationDraft | null> => {
    const r = await client.graphql({ query: openTestSetAnnotationDraft, variables: { input: { testSetId } } });
    const opened = (r.data?.openTestSetAnnotationDraft as AnnotationDraft | null | undefined) ?? null;
    if (opened) setOpenedDraft(opened);
    return opened;
  }, [testSetId]);

  const loadSection = useCallback((sectionId: string, fallback: (sectionId: string) => Promise<LoadedSection>): Promise<LoadedSection> => {
    const draftUri = draftUris.current[sectionId];
    return draftUri ? fetchSectionResult(draftUri) : fallback(sectionId);
  }, []);

  const saveDraft = useCallback(
    async (sectionId: string, data: Record<string, unknown>): Promise<void> => {
      try {
        const r = await client.graphql({
          query: saveTestSetDocumentSectionDraft,
          variables: { testSetId, objectKey, sectionId, data: JSON.stringify(data), baseRevision: revisions.current[sectionId] ?? 0 },
        });
        const saved = r.data?.saveTestSetDocumentSectionDraft;
        if (saved) {
          draftUris.current[sectionId] = saved.draftUri;
          revisions.current[sectionId] = saved.revision;
        }
      } catch (err) {
        if (reviewErrorCode(err) === 'STALE_DRAFT') {
          setNotice('stale-draft');
          await reload();
          setReloadToken((t) => t + 1);
          return;
        }
        throw err;
      }
    },
    [testSetId, objectKey, reload],
  );

  const applyAndComplete = useCallback(async (): Promise<{ reviewed: boolean }> => {
    // The drafts as the server holds them now, so another session's save is applied too.
    const fresh = await reload();
    if (!fresh) throw new EditPageError({ descriptor: editScreenSharedMessages.documentCouldNotLoad });
    const drafts = new Map((fresh.draftSections ?? []).map((d) => [d.sectionId, d.draftUri]));
    let reviewed = false;
    // Sequential: every call updates the same document record.
    for (const { sectionId } of fresh.sections ?? []) {
      const draftUri = drafts.get(sectionId);
      const data = draftUri ? ((await fetchSectionResult(draftUri)).data as Record<string, unknown>) : null;
      if (fresh.reviewObjectKey) {
        await client.graphql({
          query: completeSectionReview,
          variables: { objectKey: fresh.reviewObjectKey, sectionId, ...(data ? { editedData: JSON.stringify(data) } : {}) },
        });
      } else {
        const r = await client.graphql({
          query: saveTestSetDocumentSection,
          variables: { testSetId, objectKey, sectionId, ...(data ? { data: JSON.stringify(data) } : {}) },
        });
        reviewed = !!r.data?.saveTestSetDocumentSection?.reviewed;
      }
    }
    // A review copy's verdict is on the queue row the completions updated.
    if (fresh.reviewObjectKey) reviewed = !!(await reload())?.reviewed;
    return { reviewed };
  }, [reload, testSetId, objectKey]);

  const skip = useCallback(
    async (reason: string, note?: string): Promise<void> => {
      await client.graphql({ query: skipTestSetDocument, variables: { testSetId, objectKey, reason, note } });
    },
    [testSetId, objectKey],
  );

  return {
    queue,
    doc,
    loading,
    loadError,
    reload,
    refresh,
    draft: currentDraft(queue, openedDraft),
    openDraft,
    saveDraft,
    loadSection,
    reloadToken,
    notice,
    clearNotice,
    applyAndComplete,
    skip,
  };
};
