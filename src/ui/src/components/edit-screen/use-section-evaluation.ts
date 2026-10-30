// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The evaluation baseline of the open section, for the unified editor's direct edit:
// the baseline result (read from the evaluation baseline bucket at the section's
// output key), the document's evaluation results, and the baseline being edited.
// Loaded the way the Visual Document Editor loads them, and only when the document
// has a baseline (evaluation status BASELINE_AVAILABLE or COMPLETED).
import { useCallback, useEffect, useMemo, useState } from 'react';
import { ConsoleLogger } from 'aws-amplify/utils';

import { generateClient } from '../../api/client-shim';
import useSettingsContext from '../../contexts/settings';
import { getFileContents } from '../../graphql/generated';
import { extractClassificationIndex } from '../common/classification-comparison-utils';

const client = generateClient();
const logger = new ConsoleLogger('useSectionEvaluation');

/** The evaluation statuses under which a document has a baseline to compare with. */
export const BASELINE_STATUSES: readonly string[] = ['BASELINE_AVAILABLE', 'COMPLETED'];

export interface SectionEvaluation {
  /** The document has a baseline. */
  available: boolean;
  loading: boolean;
  /** The selected section's baseline, with any edits made here. */
  baseline: Record<string, unknown> | null;
  /** The baseline as loaded (or last saved). */
  originalBaseline: Record<string, unknown> | null;
  setBaseline: (next: Record<string, unknown>) => void;
  /** Adopt a saved baseline as the new unchanged state. */
  markBaselineSaved: (saved: Record<string, unknown> | null) => void;
  evaluationResults: Record<string, unknown> | null;
  classificationIndex: ReturnType<typeof extractClassificationIndex>;
  /** False for a reviewer-only caller, who doesn't save the baseline. */
  canEditBaseline: boolean;
}

export interface UseSectionEvaluationOptions {
  enabled: boolean;
  objectKey: string;
  /** The selected section's OutputJSONUri. */
  outputUri: string | null;
  evaluationStatus?: string | null;
  reviewerOnly: boolean;
}

/** `s3://bucket/key` → its key, or null when the URI isn't an S3 URI. */
const s3Key = (uri: string): string | null => uri.match(/^s3:\/\/[^/]+\/(.+)$/)?.[1] ?? null;

const readJson = async (s3Uri: string): Promise<Record<string, unknown> | null> => {
  const response = await client.graphql({ query: getFileContents, variables: { s3Uri } });
  const result = response.data?.getFileContents;
  if (!result || result.isBinary || !result.content) return null;
  return JSON.parse(result.content) as Record<string, unknown>;
};

const useSectionEvaluation = ({
  enabled,
  objectKey,
  outputUri,
  evaluationStatus,
  reviewerOnly,
}: UseSectionEvaluationOptions): SectionEvaluation => {
  const { settings } = useSettingsContext();
  const outputBucket = (settings as Record<string, unknown>)?.OutputBucket as string | undefined;
  const baselineBucket = (settings as Record<string, unknown>)?.EvaluationBaselineBucket as string | undefined;
  const available = enabled && !!evaluationStatus && BASELINE_STATUSES.includes(evaluationStatus);

  const [baseline, setBaselineState] = useState<Record<string, unknown> | null>(null);
  const [originalBaseline, setOriginalBaseline] = useState<Record<string, unknown> | null>(null);
  const [evaluationResults, setEvaluationResults] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setBaselineState(null);
    setOriginalBaseline(null);
    setEvaluationResults(null);
    if (!available || !outputUri) {
      setLoading(false);
      return undefined;
    }
    let cancelled = false;
    const key = s3Key(outputUri);
    const baselineUri = key && baselineBucket ? `s3://${baselineBucket}/${key}` : null;
    const resultsUri = outputBucket ? `s3://${outputBucket}/${objectKey}/evaluation/results.json` : null;
    setLoading(true);
    (async () => {
      const [loadedBaseline, loadedResults] = await Promise.all([
        baselineUri
          ? readJson(baselineUri).catch((err) => {
              logger.warn('Failed to load baseline data:', err instanceof Error ? err.message : err);
              return null;
            })
          : Promise.resolve(null),
        resultsUri
          ? readJson(resultsUri).catch((err) => {
              logger.warn('Failed to load evaluation results:', err instanceof Error ? err.message : err);
              return null;
            })
          : Promise.resolve(null),
      ]);
      if (cancelled) return;
      setBaselineState(loadedBaseline);
      setOriginalBaseline(loadedBaseline);
      setEvaluationResults(loadedResults);
      setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [available, outputUri, objectKey, outputBucket, baselineBucket]);

  const setBaseline = useCallback((next: Record<string, unknown>) => setBaselineState(next), []);
  const markBaselineSaved = useCallback((saved: Record<string, unknown> | null) => {
    setBaselineState(saved);
    setOriginalBaseline(saved);
  }, []);
  const classificationIndex = useMemo(() => extractClassificationIndex(evaluationResults), [evaluationResults]);

  return {
    available,
    loading,
    baseline,
    originalBaseline,
    setBaseline,
    markBaselineSaved,
    evaluationResults,
    classificationIndex,
    canEditBaseline: !reviewerOnly,
  };
};

export default useSectionEvaluation;
