// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Tests for the in-place class-mismatch annotations.
 *
 * The load-bearing property is that these render **nothing** unless ground
 * truth actually disagrees: they sit inside the Document Sections and Document
 * Pages tables, so a document with no evaluation — or one classified correctly
 * — must look exactly as it did before. The Visual Editor's variant is the
 * exception and shows both values, because the user turned on Show Evaluation
 * to see them.
 */

import React from 'react';
import { describe, expect, it } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import { PageClassMismatch, SectionClassMismatch, SectionClassEvaluation } from '../ClassMismatchIndicator';
import { extractClassificationIndex, EMPTY_CLASSIFICATION_INDEX } from '../classification-comparison-utils';

// Pages 1-2 are W2 in both; page 3 is BankStatement in ground truth but was
// classified Invoice; page 4 is Invoice in both.
const INDEX = extractClassificationIndex({
  doc_split_metrics: {
    page_details: [
      { page_index: 0, ground_truth_class: 'W2', predicted_class: 'W2', correct: true },
      { page_index: 1, ground_truth_class: 'W2', predicted_class: 'W2', correct: true },
      { page_index: 2, ground_truth_class: 'BankStatement', predicted_class: 'Invoice', correct: false },
      { page_index: 3, ground_truth_class: 'Invoice', predicted_class: 'Invoice', correct: true },
    ],
  },
});

describe('PageClassMismatch', () => {
  it('renders nothing when the document has no ground truth', () => {
    const { container } = render(<PageClassMismatch index={EMPTY_CLASSIFICATION_INDEX} pageNumber={1} predictedClass="W2" />);

    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when the page class matches', () => {
    const { container } = render(<PageClassMismatch index={INDEX} pageNumber={1} predictedClass="W2" />);

    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing for a page ground truth says nothing about', () => {
    const { container } = render(<PageClassMismatch index={INDEX} pageNumber={99} predictedClass="W2" />);

    expect(container).toBeEmptyDOMElement();
  });

  it('flags a mismatched page and names the expected class on hover', () => {
    render(<PageClassMismatch index={INDEX} pageNumber={3} predictedClass="Invoice" />);

    const trigger = screen.getByText('Class mismatch');
    expect(trigger).toBeInTheDocument();

    fireEvent.click(trigger);
    expect(screen.getByText('Does not match ground truth')).toBeInTheDocument();
    expect(screen.getByText('BankStatement')).toBeInTheDocument();
  });
});

describe('SectionClassMismatch', () => {
  it('renders nothing when every page of the section has the section class', () => {
    const { container } = render(<SectionClassMismatch index={INDEX} pageNumbers={[1, 2]} predictedClass="W2" />);

    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when the document has no ground truth', () => {
    const { container } = render(<SectionClassMismatch index={EMPTY_CLASSIFICATION_INDEX} pageNumbers={[1, 2]} predictedClass="W2" />);

    expect(container).toBeEmptyDOMElement();
  });

  it('flags a wrong class and names what ground truth expects', () => {
    render(<SectionClassMismatch index={INDEX} pageNumbers={[3]} predictedClass="Invoice" />);

    fireEvent.click(screen.getByText('Class mismatch'));
    expect(screen.getByText('Does not match ground truth')).toBeInTheDocument();
    expect(screen.getByText(/BankStatement — page 3/)).toBeInTheDocument();
  });

  it('reports a section that merged two ground-truth documents', () => {
    // Pages 3-4 are BankStatement and Invoice in ground truth. The section is
    // "Invoice", so a class-only check would pass it — the boundary is what is
    // wrong, and both classes are the diagnostic.
    render(<SectionClassMismatch index={INDEX} pageNumbers={[3, 4]} predictedClass="Invoice" />);

    fireEvent.click(screen.getByText('Class mismatch'));
    expect(screen.getByText('Section spans more than one ground-truth class')).toBeInTheDocument();
    expect(screen.getByText(/BankStatement — page 3/)).toBeInTheDocument();
    expect(screen.getByText(/Invoice — page 4/)).toBeInTheDocument();
  });
});

describe('SectionClassEvaluation', () => {
  it('shows both classes when they agree, since evaluation view was asked for', () => {
    render(<SectionClassEvaluation index={INDEX} pageNumbers={[1, 2]} predictedClass="W2" />);

    expect(screen.getByText('Document class (this run)')).toBeInTheDocument();
    expect(screen.getByText('Document class (ground truth)')).toBeInTheDocument();
    expect(screen.getByText('Class matches')).toBeInTheDocument();
  });

  it('shows the mismatch and why the field scores below it are a symptom', () => {
    render(<SectionClassEvaluation index={INDEX} pageNumbers={[3]} predictedClass="Invoice" />);

    expect(screen.getByText('Class mismatch')).toBeInTheDocument();
    expect(screen.getByText('BankStatement')).toBeInTheDocument();
    expect(screen.getByText(/extracted against this class's schema/)).toBeInTheDocument();
  });

  it('falls back to the baseline file class when there is no page-level comparison', () => {
    // A document with a baseline but no doc-split metrics still has a ground
    // truth class for the section.
    render(<SectionClassEvaluation index={EMPTY_CLASSIFICATION_INDEX} pageNumbers={[1]} predictedClass="Invoice" baselineClass="W2" />);

    expect(screen.getByText('W2')).toBeInTheDocument();
    expect(screen.getByText('Class mismatch')).toBeInTheDocument();
  });

  it('renders nothing when there is no ground truth from either source', () => {
    const { container } = render(<SectionClassEvaluation index={EMPTY_CLASSIFICATION_INDEX} pageNumbers={[1]} predictedClass="Invoice" />);

    expect(container).toBeEmptyDOMElement();
  });
});
