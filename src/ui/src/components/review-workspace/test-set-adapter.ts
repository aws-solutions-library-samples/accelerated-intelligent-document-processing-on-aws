// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The Test Studio adapter of the annotation screen: loads one test-set document's
// queue row, opens the set's version transition, saves a section through the review
// API, and completes the document by confirming the sections not saved yet, or skips
// it (gives it back to its queue, unassigned and not reviewed).
//
// A document with a review record (a draft-labeled pipeline copy) saves through
// completeSectionReview, which tags the label reviewed-human, records provenance and
// feeds the confidence curve. One without (authored ground truth) saves through
// saveTestSetDocumentSection, which tags and records it the same way. Both accept the
// document's assignee (or an Admin or Author) and end the assignment once every
// section is reviewed.
import { useCallback, useEffect, useState } from 'react';
import { ConsoleLogger } from 'aws-amplify/utils';

import { generateClient } from '../../api/client-shim';
import {
  completeSectionReview,
  getAnnotationQueueDocument,
  openTestSetAnnotationDraft,
  saveTestSetDocumentSection,
  skipTestSetDocument,
} from '../../graphql/generated';
import type { GetAnnotationQueueQuery } from '../../graphql/generated/operation-types';
import { describeApiError, isAuthorizationError } from '../../hooks/utils/graphql-error';
import { reviewErrorCode } from '../review-queues/review-api';
import { currentDraft } from '../test-studio/annotation-edit-flags';
import type { AnnotationDraft } from '../test-studio/annotation-edit-flags';

const client = generateClient();
const logger = new ConsoleLogger('TestSetAnnotationAdapter');

/** A test set's annotation queue, as getAnnotationQueue returns it. */
export type AnnotationQueue = NonNullable<GetAnnotationQueueQuery['getAnnotationQueue']>;
/** One document of the queue. `claimedByMe` is true when it is assigned to the caller. */
export type QueueItem = AnnotationQueue['documents'][number];

export const NOT_ASSIGNED_MESSAGE =
  "You can't open this document. Only the Annotators listed on its test set's queue can; ask an Admin to list you.";

export interface TestSetAnnotation {
  queue: AnnotationQueue | null;
  /** The document, or null while loading or when it is not in the set. */
  doc: QueueItem | null;
  loading: boolean;
  loadError: string | null;
  reload: () => Promise<void>;
  /** The open version transition; null until annotation has begun on the set. */
  draft: AnnotationDraft | null;
  openDraft: () => Promise<AnnotationDraft | null>;
  /** Save one section's edited labels; the section counts as reviewed. */
  saveSection: (sectionId: string, data: Record<string, unknown>) => Promise<void>;
  /** Confirm sections unchanged, one at a time; each counts as reviewed. */
  confirmSections: (sectionIds: string[]) => Promise<void>;
  /** Give the document back to its queue: the assignment ends and it is not reviewed. */
  skip: () => Promise<void>;
}

export const useTestSetAnnotation = (testSetId: string, objectKey: string): TestSetAnnotation => {
  const [queue, setQueue] = useState<AnnotationQueue | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [openedDraft, setOpenedDraft] = useState<AnnotationDraft | null>(null);

  const reload = useCallback(async () => {
    if (!testSetId || !objectKey) return;
    setLoading(true);
    try {
      const r = await client.graphql({ query: getAnnotationQueueDocument, variables: { testSetId, objectKey } });
      const data = r.data?.getAnnotationQueue ?? null;
      if (!data) {
        setLoadError('The document could not be loaded.');
        return;
      }
      setQueue(data);
      setLoadError(null);
    } catch (err) {
      logger.error('getAnnotationQueue failed', err);
      setLoadError(
        isAuthorizationError(err) || reviewErrorCode(err) === 'NOT_ASSIGNED'
          ? NOT_ASSIGNED_MESSAGE
          : describeApiError(err, 'load the document'),
      );
    } finally {
      setLoading(false);
    }
  }, [testSetId, objectKey]);

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

  const saveSection = useCallback(
    async (sectionId: string, data: Record<string, unknown>): Promise<void> => {
      if (doc?.reviewObjectKey) {
        await client.graphql({
          query: completeSectionReview,
          variables: { objectKey: doc.reviewObjectKey, sectionId, editedData: JSON.stringify(data) },
        });
        return;
      }
      await client.graphql({
        query: saveTestSetDocumentSection,
        variables: { testSetId, objectKey, sectionId, data: JSON.stringify(data) },
      });
    },
    [doc, testSetId, objectKey],
  );

  const confirmSections = useCallback(
    async (sectionIds: string[]): Promise<void> => {
      // Sequential: every call updates the same document record.
      for (const sectionId of sectionIds) {
        if (doc?.reviewObjectKey) {
          await client.graphql({ query: completeSectionReview, variables: { objectKey: doc.reviewObjectKey, sectionId } });
        } else {
          await client.graphql({ query: saveTestSetDocumentSection, variables: { testSetId, objectKey, sectionId } });
        }
      }
    },
    [doc, testSetId, objectKey],
  );

  const skip = useCallback(async (): Promise<void> => {
    await client.graphql({ query: skipTestSetDocument, variables: { testSetId, objectKey } });
  }, [testSetId, objectKey]);

  return {
    queue,
    doc,
    loading,
    loadError,
    reload,
    draft: currentDraft(queue, openedDraft),
    openDraft,
    saveSection,
    confirmSections,
    skip,
  };
};
