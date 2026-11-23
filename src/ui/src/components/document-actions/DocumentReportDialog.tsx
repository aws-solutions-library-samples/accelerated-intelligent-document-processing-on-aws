// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The document page's Reports › Summary, Evaluation Report and Policy Rule Validation Summary,
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
  policyRuleValidationSummary: NoMessageValues;
  policyRuleValidationEmpty: NoMessageValues;
}>({
  policyRuleValidationSummary: {
    id: 'documentActions.reportDialog.policyRuleValidationSummary',
    defaultMessage: 'Policy Rule Validation Summary',
    description: "Header and title of the dialog that shows a document's policy rule validation summary report.",
  },
  policyRuleValidationEmpty: {
    id: 'documentActions.reportDialog.policyRuleValidationEmpty',
    defaultMessage: 'Policy rule validation summary not available for this document',
    description: 'Shown in the Policy Rule Validation Summary dialog when the document has no such report.',
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
      <ReportModal header="Evaluation Report" size="max" onDismiss={onDismiss}>
        {/* Reads the JSON results and renders a real component, falling back to the
            markdown artifact when they cannot be read. */}
        <EvaluationReport reportUri={displayed.evaluationReportUri ?? ''} documentId={objectKey} />
      </ReportModal>
    );
  }
  return (
    <ReportModal header={intl.formatMessage(messages.policyRuleValidationSummary)} onDismiss={onDismiss}>
      <MarkdownReport
        reportUri={displayed.ruleValidationResultUri ?? ''}
        documentId={objectKey}
        title={intl.formatMessage(messages.policyRuleValidationSummary)}
        emptyMessage={intl.formatMessage(messages.policyRuleValidationEmpty)}
      />
    </ReportModal>
  );
};

export default DocumentReportDialog;
