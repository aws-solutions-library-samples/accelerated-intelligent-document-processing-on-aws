// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState, useEffect, useMemo } from 'react';
import { Modal, Box, SpaceBetween, Button, SegmentedControl, Alert, Spinner, Badge } from '@cloudscape-design/components';
import { defineMessages, FormattedMessage, useIntl, type NoMessageValues } from 'react-intl';
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

const messages = defineMessages<{
  loadFailed: { readonly error: string };
  saveFailed: { readonly page: string; readonly error: string };
  restored: { readonly page: string };
  restoreFailed: { readonly error: string };
  navBlockedUnfinished: NoMessageValues;
  navBlockedUnsaved: NoMessageValues;
  viewTitle: { readonly page: string };
  editTitle: { readonly page: string };
  previousPage: NoMessageValues;
  nextPage: NoMessageValues;
  cancel: NoMessageValues;
  close: NoMessageValues;
  save: NoMessageValues;
  errorHeader: NoMessageValues;
  retry: NoMessageValues;
  restore: NoMessageValues;
  unfinished: { readonly message: string };
  loading: NoMessageValues;
  viewOcrLines: NoMessageValues;
  viewMarkdown: NoMessageValues;
  viewRendered: NoMessageValues;
  viewRaw: NoMessageValues;
  viewRawEditable: NoMessageValues;
  ocrTextLines: NoMessageValues;
  noGeometry: NoMessageValues;
  noLines: NoMessageValues;
  emptyLine: NoMessageValues;
  signatures: { readonly count: number };
  signatureRegion: { readonly index: number };
  confidenceLegend: NoMessageValues;
  below: NoMessageValues;
  rawReadOnly: NoMessageValues;
  rawEditable: NoMessageValues;
  renderedReadOnly: NoMessageValues;
  markdownNote: NoMessageValues;
}>({
  loadFailed: {
    id: 'documents.pageText.loadFailed',
    defaultMessage: 'Failed to load page content: {error}',
    description: "Error in the page text dialog when the page's text could not be loaded. {error} is the error's own text.",
  },
  saveFailed: {
    id: 'documents.pageText.saveFailed',
    defaultMessage: "Could not save page {page}'s text: {error}. Nothing was changed.",
    description: "Error when the page's edited text could not be uploaded. {page} is the page number; {error} is the error's own text.",
  },
  restored: {
    id: 'documents.pageText.restored',
    defaultMessage: "Page {page}'s original text is restored. Nothing was reprocessed.",
    description: 'Confirmation after Restore original text. {page} is the page number.',
  },
  restoreFailed: {
    id: 'documents.pageText.restoreFailed',
    defaultMessage: 'Could not restore the original text: {error}',
    description: "Shown when Restore original text fails. {error} is the error's own text.",
  },
  navBlockedUnfinished: {
    id: 'documents.pageText.navBlockedUnfinished',
    defaultMessage: 'Reprocess or restore this page first',
    description: "Why Previous Page and Next Page are unavailable: the page's text was saved but not reprocessed.",
  },
  navBlockedUnsaved: {
    id: 'documents.pageText.navBlockedUnsaved',
    defaultMessage: 'Save or discard your changes first',
    description: 'Why Previous Page and Next Page are unavailable: the text has unsaved changes.',
  },
  viewTitle: {
    id: 'documents.pageText.viewTitle',
    defaultMessage: 'View Page {page} Text',
    description: "Title of the dialog that shows a page's text, read-only. {page} is the page number.",
  },
  editTitle: {
    id: 'documents.pageText.editTitle',
    defaultMessage: 'Edit Page {page} Text',
    description: "Title of the dialog that edits a page's text. {page} is the page number.",
  },
  previousPage: {
    id: 'documents.pageText.previousPage',
    defaultMessage: 'Previous Page',
    description: 'Footer button of the page text dialog that shows the previous page.',
  },
  nextPage: {
    id: 'documents.pageText.nextPage',
    defaultMessage: 'Next Page',
    description: 'Footer button of the page text dialog that shows the next page.',
  },
  cancel: {
    id: 'documents.pageText.cancel',
    defaultMessage: 'Cancel',
    description: 'Footer button that closes the page text dialog while the text has unsaved changes.',
  },
  close: {
    id: 'documents.pageText.close',
    defaultMessage: 'Close',
    description: 'Footer button that closes the page text dialog.',
  },
  save: {
    id: 'documents.pageText.save',
    defaultMessage: 'Save',
    description: 'Footer button that saves the edited page text, when the screen names no other label.',
  },
  errorHeader: {
    id: 'documents.pageText.errorHeader',
    defaultMessage: 'Error',
    description: 'Heading of the error alert in the page text dialog.',
  },
  retry: {
    id: 'documents.pageText.retry',
    defaultMessage: 'Retry reprocess',
    description: 'Button that reprocesses the document again after a failed reprocess, without uploading the text again.',
  },
  restore: {
    id: 'documents.pageText.restore',
    defaultMessage: 'Restore original text',
    description: "Button that uploads the page's text as the dialog opened it; nothing is reprocessed.",
  },
  unfinished: {
    id: 'documents.pageText.unfinished',
    defaultMessage: 'The page text was saved, but the document was not reprocessed: {message}',
    description: "Error after a save whose reprocess failed. {message} is the error's own text.",
  },
  loading: {
    id: 'documents.pageText.loading',
    defaultMessage: 'Loading page content...',
    description: "Shown while the page text dialog loads the page's text.",
  },
  viewOcrLines: {
    id: 'documents.pageText.view.ocrLines',
    defaultMessage: 'OCR Lines',
    description: "Option of the page text dialog's view switcher: the page's OCR text lines.",
  },
  viewMarkdown: {
    id: 'documents.pageText.view.markdown',
    defaultMessage: 'Markdown',
    description: "Option of the page text dialog's view switcher: the page's text as markdown.",
  },
  viewRendered: {
    id: 'documents.pageText.view.rendered',
    defaultMessage: 'Rendered',
    description: 'Option of the markdown view: the markdown as formatted text.',
  },
  viewRaw: {
    id: 'documents.pageText.view.raw',
    defaultMessage: 'Raw',
    description: 'Option of the markdown view: the markdown source, read-only.',
  },
  viewRawEditable: {
    id: 'documents.pageText.view.rawEditable',
    defaultMessage: 'Raw (editable)',
    description: 'Option of the markdown view: the markdown source, which can be edited.',
  },
  ocrTextLines: {
    id: 'documents.pageText.ocrTextLines',
    defaultMessage: 'OCR Text Lines',
    description: "Label above the list of the page's OCR text lines.",
  },
  noGeometry: {
    id: 'documents.pageText.noGeometry',
    defaultMessage: 'This OCR backend did not provide bounding-box geometry for this page, so lines cannot be highlighted on the image.',
    description: 'Notice above the OCR text lines when the OCR gave no positions for them.',
  },
  noLines: {
    id: 'documents.pageText.noLines',
    defaultMessage: 'No OCR text lines available for this page.',
    description: 'Shown in place of the OCR text lines when the page has none.',
  },
  emptyLine: {
    id: 'documents.pageText.emptyLine',
    defaultMessage: '(empty)',
    description: 'Shown in place of an OCR text line that has no text.',
  },
  signatures: {
    id: 'documents.pageText.signatures',
    defaultMessage: 'Signature detections ({count, number})',
    description: 'Heading of the signature regions the OCR detected on the page. {count} is how many.',
  },
  signatureRegion: {
    id: 'documents.pageText.signatureRegion',
    defaultMessage: '[SIGNATURE] region {index, number}',
    description: 'One detected signature region. [SIGNATURE] is the marker the page text uses for it; {index} counts from 1.',
  },
  confidenceLegend: {
    id: 'documents.pageText.confidenceLegend',
    defaultMessage: 'Confidence color:',
    description: 'Label before the badges that explain the colors of the confidence scores.',
  },
  below: {
    id: 'documents.pageText.below',
    defaultMessage: 'below',
    description: 'Badge for confidence scores below the lower threshold; it follows the badges ≥ 90 and ≥ 70.',
  },
  rawReadOnly: {
    id: 'documents.pageText.rawReadOnly',
    defaultMessage: 'Raw markdown (read-only)',
    description: 'Label above the markdown source when it cannot be edited.',
  },
  rawEditable: {
    id: 'documents.pageText.rawEditable',
    defaultMessage: 'Raw markdown (editable)',
    description: 'Label above the markdown source when it can be edited.',
  },
  renderedReadOnly: {
    id: 'documents.pageText.renderedReadOnly',
    defaultMessage: 'Rendered markdown (read-only)',
    description: 'Label above the markdown shown as formatted text.',
  },
  markdownNote: {
    id: 'documents.pageText.markdownNote',
    defaultMessage: 'Confidence scores and bounding boxes are not available in the markdown view — switch to OCR Lines for those.',
    description: "Note in the markdown view. OCR Lines is the view switcher's option.",
  },
});

/** The hint under OCR Text Lines: up to three sentences, each shown on its own, so each is a whole message. */
const hintMessages = defineMessages<{
  highlight: NoMessageValues;
  noBoundingBoxes: NoMessageValues;
  confidence: NoMessageValues;
  noConfidence: NoMessageValues;
  signatures: NoMessageValues;
}>({
  highlight: {
    id: 'documents.pageText.hint.highlight',
    defaultMessage: 'Click a row to highlight its bounding box on the image.',
    description: 'First sentence of the hint under OCR Text Lines, when the lines have positions on the page image.',
  },
  noBoundingBoxes: {
    id: 'documents.pageText.hint.noBoundingBoxes',
    defaultMessage: 'Bounding boxes are not available for this page.',
    description: 'First sentence of the hint under OCR Text Lines, when the lines have no positions on the page image.',
  },
  confidence: {
    id: 'documents.pageText.hint.confidence',
    defaultMessage: 'The number on the right is the OCR confidence score for the line.',
    description: 'Second sentence of the hint under OCR Text Lines, when the lines have confidence scores.',
  },
  noConfidence: {
    id: 'documents.pageText.hint.noConfidence',
    defaultMessage: 'Confidence scores are not available for this page.',
    description: 'Second sentence of the hint under OCR Text Lines, when the lines have no confidence scores.',
  },
  signatures: {
    id: 'documents.pageText.hint.signatures',
    defaultMessage:
      'Detected signature regions are listed at the end — a low confidence there means a faint or ambiguous mark rather than a clear signature.',
    description: 'Last sentence of the hint under OCR Text Lines, when the OCR detected signature regions.',
  },
});

const unsavedMessages = defineMessages<{
  title: NoMessageValues;
  continueEditing: NoMessageValues;
  discard: NoMessageValues;
  body: NoMessageValues;
}>({
  title: {
    id: 'documents.pageText.unsaved.title',
    defaultMessage: 'Unsaved Changes',
    description: 'Title of the dialog shown when the page text dialog is closed with unsaved changes.',
  },
  continueEditing: {
    id: 'documents.pageText.unsaved.continue',
    defaultMessage: 'Continue Editing',
    description: 'Button of the unsaved-changes dialog that goes back to the editor.',
  },
  discard: {
    id: 'documents.pageText.unsaved.discard',
    defaultMessage: 'Discard Changes',
    description: 'Button of the unsaved-changes dialog that closes without saving.',
  },
  body: {
    id: 'documents.pageText.unsaved.body',
    defaultMessage: 'You have unsaved changes. Are you sure you want to close without saving?',
    description: 'Question in the unsaved-changes dialog.',
  },
});

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
  /**
   * After the page's text is uploaded: what the save then does (the document page's Edit
   * Page Text reprocesses the document). Awaited; a throw keeps the dialog open, saying
   * the text was saved but the rest was not, with Retry reprocess (this call again, with
   * no new upload) and Restore original text.
   */
  onSave?: (page: { pageId: string; textUri: string }) => void | Promise<void>;
  /** The save button's label; Save when not given. */
  saveLabel?: string;
  /** Why the save button is unavailable now, if it is. */
  saveDisabledReason?: string;
  /** What saving does, shown above the editor in edit mode. */
  saveNotice?: React.ReactNode;
  /** Closed after the text was saved but `onSave` failed, without retrying or restoring. */
  onUnfinished?: (page: { pageId: string; textUri: string }) => void;
  onClose?: () => void;
}

/** A save whose text upload worked and whose `onSave` failed: until it is retried or the text restored. */
interface Unfinished {
  page: { pageId: string; textUri: string };
  /** The text as the dialog opened it, for Restore original text. */
  originalText: string;
  message: string;
  restoreError?: string;
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
  saveLabel,
  saveDisabledReason,
  saveNotice,
  onUnfinished,
  onClose,
}: PageTextEditorModalProps): React.JSX.Element => {
  const intl = useIntl();
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
  const [unfinished, setUnfinished] = useState<Unfinished | null>(null);
  const [restored, setRestored] = useState<string | null>(null);
  const [isRetrying, setIsRetrying] = useState(false);

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
      // String(), as the template literal did: the API client rejects with a plain `{ errors }` object, which has no message.
      setError(intl.formatMessage(messages.loadFailed, { error: String((err as Error).message) }));
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
    setRestored(null);
    const id = String(pageId);
    const originalText = originalTextContent;

    let savedUri: string;
    try {
      savedUri = await saveToS3(textUri!, wrapInJson(textContent), 'application/json');
      logger.info('Saved text content to:', savedUri);
    } catch (err) {
      logger.error('Error saving content:', err);
      setError(intl.formatMessage(messages.saveFailed, { page: id, error: String((err as Error).message) }));
      setIsSaving(false);
      return;
    }
    // The text is saved: it is what the page holds now.
    setOriginalTextContent(textContent);

    try {
      await onSave?.({ pageId: id, textUri: savedUri });
    } catch (err) {
      logger.error('Saved the page text, but the follow-up failed:', err);
      setUnfinished({ page: { pageId: id, textUri: savedUri }, originalText, message: (err as Error).message });
      setIsSaving(false);
      return;
    }
    setIsSaving(false);
    handleCloseModal();
  };

  /** Retry reprocess: `onSave` again, with no new upload. */
  const retryUnfinished = async () => {
    if (!unfinished) return;
    setIsRetrying(true);
    try {
      await onSave?.(unfinished.page);
      setUnfinished(null);
      handleCloseModal();
    } catch (err) {
      setUnfinished({ ...unfinished, message: (err as Error).message, restoreError: undefined });
    } finally {
      setIsRetrying(false);
    }
  };

  /** Restore original text: upload the text as the dialog opened it. Nothing is reprocessed. */
  const restoreOriginal = async () => {
    if (!unfinished) return;
    setIsRetrying(true);
    try {
      await saveToS3(unfinished.page.textUri, wrapInJson(unfinished.originalText), 'application/json');
      setTextContent(unfinished.originalText);
      setOriginalTextContent(unfinished.originalText);
      setRestored(intl.formatMessage(messages.restored, { page: unfinished.page.pageId }));
      setUnfinished(null);
    } catch (err) {
      setUnfinished({ ...unfinished, restoreError: intl.formatMessage(messages.restoreFailed, { error: String((err as Error).message) }) });
    } finally {
      setIsRetrying(false);
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
    if (unfinished) {
      // Closed with the text saved but not reprocessed: the page says so, with Retry reprocess.
      onUnfinished?.(unfinished.page);
      handleCloseModal();
      return;
    }
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
    setUnfinished(null);
    setRestored(null);
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

  // Moving to another page would lose unsaved edits, or leave a saved text unreprocessed.
  const navigationReason = unfinished
    ? intl.formatMessage(messages.navBlockedUnfinished)
    : hasUnsavedChanges && !isReadOnly
      ? intl.formatMessage(messages.navBlockedUnsaved)
      : undefined;

  const goToPage = (targetIndex: number) => {
    if (navigationReason) return;
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
        header={intl.formatMessage(isReadOnly ? messages.viewTitle : messages.editTitle, { page: String(pageId) })}
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
                    disabled={!canGoPrevious || isSaving || !!navigationReason}
                    disabledReason={canGoPrevious ? navigationReason : undefined}
                  >
                    <FormattedMessage {...messages.previousPage} />
                  </Button>
                  <Button
                    iconAlign="right"
                    iconName="angle-right"
                    variant="normal"
                    onClick={() => goToPage(currentPageIndex + 1)}
                    disabled={!canGoNext || isSaving || !!navigationReason}
                    disabledReason={canGoNext ? navigationReason : undefined}
                  >
                    <FormattedMessage {...messages.nextPage} />
                  </Button>
                </>
              )}
              <Button onClick={handleCloseClick} disabled={isSaving || isRetrying}>
                <FormattedMessage {...(hasUnsavedChanges ? messages.cancel : messages.close)} />
              </Button>
              {!isReadOnly && (
                <Button
                  variant="primary"
                  onClick={handleSave}
                  disabled={!hasUnsavedChanges || isSaving || !!unfinished || !!saveDisabledReason}
                  disabledReason={saveDisabledReason}
                  loading={isSaving}
                >
                  {saveLabel ?? <FormattedMessage {...messages.save} />}
                </Button>
              )}
            </SpaceBetween>
          </Box>
        }
      >
        <Box>
          {!isReadOnly && saveNotice && (
            <Box margin={{ bottom: 's' }}>
              <Alert type="info">{saveNotice}</Alert>
            </Box>
          )}
          {error && (
            <Box margin={{ bottom: 's' }}>
              <Alert type="error" header={intl.formatMessage(messages.errorHeader)}>
                {error}
              </Alert>
            </Box>
          )}
          {unfinished && (
            <Box margin={{ bottom: 's' }}>
              <Alert
                type="error"
                action={
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button loading={isRetrying} onClick={() => void retryUnfinished()}>
                      <FormattedMessage {...messages.retry} />
                    </Button>
                    <Button disabled={isRetrying} onClick={() => void restoreOriginal()}>
                      <FormattedMessage {...messages.restore} />
                    </Button>
                  </SpaceBetween>
                }
              >
                <FormattedMessage {...messages.unfinished} values={{ message: unfinished.message }} />
                {unfinished.restoreError && <Box color="text-status-error">{unfinished.restoreError}</Box>}
              </Alert>
            </Box>
          )}
          {restored && (
            <Box margin={{ bottom: 's' }}>
              <Alert type="success">{restored}</Alert>
            </Box>
          )}

          {isLoading ? (
            <Box textAlign="center" padding="xxl">
              <Spinner size="large" />
              <Box variant="p" color="text-body-secondary">
                <FormattedMessage {...messages.loading} />
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
                        { id: 'ocr-lines', text: intl.formatMessage(messages.viewOcrLines) },
                        { id: 'markdown', text: intl.formatMessage(messages.viewMarkdown) },
                      ]}
                    />
                    {viewMode === 'markdown' && (
                      <SegmentedControl
                        selectedId={markdownSubMode}
                        onChange={({ detail }) => setMarkdownSubMode(detail.selectedId)}
                        options={[
                          { id: 'rendered', text: intl.formatMessage(messages.viewRendered) },
                          { id: 'raw', text: intl.formatMessage(isReadOnly ? messages.viewRaw : messages.viewRawEditable) },
                        ]}
                      />
                    )}
                  </SpaceBetween>
                </Box>

                {viewMode === 'ocr-lines' ? (
                  <>
                    <Box fontSize="body-s" color="text-label" margin={{ bottom: 'xxxs' }}>
                      <FormattedMessage {...messages.ocrTextLines} />
                    </Box>
                    <Box fontSize="body-s" color="text-body-secondary" margin={{ bottom: 'xxs' }}>
                      <FormattedMessage {...(hasVisualView && geometryAvailable ? hintMessages.highlight : hintMessages.noBoundingBoxes)} />{' '}
                      <FormattedMessage {...(hasConfidence ? hintMessages.confidence : hintMessages.noConfidence)} />
                      {signatures.length > 0 && (
                        <>
                          {' '}
                          <FormattedMessage {...hintMessages.signatures} />
                        </>
                      )}
                    </Box>
                    {hasVisualView && !geometryAvailable && (
                      <Box margin={{ bottom: 'xs' }}>
                        <Alert type="info">
                          <FormattedMessage {...messages.noGeometry} />
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
                          <FormattedMessage {...messages.noLines} />
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
                                {line.text || (
                                  <em style={{ color: '#888' }}>
                                    <FormattedMessage {...messages.emptyLine} />
                                  </em>
                                )}
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
                            <FormattedMessage {...messages.signatures} values={{ count: signatures.length }} />
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
                                  <em>
                                    <FormattedMessage {...messages.signatureRegion} values={{ index: index + 1 }} />
                                  </em>
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
                        <FormattedMessage {...messages.confidenceLegend} /> <Badge color="green">≥ {CONFIDENCE_HIGH}</Badge>{' '}
                        <Badge color="severity-medium">≥ {CONFIDENCE_MEDIUM}</Badge>{' '}
                        <Badge color="red">
                          <FormattedMessage {...messages.below} />
                        </Badge>
                      </Box>
                    )}
                  </>
                ) : (
                  <>
                    <Box fontSize="body-s" color="text-label" margin={{ bottom: 'xxxs' }}>
                      {markdownSubMode === 'raw' ? (
                        <FormattedMessage {...(isReadOnly ? messages.rawReadOnly : messages.rawEditable)} />
                      ) : (
                        <FormattedMessage {...messages.renderedReadOnly} />
                      )}
                    </Box>
                    <Box fontSize="body-s" color="text-body-secondary" margin={{ bottom: 'xxs' }}>
                      <FormattedMessage {...messages.markdownNote} />
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
        header={intl.formatMessage(unsavedMessages.title)}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => setShowCloseWarning(false)}>
                <FormattedMessage {...unsavedMessages.continueEditing} />
              </Button>
              <Button variant="primary" onClick={handleForceClose}>
                <FormattedMessage {...unsavedMessages.discard} />
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <FormattedMessage {...unsavedMessages.body} />
      </Modal>
    </>
  );
};

export default PageTextEditorModal;
