// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { Box, Popover, SpaceBetween, StatusIndicator } from '@cloudscape-design/components';
import {
  defineMessages,
  FormattedMessage,
  useIntl,
  type MessageValue,
  type NoMessageValues,
  type TypedMessageDescriptor,
} from 'react-intl';
import {
  ClassificationIndex,
  ExpectedClass,
  SectionClassComparison,
  compareSectionClass,
  comparePageClass,
  formatPageRanges,
  groundTruthClassForPage,
} from './classification-comparison-utils';

// Every react-intl call below sits in a part that renders only once there is something to
// show, after the early `return null`s: a screen without ground truth needs no provider.
// A `className` typed `MessageValue` also takes a <FormattedMessage> element, the fallback
// for a page or section this run did not classify.
const messages = defineMessages<{
  badge: NoMessageValues;
  matches: NoMessageValues;
  header: NoMessageValues;
  headerSpans: NoMessageValues;
  groundTruth: NoMessageValues;
  thisRun: NoMessageValues;
  notClassified: NoMessageValues;
  noClass: NoMessageValues;
  pageExplanation: { readonly className: MessageValue };
  classPages: { readonly className: MessageValue; readonly count: number; readonly ranges: string };
  unknownPages: { readonly count: number; readonly ranges: string };
  sectionSplit: NoMessageValues;
  sectionWrongSchema: NoMessageValues;
  evalSplit: NoMessageValues;
  evalWrongSchema: NoMessageValues;
  classPagesInline: { readonly className: string; readonly count: number; readonly ranges: string };
  classThisRun: NoMessageValues;
  classGroundTruth: NoMessageValues;
}>({
  badge: {
    id: 'documents.classMismatch.badge',
    defaultMessage: 'Class mismatch',
    description: "Warning shown next to a class that differs from the ground truth's class.",
  },
  matches: {
    id: 'documents.classMismatch.matches',
    defaultMessage: 'Class matches',
    description: "Shown when a section's class equals the ground truth's class.",
  },
  header: {
    id: 'documents.classMismatch.header',
    defaultMessage: 'Does not match ground truth',
    description: 'Heading of the popover explaining a class mismatch.',
  },
  headerSpans: {
    id: 'documents.classMismatch.headerSpans',
    defaultMessage: 'Section spans more than one ground-truth class',
    description: "Heading of the popover when a section's pages belong to several ground-truth classes.",
  },
  groundTruth: {
    id: 'documents.classMismatch.groundTruth',
    defaultMessage: 'Ground truth',
    description: 'Label of the class the ground truth expects.',
  },
  thisRun: {
    id: 'documents.classMismatch.thisRun',
    defaultMessage: 'This run',
    description: 'Label of the class this processing run assigned.',
  },
  notClassified: {
    id: 'documents.classMismatch.notClassified',
    defaultMessage: 'not classified',
    description: 'Shown in place of the class when this run assigned none.',
  },
  noClass: {
    id: 'documents.classMismatch.noClass',
    defaultMessage: 'no class',
    description: 'Fills {className} of the page explanation when this run assigned no class.',
  },
  pageExplanation: {
    id: 'documents.classMismatch.pageExplanation',
    defaultMessage:
      'This page was extracted against the schema for {className}, so any low extraction score for it is a symptom of the classification rather than the cause.',
    description: 'Explanation in the page popover. {className} is the class this run assigned to the page.',
  },
  classPages: {
    id: 'documents.classMismatch.classPages',
    defaultMessage: '{className} — {count, plural, one {page} other {pages}} {ranges}',
    description: 'A class and its pages in the section popover. {count} is the number of pages; {ranges} lists them, such as 1-3, 5.',
  },
  unknownPages: {
    id: 'documents.classMismatch.unknownPages',
    defaultMessage: 'Ground truth has no class for {count, plural, one {page} other {pages}} {ranges}.',
    description: 'Note in the section popover. {count} is the number of pages; {ranges} lists them, such as 1-3, 5.',
  },
  sectionSplit: {
    id: 'documents.classMismatch.sectionSplit',
    defaultMessage:
      'Ground truth splits these pages into separate sections, so this section was extracted as one when it is more than one.',
    description: "Explanation in the section popover when the section's pages belong to several ground-truth classes.",
  },
  sectionWrongSchema: {
    id: 'documents.classMismatch.sectionWrongSchema',
    defaultMessage:
      'Fields in this section were extracted against the wrong schema, so its extraction scores are a symptom of the classification rather than the cause.',
    description: "Explanation in the section popover when the section's class is wrong.",
  },
  evalSplit: {
    id: 'documents.classMismatch.evalSplit',
    defaultMessage:
      'Ground truth splits these pages into separate sections, so the fields below were extracted as one section when they are more than one.',
    description: "Explanation above a section's fields in the evaluation view when its pages belong to several ground-truth classes.",
  },
  evalWrongSchema: {
    id: 'documents.classMismatch.evalWrongSchema',
    defaultMessage:
      "The fields below were extracted against this class's schema, so their scores reflect the wrong schema rather than a bad extraction.",
    description: "Explanation above a section's fields in the evaluation view when the section's class is wrong.",
  },
  classPagesInline: {
    id: 'documents.classMismatch.classPagesInline',
    defaultMessage: '{className} ({count, plural, one {page} other {pages}} {ranges})',
    description:
      'One ground-truth class and its pages, in a comma-separated list. {count} is the number of pages; {ranges} lists them, such as 1-3, 5.',
  },
  classThisRun: {
    id: 'documents.classMismatch.classThisRun',
    defaultMessage: 'Document class (this run)',
    description: 'Label of the class this run assigned to the section, in the evaluation view.',
  },
  classGroundTruth: {
    id: 'documents.classMismatch.classGroundTruth',
    defaultMessage: 'Document class (ground truth)',
    description: 'Label of the class the ground truth expects for the section, in the evaluation view.',
  },
});

/**
 * The alert shown next to a class value that disagrees with ground truth.
 *
 * Rendered inline in the Document Sections and Document Pages tables rather
 * than as its own panel: the class is already displayed there, and the question
 * "is this the class ground truth expects?" belongs next to the answer it
 * qualifies. Nothing is rendered when there is no ground truth for the row, so
 * a document that was never evaluated looks exactly as it did before.
 */
const MismatchPopover = ({
  header,
  children,
}: {
  header: TypedMessageDescriptor<NoMessageValues>;
  children: React.ReactNode;
}): React.JSX.Element => {
  const intl = useIntl();
  return (
    <Popover dismissButton={false} position="top" size="medium" triggerType="custom" header={intl.formatMessage(header)} content={children}>
      <StatusIndicator type="warning">
        <FormattedMessage {...messages.badge} />
      </StatusIndicator>
    </Popover>
  );
};

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
    <MismatchPopover header={messages.header}>
      <SpaceBetween size="xs">
        <Box>
          <Box variant="awsui-key-label">
            <FormattedMessage {...messages.groundTruth} />
          </Box>
          {expected}
        </Box>
        <Box>
          <Box variant="awsui-key-label">
            <FormattedMessage {...messages.thisRun} />
          </Box>
          {predictedClass || <FormattedMessage {...messages.notClassified} />}
        </Box>
        <Box variant="small" color="text-body-secondary">
          <FormattedMessage
            {...messages.pageExplanation}
            values={{ className: predictedClass || <FormattedMessage {...messages.noClass} /> }}
          />
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
    <MismatchPopover header={spansMultipleClasses ? messages.headerSpans : messages.header}>
      <SpaceBetween size="xs">
        <Box>
          <Box variant="awsui-key-label">
            <FormattedMessage {...messages.groundTruth} />
          </Box>
          <SpaceBetween size="xxs">
            {comparison.expected.map(({ className, pageNumbers: expectedPages }) => (
              <Box key={className}>
                <FormattedMessage
                  {...messages.classPages}
                  values={{ className, count: expectedPages.length, ranges: formatPageRanges(expectedPages) }}
                />
              </Box>
            ))}
          </SpaceBetween>
        </Box>
        <Box>
          <Box variant="awsui-key-label">
            <FormattedMessage {...messages.thisRun} />
          </Box>
          <FormattedMessage
            {...messages.classPages}
            values={{
              className: predictedClass || <FormattedMessage {...messages.notClassified} />,
              count: pageNumbers.length,
              ranges: formatPageRanges(pageNumbers),
            }}
          />
        </Box>
        {comparison.unknownPageNumbers.length > 0 && (
          <Box variant="small" color="text-body-secondary">
            <FormattedMessage
              {...messages.unknownPages}
              values={{ count: comparison.unknownPageNumbers.length, ranges: formatPageRanges(comparison.unknownPageNumbers) }}
            />
          </Box>
        )}
        <Box variant="small" color="text-body-secondary">
          {spansMultipleClasses ? <FormattedMessage {...messages.sectionSplit} /> : <FormattedMessage {...messages.sectionWrongSchema} />}
        </Box>
      </SpaceBetween>
    </MismatchPopover>
  );
};

/** Ground truth's classes for a section, each with its pages, comma-separated: `Invoice (pages 1-2), W2 (page 3)`. */
const ExpectedClassList = ({ expected }: { expected: ExpectedClass[] }): React.JSX.Element => {
  const intl = useIntl();
  return (
    <>
      {expected
        .map(({ className, pageNumbers: pages }) =>
          intl.formatMessage(messages.classPagesInline, { className, count: pages.length, ranges: formatPageRanges(pages) }),
        )
        .join(', ')}
    </>
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

  let expectedLabel: React.ReactNode = null;
  let matched = false;

  if (fromIndex && fromIndex.expected.length > 0) {
    expectedLabel = fromIndex.expected.length === 1 ? fromIndex.expected[0].className : <ExpectedClassList expected={fromIndex.expected} />;
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
          <Box variant="awsui-key-label">
            <FormattedMessage {...messages.classThisRun} />
          </Box>
          {predictedClass || <FormattedMessage {...messages.notClassified} />}
        </Box>
        <Box>
          <Box variant="awsui-key-label">
            <FormattedMessage {...messages.classGroundTruth} />
          </Box>
          {expectedLabel}
        </Box>
        <StatusIndicator type={matched ? 'success' : 'error'}>
          {matched ? <FormattedMessage {...messages.matches} /> : <FormattedMessage {...messages.badge} />}
        </StatusIndicator>
      </SpaceBetween>
      {!matched && (
        <Box variant="small" color="text-body-secondary" padding={{ top: 'xxs' }}>
          {fromIndex && fromIndex.expected.length > 1 ? (
            <FormattedMessage {...messages.evalSplit} />
          ) : (
            <FormattedMessage {...messages.evalWrongSchema} />
          )}
        </Box>
      )}
    </Box>
  );
};
