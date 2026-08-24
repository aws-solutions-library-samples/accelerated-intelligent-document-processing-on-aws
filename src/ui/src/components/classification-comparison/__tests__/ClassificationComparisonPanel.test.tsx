// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Component tests for ClassificationComparisonPanel.
 *
 * The GraphQL client is mocked at the module boundary so the panel's fetch of
 * `evaluation/results.json` runs in jsdom with no AWS. Covers the three
 * behaviors that matter for correctness of what the user is told: the panel is
 * absent when there is nothing to compare, a mismatch is shown as ground truth
 * vs predicted, and the mismatch filter defaults to whichever view is useful.
 */

import React from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

const { mockGraphql } = vi.hoisted(() => ({ mockGraphql: vi.fn() }));
vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({ graphql: mockGraphql }),
}));

import ClassificationComparisonPanel from '../ClassificationComparisonPanel';

const REPORT_URI = 's3://out/docs/pkg.pdf/evaluation/report.md';

const resultsWith = (docSplitMetrics: Record<string, unknown>) => ({
  data: {
    getFileContents: {
      isBinary: false,
      content: JSON.stringify({ doc_split_metrics: docSplitMetrics }),
    },
  },
});

const MISMATCHED = {
  page_level_accuracy: 0.5,
  total_pages: 2,
  correctly_classified_pages: 1,
  page_details: [
    { page_index: 0, ground_truth_class: 'W2', predicted_class: 'W2', correct: true },
    { page_index: 1, ground_truth_class: 'BankStatement', predicted_class: 'Invoice', correct: false },
  ],
};

const ALL_CORRECT = {
  page_level_accuracy: 1,
  total_pages: 2,
  correctly_classified_pages: 2,
  page_details: [
    { page_index: 0, ground_truth_class: 'W2', predicted_class: 'W2', correct: true },
    { page_index: 1, ground_truth_class: 'W2', predicted_class: 'W2', correct: true },
  ],
};

describe('ClassificationComparisonPanel', () => {
  beforeEach(() => {
    mockGraphql.mockReset();
  });

  it('renders nothing when the document has no evaluation report', () => {
    const { container } = render(<ClassificationComparisonPanel />);

    expect(container).toBeEmptyDOMElement();
    expect(mockGraphql).not.toHaveBeenCalled();
  });

  it('renders nothing when the results carry no classification comparison', async () => {
    // A document evaluated without section-level ground truth. An empty panel
    // would imply a comparison exists and is perfect.
    mockGraphql.mockResolvedValue({ data: { getFileContents: { isBinary: false, content: JSON.stringify({}) } } });

    const { container } = render(<ClassificationComparisonPanel evaluationReportUri={REPORT_URI} />);

    await waitFor(() => expect(mockGraphql).toHaveBeenCalled());
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });

  it('shows the ground truth class beside the predicted class for a mismatch', async () => {
    mockGraphql.mockResolvedValue(resultsWith(MISMATCHED));

    render(<ClassificationComparisonPanel evaluationReportUri={REPORT_URI} />);

    await waitFor(() => expect(screen.getByText('Classification vs Ground Truth')).toBeInTheDocument());
    // Both the page and section tables carry these headers.
    expect(screen.getAllByText('Ground truth class').length).toBeGreaterThan(0);
    expect(screen.getAllByText('This run classified as').length).toBeGreaterThan(0);
    expect(screen.getByText('BankStatement')).toBeInTheDocument();
    expect(screen.getByText('Invoice')).toBeInTheDocument();
    expect(screen.getByText('50.0%')).toBeInTheDocument();
    expect(screen.getByText('1 of 2 pages match')).toBeInTheDocument();
  });

  it('defaults to mismatches only, hiding the pages that matched', async () => {
    mockGraphql.mockResolvedValue(resultsWith(MISMATCHED));

    render(<ClassificationComparisonPanel evaluationReportUri={REPORT_URI} />);

    await waitFor(() => expect(screen.getByText('BankStatement')).toBeInTheDocument());
    // Page 1 matched, so its row (and its 'W2' class) is filtered out.
    expect(screen.queryByText('W2')).not.toBeInTheDocument();
    expect(screen.getByText('Misclassified')).toBeInTheDocument();
  });

  it('shows every page when the run has no mismatches', async () => {
    // With the filter on by default this table would be empty, which reads as
    // "no data" rather than "nothing wrong".
    mockGraphql.mockResolvedValue(resultsWith(ALL_CORRECT));

    render(<ClassificationComparisonPanel evaluationReportUri={REPORT_URI} />);

    await waitFor(() => expect(screen.getByText('100.0%')).toBeInTheDocument());
    expect(screen.getAllByText('Match')).toHaveLength(2);
  });

  it('does not surface a fetch failure as an error banner', async () => {
    // Results pruned by retention, or an older document: nothing to show, but
    // not a failure the user can act on.
    mockGraphql.mockRejectedValue(new Error('AccessDenied'));

    render(<ClassificationComparisonPanel evaluationReportUri={REPORT_URI} />);

    await waitFor(() => expect(screen.getByText(/No classification comparison is available/)).toBeInTheDocument());
  });
});
