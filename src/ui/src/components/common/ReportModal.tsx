// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// A read-only report opened from a page's bar (the Reports menu), with a Close button.
// The edit page and the document page share it, and the Summary report with it.
import React from 'react';
import { Box, Button, Modal } from '@cloudscape-design/components';
import type { ModalProps } from '@cloudscape-design/components';

import { MarkdownReport } from '../document-viewer/MarkdownViewer';

interface ReportModalProps {
  header: string;
  onDismiss: () => void;
  children: React.ReactNode;
  size?: ModalProps.Size;
}

/** A report opened from the bar: Summary, Processing Report, Evaluation Report. */
export const ReportModal = ({ header, onDismiss, children, size = 'large' }: ReportModalProps): React.JSX.Element => (
  <Modal
    visible
    size={size}
    onDismiss={onDismiss}
    header={header}
    footer={
      <Box float="right">
        <Button variant="primary" onClick={onDismiss}>
          Close
        </Button>
      </Box>
    }
  >
    {children}
  </Modal>
);

/** The document's Summary report, in a ReportModal. */
export const SummaryReportModal = ({
  reportUri,
  objectKey,
  onDismiss,
}: {
  reportUri: string;
  objectKey: string;
  onDismiss: () => void;
}): React.JSX.Element => (
  <ReportModal header="Document Summary" onDismiss={onDismiss}>
    <MarkdownReport
      reportUri={reportUri}
      documentId={objectKey}
      title="Document Summary"
      emptyMessage="Summary report not available for this document"
    />
  </ReportModal>
);

export default ReportModal;
