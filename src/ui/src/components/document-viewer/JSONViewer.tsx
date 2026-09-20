// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState, useMemo, Suspense, useEffect } from 'react';
import { Box, Button, Spinner } from '@cloudscape-design/components';
import { generateClient } from '../../api/client-shim';
import { ConsoleLogger } from 'aws-amplify/utils';
import { getFilePresignedUrl, uploadDocument } from '../../graphql/generated';
import { FILE_ACCESS_DENIED_MESSAGE, isAuthorizationError } from '../../hooks/utils/graphql-error';
import { useDocumentVersion } from '../../contexts/document-version';

// Lazy load VisualEditorModal for better performance
const VisualEditorModal = React.lazy(
  () => import('./VisualEditorModal') as unknown as Promise<{ default: React.ComponentType<Record<string, unknown>> }>,
);

interface JSONViewerProps {
  fileUri: string;
  fileType?: string;
  buttonText?: string;
  sectionData?: Record<string, unknown>;
  onOpen?: () => void;
  onClose?: () => void;
  disabled?: boolean;
  isReadOnly?: boolean;
  allSections?: Array<{ Id: string; Class: string; PageIds: number[]; OutputJSONUri?: string }>;
  currentSectionIndex?: number;
  onNavigateToSection?: (index: number) => void;
  isExternallyOpen?: boolean;
}

const client = generateClient();
const logger = new ConsoleLogger('JSONViewer');

const JSONViewer = ({
  fileUri,
  fileType: _fileType = 'text',
  buttonText = 'View/Edit Data',
  sectionData,
  onOpen,
  onClose,
  disabled,
  isReadOnly = false,
  // Section navigation props
  allSections = [],
  currentSectionIndex = 0,
  onNavigateToSection,
  // External control props for navigation
  isExternallyOpen = false,
}: JSONViewerProps): React.JSX.Element => {
  // When viewing a past document version, fetch that run's pinned bytes for
  // this URI and force read-only (editing a historical snapshot is disallowed).
  const { isHistorical, versionIdForUri } = useDocumentVersion();
  const effectiveReadOnly = isReadOnly || isHistorical;

  const [jsonData, setJsonData] = useState<Record<string, unknown> | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [showVisualEditor, setShowVisualEditor] = useState<boolean>(false);
  const [_originalContent, setOriginalContent] = useState<string | null>(null);

  // Handle external open request (for section navigation)
  useEffect(() => {
    if (isExternallyOpen && !showVisualEditor && !isLoading) {
      // Trigger the same flow as clicking the button
      handleViewEditData();
    } else if (!isExternallyOpen && showVisualEditor) {
      // External close request
      handleDismiss();
    }
  }, [isExternallyOpen]);

  // Memoize section data for the modal
  const memoizedSectionData = useMemo(
    () => ({
      ...sectionData,
      documentItem: sectionData?.documentItem || sectionData?.item,
    }),
    [sectionData],
  );

  // Fetch content and immediately open Visual Editor.
  //
  // The bytes are fetched directly from S3 via a short-lived presigned URL
  // rather than proxied through the resolver Lambda. Section `result.json`
  // files can exceed Lambda's 6 MB synchronous response cap (e.g. a large
  // single-section document), which previously surfaced as a generic
  // "Failed to load content" error. A presigned GET has no such size limit.
  const handleViewEditData = async () => {
    setIsLoading(true);
    setError(null);
    try {
      logger.info('Fetching content:', fileUri);

      const response = await client.graphql({
        query: getFilePresignedUrl,
        variables: { s3Uri: fileUri, versionId: versionIdForUri(fileUri) },
      });

      const result = response.data?.getFilePresignedUrl;

      if (!result?.presignedUrl) {
        setError('No response from server.');
        return;
      }

      // Fetch the file bytes straight from S3.
      const s3Response = await fetch(result.presignedUrl);
      if (!s3Response.ok) {
        throw new Error(`S3 fetch failed: ${s3Response.status} ${s3Response.statusText}`);
      }
      const fetchedContent = await s3Response.text();
      logger.debug('Received content');

      // Parse JSON content
      const parsed = JSON.parse(fetchedContent);
      setJsonData(parsed);
      setOriginalContent(fetchedContent);

      // Immediately open Visual Editor
      setShowVisualEditor(true);

      // Notify parent that viewer is now open
      if (onOpen) {
        onOpen();
      }
    } catch (err) {
      logger.error('Error fetching content:', err);
      setError(isAuthorizationError(err) ? FILE_ACCESS_DENIED_MESSAGE : `Failed to load content. Please try again.`);
    } finally {
      setIsLoading(false);
    }
  };

  // Handle JSON data changes from the Visual Editor
  const handleJsonChange = (jsonString: string) => {
    try {
      const parsed = JSON.parse(jsonString);
      setJsonData(parsed);
    } catch (err) {
      logger.error('Error parsing JSON:', err);
    }
  };

  // Handle saving changes
  const handleSave = async (editedJsonData: Record<string, unknown>) => {
    try {
      const editedContent = JSON.stringify(editedJsonData, null, 2);

      logger.info('Saving changes to file:', fileUri);

      // Parse the S3 URI to get the correct path
      const s3UriMatch = fileUri.match(/^s3:\/\/([^/]+)\/(.+)$/);
      if (!s3UriMatch) {
        throw new Error('Invalid S3 URI format');
      }

      const [, bucket, fullPath] = s3UriMatch;
      const fileName = fullPath.split('/').pop() ?? fullPath;
      const prefix = fullPath.substring(0, fullPath.lastIndexOf('/'));

      // Get presigned URL
      const response = await client.graphql({
        query: uploadDocument,
        variables: {
          fileName,
          contentType: 'application/json',
          prefix,
          bucket,
        },
      });

      const { presignedUrl, usePostMethod } = response.data.uploadDocument;
      const usePost = usePostMethod?.toLowerCase() === 'true';

      if (!usePost) {
        throw new Error('Server returned PUT method which is not supported');
      }

      // Parse the presigned post data
      const presignedPostData = JSON.parse(presignedUrl);

      // Create form data
      const formData = new FormData();

      // Add all fields from presigned POST data
      Object.entries(presignedPostData.fields as Record<string, string>).forEach(([key, value]) => {
        formData.append(key, value);
      });

      // Create a Blob from the JSON content and append it as a file
      const blob = new Blob([editedContent], { type: 'application/json' });
      formData.append('file', blob, fileName);

      // Upload to S3
      const uploadResponse = await fetch(presignedPostData.url, {
        method: 'POST',
        body: formData,
      });

      if (!uploadResponse.ok) {
        const errorText = await uploadResponse.text().catch(() => 'Could not read error response');
        throw new Error(`Upload failed: ${errorText}`);
      }

      // Update the original content state
      setOriginalContent(editedContent);
      logger.info('Successfully saved changes');

      return true;
    } catch (err) {
      logger.error('Error saving changes:', err);
      throw err;
    }
  };

  // Handle dismissing the Visual Editor
  const handleDismiss = () => {
    setShowVisualEditor(false);
    setJsonData(null);
    setOriginalContent(null);

    // Notify parent that viewer is now closed
    if (onClose) {
      onClose();
    }
  };

  if (!fileUri) {
    return (
      <Box color="text-status-inactive" padding={{ top: 's' }}>
        File content not available
      </Box>
    );
  }

  return (
    <Box>
      <Button onClick={handleViewEditData} loading={isLoading} disabled={isLoading || disabled}>
        {buttonText}
      </Button>

      {error && (
        <Box color="text-status-error" padding="s">
          {error}
        </Box>
      )}

      {/* Visual Editor Modal - Directly opens when content is loaded */}
      {showVisualEditor && jsonData && (
        <Suspense
          fallback={
            <Box padding="l" textAlign="center">
              <Spinner /> Loading Visual Editor...
            </Box>
          }
        >
          <VisualEditorModal
            visible={showVisualEditor}
            onDismiss={handleDismiss}
            jsonData={jsonData}
            onChange={handleJsonChange}
            onSave={handleSave}
            isReadOnly={effectiveReadOnly}
            sectionData={memoizedSectionData}
            allSections={allSections}
            currentSectionIndex={currentSectionIndex}
            onNavigateToSection={onNavigateToSection}
          />
        </Suspense>
      )}
    </Box>
  );
};

export default JSONViewer;
