// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { Box, Popover, SpaceBetween, StatusIndicator } from '@cloudscape-design/components';
import {
  ClassificationIndex,
  SectionClassComparison,
  compareSectionClass,
  comparePageClass,
  formatPageRanges,
  groundTruthClassForPage,
} from './classification-comparison-utils';

/**
 * The alert shown next to a class value that disagrees with ground truth.
 *
 * Rendered inline in the Document Sections and Document Pages tables rather
 * than as its own panel: the class is already displayed there, and the question
 * "is this the class ground truth expects?" belongs next to the answer it
 * qualifies. Nothing is rendered when there is no ground truth for the row, so
 * a document that was never evaluated looks exactly as it did before.
 */
const MismatchPopover = ({ header, children }: { header: string; children: React.ReactNode }): React.JSX.Element => (
  <Popover dismissButton={false} position="top" size="medium" triggerType="custom" header={header} content={children}>
    <StatusIndicator type="warning">Class mismatch</StatusIndicator>
  </Popover>
);

/** Ground truth for one page, beside the class this run assigned it. */
export const PageClassMismatch = ({
  index,
  pageNumber,
  predictedClass,
}: {
  index: ClassificationIndex;
  pageNumber: number;
  predictedClass: string | null | undefined;
}): React.JSX.Element | null => {
  if (!index.hasGroundTruth) return null;
  if (comparePageClass(index, pageNumber, predictedClass) !== 'mismatch') return null;

  const expected = groundTruthClassForPage(index, pageNumber);

  return (
    <MismatchPopover header="Does not match ground truth">
      <SpaceBetween size="xs">
        <Box>
          <Box variant="awsui-key-label">Ground truth</Box>
          {expected}
        </Box>
        <Box>
          <Box variant="awsui-key-label">This run</Box>
          {predictedClass || 'not classified'}
        </Box>
        <Box variant="small" color="text-body-secondary">
          This page was extracted against the schema for {predictedClass || 'no class'}, so any low extraction score for it is a symptom of
          the classification rather than the cause.
        </Box>
      </SpaceBetween>
    </MismatchPopover>
  );
};

/**
 * Ground truth for a section's pages, beside the class this run assigned.
 *
 * A section spanning two ground-truth classes is reported as a mismatch listing
 * both, even when one of them equals the predicted class: in that case the
 * section *boundary* is what is wrong, and naming both classes is what makes
 * that visible.
 */
export const SectionClassMismatch = ({
  index,
  pageNumbers,
  predictedClass,
}: {
  index: ClassificationIndex;
  pageNumbers: number[];
  predictedClass: string | null | undefined;
}): React.JSX.Element | null => {
  if (!index.hasGroundTruth) return null;

  const comparison: SectionClassComparison = compareSectionClass(index, pageNumbers, predictedClass);
  if (comparison.verdict !== 'mismatch') return null;

  const spansMultipleClasses = comparison.expected.length > 1;

  return (
    <MismatchPopover header={spansMultipleClasses ? 'Section spans more than one ground-truth class' : 'Does not match ground truth'}>
      <SpaceBetween size="xs">
        <Box>
          <Box variant="awsui-key-label">Ground truth</Box>
          <SpaceBetween size="xxs">
            {comparison.expected.map(({ className, pageNumbers: expectedPages }) => (
              <Box key={className}>
                {className} — page{expectedPages.length > 1 ? 's' : ''} {formatPageRanges(expectedPages)}
              </Box>
            ))}
          </SpaceBetween>
        </Box>
        <Box>
          <Box variant="awsui-key-label">This run</Box>
          {predictedClass || 'not classified'} — page{pageNumbers.length > 1 ? 's' : ''} {formatPageRanges(pageNumbers)}
        </Box>
        {comparison.unknownPageNumbers.length > 0 && (
          <Box variant="small" color="text-body-secondary">
            Ground truth has no class for page{comparison.unknownPageNumbers.length > 1 ? 's' : ''}{' '}
            {formatPageRanges(comparison.unknownPageNumbers)}.
          </Box>
        )}
        <Box variant="small" color="text-body-secondary">
          {spansMultipleClasses
            ? 'Ground truth splits these pages into separate documents, so this section was extracted as one document when it is more than one.'
            : 'Fields in this section were extracted against the wrong schema, so its extraction scores are a symptom of the classification rather than the cause.'}
        </Box>
      </SpaceBetween>
    </MismatchPopover>
  );
};

/**
 * The section's class beside what ground truth expects, shown in full.
 *
 * Used in the Visual Editor's **Show Evaluation** mode, where the user has
 * explicitly asked to see prediction-vs-ground-truth, so both values are shown
 * whether or not they agree — unlike the table indicators above, which stay
 * silent unless something is wrong.
 *
 * Prefers the evaluation comparison (which is page-based and so still says
 * something useful when the split itself is wrong) and falls back to the
 * baseline file's own `document_class.type` when the document carries no
 * page-level comparison.
 */
export const SectionClassEvaluation = ({
  index,
  pageNumbers,
  predictedClass,
  baselineClass,
}: {
  index: ClassificationIndex;
  pageNumbers: number[];
  predictedClass: string | null | undefined;
  baselineClass?: string | null;
}): React.JSX.Element | null => {
  const fromIndex = index.hasGroundTruth ? compareSectionClass(index, pageNumbers, predictedClass) : null;

  let expectedLabel: string | null = null;
  let matched = false;

  if (fromIndex && fromIndex.expected.length > 0) {
    expectedLabel =
      fromIndex.expected.length === 1
        ? fromIndex.expected[0].className
        : fromIndex.expected
            .map(({ className, pageNumbers: pages }) => `${className} (page${pages.length > 1 ? 's' : ''} ${formatPageRanges(pages)})`)
            .join(', ');
    matched = fromIndex.verdict === 'match';
  } else if (baselineClass) {
    expectedLabel = baselineClass;
    matched = baselineClass === predictedClass;
  }

  // No ground truth for this section's class: say nothing rather than imply the
  // prediction was confirmed.
  if (!expectedLabel) return null;

  return (
    <Box padding={{ bottom: 'xs' }}>
      <SpaceBetween direction="horizontal" size="l" alignItems="center">
        <Box>
          <Box variant="awsui-key-label">Document class (this run)</Box>
          {predictedClass || 'not classified'}
        </Box>
        <Box>
          <Box variant="awsui-key-label">Document class (ground truth)</Box>
          {expectedLabel}
        </Box>
        <StatusIndicator type={matched ? 'success' : 'error'}>{matched ? 'Class matches' : 'Class mismatch'}</StatusIndicator>
      </SpaceBetween>
      {!matched && (
        <Box variant="small" color="text-body-secondary" padding={{ top: 'xxs' }}>
          {fromIndex && fromIndex.expected.length > 1
            ? 'Ground truth splits these pages into separate documents, so the fields below were extracted as one document when they are more than one.'
            : "The fields below were extracted against this class's schema, so their scores reflect the wrong schema rather than a bad extraction."}
        </Box>
      )}
    </Box>
  );
};
