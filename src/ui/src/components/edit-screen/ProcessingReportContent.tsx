// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit screen's Processing Report, in the bar's Reports menu: the open section's
// processing report, rendered from the section result already loaded (no request of
// its own).
import React from 'react';
import { Box } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, type NoMessageValues } from 'react-intl';

import ProcessingReportTab from '../document-viewer/ProcessingReportTab';

type ProcessingReportProps = React.ComponentProps<typeof ProcessingReportTab>;

const messages = defineMessages<{ noSection: NoMessageValues }>({
  noSection: {
    id: 'documents.processingReport.noSection',
    defaultMessage: 'Open a section to see its processing report.',
    description: "Shown in the edit page's Processing Report when no section is open.",
  },
});

interface ProcessingReportContentProps {
  /** The open section's result. */
  sectionData: Record<string, unknown> | null;
}

const ProcessingReportContent = ({ sectionData }: ProcessingReportContentProps): React.JSX.Element => {
  if (!sectionData) {
    return (
      <Box color="text-body-secondary">
        <FormattedMessage {...messages.noSection} />
      </Box>
    );
  }
  const metadata = sectionData.metadata as ProcessingReportProps['metadata'];
  return (
    <ProcessingReportTab
      metadata={metadata}
      processingReport={sectionData.processing_report as string | undefined}
      inferenceResult={(sectionData.inference_result ?? sectionData.inferenceResult) as Record<string, unknown> | undefined}
      processingIssues={(metadata as Record<string, unknown> | undefined)?.processing_issues as ProcessingReportProps['processingIssues']}
    />
  );
};

export default ProcessingReportContent;
