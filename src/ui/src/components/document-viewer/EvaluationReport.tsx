// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The evaluation report, as a component rather than a rendered markdown file.
 *
 * It was built as an add-on and still looked like one: a `.md` artifact shown in
 * a panel, with a little embedded HTML for expanding nested fields. That made the
 * headline number — how accurate was this document? — something you read out of
 * prose, and made everything else unsortable and unlinkable.
 *
 * Same data, read from `evaluation/results.json`. The markdown artifact is still
 * generated and still downloadable; this changes the presentation, not the
 * pipeline. Falling back to the markdown when the JSON cannot be read is
 * deliberate: a document evaluated by an older build may have one and not the
 * other, and a report that renders is worth more than a consistent component.
 */

import React, { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  ColumnLayout,
  Container,
  ExpandableSection,
  Header,
  Popover,
  SpaceBetween,
  Spinner,
  Table,
  Toggle,
} from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';

import { generateClient } from '../../api/client-shim';
import { getFileContents } from '../../graphql/generated';
import { MarkdownReport } from './MarkdownViewer';
import type { AttributeResult, EvaluationResults, SectionResult } from './evaluationReportModel';
import { extractClassificationIndex, formatPageRanges, evaluationResultsUriFrom } from '../common/classification-comparison-utils';
import { evaluationMethodsUsed, formatScore, mismatchedAttributes, scoreBand, summarizeEvaluation } from './evaluationReportModel';

const client = generateClient();
const logger = new ConsoleLogger('EvaluationReport');

const BAND_COLOUR: Record<string, 'green' | 'blue' | 'severity-medium' | 'red' | 'grey'> = {
  good: 'green',
  fair: 'blue',
  poor: 'severity-medium',
  bad: 'red',
  unknown: 'grey',
};

interface EvaluationReportProps {
  /** URI of the markdown report; the JSON results are its sibling. */
  reportUri: string;
  documentId: string;
}

/** One of the headline figures. Omitted entirely when there is no value. */
const ScoreTile = ({ label, score, hint }: { label: string; score: number | null; hint?: string }): React.JSX.Element | null => {
  if (score === null) return null;
  const band = scoreBand(score);
  return (
    <div>
      <Box variant="awsui-key-label">{label}</Box>
      <SpaceBetween direction="horizontal" size="xs" alignItems="center">
        <Box variant="h1" padding={{ top: 'n' }}>
          {formatScore(score)}
        </Box>
        <Badge color={BAND_COLOUR[band]}>{band}</Badge>
      </SpaceBetween>
      {hint && (
        <Box variant="small" color="text-body-secondary">
          {hint}
        </Box>
      )}
    </div>
  );
};

const valueCell = (value: unknown): string => {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
};

const AttributeTable = ({ attributes }: { attributes: AttributeResult[] }): React.JSX.Element => (
  <Table
    resizableColumns
    variant="embedded"
    contentDensity="compact"
    items={attributes}
    columnDefinitions={[
      {
        id: 'matched',
        header: '',
        cell: (item: AttributeResult) => <Badge color={item.matched ? 'green' : 'red'}>{item.matched ? 'match' : 'mismatch'}</Badge>,
        width: 110,
      },
      { id: 'name', header: 'Field', cell: (item: AttributeResult) => item.name ?? '—', sortingField: 'name' },
      { id: 'expected', header: 'Expected', cell: (item: AttributeResult) => valueCell(item.expected) },
      { id: 'actual', header: 'Extracted', cell: (item: AttributeResult) => valueCell(item.actual) },
      {
        id: 'score',
        header: 'Score',
        cell: (item: AttributeResult) => (typeof item.score === 'number' ? item.score.toFixed(3) : '—'),
        sortingField: 'score',
      },
      {
        id: 'method',
        header: 'Method',
        cell: (item: AttributeResult) =>
          item.reason ? (
            // The reason is why this scored as it did — the single most useful
            // thing on the row when a score is surprising, and it was previously
            // buried in a markdown table cell.
            <Popover dismissButton={false} position="top" size="medium" triggerType="text" content={item.reason}>
              {item.evaluation_method ?? 'compare'}
            </Popover>
          ) : (
            (item.evaluation_method ?? '—')
          ),
      },
    ]}
    empty={<Box textAlign="center">No fields</Box>}
  />
);

const EvaluationReport = ({ reportUri, documentId }: EvaluationReportProps): React.JSX.Element => {
  const [results, setResults] = useState<EvaluationResults | null>(null);
  const [loading, setLoading] = useState(true);
  const [jsonUnavailable, setJsonUnavailable] = useState(false);
  const [onlyProblems, setOnlyProblems] = useState(false);
  const [showMarkdown, setShowMarkdown] = useState(false);

  const resultsUri = useMemo(() => evaluationResultsUriFrom(reportUri), [reportUri]);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      if (!resultsUri) {
        setJsonUnavailable(true);
        setLoading(false);
        return;
      }
      setLoading(true);
      try {
        const response = await client.graphql({ query: getFileContents, variables: { s3Uri: resultsUri } });
        const file = (response as { data: { getFileContents: { content: string; isBinary: boolean } } }).data.getFileContents;
        if (cancelled) return;
        if (!file || file.isBinary || !file.content) {
          setJsonUnavailable(true);
        } else {
          setResults(JSON.parse(file.content) as EvaluationResults);
        }
      } catch (err) {
        // Not an error state: fall back to the markdown, which this document
        // definitely has (its URI is how we got here).
        logger.info('Evaluation results.json unavailable, falling back to markdown:', err);
        if (!cancelled) setJsonUnavailable(true);
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    load();
    return () => {
      cancelled = true;
    };
  }, [resultsUri]);

  const summary = useMemo(() => summarizeEvaluation(results), [results]);
  const methods = useMemo(() => evaluationMethodsUsed(results), [results]);
  // Page-derived, so a section whose split is wrong still has a ground-truth
  // class for every page to compare against — which is why this is reported per
  // document rather than per section: results.json carries no page ids on its
  // section entries, and the Visual Editor's Show Evaluation mode already
  // annotates section by section.
  const misclassifiedPages = useMemo(() => {
    const index = extractClassificationIndex(results as Record<string, unknown> | null);
    if (!index.hasGroundTruth) return [];
    const byPair = new Map<string, { expected: string; predicted: string; pages: number[] }>();
    index.byPageNumber.forEach((page) => {
      if (page.correct) return;
      const key = `${page.predictedClass}→${page.groundTruthClass}`;
      const existing = byPair.get(key);
      if (existing) existing.pages.push(page.pageNumber);
      else byPair.set(key, { expected: page.groundTruthClass, predicted: page.predictedClass, pages: [page.pageNumber] });
    });
    return [...byPair.values()];
  }, [results]);

  if (loading) {
    return (
      <Box padding="l" textAlign="center">
        <Spinner /> Loading evaluation…
      </Box>
    );
  }

  // Either the JSON is unreadable (an older document may have the markdown and
  // not the JSON) or the reader asked for the markdown explicitly.
  if (jsonUnavailable || !results || showMarkdown) {
    return (
      <SpaceBetween size="s">
        {showMarkdown && !jsonUnavailable && (
          <Button iconName="arrow-left" onClick={() => setShowMarkdown(false)}>
            Back to evaluation summary
          </Button>
        )}
        <MarkdownReport
          reportUri={reportUri}
          documentId={documentId}
          title="Evaluation Report"
          emptyMessage="Evaluation report not available for this document"
        />
      </SpaceBetween>
    );
  }

  const sections = results.section_results ?? [];

  return (
    <Container
      header={
        <Header
          variant="h2"
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Toggle checked={onlyProblems} onChange={({ detail }) => setOnlyProblems(detail.checked)}>
                Mismatches only
              </Toggle>
              {/* The markdown artifact is still what you attach to a ticket or
                  hand to someone without UI access, and it already carries its
                  own download and print actions — so it stays one click away
                  rather than being replaced. */}
              <Button iconName="file" onClick={() => setShowMarkdown(true)}>
                Markdown report
              </Button>
            </SpaceBetween>
          }
        >
          Evaluation
        </Header>
      }
    >
      <SpaceBetween size="l">
        {summary.excluded && (
          <Alert type="info" header="This document was not scored">
            No section had an extractable schema{summary.exclusionReason ? ` (${summary.exclusionReason})` : ''}, so accuracy figures would
            be meaningless and are omitted.
          </Alert>
        )}

        {/* Front and centre: the two questions the report exists to answer, kept
            separate because they fail independently — a document can be classified
            perfectly and extracted badly, or the reverse. */}
        <ColumnLayout columns={3} variant="text-grid">
          <ScoreTile
            label="Extraction accuracy"
            score={summary.extractionScore}
            hint={
              summary.extractionIsWeighted
                ? 'Weighted by field importance'
                : `${summary.matchedAttributes} of ${summary.totalAttributes} fields matched`
            }
          />
          <ScoreTile label="Classification accuracy" score={summary.classificationScore} hint="Page level" />
          <ScoreTile
            label="F1 score"
            score={summary.f1Score}
            hint={`Precision ${formatScore(summary.precision)} · Recall ${formatScore(summary.recall)}`}
          />
        </ColumnLayout>

        {/* A wrong class makes the field numbers above and below it meaningless
            rather than merely wrong, so it is reported before them. */}
        {misclassifiedPages.length > 0 && (
          <Alert type="warning" header="Some pages are not the class ground truth expects">
            <SpaceBetween size="xxs">
              {misclassifiedPages.map((group) => (
                <Box key={`${group.predicted}-${group.expected}`} variant="p">
                  Page{group.pages.length > 1 ? 's' : ''} <b>{formatPageRanges(group.pages)}</b> classified as <b>{group.predicted}</b>, but
                  ground truth says <b>{group.expected}</b>.
                </Box>
              ))}
              <Box variant="small" color="text-body-secondary">
                Extraction runs against the assigned class&apos;s schema, so the fields for those pages may be wrong even where they look
                plausible. Correct the class and re-extract before reading the numbers here.
              </Box>
            </SpaceBetween>
          </Alert>
        )}

        {summary.excludedSectionCount > 0 && (
          <Box variant="small" color="text-body-secondary">
            {summary.excludedSectionCount} section{summary.excludedSectionCount === 1 ? '' : 's'} not evaluated (no extractable schema).
          </Box>
        )}

        {sections.map((section: SectionResult) => {
          const attributes = onlyProblems ? mismatchedAttributes(section) : (section.attributes ?? []);
          const mismatchCount = mismatchedAttributes(section).length;

          return (
            <ExpandableSection
              key={String(section.section_id)}
              defaultExpanded={sections.length === 1 || mismatchCount > 0}
              headerText={`Section ${section.section_id} — ${section.document_class ?? 'unknown class'}`}
              headerCounter={mismatchCount > 0 ? `(${mismatchCount} mismatched)` : undefined}
            >
              <SpaceBetween size="s">
                {onlyProblems && attributes.length === 0 ? (
                  <Box color="text-status-success">Every field in this section matched.</Box>
                ) : (
                  <AttributeTable attributes={attributes} />
                )}
              </SpaceBetween>
            </ExpandableSection>
          );
        })}

        {methods.length > 0 && (
          <ExpandableSection headerText="Comparison methods used">
            <Box variant="small">
              A surprising score is often a comparison-method question rather than an extraction one. This document used:{' '}
              {methods.join(', ')}.
            </Box>
          </ExpandableSection>
        )}
      </SpaceBetween>
    </Container>
  );
};

export default EvaluationReport;
