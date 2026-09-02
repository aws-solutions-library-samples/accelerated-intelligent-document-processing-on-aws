// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useState, useEffect } from 'react';
import {
  Container,
  Header,
  SpaceBetween,
  Table,
  Box,
  Button,
  ButtonDropdown,
  ProgressBar,
  Select,
  CollectionPreferences,
  ExpandableSection,
} from '@cloudscape-design/components';
import { generateClient } from '../../api/client-shim';
import { compareTestRuns } from '../../graphql/generated';
import TestStudioHeader from './TestStudioHeader';
import useLocalStorage from '../common/local-storage';
import useConfigurationVersions from '../../hooks/use-configuration-versions';
import { formatConfigVersionLink, formatConfigVersionText, type ConfigVersion as UtilsConfigVersion } from './utils/configVersionUtils';
import MetricInfo, { ACCURACY_METRIC_MAP, SPLIT_METRIC_MAP } from './utils/MetricInfo';
import {
  parseComparisonMetrics,
  parseWeightedOverallScoresFinite,
  parseConfigSettingValues,
  calculateAvgCostPerPage,
  parseConfidenceMetrics,
} from '../../graphql/awsjson-parsers';
import type { CostBreakdown } from '../../graphql/awsjson-types';

const client = generateClient();

interface TestComparisonProps {
  preSelectedTestRunIds?: string[];
}

interface ComparisonData {
  metrics?: Record<string, Record<string, unknown>>;
  configs?: Array<{ setting: string; values: string | Record<string, string> }>;
  error?: string;
}

interface CostComparisonRow {
  context: string;
  serviceApi: string;
  unit: string;
  isSubtotal?: boolean;
  isTotal?: boolean;
  [testRunId: string]: string | boolean | undefined;
}

const TestComparison = ({ preSelectedTestRunIds = [] }: TestComparisonProps): React.JSX.Element => {
  const { versions } = useConfigurationVersions();
  const [comparisonData, setComparisonData] = useState<ComparisonData | null>(null);
  const [comparing, setComparing] = useState(false);
  const [currentAttempt, setCurrentAttempt] = useState(1);
  const [lowestScoreCount, setLowestScoreCount] = useState<Record<string, unknown>>({ label: '5', value: 5 });
  const [preferences, setPreferences] = useLocalStorage('test-comparison-preferences', { wrapLines: false });

  // Color grading functions
  const getCostColorGrade = (cost: unknown, allCosts: unknown[]): React.CSSProperties => {
    if (!cost || cost === 'N/A') return {};
    const numCost = typeof cost === 'string' ? parseFloat(cost.replace('$', '')) : cost;
    const validCosts = allCosts.filter((c) => c && c !== 'N/A').map((c) => (typeof c === 'string' ? parseFloat(c.replace('$', '')) : c));
    if (validCosts.length === 0) return {};

    const sortedCosts = [...validCosts].sort((a, b) => Number(a) - Number(b));
    const index = sortedCosts.indexOf(numCost);
    const ratio = validCosts.length === 1 ? 0 : index / (validCosts.length - 1);

    // Green → Yellow → Red gradient
    let r, g, b;
    if (ratio <= 0.5) {
      // Green to Yellow (first half)
      const t = ratio * 2;
      r = Math.round(34 + t * (255 - 34)); // 34 to 255
      g = Math.round(139 + t * (255 - 139)); // 139 to 255
      b = Math.round(34 + t * (0 - 34)); // 34 to 0
    } else {
      // Yellow to Red (second half)
      const t = (ratio - 0.5) * 2;
      r = 255; // Stay at 255
      g = Math.round(255 + t * (0 - 255)); // 255 to 0
      b = 0; // Stay at 0
    }

    // Use black text for light backgrounds (yellow range)
    const textColor = ratio > 0.3 && ratio < 0.7 ? 'black' : 'white';

    return { backgroundColor: `rgb(${r}, ${g}, ${b})`, color: textColor };
  };

  const getScoreColorGrade = (score: unknown, allScores: unknown[]): React.CSSProperties => {
    if (!score || score === 'N/A') return {};
    const numScore = typeof score === 'string' ? parseFloat(score) : score;
    const validScores = allScores.filter((s) => s && s !== 'N/A').map((s) => (typeof s === 'string' ? parseFloat(s) : s));
    if (validScores.length === 0) return {};

    const sortedScores = [...validScores].sort((a, b) => Number(b) - Number(a)); // Highest to lowest
    const index = sortedScores.indexOf(numScore);
    const ratio = validScores.length === 1 ? 0 : index / (validScores.length - 1);

    // Green → Yellow → Red gradient
    let r, g, b;
    if (ratio <= 0.5) {
      // Green to Yellow (first half)
      const t = ratio * 2;
      r = Math.round(34 + t * (255 - 34)); // 34 to 255
      g = Math.round(139 + t * (255 - 139)); // 139 to 255
      b = Math.round(34 + t * (0 - 34)); // 34 to 0
    } else {
      // Yellow to Red (second half)
      const t = (ratio - 0.5) * 2;
      r = 255; // Stay at 255
      g = Math.round(255 + t * (0 - 255)); // 255 to 0
      b = 0; // Stay at 0
    }

    // Use black text for light backgrounds (yellow range)
    const textColor = ratio > 0.3 && ratio < 0.7 ? 'black' : 'white';

    return { backgroundColor: `rgb(${r}, ${g}, ${b})`, color: textColor };
  };

  const TestComparisonPreferences = ({
    preferences: prefs,
    setPreferences: setPrefs,
  }: {
    preferences: { wrapLines: boolean };
    setPreferences: (prefs: { wrapLines: boolean }) => void;
  }): React.JSX.Element => (
    <CollectionPreferences
      title="Preferences"
      confirmLabel="Confirm"
      cancelLabel="Cancel"
      preferences={prefs}
      onConfirm={({ detail }) => setPrefs(detail as { wrapLines: boolean })}
      wrapLinesPreference={{
        label: 'Wrap lines',
        description: 'Check to see all the text and wrap the lines',
      }}
    />
  );

  useEffect(() => {
    const fetchComparison = async () => {
      if (preSelectedTestRunIds.length >= 2) {
        setComparing(true);
        console.log('=== STARTING COMPARISON ===');
        console.log('Selected test run IDs:', preSelectedTestRunIds);

        try {
          console.log('Making GraphQL request...');
          let result;
          let attempt = 1;
          const maxRetries = 5;

          while (attempt <= maxRetries) {
            try {
              setCurrentAttempt(attempt);
              result = await client.graphql({
                query: compareTestRuns,
                variables: { testRunIds: preSelectedTestRunIds },
              });
              setCurrentAttempt(5); // Set to 100% before completing
              await new Promise((resolve) => setTimeout(resolve, 500)); // Brief pause to show 100%
              break;
            } catch (error) {
              const err = error as { message?: string; code?: string; name?: string; errors?: { errorType?: string; message?: string }[] };
              const isTimeout =
                err.message?.toLowerCase().includes('timeout') ||
                err.code === 'TIMEOUT' ||
                err.message?.includes('Request failed with status code 504') ||
                err.name === 'TimeoutError' ||
                err.code === 'NetworkError' ||
                err.errors?.some(
                  (e: { errorType?: string; message?: string }) =>
                    e.errorType === 'Lambda:ExecutionTimeoutException' || e.message?.toLowerCase().includes('timeout'),
                );
              if (isTimeout && attempt < maxRetries) {
                console.log(`compareTestRuns attempt ${attempt} failed, retrying...`, err.message);
                attempt++;

                // Animate progress during 5-second wait
                const waitTime = 5000;
                const intervalTime = 100;
                const steps = waitTime / intervalTime;
                const startProgress = (attempt - 1) * 20;
                const endProgress = attempt * 20;
                const progressStep = (endProgress - startProgress) / steps;

                let currentProgress = startProgress;
                const progressInterval = setInterval(() => {
                  currentProgress += progressStep;
                  setCurrentAttempt(Math.min(currentProgress / 20, 5));
                }, intervalTime);

                await new Promise((resolve) =>
                  setTimeout(() => {
                    clearInterval(progressInterval);
                    setCurrentAttempt(attempt);
                    resolve(undefined as unknown as void);
                  }, waitTime),
                );

                continue;
              }
              throw error;
            }
          }

          const compareData = result?.data.compareTestRuns;

          // Parse metrics if it's a JSON string
          const parsedData: ComparisonData = {
            configs: (compareData?.configs ?? undefined) as ComparisonData['configs'],
            metrics:
              typeof compareData?.metrics === 'string'
                ? (parseComparisonMetrics(compareData.metrics) as Record<string, Record<string, unknown>>)
                : (compareData?.metrics as Record<string, Record<string, unknown>> | undefined),
          };

          setComparisonData(parsedData);
        } catch (error) {
          console.error('Error comparing test runs:', error);
          const typedError = error as { errors?: Array<{ message: string }>; message?: string };
          const errorMessage =
            typedError.errors?.length && typedError.errors.length > 0
              ? typedError.errors.map((e: { message: string }) => e.message).join('; ')
              : typedError.message || 'Error comparing test runs';
          setComparisonData({ error: errorMessage });
        } finally {
          setComparing(false);
        }
      }
    };

    fetchComparison();
  }, [preSelectedTestRunIds]);

  // Helper function to create clickable test run ID headers
  const createTestRunHeader = (testRunId: string, truncate = false): React.JSX.Element => {
    const displayId = testRunId;
    const _testSetName = truncate ? completeTestRuns[testRunId]?.testSetName : null;

    if (truncate) {
      return (
        <span
          title={testRunId}
          style={{ cursor: 'pointer', color: '#0073bb' }}
          role="button"
          tabIndex={0}
          onClick={() => {
            window.location.hash = `#/test-studio?tab=results&testRunId=${testRunId}`;
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              window.location.hash = `#/test-studio?tab=results&testRunId=${testRunId}`;
            }
          }}
        >
          {displayId}
        </span>
      );
    }

    return (
      <Button
        variant="link"
        onClick={() => {
          window.location.hash = `#/test-studio?tab=results&testRunId=${testRunId}`;
        }}
        {...({ title: testRunId } as Record<string, unknown>)}
      >
        {displayId}
      </Button>
    );
  };

  const downloadToCsv = () => {
    if (!comparisonData || !comparisonData.metrics) return;

    const completeTestRuns: Record<string, Record<string, unknown>> = Object.fromEntries(
      Object.entries(comparisonData.metrics).filter(
        ([, testRun]) => testRun.status === 'COMPLETE' || testRun.status === 'PARTIAL_COMPLETE',
      ),
    );

    // Create headers
    const headers = ['Metric', ...Object.keys(completeTestRuns)];

    // Create rows for performance metrics
    const performanceRows = [
      ['Test Set', ...Object.values(completeTestRuns).map((run) => run.testSetName || 'N/A')],
      ['Context', ...Object.values(completeTestRuns).map((run) => run.context || 'N/A')],
      [
        'Config Profile',
        ...Object.values(completeTestRuns).map((run) =>
          formatConfigVersionText(
            run.configVersion as string | undefined,
            versions as unknown as UtilsConfigVersion[],
            run.configRevision as number | undefined,
          ),
        ),
      ],
      ['Files Processed', ...Object.values(completeTestRuns).map((run) => run.filesCount || 'N/A')],
      ['Files Completed', ...Object.values(completeTestRuns).map((run) => run.completedFiles || 'N/A')],
      ['Files Failed', ...Object.values(completeTestRuns).map((run) => run.failedFiles || 'N/A')],
      [
        'Total Cost',
        ...Object.values(completeTestRuns).map((run) =>
          run.totalCost !== null && run.totalCost !== undefined ? `$${Number(run.totalCost).toFixed(4)}` : 'N/A',
        ),
      ],
      [
        'Avg Cost/Page',
        ...Object.values(completeTestRuns).map((run) => {
          const avg = calculateAvgCostPerPage(run.totalCost as number, run.costBreakdown as CostBreakdown);
          return avg !== null ? `$${avg.toFixed(4)}` : 'N/A';
        }),
      ],
      [
        <>
          Average Accuracy
          <MetricInfo metric="Avg Accuracy" />
        </>,
        ...Object.values(completeTestRuns).map((run) =>
          run.overallAccuracy !== null && run.overallAccuracy !== undefined ? Number(run.overallAccuracy).toFixed(3) : 'N/A',
        ),
      ],
      [
        <>
          Average Confidence
          <MetricInfo metric="Avg Confidence" />
        </>,
        ...Object.values(completeTestRuns).map((run) =>
          run.averageConfidence !== null && run.averageConfidence !== undefined
            ? `${(Number(run.averageConfidence) * 100).toFixed(1)}%`
            : 'N/A',
        ),
      ],
      [
        <>
          Confidence AUROC
          <MetricInfo metric="AUROC" />
        </>,
        ...Object.values(completeTestRuns).map((run) => {
          if (run.confidenceMetrics) {
            const parsed = parseConfidenceMetrics(run.confidenceMetrics);
            const auroc = parsed?.overall?.auroc?.value;
            return auroc !== null && auroc !== undefined ? Number(auroc).toFixed(3) : 'N/A';
          }
          return 'N/A';
        }),
      ],
      [
        <>
          Confidence ECE
          <MetricInfo metric="ECE" />
        </>,
        ...Object.values(completeTestRuns).map((run) => {
          if (run.confidenceMetrics) {
            const parsed = parseConfidenceMetrics(run.confidenceMetrics);
            const ece = parsed?.overall?.ece?.value;
            return ece !== null && ece !== undefined ? Number(ece).toFixed(3) : 'N/A';
          }
          return 'N/A';
        }),
      ],
      [
        <>
          Confidence Brier
          <MetricInfo metric="Brier" />
        </>,
        ...Object.values(completeTestRuns).map((run) => {
          if (run.confidenceMetrics) {
            const parsed = parseConfidenceMetrics(run.confidenceMetrics);
            const brier = parsed?.overall?.brier?.value;
            return brier !== null && brier !== undefined ? Number(brier).toFixed(3) : 'N/A';
          }
          return 'N/A';
        }),
      ],
      [
        <>
          ECARB@30
          <MetricInfo metric="ECARB@30" />
        </>,
        ...Object.values(completeTestRuns).map((run) => {
          if (run.confidenceMetrics) {
            const parsed = parseConfidenceMetrics(run.confidenceMetrics);
            const ecab = (
              parsed?.overall?.error_capture_at_budget as { budgets?: Record<string, { pct_errors_caught?: number; gain?: number }> }
            )?.budgets?.['0.30'];
            if (
              ecab &&
              ecab.pct_errors_caught !== null &&
              ecab.pct_errors_caught !== undefined &&
              ecab.gain !== null &&
              ecab.gain !== undefined
            ) {
              return `${(ecab.pct_errors_caught * 100).toFixed(0)}% (${ecab.gain.toFixed(1)}x)`;
            }
          }
          return 'N/A';
        }),
      ],
      [
        'Confidence Coverage',
        ...Object.values(completeTestRuns).map((run) => {
          if (run.confidenceMetrics) {
            const parsed = parseConfidenceMetrics(run.confidenceMetrics);
            const coverage = parsed?.coverage;
            if (coverage && coverage.ratio !== null && coverage.ratio !== undefined) {
              return `${(coverage.ratio * 100).toFixed(1)}% (${coverage.fields_with_confidence}/${coverage.fields_total})`;
            }
          }
          return 'N/A';
        }),
      ],
      [
        <>
          Average Weighted Overall Score
          <MetricInfo metric="Avg Weighted Score" />
        </>,
        ...Object.values(completeTestRuns).map((run) => {
          if (run.weightedOverallScores) {
            // ``parseWeightedOverallScoresFinite`` drops excluded / null / NaN
            // entries so they can't pull the mean toward 0.
            const values = Object.values(parseWeightedOverallScoresFinite(run.weightedOverallScores as string));
            if (values.length > 0) {
              const avg = values.reduce((sum: number, score: number) => sum + score, 0) / values.length;
              return avg.toFixed(3);
            }
          }
          return 'N/A';
        }),
      ],
      [
        'Duration',
        ...Object.values(completeTestRuns).map((run) => {
          if (run.createdAt && run.completedAt) {
            const duration = new Date(run.completedAt as string).getTime() - new Date(run.createdAt as string).getTime();
            const minutes = Math.floor(duration / 60000);
            const seconds = Math.floor((duration % 60000) / 1000);
            return minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`;
          }
          return 'N/A';
        }),
      ],
    ];

    // Add cost breakdown rows
    const costRows: string[][] = [];
    const allCostItems = new Set<string>();

    Object.values(completeTestRuns).forEach((testRun) => {
      if (testRun.costBreakdown) {
        Object.entries(testRun.costBreakdown as Record<string, Record<string, unknown>>).forEach(([context, services]) => {
          Object.keys(services).forEach((serviceUnit) => {
            // Parse service/api_unit format: find last underscore to separate unit
            const lastUnderscoreIndex = serviceUnit.lastIndexOf('_');
            const serviceApi = serviceUnit.substring(0, lastUnderscoreIndex);
            const unit = serviceUnit.substring(lastUnderscoreIndex + 1);
            const [service, ...apiParts] = serviceApi.split('/');
            const api = apiParts.join('/');
            allCostItems.add(`${context}|${service}/${api}|${unit}`);
          });
        });
      }
    });

    // Add cost breakdown header
    costRows.push(['Context', 'Service/Api', 'Unit', ...Object.keys(completeTestRuns)]);

    const sortedCostItems = Array.from(allCostItems).sort();
    const contexts = [...new Set(sortedCostItems.map((item) => String(item).split('|')[0]))];

    contexts.forEach((context) => {
      const contextItems = sortedCostItems.filter((item) => String(item).startsWith(`${context}|`));

      // Add context items
      contextItems.forEach((itemKey) => {
        const [ctx, serviceApi, unit] = String(itemKey).split('|');
        const row = [ctx, serviceApi, unit];

        Object.entries(completeTestRuns).forEach(([_testRunId, testRun]) => {
          const costBd = testRun.costBreakdown as Record<string, Record<string, Record<string, unknown>>> | undefined;
          const services = costBd?.[ctx] || {};
          const serviceKey = Object.keys(services).find((key) => {
            const lastUnderscoreIndex = key.lastIndexOf('_');
            const keyServiceApi = key.substring(0, lastUnderscoreIndex);
            const keyUnit = key.substring(lastUnderscoreIndex + 1);
            return keyServiceApi === serviceApi && keyUnit === unit;
          });

          const details = (serviceKey ? services[serviceKey] : {}) as Record<string, unknown>;
          const estimatedCost = Number(details.estimated_cost) || 0;
          row.push(estimatedCost > 0 ? `$${estimatedCost.toFixed(4)}` : 'N/A');
        });

        costRows.push(row);
      });

      // Add subtotal row
      const subtotalRow = ['', `${context} Subtotal`, ''];
      Object.keys(completeTestRuns).forEach((testRunId) => {
        const contextTotal = contextItems.reduce((sum, itemKey) => {
          const [ctx, serviceApi, unit] = String(itemKey).split('|');
          const costBd = completeTestRuns[testRunId].costBreakdown as Record<string, Record<string, Record<string, unknown>>> | undefined;
          const services = costBd?.[ctx] || {};
          const serviceKey = Object.keys(services).find((key) => {
            const lastUnderscoreIndex = key.lastIndexOf('_');
            const keyServiceApi = key.substring(0, lastUnderscoreIndex);
            const keyUnit = key.substring(lastUnderscoreIndex + 1);
            return keyServiceApi === serviceApi && keyUnit === unit;
          });
          const details = (serviceKey ? services[serviceKey] : {}) as Record<string, unknown>;
          const estimatedCost = Number(details.estimated_cost) || 0;
          return sum + estimatedCost;
        }, 0);
        subtotalRow.push(`$${Number(contextTotal).toFixed(4)}`);
      });
      costRows.push(subtotalRow);
    });

    // Add total row
    const totalRow = ['', 'Total', ''];
    Object.keys(completeTestRuns).forEach((testRunId) => {
      const grandTotal = sortedCostItems.reduce((sum, itemKey) => {
        const [context, serviceApi, unit] = String(itemKey).split('|');
        const costBd = completeTestRuns[testRunId].costBreakdown as Record<string, Record<string, Record<string, unknown>>> | undefined;
        const services = costBd?.[context] || {};
        const serviceKey = Object.keys(services).find((key) => {
          const lastUnderscoreIndex = key.lastIndexOf('_');
          const keyServiceApi = key.substring(0, lastUnderscoreIndex);
          const keyUnit = key.substring(lastUnderscoreIndex + 1);
          return keyServiceApi === serviceApi && keyUnit === unit;
        });
        const details = (serviceKey ? services[serviceKey] : {}) as Record<string, unknown>;
        const estimatedCost = Number(details.estimated_cost) || 0;
        return sum + estimatedCost;
      }, 0);
      totalRow.push(`$${Number(grandTotal).toFixed(4)}`);
    });
    costRows.push(totalRow);

    // Add usage breakdown rows
    const usageRows: string[][] = [];
    usageRows.push(['Context', 'Service/Api', 'Unit', ...Object.keys(completeTestRuns)]);

    Array.from(allCostItems)
      .sort()
      .forEach((itemKey) => {
        const [context, serviceApi, unit] = String(itemKey).split('|');
        const row = [context, serviceApi, unit];

        Object.entries(completeTestRuns).forEach(([_testRunId, testRun]) => {
          const costBd = testRun.costBreakdown as Record<string, Record<string, Record<string, unknown>>> | undefined;
          const services = costBd?.[context] || {};
          const serviceKey = Object.keys(services).find((key) => {
            const lastUnderscoreIndex = key.lastIndexOf('_');
            const keyServiceApi = key.substring(0, lastUnderscoreIndex);
            const keyUnit = key.substring(lastUnderscoreIndex + 1);
            return keyServiceApi === serviceApi && keyUnit === unit;
          });

          const details = (serviceKey ? services[serviceKey] : {}) as Record<string, unknown>;
          const value = Number(details.value) || 0;
          row.push(value > 0 ? value.toLocaleString() : 'N/A');
        });

        usageRows.push(row);
      });

    // Add accuracy breakdown rows
    const accuracyRows: string[][] = [];
    const allAccuracyMetrics = new Set<string>();
    Object.values(completeTestRuns).forEach((testRun) => {
      if (testRun.accuracyBreakdown) {
        Object.keys(testRun.accuracyBreakdown as Record<string, unknown>).forEach((metric) => {
          allAccuracyMetrics.add(metric);
        });
      }
    });

    // Add accuracy breakdown header
    accuracyRows.push(['Accuracy Metric', ...Object.keys(completeTestRuns)]);

    // Add accuracy breakdown metrics
    Array.from(allAccuracyMetrics).forEach((metricKey) => {
      const row = [
        String(metricKey)
          .replace(/_/g, ' ')
          .replace(/\b\w/g, (l) => l.toUpperCase()),
      ];
      Object.entries(completeTestRuns).forEach(([_testRunId, testRun]) => {
        const accuracyBreakdown = (testRun.accuracyBreakdown || {}) as Record<string, number>;
        const value = accuracyBreakdown[metricKey as string];
        const displayValue = value !== null && value !== undefined ? Number(value).toFixed(3) : '0.000';
        row.push(displayValue);
      });
      accuracyRows.push(row);
    });

    // Add weighted overall score to accuracy breakdown. Excluded docs
    // (no extractable schema) are dropped by ``parseWeightedOverallScoresFinite``.
    const weightedRow = ['Weighted Overall Score'];
    Object.entries(completeTestRuns).forEach(([_testRunId, testRun]) => {
      if (testRun.weightedOverallScores) {
        const values = Object.values(parseWeightedOverallScoresFinite(testRun.weightedOverallScores as string));
        if (values.length > 0) {
          const avg = values.reduce((sum: number, score: number) => sum + score, 0) / values.length;
          weightedRow.push(avg.toFixed(3));
        } else {
          weightedRow.push('N/A');
        }
      } else {
        weightedRow.push('N/A');
      }
    });
    accuracyRows.push(weightedRow);

    // Add config comparison rows
    const configRows: string[][] = [];
    if (comparisonData.configs && comparisonData.configs.length > 0) {
      comparisonData.configs.forEach((config) => {
        const values = parseConfigSettingValues(config.values as string);
        const row = [config.setting, ...Object.keys(completeTestRuns).map((testRunId) => String(values[testRunId] ?? 'N/A'))];
        configRows.push(row);
      });
    }

    // Add field metrics rows (only when same test set)
    const fieldMetricsRows: string[][] = [];
    const testSetNames = new Set(Object.values(completeTestRuns).map((r) => r.testSetName));
    const hasFieldMetrics = Object.values(completeTestRuns).some((r) => r.fieldMetrics);

    // Parse confidence metrics for CSV export (outside if block for later reference)
    const confidenceDataMapCsv: Record<string, ReturnType<typeof parseConfidenceMetrics>> = {};
    let hasConfidenceDataCsv = false;

    if (testSetNames.size === 1 && hasFieldMetrics) {
      Object.entries(completeTestRuns).forEach(([testRunId, testRun]) => {
        if (testRun.confidenceMetrics) {
          const parsed = parseConfidenceMetrics(testRun.confidenceMetrics);
          if (parsed) {
            confidenceDataMapCsv[testRunId] = parsed;
          }
        }
      });

      hasConfidenceDataCsv = Object.values(confidenceDataMapCsv).some((data) => data && data.fields && Object.keys(data.fields).length > 0);

      const allFields = new Set<string>();
      Object.values(completeTestRuns).forEach((r) => {
        if (r.fieldMetrics) Object.keys(r.fieldMetrics as Record<string, unknown>).forEach((f) => allFields.add(f));
      });

      // Create header row - merge confidence metrics into same column
      const headerRow = ['Field Name'];
      Object.keys(completeTestRuns).forEach((testRunId) => {
        if (hasConfidenceDataCsv) {
          headerRow.push(`${testRunId} (Acc/Prec/Rec/AUROC/ECE/Brier)`);
        } else {
          headerRow.push(`${testRunId} (Acc/Prec/Rec)`);
        }
      });
      fieldMetricsRows.push(headerRow);

      Array.from(allFields)
        .sort()
        .forEach((fieldName) => {
          const row = [fieldName];
          Object.entries(completeTestRuns).forEach(([testRunId, testRun]) => {
            const fm = testRun.fieldMetrics as Record<string, Record<string, number>> | undefined;
            const m = fm?.[fieldName];
            let cellValue = '';

            if (m) {
              const tp = m.tp ?? 0,
                fp = m.fp ?? 0,
                tn = m.tn ?? 0,
                fn = m.fn ?? 0;
              const total = tp + fp + tn + fn;
              const acc = total > 0 ? ((tp + tn) / total).toFixed(3) : 'N/A';
              const prec = tp + fp > 0 ? (tp / (tp + fp)).toFixed(3) : 'N/A';
              const rec = tp + fn > 0 ? (tp / (tp + fn)).toFixed(3) : 'N/A';
              cellValue = `${acc} / ${prec} / ${rec}`;
            } else {
              cellValue = 'N/A / N/A / N/A';
            }

            // Merge confidence metrics into same column if available
            if (hasConfidenceDataCsv) {
              const confData = confidenceDataMapCsv[testRunId];
              const fieldConf = confData?.fields?.[fieldName];

              const auroc =
                fieldConf?.auroc?.value !== undefined && fieldConf.auroc.value !== null ? fieldConf.auroc.value.toFixed(3) : 'N/A';
              const ece = fieldConf?.ece?.value !== undefined && fieldConf.ece.value !== null ? fieldConf.ece.value.toFixed(3) : 'N/A';
              const brier =
                fieldConf?.brier?.value !== undefined && fieldConf.brier.value !== null ? fieldConf.brier.value.toFixed(3) : 'N/A';

              cellValue = `${cellValue} / ${auroc} / ${ece} / ${brier}`;
            }

            row.push(cellValue);
          });
          fieldMetricsRows.push(row);
        });
    }

    // Helper function to extract text from JSX or string for CSV
    const extractTextForCsv = (field: unknown): string => {
      if (React.isValidElement(field)) {
        // Extract text content from JSX fragments
        const extractText = (node: React.ReactNode): string => {
          if (typeof node === 'string' || typeof node === 'number') {
            return String(node);
          }
          if (React.isValidElement(node)) {
            const props = node.props as { children?: React.ReactNode };
            if (props.children) {
              return extractText(props.children);
            }
          }
          if (Array.isArray(node)) {
            return node.map(extractText).join('');
          }
          return '';
        };
        return extractText(field);
      }
      return String(field);
    };

    // Combine all data
    const csvData = [
      headers,
      ['=== PERFORMANCE METRICS ==='],
      ...performanceRows,
      [''],
      ['=== CONFIGURATION COMPARISON ==='],
      ...configRows,
      [''],
      ['=== AVERAGE ACCURACY BREAKDOWN ==='],
      ...accuracyRows,
      [''],
      ...(fieldMetricsRows.length > 0
        ? [
            [
              hasConfidenceDataCsv
                ? '=== FIELD LEVEL METRICS (Accuracy / Precision / Recall / AUROC / ECE / Brier / ECARB@30) ==='
                : '=== FIELD LEVEL METRICS (Accuracy / Precision / Recall) ===',
            ],
            ...fieldMetricsRows,
            [''],
          ]
        : []),
      ['=== COST BREAKDOWN ==='],
      ...costRows,
      [''],
      ['=== USAGE BREAKDOWN ==='],
      ...usageRows,
    ];

    const csvContent = csvData
      .map((row) => row.map((field: unknown) => `"${extractTextForCsv(field).replace(/"/g, '""')}"`).join(','))
      .join('\n'); // nosemgrep: javascript.lang.security.audit.incomplete-sanitization.incomplete-sanitization - Standard RFC 4180 CSV double-quote escaping

    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const link = document.createElement('a');
    const url = URL.createObjectURL(blob);
    const now = new Date();
    const timestamp = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(
      2,
      '0',
    )}-${String(now.getHours()).padStart(2, '0')}-${String(now.getMinutes()).padStart(2, '0')}-${String(now.getSeconds()).padStart(
      2,
      '0',
    )}`;
    link.setAttribute('href', url);
    link.setAttribute('download', `test-comparison-${timestamp}.csv`);
    link.style.visibility = 'hidden';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  const downloadToJson = () => {
    if (!comparisonData?.metrics) return;

    const completeTestRuns: Record<string, Record<string, unknown>> = Object.fromEntries(
      Object.entries(comparisonData.metrics).filter(
        ([, testRun]) => testRun.status === 'COMPLETE' || testRun.status === 'PARTIAL_COMPLETE',
      ),
    );

    // Create JSON structure matching the UI sections
    const filteredData = {
      testRuns: Object.keys(completeTestRuns),
      performanceMetrics: Object.fromEntries(
        Object.entries(completeTestRuns).map(([testRunId, testRun]) => [
          testRunId,
          {
            testSetName: testRun.testSetName,
            context: testRun.context,
            configVersion: formatConfigVersionText(
              testRun.configVersion as string | undefined,
              versions,
              testRun.configRevision as number | undefined,
            ),
            filesCount: testRun.filesCount,
            completedFiles: testRun.completedFiles,
            failedFiles: testRun.failedFiles,
            totalCost: testRun.totalCost,
            avgCostPerPage: calculateAvgCostPerPage(testRun.totalCost as number, testRun.costBreakdown as CostBreakdown),
            averageAccuracy: testRun.overallAccuracy,
            averageConfidence: testRun.averageConfidence,
            averageWeightedOverallScore: (() => {
              if (testRun.weightedOverallScores) {
                const values = Object.values(parseWeightedOverallScoresFinite(testRun.weightedOverallScores as string));
                return values.length > 0 ? values.reduce((sum: number, score: number) => sum + score, 0) / values.length : null;
              }
              return null;
            })(),
            duration:
              testRun.createdAt && testRun.completedAt
                ? (() => {
                    const duration = new Date(testRun.completedAt as string).getTime() - new Date(testRun.createdAt as string).getTime();
                    const minutes = Math.floor(duration / 60000);
                    const seconds = Math.floor((duration % 60000) / 1000);
                    return minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`;
                  })()
                : 'N/A',
            // Add overall confidence metrics to performance section
            confidenceAuroc: (() => {
              if (testRun.confidenceMetrics) {
                const parsed = parseConfidenceMetrics(testRun.confidenceMetrics);
                return parsed?.overall?.auroc?.value ?? null;
              }
              return null;
            })(),
            confidenceEce: (() => {
              if (testRun.confidenceMetrics) {
                const parsed = parseConfidenceMetrics(testRun.confidenceMetrics);
                return parsed?.overall?.ece?.value ?? null;
              }
              return null;
            })(),
            confidenceBrier: (() => {
              if (testRun.confidenceMetrics) {
                const parsed = parseConfidenceMetrics(testRun.confidenceMetrics);
                return parsed?.overall?.brier?.value ?? null;
              }
              return null;
            })(),
            ecarb30: (() => {
              if (testRun.confidenceMetrics) {
                const parsed = parseConfidenceMetrics(testRun.confidenceMetrics);
                const ecab = (
                  parsed?.overall?.error_capture_at_budget as { budgets?: Record<string, { pct_errors_caught?: number; gain?: number }> }
                )?.budgets?.['0.30'];
                if (
                  ecab?.pct_errors_caught !== null &&
                  ecab?.pct_errors_caught !== undefined &&
                  ecab?.gain !== null &&
                  ecab?.gain !== undefined
                ) {
                  return {
                    pct_errors_caught: ecab.pct_errors_caught,
                    gain: ecab.gain,
                    display: `${(ecab.pct_errors_caught * 100).toFixed(0)}% (${ecab.gain.toFixed(2)}x)`,
                  };
                }
              }
              return null;
            })(),
            confidenceCoverage: (() => {
              if (testRun.confidenceMetrics) {
                const parsed = parseConfidenceMetrics(testRun.confidenceMetrics);
                if (parsed?.coverage) {
                  return {
                    ratio: parsed.coverage.ratio,
                    fields_with_confidence: parsed.coverage.fields_with_confidence,
                    fields_total: parsed.coverage.fields_total,
                  };
                }
              }
              return null;
            })(),
          },
        ]),
      ),
      configurationDifferences: comparisonData.configs || [],
      accuracyBreakdown: Object.fromEntries(
        Object.entries(completeTestRuns).map(([testRunId, testRun]) => {
          const breakdown = { ...((testRun.accuracyBreakdown || {}) as Record<string, unknown>) };
          // Add weighted overall score to accuracy breakdown. Excluded docs
          // are dropped by ``parseWeightedOverallScoresFinite``.
          if (testRun.weightedOverallScores) {
            const values = Object.values(parseWeightedOverallScoresFinite(testRun.weightedOverallScores as string));
            if (values.length > 0) {
              breakdown.weightedOverallScore = values.reduce((sum: number, score: number) => sum + score, 0) / values.length;
            }
          }
          return [testRunId, breakdown];
        }),
      ),
      costBreakdown: Object.fromEntries(
        Object.entries(completeTestRuns).map(([testRunId, testRun]) => [testRunId, testRun.costBreakdown || {}]),
      ),
      fieldMetrics: Object.fromEntries(
        Object.entries(completeTestRuns).map(([testRunId, testRun]) => {
          const fieldMetrics = (testRun.fieldMetrics || {}) as Record<string, unknown>;

          // Merge confidence metrics into field metrics
          if (testRun.confidenceMetrics) {
            const parsed = parseConfidenceMetrics(testRun.confidenceMetrics);
            if (parsed?.fields) {
              const enrichedFields: Record<string, unknown> = {};

              Object.entries(fieldMetrics).forEach(([fieldName, metrics]) => {
                enrichedFields[fieldName] = {
                  ...(metrics as Record<string, unknown>),
                  // Add confidence metrics if available for this field
                  ...(parsed.fields?.[fieldName] && {
                    confidence: parsed.fields[fieldName],
                  }),
                };
              });

              return [testRunId, enrichedFields];
            }
          }

          return [testRunId, fieldMetrics];
        }),
      ),
    };

    const jsonData = JSON.stringify(filteredData, null, 2);
    const blob = new Blob([jsonData], { type: 'application/json' });
    const link = document.createElement('a');
    const url = URL.createObjectURL(blob);
    const now = new Date();
    const timestamp = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(
      2,
      '0',
    )}-${String(now.getHours()).padStart(2, '0')}-${String(now.getMinutes()).padStart(2, '0')}-${String(now.getSeconds()).padStart(
      2,
      '0',
    )}`;
    link.setAttribute('href', url);
    link.setAttribute('download', `test-comparison-${timestamp}.json`);
    link.style.visibility = 'hidden';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  if (comparing) {
    return <ProgressBar status="in-progress" label="Loading comparison..." value={currentAttempt * 20} />;
  }

  if (!comparisonData) {
    return (
      <Container header={<TestStudioHeader title="Compare Test Runs" />}>
        <Box>No comparison data available</Box>
      </Container>
    );
  }

  if (comparisonData.error) {
    return (
      <Container header={<TestStudioHeader title="Compare Test Runs" />}>
        <Box>Error: {comparisonData.error}</Box>
      </Container>
    );
  }

  // Debug logging
  console.log('Comparison data structure:', comparisonData);
  console.log('Metrics structure:', comparisonData.metrics);

  // Filter out incomplete test runs (include COMPLETE and PARTIAL_COMPLETE)
  const completeTestRuns = comparisonData.metrics
    ? Object.fromEntries(
        Object.entries(comparisonData.metrics).filter(
          ([, testRun]) => testRun.status === 'COMPLETE' || testRun.status === 'PARTIAL_COMPLETE',
        ),
      )
    : {};

  const hasIncompleteRuns = comparisonData.metrics
    ? Object.values(comparisonData.metrics).some((testRun) => testRun.status !== 'COMPLETE' && testRun.status !== 'PARTIAL_COMPLETE')
    : false;

  const downloadButton = (
    <ButtonDropdown
      variant="normal"
      {...({ iconName: 'download' } as Record<string, unknown>)}
      items={[
        { id: 'csv', text: 'CSV' },
        { id: 'json', text: 'JSON' },
      ]}
      onItemClick={({ detail }) => {
        if (detail.id === 'csv') {
          downloadToCsv();
        } else if (detail.id === 'json') {
          downloadToJson();
        }
      }}
    ></ButtonDropdown>
  );

  return (
    <Container
      header={
        <TestStudioHeader
          title={`Compare Test Runs (${Object.keys(completeTestRuns).length})`}
          showPrintButton={true}
          additionalActions={[downloadButton]}
        />
      }
    >
      <SpaceBetween direction="vertical" size="l">
        {/* Performance Metrics */}
        <Box>
          <Header variant="h3">Performance Metrics</Header>
          {hasIncompleteRuns && (
            <Box variant="awsui-key-label" color="text-status-warning" padding="s">
              Some test runs are not complete. Only showing results for completed test runs.
            </Box>
          )}
          {Object.keys(completeTestRuns).length > 0 ? (
            <Table
              resizableColumns
              contentDensity="compact"
              wrapLines={preferences.wrapLines}
              preferences={<TestComparisonPreferences preferences={preferences} setPreferences={setPreferences} />}
              items={[
                {
                  metric: 'Test Set',
                  ...Object.fromEntries(
                    Object.entries(completeTestRuns).map(([testRunId, testRun]) => [testRunId, testRun.testSetName || 'N/A']),
                  ),
                },
                {
                  metric: 'Context',
                  ...Object.fromEntries(
                    Object.entries(completeTestRuns).map(([testRunId, testRun]) => [testRunId, testRun.context || 'N/A']),
                  ),
                },
                {
                  metric: 'Config Profile',
                  metricKey: 'Config Profile',
                  ...Object.fromEntries(
                    Object.entries(completeTestRuns).map(([testRunId, testRun]) => [
                      testRunId,
                      formatConfigVersionLink(
                        testRun.configVersion as string | undefined,
                        versions as unknown as UtilsConfigVersion[],
                        undefined,
                        testRun.configRevision as number | undefined,
                      ),
                    ]),
                  ),
                },
                {
                  metric: 'Files Processed',
                  ...Object.fromEntries(
                    Object.entries(completeTestRuns).map(([testRunId, testRun]) => [testRunId, testRun.filesCount || 'N/A']),
                  ),
                },
                {
                  metric: 'Files Completed',
                  ...Object.fromEntries(
                    Object.entries(completeTestRuns).map(([testRunId, testRun]) => [testRunId, testRun.completedFiles || 'N/A']),
                  ),
                },
                {
                  metric: 'Files Failed',
                  ...Object.fromEntries(
                    Object.entries(completeTestRuns).map(([testRunId, testRun]) => [testRunId, testRun.failedFiles || 'N/A']),
                  ),
                },
                {
                  metric: 'Total Cost',
                  metricKey: 'Total Cost',
                  ...Object.fromEntries(
                    Object.entries(completeTestRuns).map(([testRunId, testRun]) => [
                      testRunId,
                      testRun.totalCost !== null && testRun.totalCost !== undefined ? `$${Number(testRun.totalCost).toFixed(4)}` : 'N/A',
                    ]),
                  ),
                },
                {
                  metric: 'Avg Cost/Page',
                  ...Object.fromEntries(
                    Object.entries(completeTestRuns).map(([testRunId, testRun]) => {
                      const avg = calculateAvgCostPerPage(testRun.totalCost as number, testRun.costBreakdown as CostBreakdown);
                      return [testRunId, avg !== null ? `$${avg.toFixed(4)}` : 'N/A'];
                    }),
                  ),
                },
                {
                  metric: (
                    <>
                      Average Accuracy
                      <MetricInfo metric="Avg Accuracy" />
                    </>
                  ),
                  ...Object.fromEntries(
                    Object.entries(completeTestRuns).map(([testRunId, testRun]) => [
                      testRunId,
                      testRun.overallAccuracy !== null && testRun.overallAccuracy !== undefined
                        ? Number(testRun.overallAccuracy).toFixed(3)
                        : 'N/A',
                    ]),
                  ),
                },
                {
                  metric: (
                    <>
                      Average Weighted Overall Score
                      <MetricInfo metric="Avg Weighted Score" />
                    </>
                  ),
                  metricKey: 'Average Weighted Overall Score',
                  ...Object.fromEntries(
                    Object.entries(completeTestRuns).map(([testRunId, testRun]) => {
                      if (testRun.weightedOverallScores) {
                        const values = Object.values(parseWeightedOverallScoresFinite(testRun.weightedOverallScores as string));
                        if (values.length > 0) {
                          const avg = values.reduce((sum: number, score: number) => sum + score, 0) / values.length;
                          return [testRunId, avg.toFixed(3)];
                        }
                      }
                      return [testRunId, 'N/A'];
                    }),
                  ),
                },
                {
                  metric: (
                    <>
                      Average Confidence
                      <MetricInfo metric="Avg Confidence" />
                    </>
                  ),
                  ...Object.fromEntries(
                    Object.entries(completeTestRuns).map(([testRunId, testRun]) => [
                      testRunId,
                      testRun.averageConfidence !== null && testRun.averageConfidence !== undefined
                        ? `${(Number(testRun.averageConfidence) * 100).toFixed(1)}%`
                        : 'N/A',
                    ]),
                  ),
                },
                {
                  metric: 'Duration',
                  ...Object.fromEntries(
                    Object.entries(completeTestRuns).map(([testRunId, testRun]) => {
                      if (testRun.createdAt && testRun.completedAt) {
                        const duration =
                          new Date(testRun.completedAt as string).getTime() - new Date(testRun.createdAt as string).getTime();
                        const minutes = Math.floor(duration / 60000);
                        const seconds = Math.floor((duration % 60000) / 1000);
                        return [testRunId, minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`];
                      }
                      return [testRunId, 'N/A'];
                    }),
                  ),
                },
              ]}
              columnDefinitions={[
                { id: 'metric', header: 'Metric', cell: (item: Record<string, unknown>) => item.metric as React.ReactNode, width: 260 },
                ...Object.keys(completeTestRuns).map((testRunId) => ({
                  id: testRunId,
                  header: createTestRunHeader(testRunId, true),
                  cell: (item: Record<string, unknown>) => {
                    const value = item[testRunId];
                    let style = {};

                    if (item.metricKey === 'Total Cost') {
                      const allCosts = Object.keys(completeTestRuns).map((id) => item[id]);
                      style = getCostColorGrade(value, allCosts);
                    } else if (item.metricKey === 'Average Weighted Overall Score') {
                      const allScores = Object.keys(completeTestRuns).map((id) => item[id]);
                      style = getScoreColorGrade(value, allScores);
                    }

                    return (
                      <span style={{ ...style, WebkitPrintColorAdjust: 'exact', colorAdjust: 'exact' }}>
                        {item.metricKey === 'Config Profile' ? (value as React.ReactNode) : String(value)}
                      </span>
                    );
                  },
                })),
              ]}
              variant="embedded"
            />
          ) : (
            <Box>No completed test runs available</Box>
          )}
        </Box>

        {/* Lowest Scoring Documents Across Tests */}
        <Container
          header={
            <Header
              actions={
                <Select
                  selectedOption={lowestScoreCount}
                  onChange={({ detail }) => setLowestScoreCount(detail.selectedOption as Record<string, unknown>)}
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
              Documents with Lowest Weighted Overall Scores Across Tests
            </Header>
          }
        >
          {(() => {
            const testRunIds = Object.keys(completeTestRuns);

            // Get lowest scoring documents from each test run. Excluded docs
            // (no extractable schema) are dropped by
            // ``parseWeightedOverallScoresFinite`` so they can't rank first as
            // "null.toFixed(3)".
            const getLowestDocs = (testRun: Record<string, unknown>) => {
              if (!testRun?.weightedOverallScores) return [];

              return Object.entries(parseWeightedOverallScoresFinite(testRun.weightedOverallScores as string))
                .map(([docId, score]) => ({ docId, score }))
                .sort((a, b) => a.score - b.score)
                .slice(0, Number(lowestScoreCount.value));
            };

            const allTestDocs = testRunIds.map((testRunId) => getLowestDocs(completeTestRuns[testRunId]));
            const maxRows = Math.max(...allTestDocs.map((docs) => docs.length));

            // Create table items with T1 and T2 columns
            const tableItems: Record<string, unknown>[] = Array.from({ length: maxRows }, (_, index) => {
              const item: Record<string, unknown> = { index };
              testRunIds.forEach((testRunId, testIndex) => {
                item[`t${testIndex + 1}Doc`] = allTestDocs[testIndex][index];
              });
              return item;
            });

            // Helper function to extract filename from document ID
            const getDocumentFilename = (docId: string): string => {
              return (docId.split('/').pop() ?? docId).split('\\').pop() ?? docId; // Handle both / and \ separators
            };

            // Helper to get doc from item
            const getDoc = (item: Record<string, unknown>, testIndex: number): { docId: string; score: number } | undefined =>
              item[`t${testIndex + 1}Doc`] as { docId: string; score: number } | undefined;

            // Create color mapping for matching document filenames
            const getDocumentColor = (docId: string): string => {
              const filename = getDocumentFilename(docId);

              // Count how many test runs have this filename
              let matchCount = 0;
              tableItems.forEach((item) => {
                testRunIds.forEach((_, testIndex) => {
                  const doc = getDoc(item, testIndex);
                  if (doc && getDocumentFilename(doc.docId) === filename) {
                    matchCount++;
                  }
                });
              });

              // Only color if document appears in multiple test runs
              if (matchCount <= 1) return 'transparent';

              const colors = ['#e3f2fd', '#f3e5f5', '#e8f5e8', '#fff3e0', '#fce4ec', '#e0f2f1', '#f1f8e9', '#fff8e1', '#e8eaf6', '#fafafa'];

              // Get all matching filenames
              const matchingFilenames = new Set<string>();
              tableItems.forEach((item) => {
                testRunIds.forEach((_, testIndex) => {
                  const doc = getDoc(item, testIndex);
                  if (doc) {
                    const fname = getDocumentFilename(doc.docId);
                    // Count occurrences of this filename
                    let count = 0;
                    tableItems.forEach((innerItem) => {
                      testRunIds.forEach((__, innerTestIndex) => {
                        const innerDoc = getDoc(innerItem, innerTestIndex);
                        if (innerDoc && getDocumentFilename(innerDoc.docId) === fname) {
                          count++;
                        }
                      });
                    });
                    if (count > 1) matchingFilenames.add(fname);
                  }
                });
              });

              const filenameArray = Array.from(matchingFilenames).sort();
              const colorIndex = filenameArray.indexOf(filename) % colors.length;
              return colors[colorIndex];
            };

            return tableItems.length > 0 ? (
              <Table
                resizableColumns
                wrapLines={preferences.wrapLines}
                preferences={<div />}
                items={tableItems}
                columnDefinitions={testRunIds.map((testRunId, index) => ({
                  id: `t${index + 1}`,
                  header: createTestRunHeader(testRunId, true),
                  width: 400,
                  cell: (item: Record<string, unknown>) => {
                    const doc = item[`t${index + 1}Doc`] as { docId: string; score: number } | undefined;
                    return doc ? (
                      <div
                        style={{
                          textAlign: 'left',
                          backgroundColor: getDocumentColor(doc.docId),
                          padding: '8px',
                          borderRadius: '4px',
                          WebkitPrintColorAdjust: 'exact',
                          colorAdjust: 'exact',
                        }}
                      >
                        <Button
                          variant="link"
                          onClick={() => {
                            const urlPath = doc.docId.replace(/\//g, '%252F'); // nosemgrep: javascript.lang.security.audit.incomplete-sanitization.incomplete-sanitization - Intentional double-encoding for hash routing; decoded by DocumentDetails
                            window.open(`#/documents/${urlPath}`, '_blank');
                          }}
                        >
                          {doc.docId}
                        </Button>
                        <div style={{ fontSize: '12px', color: '#666' }}>Score: {doc.score.toFixed(3)}</div>
                      </div>
                    ) : (
                      ''
                    );
                  },
                }))}
                variant="embedded"
                contentDensity="compact"
              />
            ) : (
              <Box>No documents with weighted overall scores found</Box>
            );
          })()}
        </Container>

        {/* Breakdown Tables */}
        <SpaceBetween direction="vertical" size="l">
          {/* Configuration Comparison */}
          <Container header={<Header variant="h3">Configuration Comparison</Header>}>
            {(() => {
              if (!comparisonData.configs || comparisonData.configs.length === 0) {
                return <Box>No configuration differences found - all test runs use identical configurations</Box>;
              }

              return (
                <Table
                  resizableColumns
                  wrapLines={preferences.wrapLines}
                  preferences={<div />}
                  items={comparisonData.configs.map((config) => {
                    const values = parseConfigSettingValues(config.values as string);
                    return {
                      setting: config.setting,
                      ...values, // Spread the values object to create columns for each test run
                    };
                  })}
                  columnDefinitions={[
                    { id: 'setting', header: 'Config', cell: (item: Record<string, unknown>) => String(item.setting), width: 200 },
                    ...Object.keys(completeTestRuns).map((testRunId) => ({
                      id: testRunId,
                      header: createTestRunHeader(testRunId, true),
                      cell: (item: Record<string, unknown>) => String(item[testRunId] ?? 'N/A'),
                    })),
                  ]}
                  variant="embedded"
                />
              );
            })()}
          </Container>

          {/* Average Accuracy and Split Metrics Comparison */}
          <Container header={<Header variant="h3">Average Accuracy and Split Metrics Comparison</Header>}>
            {(() => {
              const hasAccuracyData = Object.values(completeTestRuns).some((testRun) => testRun.accuracyBreakdown);
              const hasSplitData = Object.values(completeTestRuns).some((testRun) => testRun.splitClassificationMetrics);

              if (!hasAccuracyData && !hasSplitData) {
                return <Box>No accuracy or split metrics data available</Box>;
              }

              const mainItems = [];

              // Add weighted overall score
              mainItems.push({
                metric: (
                  <>
                    <span style={{ color: '#687078' }}>Extraction:</span> Weighted Overall Score
                    <MetricInfo metric="Avg Weighted Score" />
                  </>
                ),
                ...Object.fromEntries(
                  Object.entries(completeTestRuns).map(([testRunId, testRun]) => {
                    if (testRun.weightedOverallScores) {
                      const values = Object.values(parseWeightedOverallScoresFinite(testRun.weightedOverallScores as string));
                      if (values.length > 0) {
                        const avg = values.reduce((sum, score) => sum + score, 0) / values.length;
                        return [testRunId, avg.toFixed(3)];
                      }
                    }
                    return [testRunId, 'N/A'];
                  }),
                ),
              });

              // Add Page Level Accuracy and Split Accuracy With Order
              if (hasSplitData) {
                mainItems.push({
                  metric: (
                    <>
                      <span style={{ color: '#687078' }}>Classification:</span> Page Level Accuracy
                      <MetricInfo metric="Page Level Accuracy" />
                    </>
                  ),
                  ...Object.fromEntries(
                    Object.entries(completeTestRuns).map(([testRunId, testRun]) => {
                      const splitMetrics = testRun.splitClassificationMetrics || {};
                      const value = (splitMetrics as Record<string, unknown>).page_level_accuracy;
                      const displayValue = typeof value === 'number' ? value.toFixed(3) : value?.toString() || '0';
                      return [testRunId, displayValue];
                    }),
                  ),
                });

                mainItems.push({
                  metric: (
                    <>
                      <span style={{ color: '#687078' }}>Classification:</span> Split Accuracy With Order
                      <MetricInfo metric="Split Accuracy With Order" />
                    </>
                  ),
                  ...Object.fromEntries(
                    Object.entries(completeTestRuns).map(([testRunId, testRun]) => {
                      const splitMetrics = testRun.splitClassificationMetrics || {};
                      const value = (splitMetrics as Record<string, unknown>).split_accuracy_with_order;
                      const displayValue = typeof value === 'number' ? value.toFixed(3) : value?.toString() || '0';
                      return [testRunId, displayValue];
                    }),
                  ),
                });
              }

              return (
                <>
                  <Table
                    resizableColumns
                    wrapLines={preferences.wrapLines}
                    preferences={<div />}
                    items={mainItems}
                    columnDefinitions={[
                      {
                        id: 'metric',
                        header: 'Metric',
                        cell: (item: Record<string, unknown>) => item.metric as React.ReactNode,
                        width: 410,
                      },
                      ...Object.keys(completeTestRuns).map((testRunId) => ({
                        id: testRunId,
                        header: createTestRunHeader(testRunId, true),
                        cell: (item: Record<string, unknown>) => String(item[testRunId] ?? ''),
                        width: 200,
                      })),
                    ]}
                    variant="embedded"
                  />

                  {/* Additional Metrics in collapsible section */}
                  <ExpandableSection headerText="Additional Metrics">
                    <Container>
                      <Table
                        resizableColumns
                        wrapLines={preferences.wrapLines}
                        preferences={<div />}
                        items={[
                          // All accuracy breakdown metrics
                          ...(hasAccuracyData
                            ? (() => {
                                const allAccuracyMetrics = new Set();
                                Object.values(completeTestRuns).forEach((testRun) => {
                                  if (testRun.accuracyBreakdown) {
                                    Object.keys(testRun.accuracyBreakdown).forEach((metric) => {
                                      allAccuracyMetrics.add(metric);
                                    });
                                  }
                                });

                                return Array.from(allAccuracyMetrics).map((metricKey) => {
                                  const displayName = String(metricKey)
                                    .replace(/_/g, ' ')
                                    .replace(/\b\w/g, (l) => l.toUpperCase());
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
                                  const mappedMetric = extendedMetricMap[String(metricKey)];

                                  return {
                                    metric: (
                                      <>
                                        <span style={{ color: '#687078' }}>Extraction:</span> {displayName}
                                        {mappedMetric && <MetricInfo metric={mappedMetric} />}
                                      </>
                                    ),
                                    ...Object.fromEntries(
                                      Object.entries(completeTestRuns).map(([testRunId, testRun]) => {
                                        const accuracyBreakdown = (testRun.accuracyBreakdown || {}) as Record<string, number>;
                                        const value = accuracyBreakdown[metricKey as string];
                                        const displayValue = value !== null && value !== undefined ? Number(value).toFixed(3) : '0.000';
                                        return [testRunId, displayValue];
                                      }),
                                    ),
                                  };
                                });
                              })()
                            : []),
                          // Remaining split classification metrics (excluding main ones)
                          ...(hasSplitData
                            ? (() => {
                                const allSplitMetrics = new Set();
                                Object.values(completeTestRuns).forEach((testRun) => {
                                  if (testRun.splitClassificationMetrics) {
                                    Object.keys(testRun.splitClassificationMetrics)
                                      .filter((key) => key !== 'page_level_accuracy' && key !== 'split_accuracy_with_order')
                                      .forEach((metric) => {
                                        allSplitMetrics.add(metric);
                                      });
                                  }
                                });

                                return Array.from(allSplitMetrics).map((metricKey) => {
                                  const displayName = String(metricKey)
                                    .replace(/_/g, ' ')
                                    .replace(/\b\w/g, (l) => l.toUpperCase());
                                  // Map backend keys to MetricInfo keys
                                  const mappedMetric = SPLIT_METRIC_MAP[String(metricKey)];

                                  return {
                                    metric: (
                                      <>
                                        <span style={{ color: '#687078' }}>Classification:</span> {displayName}
                                        {mappedMetric && <MetricInfo metric={mappedMetric} />}
                                      </>
                                    ),
                                    ...Object.fromEntries(
                                      Object.entries(completeTestRuns).map(([testRunId, testRun]) => {
                                        const splitMetrics = testRun.splitClassificationMetrics || {};
                                        const value = (splitMetrics as Record<string, unknown>)[metricKey as string];
                                        const displayValue =
                                          typeof value === 'number' && String(metricKey).includes('accuracy')
                                            ? value.toFixed(3)
                                            : value !== null && value !== undefined
                                              ? value.toString()
                                              : '0';
                                        return [testRunId, displayValue];
                                      }),
                                    ),
                                  };
                                });
                              })()
                            : []),
                          // Confidence metrics (overall)
                          ...(() => {
                            const confidenceMetricKeys = [
                              { key: 'auroc', label: 'AUROC', metricName: 'AUROC' as const },
                              { key: 'ece', label: 'ECE', metricName: 'ECE' as const },
                              { key: 'brier', label: 'Brier Score', metricName: 'Brier' as const },
                              { key: 'ecarb_30', label: 'ECARB@30', metricName: 'ECARB@30' as const },
                            ];

                            const metrics = confidenceMetricKeys
                              .map(({ key, label, metricName }) => {
                                const row: Record<string, unknown> = {
                                  metric: (
                                    <>
                                      <span style={{ color: '#687078' }}>Confidence:</span> {label}
                                      <MetricInfo metric={metricName} />
                                    </>
                                  ),
                                };

                                let hasAnyValue = false;
                                Object.entries(completeTestRuns).forEach(([testRunId, testRun]) => {
                                  if (testRun.confidenceMetrics) {
                                    const parsed = parseConfidenceMetrics(testRun.confidenceMetrics);

                                    if (key === 'ecarb_30') {
                                      // Handle ECARB@30 with nested budgets structure
                                      const ecab = (
                                        parsed?.overall?.error_capture_at_budget as {
                                          budgets?: Record<string, { pct_errors_caught?: number; gain?: number }>;
                                        }
                                      )?.budgets?.['0.30'];
                                      if (
                                        ecab &&
                                        ecab.pct_errors_caught !== null &&
                                        ecab.pct_errors_caught !== undefined &&
                                        ecab.gain !== null &&
                                        ecab.gain !== undefined
                                      ) {
                                        row[testRunId] = `${(ecab.pct_errors_caught * 100).toFixed(0)}% (${ecab.gain.toFixed(2)}x)`;
                                        hasAnyValue = true;
                                      } else {
                                        row[testRunId] = 'N/A';
                                      }
                                    } else {
                                      // Handle standard metrics with .value
                                      const overallMetric =
                                        parsed?.overall && (parsed.overall as Record<string, { value?: number | null }>)[key];
                                      const value = overallMetric?.value;
                                      if (value !== null && value !== undefined) {
                                        row[testRunId] = Number(value).toFixed(3);
                                        hasAnyValue = true;
                                      } else {
                                        row[testRunId] = 'N/A';
                                      }
                                    }
                                  } else {
                                    row[testRunId] = 'N/A';
                                  }
                                });

                                // Only include this metric if at least one test run has a value
                                return hasAnyValue ? row : null;
                              })
                              .filter((row) => row !== null);

                            // Add Coverage Ratio
                            const coverageRow: Record<string, unknown> = {
                              metric: (
                                <>
                                  <span style={{ color: '#687078' }}>Confidence:</span> Coverage Ratio
                                  <MetricInfo metric="Coverage Ratio" />
                                </>
                              ),
                            };

                            let hasCoverage = false;
                            Object.entries(completeTestRuns).forEach(([testRunId, testRun]) => {
                              if (testRun.confidenceMetrics) {
                                const parsed = parseConfidenceMetrics(testRun.confidenceMetrics);
                                const coverage = parsed?.coverage;
                                if (coverage && coverage.ratio !== null && coverage.ratio !== undefined) {
                                  coverageRow[testRunId] =
                                    `${(coverage.ratio * 100).toFixed(1)}% (${coverage.fields_with_confidence}/${coverage.fields_total})`;
                                  hasCoverage = true;
                                } else {
                                  coverageRow[testRunId] = 'N/A';
                                }
                              } else {
                                coverageRow[testRunId] = 'N/A';
                              }
                            });

                            if (hasCoverage) {
                              metrics.push(coverageRow);
                            }

                            return metrics;
                          })(),
                        ]}
                        columnDefinitions={[
                          {
                            id: 'metric',
                            header: 'Metric',
                            cell: (item: Record<string, unknown>) => item.metric as React.ReactNode,
                            width: 410,
                          },
                          ...Object.keys(completeTestRuns).map((testRunId) => ({
                            id: testRunId,
                            header: createTestRunHeader(testRunId, true),
                            cell: (item: Record<string, unknown>) => String(item[testRunId] ?? ''),
                            width: 200,
                          })),
                        ]}
                        variant="embedded"
                      />
                    </Container>
                  </ExpandableSection>
                </>
              );
            })()}
          </Container>

          {/* Field Level Metrics Comparison - only when comparing same test set */}
          {(() => {
            const testSetNames = new Set(Object.values(completeTestRuns).map((r) => r.testSetName));
            const isSameTestSet = testSetNames.size === 1;
            const hasFieldData = Object.values(completeTestRuns).some((r) => r.fieldMetrics);

            if (!isSameTestSet || !hasFieldData) return null;

            // Parse confidence metrics for each test run
            const confidenceDataMap: Record<string, ReturnType<typeof parseConfidenceMetrics>> = {};
            Object.entries(completeTestRuns).forEach(([testRunId, testRun]) => {
              if (testRun.confidenceMetrics) {
                const parsed = parseConfidenceMetrics(testRun.confidenceMetrics);
                if (parsed) {
                  confidenceDataMap[testRunId] = parsed;
                }
              }
            });

            // Check if any test run has confidence data
            const hasConfidenceData = Object.values(confidenceDataMap).some(
              (data) => data && data.fields && Object.keys(data.fields).length > 0,
            );

            // Collect all unique field names
            const allFieldNames = new Set<string>();
            Object.values(completeTestRuns).forEach((testRun) => {
              if (testRun.fieldMetrics) {
                Object.keys(testRun.fieldMetrics as Record<string, unknown>).forEach((f) => allFieldNames.add(f));
              }
            });

            const computeMetric = (m: Record<string, number> | undefined) => {
              if (!m) return { accuracy: 'N/A', precision: 'N/A', recall: 'N/A' };
              const tp = m.tp ?? 0;
              const fp = m.fp ?? 0;
              const tn = m.tn ?? 0;
              const fn = m.fn ?? 0;
              const total = tp + fp + tn + fn;
              return {
                accuracy: total > 0 ? ((tp + tn) / total).toFixed(3) : 'N/A',
                precision: tp + fp > 0 ? (tp / (tp + fp)).toFixed(3) : 'N/A',
                recall: tp + fn > 0 ? (tp / (tp + fn)).toFixed(3) : 'N/A',
              };
            };

            const tableItems = Array.from(allFieldNames)
              .sort()
              .map((fieldName) => {
                const row: Record<string, unknown> = { fieldName };
                Object.entries(completeTestRuns).forEach(([testRunId, testRun]) => {
                  const fm = testRun.fieldMetrics as Record<string, Record<string, number>> | undefined;
                  const m = computeMetric(fm?.[fieldName]);

                  // Get confidence metrics for this field
                  const confData = confidenceDataMap[testRunId];
                  const fieldConf = confData?.fields?.[fieldName];

                  const auroc =
                    fieldConf?.auroc?.value !== undefined && fieldConf.auroc.value !== null ? fieldConf.auroc.value.toFixed(3) : 'N/A';
                  const ece = fieldConf?.ece?.value !== undefined && fieldConf.ece.value !== null ? fieldConf.ece.value.toFixed(3) : 'N/A';
                  const brier =
                    fieldConf?.brier?.value !== undefined && fieldConf.brier.value !== null ? fieldConf.brier.value.toFixed(3) : 'N/A';

                  // Extract ECARB@30
                  const ecabData = (
                    fieldConf?.error_capture_at_budget as { budgets?: Record<string, { pct_errors_caught?: number; gain?: number }> }
                  )?.budgets?.['0.30'];
                  const ecab30 =
                    ecabData &&
                    ecabData.pct_errors_caught !== null &&
                    ecabData.pct_errors_caught !== undefined &&
                    ecabData.gain !== null &&
                    ecabData.gain !== undefined
                      ? `${(ecabData.pct_errors_caught * 100).toFixed(0)}% (${ecabData.gain.toFixed(1)}x)`
                      : 'N/A';

                  // Combine accuracy and confidence metrics in one column
                  row[testRunId] = hasConfidenceData
                    ? `${m.accuracy} / ${m.precision} / ${m.recall} / ${auroc} / ${ece} / ${brier} / ${ecab30}`
                    : `${m.accuracy} / ${m.precision} / ${m.recall}`;
                  row[`${testRunId}_confidence`] = hasConfidenceData ? `${auroc} / ${ece} / ${brier} / ${ecab30}` : 'N/A';
                });
                return row;
              });

            const headerDescription = hasConfidenceData
              ? 'Values shown as: Accuracy / Precision / Recall / AUROC / ECE / Brier / ECARB@30. Color grading based on accuracy.'
              : 'Values shown as: Accuracy / Precision / Recall. Color grading based on accuracy.';

            return (
              <Container
                header={
                  <Header variant="h3" description={headerDescription}>
                    Field Level Metrics Comparison
                  </Header>
                }
              >
                <ExpandableSection headerText={`View Details (${allFieldNames.size} fields)`} defaultExpanded={false}>
                  <Table
                    resizableColumns
                    wrapLines={preferences.wrapLines}
                    preferences={<div />}
                    items={tableItems}
                    columnDefinitions={[
                      {
                        id: 'fieldName',
                        header: 'Field Name',
                        cell: (item: Record<string, unknown>) => String(item.fieldName),
                        width: 300,
                      },
                      ...Object.keys(completeTestRuns).map((testRunId) => ({
                        id: testRunId,
                        header: createTestRunHeader(testRunId, true),
                        width: hasConfidenceData ? 350 : 200,
                        cell: (item: Record<string, unknown>) => {
                          const val = String(item[testRunId] ?? 'N/A');
                          const allVals = Object.keys(completeTestRuns).map((id) => {
                            const v = String(item[id] ?? 'N/A');
                            return v.split('/')[0]?.trim();
                          });
                          const acc = val.split('/')[0]?.trim();
                          const style = getScoreColorGrade(acc, allVals);
                          return (
                            <span style={{ ...style, WebkitPrintColorAdjust: 'exact', colorAdjust: 'exact', fontSize: '0.85em' }}>
                              {val}
                            </span>
                          );
                        },
                      })),
                    ]}
                    variant="embedded"
                    contentDensity="compact"
                  />
                </ExpandableSection>
              </Container>
            );
          })()}

          {/* Cost Comparison */}
          <Container header={<Header variant="h3">Cost Breakdown Comparison</Header>}>
            {(() => {
              const allCostItems = new Set();

              // Collect all unique cost items
              Object.values(completeTestRuns).forEach((testRun) => {
                if (testRun.costBreakdown) {
                  Object.entries(testRun.costBreakdown).forEach(([context, services]) => {
                    Object.keys(services).forEach((serviceUnit) => {
                      const lastUnderscoreIndex = serviceUnit.lastIndexOf('_');
                      const serviceApi = serviceUnit.substring(0, lastUnderscoreIndex);
                      const unit = serviceUnit.substring(lastUnderscoreIndex + 1);
                      const [service, ...apiParts] = serviceApi.split('/');
                      const api = apiParts.join('/');
                      allCostItems.add(`${context}|${service}/${api}|${unit}`);
                    });
                  });
                }
              });

              const tableItems: CostComparisonRow[] = Array.from(allCostItems).map((itemKey) => {
                const [context, serviceApi, unit] = String(itemKey).split('|');
                const row: CostComparisonRow = {
                  context,
                  serviceApi,
                  unit,
                };

                // Add cost for each test run
                Object.entries(completeTestRuns).forEach(([testRunId, testRun]) => {
                  const services =
                    (testRun.costBreakdown as Record<string, Record<string, Record<string, unknown>>> | undefined)?.[context] || {};
                  const serviceKey = Object.keys(services).find((key) => {
                    const lastUnderscoreIndex = key.lastIndexOf('_');
                    const keyServiceApi = key.substring(0, lastUnderscoreIndex);
                    const keyUnit = key.substring(lastUnderscoreIndex + 1);
                    return keyServiceApi === serviceApi && keyUnit === unit;
                  });

                  const details = (serviceKey ? services[serviceKey] : {}) as Record<string, unknown>;
                  const estimatedCost = Number(details.estimated_cost) || 0;
                  row[testRunId] = estimatedCost > 0 ? `$${estimatedCost.toFixed(4)}` : 'N/A';
                });

                return row;
              });

              // Sort by context, then by service/api
              tableItems.sort((a, b) => {
                if (a.context !== b.context) return a.context.localeCompare(b.context);
                return a.serviceApi.localeCompare(b.serviceApi);
              });

              // Add context subtotals
              const finalItems: CostComparisonRow[] = [];
              tableItems.forEach((item, index) => {
                finalItems.push(item);

                // Check if this is the last item for this context
                const nextItem = tableItems[index + 1];
                const isLastInContext = !nextItem || nextItem.context !== item.context;

                if (isLastInContext) {
                  // Calculate subtotal for this context
                  const contextItems = tableItems.filter((i) => i.context === item.context);
                  const subtotalRow: CostComparisonRow = {
                    context: '',
                    serviceApi: `${item.context} Subtotal`,
                    unit: '',
                    isSubtotal: true,
                  };

                  Object.keys(completeTestRuns).forEach((testRunId) => {
                    const contextTotal = contextItems.reduce((sum, contextItem) => {
                      const value = contextItem[testRunId];
                      if (typeof value !== 'string' || value === 'N/A') return sum;
                      const numValue = parseFloat(value.replace('$', ''));
                      return sum + (isNaN(numValue) ? 0 : numValue);
                    }, 0);
                    subtotalRow[testRunId] = `$${contextTotal.toFixed(4)}`;
                  });

                  finalItems.push(subtotalRow);
                }
              });

              // Add total row
              const totalRow: CostComparisonRow = {
                context: '',
                serviceApi: 'Total',
                unit: '',
                isTotal: true,
              };

              Object.keys(completeTestRuns).forEach((testRunId) => {
                const grandTotal = tableItems.reduce((sum, item) => {
                  const value = item[testRunId];
                  if (typeof value !== 'string' || value === 'N/A') return sum;
                  const numValue = parseFloat(value.replace('$', ''));
                  return sum + (isNaN(numValue) ? 0 : numValue);
                }, 0);
                totalRow[testRunId] = `$${grandTotal.toFixed(4)}`;
              });

              finalItems.push(totalRow);

              return finalItems.length > 0 ? (
                <Table
                  resizableColumns
                  wrapLines={preferences.wrapLines}
                  preferences={<div />}
                  items={finalItems}
                  columnDefinitions={[
                    {
                      id: 'context',
                      header: 'Context',
                      cell: (item: CostComparisonRow) => (item.isSubtotal || item.isTotal ? '' : item.context),
                      width: 180,
                    },
                    {
                      id: 'serviceApi',
                      header: 'Service/Api',
                      width: 300,
                      cell: (item: CostComparisonRow) => (
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
                      cell: (item: CostComparisonRow) => (item.isSubtotal || item.isTotal ? '' : item.unit),
                      width: 200,
                    },
                    ...Object.keys(completeTestRuns).map((testRunId) => ({
                      id: testRunId,
                      header: createTestRunHeader(testRunId, true),
                      width: 100,
                      cell: (item: CostComparisonRow) => (
                        <span
                          style={{
                            fontWeight: item.isSubtotal || item.isTotal ? 'bold' : 'normal',
                            color: item.isTotal ? '#0073bb' : 'inherit',
                          }}
                        >
                          {(item[testRunId] as string) || '$0.0000'}
                        </span>
                      ),
                    })),
                  ]}
                  variant="embedded"
                />
              ) : (
                <Box>No cost breakdown data available</Box>
              );
            })()}
          </Container>

          {/* Usage Comparison */}
          <Container header={<Header variant="h3">Usage Breakdown Comparison</Header>}>
            {(() => {
              const allUsageItems = new Set();

              // Collect all unique usage items
              Object.values(completeTestRuns).forEach((testRun) => {
                if (testRun.costBreakdown) {
                  Object.entries(testRun.costBreakdown).forEach(([context, services]) => {
                    Object.keys(services).forEach((serviceUnit) => {
                      const lastUnderscoreIndex = serviceUnit.lastIndexOf('_');
                      const serviceApi = serviceUnit.substring(0, lastUnderscoreIndex);
                      const unit = serviceUnit.substring(lastUnderscoreIndex + 1);
                      const [service, ...apiParts] = serviceApi.split('/');
                      const api = apiParts.join('/');
                      allUsageItems.add(`${context}|${service}/${api}|${unit}`);
                    });
                  });
                }
              });

              const tableItems: Record<string, string>[] = Array.from(allUsageItems).map((itemKey) => {
                const [context, serviceApi, unit] = String(itemKey).split('|');
                const row: Record<string, string> = {
                  context,
                  serviceApi,
                  unit,
                };

                // Add usage value for each test run
                Object.entries(completeTestRuns).forEach(([testRunId, testRun]) => {
                  const services =
                    (testRun.costBreakdown as Record<string, Record<string, Record<string, unknown>>> | undefined)?.[context] || {};
                  const serviceKey = Object.keys(services).find((key) => {
                    const lastUnderscoreIndex = key.lastIndexOf('_');
                    const keyServiceApi = key.substring(0, lastUnderscoreIndex);
                    const keyUnit = key.substring(lastUnderscoreIndex + 1);
                    return keyServiceApi === serviceApi && keyUnit === unit;
                  });

                  const details = (serviceKey ? services[serviceKey] : {}) as Record<string, unknown>;
                  const value = Number(details.value) || 0;
                  row[testRunId] = value > 0 ? value.toLocaleString() : 'N/A';
                });

                return row;
              });

              // Sort by context, then by service/api
              tableItems.sort((a, b) => {
                if (a.context !== b.context) return a.context.localeCompare(b.context);
                return a.serviceApi.localeCompare(b.serviceApi);
              });

              return tableItems.length > 0 ? (
                <Table
                  resizableColumns
                  wrapLines={preferences.wrapLines}
                  preferences={<div />}
                  items={tableItems}
                  columnDefinitions={[
                    { id: 'context', header: 'Context', cell: (item: Record<string, string>) => item.context, width: 180 },
                    { id: 'serviceApi', header: 'Service/Api', cell: (item: Record<string, string>) => item.serviceApi, width: 300 },
                    { id: 'unit', header: 'Unit', cell: (item: Record<string, string>) => item.unit, width: 200 },
                    ...Object.keys(completeTestRuns).map((testRunId) => ({
                      id: testRunId,
                      header: createTestRunHeader(testRunId, true),
                      width: 150,
                      cell: (item: Record<string, string>) => item[testRunId] || '0',
                    })),
                  ]}
                  variant="embedded"
                />
              ) : (
                <Box>No usage breakdown data available</Box>
              );
            })()}
          </Container>
        </SpaceBetween>
      </SpaceBetween>
    </Container>
  );
};

export default TestComparison;
