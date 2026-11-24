// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The document page's Reports › Summary, Evaluation Report and Rule Validation Summary,
// each in a dialog over the document as the page shows it (a past version's reports
// when one is being viewed). Rendered inside the page's DocumentVersionProvider, so a
// past version's reports are read at that version.
import React from 'react';
import { defineMessages, useIntl, type NoMessageValues } from 'react-intl';

import { ReportModal, SummaryReportModal } from '../common/ReportModal';
import EvaluationReport from '../document-viewer/EvaluationReport';
import { MarkdownReport } from '../document-viewer/MarkdownViewer';

export type DocumentReport = 'summary' | 'evaluation' | 'rule-validation';

const messages = defineMessages<{
  evaluationReport: NoMessageValues;
  ruleValidationSummary: NoMessageValues;
  ruleValidationEmpty: NoMessageValues;
}>({
  evaluationReport: {
    id: 'documentActions.reportDialog.evaluationReport',
    defaultMessage: 'Evaluation Report',
    description: "Title of the dialog that shows the document's evaluation report, opened from the document page's Reports menu.",
  },
  ruleValidationSummary: {
    id: 'documentActions.reportDialog.ruleValidationSummary',
    defaultMessage: 'Rule Validation Summary',
    description: "Title of the dialog that shows the document's rule validation summary, opened from the document page's Reports menu.",
  },
  ruleValidationEmpty: {
    id: 'documentActions.reportDialog.ruleValidationEmpty',
    defaultMessage: 'Rule validation summary not available for this document',
    description: 'Text of the Rule Validation Summary dialog when the document has no rule validation summary.',
  },
});

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
  const intl = useIntl();
  const { objectKey } = displayed;
  if (report === 'summary')
    return <SummaryReportModal reportUri={displayed.summaryReportUri ?? ''} objectKey={objectKey} onDismiss={onDismiss} />;
  if (report === 'evaluation') {
    return (
      <ReportModal header={intl.formatMessage(messages.evaluationReport)} size="max" onDismiss={onDismiss}>
        {/* Reads the JSON results and renders a real component, falling back to the
            markdown artifact when they cannot be read. */}
        <EvaluationReport reportUri={displayed.evaluationReportUri ?? ''} documentId={objectKey} />
      </ReportModal>
    );
  }
  return (
    <ReportModal header={intl.formatMessage(messages.ruleValidationSummary)} onDismiss={onDismiss}>
      <MarkdownReport
        reportUri={displayed.ruleValidationResultUri ?? ''}
        documentId={objectKey}
        // English on purpose: MarkdownReport splices its title into English loading and error
        // sentences, and fetches the report again whenever the title changes.
        title={String(messages.ruleValidationSummary.defaultMessage)}
        emptyMessage={intl.formatMessage(messages.ruleValidationEmpty)}
      />
    </ReportModal>
  );
};

export default DocumentReportDialog;
