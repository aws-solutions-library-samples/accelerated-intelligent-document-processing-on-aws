// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Wiring test for the class-mismatch annotation in the Document Pages table.
 *
 * The indicator itself is tested in common/__tests__; what this pins is the
 * join between the two, which is the part that can silently break: page rows
 * are keyed by a string `Id` ("1", "2", …) while the comparison index is keyed
 * by 1-based page *number*, derived from the 0-based `page_index` in
 * results.json. An off-by-one here would annotate the wrong page — worse than
 * not annotating at all, since the alert names a class.
 */

import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({ graphql: vi.fn() }),
}));
vi.mock('../../common/generate-s3-presigned-url', () => ({
  default: vi.fn().mockResolvedValue('https://example/thumb.jpg'),
}));
vi.mock('../../../contexts/app', () => ({ default: () => ({ currentCredentials: {} }) }));
vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({ isReviewerOnly: false, canWrite: true, canReview: true }),
}));

import PagesPanel from '../PagesPanel';
import { DocumentVersionProvider } from '../../../contexts/document-version';
import { extractClassificationIndex, EMPTY_CLASSIFICATION_INDEX } from '../../common/classification-comparison-utils';

// Page 1 is correct; page 2 was classified Receipt but ground truth says W2.
// The two rows carry *different* predicted classes so an off-by-one in the join
// flags a visibly different row rather than merely a different count.
const INDEX = extractClassificationIndex({
  doc_split_metrics: {
    page_details: [
      { page_index: 0, ground_truth_class: 'Invoice', predicted_class: 'Invoice', correct: true },
      { page_index: 1, ground_truth_class: 'W2', predicted_class: 'Receipt', correct: false },
    ],
  },
});

const PAGES = [
  { Id: '1', Class: 'Invoice', ImageUri: 's3://b/doc/pages/1/image.jpg' },
  { Id: '2', Class: 'Receipt', ImageUri: 's3://b/doc/pages/2/image.jpg' },
];
const DOC = { objectKey: 'doc', objectStatus: 'COMPLETED' };

const renderPanel = (classificationIndex = EMPTY_CLASSIFICATION_INDEX) =>
  render(
    <DocumentVersionProvider runId={null} files={[]}>
      <PagesPanel {...({ pages: PAGES, documentItem: DOC, classificationIndex } as Record<string, unknown>)} />
    </DocumentVersionProvider>,
  );

describe('PagesPanel class mismatch annotation', () => {
  it('annotates the row for the page ground truth disagrees about, and only that row', () => {
    renderPanel(INDEX);

    const indicators = screen.getAllByText('Class mismatch');
    expect(indicators).toHaveLength(1);

    // Row-scoped, not just counted: an off-by-one in the page-number join would
    // still produce one indicator, on the wrong page.
    const row = indicators[0].closest('tr');
    expect(row).not.toBeNull();
    expect(row).toHaveTextContent('Receipt');
    expect(row).not.toHaveTextContent('Invoice');
  });

  it('adds nothing when the document has no ground truth', () => {
    renderPanel();

    expect(screen.queryByText('Class mismatch')).not.toBeInTheDocument();
    // The class values still render as before.
    expect(screen.getByText('Invoice')).toBeInTheDocument();
    expect(screen.getByText('Receipt')).toBeInTheDocument();
  });
});
