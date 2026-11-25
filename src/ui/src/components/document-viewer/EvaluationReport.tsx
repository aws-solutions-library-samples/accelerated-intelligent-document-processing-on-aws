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
import type { TableProps } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';
import { defineMessages, FormattedMessage, useIntl, type MessageTag, type NoMessageValues } from 'react-intl';

import { generateClient } from '../../api/client-shim';
import { getFileContents } from '../../graphql/generated';
import { MarkdownReport } from './MarkdownViewer';
import type {
  ComparisonRow,
  EvaluationResults,
  MetricRow,
  ScoreBand,
  SectionResult,
  SplitAnalysis,
  SplitRow,
} from './evaluationReportModel';
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
import { useFormatters } from '../../i18n/format';

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

/**
 * The markdown's rating words, used everywhere a band is shown so one number never has two labels.
 * The `unknown` band, a missing score's, has none: the report rates no missing score.
 */
const bandMessages = defineMessages<{ good: NoMessageValues; fair: NoMessageValues; poor: NoMessageValues; bad: NoMessageValues }>({
  good: {
    id: 'documents.evaluationReport.band.good',
    defaultMessage: 'Excellent',
    description:
      'Rating of a score in Reports › Evaluation Report on the document page, beside a headline score and in the Rating column of a metrics table. The best of four: a score of 90% or more, or an error rate of 10% or less.',
  },
  fair: {
    id: 'documents.evaluationReport.band.fair',
    defaultMessage: 'Good',
    description:
      'Rating of a score in Reports › Evaluation Report on the document page, beside a headline score and in the Rating column of a metrics table. The second of four: a score from 70% to under 90%, or an error rate above 10% up to 30%.',
  },
  poor: {
    id: 'documents.evaluationReport.band.poor',
    defaultMessage: 'Fair',
    description:
      'Rating of a score in Reports › Evaluation Report on the document page, beside a headline score and in the Rating column of a metrics table. The third of four: a score from 50% to under 70%, or an error rate above 30% up to 50%.',
  },
  bad: {
    id: 'documents.evaluationReport.band.bad',
    defaultMessage: 'Poor',
    description:
      'Rating of a score in Reports › Evaluation Report on the document page, beside a headline score and in the Rating column of a metrics table. The worst of four: a score under 50%, or an error rate above 50%.',
  },
});

const messages = defineMessages<{
  emptyList: NoMessageValues;
  emptyObject: NoMessageValues;
  listValue: NoMessageValues;
  objectValue: NoMessageValues;
  metricColumn: NoMessageValues;
  valueColumn: NoMessageValues;
  ratingColumn: NoMessageValues;
  notScored: NoMessageValues;
  excludedRating: NoMessageValues;
  sectionMatchColumn: NoMessageValues;
  matched: NoMessageValues;
  noMatch: NoMessageValues;
  pageOrderColumn: NoMessageValues;
  ordered: NoMessageValues;
  reordered: NoMessageValues;
  expectedSectionColumn: NoMessageValues;
  expectedClassColumn: NoMessageValues;
  expectedPagesColumn: NoMessageValues;
  predictedClassColumn: NoMessageValues;
  predictedSection: { readonly sectionId: string };
  predictedPagesColumn: NoMessageValues;
  splitEmpty: NoMessageValues;
  gradedScore: { readonly score: string };
  clustering: { readonly score: string };
  ordering: { readonly score: string };
  vMeasure: { readonly score: string };
  randIndex: { readonly score: string };
  splitErrors: NoMessageValues;
  showNested: { readonly name: string };
  hideNested: { readonly name: string };
  match: NoMessageValues;
  mismatch: NoMessageValues;
  fieldColumn: NoMessageValues;
  pairedWith: { readonly path: string };
  expectedColumn: NoMessageValues;
  extractedColumn: NoMessageValues;
  confidenceColumn: NoMessageValues;
  scoreColumn: NoMessageValues;
  aggregateScore: { readonly score: string };
  weightColumn: NoMessageValues;
  methodColumn: NoMessageValues;
  compare: NoMessageValues;
  fieldsEmpty: NoMessageValues;
  loading: NoMessageValues;
  backToSummary: NoMessageValues;
  markdownEmpty: NoMessageValues;
  mismatchesOnly: NoMessageValues;
  markdownReport: NoMessageValues;
  title: NoMessageValues;
  documentNotScored: NoMessageValues;
  documentNotScoredBody: NoMessageValues;
  documentNotScoredBodyWithReason: { readonly reason: string };
  extractionTile: NoMessageValues;
  extractionHintWeighted: { readonly matched: number; readonly total: number };
  extractionHint: { readonly matched: number; readonly total: number };
  classificationTile: NoMessageValues;
  classificationHint: { readonly correct: number; readonly total: number };
  splitTile: NoMessageValues;
  splitHint: { readonly correct: number; readonly total: number; readonly score: string };
  f1Tile: NoMessageValues;
  f1Hint: { readonly precision: string; readonly recall: string };
  misclassifiedHeader: NoMessageValues;
  misclassifiedPages: {
    readonly count: number;
    readonly pages: string;
    readonly predicted: string;
    readonly expected: string;
    readonly b: MessageTag;
  };
  misclassifiedHint: NoMessageValues;
  excludedHeader: { readonly count: number };
  excludedDescription: NoMessageValues;
  excludedExplanation: { readonly key: string; readonly code: MessageTag };
  excludedSectionColumn: NoMessageValues;
  excludedClassColumn: NoMessageValues;
  excludedReasonColumn: NoMessageValues;
  excludedPagesColumn: NoMessageValues;
  splitAnalysisHeader: NoMessageValues;
  splitUnmatched: { readonly unmatched: number; readonly expected: number };
  splitUnexpected: { readonly count: number };
  allMetricsHeader: NoMessageValues;
  allMetricsDescription: NoMessageValues;
  splitMetricsHeading: NoMessageValues;
  extractionMetricsHeading: NoMessageValues;
  extractionMetricsEmpty: NoMessageValues;
  sectionNotScored: NoMessageValues;
  sectionMismatched: { readonly count: number };
  sectionHeader: { readonly sectionId: string; readonly className: string };
  sectionHeaderUnknownClass: { readonly sectionId: string };
  sectionNotEvaluated: NoMessageValues;
  couldNotEvaluate: { readonly className: string };
  couldNotEvaluateUnknownClass: NoMessageValues;
  placeholders: NoMessageValues;
  howToFix: NoMessageValues;
  skippedFields: { readonly count: number };
  sectionMetrics: NoMessageValues;
  allMatched: NoMessageValues;
  methodsHeader: NoMessageValues;
  methodsBody: { readonly methods: string };
  howScores: NoMessageValues;
  executionTime: { readonly executionTime: string };
}>({
  emptyList: {
    id: 'documents.evaluationReport.value.emptyList',
    defaultMessage: 'empty list',
    description:
      "Shown in place of an empty list inside a field's list or object value, opened from the field table of Reports › Evaluation Report on the document page.",
  },
  emptyObject: {
    id: 'documents.evaluationReport.value.emptyObject',
    defaultMessage: 'empty object',
    description:
      "Shown in place of an empty object inside a field's list or object value, opened from the field table of Reports › Evaluation Report on the document page.",
  },
  listValue: {
    id: 'documents.evaluationReport.value.listValue',
    defaultMessage: 'List value',
    description:
      "Heading of the popover that shows a field's expected or extracted value when it is a list, in the field table of Reports › Evaluation Report on the document page.",
  },
  objectValue: {
    id: 'documents.evaluationReport.value.objectValue',
    defaultMessage: 'Object value',
    description:
      "Heading of the popover that shows a field's expected or extracted value when it is an object, in the field table of Reports › Evaluation Report on the document page.",
  },
  metricColumn: {
    id: 'documents.evaluationReport.metrics.metricColumn',
    defaultMessage: 'Metric',
    description:
      "Column header of a metrics table in Reports › Evaluation Report on the document page: the metric's name, as the evaluation records it.",
  },
  valueColumn: {
    id: 'documents.evaluationReport.metrics.valueColumn',
    defaultMessage: 'Value',
    description: "Column header of a metrics table in Reports › Evaluation Report on the document page: the metric's value.",
  },
  ratingColumn: {
    id: 'documents.evaluationReport.metrics.ratingColumn',
    defaultMessage: 'Rating',
    description: "Column header of a metrics table in Reports › Evaluation Report on the document page: the metric's rating.",
  },
  notScored: {
    id: 'documents.evaluationReport.metrics.notScored',
    defaultMessage: 'Not scored',
    description:
      'In a metrics table of Reports › Evaluation Report on the document page: the value of a metric the evaluation recorded without one, and the rating of every metric of a section that was not evaluated.',
  },
  excludedRating: {
    id: 'documents.evaluationReport.metrics.excluded',
    defaultMessage: 'Excluded',
    description:
      'Rating, in a metrics table of Reports › Evaluation Report on the document page, of a metric the evaluation recorded without a value.',
  },
  sectionMatchColumn: {
    id: 'documents.evaluationReport.split.sectionMatchColumn',
    defaultMessage: 'Section match',
    description:
      'Column header of the Section split analysis table in Reports › Evaluation Report on the document page: whether a predicted section has the same pages and class as the expected section.',
  },
  matched: {
    id: 'documents.evaluationReport.split.matched',
    defaultMessage: 'Matched',
    description:
      'Section match of a row in the Section split analysis of Reports › Evaluation Report on the document page: a predicted section has the same pages and class as the expected section.',
  },
  noMatch: {
    id: 'documents.evaluationReport.split.noMatch',
    defaultMessage: 'No match',
    description:
      'Section match of a row in the Section split analysis of Reports › Evaluation Report on the document page: no predicted section has the same pages and class as the expected section.',
  },
  pageOrderColumn: {
    id: 'documents.evaluationReport.split.pageOrderColumn',
    defaultMessage: 'Page order',
    description:
      'Column header of the Section split analysis table in Reports › Evaluation Report on the document page: whether the section also matched with its pages in the expected order.',
  },
  ordered: {
    id: 'documents.evaluationReport.split.ordered',
    defaultMessage: 'Ordered',
    description:
      'Page order of a row in the Section split analysis of Reports › Evaluation Report on the document page: the section matched with its pages in the expected order.',
  },
  reordered: {
    id: 'documents.evaluationReport.split.reordered',
    defaultMessage: 'Reordered',
    description:
      'Page order of a row in the Section split analysis of Reports › Evaluation Report on the document page: the section did not match with its pages in the expected order.',
  },
  expectedSectionColumn: {
    id: 'documents.evaluationReport.split.expectedSectionColumn',
    defaultMessage: 'Expected section',
    description:
      "Column header of the Section split analysis table in Reports › Evaluation Report on the document page: the ground truth's section id.",
  },
  expectedClassColumn: {
    id: 'documents.evaluationReport.split.expectedClassColumn',
    defaultMessage: 'Expected class',
    description:
      'Column header of the Section split analysis table in Reports › Evaluation Report on the document page: the class the ground truth gives the section.',
  },
  expectedPagesColumn: {
    id: 'documents.evaluationReport.split.expectedPagesColumn',
    defaultMessage: 'Expected pages',
    description:
      'Column header of the Section split analysis table in Reports › Evaluation Report on the document page: the pages the ground truth puts in the section.',
  },
  predictedClassColumn: {
    id: 'documents.evaluationReport.split.predictedClassColumn',
    defaultMessage: 'Predicted class',
    description:
      'Column header of the Section split analysis table in Reports › Evaluation Report on the document page: the class the processing gave the section.',
  },
  predictedSection: {
    id: 'documents.evaluationReport.split.predictedSection',
    defaultMessage: 'predicted section {sectionId}',
    description:
      "Note under the predicted class in the Section split analysis of Reports › Evaluation Report on the document page, when the row's predicted section has another id than its expected section, or no expected section. {sectionId} is the predicted section's id.",
  },
  predictedPagesColumn: {
    id: 'documents.evaluationReport.split.predictedPagesColumn',
    defaultMessage: 'Predicted pages',
    description:
      'Column header of the Section split analysis table in Reports › Evaluation Report on the document page: the pages the processing put in the section.',
  },
  splitEmpty: {
    id: 'documents.evaluationReport.split.empty',
    defaultMessage: 'No section data available',
    description: 'Shown in the Section split analysis table of Reports › Evaluation Report on the document page when it has no sections.',
  },
  gradedScore: {
    id: 'documents.evaluationReport.split.gradedScore',
    defaultMessage: 'Graded packet score {score}',
    description:
      'First part of the line under the Section split analysis table in Reports › Evaluation Report on the document page; the parts after it are joined with a middle dot. {score} is a percentage. Graded packet score is a metric explained under How scores are computed.',
  },
  clustering: {
    id: 'documents.evaluationReport.split.clustering',
    defaultMessage: 'clustering {score}',
    description:
      'Part of the graded packet score line under the Section split analysis table in Reports › Evaluation Report on the document page: the clustering part of that score. {score} is a percentage.',
  },
  ordering: {
    id: 'documents.evaluationReport.split.ordering',
    defaultMessage: 'ordering {score}',
    description:
      'Part of the graded packet score line under the Section split analysis table in Reports › Evaluation Report on the document page: the page-ordering part of that score. {score} is a percentage.',
  },
  vMeasure: {
    id: 'documents.evaluationReport.split.vMeasure',
    defaultMessage: 'V-measure {score}',
    description:
      'Part of the graded packet score line under the Section split analysis table in Reports › Evaluation Report on the document page. V-measure is the name of a clustering metric; {score} is a percentage.',
  },
  randIndex: {
    id: 'documents.evaluationReport.split.randIndex',
    defaultMessage: 'Rand index {score}',
    description:
      'Part of the graded packet score line under the Section split analysis table in Reports › Evaluation Report on the document page. Rand index is the name of a clustering metric; {score} is a percentage.',
  },
  splitErrors: {
    id: 'documents.evaluationReport.split.errors',
    defaultMessage: 'Doc split errors',
    description:
      'Heading of the errors the document split evaluation reported, in the Section split analysis of Reports › Evaluation Report on the document page. The errors stay as the evaluation writes them.',
  },
  showNested: {
    id: 'documents.evaluationReport.fields.showNested',
    defaultMessage: 'Show nested comparisons for {name}',
    description:
      "Accessible label of the button that opens a field's row in a field table of Reports › Evaluation Report on the document page, showing the comparisons nested under the field. {name} is the field's name.",
  },
  hideNested: {
    id: 'documents.evaluationReport.fields.hideNested',
    defaultMessage: 'Hide nested comparisons for {name}',
    description:
      "Accessible label of the button that closes a field's open row in a field table of Reports › Evaluation Report on the document page, hiding the comparisons nested under the field. {name} is the field's name.",
  },
  match: {
    id: 'documents.evaluationReport.fields.match',
    defaultMessage: 'match',
    description:
      'Badge on a row of a field table in Reports › Evaluation Report on the document page: the extracted value matched the expected one.',
  },
  mismatch: {
    id: 'documents.evaluationReport.fields.mismatch',
    defaultMessage: 'mismatch',
    description:
      'Badge on a row of a field table in Reports › Evaluation Report on the document page: the extracted value did not match the expected one.',
  },
  fieldColumn: {
    id: 'documents.evaluationReport.fields.fieldColumn',
    defaultMessage: 'Field',
    description: "Column header of a section's field table in Reports › Evaluation Report on the document page: the field's name.",
  },
  pairedWith: {
    id: 'documents.evaluationReport.fields.pairedWith',
    defaultMessage: 'paired with {path}',
    description:
      'Note under the name of a nested comparison in a field table of Reports › Evaluation Report on the document page, when the extracted value it was compared with is at another path, as when list items were matched out of order. {path} is that path, relative to the field.',
  },
  expectedColumn: {
    id: 'documents.evaluationReport.fields.expectedColumn',
    defaultMessage: 'Expected',
    description:
      "Column header of a section's field table in Reports › Evaluation Report on the document page: the field's expected value.",
  },
  extractedColumn: {
    id: 'documents.evaluationReport.fields.extractedColumn',
    defaultMessage: 'Extracted',
    description:
      "Column header of a section's field table in Reports › Evaluation Report on the document page: the value extraction produced.",
  },
  confidenceColumn: {
    id: 'documents.evaluationReport.fields.confidenceColumn',
    defaultMessage: 'Confidence',
    description:
      "Column header of a section's field table in Reports › Evaluation Report on the document page: the confidence of the extracted value, with its threshold when there is one.",
  },
  scoreColumn: {
    id: 'documents.evaluationReport.fields.scoreColumn',
    defaultMessage: 'Score',
    description: "Column header of a section's field table in Reports › Evaluation Report on the document page: the comparison's score.",
  },
  aggregateScore: {
    id: 'documents.evaluationReport.fields.aggregateScore',
    defaultMessage: '{score} (aggregate)',
    description:
      'Score of a field whose score is computed over the comparisons nested under it, in a field table of Reports › Evaluation Report on the document page. {score} is the score, with three decimals.',
  },
  weightColumn: {
    id: 'documents.evaluationReport.fields.weightColumn',
    defaultMessage: 'Weight',
    description:
      "Column header of a section's field table in Reports › Evaluation Report on the document page: the field's weight in the weighted score.",
  },
  methodColumn: {
    id: 'documents.evaluationReport.fields.methodColumn',
    defaultMessage: 'Method',
    description:
      "Column header of a section's field table in Reports › Evaluation Report on the document page: the comparison method used, named as the evaluation names it.",
  },
  compare: {
    id: 'documents.evaluationReport.fields.compare',
    defaultMessage: 'compare',
    description:
      'Shown in the Method column of a field table in Reports › Evaluation Report on the document page for a comparison that has a reason but no recorded method; choosing it shows the reason.',
  },
  fieldsEmpty: {
    id: 'documents.evaluationReport.fields.empty',
    defaultMessage: 'No fields',
    description: "Shown in a section's field table in Reports › Evaluation Report on the document page when the section has no fields.",
  },
  loading: {
    id: 'documents.evaluationReport.loading',
    defaultMessage: 'Loading evaluation…',
    description: "Shown in Reports › Evaluation Report on the document page while the document's evaluation results load.",
  },
  backToSummary: {
    id: 'documents.evaluationReport.backToSummary',
    defaultMessage: 'Back to evaluation summary',
    description:
      'Button above the evaluation report shown as markdown in Reports › Evaluation Report on the document page, after Markdown report was chosen: goes back to the scores and tables.',
  },
  markdownEmpty: {
    id: 'documents.evaluationReport.markdownEmpty',
    defaultMessage: 'Evaluation report not available for this document',
    description: 'Shown in Reports › Evaluation Report on the document page when the document has no evaluation report.',
  },
  mismatchesOnly: {
    id: 'documents.evaluationReport.mismatchesOnly',
    defaultMessage: 'Mismatches only',
    description:
      "Toggle in the header of Reports › Evaluation Report on the document page: when it is on, each section's field table lists only the fields that did not match.",
  },
  markdownReport: {
    id: 'documents.evaluationReport.markdownReport',
    defaultMessage: 'Markdown report',
    description:
      'Button in the header of Reports › Evaluation Report on the document page: shows the evaluation report as generated, a Markdown file, with its own download and print buttons.',
  },
  title: {
    id: 'documents.evaluationReport.title',
    defaultMessage: 'Evaluation',
    description: "Heading of the document's evaluation results in Reports › Evaluation Report on the document page.",
  },
  documentNotScored: {
    id: 'documents.evaluationReport.documentNotScored.header',
    defaultMessage: 'This document was not scored',
    description: 'Heading of a notice in Reports › Evaluation Report on the document page when no section of the document could be scored.',
  },
  documentNotScoredBody: {
    id: 'documents.evaluationReport.documentNotScored.body',
    defaultMessage: 'No section had an extractable schema, so accuracy figures would be meaningless and are omitted.',
    description:
      'Text of the notice in Reports › Evaluation Report on the document page when no section of the document could be scored, and the evaluation recorded no reason.',
  },
  documentNotScoredBodyWithReason: {
    id: 'documents.evaluationReport.documentNotScored.bodyWithReason',
    defaultMessage: 'No section had an extractable schema ({reason}), so accuracy figures would be meaningless and are omitted.',
    description:
      'Text of the notice in Reports › Evaluation Report on the document page when no section of the document could be scored. {reason} is the reason the evaluation recorded, as it writes it.',
  },
  extractionTile: {
    id: 'documents.evaluationReport.tile.extraction',
    defaultMessage: 'Extraction accuracy',
    description:
      'Label of a headline score in Reports › Evaluation Report on the document page: how well the extracted fields match the expected values.',
  },
  extractionHintWeighted: {
    id: 'documents.evaluationReport.tile.extractionHintWeighted',
    defaultMessage: 'Weighted by field importance · {matched, number} of {total, plural, one {# field} other {# fields}} matched',
    description:
      "Note under the Extraction accuracy score in Reports › Evaluation Report on the document page when the score is weighted by the fields' weights. {matched} is how many fields matched; {total} is how many fields were compared.",
  },
  extractionHint: {
    id: 'documents.evaluationReport.tile.extractionHint',
    defaultMessage: '{matched, number} of {total, plural, one {# field} other {# fields}} matched',
    description:
      'Note under the Extraction accuracy score in Reports › Evaluation Report on the document page when the score is the share of fields that matched. {matched} is how many fields matched; {total} is how many fields were compared.',
  },
  classificationTile: {
    id: 'documents.evaluationReport.tile.classification',
    defaultMessage: 'Classification accuracy',
    description:
      'Label of a headline score in Reports › Evaluation Report on the document page: the share of pages classified as the ground truth classifies them.',
  },
  classificationHint: {
    id: 'documents.evaluationReport.tile.classificationHint',
    defaultMessage: '{correct, number} of {total, plural, one {# page} other {# pages}}',
    description:
      'Note under the Classification accuracy score in Reports › Evaluation Report on the document page. {correct} is how many pages were classified as the ground truth classifies them; {total} is how many pages were compared.',
  },
  splitTile: {
    id: 'documents.evaluationReport.tile.split',
    defaultMessage: 'Split accuracy',
    description:
      'Label of a headline score in Reports › Evaluation Report on the document page: the share of expected sections that a predicted section matches, pages and class, in any page order.',
  },
  splitHint: {
    id: 'documents.evaluationReport.tile.splitHint',
    defaultMessage: '{correct, number} of {total, plural, one {# section} other {# sections}} · in order {score}',
    description:
      'Note under the Split accuracy score in Reports › Evaluation Report on the document page. {correct} is how many expected sections were matched; {total} is how many sections were compared; {score} is the stricter score that also requires the page order to match, a percentage.',
  },
  f1Tile: {
    id: 'documents.evaluationReport.tile.f1',
    defaultMessage: 'F1 score',
    description: 'Label of a headline score in Reports › Evaluation Report on the document page. F1 score is the name of a metric.',
  },
  f1Hint: {
    id: 'documents.evaluationReport.tile.f1Hint',
    defaultMessage: 'Precision {precision} · Recall {recall}',
    description:
      'Note under the F1 score in Reports › Evaluation Report on the document page. Precision and Recall are the names of the two metrics the F1 score combines; {precision} and {recall} are percentages.',
  },
  misclassifiedHeader: {
    id: 'documents.evaluationReport.misclassified.header',
    defaultMessage: 'Some pages are not the class ground truth expects',
    description:
      'Heading of a warning in Reports › Evaluation Report on the document page when pages were classified otherwise than the ground truth classifies them.',
  },
  misclassifiedPages: {
    id: 'documents.evaluationReport.misclassified.pages',
    defaultMessage:
      '{count, plural, one {Page} other {Pages}} <b>{pages}</b> classified as <b>{predicted}</b>, but ground truth says <b>{expected}</b>.',
    description:
      'Line of the warning in Reports › Evaluation Report on the document page about pages classified otherwise than the ground truth classifies them. {count} is how many pages the line lists; {pages} lists their numbers, as ranges such as 2-3; {predicted} is the class they were given; {expected} is the class the ground truth gives them. <b> makes a part bold.',
  },
  misclassifiedHint: {
    id: 'documents.evaluationReport.misclassified.hint',
    defaultMessage:
      "Extraction runs against the assigned class's schema, so the fields for those pages may be wrong even where they look plausible. Correct the class and re-extract before reading the numbers here.",
    description:
      'Note at the end of the warning in Reports › Evaluation Report on the document page about pages classified otherwise than the ground truth classifies them.',
  },
  excludedHeader: {
    id: 'documents.evaluationReport.excluded.header',
    defaultMessage: '{count, plural, one {# section not evaluated} other {# sections not evaluated}}',
    description:
      'Heading of the sections the evaluation skipped, in Reports › Evaluation Report on the document page. {count} is how many sections it skipped.',
  },
  excludedDescription: {
    id: 'documents.evaluationReport.excluded.description',
    defaultMessage: 'Skipped by evaluation and not counted in the figures above',
    description:
      'Description under the heading of the sections the evaluation skipped, in Reports › Evaluation Report on the document page.',
  },
  excludedExplanation: {
    id: 'documents.evaluationReport.excluded.explanation',
    defaultMessage:
      'A section is excluded when its class is marked <code>{key}</code>, or when the class defines no extractable attributes in the evaluation schema.',
    description:
      'Explanation above the table of sections the evaluation skipped, in Reports › Evaluation Report on the document page. {key} is the schema setting that marks a class, x-aws-idp-exclude-from-processing, shown as code.',
  },
  excludedSectionColumn: {
    id: 'documents.evaluationReport.excluded.sectionColumn',
    defaultMessage: 'Section',
    description:
      "Column header of the table of sections the evaluation skipped, in Reports › Evaluation Report on the document page: the section's id.",
  },
  excludedClassColumn: {
    id: 'documents.evaluationReport.excluded.classColumn',
    defaultMessage: 'Classification',
    description:
      "Column header of the table of sections the evaluation skipped, in Reports › Evaluation Report on the document page: the section's class.",
  },
  excludedReasonColumn: {
    id: 'documents.evaluationReport.excluded.reasonColumn',
    defaultMessage: 'Exclusion reason',
    description:
      'Column header of the table of sections the evaluation skipped, in Reports › Evaluation Report on the document page: why the evaluation skipped the section, as it writes it.',
  },
  excludedPagesColumn: {
    id: 'documents.evaluationReport.excluded.pagesColumn',
    defaultMessage: 'Pages',
    description:
      "Column header of the table of sections the evaluation skipped, in Reports › Evaluation Report on the document page: the section's pages.",
  },
  splitAnalysisHeader: {
    id: 'documents.evaluationReport.splitAnalysis.header',
    defaultMessage: 'Section split analysis',
    description:
      'Heading of the table in Reports › Evaluation Report on the document page that pairs each section the ground truth expects with the section the processing predicted.',
  },
  splitUnmatched: {
    id: 'documents.evaluationReport.splitAnalysis.unmatched',
    defaultMessage: '{unmatched} of {expected} unmatched',
    description:
      'Count shown in parentheses beside the Section split analysis heading in Reports › Evaluation Report on the document page. {unmatched} is how many expected sections no predicted section matched; {expected} is how many sections the ground truth expects.',
  },
  splitUnexpected: {
    id: 'documents.evaluationReport.splitAnalysis.unexpected',
    defaultMessage: '{count, plural, one {# unexpected} other {# unexpected}}',
    description:
      'Count shown after the unmatched count beside the Section split analysis heading in Reports › Evaluation Report on the document page, joined with a middle dot. {count} is how many predicted sections match no expected section.',
  },
  allMetricsHeader: {
    id: 'documents.evaluationReport.allMetrics.header',
    defaultMessage: 'All metrics',
    description: 'Heading of the metrics tables of the whole document in Reports › Evaluation Report on the document page.',
  },
  allMetricsDescription: {
    id: 'documents.evaluationReport.allMetrics.description',
    defaultMessage: 'Every figure the run recorded, with the rating the report applies to it',
    description: 'Description under the All metrics heading in Reports › Evaluation Report on the document page.',
  },
  splitMetricsHeading: {
    id: 'documents.evaluationReport.allMetrics.split',
    defaultMessage: 'Document split classification',
    description:
      "Heading of the metrics table of the document's split and classification, under All metrics in Reports › Evaluation Report on the document page.",
  },
  extractionMetricsHeading: {
    id: 'documents.evaluationReport.allMetrics.extraction',
    defaultMessage: 'Document extraction',
    description:
      "Heading of the metrics table of the document's extraction, under All metrics in Reports › Evaluation Report on the document page.",
  },
  extractionMetricsEmpty: {
    id: 'documents.evaluationReport.allMetrics.extractionEmpty',
    defaultMessage: 'No extraction metrics recorded',
    description:
      "Shown in the metrics table of the document's extraction, under All metrics in Reports › Evaluation Report on the document page, when the evaluation recorded none.",
  },
  sectionNotScored: {
    id: 'documents.evaluationReport.section.notScored',
    defaultMessage: 'not scored',
    description:
      "Shown in parentheses beside a section's heading in Reports › Evaluation Report on the document page when the section was not evaluated.",
  },
  sectionMismatched: {
    id: 'documents.evaluationReport.section.mismatched',
    defaultMessage: '{count} mismatched',
    description:
      "Shown in parentheses beside a section's heading in Reports › Evaluation Report on the document page, after the section's score and a middle dot. {count} is how many of its fields did not match.",
  },
  sectionHeader: {
    id: 'documents.evaluationReport.section.header',
    defaultMessage: 'Section {sectionId} — {className}',
    description:
      "Heading of a section's results in Reports › Evaluation Report on the document page. {sectionId} is the section's id; {className} is its document class.",
  },
  sectionHeaderUnknownClass: {
    id: 'documents.evaluationReport.section.headerUnknownClass',
    defaultMessage: 'Section {sectionId} — unknown class',
    description:
      "Heading of a section's results in Reports › Evaluation Report on the document page when the evaluation recorded no class for it. {sectionId} is the section's id.",
  },
  sectionNotEvaluated: {
    id: 'documents.evaluationReport.section.notEvaluated',
    defaultMessage: 'This section was not evaluated',
    description:
      "Heading of an error in a section's results in Reports › Evaluation Report on the document page when the section could not be evaluated.",
  },
  couldNotEvaluate: {
    id: 'documents.evaluationReport.section.couldNotEvaluate',
    defaultMessage: 'This section could not be evaluated for document class {className}.',
    description:
      "Text of the error in a section's results in Reports › Evaluation Report on the document page when the section could not be evaluated and the evaluation recorded no reason. {className} is the section's document class.",
  },
  couldNotEvaluateUnknownClass: {
    id: 'documents.evaluationReport.section.couldNotEvaluateUnknownClass',
    defaultMessage: 'This section could not be evaluated for document class unknown.',
    description:
      "Text of the error in a section's results in Reports › Evaluation Report on the document page when the section could not be evaluated and the evaluation recorded neither a reason nor a class.",
  },
  placeholders: {
    id: 'documents.evaluationReport.section.placeholders',
    defaultMessage:
      'It contributes zero to every document-level figure above. Its metrics below are placeholders for a section that was never scored, not measurements of a bad extraction.',
    description:
      "Note in the error in a section's results in Reports › Evaluation Report on the document page when the section could not be evaluated.",
  },
  howToFix: {
    id: 'documents.evaluationReport.section.howToFix',
    defaultMessage: 'How to fix',
    description:
      "Heading of the steps that fix a section's evaluation, in the error in a section's results in Reports › Evaluation Report on the document page when the section could not be evaluated.",
  },
  skippedFields: {
    id: 'documents.evaluationReport.section.skippedFields',
    defaultMessage:
      '{count, plural, one {# field was excluded from scoring because it could not be validated against the schema. The remaining fields were evaluated normally.} other {# fields were excluded from scoring because they could not be validated against the schema. The remaining fields were evaluated normally.}}',
    description:
      "Warning in a section's results in Reports › Evaluation Report on the document page. {count} is how many of its fields the evaluation left out because their values did not fit the class's schema.",
  },
  sectionMetrics: {
    id: 'documents.evaluationReport.section.metrics',
    defaultMessage: 'Section metrics',
    description: "Heading of a section's metrics table in Reports › Evaluation Report on the document page.",
  },
  allMatched: {
    id: 'documents.evaluationReport.section.allMatched',
    defaultMessage: 'Every field in this section matched.',
    description:
      "Shown in place of a section's field table in Reports › Evaluation Report on the document page when Mismatches only is on and every field of the section matched.",
  },
  methodsHeader: {
    id: 'documents.evaluationReport.methods.header',
    defaultMessage: 'Comparison methods used',
    description:
      "Heading of the list of comparison methods the document's evaluation used, in Reports › Evaluation Report on the document page.",
  },
  methodsBody: {
    id: 'documents.evaluationReport.methods.body',
    defaultMessage:
      'A surprising score is often a comparison-method question rather than an extraction one. This document used: {methods}.',
    description:
      "Text under Comparison methods used in Reports › Evaluation Report on the document page. {methods} lists the comparison methods the document's evaluation used, named as the evaluation names them and separated by commas.",
  },
  howScores: {
    id: 'documents.evaluationReport.howScores',
    defaultMessage: 'How scores are computed',
    description:
      'Heading of the explanation of the comparison methods and the scores, in Reports › Evaluation Report on the document page.',
  },
  executionTime: {
    id: 'documents.evaluationReport.executionTime',
    defaultMessage: 'Evaluation took {executionTime}.',
    description:
      "Last line of Reports › Evaluation Report on the document page. {executionTime} is how long the document's evaluation ran, such as 12.3 s or 2 min 5 s.",
  },
});

// How scores are computed: whole items, each naming its comparison method or metric in bold.
const helpMessages = defineMessages<{
  fieldMethods: NoMessageValues;
  exact: { readonly method: string; readonly b: MessageTag };
  numericExact: { readonly method: string; readonly key: string; readonly b: MessageTag; readonly code: MessageTag };
  fuzzy: { readonly method: string; readonly other: string; readonly b: MessageTag };
  semantic: { readonly method: string; readonly b: MessageTag };
  date: { readonly method: string; readonly key: string; readonly b: MessageTag; readonly code: MessageTag };
  llm: { readonly method: string; readonly key: string; readonly b: MessageTag; readonly code: MessageTag };
  arrayMatching: NoMessageValues;
  hungarian: { readonly method: string; readonly key: string; readonly b: MessageTag; readonly code: MessageTag };
  llmArrays: { readonly method: string; readonly b: MessageTag };
  weighting: NoMessageValues;
  weightingBody: { readonly key: string; readonly code: MessageTag };
  splitMetrics: NoMessageValues;
  pageLevel: { readonly b: MessageTag };
  splitWithoutOrder: { readonly b: MessageTag };
  splitWithOrder: { readonly b: MessageTag };
  graded: { readonly b: MessageTag };
}>({
  fieldMethods: {
    id: 'documents.evaluationReport.help.fieldMethods',
    defaultMessage: 'Field-level comparison methods',
    description: 'Heading under How scores are computed in Reports › Evaluation Report on the document page.',
  },
  exact: {
    id: 'documents.evaluationReport.help.exact',
    defaultMessage: '<b>{method}</b> — character-for-character match. IDs, codes.',
    description:
      'Explains a comparison method under How scores are computed in Reports › Evaluation Report on the document page. {method} is its name as the evaluation writes it, Exact, in bold.',
  },
  numericExact: {
    id: 'documents.evaluationReport.help.numericExact',
    defaultMessage: '<b>{method}</b> — numeric comparison with a tolerance from <code>{key}</code>. Amounts, percentages.',
    description:
      'Explains a comparison method under How scores are computed in Reports › Evaluation Report on the document page. {method} is its name as the evaluation writes it, NumericExact, in bold; {key} is the schema setting that holds the tolerance, x-aws-idp-evaluation-threshold, shown as code.',
  },
  fuzzy: {
    id: 'documents.evaluationReport.help.fuzzy',
    defaultMessage: '<b>{method}</b> / <b>{other}</b> — string similarity against a threshold. Names, addresses, minor variations.',
    description:
      'Explains two comparison methods under How scores are computed in Reports › Evaluation Report on the document page. {method} and {other} are their names as the evaluation writes them, Fuzzy and Levenshtein, in bold.',
  },
  semantic: {
    id: 'documents.evaluationReport.help.semantic',
    defaultMessage: '<b>{method}</b> — embedding similarity against a threshold. Text where meaning matters more than wording.',
    description:
      'Explains a comparison method under How scores are computed in Reports › Evaluation Report on the document page. {method} is its name as the evaluation writes it, Semantic, in bold.',
  },
  date: {
    id: 'documents.evaluationReport.help.date',
    defaultMessage:
      '<b>{method}</b> — compares resolved dates, not surface form (<code>2024-01-05</code> equals <code>January 5, 2024</code>); ranges and day-first via <code>{key}</code>.',
    description:
      'Explains a comparison method under How scores are computed in Reports › Evaluation Report on the document page. {method} is its name as the evaluation writes it, Date, in bold. The two dates in <code> are examples of one date written two ways, which a translation may write as its language writes dates. {key} is the schema setting, x-aws-idp-evaluation-method-config, shown as code.',
  },
  llm: {
    id: 'documents.evaluationReport.help.llm',
    defaultMessage:
      '<b>{method}</b> — a Bedrock model judges the pair with reasoning, configured under <code>{key}</code>. Nested objects, semantic equivalence.',
    description:
      'Explains a comparison method under How scores are computed in Reports › Evaluation Report on the document page. {method} is its name as the evaluation writes it, LLM, in bold. Bedrock is the name of an AWS service. {key} is the configuration setting, evaluation.llm_method, shown as code.',
  },
  arrayMatching: {
    id: 'documents.evaluationReport.help.arrayMatching',
    defaultMessage: 'Array-level matching',
    description: 'Heading under How scores are computed in Reports › Evaluation Report on the document page: how lists are compared.',
  },
  hungarian: {
    id: 'documents.evaluationReport.help.hungarian',
    defaultMessage:
      '<b>{method}</b> — optimal one-to-one pairing between the expected and extracted lists; each pair is then compared field by field, which is what the expandable rows above show. Configured with <code>{key}</code> on the array.',
    description:
      'Explains a list matching method under How scores are computed in Reports › Evaluation Report on the document page. {method} is its name as the evaluation writes it, Hungarian, in bold; {key} is the schema setting that selects it, x-aws-idp-evaluation-method: "HUNGARIAN", shown as code.',
  },
  llmArrays: {
    id: 'documents.evaluationReport.help.llmArrays',
    defaultMessage: '<b>{method} for arrays</b> — judges whether two lists match as a whole.',
    description:
      'Explains a list matching method under How scores are computed in Reports › Evaluation Report on the document page. {method} is the name of the comparison method as the evaluation writes it, LLM; the bold part names the method used on lists.',
  },
  weighting: {
    id: 'documents.evaluationReport.help.weighting',
    defaultMessage: 'Field weighting',
    description: 'Heading under How scores are computed in Reports › Evaluation Report on the document page: how field weights count.',
  },
  weightingBody: {
    id: 'documents.evaluationReport.help.weightingBody',
    defaultMessage:
      "<code>{key}</code> sets a field's importance (default 1.0). The weighted overall score is Σ(weight × score) / Σ(weight) per section, and the document score is the average across sections. The weight column above shows each field's value.",
    description:
      "Text under Field weighting in How scores are computed of Reports › Evaluation Report on the document page. {key} is the schema setting, x-aws-idp-evaluation-weight, shown as code. The formula gives a section's weighted score.",
  },
  splitMetrics: {
    id: 'documents.evaluationReport.help.splitMetrics',
    defaultMessage: 'Split metrics',
    description:
      "Heading under How scores are computed in Reports › Evaluation Report on the document page, shown when the document was evaluated against expected sections: the metrics of the document's split into sections.",
  },
  pageLevel: {
    id: 'documents.evaluationReport.help.pageLevel',
    defaultMessage: "<b>Page-level accuracy</b> — each page's predicted class against its expected class, ignoring how pages were grouped.",
    description:
      "Explains a split metric under How scores are computed in Reports › Evaluation Report on the document page. The bold part is the metric's name.",
  },
  splitWithoutOrder: {
    id: 'documents.evaluationReport.help.splitWithoutOrder',
    defaultMessage:
      '<b>Split accuracy (without order)</b> — an expected section counts as correct when a predicted section has the same set of pages and the same class.',
    description:
      "Explains a split metric under How scores are computed in Reports › Evaluation Report on the document page. The bold part is the metric's name.",
  },
  splitWithOrder: {
    id: 'documents.evaluationReport.help.splitWithOrder',
    defaultMessage: '<b>Split accuracy (with order)</b> — the same, and the page order must match exactly. The strictest of the three.',
    description:
      "Explains a split metric under How scores are computed in Reports › Evaluation Report on the document page, after the one without order. The bold part is the metric's name.",
  },
  graded: {
    id: 'documents.evaluationReport.help.graded',
    defaultMessage:
      '<b>Graded packet score</b> — a softer companion to the exact-match figures: <b>clustering</b> (V-measure and Rand index) rewards pages grouped together correctly even when a section is not reproduced exactly, and <b>ordering</b> rewards pages kept in sequence within their section.',
    description:
      "Explains a split metric under How scores are computed in Reports › Evaluation Report on the document page. The bold parts are the metric's name and the names of its two parts; V-measure and Rand index are names of clustering metrics.",
  },
});

/** Renders a `<b>...</b>` element value. */
const bold = (chunks: React.ReactNode[]): React.JSX.Element => <b>{chunks}</b>;
/** Renders a `<code>...</code>` element value. */
const code = (chunks: React.ReactNode[]): React.JSX.Element => <code>{chunks}</code>;

/** A band's rating word; nothing for the `unknown` band (see `bandMessages`). */
const BandLabel = ({ band }: { band: ScoreBand }): React.JSX.Element | null =>
  band === 'unknown' ? null : <FormattedMessage {...bandMessages[band]} />;

/** One of the headline figures. Omitted entirely when there is no value. */
const ScoreTile = ({ label, score, hint }: { label: string; score: number | null; hint?: string }): React.JSX.Element | null => {
  const { locale } = useFormatters();
  if (score === null) return null;
  const band = scoreBand(score);
  return (
    <div>
      <Box variant="awsui-key-label">{label}</Box>
      <SpaceBetween direction="horizontal" size="xs" alignItems="center">
        <Box variant="h1" padding={{ top: 'n' }}>
          {formatScore(score, locale)}
        </Box>
        <Badge color={BAND_COLOUR[band]}>
          <BandLabel band={band} />
        </Badge>
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
  const intl = useIntl();
  if (Array.isArray(value)) {
    if (value.length === 0)
      return (
        <Box color="text-body-secondary">
          <FormattedMessage {...messages.emptyList} />
        </Box>
      );
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
    if (entries.length === 0)
      return (
        <Box color="text-body-secondary">
          <FormattedMessage {...messages.emptyObject} />
        </Box>
      );
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
  const shape = describeValue(value, intl);
  return shape.kind === 'empty' ? <Box color="text-body-secondary">—</Box> : <span>{shape.summary}</span>;
};

/** A table cell for a value of any shape: scalars inline, structures on demand. */
const ValueCell = ({ value }: { value: unknown }): React.JSX.Element => {
  const intl = useIntl();
  const shape = describeValue(value, intl);
  if (shape.kind === 'scalar' || shape.kind === 'empty') return <span>{shape.summary}</span>;
  return (
    <Popover
      dismissButton
      position="bottom"
      size="large"
      triggerType="text"
      header={intl.formatMessage(shape.kind === 'list' ? messages.listValue : messages.objectValue)}
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

/** The markdown's "| Metric | Value | Rating |" table. */
const MetricTable = ({
  rows,
  emptyText,
  notScored = false,
}: {
  rows: MetricRow[];
  /** Shown when there are no rows; a table drawn only when it has rows needs none. */
  emptyText?: string;
  /** The section was never evaluated: its zeros are placeholders, not measurements. */
  notScored?: boolean;
}): React.JSX.Element => {
  const intl = useIntl();
  const { formatFixed } = useFormatters();
  return (
    <Table
      variant="embedded"
      contentDensity="compact"
      items={rows}
      trackBy="metric"
      columnDefinitions={[
        { id: 'metric', header: intl.formatMessage(messages.metricColumn), cell: (row: MetricRow) => row.metric },
        {
          id: 'value',
          header: intl.formatMessage(messages.valueColumn),
          cell: (row: MetricRow) => (row.value === null ? intl.formatMessage(messages.notScored) : formatFixed(row.value, 4)),
        },
        {
          id: 'rating',
          header: intl.formatMessage(messages.ratingColumn),
          cell: (row: MetricRow) =>
            notScored ? (
              <StatusIndicator type="stopped">
                <FormattedMessage {...messages.notScored} />
              </StatusIndicator>
            ) : row.value === null ? (
              <StatusIndicator type="stopped">
                <FormattedMessage {...messages.excludedRating} />
              </StatusIndicator>
            ) : row.band ? (
              <StatusIndicator type={bandIndicator[row.band]}>
                <BandLabel band={row.band} />
              </StatusIndicator>
            ) : (
              '—'
            ),
        },
      ]}
      empty={<Box textAlign="center">{emptyText}</Box>}
    />
  );
};

/** The markdown's "Section Split Analysis" table, plus its error list. */
const SplitAnalysisSection = ({ split }: { split: SplitAnalysis }): React.JSX.Element => {
  const intl = useIntl();
  const { locale } = useFormatters();
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
            header: intl.formatMessage(messages.sectionMatchColumn),
            cell: (row: SplitRow) =>
              verdict(row.sectionMatched, intl.formatMessage(row.sectionMatched ? messages.matched : messages.noMatch)),
            width: 150,
          },
          {
            id: 'order',
            header: intl.formatMessage(messages.pageOrderColumn),
            cell: (row: SplitRow) =>
              verdict(row.orderMatched, intl.formatMessage(row.orderMatched ? messages.ordered : messages.reordered)),
            width: 150,
          },
          { id: 'id', header: intl.formatMessage(messages.expectedSectionColumn), cell: (row: SplitRow) => row.sectionId ?? '—' },
          {
            id: 'expectedClass',
            header: intl.formatMessage(messages.expectedClassColumn),
            cell: (row: SplitRow) => row.expectedClass ?? '—',
          },
          {
            id: 'expectedPages',
            header: intl.formatMessage(messages.expectedPagesColumn),
            cell: (row: SplitRow) => formatPageRanges(row.expectedPages),
          },
          {
            id: 'predictedClass',
            header: intl.formatMessage(messages.predictedClassColumn),
            cell: (row: SplitRow) => (
              <span>
                {row.predictedClass ?? '—'}
                {/* Which predicted section this is, when that is not obvious: a
                    pairing with a differently-numbered section, or a prediction
                    nothing expected. */}
                {row.matchedSectionId && row.matchedSectionId !== row.sectionId && (
                  <Box variant="small" color="text-body-secondary" display="block">
                    <FormattedMessage {...messages.predictedSection} values={{ sectionId: row.matchedSectionId }} />
                  </Box>
                )}
              </span>
            ),
          },
          {
            id: 'predictedPages',
            header: intl.formatMessage(messages.predictedPagesColumn),
            cell: (row: SplitRow) => formatPageRanges(row.predictedPages),
          },
        ]}
        empty={
          <Box textAlign="center">
            <FormattedMessage {...messages.splitEmpty} />
          </Box>
        }
      />
      {split.graded && (
        <Box variant="small" color="text-body-secondary">
          {[
            intl.formatMessage(messages.gradedScore, { score: formatScore(split.graded.finalScore, locale) }),
            split.graded.clusteringScore !== null &&
              intl.formatMessage(messages.clustering, { score: formatScore(split.graded.clusteringScore, locale) }),
            split.graded.orderingScore !== null &&
              intl.formatMessage(messages.ordering, { score: formatScore(split.graded.orderingScore, locale) }),
            split.graded.vMeasure !== null && intl.formatMessage(messages.vMeasure, { score: formatScore(split.graded.vMeasure, locale) }),
            split.graded.randIndex !== null &&
              intl.formatMessage(messages.randIndex, { score: formatScore(split.graded.randIndex, locale) }),
          ]
            .filter(Boolean)
            .join(' · ')}
        </Box>
      )}
      {split.errors.length > 0 && (
        <Alert type="warning" header={intl.formatMessage(messages.splitErrors)}>
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
  const intl = useIntl();
  const { formatFixed } = useFormatters();
  const [expanded, setExpanded] = useState<ComparisonRow[]>([]);
  // Sorting is applied here rather than through useCollection: the rows carry
  // their children, and only the top level should reorder.
  const [sorting, setSorting] = useState<TableProps.SortingState<ComparisonRow> | null>(null);
  const sortedRows = useMemo(() => {
    if (!sorting?.sortingColumn.sortingField) return rows;
    const field = sorting.sortingColumn.sortingField as 'name' | 'score';
    const dir = sorting.isDescending ? -1 : 1;
    return [...rows].sort((a, b) => {
      if (field === 'score') return ((a.score ?? -1) - (b.score ?? -1)) * dir;
      return a.name.localeCompare(b.name) * dir;
    });
  }, [rows, sorting]);
  return (
    <Table
      resizableColumns
      variant="embedded"
      contentDensity="compact"
      wrapLines
      items={sortedRows}
      trackBy="key"
      sortingColumn={sorting?.sortingColumn}
      sortingDescending={sorting?.isDescending}
      onSortingChange={({ detail }) => setSorting(detail)}
      ariaLabels={{
        expandButtonLabel: (row: ComparisonRow) => intl.formatMessage(messages.showNested, { name: row.name }),
        collapseButtonLabel: (row: ComparisonRow) => intl.formatMessage(messages.hideNested, { name: row.name }),
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
          cell: (row: ComparisonRow) => (
            <Badge color={row.matched ? 'green' : 'red'}>
              <FormattedMessage {...(row.matched ? messages.match : messages.mismatch)} />
            </Badge>
          ),
          width: 150,
        },
        {
          id: 'name',
          header: intl.formatMessage(messages.fieldColumn),
          cell: (row: ComparisonRow) => (
            <span>
              {row.name}
              {row.actualPath && (
                <Box variant="small" color="text-body-secondary">
                  <FormattedMessage {...messages.pairedWith} values={{ path: row.actualPath }} />
                </Box>
              )}
            </span>
          ),
          sortingField: 'name',
        },
        {
          id: 'expected',
          header: intl.formatMessage(messages.expectedColumn),
          cell: (row: ComparisonRow) => <ValueCell value={row.expected} />,
        },
        {
          id: 'actual',
          header: intl.formatMessage(messages.extractedColumn),
          cell: (row: ComparisonRow) => <ValueCell value={row.actual} />,
        },
        {
          id: 'confidence',
          header: intl.formatMessage(messages.confidenceColumn),
          cell: (row: ComparisonRow) =>
            row.confidence === null || (describeValue(row.expected).kind === 'empty' && describeValue(row.actual).kind === 'empty')
              ? '—'
              : `${formatFixed(row.confidence, 2)}${row.confidenceThreshold !== null ? ` / ${formatFixed(row.confidenceThreshold, 2)}` : ''}`,
          width: 125,
        },
        {
          id: 'score',
          header: intl.formatMessage(messages.scoreColumn),
          cell: (row: ComparisonRow) =>
            row.score === null
              ? '—'
              : row.children
                ? // An aggregate: the score is over the nested comparisons below it,
                  // not a comparison of two values, and the markdown says so too.
                  intl.formatMessage(messages.aggregateScore, { score: formatFixed(row.score, 3) })
                : formatFixed(row.score, 3),
          sortingField: 'score',
          width: 130,
        },
        {
          id: 'weight',
          header: intl.formatMessage(messages.weightColumn),
          cell: (row: ComparisonRow) => formatFixed(row.weight === null ? 1 : row.weight, 2),
          width: 90,
        },
        {
          id: 'method',
          header: intl.formatMessage(messages.methodColumn),
          cell: (row: ComparisonRow) =>
            row.reason ? (
              // The reason is why this scored as it did — the single most useful
              // thing on the row when a score is surprising.
              <Popover dismissButton={false} position="top" size="medium" triggerType="text" content={row.reason}>
                {row.method ?? intl.formatMessage(messages.compare)}
              </Popover>
            ) : (
              (row.method ?? '—')
            ),
        },
      ]}
      empty={
        <Box textAlign="center">
          <FormattedMessage {...messages.fieldsEmpty} />
        </Box>
      }
    />
  );
};

/** The comparison methods' names as the evaluation writes them: data, never translated. */
const comparisonMethods = {
  exact: 'Exact',
  numericExact: 'NumericExact',
  fuzzy: 'Fuzzy',
  levenshtein: 'Levenshtein',
  semantic: 'Semantic',
  date: 'Date',
  llm: 'LLM',
  hungarian: 'Hungarian',
} as const;

/**
 * The markdown's "Evaluation Methods Used", "Field Weighting" and "Metrics
 * Explanation" prose, condensed. Static by design: it explains the scoring
 * scheme, not this document, and the per-document part ("which methods were
 * used here") is listed separately above it.
 */
const HowScoresAreComputed = ({ hasSplit }: { hasSplit: boolean }): React.JSX.Element => (
  <SpaceBetween size="s">
    <Box variant="h4">
      <FormattedMessage {...helpMessages.fieldMethods} />
    </Box>
    <ul style={{ margin: 0 }}>
      <li>
        <FormattedMessage {...helpMessages.exact} values={{ method: comparisonMethods.exact, b: bold }} />
      </li>
      <li>
        <FormattedMessage
          {...helpMessages.numericExact}
          values={{ method: comparisonMethods.numericExact, key: 'x-aws-idp-evaluation-threshold', b: bold, code }}
        />
      </li>
      <li>
        <FormattedMessage
          {...helpMessages.fuzzy}
          values={{ method: comparisonMethods.fuzzy, other: comparisonMethods.levenshtein, b: bold }}
        />
      </li>
      <li>
        <FormattedMessage {...helpMessages.semantic} values={{ method: comparisonMethods.semantic, b: bold }} />
      </li>
      <li>
        <FormattedMessage
          {...helpMessages.date}
          values={{ method: comparisonMethods.date, key: 'x-aws-idp-evaluation-method-config', b: bold, code }}
        />
      </li>
      <li>
        <FormattedMessage {...helpMessages.llm} values={{ method: comparisonMethods.llm, key: 'evaluation.llm_method', b: bold, code }} />
      </li>
    </ul>
    <Box variant="h4">
      <FormattedMessage {...helpMessages.arrayMatching} />
    </Box>
    <ul style={{ margin: 0 }}>
      <li>
        <FormattedMessage
          {...helpMessages.hungarian}
          values={{ method: comparisonMethods.hungarian, key: 'x-aws-idp-evaluation-method: "HUNGARIAN"', b: bold, code }}
        />
      </li>
      <li>
        <FormattedMessage {...helpMessages.llmArrays} values={{ method: comparisonMethods.llm, b: bold }} />
      </li>
    </ul>
    <Box variant="h4">
      <FormattedMessage {...helpMessages.weighting} />
    </Box>
    <Box variant="p">
      <FormattedMessage {...helpMessages.weightingBody} values={{ key: 'x-aws-idp-evaluation-weight', code }} />
    </Box>
    {hasSplit && (
      <>
        <Box variant="h4">
          <FormattedMessage {...helpMessages.splitMetrics} />
        </Box>
        <ul style={{ margin: 0 }}>
          <li>
            <FormattedMessage {...helpMessages.pageLevel} values={{ b: bold }} />
          </li>
          <li>
            <FormattedMessage {...helpMessages.splitWithoutOrder} values={{ b: bold }} />
          </li>
          <li>
            <FormattedMessage {...helpMessages.splitWithOrder} values={{ b: bold }} />
          </li>
          <li>
            <FormattedMessage {...helpMessages.graded} values={{ b: bold }} />
          </li>
        </ul>
      </>
    )}
  </SpaceBetween>
);

const EvaluationReport = ({ reportUri, documentId }: EvaluationReportProps): React.JSX.Element => {
  const intl = useIntl();
  const { locale } = useFormatters();
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
  const excluded = useMemo(() => excludedSectionRows(results, intl), [results, intl]);
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
  const executionTime = formatDuration(results?.execution_time, locale);
  // Built once per change of input rather than inline in the render: rebuilding
  // every section's rows on each render also defeated AttributeTable's sort memo,
  // which keys on row identity.
  const rowsBySection = useMemo(() => {
    const map = new Map<string, ComparisonRow[]>();
    for (const section of results?.section_results ?? []) {
      const attributes = onlyProblems ? mismatchedAttributes(section) : (section.attributes ?? []);
      map.set(String(section.section_id), attributeRows(attributes, `${section.section_id}/`, intl));
    }
    return map;
  }, [results, onlyProblems, intl]);
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
        <Spinner /> <FormattedMessage {...messages.loading} />
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
            <FormattedMessage {...messages.backToSummary} />
          </Button>
        )}
        <MarkdownReport
          reportUri={reportUri}
          documentId={documentId}
          report="evaluation"
          emptyMessage={intl.formatMessage(messages.markdownEmpty)}
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
                <FormattedMessage {...messages.mismatchesOnly} />
              </Toggle>
              {/* The markdown artifact is still what you attach to a ticket or
                  hand to someone without UI access, and it already carries its
                  own download and print actions — so it stays one click away
                  rather than being replaced. */}
              <Button iconName="file" onClick={() => setShowMarkdown(true)}>
                <FormattedMessage {...messages.markdownReport} />
              </Button>
            </SpaceBetween>
          }
        >
          <FormattedMessage {...messages.title} />
        </Header>
      }
    >
      <SpaceBetween size="l">
        {summary.excluded && (
          <Alert type="info" header={intl.formatMessage(messages.documentNotScored)}>
            {summary.exclusionReason ? (
              <FormattedMessage {...messages.documentNotScoredBodyWithReason} values={{ reason: summary.exclusionReason }} />
            ) : (
              <FormattedMessage {...messages.documentNotScoredBody} />
            )}
          </Alert>
        )}

        {/* Front and centre: the two questions the report exists to answer, kept
            separate because they fail independently — a document can be classified
            perfectly and extracted badly, or the reverse. */}
        <ColumnLayout columns={split ? 4 : 3} variant="text-grid">
          <ScoreTile
            label={intl.formatMessage(messages.extractionTile)}
            score={summary.extractionScore}
            hint={
              // Both figures, as the markdown summary has: the count is the plain
              // match rate and can differ from the weighted score by several points.
              summary.extractionIsWeighted
                ? intl.formatMessage(messages.extractionHintWeighted, {
                    matched: summary.matchedAttributes,
                    total: summary.totalAttributes,
                  })
                : intl.formatMessage(messages.extractionHint, { matched: summary.matchedAttributes, total: summary.totalAttributes })
            }
          />
          <ScoreTile
            label={intl.formatMessage(messages.classificationTile)}
            score={summary.classificationScore}
            hint={
              // The score is the split results' page-level accuracy: without them the
              // tile has no score and is not drawn, so it needs no hint of its own.
              split
                ? intl.formatMessage(messages.classificationHint, { correct: split.pageLevel.correct, total: split.pageLevel.total })
                : undefined
            }
          />
          {/* The split figures the markdown leads its summary with. Without-order
              is the headline because it is the question "did the packet split
              correctly"; the ordered figure is the stricter variant beside it. */}
          {split && (
            <ScoreTile
              label={intl.formatMessage(messages.splitTile)}
              score={split.withoutOrder.score}
              hint={intl.formatMessage(messages.splitHint, {
                correct: split.withoutOrder.correct,
                total: split.withoutOrder.total,
                score: formatScore(split.withOrder.score, locale),
              })}
            />
          )}
          <ScoreTile
            label={intl.formatMessage(messages.f1Tile)}
            score={summary.f1Score}
            hint={intl.formatMessage(messages.f1Hint, {
              precision: formatScore(summary.precision, locale),
              recall: formatScore(summary.recall, locale),
            })}
          />
        </ColumnLayout>

        {/* A wrong class makes the field numbers above and below it meaningless
            rather than merely wrong, so it is reported before them. */}
        {misclassifiedPages.length > 0 && (
          <Alert type="warning" header={intl.formatMessage(messages.misclassifiedHeader)}>
            <SpaceBetween size="xxs">
              {misclassifiedPages.map((group) => (
                <Box key={`${group.predicted}-${group.expected}`} variant="p">
                  <FormattedMessage
                    {...messages.misclassifiedPages}
                    values={{
                      count: group.pages.length,
                      pages: formatPageRanges(group.pages),
                      predicted: group.predicted,
                      expected: group.expected,
                      b: bold,
                    }}
                  />
                </Box>
              ))}
              <Box variant="small" color="text-body-secondary">
                <FormattedMessage {...messages.misclassifiedHint} />
              </Box>
            </SpaceBetween>
          </Alert>
        )}

        {excluded.length > 0 && (
          <ExpandableSection
            variant="container"
            headerText={intl.formatMessage(messages.excludedHeader, { count: excluded.length })}
            headerDescription={intl.formatMessage(messages.excludedDescription)}
          >
            <SpaceBetween size="xs">
              <Box variant="small" color="text-body-secondary">
                <FormattedMessage {...messages.excludedExplanation} values={{ key: 'x-aws-idp-exclude-from-processing', code }} />
              </Box>
              <Table
                variant="embedded"
                contentDensity="compact"
                items={excluded}
                trackBy="sectionId"
                columnDefinitions={[
                  { id: 'section', header: intl.formatMessage(messages.excludedSectionColumn), cell: (row) => row.sectionId },
                  { id: 'class', header: intl.formatMessage(messages.excludedClassColumn), cell: (row) => row.classification || '—' },
                  { id: 'reason', header: intl.formatMessage(messages.excludedReasonColumn), cell: (row) => row.reason },
                  { id: 'pages', header: intl.formatMessage(messages.excludedPagesColumn), cell: (row) => formatPageRanges(row.pages) },
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
            headerText={intl.formatMessage(messages.splitAnalysisHeader)}
            headerCounter={(() => {
              // Expected sections only: a stray prediction is not an unmatched expectation,
              // and counting both read "3 unmatched" on a one-section packet.
              const expected = split.rows.filter((r) => r.sectionId !== null);
              const unmatched = expected.filter((r) => !r.sectionMatched).length;
              const unexpected = split.rows.length - expected.length;
              const parts = [intl.formatMessage(messages.splitUnmatched, { unmatched, expected: expected.length })];
              if (unexpected > 0) parts.push(intl.formatMessage(messages.splitUnexpected, { count: unexpected }));
              return `(${parts.join(' · ')})`;
            })()}
            defaultExpanded={split.rows.some((r) => !r.sectionMatched || !r.orderMatched)}
          >
            <SplitAnalysisSection split={split} />
          </ExpandableSection>
        )}

        <ExpandableSection
          variant="container"
          headerText={intl.formatMessage(messages.allMetricsHeader)}
          headerDescription={intl.formatMessage(messages.allMetricsDescription)}
        >
          <SpaceBetween size="m">
            {splitMetricRows.length > 0 && (
              <SpaceBetween size="xs">
                <Box variant="h4">
                  <FormattedMessage {...messages.splitMetricsHeading} />
                </Box>
                <MetricTable rows={splitMetricRows} />
              </SpaceBetween>
            )}
            <SpaceBetween size="xs">
              <Box variant="h4">
                <FormattedMessage {...messages.extractionMetricsHeading} />
              </Box>
              <MetricTable rows={overallRows} emptyText={intl.formatMessage(messages.extractionMetricsEmpty)} />
            </SpaceBetween>
          </SpaceBetween>
        </ExpandableSection>

        {sections.map((section: SectionResult) => {
          const attributes = onlyProblems ? mismatchedAttributes(section) : (section.attributes ?? []);
          const mismatchCount = mismatchedAttributes(section).length;
          const failure = sectionFailure(section, intl);
          const skipped = skippedFieldCount(section);
          const sectionMetrics = metricRows(section.metrics as Record<string, unknown> | null);
          const sectionId = String(section.section_id);
          const className = section.document_class ?? null;
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
          // A never-evaluated section carries one placeholder attribute, which is not
          // a measured mismatch; counting it beside "not scored" contradicted the
          // alert below in miniature.
          const counterParts = [
            failure ? intl.formatMessage(messages.sectionNotScored) : sectionScore !== null ? formatScore(sectionScore, locale) : null,
            !failure && mismatchCount > 0 ? intl.formatMessage(messages.sectionMismatched, { count: mismatchCount }) : null,
          ].filter(Boolean);

          return (
            <ExpandableSection
              key={String(section.section_id)}
              // Container variant: it is the only one that renders headerCounter, and
              // the section score lives there.
              variant="container"
              defaultExpanded={sections.length === 1 || mismatchCount > 0 || failure !== null}
              headerText={
                className === null
                  ? intl.formatMessage(messages.sectionHeaderUnknownClass, { sectionId })
                  : intl.formatMessage(messages.sectionHeader, { sectionId, className })
              }
              headerCounter={counterParts.length > 0 ? `(${counterParts.join(' · ')})` : undefined}
            >
              <SpaceBetween size="s">
                {failure && (
                  <Alert type="error" header={intl.formatMessage(messages.sectionNotEvaluated)}>
                    <SpaceBetween size="xs">
                      <Box>
                        {failure.reason ??
                          (className === null ? (
                            <FormattedMessage {...messages.couldNotEvaluateUnknownClass} />
                          ) : (
                            <FormattedMessage {...messages.couldNotEvaluate} values={{ className }} />
                          ))}
                      </Box>
                      <Box variant="small" color="text-body-secondary">
                        <FormattedMessage {...messages.placeholders} />
                      </Box>
                      {failure.steps.length > 0 && (
                        <div>
                          <Box variant="strong">
                            <FormattedMessage {...messages.howToFix} />
                          </Box>
                          <ol style={{ margin: 0 }}>
                            {failure.steps.map((step, index) => (
                              // A step's position is its identity: its text follows the UI's language.
                              // eslint-disable-next-line react/no-array-index-key
                              <li key={index}>{step}</li>
                            ))}
                          </ol>
                        </div>
                      )}
                    </SpaceBetween>
                  </Alert>
                )}
                {!failure && skipped > 0 && (
                  <Alert type="warning">
                    <FormattedMessage {...messages.skippedFields} values={{ count: skipped }} />
                  </Alert>
                )}
                {sectionMetrics.length > 0 && (
                  <ExpandableSection headerText={intl.formatMessage(messages.sectionMetrics)} variant="footer">
                    <MetricTable rows={sectionMetrics} notScored={failure !== null} />
                  </ExpandableSection>
                )}
                {failure ? null : onlyProblems && attributes.length === 0 ? (
                  <Box color="text-status-success">
                    <FormattedMessage {...messages.allMatched} />
                  </Box>
                ) : (
                  <AttributeTable rows={rowsBySection.get(String(section.section_id)) ?? []} />
                )}
              </SpaceBetween>
            </ExpandableSection>
          );
        })}

        {methods.length > 0 && (
          <ExpandableSection headerText={intl.formatMessage(messages.methodsHeader)}>
            <Box variant="small">
              <FormattedMessage {...messages.methodsBody} values={{ methods: methods.join(', ') }} />
            </Box>
          </ExpandableSection>
        )}

        <ExpandableSection headerText={intl.formatMessage(messages.howScores)}>
          <HowScoresAreComputed hasSplit={split !== null} />
        </ExpandableSection>

        {executionTime && (
          <Box variant="small" color="text-body-secondary">
            <FormattedMessage {...messages.executionTime} values={{ executionTime }} />
          </Box>
        )}
      </SpaceBetween>
    </Container>
  );
};

export default EvaluationReport;
