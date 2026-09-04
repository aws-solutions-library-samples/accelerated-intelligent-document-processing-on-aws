// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useState, useEffect } from 'react';
import { BarChart, Bar, LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';
import {
  Container,
  Header,
  SpaceBetween,
  Box,
  ColumnLayout,
  ProgressBar,
  Badge,
  Alert,
  Table,
  Button,
  Modal,
  Textarea,
  FormField,
  Input,
  Select,
  ExpandableSection,
  RadioGroup,
  Pagination,
  Popover,
  TextFilter,
  CollectionPreferences,
} from '@cloudscape-design/components';
import { useCollection } from '@cloudscape-design/collection-hooks';

import yaml from 'js-yaml';
import { generateClient } from '../../api/client-shim';
import { getTestRun, startTestRun, getTestSets } from '../../graphql/generated';
import useConfigurationVersions from '../../hooks/use-configuration-versions';
import TestStudioHeader from './TestStudioHeader';
import useAppContext from '../../contexts/app';
import { formatConfigVersionLink } from './utils/configVersionUtils';
import MetricInfo, { ACCURACY_METRIC_MAP, SPLIT_METRIC_MAP } from './utils/MetricInfo';
import { accuracyIntervalForField, formatBounds, formatMargin, isLowEvidence } from './accuracyInterval';
import ClassificationErrorsPanel from './ClassificationErrorsPanel';
import { asFiniteNumber, formatCostUsd, formatUnitCostUsd } from './formatCost';
import {
  parseCostBreakdown,
  calculateAvgCostPerPage,
  parseAccuracyBreakdown,
  parseSplitClassificationMetrics,
  parseGradedPacketMetrics,
  parseClassificationErrors,
  parseFieldMetrics,
  parseConfusionMatrix,
  parseConfidenceMetrics,
  parseWeightedOverallScoresFinite,
  parseTestRunConfig,
} from '../../graphql/awsjson-parsers';
import type { GradedPacketMetrics } from '../../graphql/awsjson-types';
import type { TestRunInput } from '../../graphql/generated/schema-types';
import type { SelectProps } from '@cloudscape-design/components';

const client = generateClient();

interface CostItem {
  context: string;
  serviceApi: string;
  unit: string;
  value: string;
  unitCost: string;
  estimatedCost: string;
  isTotal?: boolean;
  isSubtotal?: boolean;
  sortOrder: number;
}

interface ComprehensiveBreakdownProps {
  costBreakdown: Record<string, Record<string, Record<string, unknown>>> | null;
  accuracyBreakdown: Record<string, number> | null;
  splitClassificationMetrics: Record<string, unknown> | null;
  gradedPacketMetrics: GradedPacketMetrics | null;
  fieldMetrics: Record<string, unknown> | null;
  averageWeightedScore: number | null;
  confidenceMetrics: unknown;
}

const ComprehensiveBreakdown = ({
  costBreakdown,
  accuracyBreakdown,
  splitClassificationMetrics,
  gradedPacketMetrics,
  fieldMetrics,
  averageWeightedScore,
  confidenceMetrics,
}: ComprehensiveBreakdownProps): React.JSX.Element => {
  const hasGradedPacketMetrics = !!gradedPacketMetrics?.mean && Object.keys(gradedPacketMetrics.mean).length > 0;

  if (!costBreakdown && !accuracyBreakdown && !splitClassificationMetrics && !hasGradedPacketMetrics && !fieldMetrics) {
    return <Box>No breakdown data available</Box>;
  }

  return (
    <SpaceBetween direction="vertical" size="l">
      {/* Combined Accuracy and Split Classification Metrics */}
      {(accuracyBreakdown || splitClassificationMetrics || hasGradedPacketMetrics) && (
        <Container header={<Header variant="h3">Average Accuracy and Split Metrics</Header>}>
          <SpaceBetween direction="vertical" size="m">
            {/* Main metrics */}
            <Table
              resizableColumns
              items={(() => {
                const mainItems = [];

                // Add Weighted Overall Score
                if (averageWeightedScore !== null) {
                  mainItems.push({
                    metric: (
                      <>
                        <span style={{ color: '#687078' }}>Extraction:</span> Weighted Overall Score
                        <MetricInfo metric="Avg Weighted Score" />
                      </>
                    ),
                    value: averageWeightedScore.toFixed(3),
                  });
                }

                // Add Page and Split with Order Accuracy
                if (splitClassificationMetrics) {
                  if (splitClassificationMetrics.page_level_accuracy !== undefined) {
                    mainItems.push({
                      metric: (
                        <>
                          <span style={{ color: '#687078' }}>Classification:</span> Page Level Accuracy
                          <MetricInfo metric="Page Level Accuracy" />
                        </>
                      ),
                      value:
                        typeof splitClassificationMetrics.page_level_accuracy === 'number'
                          ? splitClassificationMetrics.page_level_accuracy.toFixed(3)
                          : splitClassificationMetrics.page_level_accuracy?.toString() || '0',
                    });
                  }

                  if (splitClassificationMetrics.split_accuracy_with_order !== undefined) {
                    mainItems.push({
                      metric: (
                        <>
                          <span style={{ color: '#687078' }}>Classification:</span> Split Accuracy With Order
                          <MetricInfo metric="Split Accuracy With Order" />
                        </>
                      ),
                      value:
                        typeof splitClassificationMetrics.split_accuracy_with_order === 'number'
                          ? splitClassificationMetrics.split_accuracy_with_order.toFixed(3)
                          : splitClassificationMetrics.split_accuracy_with_order?.toString() || '0',
                    });
                  }
                }

                return mainItems;
              })()}
              columnDefinitions={[
                { id: 'metric', header: 'Metric', cell: (item) => item.metric, width: 410 },
                { id: 'value', header: 'Value', cell: (item) => item.value, width: 200 },
              ]}
              variant="embedded"
            />

            {/* Details in collapsible section */}
            <ExpandableSection headerText="Additional Metrics">
              <Container>
                <Table
                  resizableColumns
                  items={[
                    // All accuracy breakdown metrics
                    ...(accuracyBreakdown
                      ? Object.entries(accuracyBreakdown).map(([key, value]) => {
                          const displayName = key.replace(/_/g, ' ').replace(/\b\w/g, (l) => l.toUpperCase());
                          // Map backend keys to MetricInfo keys
                          const extendedMetricMap: Record<
                            string,
                            | 'Accuracy'
                            | 'Precision'
                            | 'Recall'
                            | 'F1'
                            | 'False Alarm Rate'
                            | 'False Discovery Rate'
                            | 'Avg Accuracy'
                            | 'Avg Confidence'
                          > = {
                            average_accuracy: 'Avg Accuracy',
                            avg_confidence: 'Avg Confidence',
                            f1_score: 'F1',
                            false_alarm_rate: 'False Alarm Rate',
                            false_discovery_rate: 'False Discovery Rate',
                            ...ACCURACY_METRIC_MAP,
                          };
                          const metricKey = extendedMetricMap[key];

                          return {
                            metric: (
                              <>
                                <span style={{ color: '#687078' }}>Extraction:</span> {displayName}
                                {metricKey && <MetricInfo metric={metricKey} />}
                              </>
                            ),
                            value: value !== null && value !== undefined ? value.toFixed(3) : '0.000',
                          };
                        })
                      : []),
                    // Confidence calibration metrics (Stickler v0.4.0+)
                    ...(() => {
                      const confMetrics = parseConfidenceMetrics(confidenceMetrics);
                      if (!confMetrics) return [];

                      const items = [];
                      const { overall, coverage } = confMetrics;

                      if (overall) {
                        if (overall.auroc?.value !== null && overall.auroc?.value !== undefined) {
                          items.push({
                            metric: (
                              <>
                                <span style={{ color: '#687078' }}>Confidence:</span> AUROC
                                <MetricInfo metric="AUROC" />
                              </>
                            ),
                            value: overall.auroc.value.toFixed(3),
                          });
                        }
                        if (overall.ece?.value !== null && overall.ece?.value !== undefined) {
                          items.push({
                            metric: (
                              <>
                                <span style={{ color: '#687078' }}>Confidence:</span> ECE (Calibration Error)
                                <MetricInfo metric="ECE" />
                              </>
                            ),
                            value: overall.ece.value.toFixed(3),
                          });
                        }
                        if (overall.brier?.value !== null && overall.brier?.value !== undefined) {
                          items.push({
                            metric: (
                              <>
                                <span style={{ color: '#687078' }}>Confidence:</span> Brier Score
                                <MetricInfo metric="Brier" />
                              </>
                            ),
                            value: overall.brier.value.toFixed(3),
                          });
                        }
                        // ECARB@30
                        const ecarbData = overall.error_capture_at_budget as {
                          budgets?: Record<string, { pct_errors_caught?: number; gain?: number }>;
                        };
                        const ecarb30 = ecarbData?.budgets?.['0.30'];
                        if (ecarb30?.pct_errors_caught !== null && ecarb30?.pct_errors_caught !== undefined) {
                          items.push({
                            metric: (
                              <>
                                <span style={{ color: '#687078' }}>Confidence:</span> ECARB@30
                                <MetricInfo metric="ECARB@30" />
                              </>
                            ),
                            value: `${(ecarb30.pct_errors_caught * 100).toFixed(0)}% (${ecarb30.gain?.toFixed(2)}x)`,
                          });
                        }
                      }

                      if (coverage) {
                        items.push({
                          metric: (
                            <>
                              <span style={{ color: '#687078' }}>Confidence:</span> Coverage Ratio
                              <MetricInfo metric="Coverage Ratio" />
                            </>
                          ),
                          value: `${(coverage.ratio * 100).toFixed(1)}% (${coverage.fields_with_confidence}/${coverage.fields_total})`,
                        });
                      }

                      return items;
                    })(),
                    // Graded packet metrics (V-measure / Rand / ordering) —
                    // R14 supplemental scores on top of the exact-match split
                    // counters above. Mean across documents that reported
                    // each key; skipped entirely when the panel has no data
                    // (older test runs or classification runs with no page
                    // overlap).
                    ...(hasGradedPacketMetrics
                      ? Object.entries(gradedPacketMetrics!.mean!).map(([key, value]) => {
                          const displayName = key.replace(/_/g, ' ').replace(/\b\w/g, (l) => l.toUpperCase());
                          return {
                            metric: (
                              <>
                                <span style={{ color: '#687078' }}>Classification (graded):</span> {displayName}
                              </>
                            ),
                            value: typeof value === 'number' ? value.toFixed(3) : String(value ?? '0'),
                          };
                        })
                      : []),
                    // Remaining split classification metrics
                    ...(splitClassificationMetrics
                      ? Object.entries(splitClassificationMetrics)
                          .filter(([key]) => key !== 'page_level_accuracy' && key !== 'split_accuracy_with_order')
                          .map(([key, value]) => {
                            const displayName = key.replace(/_/g, ' ').replace(/\b\w/g, (l) => l.toUpperCase());
                            // Map backend keys to MetricInfo keys
                            const mappedMetric = SPLIT_METRIC_MAP[key];

                            return {
                              metric: (
                                <>
                                  <span style={{ color: '#687078' }}>Classification:</span> {displayName}
                                  {mappedMetric && <MetricInfo metric={mappedMetric} />}
                                </>
                              ),
                              value:
                                typeof value === 'number' && key.includes('accuracy')
                                  ? value.toFixed(3)
                                  : value !== null && value !== undefined
                                    ? value.toString()
                                    : '0',
                            };
                          })
                      : []),
                  ]}
                  columnDefinitions={[
                    { id: 'metric', header: 'Metric', cell: (item) => item.metric, width: 410 },
                    { id: 'value', header: 'Value', cell: (item) => item.value, width: 200 },
                  ]}
                  variant="embedded"
                />
              </Container>
            </ExpandableSection>
          </SpaceBetween>
        </Container>
      )}

      {/* Field Metrics */}
      {fieldMetrics &&
        Object.keys(fieldMetrics).length > 0 &&
        (() => {
          // Initialize with all object fields expanded
          const initialExpanded = new Set<string>();
          Object.keys(fieldMetrics).forEach((fieldName) => {
            // Check if this field has children
            if (Object.keys(fieldMetrics).some((f) => f.startsWith(fieldName + '.'))) {
              initialExpanded.add(fieldName);
            }
          });

          const [expandedFields, setExpandedFields] = React.useState<Set<string>>(initialExpanded);
          const [fieldMetricsPageSize, setFieldMetricsPageSize] = React.useState(10);
          // Parse confidence metrics to enrich field-level data
          // Backend now aggregates confidence metrics to match field_metrics structure
          const parsedConfidenceMetrics = parseConfidenceMetrics(confidenceMetrics);
          const hasConfidenceData = parsedConfidenceMetrics?.fields && Object.keys(parsedConfidenceMetrics.fields).length > 0;

          const [fieldMetricsVisibleColumns, setFieldMetricsVisibleColumns] = React.useState([
            'fieldName',
            'accuracy',
            // Directly after accuracy: a per-field accuracy measured on 3 observations
            // and one measured on 300 read identically without them.
            'observations',
            'accuracyMargin',
            'precision',
            'recall',
            ...(hasConfidenceData ? ['auroc', 'ecab30', 'ece', 'brier'] : []),
            'tp',
            'fp',
            'tn',
            'fn',
          ]);

          // Build hierarchical structure with confidence data
          const allItems = Object.entries(fieldMetrics).map(([fieldName, metrics]) => {
            const m = metrics as {
              tp?: number;
              fp?: number;
              tn?: number;
              fn?: number;
              accuracy_observations?: number;
              accuracy_margin?: number;
              accuracy_low?: number;
              accuracy_high?: number;
            };
            const tp = m.tp ?? 0;
            const fp = m.fp ?? 0;
            const tn = m.tn ?? 0;
            const fn = m.fn ?? 0;
            const total = tp + fp + tn + fn;
            const interval = accuracyIntervalForField(m);

            // Extract field-level confidence metrics (if available)
            // Backend aggregates confidence to match field_metrics keys (pattern-based)
            const fieldConfidence = parsedConfidenceMetrics?.fields?.[fieldName];
            const auroc = fieldConfidence?.auroc?.value;
            const ece = fieldConfidence?.ece?.value;
            const brier = fieldConfidence?.brier?.value;

            // Extract ECARB@30 (Error Capture at Review Budget 30%)
            const ecab = (
              fieldConfidence?.error_capture_at_budget as { budgets?: Record<string, { pct_errors_caught?: number; gain?: number }> }
            )?.budgets?.['0.30'];
            const ecabPct = ecab?.pct_errors_caught;
            const ecabGain = ecab?.gain;
            const ecabDisplay =
              ecabPct !== null && ecabPct !== undefined && ecabGain !== null && ecabGain !== undefined
                ? `${(ecabPct * 100).toFixed(0)}% (${ecabGain.toFixed(1)}x)`
                : 'N/A';

            return {
              fieldName,
              tp,
              fp,
              tn,
              fn,
              accuracy: total > 0 ? ((tp + tn) / total).toFixed(3) : 'N/A',
              observations: interval ? interval.observations : 0,
              accuracyMargin: formatMargin(interval),
              // Sorting must key on the number: the displayed value is formatted, and
              // "±10.2" sorts before "±5.9" as a string. The accuracy column gets away
              // with a string because toFixed(3) is fixed-width; margins are not.
              accuracyMarginValue: interval ? interval.margin : -1,
              accuracyBounds: formatBounds(interval),
              lowEvidence: isLowEvidence(interval),
              precision: tp + fp > 0 ? (tp / (tp + fp)).toFixed(3) : 'N/A',
              recall: tp + fn > 0 ? (tp / (tp + fn)).toFixed(3) : 'N/A',
              auroc: auroc !== null && auroc !== undefined ? auroc.toFixed(3) : 'N/A',
              ece: ece !== null && ece !== undefined ? ece.toFixed(3) : 'N/A',
              brier: brier !== null && brier !== undefined ? brier.toFixed(3) : 'N/A',
              ecab30: ecabDisplay,
              depth: (fieldName.match(/\./g) || []).length,
            };
          });

          // Check if a field has children
          const hasChildren = (fieldName: string) =>
            allItems.some((item) => item.fieldName.startsWith(fieldName + '.') && item.depth === (fieldName.match(/\./g) || []).length + 1);

          // Get parent field name
          const getParent = (fieldName: string) => {
            const parts = fieldName.split('.');
            return parts.length > 1 ? parts.slice(0, -1).join('.') : null;
          };

          // Check if field should be visible
          const isVisible = (item: { fieldName: string }) => {
            let parent = getParent(item.fieldName);
            while (parent) {
              if (!expandedFields.has(parent)) return false;
              parent = getParent(parent);
            }
            return true;
          };

          // Build display items
          const displayItems = allItems.filter(isVisible).map((item) => ({
            ...item,
            hasChildren: hasChildren(item.fieldName),
            isExpanded: expandedFields.has(item.fieldName),
          }));

          const toggleExpand = (fieldName: string) => {
            setExpandedFields((prev) => {
              const next = new Set(prev);
              if (next.has(fieldName)) {
                next.delete(fieldName);
              } else {
                next.add(fieldName);
              }
              return next;
            });
          };

          const expandAll = () => {
            const allParents = new Set<string>();
            allItems.forEach((item) => {
              if (hasChildren(item.fieldName)) allParents.add(item.fieldName);
            });
            setExpandedFields(allParents);
          };

          const collapseAll = () => {
            setExpandedFields(new Set<string>());
          };

          const allExpanded = allItems.filter((item) => hasChildren(item.fieldName)).every((item) => expandedFields.has(item.fieldName));

          const { items, collectionProps, paginationProps, filterProps } = useCollection(displayItems, {
            filtering: {
              empty: 'No field metrics found',
              noMatch: 'No fields match the filter',
            },
            pagination: { pageSize: fieldMetricsPageSize },
            sorting: { defaultState: { sortingColumn: { sortingField: 'fieldName' }, isDescending: false } },
          });

          const allColumnDefinitions = [
            {
              id: 'fieldName',
              header: 'Field Name',
              cell: (item: (typeof displayItems)[0]) => {
                const indent = item.depth * 20;
                return (
                  <span
                    role={item.hasChildren ? 'button' : undefined}
                    tabIndex={item.hasChildren ? 0 : undefined}
                    style={{
                      cursor: item.hasChildren ? 'pointer' : 'default',
                      paddingLeft: `${indent}px`,
                      display: 'inline-flex',
                      alignItems: 'center',
                    }}
                    onClick={() => item.hasChildren && toggleExpand(item.fieldName)}
                    onKeyDown={(e) => {
                      if (item.hasChildren && (e.key === 'Enter' || e.key === ' ')) {
                        e.preventDefault();
                        toggleExpand(item.fieldName);
                      }
                    }}
                  >
                    <span style={{ display: 'inline-block', width: '16px', flexShrink: 0 }}>
                      {item.hasChildren ? (item.isExpanded ? '▼' : '▶') : ''}
                    </span>
                    {item.fieldName}
                  </span>
                );
              },
              sortingField: 'fieldName',
              minWidth: 200,
            },
            {
              id: 'accuracy',
              header: (
                <>
                  Accuracy
                  <MetricInfo metric="Accuracy" />
                </>
              ),
              cell: (item: (typeof displayItems)[0]) => item.accuracy,
              sortingField: 'accuracy',
              minWidth: 150,
            },
            {
              id: 'observations',
              header: 'Observations',
              cell: (item: (typeof displayItems)[0]) => (item.observations > 0 ? item.observations : '—'),
              sortingField: 'observations',
              minWidth: 120,
            },
            {
              id: 'accuracyMargin',
              header: '95% margin',
              cell: (item: (typeof displayItems)[0]) =>
                item.observations > 0 ? (
                  <Popover
                    dismissButton={false}
                    position="top"
                    size="small"
                    triggerType="text"
                    content={
                      <SpaceBetween size="xxs">
                        <Box variant="small">95% confidence interval: {item.accuracyBounds}</Box>
                        <Box variant="small" color="text-body-secondary">
                          Measured on {item.observations} observation(s). Sampling uncertainty only — it does not account for errors in the
                          ground truth itself, or for observations that repeat within one document.
                        </Box>
                      </SpaceBetween>
                    }
                  >
                    {/* Not colour alone: a subdued margin is a real statement about
                        evidence, so it carries text too. */}
                    <Box variant="span" color={item.lowEvidence ? 'text-status-inactive' : undefined}>
                      {item.accuracyMargin}
                      {item.lowEvidence ? ' (low n)' : ''}
                    </Box>
                  </Popover>
                ) : (
                  '—'
                ),
              sortingField: 'accuracyMarginValue',
              minWidth: 130,
            },
            {
              id: 'precision',
              header: (
                <>
                  Precision
                  <MetricInfo metric="Precision" />
                </>
              ),
              cell: (item: (typeof displayItems)[0]) => item.precision,
              sortingField: 'precision',
              minWidth: 150,
            },
            {
              id: 'recall',
              header: (
                <>
                  Recall
                  <MetricInfo metric="Recall" />
                </>
              ),
              cell: (item: (typeof displayItems)[0]) => item.recall,
              sortingField: 'recall',
              minWidth: 140,
            },
            ...(hasConfidenceData
              ? [
                  {
                    id: 'auroc',
                    header: (
                      <>
                        AUROC
                        <MetricInfo metric="AUROC" />
                      </>
                    ),
                    cell: (item: (typeof displayItems)[0]) => item.auroc,
                    sortingField: 'auroc',
                    minWidth: 145,
                  },
                  {
                    id: 'ece',
                    header: (
                      <>
                        ECE
                        <MetricInfo metric="ECE" />
                      </>
                    ),
                    cell: (item: (typeof displayItems)[0]) => item.ece,
                    sortingField: 'ece',
                  },
                  {
                    id: 'brier',
                    header: (
                      <>
                        Brier
                        <MetricInfo metric="Brier" />
                      </>
                    ),
                    cell: (item: (typeof displayItems)[0]) => item.brier,
                    sortingField: 'brier',
                  },
                  {
                    id: 'ecab30',
                    header: (
                      <>
                        ECARB@30
                        <MetricInfo metric="ECARB@30" />
                      </>
                    ),
                    cell: (item: (typeof displayItems)[0]) => item.ecab30,
                    sortingField: 'ecab30',
                    minWidth: 160,
                  },
                ]
              : []),
            {
              id: 'tp',
              header: (
                <>
                  TP
                  <MetricInfo metric="TP" />
                </>
              ),
              cell: (item: (typeof displayItems)[0]) => item.tp,
              sortingField: 'tp',
            },
            {
              id: 'fp',
              header: (
                <>
                  FP
                  <MetricInfo metric="FP" />
                </>
              ),
              cell: (item: (typeof displayItems)[0]) => item.fp,
              sortingField: 'fp',
            },
            {
              id: 'tn',
              header: (
                <>
                  TN
                  <MetricInfo metric="TN" />
                </>
              ),
              cell: (item: (typeof displayItems)[0]) => item.tn,
              sortingField: 'tn',
            },
            {
              id: 'fn',
              header: (
                <>
                  FN
                  <MetricInfo metric="FN" />
                </>
              ),
              cell: (item: (typeof displayItems)[0]) => item.fn,
              sortingField: 'fn',
            },
          ];

          return (
            <Container header={<Header variant="h3">Field Level Metrics</Header>}>
              <ExpandableSection headerText="View Details" defaultExpanded={false}>
                <Table
                  {...collectionProps}
                  resizableColumns
                  visibleColumns={fieldMetricsVisibleColumns}
                  filter={
                    <TextFilter
                      filteringText={filterProps.filteringText}
                      onChange={filterProps.onChange}
                      filteringAriaLabel="Filter field metrics"
                      filteringPlaceholder="Search fields..."
                    />
                  }
                  items={items}
                  columnDefinitions={allColumnDefinitions}
                  pagination={
                    <SpaceBetween direction="horizontal" size="xs">
                      <Pagination
                        currentPageIndex={paginationProps.currentPageIndex}
                        pagesCount={paginationProps.pagesCount}
                        onChange={paginationProps.onChange}
                      />
                    </SpaceBetween>
                  }
                  preferences={
                    <SpaceBetween direction="horizontal" size="xs">
                      <Button
                        variant="icon"
                        iconName={allExpanded ? 'treeview-collapse' : 'treeview-expand'}
                        onClick={allExpanded ? collapseAll : expandAll}
                        ariaLabel={allExpanded ? 'Collapse all' : 'Expand all'}
                      />
                      <CollectionPreferences
                        title="Preferences"
                        confirmLabel="Confirm"
                        cancelLabel="Cancel"
                        preferences={{
                          pageSize: fieldMetricsPageSize,
                          visibleContent: fieldMetricsVisibleColumns,
                        }}
                        onConfirm={({ detail }) => {
                          if (detail.pageSize) setFieldMetricsPageSize(detail.pageSize);
                          if (detail.visibleContent) setFieldMetricsVisibleColumns([...detail.visibleContent]);
                        }}
                        pageSizePreference={{
                          title: 'Page size',
                          options: [
                            { value: 10, label: '10 fields' },
                            { value: 25, label: '25 fields' },
                            { value: 50, label: '50 fields' },
                            { value: 100, label: '100 fields' },
                          ],
                        }}
                        visibleContentPreference={{
                          title: 'Visible columns',
                          options: [
                            {
                              label: 'Field metrics columns',
                              options: [
                                { id: 'fieldName', label: 'Field Name', editable: false },
                                { id: 'accuracy', label: 'Accuracy' },
                                { id: 'observations', label: 'Observations' },
                                { id: 'accuracyMargin', label: '95% margin' },
                                { id: 'precision', label: 'Precision' },
                                { id: 'recall', label: 'Recall' },
                                ...(hasConfidenceData
                                  ? [
                                      { id: 'auroc', label: 'AUROC (Confidence)' },
                                      { id: 'ecab30', label: 'ECARB@30 (Review Budget)' },
                                      { id: 'ece', label: 'ECE (Calibration)' },
                                      { id: 'brier', label: 'Brier Score' },
                                    ]
                                  : []),
                                { id: 'tp', label: 'TP' },
                                { id: 'fp', label: 'FP' },
                                { id: 'tn', label: 'TN' },
                                { id: 'fn', label: 'FN' },
                              ],
                            },
                          ],
                        }}
                      />
                    </SpaceBetween>
                  }
                  variant="embedded"
                />
              </ExpandableSection>
            </Container>
          );
        })()}

      {/* Cost breakdown */}
      {costBreakdown && (
        <Container header={<Header variant="h3">Estimated Cost</Header>}>
          <Table
            resizableColumns
            wrapLines
            items={(() => {
              const costItems: CostItem[] = [];
              let totalCost = 0;
              const contextTotals: Record<string, number> = {};

              // First pass: collect all items and calculate context totals
              Object.entries(costBreakdown).forEach(([context, services]) => {
                let contextSubtotal = 0;

                Object.entries(services).forEach(([serviceUnit, details]) => {
                  // Parse service/api_unit format: find last underscore to separate unit
                  const lastUnderscoreIndex = serviceUnit.lastIndexOf('_');
                  const serviceApi = serviceUnit.substring(0, lastUnderscoreIndex);
                  const unit = serviceUnit.substring(lastUnderscoreIndex + 1);
                  const [service, ...apiParts] = serviceApi.split('/');
                  const api = apiParts.join('/');

                  const cost = (details.estimated_cost as number) || 0;
                  const unitCost = asFiniteNumber(details.unit_cost);
                  // Rows like `totalTokens` and `requests` are counts, not charges:
                  // nothing prices them and nothing is billed for them. Seen live
                  // reading "$0" unit cost beside "N/A" estimated cost, which says
                  // both "free" and "not priced" in the same row.
                  const isUnpriced = cost === 0 && (unitCost === null || unitCost === 0);
                  contextSubtotal += cost;

                  costItems.push({
                    context,
                    serviceApi: `${service}/${api}`,
                    unit: (details.unit as string) || unit,
                    value: (details.value as string) || 'N/A',
                    unitCost: isUnpriced ? '—' : unitCost === null ? 'Not priced' : formatUnitCostUsd(unitCost),
                    estimatedCost: isUnpriced ? '—' : cost > 0 ? formatCostUsd(cost) : 'N/A',
                    sortOrder: 0, // Regular items
                  });
                });

                contextTotals[context] = contextSubtotal;
                totalCost += contextSubtotal;
              });

              // Sort items by context first, then by service/api
              costItems.sort((a, b) => {
                if (a.context !== b.context) {
                  return a.context.localeCompare(b.context);
                }
                return a.serviceApi.localeCompare(b.serviceApi);
              });

              // Second pass: insert subtotal rows after each context group
              const finalItems: CostItem[] = [];
              const _currentContext: string | null = null;

              costItems.forEach((item, index) => {
                // Add the regular item
                finalItems.push(item);

                // Check if this is the last item for this context
                const nextItem = costItems[index + 1];
                const isLastInContext = !nextItem || nextItem.context !== item.context;

                if (isLastInContext) {
                  // Add subtotal row for every context
                  finalItems.push({
                    context: '',
                    serviceApi: `${item.context} Subtotal`,
                    unit: '',
                    value: '',
                    unitCost: '',
                    estimatedCost: formatCostUsd(contextTotals[item.context]),
                    isSubtotal: true,
                    sortOrder: 1, // Subtotal items
                  });
                }
              });

              // Add total row
              if (totalCost > 0) {
                finalItems.push({
                  context: '',
                  serviceApi: 'Total',
                  unit: '',
                  value: '',
                  unitCost: '',
                  estimatedCost: formatCostUsd(totalCost),
                  isTotal: true,
                  sortOrder: 2, // Total item
                });
              }

              return finalItems;
            })()}
            columnDefinitions={[
              {
                id: 'context',
                header: 'Context',
                cell: (item) => (item.isSubtotal || item.isTotal ? '' : item.context),
              },
              {
                id: 'serviceApi',
                header: 'Service/Api',
                cell: (item) => (
                  <span
                    style={{
                      fontWeight: item.isSubtotal || item.isTotal ? 'bold' : 'normal',
                      color: item.isTotal ? '#0073bb' : 'inherit',
                    }}
                  >
                    {item.serviceApi}
                  </span>
                ),
              },
              {
                id: 'unit',
                header: 'Unit',
                cell: (item) => (item.isSubtotal || item.isTotal ? '' : item.unit),
              },
              {
                id: 'value',
                header: 'Value',
                cell: (item) => {
                  if (item.isSubtotal || item.isTotal) return '';
                  const value = item.value;
                  if (value === 'N/A' || !value) return 'N/A';
                  const numValue = parseFloat(value.toString().replace(/,/g, ''));
                  return isNaN(numValue) ? value : numValue.toLocaleString();
                },
              },
              {
                id: 'unitCost',
                header: 'Unit Cost',
                cell: (item) => (item.isSubtotal || item.isTotal ? '' : item.unitCost),
              },
              {
                id: 'estimatedCost',
                header: 'Estimated Cost',
                cell: (item) => {
                  if (item.isSubtotal || item.isTotal) {
                    return <span style={{ fontWeight: 'bold', color: item.isTotal ? '#0073bb' : 'inherit' }}>{item.estimatedCost}</span>;
                  }
                  // Already formatted when the row was built; re-running toFixed(4)
                  // here is what turned a sub-cent row back into '$0.0000'.
                  return item.estimatedCost || 'N/A';
                },
              },
            ]}
            variant="embedded"
          />
        </Container>
      )}
    </SpaceBetween>
  );
};

interface TestResultsProps {
  testRunId: string;
  setSelectedTestRunId?: ((id: string | null) => void) | null;
}

interface RangeDoc {
  docId: string;
  score: number;
}

interface SelectedRange {
  range: string;
  docs: RangeDoc[];
}

const TestResults = ({ testRunId, setSelectedTestRunId }: TestResultsProps): React.JSX.Element => {
  const { addTestRun } = useAppContext();
  const { versions } = useConfigurationVersions();
  const [results, setResults] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reRunLoading, setReRunLoading] = useState(false);
  const [showReRunModal, setShowReRunModal] = useState(false);
  const [reRunContext, setReRunContext] = useState('');
  const [reRunNumberOfFiles, setReRunNumberOfFiles] = useState('');
  const [testSetFileCount, setTestSetFileCount] = useState<number | null>(null);
  const [testSetStatus, setTestSetStatus] = useState<string | null>(null);
  const [testSetFilePattern, setTestSetFilePattern] = useState<string | null>(null);
  const [chartType, setChartType] = useState<SelectProps.Option>({ label: 'Bar Chart', value: 'bar' });
  const [showDocumentsModal, setShowDocumentsModal] = useState(false);
  const [selectedRangeData, setSelectedRangeData] = useState<SelectedRange | null>(null);
  const [lowestScoreCount, setLowestScoreCount] = useState<SelectProps.Option>({ label: '5', value: '5' });

  // Config export modal state
  const [showConfigExportModal, setShowConfigExportModal] = useState(false);
  const [configExportFormat, setConfigExportFormat] = useState('json');
  const [configExportFileName, setConfigExportFileName] = useState('');

  const checkTestSetStatus = async () => {
    if (!results?.testSetId) return;

    try {
      const testSetsResult = await client.graphql({
        query: getTestSets,
      });

      const testSets = testSetsResult.data.getTestSets || [];
      const testSet = testSets.find((ts) => ts?.id === results.testSetId);

      if (testSet) {
        setTestSetStatus(testSet.status ?? null);
        setTestSetFileCount(testSet.fileCount ?? null);
        setTestSetFilePattern(testSet.filePattern ?? null);
      } else {
        setTestSetStatus('NOT_FOUND');
        setTestSetFileCount(0);
        setTestSetFilePattern(null);
      }
    } catch (err) {
      console.error('Failed to check test set status:', err);
      setTestSetStatus('ERROR');
      setTestSetFileCount(0);
    }
  };

  useEffect(() => {
    let isCancelled = false;

    const fetchResults = async () => {
      if (isCancelled) return;

      try {
        const result = await client.graphql({
          query: getTestRun,
          variables: { testRunId },
        });

        if (isCancelled) return;

        const testRun = result.data.getTestRun;
        console.log('Test results:', testRun);
        setResults(testRun as Record<string, unknown> | null);
      } catch (err) {
        if (!isCancelled) {
          const typedErr = err as { errors?: Array<{ message: string }> };
          const errorMsg = typedErr.errors?.[0]?.message || (err as Error).message || 'Unknown error';
          setError(errorMsg);
        }
      } finally {
        if (!isCancelled) {
          setLoading(false);
        }
      }
    };

    fetchResults();

    return () => {
      isCancelled = true;
    };
  }, [testRunId]);

  useEffect(() => {
    if (results?.testSetId) {
      checkTestSetStatus();
    }
  }, [results]);

  if (loading) return <ProgressBar status="in-progress" label="Loading test results..." />;

  if (error) {
    const handleBackClick = () => {
      if (setSelectedTestRunId) {
        setSelectedTestRunId(null);
      } else {
        window.location.replace('#/test-studio?tab=executions');
      }
    };

    // Determine if this is a processing state or actual error
    const isProcessing =
      error.includes('evaluating results') || error.includes('not complete') || error.includes('QUEUED') || error.includes('RUNNING');

    return (
      <Container header={<Header variant="h1">Test Results: {testRunId}</Header>}>
        <SpaceBetween size="m">
          <Alert type={isProcessing ? 'info' : 'error'}>{error}</Alert>
          <Button onClick={handleBackClick}>Back to Test Results</Button>
        </SpaceBetween>
      </Container>
    );
  }

  if (!results) {
    const handleBackClick = () => {
      if (setSelectedTestRunId) {
        setSelectedTestRunId(null);
      } else {
        window.location.replace('#/test-studio?tab=executions');
      }
    };

    return (
      <Container header={<TestStudioHeader title={`Test Results: ${testRunId}`} onBackClick={handleBackClick} />}>
        <Box>No test results found</Box>
      </Container>
    );
  }

  const getStatusColor = (status: string): string => {
    if (status === 'COMPLETE') return 'green';
    if (status === 'RUNNING') return 'blue';
    return 'red';
  };

  const hasAccuracyData = results.overallAccuracy !== null && results.overallAccuracy !== undefined;

  // Calculate average weighted overall score. ``parseWeightedOverallScoresFinite``
  // drops documents whose weighted score isn't a finite number — the
  // aggregation Lambda already omits excluded (no-op) docs, but this also
  // guards against any stray null/NaN in older cached payloads so the mean
  // isn't pulled down by a synthetic 0.0.
  const averageWeightedScore = (() => {
    if (!results.weightedOverallScores) return null;
    const values = Object.values(parseWeightedOverallScoresFinite(results.weightedOverallScores as string));
    return values.length > 0 ? values.reduce((sum, score) => sum + score, 0) / values.length : null;
  })();

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const costBreakdown: any = results.costBreakdown ? parseCostBreakdown(results.costBreakdown as string) : null;

  // Calculate avg cost per page from costBreakdown page counts
  const avgCostPerPage = calculateAvgCostPerPage(results.totalCost as number, costBreakdown);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const accuracyBreakdown: any = results.accuracyBreakdown ? parseAccuracyBreakdown(results.accuracyBreakdown as string) : null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const splitClassificationMetrics: any = results.splitClassificationMetrics
    ? parseSplitClassificationMetrics(results.splitClassificationMetrics as string)
    : null;
  // Graded packet metrics (R14). Parse yields ``{}`` for absent/empty
  // payloads; treat that as null so the panel's presence check works with
  // ``||`` like the sibling metrics.
  const parsedGradedPacketMetrics = results.gradedPacketMetrics ? parseGradedPacketMetrics(results.gradedPacketMetrics as string) : null;
  const gradedPacketMetrics =
    parsedGradedPacketMetrics && parsedGradedPacketMetrics.mean && Object.keys(parsedGradedPacketMetrics.mean).length > 0
      ? parsedGradedPacketMetrics
      : null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const fieldMetrics: any = results.fieldMetrics ? parseFieldMetrics(results.fieldMetrics as string) : null;
  // Per-section classification mismatches. Absent on runs aggregated before this
  // shipped and on the Athena fallback path, both of which parse to {} — the
  // panel renders nothing rather than an empty table in that case.
  const classificationErrors = results.classificationErrors ? parseClassificationErrors(results.classificationErrors as string) : null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const _confusionMatrix: any = results.confusionMatrix ? parseConfusionMatrix(results.confusionMatrix as string) : null;

  // Helper function to get merged config from results.config
  // The config may be stored as a JSON string (possibly double-stringified) with {Default: {...}, Custom: {...}} or already merged
  const getMergedConfig = (config: unknown): Record<string, unknown> | null => {
    if (!config) return null;

    // Parse if it's a string (may be double-stringified) using typed parser
    let parsedConfig: Record<string, unknown> | null =
      typeof config === 'string' ? parseTestRunConfig(config) : (config as Record<string, unknown>);
    if (!parsedConfig) return null;

    // If config is wrapped in a "Config" object, extract it
    if (parsedConfig.Config && typeof parsedConfig.Config === 'object') {
      parsedConfig = parsedConfig.Config as Record<string, unknown>;
    }

    // Deep merge helper function
    const deepMerge = (target: Record<string, unknown>, source: Record<string, unknown>): Record<string, unknown> => {
      const output = { ...target };
      if (source && typeof source === 'object' && !Array.isArray(source)) {
        Object.keys(source).forEach((key) => {
          if (source[key] && typeof source[key] === 'object' && !Array.isArray(source[key])) {
            if (target[key] && typeof target[key] === 'object' && !Array.isArray(target[key])) {
              output[key] = deepMerge(target[key] as Record<string, unknown>, source[key] as Record<string, unknown>);
            } else {
              output[key] = { ...(source[key] as Record<string, unknown>) };
            }
          } else {
            output[key] = source[key];
          }
        });
      }
      return output;
    };

    // If config has Default and Custom properties, merge them
    const parsed = parsedConfig as Record<string, unknown>;
    if (parsed && parsed.Default && typeof parsed.Default === 'object') {
      const defaultConfig = parsed.Default as Record<string, unknown>;
      const customConfig = (parsed.Custom || {}) as Record<string, unknown>;

      console.log('Merging Default and Custom configs');
      return deepMerge(defaultConfig, customConfig);
    }

    // Config is already in merged format
    console.log('Config already in merged format or no Default/Custom found');
    return parsed as Record<string, unknown>;
  };

  // Open config export modal
  const openConfigExportModal = () => {
    if (!results?.config) {
      console.error('No config data available');
      return;
    }
    // Set default filename based on testRunId
    setConfigExportFileName(`test-run-${results.testRunId}-config`);
    setConfigExportFormat('json');
    setShowConfigExportModal(true);
  };

  // Handle config export
  const handleConfigExport = () => {
    try {
      const mergedConfig = getMergedConfig(results.config);
      if (!mergedConfig) {
        console.error('No config data available');
        return;
      }

      let content;
      let mimeType;
      let fileExtension;

      if (configExportFormat === 'yaml') {
        content = yaml.dump(mergedConfig);
        mimeType = 'text/yaml';
        fileExtension = 'yaml';
      } else {
        content = JSON.stringify(mergedConfig, null, 2);
        mimeType = 'application/json';
        fileExtension = 'json';
      }

      const blob = new Blob([content], { type: mimeType });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `${configExportFileName}.${fileExtension}`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
      setShowConfigExportModal(false);
    } catch (err) {
      console.error('Export failed:', err);
    }
  };

  const handleReRun = async () => {
    console.log('=== handleReRun START ===');
    console.log('results.testSetId:', results?.testSetId);
    console.log('results.testSetName:', results?.testSetName);

    const testSetId = results?.testSetId;
    console.log('Using testSetId:', testSetId);

    if (!testSetId) {
      console.error('No testSetId found in results. Cannot re-run without testSetId.');
      return;
    }

    // Validate numberOfFiles if provided
    if (reRunNumberOfFiles.trim()) {
      const numFiles = parseInt(reRunNumberOfFiles.trim(), 10);
      if (isNaN(numFiles) || numFiles <= 0) {
        console.error('Invalid numberOfFiles value');
        return;
      }
      if (testSetFileCount !== null && numFiles > testSetFileCount) {
        console.error(`numberOfFiles (${numFiles}) exceeds test set file count (${testSetFileCount})`);
        return;
      }
    }

    setReRunLoading(true);

    try {
      // Use the generated input type rather than a hand-written structural copy:
      // a locally-declared shape stops matching whenever the schema gains a
      // field, and the mismatch surfaces as `startTestRun does not exist on
      // type {}` at the call site rather than as an error here.
      const input: TestRunInput = {
        testSetId: testSetId as string,
        ...(reRunContext && { context: reRunContext }),
        ...(reRunNumberOfFiles.trim() && { numberOfFiles: parseInt(reRunNumberOfFiles.trim(), 10) }),
      };

      console.log('About to call GraphQL with input:', input);

      const result = await client.graphql({
        query: startTestRun,
        variables: { input },
      });

      console.log('GraphQL call completed, result:', result);

      if (result?.data?.startTestRun) {
        console.log('Success! Closing modal and redirecting...');
        const newTestRun = result.data.startTestRun;
        // Add to active test runs
        addTestRun(
          newTestRun.testRunId as string,
          newTestRun.testSetName as string,
          reRunContext,
          newTestRun.filesCount as number,
          newTestRun.configVersion || '',
        );
        setShowReRunModal(false);
        setReRunContext('');
        setReRunNumberOfFiles('');
        // Navigate to test executions tab
        window.location.hash = '#/test-studio?tab=executions';
      } else {
        console.error('No startTestRun data in result');
      }
    } catch (err) {
      console.error('GraphQL call failed:', err);
      const typedErr = err as { errors?: Array<{ message: string }> };
      if (typedErr.errors) {
        typedErr.errors.forEach((errorItem: { message: string }, index: number) => {
          console.error(`Error ${index}:`, errorItem.message);
        });
      }
    } finally {
      setReRunLoading(false);
    }
    console.log('=== handleReRun END ===');
  };

  const reRunButton = results?.testSetId ? (
    <Button
      onClick={() => {
        setShowReRunModal(true);
      }}
      iconName="arrow-right"
      disabled={!testSetFileCount || testSetFileCount === 0}
    >
      Re-Run
    </Button>
  ) : null;

  const configButton = (
    <Button onClick={openConfigExportModal} iconName="download">
      Config
    </Button>
  );

  const contextDescription = results.context ? (
    <Box variant="p" color="text-body-secondary" margin={{ top: 'xs' }}>
      Context: {String(results.context)}
    </Box>
  ) : null;

  const handleBackClick = () => {
    if (setSelectedTestRunId) {
      setSelectedTestRunId(null);
    } else {
      window.location.replace('#/test-studio?tab=executions');
    }
  };

  return (
    <Container
      header={
        <TestStudioHeader
          title={`Test Results: ${results.testRunId} (${results.testSetName})`}
          description={contextDescription}
          showPrintButton={true}
          additionalActions={[configButton, reRunButton].filter(Boolean)}
          onBackClick={handleBackClick}
        />
      }
    >
      <SpaceBetween direction="vertical" size="l">
        {/* Overall Status */}
        <Box>
          <Badge color={getStatusColor(results.status as string) as 'blue' | 'green' | 'grey' | 'red'}>{String(results.status)}</Badge>
          <Box margin={{ left: 's' }} display="inline">
            {String(results.completedFiles)}/{String(results.filesCount)} files processed
          </Box>
        </Box>

        {/* Test Results Alert */}
        {hasAccuracyData && (
          <Alert type="success" header="Test Results Available">
            Test run completed with accuracy and performance metrics
          </Alert>
        )}

        {/* A draft-labeling run has no baseline by construction — producing the
            labels is the point — so evaluation never runs and there are never
            accuracy metrics. Warning that they "are not available" described an
            expected outcome as a fault, on a screen already displaying "Context:
            Draft labeling run" two lines above. The server owns the rule and
            reports it as isDraftLabeling. */}
        {!hasAccuracyData && results.status === 'COMPLETE' && results.isDraftLabeling && (
          <Alert type="info" header="No accuracy metrics — this run produced labels rather than being scored">
            A draft-labeling run creates the ground truth, so there is nothing to score it against. Review the drafts in the test set, then
            run a test against the published version to get accuracy metrics.
          </Alert>
        )}

        {!hasAccuracyData && results.status === 'COMPLETE' && !results.isDraftLabeling && (
          <Alert type="warning" header="No Accuracy Data">
            Test run completed but accuracy metrics are not available. This usually means the test set had no published ground truth to
            score against.
          </Alert>
        )}

        {/* Key Metrics */}
        <ColumnLayout columns={6} variant="text-grid">
          <Box>
            <Box variant="awsui-key-label">Total Cost</Box>
            <Box fontSize="heading-l">
              {results.totalCost !== null && results.totalCost !== undefined ? `$${(results.totalCost as number).toFixed(4)}` : 'N/A'}
            </Box>
          </Box>
          <Box>
            <Box variant="awsui-key-label">Avg Cost/Page</Box>
            <Box fontSize="heading-l">{avgCostPerPage !== null ? `$${avgCostPerPage.toFixed(4)}` : 'N/A'}</Box>
          </Box>
          <Box>
            <Box variant="awsui-key-label">
              Avg Confidence
              <MetricInfo metric="Avg Confidence" />
            </Box>
            <Box fontSize="heading-l">
              {results.averageConfidence !== null && results.averageConfidence !== undefined
                ? `${((results.averageConfidence as number) * 100).toFixed(1)}%`
                : 'N/A'}
            </Box>
          </Box>
          <Box>
            <Box variant="awsui-key-label">
              Avg Accuracy
              <MetricInfo metric="Avg Accuracy" />
            </Box>
            <Box fontSize="heading-l">
              {results.overallAccuracy !== null && results.overallAccuracy !== undefined
                ? (results.overallAccuracy as number).toFixed(3)
                : 'N/A'}
            </Box>
          </Box>
          <Box>
            <Box variant="awsui-key-label">
              Avg Weighted Score
              <MetricInfo metric="Avg Weighted Score" />
            </Box>
            <Box fontSize="heading-l">{averageWeightedScore !== null ? averageWeightedScore.toFixed(3) : 'N/A'}</Box>
          </Box>
          {/* Excluded Docs KPI — surfaced only when a run has no-op documents
              (class has no extractable schema). Hidden on the happy path so
              the KPI row stays lean for the common case; visible with a
              count + info icon whenever the excluded set is non-empty so the
              drop from filesCount is explicit. */}
          {Number(results.excludedDocumentCount ?? 0) > 0 && (
            <Box>
              <Box variant="awsui-key-label">
                Excluded Docs
                <MetricInfo metric="Excluded Docs" />
              </Box>
              <Box fontSize="heading-l">{Number(results.excludedDocumentCount)}</Box>
            </Box>
          )}
          <Box>
            <Box variant="awsui-key-label">Duration</Box>
            <Box fontSize="heading-l">
              {results.createdAt && results.completedAt
                ? (() => {
                    const duration = new Date(results.completedAt as string).getTime() - new Date(results.createdAt as string).getTime();
                    const minutes = Math.floor(duration / 60000);
                    const seconds = Math.floor((duration % 60000) / 1000);
                    return minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`;
                  })()
                : 'N/A'}
            </Box>
          </Box>
          {Boolean(results.configVersion) && (
            <Box>
              <Box variant="awsui-key-label">Config Profile</Box>
              <Box fontSize="heading-l">{formatConfigVersionLink(results.configVersion as string, versions)}</Box>
            </Box>
          )}
        </ColumnLayout>

        {/* Weighted Overall Scores Distribution Chart */}
        {results.weightedOverallScores && Object.keys(results.weightedOverallScores).length > 1 && (
          <Container
            header={
              <Header
                variant="h3"
                actions={
                  <Select
                    selectedOption={chartType}
                    onChange={({ detail }) => setChartType(detail.selectedOption)}
                    options={[
                      { label: 'Bar Chart', value: 'bar' },
                      { label: 'Line Chart', value: 'line' },
                    ]}
                    placeholder="Select chart type"
                  />
                }
                description={(() => {
                  // Chart-header subtitle: "N documents · X scored · Y excluded".
                  // Total-only ("N documents") when nothing is excluded, so the
                  // subtitle still gives useful context on the happy path without
                  // shouting about an exclusion condition that doesn't exist.
                  const scored = results.weightedOverallScores
                    ? Object.keys(parseWeightedOverallScoresFinite(results.weightedOverallScores as string)).length
                    : 0;
                  const excluded = Number(results.excludedDocumentCount ?? 0);
                  const total = scored + excluded;
                  if (total === 0) return undefined;
                  if (excluded === 0) {
                    return `${total} document${total === 1 ? '' : 's'}`;
                  }
                  return `${total} documents · ${scored} scored · ${excluded} excluded`;
                })()}
              >
                Weighted Overall Score Distribution ({String(results.testRunId)})
              </Header>
            }
          >
            {(() => {
              const generateChartData = () => {
                // ``parseWeightedOverallScoresFinite`` drops excluded / null / NaN
                // entries — otherwise ``null < 0.1`` in JS silently lands them
                // in the 0.0-0.1 bucket.
                const scores =
                  typeof results.weightedOverallScores === 'string'
                    ? parseWeightedOverallScoresFinite(results.weightedOverallScores)
                    : parseWeightedOverallScoresFinite(results.weightedOverallScores);

                // Create score range buckets
                const buckets: Record<string, { count: number; docs: RangeDoc[] }> = {
                  '0.0-0.1': { count: 0, docs: [] },
                  '0.1-0.2': { count: 0, docs: [] },
                  '0.2-0.3': { count: 0, docs: [] },
                  '0.3-0.4': { count: 0, docs: [] },
                  '0.4-0.5': { count: 0, docs: [] },
                  '0.5-0.6': { count: 0, docs: [] },
                  '0.6-0.7': { count: 0, docs: [] },
                  '0.7-0.8': { count: 0, docs: [] },
                  '0.8-0.9': { count: 0, docs: [] },
                  '0.9-1.0': { count: 0, docs: [] },
                };

                Object.entries(scores).forEach(([docId, score]) => {
                  let bucket;
                  if (score < 0.1) bucket = '0.0-0.1';
                  else if (score < 0.2) bucket = '0.1-0.2';
                  else if (score < 0.3) bucket = '0.2-0.3';
                  else if (score < 0.4) bucket = '0.3-0.4';
                  else if (score < 0.5) bucket = '0.4-0.5';
                  else if (score < 0.6) bucket = '0.5-0.6';
                  else if (score < 0.7) bucket = '0.6-0.7';
                  else if (score < 0.8) bucket = '0.7-0.8';
                  else if (score < 0.9) bucket = '0.8-0.9';
                  else bucket = '0.9-1.0';

                  buckets[bucket].count++;
                  buckets[bucket].docs.push({ docId, score });
                });

                let maxCount = 0;
                const mappedData = Object.entries(buckets).map(([range, data]) => {
                  if (data.count > maxCount) {
                    maxCount = data.count;
                  }

                  const sortedDocs = data.docs.sort((a, b) => b.score - a.score);
                  const topDocs = sortedDocs.slice(0, 3);

                  let tooltip = `${data.count} documents in range ${range}\n\n`;
                  topDocs.forEach((doc) => {
                    tooltip += `• ${doc.docId} (${doc.score?.toFixed(3)})\n`;
                  });
                  if (data.docs.length > 3) {
                    tooltip += `\n...and ${data.docs.length - 3} more documents`;
                  }

                  return {
                    x: range,
                    y: data.count,
                    tooltip: tooltip,
                  };
                });

                return { mappedData, maxCount, buckets };
              };

              const { mappedData, maxCount: _maxCount, buckets } = generateChartData();

              const chartData = mappedData.map((item) => ({
                range: item.x,
                count: item.y,
                tooltip: item.tooltip,
              }));

              return (
                <ResponsiveContainer width="100%" height={320}>
                  {chartType.value === 'bar' ? (
                    <BarChart data={chartData} margin={{ top: 20, right: 20, left: 20, bottom: 20 }}>
                      <CartesianGrid strokeDasharray="3 3" />
                      <XAxis
                        dataKey="range"
                        angle={-45}
                        textAnchor="end"
                        height={55}
                        interval={0}
                        label={{ value: 'Weighted Overall Score Range', position: 'insideBottom', offset: -8 }}
                      />
                      <YAxis
                        label={{ value: 'Number of Documents', angle: -90, position: 'insideLeft', style: { textAnchor: 'middle' } }}
                      />
                      <Tooltip
                        formatter={(value, _name) => [value, 'Number of Documents']}
                        labelFormatter={(label) => `Score Range: ${label}`}
                      />
                      <Bar
                        dataKey="count"
                        fill="#0073bb"
                        // eslint-disable-next-line @typescript-eslint/no-explicit-any
                        onClick={(data: any) => {
                          const range = data.range as string;
                          if (range && buckets[range] && buckets[range].docs.length > 0) {
                            const docs = buckets[range].docs.sort((a, b) => b.score - a.score);
                            setSelectedRangeData({ range, docs });
                            setTimeout(() => {
                              setShowDocumentsModal(true);
                            }, 0);
                          }
                        }}
                      />
                    </BarChart>
                  ) : (
                    <LineChart data={chartData} margin={{ top: 20, right: 20, left: 20, bottom: 20 }}>
                      <CartesianGrid strokeDasharray="3 3" />
                      <XAxis
                        dataKey="range"
                        angle={-45}
                        textAnchor="end"
                        height={55}
                        interval={0}
                        label={{ value: 'Weighted Overall Score Range', position: 'insideBottom', offset: -8 }}
                      />
                      <YAxis
                        label={{ value: 'Number of Documents', angle: -90, position: 'insideLeft', style: { textAnchor: 'middle' } }}
                      />
                      <Tooltip
                        formatter={(value, _name) => [value, 'Number of Documents']}
                        labelFormatter={(label) => `Score Range: ${label}`}
                      />
                      <Line
                        type="monotone"
                        dataKey="count"
                        stroke="#0073bb"
                        strokeWidth={2}
                        dot={{ fill: '#0073bb', strokeWidth: 2, r: 4, cursor: 'pointer' }}
                        activeDot={{
                          r: 6,
                          cursor: 'pointer',
                          // eslint-disable-next-line @typescript-eslint/no-explicit-any
                          onClick: (data: any) => {
                            const range = data.payload.range as string;
                            if (range && buckets[range] && buckets[range].docs.length > 0) {
                              const docs = buckets[range].docs.sort((a: RangeDoc, b: RangeDoc) => b.score - a.score);
                              setSelectedRangeData({ range, docs });
                              setTimeout(() => {
                                setShowDocumentsModal(true);
                              }, 0);
                            }
                          },
                        }}
                      />
                    </LineChart>
                  )}
                </ResponsiveContainer>
              );
            })()}
          </Container>
        )}

        {/* Lowest Scoring Documents Table. Suppressed for a draft-labeling run:
            nothing was scored, so the table rendered its headers over an empty
            body — directly under an alert explaining that there are no scores. */}
        {results?.weightedOverallScores && !results.isDraftLabeling && (
          <Container
            header={
              <Header
                actions={
                  <Select
                    selectedOption={lowestScoreCount}
                    onChange={({ detail }) => setLowestScoreCount(detail.selectedOption)}
                    options={[
                      { label: '5', value: '5' },
                      { label: '10', value: '10' },
                      { label: '20', value: '20' },
                      { label: '50', value: '50' },
                    ]}
                    placeholder="Select count"
                  />
                }
              >
                Documents with Lowest Weighted Overall Scores
              </Header>
            }
          >
            {(() => {
              // ``parseWeightedOverallScoresFinite`` drops excluded / null / NaN
              // entries so a stray null can't sort to the front and render
              // as "null.toFixed(3)".
              const scores =
                typeof results.weightedOverallScores === 'string'
                  ? parseWeightedOverallScoresFinite(results.weightedOverallScores)
                  : parseWeightedOverallScoresFinite(results.weightedOverallScores);

              const sortedDocs = Object.entries(scores)
                .map(([docId, score]) => ({ docId, score }))
                .sort((a, b) => a.score - b.score)
                .slice(0, Number(lowestScoreCount.value));

              return (
                <Table
                  resizableColumns
                  items={sortedDocs}
                  columnDefinitions={[
                    {
                      id: 'docId',
                      header: 'Document ID',
                      cell: (item) => (
                        <Button
                          variant="link"
                          onClick={() => {
                            const urlPath = item.docId.replace(/\//g, '%252F');
                            window.open(`#/documents/${urlPath}`, '_blank');
                          }}
                        >
                          {item.docId}
                        </Button>
                      ),
                    },
                    {
                      id: 'score',
                      header: 'Weighted Overall Score',
                      cell: (item) => (item.score as number).toFixed(3),
                    },
                  ]}
                  variant="embedded"
                  contentDensity="compact"
                />
              );
            })()}
          </Container>
        )}

        {/* Above the breakdown tables on purpose: a wrong class invalidates the
            field numbers below it, so it should be read first. */}
        <ClassificationErrorsPanel classificationErrors={classificationErrors} testSetId={results.testSetId as string | undefined} />

        {/* Breakdown Tables. A draft-labeling run keeps its COST breakdown — it
            spends real money and that is worth seeing — but not the accuracy
            tables, which the backend fills with structural zeros. "Classification:
            Page Level Accuracy 0.000" on a run that scored nothing asserts total
            failure where the honest answer is "not applicable". */}
        {(costBreakdown || accuracyBreakdown || splitClassificationMetrics || gradedPacketMetrics || fieldMetrics) && (
          <ComprehensiveBreakdown
            costBreakdown={costBreakdown}
            accuracyBreakdown={results.isDraftLabeling ? null : accuracyBreakdown}
            splitClassificationMetrics={results.isDraftLabeling ? null : splitClassificationMetrics}
            gradedPacketMetrics={results.isDraftLabeling ? null : gradedPacketMetrics}
            fieldMetrics={results.isDraftLabeling ? null : fieldMetrics}
            averageWeightedScore={results.isDraftLabeling ? null : averageWeightedScore}
            confidenceMetrics={results.isDraftLabeling ? null : results.confidenceMetrics}
          />
        )}
      </SpaceBetween>

      <Modal
        visible={showReRunModal}
        onDismiss={() => {
          setShowReRunModal(false);
          setReRunContext('');
          setReRunNumberOfFiles('');
        }}
        header="Re-Run Test"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                onClick={() => {
                  setShowReRunModal(false);
                  setReRunContext('');
                  setReRunNumberOfFiles('');
                }}
              >
                Cancel
              </Button>
              <Button variant="primary" onClick={handleReRun} loading={reRunLoading}>
                Re-Run Test
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Box>
            <strong>Test Set:</strong> {String(results?.testSetName || 'N/A')}
            <br />
            <strong>Pattern:</strong> {testSetStatus === 'NOT_FOUND' ? 'Test set not found' : testSetFilePattern || 'Uploaded files'}
            <br />
            <strong>Files:</strong>{' '}
            {testSetStatus === 'NOT_FOUND' ? 'Test set deleted' : testSetFileCount !== null ? `${testSetFileCount} files` : 'Loading...'}
          </Box>
          <FormField
            label="Number of Files"
            // Same reasoning as TestRunner: an unknown maximum must not render as
            // a maximum of zero.
            description={
              testSetFileCount
                ? `Optional: Limit the number of files to process (max: ${testSetFileCount})`
                : 'Optional: Limit the number of files to process.'
            }
          >
            <Input
              value={reRunNumberOfFiles}
              onChange={({ detail }) => {
                const value = detail.value;

                // Allow empty value
                if (value === '') {
                  setReRunNumberOfFiles('');
                  return;
                }

                // Only allow digits (reject any non-digit characters)
                if (!/^\d+$/.test(value)) {
                  return; // Don't update state if invalid characters
                }

                // Check range
                const num = parseInt(value, 10);
                if (num > 0 && num <= (testSetFileCount || 0)) {
                  setReRunNumberOfFiles(value);
                }
                // If number is too large, don't update the state (prevents typing)
              }}
              placeholder={`Enter 1-${testSetFileCount || 0}`}
              type="text"
              inputMode="numeric"
            />
          </FormField>
          <FormField label="Context" description="Optional context information for this test run">
            <Textarea
              value={reRunContext}
              onChange={({ detail }) => setReRunContext(detail.value)}
              placeholder="Enter context information..."
              rows={3}
            />
          </FormField>
        </SpaceBetween>
      </Modal>

      <Modal
        visible={showDocumentsModal}
        onDismiss={() => setShowDocumentsModal(false)}
        header={`Documents in Range ${selectedRangeData?.range || ''}`}
        size="medium"
      >
        <Box>
          {selectedRangeData && selectedRangeData.docs?.length > 0 ? (
            <Table
              resizableColumns
              items={selectedRangeData.docs}
              columnDefinitions={[
                {
                  id: 'docId',
                  header: 'Document ID',
                  cell: (item) => (
                    <Button
                      variant="link"
                      onClick={() => {
                        const urlPath = item.docId.replace(/\//g, '%252F');
                        window.open(`#/documents/${urlPath}`, '_blank');
                      }}
                    >
                      {item.docId}
                    </Button>
                  ),
                },
                {
                  id: 'score',
                  header: 'Score',
                  cell: (item) => item.score.toFixed(3),
                },
              ]}
              variant="embedded"
              contentDensity="compact"
            />
          ) : (
            <Box>No documents found in this range</Box>
          )}
        </Box>
      </Modal>

      <Modal
        visible={showConfigExportModal}
        onDismiss={() => setShowConfigExportModal(false)}
        header="Export Configuration"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShowConfigExportModal(false)}>
                Cancel
              </Button>
              <Button variant="primary" onClick={handleConfigExport}>
                Export
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween direction="vertical" size="l">
          <FormField label="File format">
            <RadioGroup
              value={configExportFormat}
              onChange={({ detail }) => setConfigExportFormat(detail.value)}
              items={[
                { value: 'json', label: 'JSON' },
                { value: 'yaml', label: 'YAML' },
              ]}
            />
          </FormField>
          <FormField label="File name">
            <Input
              value={configExportFileName}
              onChange={({ detail }) => setConfigExportFileName(detail.value)}
              placeholder="configuration"
            />
          </FormField>
        </SpaceBetween>
      </Modal>
    </Container>
  );
};

export default TestResults;
