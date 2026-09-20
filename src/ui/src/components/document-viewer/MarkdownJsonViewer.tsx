// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState, useEffect } from 'react';
import type { SegmentedControlProps, ToggleProps } from '@cloudscape-design/components';
import { Box, SpaceBetween, Button, Toggle, Alert, SegmentedControl } from '@cloudscape-design/components';
import { generateClient } from '../../api/client-shim';
import { ConsoleLogger } from 'aws-amplify/utils';
import { Editor } from '@monaco-editor/react';
import type * as Monaco from 'monaco-editor';
import { getFileContents, uploadDocument } from '../../graphql/generated';
import { FILE_ACCESS_DENIED_MESSAGE, isAuthorizationError } from '../../hooks/utils/graphql-error';
import MarkdownViewer from './MarkdownViewer';

interface TextEditorViewProps {
  fileContent: string | null;
  onChange?: (value: string | undefined) => void;
  isReadOnly: boolean;
  fileType: string;
}

interface FileEditorViewProps {
  fileContent: string | null;
  onChange?: (value: string) => void;
  isReadOnly?: boolean;
  fileType?: string;
  viewMode: string;
  onViewModeChange: (event: { detail: SegmentedControlProps.ChangeDetail }) => void;
  isConfidenceAvailable: boolean;
}

interface MarkdownJsonViewerProps {
  fileUri: string;
  textConfidenceUri?: string;
  fileType?: string;
  buttonText?: string;
}

const client = generateClient();
const logger = new ConsoleLogger('MarkdownJsonViewer');

const EDITOR_DEFAULT_HEIGHT = '600px';

declare global {
  interface Window {
    monacoEditor: Monaco.editor.IStandaloneCodeEditor | null;
    editorResizeTimeout: ReturnType<typeof setTimeout> | null;
  }
}

const TextEditorView = ({ fileContent, onChange, isReadOnly, fileType }: TextEditorViewProps): React.JSX.Element => {
  const [isEditorReady, setIsEditorReady] = useState<boolean>(false);

  useEffect(() => {
    let timeoutId: ReturnType<typeof setTimeout>;
    if (isEditorReady) {
      timeoutId = setTimeout(() => {
        if (window.monacoEditor) {
          window.monacoEditor.layout();
        }
      }, 100);
    }
    return () => {
      if (timeoutId) clearTimeout(timeoutId);
      if (window.editorResizeTimeout) {
        clearTimeout(window.editorResizeTimeout);
      }
      // Clear any global references when component unmounts
      window.monacoEditor = null;
    };
  }, [isEditorReady]);

  const handleEditorDidMount = (editor: Monaco.editor.IStandaloneCodeEditor) => {
    window.monacoEditor = editor;
    editor.layout();
    setIsEditorReady(true);

    // Add a resize observer to handle editor resizing
    const resizeObserver = new ResizeObserver(() => {
      // Debounce the layout call to prevent excessive updates
      if (window.editorResizeTimeout) {
        clearTimeout(window.editorResizeTimeout);
      }
      window.editorResizeTimeout = setTimeout(() => {
        editor.layout();
      }, 100);
    });

    // Observe the editor's container
    const container = editor.getContainerDomNode();
    if (container?.parentElement) {
      resizeObserver.observe(container.parentElement);
    }
  };

  return (
    <Box
      {...({ className: 'file-editor-container', style: { border: '2px solid #e9ebed', width: '100%', minWidth: '600px' } } as Record<
        string,
        unknown
      >)}
    >
      <div style={{ height: EDITOR_DEFAULT_HEIGHT, position: 'relative', overflow: 'hidden', width: '100%' }}>
        <Editor
          height="100%"
          defaultLanguage={fileType === 'json' ? 'json' : fileType}
          value={
            fileType === 'json' && typeof fileContent === 'string'
              ? JSON.stringify(JSON.parse(fileContent), null, 2)
              : (fileContent ?? undefined)
          }
          onChange={onChange}
          onMount={handleEditorDidMount}
          options={{
            readOnly: isReadOnly,
            minimap: { enabled: false },
            fontSize: 14,
            wordWrap: 'on',
            wrappingIndent: 'indent',
            automaticLayout: false,
            scrollBeyondLastLine: false,
            fixedOverflowWidgets: true,
            scrollbar: {
              vertical: 'visible',
              horizontal: 'visible',
            },
            formatOnPaste: true,
            formatOnType: true,
          }}
          theme="vs-light"
          loading={<Box padding="s">Loading editor...</Box>}
        />
      </div>
    </Box>
  );
};

const FileEditorView = ({
  fileContent,
  onChange,
  isReadOnly = true,
  fileType = 'text',
  viewMode,
  onViewModeChange,
  isConfidenceAvailable,
}: FileEditorViewProps): React.JSX.Element => {
  const [isValid, setIsValid] = useState<boolean>(true);
  const [jsonData, setJsonData] = useState<Record<string, unknown> | null>(null);

  useEffect(() => {
    if (fileType === 'json') {
      try {
        const parsed = typeof fileContent === 'string' ? JSON.parse(fileContent) : fileContent;
        setJsonData(parsed);
        setIsValid(true);
      } catch (error) {
        setIsValid(false);
        logger.error('Invalid JSON:', error);
      }
    }
  }, [fileContent, fileType]);

  const handleTextEditorChange = (value: string | undefined) => {
    if (fileType === 'json') {
      try {
        const parsed = JSON.parse(value ?? '');
        setJsonData(parsed);
        setIsValid(true);
        if (onChange && value !== undefined) {
          onChange(value);
        }
      } catch (error) {
        setIsValid(false);
        logger.error('Invalid JSON:', error);
      }
    } else if (onChange && value !== undefined) {
      onChange(value);
    }
  };

  return (
    <Box>
      <SpaceBetween direction="vertical" size="xs">
        <SegmentedControl
          selectedId={viewMode}
          onChange={onViewModeChange}
          options={[
            { id: 'markdown', text: 'Markdown View' },
            { id: 'confidence', text: 'Text Confidence View' },
            { id: 'text', text: 'Text View' },
          ]}
        />

        {!isValid && (
          <Alert type="error" header="Invalid format">
            The content format is invalid. Please correct any syntax errors.
          </Alert>
        )}
      </SpaceBetween>

      {viewMode === 'confidence' && !isConfidenceAvailable ? (
        <Box color="text-status-inactive" padding="l" textAlign="center">
          Text confidence content is not available for this page.
        </Box>
      ) : viewMode === 'markdown' || viewMode === 'confidence' ? (
        <MarkdownViewer
          simple
          content={
            typeof fileContent === 'string'
              ? (() => {
                  try {
                    // Try to parse it as JSON and extract text content
                    const parsed = JSON.parse(fileContent);
                    return parsed.text || parsed.Text || fileContent;
                  } catch (e) {
                    // If it's not valid JSON, just return the content as is
                    return fileContent;
                  }
                })()
              : jsonData?.text || jsonData?.Text || JSON.stringify(jsonData, null, 2)
          }
        />
      ) : (
        <TextEditorView
          fileContent={typeof fileContent === 'string' ? fileContent : JSON.stringify(jsonData, null, 2)}
          onChange={handleTextEditorChange}
          isReadOnly={isReadOnly}
          fileType="json"
        />
      )}
    </Box>
  );
};

const MarkdownJsonViewer = ({
  fileUri,
  textConfidenceUri,
  fileType = 'text',
  buttonText = 'View File',
}: MarkdownJsonViewerProps): React.JSX.Element => {
  const [fileContent, setFileContent] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [isEditing, setIsEditing] = useState<boolean>(false);
  const [editedContent, setEditedContent] = useState<string | null>(null);
  const [viewMode, setViewMode] = useState<string>('markdown');
  const [loadedUri, setLoadedUri] = useState<string | null>(null);
  const [isViewerOpen, setIsViewerOpen] = useState<boolean>(false);

  const fetchContent = async (forceRefetch = false) => {
    // Determine which URI to fetch based on view mode
    const uriToFetch = viewMode === 'confidence' ? textConfidenceUri : fileUri;

    // If confidence view is selected but no URI available, don't fetch
    if (viewMode === 'confidence' && !textConfidenceUri) {
      setFileContent(null);
      setLoadedUri(null);
      return;
    }

    // Skip fetch if we already have the content for this URI (unless forced)
    if (!forceRefetch && loadedUri === uriToFetch && fileContent) {
      return;
    }

    setIsLoading(true);
    setError(null);
    try {
      logger.info('Fetching content:', uriToFetch);

      const response = await client.graphql({
        query: getFileContents,
        variables: { s3Uri: uriToFetch as string },
      });

      // Handle the updated response structure
      const result = response.data.getFileContents;
      if (!result) {
        setError('No content returned from server.');
        return;
      }
      const fetchedContent = result.content;
      logger.debug('Received content type:', result.contentType);
      logger.debug('Binary content?', result.isBinary);
      if (result.isBinary === true) {
        setError('This file contains binary content that cannot be viewed in the Markdown viewer.');
        return;
      }
      logger.debug('Received content:', `${fetchedContent?.substring(0, 100)}...`);
      setFileContent(fetchedContent ?? null);
      setLoadedUri(uriToFetch as string);
    } catch (err) {
      logger.error('Error fetching content:', err);
      setError(isAuthorizationError(err) ? FILE_ACCESS_DENIED_MESSAGE : `Failed to load ${fileType} content. Please try again.`);
    } finally {
      setIsLoading(false);
    }
  };

  const handleEditToggle = ({ detail }: { detail: ToggleProps.ChangeDetail }) => {
    setIsEditing(detail.checked);
    if (detail.checked && !editedContent) {
      setEditedContent(fileContent);
    }
  };

  const handleContentChange = (newContent: string) => {
    setEditedContent(newContent);
  };

  const handleSave = async () => {
    try {
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
      const blob = new Blob([editedContent ?? ''], { type: 'application/json' });
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

      // Update the file content state to reflect saved changes
      setFileContent(editedContent);
      setIsEditing(false);
      setSuccess('File saved and uploaded successfully');
      logger.info('Successfully saved changes');

      // Clear success message after 3 seconds
      setTimeout(() => {
        setSuccess(null);
      }, 3000);
    } catch (err) {
      logger.error('Error saving changes:', err);
      setError(`Failed to save changes: ${(err as Error).message}`);
    }
  };

  const closeViewer = () => {
    setFileContent(null);
    setEditedContent(null);
    setIsEditing(false);
    setIsViewerOpen(false);
    setLoadedUri(null);
  };

  if (!fileUri) {
    return (
      <Box color="text-status-inactive" padding={{ top: 's' }}>
        File content not available
      </Box>
    );
  }

  const handleViewModeChange = ({ detail }: { detail: SegmentedControlProps.ChangeDetail }) => {
    const newMode = detail.selectedId;
    setViewMode(newMode);

    // Determine if we need to fetch new content
    const newUri = newMode === 'confidence' ? textConfidenceUri : fileUri;

    // Only fetch if switching to a different URI
    if (newUri && newUri !== loadedUri) {
      fetchContent();
    }
  };

  // Effect to fetch content when viewer opens or view mode changes
  React.useEffect(() => {
    if (isViewerOpen) {
      fetchContent();
    }
  }, [isViewerOpen, viewMode]);

  const openViewer = () => {
    setIsViewerOpen(true);
  };

  return (
    <Box className="w-full">
      {!isViewerOpen && (
        <Button onClick={openViewer} loading={isLoading} disabled={isLoading}>
          {buttonText}
        </Button>
      )}

      {error && (
        <Box color="text-status-error" padding="s">
          {error}
        </Box>
      )}

      {success && (
        <Box color="text-status-success" padding="s">
          {success}
        </Box>
      )}

      {isViewerOpen && (
        <SpaceBetween
          size="s"
          {...({ className: 'json-viewer-container', style: { width: '100%', minWidth: '700px' } } as Record<string, unknown>)}
        >
          <Box>
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={closeViewer}>Close</Button>
              <Toggle onChange={handleEditToggle} checked={isEditing}>
                Edit mode
              </Toggle>
              {isEditing && (
                <Button variant="primary" onClick={handleSave} disabled={!editedContent || editedContent === fileContent}>
                  Save Changes
                </Button>
              )}
            </SpaceBetween>
          </Box>
          <div style={{ width: '100%' }}>
            <FileEditorView
              fileContent={isLoading ? null : isEditing ? editedContent : fileContent}
              onChange={handleContentChange}
              isReadOnly={!isEditing}
              fileType={fileType}
              viewMode={viewMode}
              onViewModeChange={handleViewModeChange}
              isConfidenceAvailable={!!textConfidenceUri}
            />
          </div>
        </SpaceBetween>
      )}
    </Box>
  );
};

export default MarkdownJsonViewer;
