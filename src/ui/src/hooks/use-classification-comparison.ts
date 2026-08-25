// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Loads a document's ground-truth-vs-predicted classification comparison from
// its `evaluation/results.json`, as a page-number-keyed index.
//
// Fetched once per document here rather than in each panel that annotates with
// it (Document Sections and Document Pages both need it), so the two tables
// share one request and one parse.
//
// Returns an empty index — never an error — when there is nothing to compare:
// evaluation not run, evaluated without section-level ground truth, or results
// pruned by retention. Callers annotate nothing in that case, so a document
// with no ground truth looks exactly as it did before.
import { useEffect, useState } from 'react';
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from '../api/client-shim';
import { getFileContents } from '../graphql/generated';
import { useDocumentVersion } from '../contexts/document-version';
import {
  ClassificationIndex,
  EMPTY_CLASSIFICATION_INDEX,
  evaluationResultsUriFrom,
  extractClassificationIndex,
} from '../components/common/classification-comparison-utils';

const client = generateClient();
const logger = new ConsoleLogger('useClassificationComparison');

const useClassificationComparison = (evaluationReportUri: string | undefined | null): ClassificationIndex => {
  const { versionIdForUri, runId } = useDocumentVersion();
  const [index, setIndex] = useState<ClassificationIndex>(EMPTY_CLASSIFICATION_INDEX);

  const resultsUri = evaluationResultsUriFrom(evaluationReportUri);

  useEffect(() => {
    if (!resultsUri) {
      setIndex(EMPTY_CLASSIFICATION_INDEX);
      return undefined;
    }

    let cancelled = false;
    const load = async () => {
      try {
        const response = await client.graphql({
          query: getFileContents,
          // versionIdForUri keeps a historical run snapshot pointing at that
          // run's results rather than the latest ones.
          variables: { s3Uri: resultsUri, versionId: versionIdForUri(resultsUri) },
        });
        const result = (response as { data: { getFileContents: { content: string; isBinary: boolean } } }).data.getFileContents;
        if (cancelled) return;
        if (result?.isBinary || !result?.content) {
          setIndex(EMPTY_CLASSIFICATION_INDEX);
          return;
        }
        setIndex(extractClassificationIndex(JSON.parse(result.content)));
      } catch (error) {
        // Nothing to compare and nothing the user can act on, so this stays a
        // debug log: the tables simply render unannotated.
        logger.debug('Classification comparison unavailable:', error instanceof Error ? error.message : error);
        if (!cancelled) setIndex(EMPTY_CLASSIFICATION_INDEX);
      }
    };

    load();
    return () => {
      cancelled = true;
    };
  }, [resultsUri, runId]);

  return index;
};

export default useClassificationComparison;
