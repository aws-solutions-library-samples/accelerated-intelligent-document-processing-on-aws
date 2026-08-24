// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState, useEffect, useMemo } from 'react';
import {
  Alert,
  Badge,
  Box,
  ColumnLayout,
  Container,
  ExpandableSection,
  Header,
  Popover,
  SpaceBetween,
  StatusIndicator,
  Table,
  Toggle,
} from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from '../../api/client-shim';
import { getFileContents } from '../../graphql/generated';
import { useDocumentVersion } from '../../contexts/document-version';
import {
  ClassificationComparison,
  PageComparison,
  SectionComparison,
  countPageMismatches,
  countSectionMismatches,
  evaluationResultsUriFrom,
  extractClassificationComparison,
  formatAccuracy,
  formatPageRanges,
} from './classification-comparison-utils';

const client = generateClient();
const logger = new ConsoleLogger('ClassificationComparisonPanel');

interface ClassificationComparisonPanelProps {
  /** The document's evaluation report URI; results.json is derived from it. */
  evaluationReportUri?: string;
  evaluationStatus?: string;
}

const ClassCell = ({ value, tone }: { value: string | null; tone: 'expected' | 'actual' | 'plain' }): React.JSX.Element => {
  if (!value) {
    return <Box color="text-status-inactive">not classified</Box>;
  }
  if (tone === 'plain') return <span>{value}</span>;
  return <Badge color={tone === 'expected' ? 'blue' : 'grey'}>{value}</Badge>;
};

/**
 * "(2)" when nothing is filtered, "(2 of 7)" when the mismatch filter is
 * hiding rows — so a short table never looks like the whole picture.
 */
const countLabel = (visible: number, total: number): string => (visible === total ? `(${visible})` : `(${visible} of ${total})`);

const MatchCell = ({ matched, label }: { matched: boolean; label: string }): React.JSX.Element => (
  <StatusIndicator type={matched ? 'success' : 'error'}>{matched ? 'Match' : label}</StatusIndicator>
);

const ClassificationComparisonPanel = ({
  evaluationReportUri,
  evaluationStatus,
}: ClassificationComparisonPanelProps): React.JSX.Element | null => {
  const { versionIdForUri, runId } = useDocumentVersion();
  const [comparison, setComparison] = useState<ClassificationComparison | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [loadFailed, setLoadFailed] = useState<boolean>(false);
  const [mismatchesOnly, setMismatchesOnly] = useState<boolean>(true);

  const resultsUri = useMemo(() => evaluationResultsUriFrom(evaluationReportUri), [evaluationReportUri]);

  useEffect(() => {
    if (!resultsUri) {
      setComparison(null);
      return;
    }

    let cancelled = false;
    const load = async () => {
      setIsLoading(true);
      setLoadFailed(false);
      try {
        const response = await client.graphql({
          query: getFileContents,
          variables: { s3Uri: resultsUri, versionId: versionIdForUri(resultsUri) },
        });
        const result = (response as { data: { getFileContents: { content: string; isBinary: boolean } } }).data.getFileContents;
        if (cancelled) return;
        if (result?.isBinary || !result?.content) {
          setComparison(null);
          return;
        }
        setComparison(extractClassificationComparison(JSON.parse(result.content)));
      } catch (error) {
        // A document evaluated before this data existed, or one whose results
        // were pruned, simply has nothing to show — so this is not surfaced as
        // an error banner, only as a quiet note if the panel is expanded.
        logger.debug('Classification comparison unavailable:', error instanceof Error ? error.message : error);
        if (!cancelled) {
          setComparison(null);
          setLoadFailed(true);
        }
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    };

    load();
    return () => {
      cancelled = true;
    };
  }, [resultsUri, runId]);

  const pageMismatches = comparison ? countPageMismatches(comparison) : 0;
  const sectionMismatches = comparison ? countSectionMismatches(comparison) : 0;

  // Default the filter to whichever view is useful: mismatches when there are
  // any (the reason someone opens this from a low-scoring document), the full
  // list when the run was clean and there would otherwise be an empty table.
  useEffect(() => {
    if (comparison) setMismatchesOnly(pageMismatches > 0 || sectionMismatches > 0);
  }, [comparison, pageMismatches, sectionMismatches]);

  const visiblePages = useMemo(
    () => (comparison ? comparison.pages.filter((page) => !mismatchesOnly || !page.correct) : []),
    [comparison, mismatchesOnly],
  );
  const visibleSections = useMemo(
    () => (comparison ? comparison.sections.filter((section) => !mismatchesOnly || !section.matched) : []),
    [comparison, mismatchesOnly],
  );

  // Nothing to render when the document was never evaluated against a
  // baseline. Showing an empty panel would imply the comparison exists and is
  // perfect, which is a different claim.
  if (!resultsUri && evaluationStatus !== 'RUNNING') return null;
  if (!isLoading && !comparison && !loadFailed) return null;

  const hasMismatch = pageMismatches > 0 || sectionMismatches > 0;

  return (
    <Container
      header={
        <Header
          variant="h2"
          description="What the ground truth says each page and section is, next to what this run classified it as."
          counter={hasMismatch ? `(${pageMismatches + sectionMismatches} mismatched)` : undefined}
          actions={
            comparison ? (
              <Toggle checked={mismatchesOnly} onChange={({ detail }) => setMismatchesOnly(detail.checked)} disabled={!hasMismatch}>
                Mismatches only
              </Toggle>
            ) : undefined
          }
        >
          Classification vs Ground Truth
        </Header>
      }
    >
      {isLoading && <StatusIndicator type="loading">Loading classification comparison…</StatusIndicator>}

      {!isLoading && !comparison && (
        <Box color="text-status-inactive">
          No classification comparison is available for this document. It is produced when a document is evaluated against ground truth that
          includes section boundaries.
        </Box>
      )}

      {!isLoading && comparison && (
        <SpaceBetween size="l">
          <ColumnLayout columns={3} variant="text-grid">
            <div>
              <Box variant="awsui-key-label">Page classification accuracy</Box>
              <Box variant="h3">{formatAccuracy(comparison.pageLevelAccuracy)}</Box>
              <Box color="text-body-secondary" fontSize="body-s">
                {comparison.correctPages} of {comparison.totalPages} pages match
              </Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Section split accuracy</Box>
              <Box variant="h3">{formatAccuracy(comparison.splitAccuracyWithoutOrder)}</Box>
              <Box color="text-body-secondary" fontSize="body-s">
                Same pages grouped together, same class
              </Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Section split accuracy (with order)</Box>
              <Box variant="h3">{formatAccuracy(comparison.splitAccuracyWithOrder)}</Box>
              <Box color="text-body-secondary" fontSize="body-s">
                As above, and the page order agrees
              </Box>
            </div>
          </ColumnLayout>

          {comparison.errors.length > 0 && (
            <Alert type="warning" header="Some sections could not be compared">
              <ul>
                {comparison.errors.map((error) => (
                  <li key={error}>{error}</li>
                ))}
              </ul>
            </Alert>
          )}

          {hasMismatch && (
            <Alert type="info">
              A classification mismatch means fields were extracted against the wrong schema, so low extraction scores for these pages are a
              symptom rather than the cause. Fix the classification first.
            </Alert>
          )}

          <Table
            variant="embedded"
            header={
              <Header variant="h3" counter={countLabel(visiblePages.length, comparison.pages.length)}>
                Pages
              </Header>
            }
            items={visiblePages}
            trackBy="pageIndex"
            columnDefinitions={[
              {
                id: 'page',
                header: 'Page',
                cell: (item: PageComparison) => item.pageNumber,
                width: 90,
              },
              {
                id: 'groundTruth',
                header: 'Ground truth class',
                cell: (item: PageComparison) => <ClassCell value={item.groundTruthClass} tone="expected" />,
                minWidth: 200,
              },
              {
                id: 'predicted',
                header: 'This run classified as',
                cell: (item: PageComparison) => <ClassCell value={item.predictedClass} tone="actual" />,
                minWidth: 200,
              },
              {
                id: 'match',
                header: 'Result',
                cell: (item: PageComparison) => <MatchCell matched={item.correct} label="Misclassified" />,
                width: 160,
              },
            ]}
            empty={
              <Box textAlign="center" color="text-status-inactive" padding={{ vertical: 's' }}>
                {comparison.pages.length === 0 ? 'No page-level comparison available' : 'Every page matches ground truth'}
              </Box>
            }
          />

          <ExpandableSection
            headerText={`Sections ${countLabel(visibleSections.length, comparison.sections.length)}`}
            defaultExpanded={sectionMismatches > 0}
          >
            <Table
              variant="embedded"
              items={visibleSections}
              trackBy="rowKey"
              columnDefinitions={[
                {
                  id: 'match',
                  header: 'Result',
                  cell: (item: SectionComparison) => (
                    <SpaceBetween size="xxs">
                      <MatchCell
                        matched={item.matched}
                        label={item.groundTruthClass === null ? 'Extra section' : item.predictedClass === null ? 'Not found' : 'Mismatch'}
                      />
                      {item.matched && !item.orderMatched && (
                        <Popover
                          dismissButton={false}
                          position="top"
                          triggerType="text"
                          content="The right pages were grouped under the right class, but not in the ground truth's order."
                        >
                          <StatusIndicator type="warning">Page order differs</StatusIndicator>
                        </Popover>
                      )}
                    </SpaceBetween>
                  ),
                  minWidth: 180,
                },
                {
                  id: 'groundTruth',
                  header: 'Ground truth class',
                  cell: (item: SectionComparison) => <ClassCell value={item.groundTruthClass} tone="expected" />,
                  minWidth: 180,
                },
                {
                  id: 'groundTruthPages',
                  header: 'GT pages',
                  cell: (item: SectionComparison) => formatPageRanges(item.groundTruthPages),
                  width: 120,
                },
                {
                  id: 'predicted',
                  header: 'This run classified as',
                  cell: (item: SectionComparison) => <ClassCell value={item.predictedClass} tone="actual" />,
                  minWidth: 180,
                },
                {
                  id: 'predictedPages',
                  header: 'Run pages',
                  cell: (item: SectionComparison) => formatPageRanges(item.predictedPages),
                  width: 120,
                },
                {
                  id: 'sectionId',
                  header: 'Section',
                  cell: (item: SectionComparison) => item.sectionId ?? item.predictedSectionId ?? '—',
                  width: 120,
                },
              ]}
              empty={
                <Box textAlign="center" color="text-status-inactive" padding={{ vertical: 's' }}>
                  {comparison.sections.length === 0 ? 'No section-level comparison available' : 'Every section matches ground truth'}
                </Box>
              }
            />
          </ExpandableSection>
        </SpaceBetween>
      )}
    </Container>
  );
};

export default ClassificationComparisonPanel;
