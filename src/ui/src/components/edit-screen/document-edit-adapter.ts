// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The direct-edit adapter of the unified editor: loads any processed document (whether
// or not it is in review), its page images and section results; saves a section's
// edits without reprocessing (Save); and submits the document for
// reprocessing (Complete).
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ConsoleLogger } from 'aws-amplify/utils';

import { generateClient } from '../../api/client-shim';
import useAppContext from '../../contexts/app';
import useSettingsContext from '../../contexts/settings';
import { getDocument as getDocumentQuery } from '../../graphql/generated';
import useUserRole from '../../hooks/use-user-role';
import mapDocumentsAttributes from '../common/map-document-attributes';
import { submitProcessChanges } from '../common/process-changes';
import { isInReviewQueue } from '../common/review-status';
import { saveDirectSectionEdits } from './document-edit-save';
import { fetchSectionResult } from './production-item-edit-adapter';
import type { LoadedSection, ReviewPage, ReviewSectionRef } from './types';
import type { SectionEvaluation } from './use-section-evaluation';

const client = generateClient();
const logger = new ConsoleLogger('DocumentEditAdapter');

/** The document fields the direct edit reads (from mapDocumentsAttributes). */
export interface DirectEditDocument {
  objectKey: string;
  objectStatus?: string;
  evaluationStatus?: string | null;
  /** The document's summary report (`s3://…`), once summarization has run. */
  summaryReportUri?: string | null;
  hitlStatus?: string;
  /** The document's HITL review events (parsed HITLReviewHistory). */
  hitlReviewHistory?: { action?: string; timestamp?: string; user?: string; sectionId?: string; [key: string]: unknown }[];
  hitlReviewOwner?: string;
  hitlReviewOwnerEmail?: string;
  configVersion?: string | null;
  sections?: { Id: string | number; Class?: string | null; PageIds?: (string | number)[] | null; OutputJSONUri?: string | null }[];
  pages?: { Id: string | number; ImageUri?: string }[];
}

export interface DocumentEditModel {
  document: DirectEditDocument | null;
  loading: boolean;
  loadError: string | null;
  reload: () => Promise<void>;
  pages: ReviewPage[];
  sections: ReviewSectionRef[];
  loadSection: (sectionId: string) => Promise<LoadedSection>;
  /** Save a section's edits (and the baseline's, when `evaluation` has some); returns the stored prediction. */
  saveSection: (sectionId: string, data: Record<string, unknown>, evaluation?: SectionEvaluation) => Promise<Record<string, unknown>>;
  processChanges: () => Promise<void>;
  /** The document is in a review queue: edits are made in the queue. */
  inReview: boolean;
  /** A Reviewer without the Admin or Author role, who saves through the review API. */
  reviewerOnly: boolean;
}

export const useDocumentEdit = (objectKey: string): DocumentEditModel => {
  const { user } = useAppContext();
  const { settings } = useSettingsContext();
  const { isReviewerOnly, canReview, canWrite } = useUserRole();
  const [document, setDocument] = useState<DirectEditDocument | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  // Each section's result as loaded or last saved: what an edit is compared with.
  const originals = useRef<Record<string, Record<string, unknown>>>({});

  const reload = useCallback(async () => {
    if (!objectKey) return;
    setLoading(true);
    try {
      const result = await client.graphql({ query: getDocumentQuery, variables: { objectKey } });
      const raw = result.data?.getDocument;
      if (!raw) {
        setDocument(null);
        setLoadError('NOT_FOUND');
        return;
      }
      const [mapped] = mapDocumentsAttributes([raw as unknown as { ObjectKey: string }]);
      setDocument(mapped as unknown as DirectEditDocument);
      setLoadError(null);
    } catch (err) {
      logger.error('getDocument failed', err);
      setLoadError(err instanceof Error ? err.message : 'The document could not be loaded.');
    } finally {
      setLoading(false);
    }
  }, [objectKey]);

  useEffect(() => {
    originals.current = {};
    void reload();
  }, [reload]);

  const sections: ReviewSectionRef[] = useMemo(
    () =>
      (document?.sections ?? []).map((s) => ({
        sectionId: String(s.Id),
        documentClass: s.Class ?? null,
        resultUri: s.OutputJSONUri ?? '',
        pageIds: (s.PageIds ?? []).map(String),
      })),
    [document?.sections],
  );

  const pages: ReviewPage[] = useMemo(
    () =>
      (document?.pages ?? [])
        .filter((p) => p.Id != null && !!p.ImageUri)
        .map((p) => ({ Id: String(p.Id), ImageUri: p.ImageUri as string })),
    [document?.pages],
  );

  const loadSection = useCallback(
    async (sectionId: string): Promise<LoadedSection> => {
      const section = sections.find((s) => s.sectionId === sectionId);
      if (!section?.resultUri) throw new Error('This section has no result to edit');
      const loaded = await fetchSectionResult(section.resultUri);
      originals.current[sectionId] = JSON.parse(loaded.raw) as Record<string, unknown>;
      return loaded;
    },
    [sections],
  );

  const reviewerOnly = isReviewerOnly || (canReview && !canWrite);

  const saveSection = useCallback(
    async (sectionId: string, data: Record<string, unknown>, evaluation?: SectionEvaluation): Promise<Record<string, unknown>> => {
      const section = sections.find((s) => s.sectionId === sectionId);
      if (!section?.resultUri) throw new Error('This section has no result to save');
      const saved = await saveDirectSectionEdits({
        objectKey,
        sectionId,
        outputUri: section.resultUri,
        username: user?.username || 'unknown',
        reviewerOnly,
        prediction: data,
        originalPrediction: originals.current[sectionId] ?? {},
        baseline: evaluation?.available ? evaluation.baseline : null,
        originalBaseline: evaluation?.available ? evaluation.originalBaseline : null,
        baselineBucket: (settings as Record<string, unknown>)?.EvaluationBaselineBucket as string | undefined,
      });
      originals.current[sectionId] = JSON.parse(JSON.stringify(saved.prediction)) as Record<string, unknown>;
      if (evaluation?.available && !saved.baselineSkipped) evaluation.markBaselineSaved(saved.baseline);
      return saved.prediction;
    },
    [objectKey, reviewerOnly, sections, settings, user?.username],
  );

  const processChanges = useCallback(() => submitProcessChanges(objectKey, []), [objectKey]);

  return {
    document,
    loading,
    loadError,
    reload,
    pages,
    sections,
    loadSection,
    saveSection,
    processChanges,
    inReview: isInReviewQueue(document?.hitlStatus),
    reviewerOnly,
  };
};
