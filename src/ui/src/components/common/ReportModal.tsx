// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// A read-only report opened from a page's bar (the Reports menu), with a Close button.
// The edit page and the document page share it, and the Summary report with it.
import React from 'react';
import { Box, Button, Modal } from '@cloudscape-design/components';
import type { ModalProps } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';

import { MarkdownReport } from '../document-viewer/MarkdownViewer';

/** The label of the report dialogs' Close button. */
export const reportModalMessages = defineMessages<{ close: NoMessageValues }>({
  close: {
    id: 'common.reportModal.close',
    defaultMessage: 'Close',
    description: 'Button that closes a report dialog opened from the Reports menu of the document page or the edit page.',
  },
});

const summaryMessages = defineMessages<{ title: NoMessageValues; empty: NoMessageValues }>({
  title: {
    id: 'common.summaryReport.title',
    defaultMessage: 'Document Summary',
    description:
      "Title of the dialog that shows the document's summary, opened from Reports › Summary on the document page or the edit page.",
  },
  empty: {
    id: 'common.summaryReport.empty',
    defaultMessage: 'Summary report not available for this document',
    description: 'Text of the Document Summary dialog when the document has no summary report.',
  },
});

interface ReportModalProps {
  header: string;
  onDismiss: () => void;
  children: React.ReactNode;
  size?: ModalProps.Size;
}

/**
 * The one width of the dialogs the Reports menus open: Summary, Processing Report,
 * Policy Rule Validation Summary, Estimated Cost and the editor's History. A report that
 * needs more room (Evaluation Report) passes its own size.
 */
export const REPORT_MODAL_SIZE: ModalProps.Size = 'x-large';

/** A report opened from the bar: Summary, Processing Report, Evaluation Report. */
export const ReportModal = ({ header, onDismiss, children, size = REPORT_MODAL_SIZE }: ReportModalProps): React.JSX.Element => (
  <Modal
    visible
    size={size}
    onDismiss={onDismiss}
    header={header}
    footer={
      <Box float="right">
        <Button variant="primary" onClick={onDismiss}>
          <FormattedMessage {...reportModalMessages.close} />
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
}): React.JSX.Element => {
  const intl = useIntl();
  return (
    <ReportModal header={intl.formatMessage(summaryMessages.title)} onDismiss={onDismiss}>
      <MarkdownReport
        reportUri={reportUri}
        documentId={objectKey}
        report="summary"
        emptyMessage={intl.formatMessage(summaryMessages.empty)}
      />
    </ReportModal>
  );
};

export default ReportModal;
