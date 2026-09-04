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
  StatusIndicator,
  Table,
  Toggle,
} from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';

import { generateClient } from '../../api/client-shim';
import { getFileContents } from '../../graphql/generated';
import { MarkdownReport } from './MarkdownViewer';
import type { ComparisonRow, EvaluationResults, MetricRow, SectionResult, SplitAnalysis, SplitRow } from './evaluationReportModel';
import { extractClassificationIndex, formatPageRanges, evaluationResultsUriFrom } from '../common/classification-comparison-utils';
import {
  attributeRows,
  describeValue,
  evaluationMethodsUsed,
  excludedSectionRows,
  formatDuration,
  formatScore,
  metricRows,
  mismatchedAttributes,
  scoreBand,
  sectionFailure,
  skippedFieldCount,
  splitAnalysis,
  summarizeEvaluation,
} from './evaluationReportModel';

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

/**
 * A nested value, rendered as structure rather than as one JSON string.
 *
 * Lists are numbered and objects keyed, recursively, so a Hungarian-matched
 * list of transactions reads as the rows it is. Bounded height, because a
 * 500-item list belongs behind a scroll, not across the page.
 */
const StructuredValue = ({ value, depth = 0 }: { value: unknown; depth?: number }): React.JSX.Element => {
  if (Array.isArray(value)) {
    if (value.length === 0) return <Box color="text-body-secondary">empty list</Box>;
    return (
      <ol style={{ margin: 0, paddingLeft: '1.4em' }}>
        {value.map((item, index) => (
          // A list position is the identity of a list item; there is nothing else to key on.
          // eslint-disable-next-line react/no-array-index-key
          <li key={index}>
            <StructuredValue value={item} depth={depth + 1} />
          </li>
        ))}
      </ol>
    );
  }
  if (value !== null && typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length === 0) return <Box color="text-body-secondary">empty object</Box>;
    return (
      <dl style={{ margin: 0, paddingLeft: depth ? '0.6em' : 0 }}>
        {entries.map(([k, v]) => (
          <div key={k} style={{ display: 'flex', gap: '0.5em', alignItems: 'baseline' }}>
            <dt style={{ fontWeight: 600, whiteSpace: 'nowrap' }}>{k}:</dt>
            <dd style={{ margin: 0 }}>
              <StructuredValue value={v} depth={depth + 1} />
            </dd>
          </div>
        ))}
      </dl>
    );
  }
  const shape = describeValue(value);
  return shape.kind === 'empty' ? <Box color="text-body-secondary">—</Box> : <span>{shape.summary}</span>;
};

/** A table cell for a value of any shape: scalars inline, structures on demand. */
const ValueCell = ({ value }: { value: unknown }): React.JSX.Element => {
  const shape = describeValue(value);
  if (shape.kind === 'scalar' || shape.kind === 'empty') return <span>{shape.summary}</span>;
  return (
    <Popover
      dismissButton
      position="bottom"
      size="large"
      triggerType="text"
      header={shape.kind === 'list' ? 'List value' : 'Object value'}
      content={
        <div style={{ maxHeight: '50vh', overflow: 'auto' }}>
          <StructuredValue value={value} />
        </div>
      }
    >
      {shape.summary}
    </Popover>
  );
};

const bandIndicator: Record<string, 'success' | 'info' | 'warning' | 'error'> = {
  good: 'success',
  fair: 'info',
  poor: 'warning',
  bad: 'error',
};

const BAND_LABEL: Record<string, string> = { good: 'Excellent', fair: 'Good', poor: 'Fair', bad: 'Poor' };

/** The markdown's "| Metric | Value | Rating |" table. */
const MetricTable = ({ rows, emptyText }: { rows: MetricRow[]; emptyText: string }): React.JSX.Element => (
  <Table
    variant="embedded"
    contentDensity="compact"
    items={rows}
    trackBy="metric"
    columnDefinitions={[
      { id: 'metric', header: 'Metric', cell: (row: MetricRow) => row.metric },
      { id: 'value', header: 'Value', cell: (row: MetricRow) => (row.value === null ? 'Not scored' : row.value.toFixed(4)) },
      {
        id: 'rating',
        header: 'Rating',
        cell: (row: MetricRow) =>
          row.value === null ? (
            <StatusIndicator type="stopped">Excluded</StatusIndicator>
          ) : row.band ? (
            <StatusIndicator type={bandIndicator[row.band]}>{BAND_LABEL[row.band]}</StatusIndicator>
          ) : (
            '—'
          ),
      },
    ]}
    empty={<Box textAlign="center">{emptyText}</Box>}
  />
);

/** The markdown's "Section Split Analysis" table, plus its error list. */
const SplitAnalysisSection = ({ split }: { split: SplitAnalysis }): React.JSX.Element => {
  const verdict = (ok: boolean, label: string) => <StatusIndicator type={ok ? 'success' : 'error'}>{label}</StatusIndicator>;
  return (
    <SpaceBetween size="s">
      <Table
        variant="embedded"
        contentDensity="compact"
        wrapLines
        items={split.rows}
        trackBy={(row: SplitRow) => `${row.sectionId ?? 'pred'}-${row.matchedSectionId ?? 'none'}`}
        columnDefinitions={[
          {
            id: 'section',
            header: 'Section match',
            cell: (row: SplitRow) => verdict(row.sectionMatched, row.sectionMatched ? 'Matched' : 'No match'),
          },
          {
            id: 'order',
            header: 'Page order',
            cell: (row: SplitRow) => verdict(row.orderMatched, row.orderMatched ? 'In order' : 'Differs'),
          },
          { id: 'id', header: 'Expected section', cell: (row: SplitRow) => row.sectionId ?? '—' },
          { id: 'expectedClass', header: 'Expected class', cell: (row: SplitRow) => row.expectedClass ?? '—' },
          { id: 'expectedPages', header: 'Expected pages', cell: (row: SplitRow) => formatPageRanges(row.expectedPages) },
          {
            id: 'predictedClass',
            header: 'Predicted class',
            cell: (row: SplitRow) => (
              <span>
                {row.predictedClass ?? '—'}
                {/* Which predicted section this is, when that is not obvious: a
                    pairing with a differently-numbered section, or a prediction
                    nothing expected. */}
                {row.matchedSectionId && row.matchedSectionId !== row.sectionId && (
                  <Box variant="small" color="text-body-secondary">
                    predicted section {row.matchedSectionId}
                  </Box>
                )}
              </span>
            ),
          },
          { id: 'predictedPages', header: 'Predicted pages', cell: (row: SplitRow) => formatPageRanges(row.predictedPages) },
        ]}
        empty={<Box textAlign="center">No section data available</Box>}
      />
      {split.graded && (
        <Box variant="small" color="text-body-secondary">
          Graded packet score {formatScore(split.graded.finalScore)}
          {split.graded.clusteringScore !== null && ` · clustering ${formatScore(split.graded.clusteringScore)}`}
          {split.graded.orderingScore !== null && ` · ordering ${formatScore(split.graded.orderingScore)}`}
          {split.graded.vMeasure !== null && ` · V-measure ${formatScore(split.graded.vMeasure)}`}
          {split.graded.randIndex !== null && ` · Rand index ${formatScore(split.graded.randIndex)}`}
        </Box>
      )}
      {split.errors.length > 0 && (
        <Alert type="warning" header="Doc split errors">
          <ul style={{ margin: 0 }}>
            {split.errors.map((error, index) => (
              // eslint-disable-next-line react/no-array-index-key
              <li key={index}>{error}</li>
            ))}
          </ul>
        </Alert>
      )}
    </SpaceBetween>
  );
};

/**
 * The attribute table, with the markdown's extra columns (confidence against its
 * threshold, weight) and its nested comparisons as expandable rows: an aggregate
 * attribute — a nested object or a matched list — opens into the field-by-field
 * pairs beneath it, which is where a Hungarian match says which item went with
 * which.
 */
const AttributeTable = ({ rows }: { rows: ComparisonRow[] }): React.JSX.Element => {
  const [expanded, setExpanded] = useState<ComparisonRow[]>([]);
  return (
    <Table
      resizableColumns
      variant="embedded"
      contentDensity="compact"
      wrapLines
      items={rows}
      trackBy="key"
      ariaLabels={{
        expandButtonLabel: (row: ComparisonRow) => `Show nested comparisons for ${row.name}`,
        collapseButtonLabel: (row: ComparisonRow) => `Hide nested comparisons for ${row.name}`,
      }}
      expandableRows={{
        getItemChildren: (row: ComparisonRow) => row.children ?? [],
        isItemExpandable: (row: ComparisonRow) => (row.children?.length ?? 0) > 0,
        expandedItems: expanded,
        onExpandableItemToggle: ({ detail }) =>
          setExpanded((current) => (detail.expanded ? [...current, detail.item] : current.filter((r) => r.key !== detail.item.key))),
      }}
      columnDefinitions={[
        {
          id: 'matched',
          header: '',
          cell: (row: ComparisonRow) => <Badge color={row.matched ? 'green' : 'red'}>{row.matched ? 'match' : 'mismatch'}</Badge>,
          width: 150,
        },
        {
          id: 'name',
          header: 'Field',
          cell: (row: ComparisonRow) => (
            <span>
              {row.name}
              {row.actualPath && (
                <Box variant="small" color="text-body-secondary">
                  paired with {row.actualPath}
                </Box>
              )}
            </span>
          ),
          sortingField: 'name',
          minWidth: 240,
        },
        { id: 'expected', header: 'Expected', cell: (row: ComparisonRow) => <ValueCell value={row.expected} /> },
        { id: 'actual', header: 'Extracted', cell: (row: ComparisonRow) => <ValueCell value={row.actual} /> },
        {
          id: 'confidence',
          header: 'Confidence',
          cell: (row: ComparisonRow) =>
            row.confidence === null
              ? '—'
              : `${row.confidence.toFixed(2)}${row.confidenceThreshold !== null ? ` / ${row.confidenceThreshold.toFixed(2)}` : ''}`,
          width: 120,
        },
        {
          id: 'score',
          header: 'Score',
          cell: (row: ComparisonRow) =>
            row.score === null
              ? '—'
              : row.children
                ? // An aggregate: the score is over the nested comparisons below it,
                  // not a comparison of two values, and the markdown says so too.
                  `${row.score.toFixed(3)} (aggregate)`
                : row.score.toFixed(3),
          sortingField: 'score',
          width: 150,
        },
        { id: 'weight', header: 'Weight', cell: (row: ComparisonRow) => (row.weight === null ? '1.00' : row.weight.toFixed(2)), width: 90 },
        {
          id: 'method',
          header: 'Method',
          minWidth: 200,
          cell: (row: ComparisonRow) =>
            row.reason ? (
              // The reason is why this scored as it did — the single most useful
              // thing on the row when a score is surprising.
              <Popover dismissButton={false} position="top" size="medium" triggerType="text" content={row.reason}>
                {row.method ?? 'compare'}
              </Popover>
            ) : (
              (row.method ?? '—')
            ),
        },
      ]}
      empty={<Box textAlign="center">No fields</Box>}
    />
  );
};

/**
 * The markdown's "Evaluation Methods Used", "Field Weighting" and "Metrics
 * Explanation" prose, condensed. Static by design: it explains the scoring
 * scheme, not this document, and the per-document part ("which methods were
 * used here") is listed separately above it.
 */
const HowScoresAreComputed = ({ hasSplit }: { hasSplit: boolean }): React.JSX.Element => (
  <SpaceBetween size="s">
    <Box variant="h4">Field-level comparison methods</Box>
    <ul style={{ margin: 0 }}>
      <li>
        <b>Exact</b> — character-for-character match. IDs, codes.
      </li>
      <li>
        <b>NumericExact</b> — numeric comparison with a tolerance from <code>x-aws-idp-evaluation-threshold</code>. Amounts, percentages.
      </li>
      <li>
        <b>Fuzzy</b> / <b>Levenshtein</b> — string similarity against a threshold. Names, addresses, minor variations.
      </li>
      <li>
        <b>Semantic</b> — embedding similarity against a threshold. Text where meaning matters more than wording.
      </li>
      <li>
        <b>Date</b> — compares resolved dates, not surface form (<code>2024-01-05</code> equals <code>January 5, 2024</code>); ranges and
        day-first via <code>x-aws-idp-evaluation-method-config</code>.
      </li>
      <li>
        <b>LLM</b> — a Bedrock model judges the pair with reasoning, configured under <code>evaluation.llm_method</code>. Nested objects,
        semantic equivalence.
      </li>
    </ul>
    <Box variant="h4">Array-level matching</Box>
    <ul style={{ margin: 0 }}>
      <li>
        <b>Hungarian</b> — optimal one-to-one pairing between the expected and extracted lists; each pair is then compared field by field,
        which is what the expandable rows above show. Configured with <code>x-aws-idp-evaluation-method: &quot;HUNGARIAN&quot;</code> on the
        array.
      </li>
      <li>
        <b>LLM for arrays</b> — judges whether two lists match as a whole.
      </li>
    </ul>
    <Box variant="h4">Field weighting</Box>
    <Box variant="p">
      <code>x-aws-idp-evaluation-weight</code> sets a field&apos;s importance (default 1.0). The weighted overall score is Σ(weight × score)
      / Σ(weight) per section, and the document score is the average across sections. The weight column above shows each field&apos;s value.
    </Box>
    {hasSplit && (
      <>
        <Box variant="h4">Split metrics</Box>
        <ul style={{ margin: 0 }}>
          <li>
            <b>Page-level accuracy</b> — each page&apos;s predicted class against its expected class, ignoring how pages were grouped.
          </li>
          <li>
            <b>Split accuracy (without order)</b> — an expected section counts as correct when a predicted section has the same set of pages
            and the same class.
          </li>
          <li>
            <b>Split accuracy (with order)</b> — the same, and the page order must match exactly. The strictest of the three.
          </li>
        </ul>
      </>
    )}
  </SpaceBetween>
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
  const split = useMemo(() => splitAnalysis(results), [results]);
  const excluded = useMemo(() => excludedSectionRows(results), [results]);
  const overallRows = useMemo(() => metricRows(results?.overall_metrics as Record<string, unknown> | null), [results]);
  const splitMetricRows = useMemo(() => {
    const raw = (results?.doc_split_metrics ?? null) as Record<string, unknown> | null;
    if (!raw) return [];
    // The three headline split metrics, in the markdown's order; the rest of the
    // split payload is detail, rendered as the analysis table.
    return metricRows({
      page_level_accuracy: raw.page_level_accuracy,
      split_accuracy_without_order: raw.split_accuracy_without_order,
      split_accuracy_with_order: raw.split_accuracy_with_order,
    });
  }, [results]);
  const executionTime = formatDuration(results?.execution_time);
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
        <ColumnLayout columns={split ? 4 : 3} variant="text-grid">
          <ScoreTile
            label="Extraction accuracy"
            score={summary.extractionScore}
            hint={
              summary.extractionIsWeighted
                ? 'Weighted by field importance'
                : `${summary.matchedAttributes} of ${summary.totalAttributes} fields matched`
            }
          />
          <ScoreTile
            label="Classification accuracy"
            score={summary.classificationScore}
            hint={split ? `${split.pageLevel.correct} of ${split.pageLevel.total} pages` : 'Page level'}
          />
          {/* The split figures the markdown leads its summary with. Without-order
              is the headline because it is the question "did the packet split
              correctly"; the ordered figure is the stricter variant beside it. */}
          {split && (
            <ScoreTile
              label="Split accuracy"
              score={split.withoutOrder.score}
              hint={`${split.withoutOrder.correct} of ${split.withoutOrder.total} sections · in order ${formatScore(split.withOrder.score)}`}
            />
          )}
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

        {excluded.length > 0 && (
          <ExpandableSection
            variant="container"
            headerText={`${excluded.length} section${excluded.length === 1 ? '' : 's'} not evaluated`}
            headerDescription="Skipped by evaluation and not counted in the figures above"
          >
            <SpaceBetween size="xs">
              <Box variant="small" color="text-body-secondary">
                A section is excluded when its class is marked <code>x-aws-idp-exclude-from-processing</code>, or when the class defines no
                extractable attributes in the evaluation schema.
              </Box>
              <Table
                variant="embedded"
                contentDensity="compact"
                items={excluded}
                trackBy="sectionId"
                columnDefinitions={[
                  { id: 'section', header: 'Section', cell: (row) => row.sectionId },
                  { id: 'class', header: 'Classification', cell: (row) => row.classification || '—' },
                  { id: 'reason', header: 'Exclusion reason', cell: (row) => row.reason },
                  { id: 'pages', header: 'Pages', cell: (row) => formatPageRanges(row.pages) },
                ]}
              />
            </SpaceBetween>
          </ExpandableSection>
        )}

        {/* The markdown's "Section Split Analysis": which expected section was
            matched to which predicted one, and where pages or order went wrong.
            Open by default when something did, since that is when it is read. */}
        {split && (
          <ExpandableSection
            variant="container"
            headerText="Section split analysis"
            headerCounter={`(${split.rows.filter((r) => !r.sectionMatched).length} unmatched)`}
            defaultExpanded={split.rows.some((r) => !r.sectionMatched || !r.orderMatched)}
          >
            <SplitAnalysisSection split={split} />
          </ExpandableSection>
        )}

        <ExpandableSection
          variant="container"
          headerText="All metrics"
          headerDescription="Every figure the run recorded, with the rating the report applies to it"
        >
          <SpaceBetween size="m">
            {splitMetricRows.length > 0 && (
              <SpaceBetween size="xs">
                <Box variant="h4">Document split classification</Box>
                <MetricTable rows={splitMetricRows} emptyText="No split metrics" />
              </SpaceBetween>
            )}
            <SpaceBetween size="xs">
              <Box variant="h4">Document extraction</Box>
              <MetricTable rows={overallRows} emptyText="No extraction metrics recorded" />
            </SpaceBetween>
          </SpaceBetween>
        </ExpandableSection>

        {sections.map((section: SectionResult) => {
          const attributes = onlyProblems ? mismatchedAttributes(section) : (section.attributes ?? []);
          const mismatchCount = mismatchedAttributes(section).length;
          const failure = sectionFailure(section);
          const skipped = skippedFieldCount(section);
          const sectionMetrics = metricRows(section.metrics as Record<string, unknown> | null);
          // The section's own score, in the header where the markdown's "### Section"
          // heading had none: a document score is an average of these, so a reader
          // needs to see which section pulled it down without opening each one.
          const metricsMap = (section.metrics ?? {}) as Record<string, unknown>;
          const sectionScore =
            typeof metricsMap.weighted_overall_score === 'number'
              ? metricsMap.weighted_overall_score
              : typeof metricsMap.f1_score === 'number'
                ? metricsMap.f1_score
                : null;
          const counterParts = [
            failure ? 'not scored' : sectionScore !== null ? formatScore(sectionScore) : null,
            mismatchCount > 0 ? `${mismatchCount} mismatched` : null,
          ].filter(Boolean);

          return (
            <ExpandableSection
              key={String(section.section_id)}
              // Container variant: it is the only one that renders headerCounter, and
              // the section score lives there.
              variant="container"
              defaultExpanded={sections.length === 1 || mismatchCount > 0 || failure !== null}
              headerText={`Section ${section.section_id} — ${section.document_class ?? 'unknown class'}`}
              headerCounter={counterParts.length > 0 ? `(${counterParts.join(' · ')})` : undefined}
            >
              <SpaceBetween size="s">
                {failure && (
                  <Alert type="error" header="This section was not evaluated">
                    <SpaceBetween size="xs">
                      <Box>
                        {failure.reason ?? `This section could not be evaluated for document class ${section.document_class ?? 'unknown'}.`}
                      </Box>
                      <Box variant="small" color="text-body-secondary">
                        It contributes zero to every document-level figure above. Its metrics below are placeholders for a section that was
                        never scored, not measurements of a bad extraction.
                      </Box>
                      {failure.steps.length > 0 && (
                        <div>
                          <Box variant="strong">How to fix</Box>
                          <ol style={{ margin: 0 }}>
                            {failure.steps.map((step) => (
                              <li key={step}>{step}</li>
                            ))}
                          </ol>
                        </div>
                      )}
                    </SpaceBetween>
                  </Alert>
                )}
                {!failure && skipped > 0 && (
                  <Alert type="warning">
                    {skipped} field{skipped === 1 ? ' was' : 's were'} excluded from scoring because {skipped === 1 ? 'it' : 'they'} could
                    not be validated against the schema. The remaining fields were evaluated normally.
                  </Alert>
                )}
                {sectionMetrics.length > 0 && (
                  <ExpandableSection headerText="Section metrics" variant="footer">
                    <MetricTable rows={sectionMetrics} emptyText="No metrics" />
                  </ExpandableSection>
                )}
                {failure ? null : onlyProblems && attributes.length === 0 ? (
                  <Box color="text-status-success">Every field in this section matched.</Box>
                ) : (
                  <AttributeTable rows={attributeRows(attributes, `${section.section_id}/`)} />
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

        <ExpandableSection headerText="How scores are computed">
          <HowScoresAreComputed hasSplit={split !== null} />
        </ExpandableSection>

        {executionTime && (
          <Box variant="small" color="text-body-secondary">
            Evaluation took {executionTime}.
          </Box>
        )}
      </SpaceBetween>
    </Container>
  );
};

export default EvaluationReport;
