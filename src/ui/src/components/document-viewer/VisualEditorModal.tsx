// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/* eslint-disable prettier/prettier */
 

import React, { useState, useEffect, useRef, useMemo } from 'react';
import {
  Modal,
  Box,
  SpaceBetween,
  Container,
  Header,
  Spinner,
  Button,
  Toggle,
  Alert,
  Tabs,
} from '@cloudscape-design/components';
import { generateClient } from '../../api/client-shim';
import { ConsoleLogger } from 'aws-amplify/utils';
import useAppContext from '../../contexts/app';
import useSettingsContext from '../../contexts/settings';
import useUserRole from '../../hooks/use-user-role';
import { getFileContents, uploadDocument, completeSectionReview } from '../../graphql/generated';
import FormFieldRenderer from './FormFieldRenderer';
import JSONEditorTab from './JSONEditorTab';
import type { BoxProps } from '@cloudscape-design/components';
import PageImageViewer from '../common/PageImageViewer';
import type { PageImageViewerHandle } from '../common/PageImageViewer';
import EditHistoryTab from './EditHistoryTab';
import { SectionClassEvaluation } from '../common/ClassMismatchIndicator';
import { extractClassificationIndex } from '../common/classification-comparison-utils';
import ProcessingReportTab from './ProcessingReportTab';

const client = generateClient();

const logger = new ConsoleLogger('VisualEditorModal');

// Extended Box props to allow native HTML attributes that Cloudscape passes through at runtime
type ExtendedBoxProps = BoxProps & React.HTMLAttributes<HTMLDivElement>;
const ExtBox = Box as React.ComponentType<ExtendedBoxProps>;



interface VisualEditorModalProps {
  visible: boolean;
  onDismiss: () => void;
  jsonData: Record<string, unknown> | null;
  onChange: ((jsonString: string) => void) | null;
  isReadOnly: boolean;
  sectionData: Record<string, unknown> | null;
  allSections?: Array<{ Id: string; Class: string; PageIds: number[]; OutputJSONUri?: string }>;
  currentSectionIndex?: number;
  onNavigateToSection?: ((index: number) => void) | null;
}

const VisualEditorModal = ({
  visible,
  onDismiss,
  jsonData,
  onChange,
  isReadOnly,
  sectionData,
  // Section navigation props
  allSections = [],
  currentSectionIndex = 0,
  onNavigateToSection,
}: VisualEditorModalProps) => {
  const { user } = useAppContext();
  const { settings } = useSettingsContext();
  // Role flags from the Cognito token (reliable regardless of how the modal was
  // opened). Reviewers (Admin/Reviewer) cannot call uploadDocument (Admin/Author
  // only), so their edits must be saved via completeSectionReview — see handleSaveChanges.
  const { canWrite, canReview } = useUserRole();
  const [currentPage, setCurrentPage] = useState<string | number | null>(null);
  const [activeFieldGeometry, setActiveFieldGeometry] = useState<Record<string, unknown> | null>(null);
  const [localJsonData, setLocalJsonData] = useState<Record<string, unknown> | null>(jsonData);
  // Evaluation comparison state
  const [baselineData, setBaselineData] = useState<Record<string, unknown> | null>(null);
  const [evaluationResults, setEvaluationResults] = useState<Record<string, unknown> | null>(null);
  const [loadingEvaluation, setLoadingEvaluation] = useState(false);
  const [showEvaluation, setShowEvaluation] = useState(false);
  // Collapse/expand state - stores path keys of collapsed items
  const [collapsedPaths, setCollapsedPaths] = useState<Set<string>>(new Set());
  // Filter mode state
  const [filterMode, setFilterMode] = useState('none'); // 'none', 'confidence-alerts', 'eval-mismatches'

  // Change tracking state for saving edits
  const [predictionChanges, setPredictionChanges] = useState<Map<string, { original: unknown; current: unknown }>>(new Map());
  const [baselineChanges, setBaselineChanges] = useState<Map<string, { original: unknown; current: unknown }>>(new Map());
  const [originalPredictionData, setOriginalPredictionData] = useState<Record<string, unknown> | null>(null);
  const [originalBaselineData, setOriginalBaselineData] = useState<Record<string, unknown> | null>(null);
  const [localBaselineData, setLocalBaselineData] = useState<Record<string, unknown> | null>(null);
  const [isSaving, setIsSaving] = useState(false);
  const [_saveError, setSaveError] = useState<string | null>(null);
  const [_showUnsavedChangesModal, setShowUnsavedChangesModal] = useState(false);
  const [_pendingDismiss, setPendingDismiss] = useState(false);
  // Tab navigation state
  const [activeTabId, setActiveTabId] = useState('visual');


  // Toggle collapse handler
  const handleToggleCollapse = (pathKey: string) => {
    setCollapsedPaths((prev) => {
      const newSet = new Set(prev);
      if (newSet.has(pathKey)) {
        newSet.delete(pathKey);
      } else {
        newSet.add(pathKey);
      }
      return newSet;
    });
  };

  // Expand all handler
  const handleExpandAll = () => {
    setCollapsedPaths(new Set());
  };

  // Collapse all handler - recursively collapse ALL arrays and objects at every level
  const handleCollapseAll = () => {
    // Get inferenceResult from jsonData - same logic used later in component
    const result = localJsonData?.inference_result || localJsonData?.inferenceResult || localJsonData;
    const allPaths = new Set<string>();

    // Recursive function to add all collapsible paths
    const addCollapsiblePaths = (obj: unknown, currentPath: string) => {
      if (obj && typeof obj === 'object') {
        if (Array.isArray(obj)) {
          // This is an array - add its path and recurse into items
          if (currentPath) {
            allPaths.add(currentPath);
          }
          obj.forEach((item, index) => {
            addCollapsiblePaths(item, `${currentPath}.[${index}]`);
          });
        } else {
          // This is an object - add its path and recurse into properties
          if (currentPath) {
            allPaths.add(currentPath);
          }
          Object.entries(obj).forEach(([key, val]) => {
            if (Array.isArray(val) || (typeof val === 'object' && val !== null)) {
              const newPath = currentPath ? `${currentPath}.${key}` : key;
              addCollapsiblePaths(val, newPath);
            }
          });
        }
      }
    };

    if (result && typeof result === 'object') {
      Object.entries(result).forEach(([key, val]) => {
        if (Array.isArray(val) || (typeof val === 'object' && val !== null)) {
          addCollapsiblePaths(val, `Document Data.${key}`);
        }
      });
    }

    setCollapsedPaths(allPaths);
  };
  const debounceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const viewerRef = useRef<PageImageViewerHandle | null>(null);

  // Check if baseline is available - check multiple possible paths
  const sectionDocItem = sectionData?.documentItem as Record<string, unknown> | undefined;
  const evaluationStatus = sectionDocItem?.evaluationStatus || sectionDocItem?.EvaluationStatus;
  const isBaselineAvailable = evaluationStatus === 'BASELINE_AVAILABLE' || evaluationStatus === 'COMPLETED';

  // Per-page ground-truth classes for this document, from the same results.json
  // the field-level comparison already uses — so the class comparison costs no
  // extra request.
  const classificationIndex = useMemo(() => extractClassificationIndex(evaluationResults), [evaluationResults]);
  const sectionPageNumbers = useMemo(
    () => ((sectionData?.PageIds as Array<string | number> | undefined) ?? []).map((pageId) => Number(pageId)).filter((n) => !Number.isNaN(n)),
    [sectionData?.PageIds],
  );

  // Construct baseline URI from output URI
  const constructBaselineUri = (outputUri: string | undefined) => {
    if (!outputUri) {
      logger.debug('constructBaselineUri: No output URI provided');
      return null;
    }
    const outputBucketName = (settings as Record<string, unknown>)?.OutputBucket as string | undefined;
    const baselineBucketName = (settings as Record<string, unknown>)?.EvaluationBaselineBucket as string | undefined;

    logger.debug('constructBaselineUri:', { outputUri, outputBucketName, baselineBucketName });

    if (!outputBucketName || !baselineBucketName) {
      logger.warn('Bucket names not available in settings:', { outputBucketName, baselineBucketName });
      return null;
    }
    const match = outputUri.match(/^s3:\/\/([^/]+)\/(.+)$/);
    if (!match) {
      logger.warn('Invalid S3 URI format:', outputUri);
      return null;
    }
    const [, , objectKey] = match;
    const baselineUri = `s3://${baselineBucketName}/${objectKey}`;
    logger.debug('Constructed baseline URI:', baselineUri);
    return baselineUri;
  };

  // Construct evaluation results URI from input key
  const constructEvaluationResultsUri = (inputKey: string | undefined, outputBucket: string | undefined) => {
    if (!inputKey || !outputBucket) {
      return null;
    }
    return `s3://${outputBucket}/${inputKey}/evaluation/results.json`;
  };

  // Load baseline data and evaluation results when modal opens
  useEffect(() => {
    logger.debug('Evaluation load effect:', {
      visible,
      isBaselineAvailable,
      evaluationStatus,
      hasBaselineData: !!baselineData,
      hasEvaluationResults: !!evaluationResults,
      outputUri: sectionData?.OutputJSONUri,
    });

    if (!visible || !isBaselineAvailable) return;
    if (baselineData && evaluationResults) return; // Already loaded

    const loadEvaluationData = async () => {
      const outputUri = sectionData?.OutputJSONUri as string | undefined;
      // inputKey can be objectKey or inputKey depending on context
      const inputKey = sectionDocItem?.objectKey || sectionDocItem?.inputKey || sectionDocItem?.InputKey;
      const outputBucket =
        sectionDocItem?.outputBucket ||
        sectionDocItem?.OutputBucket ||
        (settings as Record<string, unknown>)?.OutputBucket;

      setLoadingEvaluation(true);
      try {
        // Load baseline data
        const baselineUri = constructBaselineUri(outputUri);
        if (baselineUri && !baselineData) {
          try {
            const baselineResponse = await client.graphql({
              query: getFileContents,
              variables: { s3Uri: baselineUri },
            });
            const baselineResult = baselineResponse.data.getFileContents;
            if (baselineResult && !baselineResult.isBinary && baselineResult.content) {
              const parsed = JSON.parse(baselineResult.content);
              setBaselineData(parsed);
              logger.info('Baseline data loaded successfully');
            }
          } catch (error) {
            logger.warn('Failed to load baseline data:', error instanceof Error ? error.message : error);
          }
        }

        // Load evaluation results
        const evalResultsUri = constructEvaluationResultsUri(inputKey as string | undefined, outputBucket as string | undefined);
        if (evalResultsUri && !evaluationResults) {
          try {
            const evalResponse = await client.graphql({
              query: getFileContents,
              variables: { s3Uri: evalResultsUri },
            });
            const evalResult = (evalResponse as { data: { getFileContents: { isBinary: boolean; content: string } } }).data.getFileContents;
            if (!evalResult.isBinary && evalResult.content) {
              const parsed = JSON.parse(evalResult.content);
              setEvaluationResults(parsed);
              logger.info('Evaluation results loaded successfully:', parsed);
            }
          } catch (error) {
            logger.warn('Failed to load evaluation results:', (error as Error).message);
          }
        }
      } finally {
        setLoadingEvaluation(false);
      }
    };

    loadEvaluationData();
  }, [visible, isBaselineAvailable, sectionData?.OutputJSONUri, sectionDocItem?.inputKey, settings]);

  // Reset evaluation state when modal closes
  useEffect(() => {
    if (!visible) {
      setBaselineData(null);
      setEvaluationResults(null);
      setShowEvaluation(false);
      setLoadingEvaluation(false);
    }
  }, [visible]);

  // Check if section needs review (either low confidence or HITL triggered)
  const _needsReview =
    (sectionData?.confidenceAlertCount as number) > 0 || (sectionDocItem?.hitlTriggered && !sectionDocItem?.hitlCompleted);

  // Check if this specific section is already completed
  const _isSectionCompleted = sectionData?.isSectionCompleted || false;

  // Check if user is reviewer only (not admin)
  const _isReviewerOnly = sectionData?.isReviewerOnly || false;

  // Sync local data with props and store original for change tracking
  useEffect(() => {
    setLocalJsonData(jsonData);
    // Store original prediction data for change tracking (deep copy)
    // Only set once on initial load (when originalPredictionData is null)
    if (jsonData) {
      logger.info('🔧 useEffect - setting localJsonData', { hasJsonData: !!jsonData, hasOriginal: !!originalPredictionData });
      if (!originalPredictionData) {
        const originalCopy = JSON.parse(JSON.stringify(jsonData));
        setOriginalPredictionData(originalCopy);
        logger.info('🔧 Set originalPredictionData:', { keys: Object.keys(originalCopy || {}) });
      }
    }
  }, [jsonData]);

  // Initialize localBaselineData when baselineData loads
  useEffect(() => {
    if (baselineData && !localBaselineData) {
      setLocalBaselineData(JSON.parse(JSON.stringify(baselineData)));
      setOriginalBaselineData(JSON.parse(JSON.stringify(baselineData)));
    }
  }, [baselineData]);

  // Calculate change counts for display
  const predictionChangeCount = predictionChanges.size;
  const baselineChangeCount = baselineChanges.size;
  const hasUnsavedChanges = predictionChangeCount > 0 || baselineChangeCount > 0;

  // Track a field change
  const trackPredictionChange = (fieldPath: string, originalValue: unknown, newValue: unknown) => {
    logger.info('📝 TRACK PREDICTION CHANGE:', { fieldPath, originalValue, newValue });
    setPredictionChanges((prev) => {
      const newMap = new Map(prev);
      if (JSON.stringify(originalValue) === JSON.stringify(newValue)) {
        // Value reverted to original, remove from changes
        newMap.delete(fieldPath);
        logger.info('📝 Removed from changes (reverted):', fieldPath);
      } else {
        newMap.set(fieldPath, { original: originalValue, current: newValue });
        logger.info('📝 Added to changes:', { fieldPath, mapSize: newMap.size, allKeys: [...newMap.keys()] });
      }
      return newMap;
    });
  };

  const trackBaselineChange = (fieldPath: string, originalValue: unknown, newValue: unknown) => {
    setBaselineChanges((prev) => {
      const newMap = new Map(prev);
      if (JSON.stringify(originalValue) === JSON.stringify(newValue)) {
        newMap.delete(fieldPath);
      } else {
        newMap.set(fieldPath, { original: originalValue, current: newValue });
      }
      return newMap;
    });
  };

  // Discard all changes
  const handleDiscardAllChanges = () => {
    if (originalPredictionData) {
      setLocalJsonData(JSON.parse(JSON.stringify(originalPredictionData)));
    }
    if (originalBaselineData) {
      setLocalBaselineData(JSON.parse(JSON.stringify(originalBaselineData)));
    }
    setPredictionChanges(new Map());
    setBaselineChanges(new Map());
  };

  // Save changes to S3
  const handleSaveChanges = async () => {
    setIsSaving(true);
    setSaveError(null);

    try {
      // Extract the actual file path from OutputJSONUri to ensure we save to the exact same location
      const outputUri = sectionData?.OutputJSONUri as string | undefined;
      logger.info('💾 handleSaveChanges - outputUri:', outputUri);

      if (!outputUri) {
        throw new Error('Cannot determine output URI for saving');
      }

      // Parse the S3 URI to get bucket and key
      const outputUriMatch = outputUri.match(/^s3:\/\/([^/]+)\/(.+)$/);
      if (!outputUriMatch) {
        throw new Error(`Invalid S3 URI format: ${outputUri}`);
      }
      const [, outputBucketFromUri, outputFileKey] = outputUriMatch;
      logger.info('💾 Parsed output URI:', { bucket: outputBucketFromUri, key: outputFileKey });

      // Reviewers are NOT authorized to call the `uploadDocument` mutation
      // (restricted to Admin/Author by RBAC). They persist HITL edits through
      // the reviewer-permitted `completeSectionReview` mutation instead.
      // Derive this from the Cognito token (canReview && !canWrite = reviewer,
      // not Admin/Author) rather than relying solely on the `isReviewerOnly`
      // prop, which some SectionsPanel FileViewer entry points hardcode to false.
      const isReviewerOnly = (canReview && !canWrite) || Boolean(sectionData?.isReviewerOnly);

      const results: {
        predictions: { success: boolean; changedFields: string[] } | null;
        baseline: { success: boolean; changedFields: string[] } | null;
      } = { predictions: null, baseline: null };

      // Build combined edit entry for both files
      const username = user?.username || 'unknown';
      const timestamp = new Date().toISOString();

      // Build prediction diffs
      const predictionDiffs: Record<string, { originalValue: unknown; newValue: unknown }> = {};
      predictionChanges.forEach((change: { original: unknown; current: unknown }, fieldPath: string) => {
        predictionDiffs[fieldPath] = {
          originalValue: change.original,
          newValue: change.current,
        };
      });

      // Build baseline diffs
      const baselineDiffs: Record<string, { originalValue: unknown; newValue: unknown }> = {};
      baselineChanges.forEach((change: { original: unknown; current: unknown }, fieldPath: string) => {
        baselineDiffs[fieldPath] = {
          originalValue: change.original,
          newValue: change.current,
        };
      });

      // Combined edit entry that will be saved to both files
      const editEntry = {
        timestamp,
        editedBy: username,
        predictionEdits: {
          changedFields: [...predictionChanges.keys()],
          changeCount: predictionChangeCount,
          diffs: predictionDiffs,
        },
        baselineEdits: {
          changedFields: [...baselineChanges.keys()],
          changeCount: baselineChangeCount,
          diffs: baselineDiffs,
        },
      };

      // Save predictions if changed
      if (predictionChangeCount > 0) {
        logger.info('💾 Saving prediction changes...', { count: predictionChangeCount });

        // Add edit history metadata with combined changes
        const dataToSave: Record<string, unknown> = { ...localJsonData };
        const editHistory = (dataToSave._editHistory as unknown[]) || [];
        editHistory.push(editEntry);
        dataToSave._editHistory = editHistory;

        // Reviewers are NOT authorized to call the `uploadDocument` mutation
        // (restricted to Admin/Author by RBAC). They persist their HITL edits
        // through the reviewer-permitted `completeSectionReview` mutation, which
        // writes the full JSON to the section's output URI server-side (no
        // presigned URL needed) and records the section as reviewed. The
        // Admin/Author presigned-upload path is preserved below.
        if (isReviewerOnly) {
          const reviewObjectKey = (sectionDocItem?.ObjectKey ||
            sectionDocItem?.objectKey ||
            sectionDocItem?.key ||
            sectionDocItem?.Key ||
            sectionDocItem?.id ||
            sectionDocItem?.Id) as string | undefined;
          const reviewSectionId = (sectionData?.Id ?? sectionData?.SectionId) as string | number | undefined;

          if (!reviewObjectKey) {
            throw new Error('Cannot determine document object key for saving reviewer changes');
          }
          if (reviewSectionId === undefined || reviewSectionId === null) {
            throw new Error('Cannot determine section id for saving reviewer changes');
          }

          logger.info('💾 Saving prediction changes via completeSectionReview (reviewer path):', {
            objectKey: reviewObjectKey,
            sectionId: reviewSectionId,
          });

          const reviewResponse = await client.graphql({
            query: completeSectionReview,
            variables: {
              objectKey: reviewObjectKey,
              sectionId: String(reviewSectionId),
              // editedData is AWSJSON — send the full JSON structure as a string.
              // The resolver saves it verbatim to the section's output URI.
              editedData: JSON.stringify(dataToSave),
            },
          });

          if (!reviewResponse?.data?.completeSectionReview) {
            throw new Error('completeSectionReview returned no data');
          }
          logger.info('✅ Predictions saved via completeSectionReview');
        } else {
          // Admin/Author path: request a presigned upload URL and write to S3 directly.
          // Use the exact same path from the original output URI
          logger.info('💾 Prediction file path:', outputFileKey);

          // Extract prefix (directory) and filename for uploadDocument
          const predictionPrefix = outputFileKey.substring(0, outputFileKey.lastIndexOf('/'));
          const predictionFilename = outputFileKey.split('/').pop() ?? outputFileKey;

          const predictionUploadResponse = await client.graphql({
            query: uploadDocument,
            variables: {
              fileName: predictionFilename,
              prefix: predictionPrefix,
              contentType: 'application/json',
              bucket: outputBucketFromUri,
            },
          });

          const predUploadData = predictionUploadResponse.data.uploadDocument;
          const predictionPresignedUrl = predUploadData.presignedUrl;
          const predictionUsePost = predUploadData.usePostMethod?.toLowerCase() === 'true';

          // Upload the JSON data
          const predictionContent = JSON.stringify(dataToSave, null, 2);

          if (predictionUsePost) {
            // POST method using presigned POST data (contains url + fields)
            const presignedPostData = JSON.parse(predictionPresignedUrl);
            const formData = new FormData();

            // Add all required fields from presigned POST data
            Object.entries(presignedPostData.fields).forEach(([key, fieldValue]) => {
              formData.append(key, fieldValue as string);
            });

            // Append the file content as last field (required for S3 presigned POST)
            const blob = new Blob([predictionContent], { type: 'application/json' });
            formData.append('file', blob, predictionFilename);

            logger.info('📤 Uploading predictions via presigned POST to:', presignedPostData.url);
            const uploadResponse = await fetch(presignedPostData.url, {
              method: 'POST',
              body: formData,
            });

            if (!uploadResponse.ok) {
              const errorText = await uploadResponse.text().catch(() => 'Could not read error response');
              throw new Error(`Prediction upload failed: ${errorText}`);
            }
          } else {
            // PUT method (standard presigned URL)
            await fetch(predictionPresignedUrl, {
              method: 'PUT',
              headers: { 'Content-Type': 'application/json' },
              body: predictionContent,
            });
          }

          logger.info('✅ Predictions saved successfully');
        }

        results.predictions = { success: true, changedFields: [...predictionChanges.keys()] };
      }

      // Save baseline if changed.
      // Baseline edits are an evaluation/Author feature and require the
      // `uploadDocument` mutation (Admin/Author only). Reviewers cannot write to
      // the EvaluationBaselineBucket, so skip the baseline save for reviewers to
      // avoid an Unauthorized error (they should not be editing baselines).
      if (baselineChangeCount > 0 && localBaselineData && !isReviewerOnly) {
        logger.info('💾 Saving baseline changes...', { count: baselineChangeCount });

        // Add edit history metadata with combined changes (same as predictions)
        const baselineToSave: Record<string, unknown> = { ...localBaselineData };
        const baselineEditHistory = (baselineToSave._editHistory as unknown[]) || [];
        baselineEditHistory.push(editEntry);
        baselineToSave._editHistory = baselineEditHistory;

        // Get presigned URL for baseline (EvaluationBaselineBucket)
        // Baseline uses the same path as predictions, just in a different bucket
        const baselineBucket = (settings as Record<string, unknown>)?.EvaluationBaselineBucket as string | undefined;
        if (!baselineBucket) {
          throw new Error('EvaluationBaselineBucket not configured in settings');
        }

        // Use the same file path as predictions (outputFileKey) since baseline mirrors the structure
        logger.info('💾 Baseline file path:', outputFileKey);

        // Extract prefix (directory) and filename for uploadDocument
        const baselinePrefix = outputFileKey.substring(0, outputFileKey.lastIndexOf('/'));
        const baselineFilename = outputFileKey.split('/').pop() ?? outputFileKey;

        const baselineUploadResponse = await client.graphql({
          query: uploadDocument,
          variables: {
            fileName: baselineFilename,
            prefix: baselinePrefix,
            contentType: 'application/json',
            bucket: baselineBucket,
          },
        });

        const baseUploadData = baselineUploadResponse.data.uploadDocument;
        const baselinePresignedUrl = baseUploadData.presignedUrl;
        const baselineUsePost = baseUploadData.usePostMethod?.toLowerCase() === 'true';

        // Upload the JSON data
        const baselineContent = JSON.stringify(baselineToSave, null, 2);

        if (baselineUsePost) {
          // POST method using presigned POST data (contains url + fields)
          const presignedPostData = JSON.parse(baselinePresignedUrl);
          const formData = new FormData();

          // Add all required fields from presigned POST data
          Object.entries(presignedPostData.fields).forEach(([key, fieldValue]) => {
            formData.append(key, fieldValue as string);
          });

          // Append the file content as last field (required for S3 presigned POST)
          const blob = new Blob([baselineContent], { type: 'application/json' });
          formData.append('file', blob, baselineFilename as string);

          logger.info('📤 Uploading baseline via presigned POST to:', presignedPostData.url);
          const uploadResponse = await fetch(presignedPostData.url, {
            method: 'POST',
            body: formData,
          });

          if (!uploadResponse.ok) {
            const errorText = await uploadResponse.text().catch(() => 'Could not read error response');
            throw new Error(`Baseline upload failed: ${errorText}`);
          }
        } else {
          // PUT method (standard presigned URL)
          await fetch(baselinePresignedUrl, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: baselineContent,
          });
        }

        results.baseline = { success: true, changedFields: [...baselineChanges.keys()] };
        logger.info('✅ Baseline saved successfully');
      }

      // Success! Reset change tracking and update originals for what was saved.
      setOriginalPredictionData(JSON.parse(JSON.stringify(localJsonData)));
      setPredictionChanges(new Map());

      // Baseline edits are only persisted on the Admin/Author path. For reviewers
      // the baseline save is skipped, so keep their baseline changes (and the
      // "unsaved" indicator) rather than silently discarding them and falsely
      // reporting success.
      const baselineSkippedForReviewer = isReviewerOnly && baselineChangeCount > 0;
      if (!baselineSkippedForReviewer) {
        if (localBaselineData) {
          setOriginalBaselineData(JSON.parse(JSON.stringify(localBaselineData)));
        }
        setBaselineChanges(new Map());
      }

      // Show an accurate message about what was (and wasn't) saved.
      const savedItems = [];
      if (results.predictions) savedItems.push(`${results.predictions.changedFields.length} prediction field(s)`);
      if (results.baseline) savedItems.push(`${results.baseline.changedFields.length} baseline field(s)`);

      let saveMessage = savedItems.length > 0 ? `✅ Successfully saved:\n${savedItems.join('\n')}` : 'ℹ️ No changes were saved.';
      if (baselineSkippedForReviewer) {
        saveMessage += `\n\n⚠️ ${baselineChangeCount} baseline edit(s) were NOT saved — editing evaluation baselines requires an Author or Admin role.`;
      }
      alert(saveMessage);
    } catch (error) {
      logger.error('❌ Error saving changes:', error);
      setSaveError((error as Error).message || 'Failed to save changes');
      alert(`❌ Error saving changes:\n${(error as Error).message}`);
    } finally {
      setIsSaving(false);
    }
  };

  // Debounced parent onChange function with non-blocking execution
  const debouncedParentOnChange = (jsonString: string) => {
    // Clear existing timer
    if (debounceTimerRef.current) {
      clearTimeout(debounceTimerRef.current);
    }

    // Set new timer - call parent after 1 second of no typing
    debounceTimerRef.current = setTimeout(() => {
      if (onChange) {
        const parentCallStart = performance.now();
        logger.debug('🚀 DEBOUNCED PARENT onChange - Calling parent onChange...');

        // Use requestIdleCallback to ensure parent onChange doesn't block UI
        // If not available, fall back to setTimeout with 0 delay
        const executeParentChange = () => {
          try {
            onChange(jsonString);
            const parentCallEnd = performance.now();
            logger.debug('🏁 DEBOUNCED PARENT onChange - Parent onChange completed:', {
              duration: `${(parentCallEnd - parentCallStart).toFixed(2)}ms`,
            });
          } catch (error) {
            logger.error('Error in parent onChange:', error);
          }
        };

        if (window.requestIdleCallback) {
          // Use requestIdleCallback to run during browser idle time
          window.requestIdleCallback(executeParentChange, { timeout: 5000 });
        } else {
          // Fallback: use setTimeout to yield control back to browser
          setTimeout(executeParentChange, 0);
        }
      }
    }, 1000); // 1 second debounce
  };

  // Cleanup debounce timer on unmount
  useEffect(() => {
    return () => {
      if (debounceTimerRef.current) {
        clearTimeout(debounceTimerRef.current);
      }
    };
  }, []);

  // Extract inference results and page IDs from local data for immediate UI updates
  const inferenceResult = localJsonData?.inference_result || localJsonData?.inferenceResult || localJsonData;
  // Both feed PageImageViewer's image-load effect, so a fresh array identity on
  // every render would re-run the presigning loop continuously. `sectionData` is
  // rebuilt inline by its parent each render, hence keying on the PageIds content
  // rather than the object identity.
  const pageIdsKey = JSON.stringify(sectionData?.PageIds ?? []);
  const pageIds = useMemo(
    () => (sectionData?.PageIds as Array<string | number>) || [],
    [pageIdsKey],
  );
  const documentPagesForViewer = useMemo(
    () =>
      pageIds.map((pageId: string | number) => ({
        Id: String(pageId),
        // Pages hang off the DOCUMENT, not the section — the section only names
        // its page ids.
        ImageUri: (sectionDocItem?.pages as Record<string, unknown>[] | undefined)?.find((p) => String(p.Id) === String(pageId))
          ?.ImageUri as string | undefined,
      })),
    [pageIds, sectionDocItem?.pages],
  );
  const pageIdStrings = useMemo(() => pageIds.map(String), [pageIds]);

  // Reset per-document view state when the modal closes. Image loading belongs to
  // PageImageViewer, which presigns and caches per page id.
  useEffect(() => {
    if (!visible) {
      setCurrentPage(null);
      setActiveFieldGeometry(null);
    }
  }, [visible]);

  // Handle field focus - update active field geometry and switch to the correct page
  // This function is intentionally kept lightweight and independent of debounced operations
  const handleFieldFocus = (geometry: Record<string, unknown> | null) => {
    const focusStart = performance.now();
    logger.debug('VisualEditorModal - handleFieldFocus START:', { timestamp: focusStart });

    // Use setTimeout to make this completely asynchronous and non-blocking
    setTimeout(() => {
      if (geometry) {
        setActiveFieldGeometry(geometry);

        // If geometry has a page field, switch to that page
        if (geometry.page !== undefined && pageIds.length > 0) {
          const pageIndex = (geometry.page as number) - 1;
          if (pageIndex >= 0 && pageIndex < pageIds.length) {
            const targetPageId = pageIds[pageIndex];
            setCurrentPage(targetPageId);
          }
        }
      } else {
        setActiveFieldGeometry(null);
      }

      const focusEnd = performance.now();
      logger.debug('VisualEditorModal - handleFieldFocus END:', {
        duration: `${(focusEnd - focusStart).toFixed(2)}ms`,
      });
    }, 0);
  };

  /**
   * Double-click zooms; single-click only highlights. Zooming is requested
   * explicitly on the viewer rather than driven by the active geometry, so
   * clicking through fields outlines them in place instead of moving the page.
   */
  const handleFieldDoubleClick = (geometry: Record<string, unknown> | null) => {
    if (!geometry) return;
    if (geometry.page !== undefined && pageIds.length > 0) {
      const pageIndex = (geometry.page as number) - 1;
      if (pageIndex >= 0 && pageIndex < pageIds.length && pageIds[pageIndex] !== currentPage) {
        setCurrentPage(pageIds[pageIndex]);
      }
    }
    setActiveFieldGeometry(geometry);
    // After the page switch has rendered, so the viewer measures the right image.
    requestAnimationFrame(() => viewerRef.current?.zoomToField(geometry as never));
  };

  // Handle unsaved changes modal actions
  const _handleDiscardAndClose = () => {
    setShowUnsavedChangesModal(false);
    setPendingDismiss(false);
    handleDiscardAllChanges();
    onDismiss();
  };

  const _handleReturnToEditor = () => {
    setShowUnsavedChangesModal(false);
    setPendingDismiss(false);
  };

  return (
    <Modal
      onDismiss={() => {
        if (hasUnsavedChanges) {
          const confirmDiscard = window.confirm(
            `You have unsaved changes:\n` +
              `• ${predictionChangeCount} prediction edit(s)\n` +
              `• ${baselineChangeCount} baseline edit(s)\n\n` +
              `Discard changes and close?`,
          );
          if (confirmDiscard) {
            handleDiscardAllChanges();
            onDismiss();
          }
        } else {
          onDismiss();
        }
      }}
      visible={visible}
      header="Visual Document Editor"
      size="max"
      footer={
        <ExtBox>
          <SpaceBetween direction="horizontal" size="xs" alignItems="center">
            {/* Left side - Section info */}
            <ExtBox>
              <SpaceBetween direction="horizontal" size="m">
                <ExtBox>
                  <strong>Section:</strong> {String(sectionData?.Id || sectionData?.SectionId || 'N/A')}
                </ExtBox>
                <ExtBox>
                  <strong>Type:</strong> {(localJsonData?.document_class as Record<string, unknown>)?.type as string || 'N/A'}
                </ExtBox>
              </SpaceBetween>
            </ExtBox>

            {/* Change indicator */}
            {!isReadOnly && hasUnsavedChanges && (
              <ExtBox color="text-status-warning">
                <SpaceBetween direction="horizontal" size="xs">
                  <span>📝</span>
                  <span>
                    Unsaved: {predictionChangeCount > 0 && `${predictionChangeCount} prediction`}
                    {predictionChangeCount > 0 && baselineChangeCount > 0 && ', '}
                    {baselineChangeCount > 0 && `${baselineChangeCount} baseline`}
                  </span>
                </SpaceBetween>
              </ExtBox>
            )}

            {/* Spacer */}
            <ExtBox style={{ flex: 1 }} />

            {/* Right side - buttons */}
            <SpaceBetween direction="horizontal" size="xs">
              {/* Discard button - only when there are changes */}
              {!isReadOnly && hasUnsavedChanges && (
                <Button variant="link" onClick={handleDiscardAllChanges}>
                  Discard All Changes
                </Button>
              )}

              {/* Save button - only when there are unsaved changes */}
              {!isReadOnly && hasUnsavedChanges && (
                <Button variant="primary" onClick={handleSaveChanges} loading={isSaving} disabled={isSaving}>
                  {isSaving ? 'Saving...' : 'Save All Changes'}
                </Button>
              )}

              {/* Section Navigation buttons */}
              {allSections.length > 1 && onNavigateToSection && (
                <>
                  <Button
                    iconName="angle-left"
                    variant="normal"
                    onClick={() => {
                      if (hasUnsavedChanges) {
                        alert('Please save or discard your changes before navigating to another section.');
                      } else if (currentSectionIndex > 0) {
                        onNavigateToSection(currentSectionIndex - 1);
                      }
                    }}
                    disabled={currentSectionIndex === 0 || hasUnsavedChanges}
                  >
                    Previous Section
                  </Button>
                  <Button
                    iconAlign="right"
                    iconName="angle-right"
                    variant="normal"
                    onClick={() => {
                      if (hasUnsavedChanges) {
                        alert('Please save or discard your changes before navigating to another section.');
                      } else if (currentSectionIndex < allSections.length - 1) {
                        onNavigateToSection(currentSectionIndex + 1);
                      }
                    }}
                    disabled={currentSectionIndex === allSections.length - 1 || hasUnsavedChanges}
                  >
                    Next Section
                  </Button>
                </>
              )}

              {/* Close button */}
              <Button
                variant={hasUnsavedChanges ? 'normal' : 'primary'}
                onClick={() => {
                  if (hasUnsavedChanges) {
                    const confirmDiscard = window.confirm(
                      `You have unsaved changes:\n` +
                        `• ${predictionChangeCount} prediction edit(s)\n` +
                        `• ${baselineChangeCount} baseline edit(s)\n\n` +
                        `Discard changes and close?`,
                    );
                    if (confirmDiscard) {
                      handleDiscardAllChanges();
                      onDismiss();
                    }
                  } else {
                    onDismiss();
                  }
                }}
              >
                Close
              </Button>
            </SpaceBetween>
          </SpaceBetween>
        </ExtBox>
      }
    >
      <Tabs
        activeTabId={activeTabId}
        onChange={({ detail }) => setActiveTabId(detail.activeTabId)}
        tabs={[
          {
            id: 'visual',
            label: 'Visual Editor',
            content: (
              <div
                style={{
                  display: 'flex',
                  flexDirection: 'row',
                  alignItems: 'flex-start',
                  gap: '20px',
                  height: 'calc(100vh - 300px)',
                  maxHeight: '700px',
                  minHeight: '450px',
                  width: '100%',
                }}
              >
                {/* Left side - Page images. Uses the shared PageImageViewer, which
                    owns page navigation, zoom, pan and bounding-box overlay, so this
                    editor and the annotation editor share one image pane. */}
                <div
                  style={{
                    width: '50%',
                    minWidth: '50%',
                    maxWidth: '50%',
                    height: '100%',
                    display: 'flex',
                    flexDirection: 'column',
                    flex: '0 0 50%',
                    overflow: 'hidden',
                  }}
                >
                  {/* A plain bordered box rather than Cloudscape Container: the
                      viewer needs a definite height to place its control strip, and
                      Container's auto-height content box does not pass one down —
                      a fixed height here overflowed the row on shorter viewports and
                      clipped the zoom controls. */}
                  <div
                    style={{
                      flex: 1,
                      minHeight: 0,
                      display: 'flex',
                      flexDirection: 'column',
                      border: '1px solid #c6c6cd',
                      borderRadius: '16px',
                      padding: '12px',
                      boxSizing: 'border-box',
                    }}
                  >
                    <Header variant="h3">Document Pages ({pageIds.length})</Header>
                    <div style={{ flex: 1, minHeight: 0 }}>
                      <PageImageViewer
                        pageIds={pageIdStrings}
                        documentPages={documentPagesForViewer}
                        activeFieldGeometry={activeFieldGeometry as never}
                        onPageChange={(pageId) => setCurrentPage(pageId)}
                        height="100%"
                        zoomHandle={viewerRef}
                      />
                    </div>
                  </div>
                </div>

                {/* Right side - Form fields - Independently scrollable */}
                <div
                  style={{
                    width: '50%',
                    minWidth: '50%',
                    maxWidth: '50%',
                    height: '100%',
                    display: 'flex',
                    flexDirection: 'column',
                    flex: '0 0 50%',
                    overflow: 'hidden',
                  }}
                >
                  <Container
                    header={
                      <Header
                        variant="h3"
                        actions={
                          <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                            {/* Expand/Collapse controls */}
                            <Button variant="normal" onClick={handleExpandAll}>
                              + Expand All
                            </Button>
                            <Button variant="normal" onClick={handleCollapseAll}>
                              − Collapse All
                            </Button>
                            {/* Filter dropdown */}
                            <select
                              value={filterMode}
                              onChange={(e) => setFilterMode(e.target.value)}
                              style={{
                                padding: '4px 8px',
                                borderRadius: '4px',
                                border: '1px solid #ccc',
                              }}
                            >
                              <option value="none">Show All</option>
                              <option value="confidence-alerts">Confidence Alerts Only</option>
                              <option value="eval-mismatches" disabled={!showEvaluation}>
                                Eval Mismatches Only
                              </option>
                            </select>
                            {/* Evaluation toggle */}
                            {(isBaselineAvailable || loadingEvaluation) && (
                              <>
                                {loadingEvaluation && <Spinner {...({ size: 'small' } as Record<string, unknown>)} />}
                                <Toggle
                                  checked={showEvaluation}
                                  onChange={({ detail }) => {
                                    setShowEvaluation(detail.checked);
                                    // Reset filter when turning off evaluation
                                    if (!detail.checked) {
                                      setFilterMode('none');
                                    }
                                  }}
                                  disabled={loadingEvaluation || !baselineData}
                                >
                                  Show Evaluation
                                </Toggle>
                              </>
                            )}
                          </SpaceBetween>
                        }
                      >
                        Document Data
                      </Header>
                    }
                  >
                    <div
                      style={{
                        flex: 1,
                        overflowY: 'auto',
                        overflowX: 'hidden',
                        padding: '16px',
                        boxSizing: 'border-box',
                        maxHeight: '550px',
                        minHeight: '350px',
                      }}
                    >
                      <ExtBox style={{ minHeight: 'fit-content' }}>
                        {/* Read-only is the default here; without this the greyed
                            inputs read as broken rather than as a mode. */}
                        {isReadOnly && (
                          <Box variant="small" color="text-body-secondary" padding={{ bottom: 'xs' }}>
                            Viewing extracted values. To change them, close this and use <b>Edit mode</b> on the section.
                          </Box>
                        )}
                        {/* The section's own class, compared first: every field
                            below was extracted against this class's schema, so a
                            class mismatch explains the field mismatches rather than
                            adding to them.

                            Deliberately NOT behind the Show Evaluation toggle. The
                            baseline is fetched whenever one exists — see the effect
                            above, which does not consult the toggle — so this line
                            costs nothing to render, and gating it alongside the
                            field-by-field comparison meant the headline signal was
                            invisible until you found a toggle that defaults to off.
                            A reviewer went looking for it here, in the obvious
                            place, and concluded it had not been built. The noisy
                            part — per-field scores and reasons — stays gated. */}
                        {baselineData && (
                          <SectionClassEvaluation
                            index={classificationIndex}
                            pageNumbers={sectionPageNumbers}
                            predictedClass={(localJsonData?.document_class as Record<string, unknown>)?.type as string | undefined}
                            baselineClass={(localBaselineData?.document_class as Record<string, unknown>)?.type as string | undefined}
                          />
                        )}
                        {showEvaluation && baselineData && (
                          <Alert type="info" header="Evaluation Comparison Mode">
                            Showing predicted values with evaluation baseline. Fields with mismatches are highlighted with evaluation scores
                            and reasons.
                          </Alert>
                        )}
                        {/* A baseline exists but the comparison is off, so say what
                            turning it on would add. Without this the toggle is the
                            only clue the capability exists, and it renders late —
                            it appears only once the evaluation status resolves. */}
                        {baselineData && !showEvaluation && (
                          <Box variant="small" color="text-body-secondary">
                            This document has an evaluation baseline. Turn on <b>Show Evaluation</b> to compare every field against it, not
                            just the class.
                          </Box>
                        )}
                        {inferenceResult ? (
                          <FormFieldRenderer
                            fieldKey="Document Data"
                            value={inferenceResult}
                            baselineValue={
                              showEvaluation
                                ? localBaselineData?.inference_result || localBaselineData?.inferenceResult || localBaselineData
                                : null
                            }
                            showComparison={showEvaluation}
                            evaluationResults={showEvaluation ? evaluationResults : null}
                            sectionId={sectionData?.Id || sectionData?.SectionId}
                            onBaselineChange={(newBaselineValue: Record<string, unknown>) => {
                              if (!isReadOnly && localBaselineData) {
                                const updatedBaseline = { ...localBaselineData };
                                if (updatedBaseline.inference_result) {
                                  updatedBaseline.inference_result = newBaselineValue;
                                } else if (updatedBaseline.inferenceResult) {
                                  updatedBaseline.inferenceResult = newBaselineValue;
                                } else {
                                  Object.keys(updatedBaseline).forEach((key) => {
                                    delete updatedBaseline[key];
                                  });
                                  Object.keys(newBaselineValue).forEach((key) => {
                                    updatedBaseline[key] = newBaselineValue[key];
                                  });
                                }
                                setLocalBaselineData(updatedBaseline);

                                // Track baseline changes at field level
                                if (originalBaselineData) {
                                  const originalBaselineResult =
                                    originalBaselineData.inference_result || originalBaselineData.inferenceResult || originalBaselineData;
                                  // Find what changed by comparing (excluding array indices from path)
                                  const findBaselineChanges = (original: unknown, current: unknown, pathParts: string[] = []) => {
                                    if (typeof current !== 'object' || current === null) {
                                      const pathStr = pathParts.join('.') || 'root';
                                      if (JSON.stringify(original) !== JSON.stringify(current)) {
                                        trackBaselineChange(pathStr, original, current);
                                      } else {
                                        // Value reverted to original
                                        setBaselineChanges((prev) => {
                                          const newMap = new Map(prev);
                                          newMap.delete(pathStr);
                                          return newMap;
                                        });
                                      }
                                      return;
                                    }
                                    if (Array.isArray(current)) {
                                      current.forEach((item, idx) => {
                                        const origItem = original && Array.isArray(original) ? original[idx] : undefined;
                                        // Don't add index to path - just recurse into item
                                        findBaselineChanges(origItem, item, pathParts);
                                      });
                                    } else {
                                      Object.keys(current).forEach((key) => {
                                        const origVal = original ? (original as Record<string, unknown>)[key] : undefined;
                                        findBaselineChanges(origVal, (current as Record<string, unknown>)[key], [...pathParts, key]);
                                      });
                                    }
                                  };
                                  findBaselineChanges(originalBaselineResult, newBaselineValue);
                                }
                              }
                            }}
                            onChange={(newValue: Record<string, unknown>) => {
                              if (!isReadOnly) {
                                // Update local state immediately for responsive UI
                                const updatedData = { ...localJsonData };
                                if (updatedData.inference_result) {
                                  updatedData.inference_result = newValue;
                                } else if (updatedData.inferenceResult) {
                                  updatedData.inferenceResult = newValue;
                                } else {
                                  // If there's no inference_result field, update the entire object
                                  Object.keys(updatedData).forEach((key) => {
                                    delete updatedData[key];
                                  });
                                  Object.keys(newValue).forEach((key) => {
                                    updatedData[key] = newValue[key];
                                  });
                                }

                                // Update local state immediately for responsive UI
                                setLocalJsonData(updatedData);
                                logger.debug('💨 LOCAL UPDATE - Updated local state immediately');

                                // Track prediction changes by comparing with original
                                logger.info('🔄 onChange called - checking if we have original data:', {
                                  hasOriginalPredictionData: !!originalPredictionData,
                                  isReadOnly,
                                });
                                if (originalPredictionData) {
                                  const originalInferenceResult =
                                    originalPredictionData.inference_result ||
                                    originalPredictionData.inferenceResult ||
                                    originalPredictionData;
                                  logger.info('🔄 Comparing values:', {
                                    newValueType: typeof newValue,
                                    originalType: typeof originalInferenceResult,
                                    areDifferent: JSON.stringify(newValue) !== JSON.stringify(originalInferenceResult),
                                  });
                                  // Simple comparison - if different, mark as changed
                                  if (JSON.stringify(newValue) !== JSON.stringify(originalInferenceResult)) {
                                    // Find what changed by comparing
                                    // Note: paths exclude array indices to match FormFieldRenderer's path calculation
                                    const findChanges = (original: unknown, current: unknown, pathParts: string[] = []) => {
                                      if (typeof current !== 'object' || current === null) {
                                        const pathStr = pathParts.join('.') || 'root';
                                        if (JSON.stringify(original) !== JSON.stringify(current)) {
                                          trackPredictionChange(pathStr, original, current);
                                        }
                                        return;
                                      }
                                      if (Array.isArray(current)) {
                                        current.forEach((item, idx) => {
                                          const origItem = original && Array.isArray(original) ? original[idx] : undefined;
                                          // Don't add index to path - just recurse into item
                                          findChanges(origItem, item, pathParts);
                                        });
                                      } else {
                                        Object.keys(current).forEach((key) => {
                                          const origVal = original ? (original as Record<string, unknown>)[key] : undefined;
                                          findChanges(origVal, (current as Record<string, unknown>)[key], [...pathParts, key]);
                                        });
                                      }
                                    };
                                    findChanges(originalInferenceResult, newValue);
                                  } else {
                                    // No changes - clear tracking
                                    setPredictionChanges(new Map());
                                  }
                                }

                                // Debounce expensive parent call
                                if (onChange) {
                                  const jsonStart = performance.now();
                                  logger.debug('🔄 DEBOUNCED - JSON stringify starting...');

                                  try {
                                    const jsonString = JSON.stringify(updatedData, null, 2);
                                    const jsonEnd = performance.now();
                                    logger.debug('✅ DEBOUNCED - JSON stringify completed:', {
                                      duration: `${(jsonEnd - jsonStart).toFixed(2)}ms`,
                                      jsonLength: jsonString.length,
                                    });

                                    // Call debounced parent onChange
                                    debouncedParentOnChange(jsonString);
                                  } catch (error) {
                                    logger.error('Error stringifying JSON:', error);
                                  }
                                }
                              }
                            }}
                            isReadOnly={isReadOnly}
                            onFieldFocus={handleFieldFocus}
                            onFieldDoubleClick={handleFieldDoubleClick}
                            path={[]}
                            explainabilityInfo={jsonData?.explainability_info}
                            mergedConfig={sectionData?.mergedConfig}
                            collapsedPaths={collapsedPaths}
                            onToggleCollapse={handleToggleCollapse}
                            filterMode={filterMode}
                            displayPath={[]}
                            predictionChanges={predictionChanges}
                            baselineChanges={baselineChanges}
                          />
                        ) : (
                          <ExtBox padding="xl" textAlign="center">
                            No data available
                          </ExtBox>
                        )}
                      </ExtBox>
                    </div>
                  </Container>
                </div>
              </div>
            ),
          },
          {
            id: 'json',
            label: 'JSON Editor',
            content: (
              <JSONEditorTab
                predictionData={localJsonData}
                baselineData={localBaselineData}
                isReadOnly={Boolean(isReadOnly)}
                showBaseline={showEvaluation}
                onShowBaselineChange={(checked) => setShowEvaluation(checked)}
                isBaselineAvailable={isBaselineAvailable}
                loadingEvaluation={loadingEvaluation}
                onPredictionChange={(newPredictionValue) => {
                  if (!isReadOnly) {
                    // Update local state - wrap back in the expected structure
                    const updatedData = { ...localJsonData };
                    if (updatedData.inference_result) {
                      updatedData.inference_result = newPredictionValue;
                    } else if (updatedData.inferenceResult) {
                      updatedData.inferenceResult = newPredictionValue;
                    } else {
                      // Update the root level
                      Object.keys(updatedData).forEach((key) => {
                        if (key !== '_editHistory' && key !== 'explainability_info') {
                          delete updatedData[key];
                        }
                      });
                      Object.keys(newPredictionValue).forEach((key) => {
                        updatedData[key] = newPredictionValue[key];
                      });
                    }
                    setLocalJsonData(updatedData);

                    // Track changes
                    if (originalPredictionData) {
                      const originalInferenceResult =
                        originalPredictionData.inference_result || originalPredictionData.inferenceResult || originalPredictionData;
                      if (JSON.stringify(newPredictionValue) !== JSON.stringify(originalInferenceResult)) {
                        trackPredictionChange('json-edit', originalInferenceResult, newPredictionValue);
                      }
                    }
                  }
                }}
                onBaselineChange={(newBaselineValue) => {
                  if (!isReadOnly && localBaselineData) {
                    const updatedBaseline = { ...localBaselineData };
                    if (updatedBaseline.inference_result) {
                      updatedBaseline.inference_result = newBaselineValue;
                    } else if (updatedBaseline.inferenceResult) {
                      updatedBaseline.inferenceResult = newBaselineValue;
                    } else {
                      Object.keys(updatedBaseline).forEach((key) => {
                        if (key !== '_editHistory' && key !== 'explainability_info') {
                          delete updatedBaseline[key];
                        }
                      });
                      Object.keys(newBaselineValue).forEach((key) => {
                        updatedBaseline[key] = newBaselineValue[key];
                      });
                    }
                    setLocalBaselineData(updatedBaseline);

                    // Track changes
                    if (originalBaselineData) {
                      const originalBaselineResult =
                        originalBaselineData.inference_result || originalBaselineData.inferenceResult || originalBaselineData;
                      if (JSON.stringify(newBaselineValue) !== JSON.stringify(originalBaselineResult)) {
                        trackBaselineChange('json-edit', originalBaselineResult, newBaselineValue);
                      }
                    }
                  }
                }}
              />
            ),
          },
          {
            id: 'history',
            // "Edit history", not "Revision History": these are per-field edits to
            // extracted values, matching the underlying `_editHistory` data — not
            // numbered immutable snapshots. "Revision" is reserved for
            // Configuration Profile revisions, which ARE that.
            label: 'Edit history',
            content: <EditHistoryTab predictionData={localJsonData} baselineData={localBaselineData} />,
          },
          {
            id: 'processing',
            label: 'Processing Report',
            content: (
              <ProcessingReportTab
                metadata={localJsonData?.metadata as Record<string, unknown> | undefined}
                processingReport={localJsonData?.processing_report as string | undefined}
                inferenceResult={
                  (localJsonData?.inference_result || localJsonData?.inferenceResult) as
                    | Record<string, unknown>
                    | undefined
                }
                processingIssues={
                  (localJsonData?.metadata as Record<string, unknown> | undefined)?.processing_issues as
                    | { stage?: string; severity?: string; code?: string; message?: string; rootCause?: string }[]
                    | undefined
                }
              />
            ),
          },
        ]}
      />
    </Modal>
  );
};

export default VisualEditorModal;
