// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState, useEffect, useMemo } from 'react';
import { Modal, Box, SpaceBetween, Button, SegmentedControl, Alert, Spinner, Badge } from '@cloudscape-design/components';
import { generateClient } from '../../api/client-shim';
import { ConsoleLogger } from 'aws-amplify/utils';
import { Editor } from '@monaco-editor/react';
import MarkdownViewer from '../document-viewer/MarkdownViewer';
import PageImageViewer from '../common/PageImageViewer';
import { getFileContents, uploadDocument } from '../../graphql/generated';

const client = generateClient();
const logger = new ConsoleLogger('PageTextEditorModal');

const EDITOR_HEIGHT = '600px';

// Confidence color thresholds for OCR bounding-box overlays.
const CONFIDENCE_HIGH = 90;
const CONFIDENCE_MEDIUM = 70;

/**
 * One text unit (LINE) from the consolidated pageData.json artifact.
 * confidence/geometry are independently optional per the OCR backend.
 */
interface OcrPageDataLine {
  text?: string;
  confidence?: number | null;
  geometry?: {
    boundingBox?: { left: number; top: number; width: number; height: number };
  } | null;
}

/**
 * One signature region the OCR engine detected (Textract SIGNATURES feature).
 * Kept separate from `lines` because it has confidence and geometry but no text.
 */
interface OcrPageDataSignature {
  id?: string;
  confidence?: number | null;
  geometry?: {
    boundingBox?: { left: number; top: number; width: number; height: number };
  } | null;
}

interface OcrPageData {
  provider?: string;
  geometryAvailable?: boolean;
  confidenceAvailable?: boolean;
  lines?: OcrPageDataLine[];
  signatures?: OcrPageDataSignature[];
}

/** One page's artifact URIs, as supplied by PagesPanel. */
interface PageNavItem {
  Id: string | number;
  ImageUri?: string;
  TextUri?: string;
  TextConfidenceUri?: string;
  OcrPageDataUri?: string;
}

interface PageTextEditorModalProps {
  visible: boolean;
  /** Full ordered list of the document's pages (enables Next/Previous navigation). */
  pages?: PageNavItem[];
  /** The page the modal was opened on. */
  initialPageId?: string | number;
  isReadOnly?: boolean;
  onSave?: (pageId: string | number | undefined, newTextUri: string | null, newConfidenceUri: string | null) => void;
  onClose?: () => void;
}

/**
 * Map an OCR confidence score (0-100) to an overlay color.
 * Lines without confidence are drawn neutral.
 */
const confidenceColor = (confidence: number | null | undefined): string => {
  if (confidence === null || confidence === undefined) return '#888';
  if (confidence >= CONFIDENCE_HIGH) return '#2ca02c';
  if (confidence >= CONFIDENCE_MEDIUM) return '#ff7f0e';
  return '#d62728';
};

/**
 * Extract plain text from JSON-wrapped content
 * Handles both {"text": "..."} and plain text formats
 */
const extractPlainText = (content: string): string => {
  if (!content) return '';

  try {
    const parsed = JSON.parse(content);
    return parsed.text || parsed.Text || content;
  } catch (e) {
    // Already plain text
    return content;
  }
};

/**
 * Wrap plain text in JSON structure for backward compatibility
 */
const wrapInJson = (text: string): string => {
  return JSON.stringify({ text: text || '' }, null, 2);
};

const PageTextEditorModal = ({
  visible,
  pages = [],
  initialPageId,
  isReadOnly = true,
  onSave,
  onClose,
}: PageTextEditorModalProps): React.JSX.Element => {
  // Which page is currently shown; navigation (arrows / Next-Previous) moves it.
  const [currentPageId, setCurrentPageId] = useState<string | number | undefined>(initialPageId);

  const pageIds = useMemo(() => pages.map((p) => String(p.Id)), [pages]);
  const currentPage = useMemo(() => pages.find((p) => String(p.Id) === String(currentPageId)), [pages, currentPageId]);

  // 1-based position of the shown page within the document (for bounding-box geometry
  // and footer navigation). PageImageViewer keys page switching off geometry.page, so
  // the selected line's geometry must carry the *current* page number — otherwise
  // selecting a line would snap the viewer back to page 1.
  const currentPageNumber = useMemo(() => {
    const idx = pageIds.indexOf(String(currentPageId));
    return idx >= 0 ? idx + 1 : 1;
  }, [pageIds, currentPageId]);

  const pageId = currentPage?.Id ?? currentPageId;
  const textUri = currentPage?.TextUri;
  const imageUri = currentPage?.ImageUri;
  const ocrPageDataUri = currentPage?.OcrPageDataUri;

  // Whether the visual (image) view is available for this page.
  const hasVisualView = Boolean(imageUri);

  // Right-pane view: 'ocr-lines' (clickable OCR lines with confidence + bbox) or
  // 'markdown' (the page's extracted markdown). The page image is always shown on
  // the left when available, regardless of which right-pane view is active.
  const [viewMode, setViewMode] = useState('ocr-lines');
  // Within the markdown view, toggle between the rendered preview and the raw
  // markdown source (the raw source is the editable surface in edit mode).
  const [markdownSubMode, setMarkdownSubMode] = useState('rendered');
  const [textContent, setTextContent] = useState('');
  const [originalTextContent, setOriginalTextContent] = useState('');
  const [pageData, setPageData] = useState<OcrPageData | null>(null);
  const [selectedLineIndex, setSelectedLineIndex] = useState<number | null>(null);
  const [selectedSignatureIndex, setSelectedSignatureIndex] = useState<number | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hasUnsavedChanges, setHasUnsavedChanges] = useState(false);
  const [showCloseWarning, setShowCloseWarning] = useState(false);

  const geometryAvailable = Boolean(pageData?.geometryAvailable);
  const lines = useMemo(() => pageData?.lines ?? [], [pageData]);
  // Signature regions the OCR engine detected. Older documents predate the
  // artifact carrying them, so absence is normal.
  const signatures = useMemo(() => pageData?.signatures ?? [], [pageData]);
  // Whether any line actually carries a confidence score (matches the per-line
  // score column and legend below), so the description can reflect reality.
  const hasConfidence = useMemo(() => lines.some((l) => l.confidence != null), [lines]);

  // Only the currently-selected row's box is drawn on the image (the section
  // Visual Editor pattern). Drawing every line at once is unreadable. Lines and
  // signature regions share one selection: picking one clears the other.
  const activeFieldGeometry = useMemo(() => {
    const selected =
      selectedSignatureIndex !== null ? signatures[selectedSignatureIndex] : selectedLineIndex !== null ? lines[selectedLineIndex] : null;
    if (!selected?.geometry?.boundingBox) return null;
    // Carry the current page number so PageImageViewer keeps the box on the page
    // being viewed (not page 1). pageData lines are scoped to the current page.
    return { boundingBox: selected.geometry.boundingBox, page: currentPageNumber };
  }, [selectedLineIndex, selectedSignatureIndex, lines, signatures, currentPageNumber]);

  // All pages with an image, so PageImageViewer can navigate across them.
  const documentPages = useMemo(() => pages.filter((p) => p.ImageUri).map((p) => ({ Id: String(p.Id), ImageUri: p.ImageUri })), [pages]);

  // When the modal opens, start on the page it was launched from.
  useEffect(() => {
    if (visible) {
      setCurrentPageId(initialPageId);
    }
  }, [visible, initialPageId]);

  // Reset line selection / default view whenever the shown page changes.
  useEffect(() => {
    setSelectedLineIndex(null);
    setSelectedSignatureIndex(null);
    setViewMode('ocr-lines');
    setMarkdownSubMode('rendered');
  }, [currentPageId]);

  // Fetch content when modal opens
  useEffect(() => {
    if (visible && textUri) {
      fetchContent();
    }
  }, [visible, textUri, ocrPageDataUri]);

  // Track unsaved changes
  useEffect(() => {
    setHasUnsavedChanges(textContent !== originalTextContent);
  }, [textContent, originalTextContent]);

  const fetchContent = async () => {
    setIsLoading(true);
    setError(null);

    try {
      // Fetch text content
      const textResponse = await client.graphql({
        query: getFileContents,
        variables: { s3Uri: textUri as string },
      });

      const textResult = textResponse.data?.getFileContents;
      if (!textResult) {
        throw new Error('No response from getFileContents');
      }
      if (textResult.isBinary) {
        throw new Error('Text file contains binary content');
      }

      // Extract plain text from JSON wrapper
      const plainText = extractPlainText(textResult.content ?? '');
      setTextContent(plainText);
      setOriginalTextContent(plainText);

      // Fetch consolidated OCR page data (text + confidence + geometry) if
      // available. Older documents predate this artifact, so absence is normal
      // and the visual view simply omits bounding-box overlays.
      if (ocrPageDataUri) {
        try {
          const pageDataResponse = await client.graphql({
            query: getFileContents,
            variables: { s3Uri: ocrPageDataUri },
          });

          const pageDataResult = pageDataResponse.data?.getFileContents;
          if (pageDataResult && !pageDataResult.isBinary && pageDataResult.content) {
            const parsed = JSON.parse(pageDataResult.content) as OcrPageData;
            setPageData(parsed);
            // If this backend produced no OCR lines (e.g. an older document with
            // no pageData, or an empty page), default the right pane to Markdown
            // so there is always something to show rather than an empty list.
            if (!parsed.lines?.length && !parsed.signatures?.length) {
              setViewMode('markdown');
            }
          } else {
            // No pageData content at all: fall back to the markdown view.
            setViewMode('markdown');
          }
        } catch (err) {
          logger.warn('Failed to load OCR page data:', err);
          // Not critical - continue without geometry overlays
          setViewMode('markdown');
        }
      } else {
        // Older documents predate the pageData.json artifact entirely; there are
        // no OCR lines to list, so show the markdown text by default.
        setViewMode('markdown');
      }
    } catch (err) {
      logger.error('Error fetching content:', err);
      setError(`Failed to load page content: ${(err as Error).message}`);
    } finally {
      setIsLoading(false);
    }
  };

  const handleTextChange = (value: string | undefined): void => {
    setTextContent(value || '');
  };

  const handleSave = async () => {
    setIsSaving(true);
    setError(null);

    try {
      let newTextUri = null;

      // Save text content if changed
      if (textContent !== originalTextContent) {
        newTextUri = await saveToS3(textUri!, wrapInJson(textContent), 'application/json');
        logger.info('Saved text content to:', newTextUri);
      }

      // Update original content to mark as saved
      setOriginalTextContent(textContent);

      // Notify parent of save. Confidence is no longer edited here, so the
      // confidence URI is always passed through unchanged (null).
      if (onSave) {
        onSave(pageId, newTextUri, null);
      }

      // Close modal after successful save
      handleCloseModal();
    } catch (err) {
      logger.error('Error saving content:', err);
      setError(`Failed to save changes: ${(err as Error).message}`);
    } finally {
      setIsSaving(false);
    }
  };

  const saveToS3 = async (s3Uri: string, content: string, contentType: string): Promise<string> => {
    // Parse S3 URI to get bucket and key
    const match = s3Uri.match(/^s3:\/\/([^/]+)\/(.+)$/);
    if (!match) {
      throw new Error('Invalid S3 URI format');
    }

    const [, bucket, fullPath] = match;
    const fileName = fullPath.split('/').pop() ?? fullPath;
    const prefix = fullPath.substring(0, fullPath.lastIndexOf('/'));

    // Get presigned URL
    const response = await client.graphql({
      query: uploadDocument,
      variables: {
        fileName,
        contentType,
        prefix,
        bucket,
      },
    });

    const { presignedUrl, usePostMethod } = response.data.uploadDocument;
    const usePost = usePostMethod?.toLowerCase() === 'true';

    if (!usePost) {
      throw new Error('Server returned PUT method which is not supported');
    }

    // Parse presigned POST data
    const presignedPostData = JSON.parse(presignedUrl);

    // Create form data
    const formData = new FormData();
    Object.entries(presignedPostData.fields).forEach(([key, value]) => {
      formData.append(key, value as string);
    });

    // Add file
    const blob = new Blob([content], { type: contentType });
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

    return s3Uri; // Return the same URI (content updated in place)
  };

  const handleCloseClick = () => {
    if (hasUnsavedChanges && !isReadOnly) {
      setShowCloseWarning(true);
    } else {
      handleCloseModal();
    }
  };

  const handleCloseModal = () => {
    setShowCloseWarning(false);
    setTextContent('');
    setOriginalTextContent('');
    setPageData(null);
    setSelectedLineIndex(null);
    setSelectedSignatureIndex(null);
    setError(null);
    setHasUnsavedChanges(false);
    if (onClose) {
      onClose();
    }
  };

  const handleForceClose = () => {
    handleCloseModal();
  };

  // Footer page navigation (mirrors the section Visual Editor's Previous/Next Section).
  const currentPageIndex = pageIds.indexOf(String(currentPageId));
  const canGoPrevious = currentPageIndex > 0;
  const canGoNext = currentPageIndex >= 0 && currentPageIndex < pageIds.length - 1;

  const goToPage = (targetIndex: number) => {
    if (hasUnsavedChanges && !isReadOnly) {
      alert('Please save or discard your changes before navigating to another page.');
      return;
    }
    if (targetIndex >= 0 && targetIndex < pageIds.length) {
      setCurrentPageId(pageIds[targetIndex]);
    }
  };

  return (
    <>
      <Modal
        visible={visible}
        onDismiss={handleCloseClick}
        size="max"
        header={`${isReadOnly ? 'View' : 'Edit'} Page ${pageId} Text`}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              {/* Page navigation (mirrors the section Visual Editor's Previous/Next Section) */}
              {pageIds.length > 1 && (
                <>
                  <Button
                    iconName="angle-left"
                    variant="normal"
                    onClick={() => goToPage(currentPageIndex - 1)}
                    disabled={!canGoPrevious || isSaving}
                  >
                    Previous Page
                  </Button>
                  <Button
                    iconAlign="right"
                    iconName="angle-right"
                    variant="normal"
                    onClick={() => goToPage(currentPageIndex + 1)}
                    disabled={!canGoNext || isSaving}
                  >
                    Next Page
                  </Button>
                </>
              )}
              <Button onClick={handleCloseClick} disabled={isSaving}>
                {hasUnsavedChanges ? 'Cancel' : 'Close'}
              </Button>
              {!isReadOnly && (
                <Button variant="primary" onClick={handleSave} disabled={!hasUnsavedChanges || isSaving} loading={isSaving}>
                  Save
                </Button>
              )}
            </SpaceBetween>
          </Box>
        }
      >
        <Box>
          {error && (
            <Box margin={{ bottom: 's' }}>
              <Alert type="error" header="Error">
                {error}
              </Alert>
            </Box>
          )}

          {isLoading ? (
            <Box textAlign="center" padding="xxl">
              <Spinner size="large" />
              <Box variant="p" color="text-body-secondary">
                Loading page content...
              </Box>
            </Box>
          ) : (
            <div style={{ display: 'flex', gap: '12px', minHeight: EDITOR_HEIGHT, alignItems: 'stretch' }}>
              {/* Left pane: page image with the selected line's bounding box.
                  Always shown when the page has an image, regardless of the
                  right-pane view (OCR lines vs. markdown). */}
              {hasVisualView && (
                <div style={{ flex: '0 0 50%', display: 'flex', flexDirection: 'column', minWidth: 0 }}>
                  <PageImageViewer
                    key={String(pageId)}
                    pageIds={pageIds}
                    documentPages={documentPages}
                    initialPage={String(pageId)}
                    onPageChange={(newPageId) => setCurrentPageId(newPageId)}
                    height={EDITOR_HEIGHT}
                    activeFieldGeometry={activeFieldGeometry}
                  />
                </div>
              )}

              {/* Right pane: toggle between clickable OCR lines and the page markdown. */}
              <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
                <Box margin={{ bottom: 'xs' }}>
                  <SpaceBetween direction="horizontal" size="xs">
                    <SegmentedControl
                      selectedId={viewMode}
                      onChange={({ detail }) => setViewMode(detail.selectedId)}
                      options={[
                        { id: 'ocr-lines', text: 'OCR Lines' },
                        { id: 'markdown', text: 'Markdown' },
                      ]}
                    />
                    {viewMode === 'markdown' && (
                      <SegmentedControl
                        selectedId={markdownSubMode}
                        onChange={({ detail }) => setMarkdownSubMode(detail.selectedId)}
                        options={[
                          { id: 'rendered', text: 'Rendered' },
                          { id: 'raw', text: isReadOnly ? 'Raw' : 'Raw (editable)' },
                        ]}
                      />
                    )}
                  </SpaceBetween>
                </Box>

                {viewMode === 'ocr-lines' ? (
                  <>
                    <Box fontSize="body-s" color="text-label" margin={{ bottom: 'xxxs' }}>
                      OCR Text Lines
                    </Box>
                    <Box fontSize="body-s" color="text-body-secondary" margin={{ bottom: 'xxs' }}>
                      {hasVisualView && geometryAvailable
                        ? 'Click a row to highlight its bounding box on the image.'
                        : 'Bounding boxes are not available for this page.'}
                      {hasConfidence
                        ? ' The number on the right is the OCR confidence score for the line.'
                        : ' Confidence scores are not available for this page.'}
                      {signatures.length > 0 &&
                        ' Detected signature regions are listed at the end — a low confidence there means a faint or ambiguous mark rather than a clear signature.'}
                    </Box>
                    {hasVisualView && !geometryAvailable && (
                      <Box margin={{ bottom: 'xs' }}>
                        <Alert type="info">
                          This OCR backend did not provide bounding-box geometry for this page, so lines cannot be highlighted on the image.
                        </Alert>
                      </Box>
                    )}
                    <div
                      style={{
                        border: '1px solid #e9ebed',
                        height: EDITOR_HEIGHT,
                        overflow: 'auto',
                        backgroundColor: '#fff',
                      }}
                    >
                      {lines.length === 0 && signatures.length === 0 ? (
                        <Box padding="m" color="text-body-secondary">
                          No OCR text lines available for this page.
                        </Box>
                      ) : (
                        lines.map((line, index) => {
                          const canHighlight = hasVisualView && Boolean(line.geometry?.boundingBox);
                          const isSelected = selectedSignatureIndex === null && selectedLineIndex === index;
                          const select = () => {
                            setSelectedSignatureIndex(null);
                            setSelectedLineIndex(isSelected ? null : index);
                          };
                          return (
                            <div
                              // eslint-disable-next-line react/no-array-index-key -- lines are a stable ordered OCR list
                              key={`ocr-line-${index}`}
                              onClick={() => canHighlight && select()}
                              role={canHighlight ? 'button' : undefined}
                              tabIndex={canHighlight ? 0 : undefined}
                              onKeyDown={(e) => {
                                if (canHighlight && (e.key === 'Enter' || e.key === ' ')) {
                                  e.preventDefault();
                                  select();
                                }
                              }}
                              style={{
                                display: 'flex',
                                justifyContent: 'space-between',
                                alignItems: 'center',
                                gap: '8px',
                                padding: '4px 8px',
                                borderBottom: '1px solid #f2f3f3',
                                cursor: canHighlight ? 'pointer' : 'default',
                                backgroundColor: isSelected ? '#f0f7ff' : 'transparent',
                                borderLeft: isSelected ? '3px solid #0972d3' : '3px solid transparent',
                              }}
                            >
                              <span style={{ fontSize: '13px', wordBreak: 'break-word', whiteSpace: 'pre-wrap' }}>
                                {line.text || <em style={{ color: '#888' }}>(empty)</em>}
                              </span>
                              {line.confidence != null && (
                                <span style={{ flexShrink: 0, color: confidenceColor(line.confidence), fontSize: '12px', fontWeight: 600 }}>
                                  {line.confidence}
                                </span>
                              )}
                            </div>
                          );
                        })
                      )}
                      {/* Signature regions have confidence + geometry but no text,
                          so they are listed separately from the OCR lines. Without
                          this they were invisible in the UI even though the OCR
                          engine had flagged them. */}
                      {signatures.length > 0 && (
                        <>
                          <Box padding={{ vertical: 'xxs', horizontal: 's' }} fontSize="body-s" color="text-label" fontWeight="bold">
                            Signature detections ({signatures.length})
                          </Box>
                          {signatures.map((signature, index) => {
                            const canHighlight = hasVisualView && Boolean(signature.geometry?.boundingBox);
                            const isSelected = selectedSignatureIndex === index;
                            const select = () => {
                              setSelectedLineIndex(null);
                              setSelectedSignatureIndex(isSelected ? null : index);
                            };
                            return (
                              <div
                                key={signature.id ?? `ocr-signature-${index}`}
                                onClick={() => canHighlight && select()}
                                role={canHighlight ? 'button' : undefined}
                                tabIndex={canHighlight ? 0 : undefined}
                                onKeyDown={(e) => {
                                  if (canHighlight && (e.key === 'Enter' || e.key === ' ')) {
                                    e.preventDefault();
                                    select();
                                  }
                                }}
                                style={{
                                  display: 'flex',
                                  justifyContent: 'space-between',
                                  alignItems: 'center',
                                  gap: '8px',
                                  padding: '4px 8px',
                                  borderBottom: '1px solid #f2f3f3',
                                  cursor: canHighlight ? 'pointer' : 'default',
                                  backgroundColor: isSelected ? '#f0f7ff' : 'transparent',
                                  borderLeft: isSelected ? '3px solid #0972d3' : '3px solid transparent',
                                }}
                              >
                                <span style={{ fontSize: '13px' }}>
                                  <em>[SIGNATURE] region {index + 1}</em>
                                </span>
                                {signature.confidence != null && (
                                  <span
                                    style={{
                                      flexShrink: 0,
                                      color: confidenceColor(signature.confidence),
                                      fontSize: '12px',
                                      fontWeight: 600,
                                    }}
                                  >
                                    {signature.confidence}
                                  </span>
                                )}
                              </div>
                            );
                          })}
                        </>
                      )}
                    </div>
                    {(hasConfidence || signatures.some((s) => s.confidence != null)) && (
                      <Box fontSize="body-s" color="text-body-secondary" margin={{ top: 'xxs' }}>
                        Confidence color: <Badge color="green">≥ {CONFIDENCE_HIGH}</Badge>{' '}
                        <Badge color="severity-medium">≥ {CONFIDENCE_MEDIUM}</Badge> <Badge color="red">below</Badge>
                      </Box>
                    )}
                  </>
                ) : (
                  <>
                    <Box fontSize="body-s" color="text-label" margin={{ bottom: 'xxxs' }}>
                      {markdownSubMode === 'raw'
                        ? `Raw markdown (${isReadOnly ? 'read-only' : 'editable'})`
                        : 'Rendered markdown (read-only)'}
                    </Box>
                    <Box fontSize="body-s" color="text-body-secondary" margin={{ bottom: 'xxs' }}>
                      Confidence scores and bounding boxes are not available in the markdown view — switch to OCR Lines for those.
                    </Box>
                    {markdownSubMode === 'raw' ? (
                      <div style={{ border: '1px solid #e9ebed', height: EDITOR_HEIGHT }}>
                        <Editor
                          key="editor-markdown"
                          height={EDITOR_HEIGHT}
                          defaultLanguage="markdown"
                          value={textContent}
                          onChange={handleTextChange}
                          options={{
                            readOnly: isReadOnly,
                            minimap: { enabled: false },
                            fontSize: 14,
                            wordWrap: 'on',
                            wrappingIndent: 'indent',
                            automaticLayout: true,
                            scrollBeyondLastLine: false,
                          }}
                          theme="vs-light"
                        />
                      </div>
                    ) : (
                      <div
                        style={{
                          border: '1px solid #e9ebed',
                          height: EDITOR_HEIGHT,
                          overflow: 'auto',
                          padding: '16px',
                          backgroundColor: '#fafafa',
                        }}
                        className="page-text-markdown-preview"
                      >
                        <MarkdownViewer simple content={textContent} />
                      </div>
                    )}
                  </>
                )}
              </div>
            </div>
          )}
        </Box>
      </Modal>

      {/* Unsaved changes warning modal */}
      <Modal
        visible={showCloseWarning}
        onDismiss={() => setShowCloseWarning(false)}
        header="Unsaved Changes"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => setShowCloseWarning(false)}>Continue Editing</Button>
              <Button variant="primary" onClick={handleForceClose}>
                Discard Changes
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        You have unsaved changes. Are you sure you want to close without saving?
      </Modal>
    </>
  );
};

export default PageTextEditorModal;
