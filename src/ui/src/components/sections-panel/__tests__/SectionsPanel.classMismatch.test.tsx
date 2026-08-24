// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Wiring test for the class-mismatch annotation in the Document Sections table.
 *
 * The indicator's own logic is tested in common/__tests__; this pins the join —
 * a section's `PageIds` against the page-keyed comparison index — and the rule
 * that a section is only annotated when ground truth disagrees, so a document
 * without evaluation renders exactly as before.
 */

import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({ graphql: vi.fn() }),
}));
vi.mock('../../../contexts/app', () => ({ default: () => ({ currentCredentials: {} }) }));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({ isReviewerOnly: false, canWrite: true, canReview: true }),
}));

import SectionsPanel from '../SectionsPanel';
import { DocumentVersionProvider } from '../../../contexts/document-version';
import { extractClassificationIndex, EMPTY_CLASSIFICATION_INDEX } from '../../common/classification-comparison-utils';

// Ground truth: pages 1-2 are W2, page 3 is BankStatement.
const INDEX = extractClassificationIndex({
  doc_split_metrics: {
    page_details: [
      { page_index: 0, ground_truth_class: 'W2', predicted_class: 'W2', correct: true },
      { page_index: 1, ground_truth_class: 'W2', predicted_class: 'W2', correct: true },
      { page_index: 2, ground_truth_class: 'BankStatement', predicted_class: 'Invoice', correct: false },
    ],
  },
});

// Section 1 is right; section 2 claims Invoice for a BankStatement page.
const SECTIONS = [
  { Id: '1', Class: 'W2', PageIds: [1, 2] },
  { Id: '2', Class: 'Invoice', PageIds: [3] },
];
const DOC = { objectKey: 'doc', objectStatus: 'COMPLETED' };

const renderPanel = (classificationIndex = EMPTY_CLASSIFICATION_INDEX) =>
  render(
    <DocumentVersionProvider runId={null} files={[]}>
      <SectionsPanel {...({ sections: SECTIONS, pages: [], documentItem: DOC, classificationIndex } as Record<string, unknown>)} />
    </DocumentVersionProvider>,
  );

describe('SectionsPanel class mismatch annotation', () => {
  it('annotates the section whose class disagrees with its pages, and only that one', () => {
    renderPanel(INDEX);

    const indicators = screen.getAllByText('Class mismatch');
    expect(indicators).toHaveLength(1);

    const row = indicators[0].closest('tr');
    expect(row).not.toBeNull();
    expect(row).toHaveTextContent('Invoice');
    expect(row).not.toHaveTextContent('W2');
  });

  it('adds nothing when the document has no ground truth', () => {
    renderPanel();

    expect(screen.queryByText('Class mismatch')).not.toBeInTheDocument();
    expect(screen.getByText('W2')).toBeInTheDocument();
    expect(screen.getByText('Invoice')).toBeInTheDocument();
  });
});
