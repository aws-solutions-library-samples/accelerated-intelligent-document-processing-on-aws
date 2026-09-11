// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The report mounted against a payload shaped like a live results.json.
 *
 * The parity and model tests read source and call pure functions; neither would
 * notice a Cloudscape prop that throws at render (an expandable table without a
 * stable trackBy, say). This mounts the component with the GraphQL client mocked
 * and checks that each added section reaches the DOM.
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { describe, expect, it, vi } from 'vitest';

// Hoisted with the mocks: vi.mock factories run before module-level constants exist.
const { RESULTS } = vi.hoisted(() => ({
  RESULTS: {
    document_id: 'packet.pdf',
    execution_time: 12.3,
    overall_metrics: {
      precision: 0.9,
      recall: 0.8,
      f1_score: 0.85,
      accuracy: 0.8,
      false_alarm_rate: 0.1,
      false_discovery_rate: 0.1,
      weighted_overall_score: 0.82,
    },
    doc_split_metrics: {
      page_level_accuracy: 0.5,
      split_accuracy_without_order: 0.5,
      split_accuracy_with_order: 0.0,
      total_pages: 2,
      total_splits: 2,
      correctly_classified_pages: 1,
      correctly_split_without_order: 1,
      correctly_split_with_order: 0,
      page_details: [
        { page_index: 0, ground_truth_class: 'W2', predicted_class: 'W2', correct: true },
        { page_index: 1, ground_truth_class: 'W2', predicted_class: 'form', correct: false },
      ],
      section_details_without_order: [
        {
          section_id: '1',
          ground_truth_class: 'W2',
          ground_truth_pages: [0],
          matched: true,
          matched_section_id: '1',
          predicted_class: 'W2',
          predicted_pages: [0],
        },
        {
          section_id: '2',
          ground_truth_class: 'W2',
          ground_truth_pages: [1],
          matched: false,
          matched_section_id: null,
          predicted_class: 'No Match',
          predicted_pages: [],
        },
      ],
      section_details_with_order: [
        {
          section_id: '1',
          ground_truth_class: 'W2',
          ground_truth_pages: [0],
          matched: true,
          order_matched: false,
          matched_section_id: '1',
          predicted_class: 'W2',
          predicted_pages: [0],
        },
        {
          section_id: '2',
          ground_truth_class: 'W2',
          ground_truth_pages: [1],
          matched: false,
          order_matched: false,
          matched_section_id: null,
          predicted_class: 'No Match',
          predicted_pages: [],
        },
      ],
      predicted_sections: [
        { section_id: '1', document_class: 'W2', page_indices: [0] },
        { section_id: '2', document_class: 'form', page_indices: [1] },
      ],
      errors: ['section 2 had no prediction'],
      final_score: 0.75,
    },
    excluded_sections: [{ section_id: '3', classification: 'Cover', exclusion_reason: 'no extractable attributes', page_ids: ['3'] }],
    section_results: [
      {
        section_id: '1',
        document_class: 'W2',
        metrics: {
          precision: 0.9,
          recall: 0.8,
          f1_score: 0.85,
          weighted_overall_score: 0.82,
          _stickler_counts: { tp: 1 },
          skipped_field_count: 1,
        },
        attributes: [
          {
            name: 'Employer',
            expected: 'Acme',
            actual: 'Acme',
            matched: true,
            score: 1,
            evaluation_method: 'Exact',
            confidence: 0.97,
            confidence_threshold: 0.8,
            weight: 2,
          },
          {
            name: 'Boxes',
            expected: [
              { code: 'A', amount: 1 },
              { code: 'B', amount: 2 },
            ],
            actual: [
              { code: 'B', amount: 2 },
              { code: 'A', amount: 9 },
            ],
            matched: false,
            score: 0.5,
            evaluation_method: 'Hungarian',
            reason: 'one of two items matched',
            field_comparison_details: [
              {
                expected_key: 'Boxes[0].amount',
                actual_key: 'Boxes[1].amount',
                expected_value: 1,
                actual_value: 9,
                match: false,
                score: 0,
                evaluation_method: 'NumericExact',
                reason: 'differs',
              },
              {
                expected_key: 'Boxes[1].amount',
                actual_key: 'Boxes[0].amount',
                expected_value: 2,
                actual_value: 2,
                match: true,
                score: 1,
                evaluation_method: 'NumericExact',
                reason: 'exact',
              },
            ],
          },
        ],
      },
      {
        section_id: '2',
        document_class: 'Payslip',
        metrics: { precision: 0, recall: 0, evaluation_failed: true, failure_type: 'missing_schema_configuration' },
        attributes: [{ name: 'n/a', reason: 'No evaluation configuration found for Payslip' }],
      },
    ],
  },
}));

vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({
    graphql: vi.fn().mockResolvedValue({ data: { getFileContents: { content: JSON.stringify(RESULTS), isBinary: false } } }),
  }),
}));

vi.mock('../MarkdownViewer', () => ({ MarkdownReport: () => <div>markdown fallback</div> }));

import EvaluationReport from '../EvaluationReport';

describe('EvaluationReport mounted against a packet payload', () => {
  it('renders every section the markdown has', async () => {
    render(<EvaluationReport reportUri="s3://b/packet.pdf/evaluation/report.md" documentId="packet.pdf" />);

    await waitFor(() => expect(screen.getByText('Split accuracy')).toBeInTheDocument());
    expect(screen.getByText('1 of 2 pages')).toBeInTheDocument();
    expect(screen.getByText('1 section not evaluated')).toBeInTheDocument();
    expect(screen.getByText('Section split analysis')).toBeInTheDocument();
    expect(screen.getByText('All metrics')).toBeInTheDocument();
    expect(screen.getByText('How scores are computed')).toBeInTheDocument();
    expect(screen.getByText('Evaluation took 12.3 s.')).toBeInTheDocument();
    // One vocabulary for a band, wherever it appears: the tile says what the tables say.
    expect(screen.queryByText('good')).not.toBeInTheDocument();
    expect(screen.getAllByText('Good').length).toBeGreaterThan(0);
  });

  it('sorts the attribute table when a sortable header is clicked', async () => {
    render(<EvaluationReport reportUri="s3://b/packet.pdf/evaluation/report.md" documentId="packet.pdf" />);
    await waitFor(() => expect(screen.getByText('Boxes')).toBeInTheDocument());
    const table = screen.getByText('Boxes').closest('table')!;
    const names = () => [...table.querySelectorAll('tbody tr')].map((tr) => tr.querySelectorAll('td')[1]?.textContent?.trim());
    expect(names()).toEqual(['Employer', 'Boxes']);
    await userEvent.click(screen.getAllByRole('button', { name: /^Score/ })[0]);
    // Ascending by score: Boxes (0.5) before Employer (1).
    await waitFor(() => expect(names()).toEqual(['Boxes', 'Employer']));
  });

  it('puts the section score and the not-scored state in the section headers', async () => {
    render(<EvaluationReport reportUri="s3://b/packet.pdf/evaluation/report.md" documentId="packet.pdf" />);

    await waitFor(() => expect(screen.getByText('Section 1 — W2')).toBeInTheDocument());
    // Header counters render in their own node; match on content, not exact text.
    expect(screen.getByText(/82\.0% · 1 mismatched/)).toBeInTheDocument();
    // No mismatch count beside it: the placeholder attribute of a never-evaluated
    // section is not a measured mismatch.
    expect(screen.getByText('(not scored)')).toBeInTheDocument();
    expect(screen.getByText('This section was not evaluated')).toBeInTheDocument();
    expect(screen.getByText('No evaluation configuration found for Payslip')).toBeInTheDocument();
    expect(screen.getByText(/Add a configuration for 'Payslip'/)).toBeInTheDocument();
  });

  it('shows a list as a count and opens its nested comparisons on demand', async () => {
    render(<EvaluationReport reportUri="s3://b/packet.pdf/evaluation/report.md" documentId="packet.pdf" />);

    await waitFor(() => expect(screen.getByText('Boxes')).toBeInTheDocument());
    // Two cells (expected and extracted), each summarised, none flattened to JSON.
    expect(screen.getAllByText('2 items')).toHaveLength(2);
    expect(screen.queryByText(/\[\{"code"/)).not.toBeInTheDocument();
    expect(screen.getByText('0.500 (aggregate)')).toBeInTheDocument();

    // The aggregate row expands into its field-by-field comparisons.
    await userEvent.click(screen.getByRole('button', { name: 'Show nested comparisons for Boxes' }));
    expect(await screen.findByText('[0].amount')).toBeInTheDocument();
    expect(screen.getByText('paired with [1].amount')).toBeInTheDocument();
  });

  it('opens the split analysis by default when a section went wrong, with the unmatched prediction', async () => {
    render(<EvaluationReport reportUri="s3://b/packet.pdf/evaluation/report.md" documentId="packet.pdf" />);

    // Expected sections only; the stray prediction is reported separately.
    await waitFor(() => expect(screen.getByText(/1 of 2 unmatched · 1 unexpected/)).toBeInTheDocument());
    expect(screen.getByText('section 2 had no prediction')).toBeInTheDocument();
    expect(screen.getByText(/Graded packet score 75\.0%/)).toBeInTheDocument();
    // The predicted "form" section that matched nothing is listed too.
    expect(screen.getAllByText('form').length).toBeGreaterThan(0);
  });
});
