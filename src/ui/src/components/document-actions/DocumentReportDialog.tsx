// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The document page's Reports › Summary, Evaluation Report and Rule Validation Summary,
// each in a dialog over the document as the page shows it (a past version's reports
// when one is being viewed). Rendered inside the page's DocumentVersionProvider, so a
// past version's reports are read at that version.
import React from 'react';

import { ReportModal, SummaryReportModal } from '../common/ReportModal';
import EvaluationReport from '../document-viewer/EvaluationReport';
import { MarkdownReport } from '../document-viewer/MarkdownViewer';

export type DocumentReport = 'summary' | 'evaluation' | 'rule-validation';

interface DocumentReportDialogProps {
  report: DocumentReport;
  displayed: {
    objectKey: string;
    summaryReportUri?: string | null;
    evaluationReportUri?: string | null;
    ruleValidationResultUri?: string | null;
  };
  onDismiss: () => void;
}

const DocumentReportDialog = ({ report, displayed, onDismiss }: DocumentReportDialogProps): React.JSX.Element => {
  const { objectKey } = displayed;
  if (report === 'summary')
    return <SummaryReportModal reportUri={displayed.summaryReportUri ?? ''} objectKey={objectKey} onDismiss={onDismiss} />;
  if (report === 'evaluation') {
    return (
      <ReportModal header="Evaluation Report" size="max" onDismiss={onDismiss}>
        {/* Reads the JSON results and renders a real component, falling back to the
            markdown artifact when they cannot be read. */}
        <EvaluationReport reportUri={displayed.evaluationReportUri ?? ''} documentId={objectKey} />
      </ReportModal>
    );
  }
  return (
    <ReportModal header="Rule Validation Summary" onDismiss={onDismiss}>
      <MarkdownReport
        reportUri={displayed.ruleValidationResultUri ?? ''}
        documentId={objectKey}
        title="Rule Validation Summary"
        emptyMessage="Rule validation summary not available for this document"
      />
    </ReportModal>
  );
};

export default DocumentReportDialog;
