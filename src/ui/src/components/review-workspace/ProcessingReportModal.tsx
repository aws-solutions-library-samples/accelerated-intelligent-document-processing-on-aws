// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The unified editor's Processing Report: the open section's processing report,
// rendered from the section result already loaded (no request of its own).
import React from 'react';
import { Box, Button, Modal } from '@cloudscape-design/components';

import ProcessingReportTab from '../document-viewer/ProcessingReportTab';

type ProcessingReportProps = React.ComponentProps<typeof ProcessingReportTab>;

interface ProcessingReportModalProps {
  visible: boolean;
  onDismiss: () => void;
  /** The open section's result. */
  sectionData: Record<string, unknown> | null;
}

const ProcessingReportModal = ({ visible, onDismiss, sectionData }: ProcessingReportModalProps): React.JSX.Element => {
  const metadata = sectionData?.metadata as ProcessingReportProps['metadata'];
  return (
    <Modal
      visible={visible}
      size="large"
      onDismiss={onDismiss}
      header="Processing report"
      footer={
        <Box float="right">
          <Button variant="primary" onClick={onDismiss}>
            Close
          </Button>
        </Box>
      }
    >
      {sectionData ? (
        <ProcessingReportTab
          metadata={metadata}
          processingReport={sectionData.processing_report as string | undefined}
          inferenceResult={(sectionData.inference_result ?? sectionData.inferenceResult) as Record<string, unknown> | undefined}
          processingIssues={
            (metadata as Record<string, unknown> | undefined)?.processing_issues as ProcessingReportProps['processingIssues']
          }
        />
      ) : (
        <Box color="text-body-secondary">Open a section to see its processing report.</Box>
      )}
    </Modal>
  );
};

export default ProcessingReportModal;
