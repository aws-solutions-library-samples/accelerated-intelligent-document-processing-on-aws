// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The edit screen's Processing Report, a tab of History: the open section's
// processing report, rendered from the section result already loaded (no request of
// its own).
import React from 'react';
import { Box } from '@cloudscape-design/components';

import ProcessingReportTab from '../document-viewer/ProcessingReportTab';

type ProcessingReportProps = React.ComponentProps<typeof ProcessingReportTab>;

interface ProcessingReportContentProps {
  /** The open section's result. */
  sectionData: Record<string, unknown> | null;
}

const ProcessingReportContent = ({ sectionData }: ProcessingReportContentProps): React.JSX.Element => {
  if (!sectionData) return <Box color="text-body-secondary">Open a section to see its processing report.</Box>;
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
