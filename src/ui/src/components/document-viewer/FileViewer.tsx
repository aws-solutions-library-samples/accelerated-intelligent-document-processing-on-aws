// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState } from 'react';
import { Box } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues, type TypedMessageDescriptor } from 'react-intl';
import { generateClient } from '../../api/client-shim';
import { ConsoleLogger } from 'aws-amplify/utils';
import DOMPurify from 'dompurify';
// Note: XLSX and mammoth imports removed since we're using download approach for Excel/Docx files
import useSettingsContext from '../../contexts/settings';
import { getFileContents, getFilePresignedUrl } from '../../graphql/generated';
import { fileAccessMessages, isAuthorizationError } from '../../hooks/utils/graphql-error';
import { useDocumentVersion } from '../../contexts/document-version';

interface FileViewerProps {
  objectKey: string;
  // Bucket holding the object; defaults to the stack's InputBucket.
  bucket?: string;
}

const client = generateClient();
const logger = new ConsoleLogger('FileViewer');

const messages = defineMessages<{
  loadFailed: NoMessageValues;
  loading: NoMessageValues;
  cannotDisplay: NoMessageValues;
  frameTitle: NoMessageValues;
  pdfFrameTitle: NoMessageValues;
  openPdf: NoMessageValues;
  previewAlt: NoMessageValues;
  imageTitle: NoMessageValues;
  imageCannotDisplay: NoMessageValues;
  imageDownload: NoMessageValues;
  officeTitle: { readonly app: string };
  officeCannotDisplay: { readonly app: string };
  officeDownload: { readonly app: string };
  officeOpensIn: { readonly app: string };
}>({
  loadFailed: {
    id: 'documents.fileViewer.loadFailed',
    defaultMessage: 'Failed to load document. Please try again.',
    description: 'Shown in the source view when the document could not be loaded.',
  },
  loading: {
    id: 'documents.fileViewer.loading',
    defaultMessage: 'Loading document...',
    description: 'Shown while the source view loads the document.',
  },
  cannotDisplay: {
    id: 'documents.fileViewer.cannotDisplay',
    defaultMessage: 'Unable to display document.',
    description: 'Shown in the source view when the document cannot be shown.',
  },
  frameTitle: {
    id: 'documents.fileViewer.frameTitle',
    defaultMessage: 'Document Viewer',
    description: 'Accessible title of the frame that shows the document in the source view.',
  },
  pdfFrameTitle: {
    id: 'documents.fileViewer.pdfFrameTitle',
    defaultMessage: 'PDF Viewer',
    description: 'Accessible title of the frame that shows a PDF document in the source view.',
  },
  openPdf: {
    id: 'documents.fileViewer.openPdf',
    defaultMessage: 'Open PDF in a new tab',
    description: 'Link under a PDF document, for browsers that do not show PDFs themselves.',
  },
  previewAlt: {
    id: 'documents.fileViewer.previewAlt',
    defaultMessage: 'Document preview',
    description: 'Alternative text of an image document in the source view.',
  },
  imageTitle: {
    id: 'documents.fileViewer.image.title',
    defaultMessage: 'Image Document',
    description: 'Heading shown when an image document cannot be displayed; an icon comes before it.',
  },
  imageCannotDisplay: {
    id: 'documents.fileViewer.image.cannotDisplay',
    defaultMessage: 'Unable to display this image.',
    description: 'Shown when an image document cannot be displayed.',
  },
  imageDownload: {
    id: 'documents.fileViewer.image.download',
    defaultMessage: 'Download Image',
    description: 'Link that downloads an image document that cannot be displayed; an icon comes before it.',
  },
  officeTitle: {
    id: 'documents.fileViewer.office.title',
    defaultMessage: '{app} Document',
    description: 'Heading for an Office document, which the browser cannot display. {app} is Excel or Word.',
  },
  officeCannotDisplay: {
    id: 'documents.fileViewer.office.cannotDisplay',
    defaultMessage: 'This {app} document cannot be displayed directly in the browser.',
    description: 'Says why an Office document is offered as a download. {app} is Excel or Word.',
  },
  officeDownload: {
    id: 'documents.fileViewer.office.download',
    defaultMessage: 'Download {app} File',
    description: 'Link that downloads an Office document; an icon comes before it. {app} is Excel or Word.',
  },
  officeOpensIn: {
    id: 'documents.fileViewer.office.opensIn',
    defaultMessage: 'The file will open in your default {app} application.',
    description: 'Note under the download link of an Office document. {app} is Excel or Word.',
  },
});

/** The Office application each downloadable type opens in: product names, the same in every language. */
const FILE_TYPE_NAMES = { excel: 'Excel', docx: 'Word' } as const;

/** A `data:` URL holding `text` as `contentType`, UTF-8 encoded. */
const TEXT_DATA_URL = (contentType: string, text: string): string => `data:${contentType};charset=utf-8,${encodeURIComponent(text)}`;

// Helper function to create a safe data URL for HTML content
const createSafeDataUrl = (content: string, contentType: string): string => {
  // For HTML content, sanitize with DOMPurify
  if (contentType.includes('html')) {
    const sanitizedContent = DOMPurify.sanitize(content, {
      FORBID_TAGS: ['script', 'iframe', 'object', 'embed'],
      FORBID_ATTR: ['onerror', 'onload', 'onclick', 'onmouseover', 'onmouseout'],
    });

    return TEXT_DATA_URL(contentType, sanitizedContent);
  }

  // For CSV files, treat as plain text for better browser compatibility
  if (contentType.includes('csv')) {
    return `data:text/plain;charset=utf-8,${encodeURIComponent(content)}`;
  }

  // For other text-based content, use as is
  return TEXT_DATA_URL(contentType, content);
};

// Helper function to detect file type from object key or content type
const detectFileType = (objectKey: string, contentType: string | null): string => {
  // First check content type if available
  if (contentType) {
    if (contentType.includes('pdf')) return 'pdf';
    if (contentType.includes('image/')) return 'image';
    if (contentType.includes('html')) return 'html';
    if (contentType.includes('text/')) return 'text';
    if (contentType.includes('json')) return 'json';
    if (contentType.includes('spreadsheet') || contentType.includes('excel')) return 'excel';
    if (contentType.includes('wordprocessingml') || contentType.includes('msword')) return 'docx';
  }
  // Fallback to checking file extension
  if (objectKey) {
    const extension = objectKey.split('.').pop()?.toLowerCase() ?? '';
    if (extension === 'pdf') return 'pdf';
    if (['jpg', 'jpeg', 'png', 'gif', 'svg', 'webp'].includes(extension)) return 'image';
    if (extension === 'html' || extension === 'htm') return 'html';
    if (['txt', 'md', 'csv', 'log'].includes(extension)) return 'text';
    if (extension === 'json') return 'json';
    if (['xlsx', 'xls', 'xlsm', 'xlsb'].includes(extension)) return 'excel';
    if (['docx', 'doc'].includes(extension)) return 'docx';
  }
  return 'unknown';
};

/**
 * Binary content is always presigned by the `getFilePresignedUrl` resolver.
 *
 * There is no client-signing option any more. The Cognito Identity Pool's
 * authenticated role grants `s3:GetObject` on **no** bucket (issue #1033), so a
 * URL signed with the browser's own credentials 403s at fetch time for every
 * bucket this viewer is pointed at — including the Input and Output buckets,
 * which it used to reach. The resolver holds the read access behind its own
 * bucket allow-list and applies the caller's group floor and per-user key scope
 * (allowedConfigVersions, allowedTestSets), which one shared IAM role cannot
 * express.
 */
const FileViewer = ({ objectKey, bucket }: FileViewerProps): React.JSX.Element => {
  // Fetch pinned bytes when viewing a past document version.
  const { versionIdForUri } = useDocumentVersion();
  const intl = useIntl();
  const [presignedUrl, setPresignedUrl] = useState<string | null>(null);
  const [fileContent, setFileContent] = useState<string | null>(null);
  const [contentType, setContentType] = useState<string | null>(null);
  const { settings } = useSettingsContext();
  const [isLoading, setIsLoading] = useState<boolean>(false);
  // A message, formatted at render, so it follows a language switch.
  const [error, setError] = useState<TypedMessageDescriptor<NoMessageValues> | null>(null);
  const [viewMethod, setViewMethod] = useState<'presigned' | 'content'>('presigned');
  const [imageError, setImageError] = useState(false);
  const [imageErrorUrl, setImageErrorUrl] = useState('');

  // Fetch file contents via GraphQL API and process special file types
  const fetchFileContents = async (s3Url: string) => {
    try {
      logger.info('Fetching file contents via GraphQL for:', s3Url);
      const response = await client.graphql({
        query: getFileContents,
        variables: { s3Uri: s3Url, versionId: versionIdForUri(s3Url) },
      });

      const result = (response as { data: { getFileContents: { contentType: string; isBinary: boolean; content: string } } }).data
        .getFileContents;
      logger.info('Content type received:', result.contentType);
      logger.info('Binary content?', result.isBinary);
      logger.info('Content length:', result.content ? result.content.length : 'null');
      logger.info('Content type:', typeof result.content);
      logger.info('Content preview:', result.content ? result.content.substring(0, 100) : 'null');

      // Get file type
      const fileType = detectFileType(objectKey, result.contentType);
      logger.info('Detected file type:', fileType);

      // For Excel and Docx files, we always use presigned URL with download approach
      if (fileType === 'excel' || fileType === 'docx') {
        logger.info(`${fileType} file detected, using presigned URL with download approach`);
      }

      // Set content and type for other files
      setFileContent(result.content);
      setContentType(result.contentType);

      // Determine view method based on content type and binary flag
      let selectedViewMethod: 'content' | 'presigned';
      if (
        fileType === 'pdf' ||
        fileType === 'excel' ||
        fileType === 'docx' ||
        (result.isBinary === true && fileType !== 'html' && fileType !== 'text')
      ) {
        // Use presigned URL for PDFs, Excel, Docx, and other binary content
        selectedViewMethod = 'presigned';
      } else if (fileType === 'html' || fileType === 'text' || result.isBinary === false) {
        // Use content-based viewing for HTML, text content, and non-binary files (like CSV)
        selectedViewMethod = 'content';
      } else {
        // Use presigned URL for other content
        selectedViewMethod = 'presigned';
      }

      logger.info('Selected view method:', selectedViewMethod);
      setViewMethod(selectedViewMethod);

      return result;
    } catch (err) {
      logger.error('Error fetching file contents:', err);
      throw err;
    }
  };

  // Generate presigned URL for direct viewing
  const generateUrl = async () => {
    setIsLoading(true);
    setError(null);
    try {
      const sourceBucket = bucket || ((settings as Record<string, unknown>).InputBucket as string | undefined);
      if (!sourceBucket) {
        throw new Error('Input bucket not configured');
      }

      // Use s3:// URI scheme (matches what every other file viewer in the app does).
      // Avoids dependence on the VITE_AWS_REGION build-time env var (which, if missing,
      // produces an invalid URL like https://<bucket>.s3.undefined.amazonaws.com/<key>),
      // and sidesteps S3 HTTPS URL encoding issues for keys with special characters.
      const s3Url = `s3://${sourceBucket}/${objectKey}`;

      // First fetch the content via GraphQL to determine content type
      const result = await fetchFileContents(s3Url);

      // Get file type to determine if we need presigned URL
      const fileType = detectFileType(objectKey, result.contentType);

      // Determine if we need presigned URL based on the same logic as in fetchFileContents
      const needsPresignedUrl =
        fileType === 'pdf' ||
        fileType === 'excel' ||
        fileType === 'docx' ||
        (result.isBinary === true && fileType !== 'html' && fileType !== 'text');

      // Generate presigned URL only if needed
      if (needsPresignedUrl) {
        logger.info('Generating presigned URL (server) for:', s3Url);
        const response = await client.graphql({
          query: getFilePresignedUrl,
          variables: { s3Uri: s3Url, versionId: versionIdForUri(s3Url) },
        });
        const url = response.data?.getFilePresignedUrl?.presignedUrl;
        if (!url) {
          throw new Error('No presigned URL returned by server');
        }
        setPresignedUrl(url);
      } else {
        logger.info('Using content-based viewing, no presigned URL needed');
      }
    } catch (err) {
      logger.error('Error preparing document for viewing:', err);
      // "Please try again" is advice that cannot work for a 403, and the file reads
      // now require an assigned Cognito group — so branch on the error.
      setError(isAuthorizationError(err) ? fileAccessMessages.denied : messages.loadFailed);
    } finally {
      setIsLoading(false);
    }
  };

  // Reset image error state when presigned URL changes
  React.useEffect(() => {
    if (presignedUrl) {
      setImageError(false);
      setImageErrorUrl('');
    }
  }, [presignedUrl]);

  React.useEffect(() => {
    generateUrl();
  }, [objectKey, bucket]);

  if (error) {
    return (
      <Box color="text-status-error" padding="s">
        <FormattedMessage {...error} />
      </Box>
    );
  }

  // Render loading state
  if (isLoading) {
    return (
      <Box textAlign="center" padding="s">
        <FormattedMessage {...messages.loading} />
      </Box>
    );
  }

  // Render content-based viewer (for HTML and text files)
  if (viewMethod === 'content' && fileContent && contentType) {
    logger.info('Rendering content-based viewer');
    logger.info('File content length:', fileContent.length);
    logger.info('Content type for data URL:', contentType);

    const safeDataUrl = createSafeDataUrl(fileContent, contentType);
    logger.info('Generated data URL length:', safeDataUrl.length);
    logger.info('Data URL preview:', safeDataUrl.substring(0, 200));

    return (
      <Box className="document-container" padding={{ top: 's' }}>
        <iframe
          src={safeDataUrl}
          title={intl.formatMessage(messages.frameTitle)}
          width="100%"
          height="800px"
          className="h-full w-full"
          sandbox="allow-same-origin allow-popups allow-forms"
          referrerPolicy="no-referrer"
        />
      </Box>
    );
  }

  // Render presigned URL viewer (for PDFs, images, etc.)
  if (viewMethod === 'presigned' && presignedUrl) {
    // Get the file type
    const fileType = detectFileType(objectKey, contentType);
    const isPdf = fileType === 'pdf';
    logger.info('Rendering presigned URL viewer for file type:', fileType);
    logger.info('Presigned URL:', presignedUrl);

    if (isPdf) {
      // Render PDFs in an iframe rather than <object> to comply with the hardened
      // CloudFront Content-Security-Policy (`object-src 'none'`, introduced in v0.5.9).
      // Modern browsers render PDFs natively inside iframes using their built-in
      // viewer (Chrome's PDFium, Firefox's pdf.js, Safari's WebKit PDF viewer).
      //
      // Intentionally no `sandbox` attribute on this iframe: Chrome's built-in PDF
      // viewer relies on its own internal scripts and is blocked by Chromium when
      // loaded inside a sandboxed iframe ("This page has been blocked by Chrome").
      // The content is an S3 presigned URL to a file in a bucket owned by this stack
      // and served as `Content-Type: application/pdf` with `Content-Disposition: inline`,
      // so there is no HTML/JS execution risk from the iframe source itself.
      //
      // A visible "Open PDF in a new tab" link is included as a fallback for browsers
      // that have their built-in PDF viewer disabled.
      return (
        <Box className="document-container" padding={{ top: 's' }}>
          <iframe
            src={presignedUrl}
            title={intl.formatMessage(messages.pdfFrameTitle)}
            width="100%"
            height="800px"
            className="h-full w-full"
            referrerPolicy="no-referrer"
          />
          <Box padding={{ top: 'xs' }}>
            <a href={presignedUrl} target="_blank" rel="noopener noreferrer">
              <FormattedMessage {...messages.openPdf} />
            </a>
          </Box>
        </Box>
      );
    }
    // For image files, display them inline using img tag
    if (fileType === 'image') {
      const handleImageError = () => {
        setImageError(true);
        setImageErrorUrl(presignedUrl);
      };

      return (
        <Box className="document-container" padding={{ top: 's' }}>
          <Box textAlign="center">
            {!imageError ? (
              <img
                src={presignedUrl}
                alt={intl.formatMessage(messages.previewAlt)}
                style={{
                  maxWidth: '100%',
                  maxHeight: '800px',
                  height: 'auto',
                  objectFit: 'contain',
                }}
                onError={handleImageError}
              />
            ) : (
              <Box padding="xl">
                <h3>
                  🖼️ <FormattedMessage {...messages.imageTitle} />
                </h3>
                <p>
                  <FormattedMessage {...messages.imageCannotDisplay} />
                </p>
                <p>
                  <a
                    href={imageErrorUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    style={{
                      display: 'inline-block',
                      padding: '12px 24px',
                      backgroundColor: '#0073bb',
                      color: 'white',
                      textDecoration: 'none',
                      borderRadius: '4px',
                      fontWeight: 'bold',
                      margin: '10px',
                    }}
                  >
                    📥 <FormattedMessage {...messages.imageDownload} />
                  </a>
                </p>
              </Box>
            )}
          </Box>
        </Box>
      );
    }

    // For Excel and Docx files, provide download link since browsers can't display them inline
    if (fileType === 'excel' || fileType === 'docx') {
      const fileTypeName = FILE_TYPE_NAMES[fileType];
      return (
        <Box className="document-container" padding={{ top: 's' }} textAlign="center">
          <Box padding="xl">
            <h3>
              📄 <FormattedMessage {...messages.officeTitle} values={{ app: fileTypeName }} />
            </h3>
            <p>
              <FormattedMessage {...messages.officeCannotDisplay} values={{ app: fileTypeName }} />
            </p>
            <p>
              <a
                href={presignedUrl}
                target="_blank"
                rel="noopener noreferrer"
                style={{
                  display: 'inline-block',
                  padding: '12px 24px',
                  backgroundColor: '#0073bb',
                  color: 'white',
                  textDecoration: 'none',
                  borderRadius: '4px',
                  fontWeight: 'bold',
                  margin: '10px',
                }}
              >
                📥 <FormattedMessage {...messages.officeDownload} values={{ app: fileTypeName }} />
              </a>
            </p>
            <p style={{ fontSize: '14px', color: '#666' }}>
              <FormattedMessage {...messages.officeOpensIn} values={{ app: fileTypeName }} />
            </p>
          </Box>
        </Box>
      );
    }

    // For other non-PDF content, use iframe with appropriate sandbox settings
    return (
      <Box className="document-container" padding={{ top: 's' }}>
        <iframe
          src={presignedUrl}
          title={intl.formatMessage(messages.frameTitle)}
          width="100%"
          height="800px"
          className="h-full w-full"
          sandbox="allow-same-origin allow-popups allow-forms"
          referrerPolicy="no-referrer"
        />
      </Box>
    );
  }

  // Default fallback
  return (
    <Box textAlign="center" padding="s">
      <FormattedMessage {...messages.cannotDisplay} />
    </Box>
  );
};

export default FileViewer;
