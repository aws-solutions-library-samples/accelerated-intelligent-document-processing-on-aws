// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { Popover, Icon, Link, Box } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';

const logger = new ConsoleLogger('MetricInfo');

interface MetricConfig {
  description: string;
  docsUrl?: string;
}

interface MetricInfoProps {
  metric:
    | 'AUROC'
    | 'ECE'
    | 'Brier'
    | 'ECARB@30'
    | 'Accuracy'
    | 'Precision'
    | 'Recall'
    | 'F1'
    | 'TP'
    | 'FP'
    | 'TN'
    | 'FN'
    | 'False Alarm Rate'
    | 'False Discovery Rate'
    | 'Avg Confidence'
    | 'Avg Accuracy'
    | 'Avg Weighted Score'
    | 'Excluded Docs'
    | 'Coverage Ratio'
    | 'Correctly Split With Order'
    | 'Split Accuracy Without Order'
    | 'Correctly Split Without Order'
    | 'Correctly Classified Pages'
    | 'Total Pages'
    | 'Total Splits'
    | 'Page Level Accuracy'
    | 'Split Accuracy With Order';
}

// Backend key to MetricInfo key mappings (exported for use in TestResults and TestComparison)
export const ACCURACY_METRIC_MAP: Record<string, MetricInfoProps['metric']> = {
  accuracy: 'Accuracy',
  precision: 'Precision',
  recall: 'Recall',
  f1: 'F1',
};

export const SPLIT_METRIC_MAP: Record<string, MetricInfoProps['metric']> = {
  page_level_accuracy: 'Page Level Accuracy',
  split_accuracy_with_order: 'Split Accuracy With Order',
  correctly_split_with_order: 'Correctly Split With Order',
  split_accuracy_without_order: 'Split Accuracy Without Order',
  correctly_split_without_order: 'Correctly Split Without Order',
  correctly_classified_pages: 'Correctly Classified Pages',
  total_pages: 'Total Pages',
  total_splits: 'Total Splits',
};

// Centralized metric definitions
const METRIC_CONFIGS: Record<string, MetricConfig> = {
  // Confidence calibration metrics
  AUROC: {
    description: 'Area Under ROC Curve - how well confidence distinguishes correct from incorrect (1.0 = perfect, 0.5 = random)',
    docsUrl: 'https://en.wikipedia.org/wiki/Receiver_operating_characteristic#Area_under_the_curve',
  },
  ECE: {
    description: 'Expected Calibration Error - how far confidence is from actual accuracy (0.0 = perfect)',
    docsUrl: 'https://en.wikipedia.org/wiki/Calibration_(statistics)',
  },
  Brier: {
    description: 'Brier Score - mean squared error between confidence and correctness (0.0 = perfect)',
    docsUrl: 'https://en.wikipedia.org/wiki/Brier_score',
  },
  'ECARB@30': {
    description: 'Error Capture at 30% Budget - % errors caught reviewing lowest-confidence 30% of data',
    docsUrl: 'https://awslabs.github.io/stickler/Advanced/confidence-metrics/#error-capture-at-review-budget',
  },

  // Accuracy metrics
  Accuracy: {
    description: 'Accuracy - ratio of correct predictions to total predictions: (TP+TN)/(TP+TN+FP+FN)',
    docsUrl: 'https://en.wikipedia.org/wiki/Accuracy_and_precision#In_binary_classification',
  },
  Precision: {
    description: 'Precision - ratio of true positives to predicted positives: TP/(TP+FP). Measures how many predicted matches are correct',
    docsUrl: 'https://en.wikipedia.org/wiki/Precision_and_recall#Precision',
  },
  Recall: {
    description:
      'Recall (Sensitivity) - ratio of true positives to actual positives: TP/(TP+FN). Measures how many actual matches were found',
    docsUrl: 'https://en.wikipedia.org/wiki/Precision_and_recall#Recall',
  },
  F1: {
    description: 'F1 Score - harmonic mean of precision and recall: 2*(Precision*Recall)/(Precision+Recall). Balances both metrics',
    docsUrl: 'https://en.wikipedia.org/wiki/F-score',
  },

  // Confusion matrix components
  TP: {
    description: 'True Positives - number of correct positive predictions (predicted match, actual match)',
  },
  FP: {
    description: 'False Positives - number of incorrect positive predictions (predicted match, actual non-match)',
  },
  TN: {
    description: 'True Negatives - number of correct negative predictions (predicted non-match, actual non-match)',
  },
  FN: {
    description: 'False Negatives - number of incorrect negative predictions (predicted non-match, actual match)',
  },

  // Error rates
  'False Alarm Rate': {
    description:
      'False Alarm Rate (False Positive Rate) - ratio of false positives to actual negatives: FP/(FP+TN). Measures incorrect positive predictions',
    docsUrl: 'https://en.wikipedia.org/wiki/False_positive_rate',
  },
  'False Discovery Rate': {
    description:
      'False Discovery Rate - ratio of false positives to predicted positives: FP/(FP+TP). Measures unreliability of positive predictions',
    docsUrl: 'https://en.wikipedia.org/wiki/False_discovery_rate',
  },

  // Aggregate metrics (document-level averages)
  'Avg Confidence': {
    description: 'Average Confidence - mean confidence score across all predictions. Higher values indicate model certainty',
  },
  'Avg Accuracy': {
    description: 'Average Accuracy - mean accuracy across all documents in the test run',
  },
  'Avg Weighted Score': {
    description:
      'Average Weighted Overall Score - mean weighted score across all documents, where each document score is weighted by field importance',
  },
  'Excluded Docs': {
    description:
      'Documents excluded from scoring because every section was classified into a class with no extractable schema (either no attributes defined in the config, or the class was auto-classified but never configured). These docs are not included in the histogram, the lowest-scores table, or the run-level average — nothing was extracted and nothing was expected to be extracted, so a 0.000 score would misrepresent extraction quality.',
  },
  'Coverage Ratio': {
    description:
      'Coverage Ratio - percentage of fields that have confidence scores. Shows how many fields the model provided confidence values for',
  },

  // Split classification metrics
  'Page Level Accuracy': {
    description: 'Page Level Accuracy - percentage of pages correctly classified into document types',
  },
  'Split Accuracy With Order': {
    description: 'Split Accuracy With Order - percentage of document splits that are correct including the order of splits',
  },
  'Correctly Split With Order': {
    description: 'Correctly Split With Order - number of document splits that match the expected splits in the correct order',
  },
  'Split Accuracy Without Order': {
    description: 'Split Accuracy Without Order - percentage of document splits that are correct ignoring the order',
  },
  'Correctly Split Without Order': {
    description: 'Correctly Split Without Order - number of document splits that match the expected splits regardless of order',
  },
  'Correctly Classified Pages': {
    description: 'Correctly Classified Pages - number of pages that were correctly classified into their document types',
  },
  'Total Pages': {
    description: 'Total Pages - total number of pages processed in the document splitting task',
  },
  'Total Splits': {
    description: 'Total Splits - total number of document splits identified in the processing',
  },
};

/**
 * MetricInfo component displays an info icon with a tooltip explaining a metric.
 * Used to provide user-friendly explanations for confidence calibration and accuracy metrics.
 */
const MetricInfo: React.FC<MetricInfoProps> = ({ metric }) => {
  const config = METRIC_CONFIGS[metric];

  if (!config) {
    logger.warn(`No configuration found for metric: ${metric}`);
    return null;
  }

  const handleClick = (e: React.MouseEvent) => {
    // Stop propagation to prevent table sorting, but don't preventDefault so Popover can open
    e.stopPropagation();
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' || e.key === ' ') {
      // Stop propagation to prevent table sorting and prevent default Space scroll behavior
      e.stopPropagation();
      e.preventDefault();
    }
  };

  return (
    <span
      onClick={handleClick}
      onKeyDown={handleKeyDown}
      style={{ display: 'inline-block' }}
      role="button"
      tabIndex={0}
      aria-label={`Information about ${metric} metric`}
    >
      <Popover
        dismissButton={false}
        position="top"
        size="small"
        triggerType="custom"
        content={
          <Box variant="p">
            {config.description}
            {config.docsUrl && (
              <>
                {' '}
                <Link href={config.docsUrl} external>
                  Learn more
                </Link>
              </>
            )}
          </Box>
        }
      >
        <Box display="inline" margin={{ left: 'xs' }}>
          <Icon name="status-info" size="small" variant="subtle" />
        </Box>
      </Popover>
    </span>
  );
};

export default MetricInfo;
