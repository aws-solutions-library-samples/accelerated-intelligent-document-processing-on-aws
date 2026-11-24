// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState, useEffect, useRef } from 'react';
import { Box, Button, SpaceBetween } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, type NoMessageValues, type TypedMessageDescriptor } from 'react-intl';
import { generateClient } from '../../api/client-shim';
import { ConsoleLogger } from 'aws-amplify/utils';
import SafeMarkdown from '../common/SafeMarkdown';
import { getFileContents } from '../../graphql/generated';
import { fileAccessMessages, isAuthorizationError } from '../../hooks/utils/graphql-error';
import { useDocumentVersion } from '../../contexts/document-version';

import './MarkdownViewer.css';

interface MarkdownViewerProps {
  content: string;
  documentName?: string;
  title?: string;
  simple?: boolean;
  height?: string;
}

/**
 * Which report a MarkdownReport shows. It selects the report's loading and error sentences
 * and the title of its printed page.
 */
export type MarkdownReportKind = 'summary' | 'evaluation' | 'ruleValidation';

interface MarkdownReportProps {
  reportUri: string;
  documentId?: string;
  report: MarkdownReportKind;
  /** Shown instead of the report when `reportUri` is empty. */
  emptyMessage: string;
}

const client = generateClient();
const logger = new ConsoleLogger('MarkdownViewer');

// Default height for the simple mode
const MARKDOWN_DEFAULT_HEIGHT = '600px';

const messages = defineMessages<{
  noContent: NoMessageValues;
  unmatchedItems: NoMessageValues;
  expandDetails: NoMessageValues;
  download: NoMessageValues;
  print: NoMessageValues;
}>({
  noContent: {
    id: 'documents.markdownViewer.noContent',
    defaultMessage: 'No content to display',
    description: "Shown in the page text dialog's rendered markdown view when the page's text is empty.",
  },
  unmatchedItems: {
    id: 'documents.markdownViewer.unmatchedItems',
    defaultMessage: 'Unmatched Items',
    description:
      'Toggle button in the toolbar of an evaluation report shown as markdown. When it is on, the rows whose values matched are hidden, so only the unmatched items show.',
  },
  expandDetails: {
    id: 'documents.markdownViewer.expandDetails',
    defaultMessage: 'Expand Details',
    description:
      'Toggle button in the toolbar of an evaluation report shown as markdown. When it is on, every collapsible details block of the report is open; when it is off, they are closed.',
  },
  download: {
    id: 'documents.markdownViewer.download',
    defaultMessage: 'Download',
    description: 'Button in the toolbar of a report shown as markdown: downloads the report as a Markdown (.md) file.',
  },
  print: {
    id: 'documents.markdownViewer.print',
    defaultMessage: 'Print',
    description: 'Button in the toolbar of a report shown as markdown: opens the report in a new window and prints it.',
  },
});

/** What a report shows while it loads and when it cannot be shown: whole sentences, one set per report. */
const reportMessages = defineMessages<{
  summaryLoading: NoMessageValues;
  summaryBinary: NoMessageValues;
  summaryFailed: NoMessageValues;
  evaluationLoading: NoMessageValues;
  evaluationBinary: NoMessageValues;
  evaluationFailed: NoMessageValues;
  ruleValidationLoading: NoMessageValues;
  ruleValidationBinary: NoMessageValues;
  ruleValidationFailed: NoMessageValues;
}>({
  summaryLoading: {
    id: 'documents.markdownReport.summary.loading',
    defaultMessage: 'Loading document summary...',
    description: "Shown in the Document Summary dialog while the document's summary loads.",
  },
  summaryBinary: {
    id: 'documents.markdownReport.summary.binary',
    defaultMessage: 'This file contains binary content that cannot be viewed in the document summary.',
    description: "Shown in the Document Summary dialog when the document's summary file holds binary content instead of text.",
  },
  summaryFailed: {
    id: 'documents.markdownReport.summary.failed',
    defaultMessage: 'Failed to load document summary. Please try again.',
    description:
      "Shown in the Document Summary dialog when the document's summary could not be loaded. A missing permission has a message of its own.",
  },
  evaluationLoading: {
    id: 'documents.markdownReport.evaluation.loading',
    defaultMessage: 'Loading evaluation report...',
    description: 'Shown in the Evaluation Report dialog while the markdown version of the evaluation report loads.',
  },
  evaluationBinary: {
    id: 'documents.markdownReport.evaluation.binary',
    defaultMessage: 'This file contains binary content that cannot be viewed in the evaluation report.',
    description:
      'Shown in the Evaluation Report dialog when the markdown file of the evaluation report holds binary content instead of text.',
  },
  evaluationFailed: {
    id: 'documents.markdownReport.evaluation.failed',
    defaultMessage: 'Failed to load evaluation report. Please try again.',
    description:
      'Shown in the Evaluation Report dialog when the markdown version of the evaluation report could not be loaded. A missing permission has a message of its own.',
  },
  ruleValidationLoading: {
    id: 'documents.markdownReport.ruleValidation.loading',
    defaultMessage: 'Loading rule validation summary...',
    description: "Shown in the Rule Validation Summary dialog while the document's rule validation summary loads.",
  },
  ruleValidationBinary: {
    id: 'documents.markdownReport.ruleValidation.binary',
    defaultMessage: 'This file contains binary content that cannot be viewed in the rule validation summary.',
    description:
      "Shown in the Rule Validation Summary dialog when the document's rule validation summary file holds binary content instead of text.",
  },
  ruleValidationFailed: {
    id: 'documents.markdownReport.ruleValidation.failed',
    defaultMessage: 'Failed to load rule validation summary. Please try again.',
    description:
      "Shown in the Rule Validation Summary dialog when the document's rule validation summary could not be loaded. A missing permission has a message of its own.",
  },
});

/** The sentences of each report. */
const reportSentences: Record<MarkdownReportKind, Record<'loading' | 'binary' | 'failed', TypedMessageDescriptor<NoMessageValues>>> = {
  summary: { loading: reportMessages.summaryLoading, binary: reportMessages.summaryBinary, failed: reportMessages.summaryFailed },
  evaluation: {
    loading: reportMessages.evaluationLoading,
    binary: reportMessages.evaluationBinary,
    failed: reportMessages.evaluationFailed,
  },
  ruleValidation: {
    loading: reportMessages.ruleValidationLoading,
    binary: reportMessages.ruleValidationBinary,
    failed: reportMessages.ruleValidationFailed,
  },
};

/**
 * Each report's title on its printed page, also used to name the report in the log lines of its
 * fetch. English by design, like the rest of the print window, and counted by the string-debt
 * gate with it.
 */
const printTitles: Record<MarkdownReportKind, string> = {
  summary: 'Document Summary',
  evaluation: 'Evaluation Report',
  ruleValidation: 'Rule Validation Summary',
};

/** Text for an HTML context: the print window's <title> is built as a string. */
const escapeHtmlText = (value: string): string =>
  value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

const MarkdownViewer = ({
  content,
  documentName,
  title,
  simple = false,
  height = MARKDOWN_DEFAULT_HEIGHT,
}: MarkdownViewerProps): React.JSX.Element => {
  const contentRef = useRef<HTMLDivElement>(null);
  const [showOnlyUnmatched, setShowOnlyUnmatched] = useState(false);
  const [detailsExpanded, setDetailsExpanded] = useState(false);

  // Toggle showing only unmatched rows
  const toggleUnmatchedOnly = () => {
    if (!contentRef.current) return;

    const matchedRows = contentRef.current.querySelectorAll('tr.matched-row');
    const newState = !showOnlyUnmatched;

    matchedRows.forEach((row) => {
      (row as HTMLElement).style.display = newState ? 'none' : '';
    });

    setShowOnlyUnmatched(newState);
  };

  // Toggle expand/collapse all details elements
  const toggleExpandDetails = () => {
    if (!contentRef.current) return;

    const newState = !detailsExpanded;
    contentRef.current.querySelectorAll('details').forEach((d) => {
      d.open = newState;
    });

    setDetailsExpanded(newState);
  };

  // Handle anchor link clicks for smooth scrolling within the document
  const handleAnchorClick = (event: MouseEvent) => {
    const target = event.target as HTMLElement;
    if (target.tagName === 'A' && (target as HTMLAnchorElement).href && (target as HTMLAnchorElement).href.includes('#')) {
      const url = new URL((target as HTMLAnchorElement).href);
      // Check if this is an internal anchor link (same origin + hash)
      if (url.origin === window.location.origin && url.hash) {
        event.preventDefault();
        const targetId = url.hash.substring(1); // Remove the # symbol

        // Special handling for "Back to Top" links
        if (targetId === 'table-of-contents') {
          // Scroll to the top of the markdown content container
          if (contentRef.current) {
            contentRef.current.scrollTo({
              top: 0,
              behavior: 'smooth',
            });
          }
          return;
        }

        const targetElement = document.getElementById(targetId);
        if (targetElement) {
          // Scroll within the contentRef container, not the entire page
          if (contentRef.current) {
            const containerTop = contentRef.current.scrollTop;
            const containerRect = contentRef.current.getBoundingClientRect();
            const elementRect = targetElement.getBoundingClientRect();
            const relativeTop = elementRect.top - containerRect.top + containerTop;

            contentRef.current.scrollTo({
              top: relativeTop - 20, // 20px offset for padding
              behavior: 'smooth',
            });
          }
        }
      }
    }
  };

  // Add click event listener when component mounts
  useEffect(() => {
    const currentRef = contentRef.current;
    if (currentRef) {
      currentRef.addEventListener('click', handleAnchorClick);
      return () => {
        currentRef.removeEventListener('click', handleAnchorClick);
      };
    }
    return undefined;
  }, [content]);

  const handlePrint = () => {
    const printWindow = window.open('', '_blank') as Window | null;
    const printContent = `
      <!DOCTYPE html>
      <html>
        <head>
          <title>${escapeHtmlText(title || 'Report')} - ${escapeHtmlText(documentName || 'Document')}</title>
          <style>
            body { font-family: Arial, sans-serif; padding: 20px; }
            table { border-collapse: collapse; margin: 16px 0; width: auto; }
            th, td { border: 1px solid #ddd; padding: 8px 12px; }
            th { background-color: #f1f1f1; font-weight: bold; text-align: left; }
            tr:nth-child(even) { background-color: #f9f9f9; }
            h1 { font-size: 24px; margin-bottom: 16px; }
            h2 { font-size: 20px; margin-bottom: 12px; margin-top: 24px; }
            h3 { font-size: 18px; margin-bottom: 8px; margin-top: 16px; }
          </style>
        </head>
        <body>
          <div>${contentRef.current?.innerHTML || ''}</div>
        </body>
      </html>
    `;

    if (printWindow) {
      printWindow.document.open();
      printWindow.document.write(printContent);
      printWindow.document.close();
      printWindow.onload = () => {
        printWindow.print();
      };
    }
  };

  const handleDownload = () => {
    // Create a blob from the markdown content
    const blob = new Blob([content], { type: 'text/markdown' });
    const url = URL.createObjectURL(blob);

    // Create a temporary link element
    const a = document.createElement('a');
    a.href = url;
    a.download = `${documentName || title?.toLowerCase().replace(/\s+/g, '-') || 'report'}.md`;

    // Append, click, and remove
    document.body.appendChild(a);
    a.click();

    // Clean up
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  // For simple mode, just show the markdown content without the controls
  if (simple) {
    return (
      <Box
        {...({
          style: {
            height,
            position: 'relative',
            overflow: 'auto',
            padding: '16px',
            backgroundColor: '#ffffff',
            border: '2px solid #e9ebed',
            borderRadius: '4px',
            width: '100%',
            minWidth: '600px',
          },
        } as Record<string, unknown>)}
      >
        {content ? (
          <SafeMarkdown>{content}</SafeMarkdown>
        ) : (
          <Box textAlign="center" padding="l">
            <FormattedMessage {...messages.noContent} />
          </Box>
        )}
      </Box>
    );
  }

  // Check if this is an evaluation report with enhanced features
  const isEvalReport = content && (content.includes('eval-report-v2') || content.includes('matched-row'));

  // Standard mode with controls and improved styling
  return (
    <Box
      {...({
        className: 'markdown-viewer',
        style: {
          border: '1px solid #e9ebed',
          borderRadius: '8px',
          backgroundColor: '#ffffff',
          padding: '0',
        },
      } as Record<string, unknown>)}
    >
      {/* Sticky toolbar container */}
      <div
        style={{
          position: 'sticky',
          top: 0,
          zIndex: 100,
          backgroundColor: '#ffffff',
          borderBottom: '1px solid #e9ebed',
          padding: '16px',
        }}
      >
        <SpaceBetween direction="horizontal" size="xs">
          {isEvalReport && (
            <>
              <Button variant={showOnlyUnmatched ? 'primary' : 'normal'} onClick={toggleUnmatchedOnly} iconName="filter" iconAlign="left">
                <FormattedMessage {...messages.unmatchedItems} />
              </Button>
              <Button variant={detailsExpanded ? 'primary' : 'normal'} onClick={toggleExpandDetails} iconName="expand" iconAlign="left">
                <FormattedMessage {...messages.expandDetails} />
              </Button>
            </>
          )}
          <Button variant="normal" onClick={handleDownload} iconName="download" iconAlign="left" formAction="none">
            <FormattedMessage {...messages.download} />
          </Button>
          <Button variant="normal" onClick={handlePrint} iconAlign="left" formAction="none">
            <FormattedMessage {...messages.print} />
          </Button>
        </SpaceBetween>
      </div>

      {/* Content container with overflow */}
      <div
        ref={contentRef}
        style={{
          padding: '20px',
          overflowX: 'auto',
          overflowY: 'auto',
          maxHeight: 'calc(100vh - 400px)',
        }}
      >
        <SafeMarkdown>{content}</SafeMarkdown>
      </div>
    </Box>
  );
};

const MarkdownReport = ({ reportUri, documentId, report, emptyMessage }: MarkdownReportProps): React.JSX.Element | null => {
  const { versionIdForUri, runId } = useDocumentVersion();
  const [reportContent, setReportContent] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  // Why the report cannot be shown, worded where it shows.
  const [error, setError] = useState<'binary' | 'denied' | 'failed' | null>(null);

  useEffect(() => {
    const title = printTitles[report];
    const fetchReport = async () => {
      if (!reportUri) return;

      setIsLoading(true);
      setError(null);
      try {
        logger.info(`Fetching ${title}:`, reportUri);

        const response = await client.graphql({
          query: getFileContents,
          variables: { s3Uri: reportUri, versionId: versionIdForUri(reportUri) },
        });

        // Get content from the updated response structure
        const result = (response as { data: { getFileContents: { content: string; contentType: string; isBinary: boolean } } }).data
          .getFileContents;
        const { content } = result;
        logger.debug(`Received ${title} content type:`, result.contentType);
        logger.debug(`Binary content?`, result.isBinary);
        if (result.isBinary === true) {
          setError('binary');
          setIsLoading(false);
          return;
        }
        logger.debug(`Received ${title} content:`, `${content.substring(0, 100)}...`);

        setReportContent(content);
      } catch (err) {
        logger.error(`Error fetching ${title}:`, err);
        // Retrying cannot clear a 403, and the file reads now require an assigned
        // Cognito group, so an authorization failure gets its own message.
        setError(isAuthorizationError(err) ? 'denied' : 'failed');
      } finally {
        setIsLoading(false);
      }
    };

    fetchReport();
  }, [reportUri, report, runId]);

  if (!reportUri) {
    return (
      <Box color="text-status-inactive" padding={{ top: 's' }}>
        {emptyMessage}
      </Box>
    );
  }

  const sentences = reportSentences[report];

  if (error) {
    return (
      <Box color="text-status-error" padding="s">
        <FormattedMessage {...(error === 'denied' ? fileAccessMessages.denied : sentences[error])} />
      </Box>
    );
  }

  if (isLoading) {
    return (
      <Box textAlign="center" padding="s">
        <FormattedMessage {...sentences.loading} />
      </Box>
    );
  }

  return reportContent ? (
    <MarkdownViewer content={reportContent} documentName={documentId || 'document'} title={printTitles[report]} />
  ) : null;
};

export { MarkdownReport };
export default MarkdownViewer;
