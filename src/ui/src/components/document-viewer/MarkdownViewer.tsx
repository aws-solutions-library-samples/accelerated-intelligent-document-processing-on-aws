// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState, useEffect, useRef } from 'react';
import { Box, Button, SpaceBetween } from '@cloudscape-design/components';
import { generateClient } from '../../api/client-shim';
import { ConsoleLogger } from 'aws-amplify/utils';
import SafeMarkdown from '../common/SafeMarkdown';
import { getFileContents } from '../../graphql/generated';
import { FILE_ACCESS_DENIED_MESSAGE, isAuthorizationError } from '../../hooks/utils/graphql-error';
import { useDocumentVersion } from '../../contexts/document-version';

import './MarkdownViewer.css';

interface MarkdownViewerProps {
  content: string;
  documentName?: string;
  title?: string;
  simple?: boolean;
  height?: string;
}

interface MarkdownReportProps {
  reportUri: string;
  documentId?: string;
  title?: string;
  emptyMessage?: string;
}

const client = generateClient();
const logger = new ConsoleLogger('MarkdownViewer');

// Default height for the simple mode
const MARKDOWN_DEFAULT_HEIGHT = '600px';

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
          <title>${title || 'Report'} - ${documentName || 'Document'}</title>
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
            No content to display
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
                Unmatched Items
              </Button>
              <Button variant={detailsExpanded ? 'primary' : 'normal'} onClick={toggleExpandDetails} iconName="expand" iconAlign="left">
                Expand Details
              </Button>
            </>
          )}
          <Button variant="normal" onClick={handleDownload} iconName="download" iconAlign="left" formAction="none">
            Download
          </Button>
          <Button variant="normal" onClick={handlePrint} iconAlign="left" formAction="none">
            Print
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

const MarkdownReport = ({ reportUri, documentId, title = 'Report', emptyMessage }: MarkdownReportProps): React.JSX.Element | null => {
  const { versionIdForUri, runId } = useDocumentVersion();
  const [reportContent, setReportContent] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
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
          setError(`This file contains binary content that cannot be viewed in the ${title.toLowerCase()}.`);
          setIsLoading(false);
          return;
        }
        logger.debug(`Received ${title} content:`, `${content.substring(0, 100)}...`);

        setReportContent(content);
      } catch (err) {
        logger.error(`Error fetching ${title}:`, err);
        // Retrying cannot clear a 403, and the file reads now require an assigned
        // Cognito group, so an authorization failure gets its own message.
        setError(isAuthorizationError(err) ? FILE_ACCESS_DENIED_MESSAGE : `Failed to load ${title.toLowerCase()}. Please try again.`);
      } finally {
        setIsLoading(false);
      }
    };

    fetchReport();
  }, [reportUri, title, runId]);

  if (!reportUri) {
    return (
      <Box color="text-status-inactive" padding={{ top: 's' }}>
        {emptyMessage || `${title} not available for this document`}
      </Box>
    );
  }

  if (error) {
    return (
      <Box color="text-status-error" padding="s">
        {error}
      </Box>
    );
  }

  if (isLoading) {
    return (
      <Box textAlign="center" padding="s">
        Loading {title.toLowerCase()}...
      </Box>
    );
  }

  return reportContent ? <MarkdownViewer content={reportContent} documentName={documentId || 'document'} title={title} /> : null;
};

export { MarkdownReport };
export default MarkdownViewer;
