// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Which documents a test run misclassified, and as what.
 *
 * The run view used to report classification as two aggregate percentages, so a
 * misclassified document could only be found by opening the markdown evaluation
 * report. That matters more than a percentage suggests: extraction runs against
 * the class's schema, so a wrong class makes every field beneath it meaningless,
 * and it can be *confidently* wrong — which means the document also ranks
 * low-priority by confidence alert count in the annotation queue and never gets
 * opened. This panel is the route in.
 */

import React from 'react';
import { Alert, Badge, Box, Container, Header, Link, Table } from '@cloudscape-design/components';

import type { ClassificationError, ClassificationErrors } from '../../graphql/awsjson-types';
import { testSetAnnotateHref } from '../../routes/constants';

interface ClassificationErrorsPanelProps {
  classificationErrors: ClassificationErrors | null;
  /** Owning test set, needed to link a row to its document. Omitted for runs whose set is unknown. */
  testSetId?: string | null;
}

const KIND_LABEL: Record<string, string> = {
  class: 'Wrong class',
  unmatched: 'No matching section',
  order: 'Page order',
};

const KIND_COLOUR: Record<string, 'red' | 'severity-medium' | 'grey'> = {
  class: 'red',
  unmatched: 'severity-medium',
  order: 'grey',
};

const formatPages = (pages: number[] | undefined): string => {
  if (!pages || pages.length === 0) return '—';
  return pages.join(', ');
};

const ClassificationErrorsPanel = ({ classificationErrors, testSetId }: ClassificationErrorsPanelProps): React.JSX.Element | null => {
  const errors = classificationErrors?.errors ?? [];
  const total = classificationErrors?.total ?? errors.length;
  const truncated = Boolean(classificationErrors?.truncated);

  // Nothing to say when a run classified everything correctly. Rendering an
  // empty table would read as "no data" rather than "no problems".
  if (total === 0) return null;

  // Counted over the CAPPED list, since the backend reports only an overall
  // `total` and no per-kind total (see MAX_CLASSIFICATION_ERRORS). So when the
  // list is truncated this is a floor, and the footer has to say so rather than
  // state it as the count.
  const classErrorCount = errors.filter((e) => e.kind === 'class').length;

  return (
    <Container
      header={
        <Header
          variant="h3"
          counter={`(${total})`}
          description="Sections whose classification disagreed with the ground truth. A wrong class means extraction ran the wrong schema, so the fields for that document are unreliable even where they look plausible."
        >
          Classification errors
        </Header>
      }
    >
      {classificationErrors?.truncated && (
        <Box padding={{ bottom: 's' }}>
          <Alert type="info">
            Showing the first {errors.length} of {total} — wrong-class errors first. The full detail is in each document&apos;s evaluation
            report.
          </Alert>
        </Box>
      )}
      <Table
        resizableColumns
        variant="embedded"
        contentDensity="compact"
        items={errors}
        columnDefinitions={[
          {
            id: 'kind',
            header: 'Issue',
            cell: (item: ClassificationError) => (
              <Badge color={KIND_COLOUR[item.kind ?? 'order'] ?? 'grey'}>{KIND_LABEL[item.kind ?? ''] ?? item.kind ?? 'Unknown'}</Badge>
            ),
            sortingField: 'kind',
          },
          {
            id: 'document',
            header: 'Document',
            cell: (item: ClassificationError) =>
              testSetId && item.doc_key ? (
                // Deep link so the row is actionable: correcting the class is
                // done in the annotation queue, not here.
                //
                // The helper supplies BOTH the leading '#' and the ?doc= param.
                // Adding either here produced '##/test-studio/...', where
                // everything after the first '#' is the fragment — so HashRouter
                // saw a path of '#/test-studio/...', matched nothing, dropped the
                // query, and every row landed on the app root.
                <Link href={testSetAnnotateHref(testSetId, item.doc_key)}>{item.doc_key}</Link>
              ) : (
                (item.doc_key ?? '—')
              ),
            sortingField: 'doc_key',
          },
          {
            id: 'expected',
            header: 'Expected class',
            cell: (item: ClassificationError) => item.expected_class ?? '—',
            sortingField: 'expected_class',
          },
          {
            id: 'predicted',
            header: 'Predicted class',
            cell: (item: ClassificationError) => item.predicted_class ?? <i>no matching section</i>,
            sortingField: 'predicted_class',
          },
          {
            id: 'pages',
            header: 'Pages (expected / predicted)',
            cell: (item: ClassificationError) => `${formatPages(item.expected_pages)} / ${formatPages(item.predicted_pages)}`,
          },
        ]}
        empty={<Box textAlign="center">No classification errors</Box>}
        footer={
          classErrorCount > 0 ? (
            <Box variant="small" color="text-body-secondary">
              {truncated ? 'At least ' : ''}
              {classErrorCount} section{classErrorCount === 1 ? '' : 's'} extracted under the wrong schema. Correct the class in the
              annotation queue and re-extract, then re-run to see the effect on accuracy.
            </Box>
          ) : undefined
        }
      />
    </Container>
  );
};

export default ClassificationErrorsPanel;
