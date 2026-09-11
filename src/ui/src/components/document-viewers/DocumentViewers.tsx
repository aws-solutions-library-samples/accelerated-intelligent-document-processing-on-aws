// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState, useEffect } from 'react';
import { SpaceBetween, Box, Button, StatusIndicator } from '@cloudscape-design/components';
import { generateClient } from '../../api/client-shim';
import { ConsoleLogger } from 'aws-amplify/utils';

import { copyToBaseline } from '../../graphql/generated';
import FileViewer from '../document-viewer/FileViewer';
import { MarkdownReport } from '../document-viewer/MarkdownViewer';
import EvaluationReport from '../document-viewer/EvaluationReport';

interface ViewerControlsProps {
  onViewSource: () => void;
  onViewReport: () => void;
  onViewSummary: () => void;
  onViewRuleValidation: () => void;
  onSetAsBaseline: () => void;
  isSourceVisible: boolean;
  isReportVisible: boolean;
  isSummaryVisible: boolean;
  isRuleValidationVisible: boolean;
  evaluationReportUri?: string;
  summaryReportUri?: string;
  ruleValidationResultUri?: string;
  copyStatus: string | null;
  evaluationStatus?: string;
}

interface ViewerContentProps {
  isSourceVisible: boolean;
  isReportVisible: boolean;
  isSummaryVisible: boolean;
  isRuleValidationVisible: boolean;
  objectKey: string;
  evaluationReportUri?: string;
  summaryReportUri?: string;
  ruleValidationResultUri?: string;
}

interface DocumentViewersProps {
  objectKey: string;
  evaluationReportUri?: string;
  summaryReportUri?: string;
  ruleValidationResultUri?: string;
  evaluationStatus?: string;
}

const client = generateClient();
const logger = new ConsoleLogger('DocumentViewers');

const ViewerControls = ({
  onViewSource,
  onViewReport,
  onViewSummary,
  onViewRuleValidation,
  onSetAsBaseline,
  isSourceVisible,
  isReportVisible,
  isSummaryVisible,
  isRuleValidationVisible,
  evaluationReportUri,
  summaryReportUri,
  ruleValidationResultUri,
  copyStatus,
  evaluationStatus,
}: ViewerControlsProps): React.JSX.Element => (
  <SpaceBetween direction="horizontal" size="xs">
    <Button onClick={onViewSource} variant={isSourceVisible ? 'primary' : 'normal'}>
      {isSourceVisible ? 'Close Source Document' : 'View Source Document'}
    </Button>
    {evaluationReportUri && (
      <Button onClick={onViewReport} variant={isReportVisible ? 'primary' : 'normal'}>
        {isReportVisible ? 'Close Evaluation Report' : 'View Evaluation Report'}
      </Button>
    )}
    {summaryReportUri && (
      <Button onClick={onViewSummary} variant={isSummaryVisible ? 'primary' : 'normal'}>
        {isSummaryVisible ? 'Close Document Summary' : 'View Document Summary'}
      </Button>
    )}
    {ruleValidationResultUri && (
      <Button onClick={onViewRuleValidation} variant={isRuleValidationVisible ? 'primary' : 'normal'}>
        {isRuleValidationVisible ? 'Close Rule Validation Summary' : 'View Rule Validation Summary'}
      </Button>
    )}
    <Button onClick={onSetAsBaseline} disabled={copyStatus === 'in-progress' || evaluationStatus === 'BASELINE_COPYING'}>
      Use as Evaluation Baseline
    </Button>
    {copyStatus === 'show-message' && <StatusIndicator type="info">Copy started - see Evaluation status above</StatusIndicator>}
    {evaluationStatus === 'BASELINE_COPYING' && <StatusIndicator type="in-progress">Baseline copying in progress</StatusIndicator>}
    {evaluationStatus === 'BASELINE_AVAILABLE' && !copyStatus && <StatusIndicator type="success">Baseline available</StatusIndicator>}
    {evaluationStatus === 'BASELINE_ERROR' && <StatusIndicator type="error">Baseline copy failed</StatusIndicator>}
  </SpaceBetween>
);

const ViewerContent = ({
  isSourceVisible,
  isReportVisible,
  isSummaryVisible,
  isRuleValidationVisible,
  objectKey,
  evaluationReportUri,
  summaryReportUri,
  ruleValidationResultUri,
}: ViewerContentProps): React.JSX.Element | null => {
  if (!isSourceVisible && !isReportVisible && !isSummaryVisible && !isRuleValidationVisible) {
    return null;
  }

  return (
    <div className="flex flex-col lg:flex-row gap-4 mt-4">
      {isSourceVisible && (
        <div className="flex-1 min-w-0">
          <FileViewer objectKey={objectKey} {...({ showControls: false } as Record<string, unknown>)} />
        </div>
      )}
      {isReportVisible && (
        <div className="flex-1 min-w-0">
          {/* Reads the JSON results and renders a real component, falling back to
              the markdown artifact when they cannot be read. The markdown stays
              one click away from inside it. */}
          <EvaluationReport reportUri={evaluationReportUri ?? ''} documentId={objectKey} />
        </div>
      )}
      {isSummaryVisible && (
        <div className="flex-1 min-w-0">
          <MarkdownReport
            reportUri={summaryReportUri ?? ''}
            documentId={objectKey}
            title="Document Summary"
            emptyMessage="Summary report not available for this document"
          />
        </div>
      )}
      {isRuleValidationVisible && (
        <div className="flex-1 min-w-0">
          <MarkdownReport
            reportUri={ruleValidationResultUri ?? ''}
            documentId={objectKey}
            title="Rule Validation Summary"
            emptyMessage="Rule validation summary not available for this document"
          />
        </div>
      )}
    </div>
  );
};

const DocumentViewers = ({
  objectKey,
  evaluationReportUri,
  summaryReportUri,
  ruleValidationResultUri,
  evaluationStatus,
}: DocumentViewersProps): React.JSX.Element => {
  const [isSourceVisible, setIsSourceVisible] = useState(false);
  const [isReportVisible, setIsReportVisible] = useState(false);
  const [isSummaryVisible, setIsSummaryVisible] = useState(false);
  const [isRuleValidationVisible, setIsRuleValidationVisible] = useState(false);
  const [copyStatus, setCopyStatus] = useState<string | null>(null);

  // Show temporary message when copy is initiated, then clear it
  useEffect(() => {
    let messageTimer: ReturnType<typeof setTimeout>;

    if (copyStatus === 'show-message') {
      // Clear the message after 5 seconds
      messageTimer = setTimeout(() => {
        setCopyStatus(null);
      }, 5000);
    }

    // Cleanup timer
    return () => {
      if (messageTimer) {
        clearTimeout(messageTimer);
      }
    };
  }, [copyStatus]);

  const handleViewSource = () => {
    setIsSourceVisible(!isSourceVisible);
  };

  const handleViewReport = () => {
    setIsReportVisible(!isReportVisible);
  };

  const handleViewSummary = () => {
    setIsSummaryVisible(!isSummaryVisible);
  };

  const handleViewRuleValidation = () => {
    setIsRuleValidationVisible(!isRuleValidationVisible);
  };

  const handleSetAsBaseline = async () => {
    // Set in-progress status to disable button
    setCopyStatus('in-progress');

    try {
      const result = await client.graphql({
        query: copyToBaseline,
        variables: {
          objectKey,
        },
      });

      // The Lambda returns immediately, so check the result
      if ((result as { data: { copyToBaseline: { success: boolean; message: string } } }).data.copyToBaseline.success) {
        // Operation started successfully, show temporary message
        setCopyStatus('show-message');
        logger.info('Copy operation started:', (result as { data: { copyToBaseline: { message: string } } }).data.copyToBaseline.message);
      } else {
        // Immediate failure (e.g., file not found)
        setCopyStatus('error');
        logger.error(
          'Failed to start copy operation:',
          (result as { data: { copyToBaseline: { message: string } } }).data.copyToBaseline.message,
        );

        // Clear error status after 5 seconds
        setTimeout(() => setCopyStatus(null), 5000);
      }
    } catch (error) {
      setCopyStatus('error');
      logger.error('Error initiating copy to evaluation baseline:', error);

      // Clear error status after 5 seconds
      setTimeout(() => setCopyStatus(null), 5000);
    }
  };

  return (
    <Box>
      <SpaceBetween size="s">
        <ViewerControls
          onViewSource={handleViewSource}
          onViewReport={handleViewReport}
          onViewSummary={handleViewSummary}
          onViewRuleValidation={handleViewRuleValidation}
          onSetAsBaseline={handleSetAsBaseline}
          isSourceVisible={isSourceVisible}
          isReportVisible={isReportVisible}
          isSummaryVisible={isSummaryVisible}
          isRuleValidationVisible={isRuleValidationVisible}
          evaluationReportUri={evaluationReportUri}
          summaryReportUri={summaryReportUri}
          ruleValidationResultUri={ruleValidationResultUri}
          copyStatus={copyStatus}
          evaluationStatus={evaluationStatus}
        />
        <ViewerContent
          isSourceVisible={isSourceVisible}
          isReportVisible={isReportVisible}
          isSummaryVisible={isSummaryVisible}
          isRuleValidationVisible={isRuleValidationVisible}
          objectKey={objectKey}
          evaluationReportUri={evaluationReportUri}
          summaryReportUri={summaryReportUri}
          ruleValidationResultUri={ruleValidationResultUri}
        />
      </SpaceBetween>
    </Box>
  );
};

export default DocumentViewers;
