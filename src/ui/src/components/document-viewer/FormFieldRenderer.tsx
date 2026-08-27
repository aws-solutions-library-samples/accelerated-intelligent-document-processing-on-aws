// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/* eslint-disable prettier/prettier */

// Extracted verbatim from VisualEditorModal.tsx so the recursive form renderer
// (and its comparison-key helpers) can be reused outside the modal — e.g. the
// Test Studio ground-truth editor. Pure presentational: no hooks, no S3/API
// calls; everything it needs arrives via props.

import React, { memo } from 'react';
import {
  Box,
  SpaceBetween,
  FormField,
  Input,
  Checkbox,
  Button,
} from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';
import { getFieldConfidenceInfo } from '../common/confidence-alerts-utils';
import type { BoxProps } from '@cloudscape-design/components';

// Extended Box props to allow native HTML attributes that Cloudscape passes through at runtime
type ExtendedBoxProps = BoxProps & React.HTMLAttributes<HTMLDivElement>;
const ExtBox = Box as React.ComponentType<ExtendedBoxProps>;

const logger = new ConsoleLogger('FormFieldRenderer');

// Build the canonical Stickler comparison key for a field from its render path.
// e.g. path=["LineItems", 0, "Description"] -> "LineItems[0].Description".
// NOTE: `path` already INCLUDES the current field key (children are rendered
// with path={[...path, key]} and array items with path={[...path, index]}), so
// the field key must NOT be appended again here. The "Document Data" synthetic
// root and undefined segments are ignored, and numeric segments become
// "[index]" subscripts on the preceding field name.
const buildComparisonKey = (path: (string | number)[]): string => {
  const segments = path.filter((p) => p !== undefined && p !== 'Document Data');
  let key = '';
  for (const seg of segments) {
    if (typeof seg === 'number') {
      key += `[${seg}]`;
    } else {
      key += key ? `.${seg}` : String(seg);
    }
  }
  return key;
};

// Find the nested field_comparison_details entry whose canonical key matches
// this field's render path. Returns the matching detail (with the per-field
// evaluation_method/weight the backend annotates) or null.
const findNestedComparisonDetail = (
  evaluationResults: Record<string, unknown> | null,
  sectionId: unknown,
  comparisonKey: string,
): Record<string, unknown> | null => {
  const sectionResults = evaluationResults?.section_results as Record<string, unknown>[] | undefined;
  if (!sectionResults || !comparisonKey) return null;
  let sectionResult = sectionResults.find((sr) => String(sr.section_id) === String(sectionId));
  if (!sectionResult && sectionResults.length === 1) {
    [sectionResult] = sectionResults;
  }
  const attributes = sectionResult?.attributes as Record<string, unknown>[] | undefined;
  if (!attributes?.length) return null;
  for (const attr of attributes) {
    const details = attr.field_comparison_details as Record<string, unknown>[] | undefined;
    if (Array.isArray(details)) {
      for (const detail of details) {
        if (String(detail.expected_key || '') === comparisonKey) {
          return detail;
        }
      }
    }
  }
  return null;
};

// Memoized component to render a form field based on its type
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const FormFieldRenderer = memo<Record<string, any>>(
  ({
    fieldKey,
    value,
    onChange,
    onBaselineChange, // New prop for baseline editing
    isReadOnly,
    confidence,
    geometry,
    onFieldFocus,
    onFieldDoubleClick,
    // Notified with the canonical path of the clicked field ("LineItems[0].Rate"),
    // whether or not it has geometry. Separate from onFieldFocus, which carries a
    // bounding box and therefore cannot fire for a field the OCR did not locate —
    // so it cannot be used to tell which field is selected.
    onFieldPathSelect,
    path = [],
    explainabilityInfo = null,
    mergedConfig = null,
    baselineValue = null,
    showComparison = false,
    evaluationResults = null,
    sectionId = null,
    collapsedPaths = new Set(),
    onToggleCollapse,
    filterMode = 'none', // 'none', 'confidence-alerts', 'eval-mismatches'
    displayPath = [], // Separate path for collapse tracking (includes "Document Data" and display keys)
    // Change tracking props
    predictionChanges = new Map(),
    /**
     * Edited field paths WITH array indices preserved, for the provenance and
     * confidence decisions.
     *
     * `predictionChanges` is keyed on a path with array indices stripped — a
     * convention shared by both of its producers and by `trackingPath` below. That
     * is tolerable for the ✏️ badge it originally drove, but not for asserting
     * authorship: editing `LineItems[3].Rate` sets the key `LineItems.Rate`, which
     * every row's Rate cell also computes, so all of them would claim "Your value:"
     * and lose the model's confidence on values nobody touched.
     *
     * Supplied only by the ground-truth editor. Callers that do not pass it keep
     * the collapsed-key behaviour exactly, so the modal's own tracking (and
     * `baselineChanges`, which shares the collapsed key) is untouched.
     */
    editedFieldPaths = null,
    baselineChanges = new Map(),
    _onRevertPrediction = null,
    _onRevertBaseline = null,
  }) => {
    // Calculate path key for collapse state using displayPath
    const pathKey = [...displayPath, fieldKey].join('.');
    const isCollapsed = collapsedPaths.has(pathKey);

    // Helper to check if a value has confidence alerts (recursively)
    const hasConfidenceAlertInTree = (
      val: unknown,
      currentFilteredPath: (string | number)[],
      explainInfo: Record<string, unknown> | Record<string, unknown>[] | null,
      config: Record<string, unknown> | null,
    ): boolean => {
      // Handle null/undefined values - they can still have low confidence in explainability_info
      if (val === null || val === undefined) {
        const fieldInfo = getFieldConfidenceInfo(fieldKey, explainInfo, currentFilteredPath, config);
        if (fieldInfo.hasConfidenceInfo && fieldInfo.displayMode === 'with-threshold' && !fieldInfo.isAboveThreshold) {
          return true;
        }
        return false;
      }

      // For primitives, check if this field has low confidence
      if (typeof val === 'string' || typeof val === 'number' || typeof val === 'boolean') {
        const fieldInfo = getFieldConfidenceInfo(fieldKey, explainInfo, currentFilteredPath, config);
        if (fieldInfo.hasConfidenceInfo && fieldInfo.displayMode === 'with-threshold' && !fieldInfo.isAboveThreshold) {
          return true;
        }
      }

      // For objects, check each property
      if (typeof val === 'object' && val !== null && !Array.isArray(val)) {
        return Object.entries(val).some(([k, v]) => {
          const nestedPath = [...currentFilteredPath, k];
          return hasConfidenceAlertInTreeDeep(v, k, nestedPath, explainInfo, config);
        });
      }

      // For arrays, check each item
      if (Array.isArray(val)) {
        return val.some((item, idx) => {
          const nestedPath = [...currentFilteredPath, idx];
          return hasConfidenceAlertInTreeDeep(item, `[${idx}]`, nestedPath, explainInfo, config);
        });
      }

      return false;
    };

    // Deep helper that takes fieldKey as parameter
    const hasConfidenceAlertInTreeDeep = (
      val: unknown,
      fKey: string,
      currentFilteredPath: (string | number)[],
      explainInfo: Record<string, unknown> | Record<string, unknown>[] | null,
      config: Record<string, unknown> | null,
    ): boolean => {
      // Handle null/undefined values - they can still have low confidence in explainability_info
      if (val === null || val === undefined) {
        const fieldInfo = getFieldConfidenceInfo(fKey, explainInfo, currentFilteredPath.slice(0, -1), config);
        if (fieldInfo.hasConfidenceInfo && fieldInfo.displayMode === 'with-threshold' && !fieldInfo.isAboveThreshold) {
          return true;
        }
        return false;
      }

      if (typeof val === 'string' || typeof val === 'number' || typeof val === 'boolean') {
        const fieldInfo = getFieldConfidenceInfo(fKey, explainInfo, currentFilteredPath.slice(0, -1), config);
        if (fieldInfo.hasConfidenceInfo && fieldInfo.displayMode === 'with-threshold' && !fieldInfo.isAboveThreshold) {
          return true;
        }
      }

      if (typeof val === 'object' && val !== null && !Array.isArray(val)) {
        return Object.entries(val).some(([k, v]) => {
          const nestedPath = [...currentFilteredPath, k];
          return hasConfidenceAlertInTreeDeep(v, k, nestedPath, explainInfo, config);
        });
      }

      if (Array.isArray(val)) {
        return val.some((item, idx) => {
          const nestedPath = [...currentFilteredPath, idx];
          return hasConfidenceAlertInTreeDeep(item, `[${idx}]`, nestedPath, explainInfo, config);
        });
      }

      return false;
    };

    // Helper to check if a value has eval mismatches (recursively)
    const hasEvalMismatchInTree = (
      val: unknown,
      baseval: unknown,
      evalResults: Record<string, unknown> | null,
      secId: unknown,
    ): boolean => {
      if (!evalResults?.section_results) return false;

      // For primitives, check direct mismatch
      if (typeof val === 'string' || typeof val === 'number' || typeof val === 'boolean') {
        if (baseval !== null && JSON.stringify(val) !== JSON.stringify(baseval)) {
          return true;
        }
      }

      // For objects, check each property
      if (typeof val === 'object' && val !== null && !Array.isArray(val)) {
        return Object.entries(val).some(([k, v]) => {
          const nestedBaseline = baseval && typeof baseval === 'object' ? (baseval as Record<string, unknown>)[k] : null;
          return hasEvalMismatchInTree(v, nestedBaseline, evalResults, secId);
        });
      }

      // For arrays, check each item
      if (Array.isArray(val)) {
        return val.some((item, idx) => {
          const nestedBaseline = baseval && Array.isArray(baseval) ? baseval[idx] : null;
          return hasEvalMismatchInTree(item, nestedBaseline, evalResults, secId);
        });
      }

      return false;
    };

    // Get confidence information from explainability data (for all fields)
    // Filter out structural keys from the path for explainability lookup
    // We need to remove top-level keys like 'inference_result', 'explainability_info', 'Document Data', etc.
    const structuralKeys = ['inference_result', 'inferenceResult', 'explainability_info', 'Document Data'];
    let filteredPath = path.filter((pathSegment: string) => !structuralKeys.includes(pathSegment) && typeof pathSegment !== 'undefined');

    // Remove the field name itself from the path if it's the last element
    // The path should point to the parent container, not include the field name
    if (filteredPath.length > 0 && filteredPath[filteredPath.length - 1] === fieldKey) {
      filteredPath = filteredPath.slice(0, -1);
    }

    // Check if this field should be filtered out
    const shouldFilterEval = filterMode === 'eval-mismatches' && showComparison;
    const shouldFilterConfidence = filterMode === 'confidence-alerts';

    const hasMismatchInSubtree = shouldFilterEval ? hasEvalMismatchInTree(value, baselineValue, evaluationResults, sectionId) : true;

    // When checking for confidence alerts in a subtree, we need to include the current fieldKey in the path
    // so that the recursive check can properly navigate the explainability data structure.
    // Without this, paths like "ServiceInformation.[1].Charges" would incorrectly try to navigate
    // explainabilityData[1] instead of explainabilityData.ServiceInformation[1]
    // However:
    // 1. For array items, fieldKey is a display string like "[1]" which shouldn't be added to the path
    //    because the numeric index is already present in the path from the parent's recursive call.
    // 2. For primitive/leaf fields, don't add fieldKey because hasConfidenceAlertInTree uses fieldKey
    //    separately when calling getFieldConfidenceInfo - adding it would cause duplication.
    const isArrayItemDisplay = fieldKey.startsWith('[') && fieldKey.endsWith(']');
    const isPrimitiveValue = typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean' || value === null;
    const pathForAlertCheck =
      structuralKeys.includes(fieldKey) || isArrayItemDisplay || isPrimitiveValue ? filteredPath : [...filteredPath, fieldKey];
    const hasConfidenceAlertInSubtree = shouldFilterConfidence
      ? hasConfidenceAlertInTree(value, pathForAlertCheck, explainabilityInfo, mergedConfig)
      : true;

    // If filter is active and no matches in subtree, hide this field (except root)
    if (shouldFilterEval && !hasMismatchInSubtree && fieldKey !== 'Document Data') {
      return null;
    }
    if (shouldFilterConfidence && !hasConfidenceAlertInSubtree && fieldKey !== 'Document Data') {
      return null;
    }

    // Look up evaluation result for this field from evaluationResults
    let evalResult = null;
    if (showComparison && evaluationResults?.section_results) {
      // Get section result (use first if only one)
      let sectionResult = evaluationResults.section_results.find(
        (sr: Record<string, unknown>) => String(sr.section_id) === String(sectionId),
      );
      if (!sectionResult && evaluationResults.section_results.length === 1) {
        sectionResult = evaluationResults.section_results[0];
      }

      if (sectionResult?.attributes?.length > 0) {
        // First, try an exact match on the canonical comparison key
        // (e.g. "LineItems[0].Description" or "checks[0].bankInfo.bank"). This
        // is how per-list-item leaf fields arrive - as flat entries carrying the
        // backend-annotated per-field evaluation_method and weight. Restrict this
        // to scalar (non-object, non-array) leaves; object/array nodes have their
        // own group/array eval handling and must not be driven by an aggregate
        // detail that happens to share their path.
        const isScalarLeaf = value === null || typeof value !== 'object';
        const comparisonKey = isScalarLeaf ? buildComparisonKey(path) : '';
        const keyedDetail = comparisonKey
          ? findNestedComparisonDetail(evaluationResults, sectionId, comparisonKey)
          : null;
        if (keyedDetail) {
          evalResult = {
            matched: keyedDetail.match,
            score: keyedDetail.score,
            threshold: keyedDetail.threshold,
            reason: keyedDetail.reason,
            expected: keyedDetail.expected_value,
            actual: keyedDetail.actual_value,
            parentPath: keyedDetail.expected_key || '',
            evaluationMethod: keyedDetail.evaluation_method,
            weight: keyedDetail.weight,
          };
        }

        // Fall back to the legacy object-valued match: some details carry the
        // whole object as actual_value with our field as a property.
        if (!evalResult) {
          for (const attr of sectionResult.attributes) {
            if (attr.field_comparison_details && Array.isArray(attr.field_comparison_details)) {
              // Search field_comparison_details for paths that end with our field/path
              for (const detail of attr.field_comparison_details) {
                const expectedKey = detail.expected_key || '';
                // Check if this detail matches our path
                // For nested paths like "checks[0].bankInfo" when looking at "bank" field
                // We check if the actual/expected values contain our field

                // Also extract leaf-level field values from actual_value/expected_value
                if (detail.actual_value && typeof detail.actual_value === 'object') {
                  // Check if our fieldKey is a property in actual_value
                  if (fieldKey in detail.actual_value) {
                    evalResult = {
                      matched: detail.match,
                      score: detail.score,
                      threshold: attr.evaluation_threshold, // Get threshold from parent attribute
                      reason: detail.reason,
                      expected: detail.expected_value?.[fieldKey],
                      actual: detail.actual_value?.[fieldKey],
                      parentPath: expectedKey,
                    };
                    break;
                  }
                }
              }
              if (evalResult) break;
            }
          }
        }
      }
    }

    // Use evaluation result for match status if available, otherwise compare values
    const hasEvalResult = evalResult !== null && evalResult !== undefined;
    const isMatchedFromEval = evalResult ? evalResult.matched : null;
    const valuesMatch = hasEvalResult
      ? isMatchedFromEval
      : !showComparison || baselineValue === null || JSON.stringify(value) === JSON.stringify(baselineValue);
    const hasMismatch = showComparison && baselineValue !== null && !valuesMatch;

    // Extract score, threshold, and reason from evaluation result
    const evalScore = evalResult?.score;
    const _evalThreshold = evalResult?.threshold;
    const evalReason = evalResult?.reason;
    // Per-field comparison method and weight (backend-annotated); only present
    // on path-keyed leaf matches, mirroring the top-level attributes table.
    const evalMethod = evalResult?.evaluationMethod ? String(evalResult.evaluationMethod) : undefined;
    const evalWeight = typeof evalResult?.weight === 'number' ? evalResult.weight : undefined;

    // Determine field type
    let fieldType: string = typeof value;
    if (Array.isArray(value)) {
      fieldType = 'array';
    } else if (value === null || value === undefined) {
      fieldType = 'null';
    }

    const confidenceInfo = getFieldConfidenceInfo(fieldKey, explainabilityInfo, filteredPath, mergedConfig);

    // Determine color and style for confidence display
    let confidenceColor: BoxProps.Color | undefined;
    let confidenceStyle: React.CSSProperties | undefined;
    if (confidenceInfo.hasConfidenceInfo) {
      if (confidenceInfo.displayMode === 'with-threshold') {
        confidenceColor = confidenceInfo.isAboveThreshold ? 'text-status-success' : 'text-status-error';
        confidenceStyle = undefined;
      } else {
        confidenceColor = undefined;
        confidenceStyle = { color: confidenceInfo.textColor };
      }
    }

    // Create label with confidence score if available (legacy support)
    const label = confidence !== undefined ? `${fieldKey} (${(confidence * 100).toFixed(1)}%)` : fieldKey;

    // Handle field focus - pass geometry info if available
    // Reaching a field by keyboard has to do what clicking it does. The wrapper
    // used to be the focusable thing, so tabbing to a field both highlighted it
    // and selected its path; now that the wrapper is no longer a control (see
    // below), the input's own focus is the only place that can still happen.
    const handleFocus = () => {
      if (onFieldPathSelect) {
        const selectedPath = buildComparisonKey(path);
        if (selectedPath) onFieldPathSelect(selectedPath);
      }
      if (geometry && onFieldFocus) {
        onFieldFocus(geometry);
      }
    };

    // Handle field click - optimized version
    const handleClick = (event?: { stopPropagation: () => void }) => {
      const clickStart = performance.now();
      logger.debug('🖱️ FIELD CLICK START:', { fieldKey, timestamp: clickStart });

      if (event) {
        event.stopPropagation();
      }

      // Announce the selection before the geometry hunt below, so a field with no
      // bounding box is still selectable (and therefore still linkable).
      if (onFieldPathSelect) {
        const selectedPath = buildComparisonKey(path);
        if (selectedPath) onFieldPathSelect(selectedPath);
      }

      let actualGeometry = geometry;

      // Try to extract geometry from explainabilityInfo if not provided
      if (!actualGeometry && explainabilityInfo && Array.isArray(explainabilityInfo) && explainabilityInfo[0]) {
        const [firstExplainabilityItem] = explainabilityInfo;

        // Try direct field lookup first
        let fieldInfo = firstExplainabilityItem[fieldKey];

        // If not found directly, try to navigate the full path
        if (!fieldInfo) {
          const fullPathParts = [...path, fieldKey];
          let pathFieldInfo = firstExplainabilityItem;

          fullPathParts.forEach((pathPart: string | number) => {
            if (pathFieldInfo && typeof pathFieldInfo === 'object') {
              if (Array.isArray(pathFieldInfo) && !Number.isNaN(parseInt(String(pathPart), 10))) {
                const arrayIndex = parseInt(String(pathPart), 10);
                if (arrayIndex >= 0 && arrayIndex < pathFieldInfo.length) {
                  // nosemgrep: javascript.lang.security.audit.prototype-pollution.prototype-pollution-loop
                  pathFieldInfo = pathFieldInfo[arrayIndex];
                } else {
                  pathFieldInfo = null;
                }
              } else if (pathFieldInfo[pathPart]) {
                // nosemgrep: javascript.lang.security.audit.prototype-pollution.prototype-pollution-loop
                pathFieldInfo = pathFieldInfo[pathPart];
              } else {
                pathFieldInfo = null;
              }
            } else {
              pathFieldInfo = null;
            }
          });

          fieldInfo = pathFieldInfo;
        }

        if (fieldInfo && fieldInfo.geometry && Array.isArray(fieldInfo.geometry) && fieldInfo.geometry[0]) {
          actualGeometry = fieldInfo.geometry[0];
        }
      }

      if (actualGeometry && onFieldFocus) {
        const focusStart = performance.now();
        logger.debug('🎯 FIELD FOCUS START:', { fieldKey, timestamp: focusStart });
        onFieldFocus(actualGeometry);
        const focusEnd = performance.now();
        logger.debug('✅ FIELD FOCUS END:', { fieldKey, duration: `${(focusEnd - focusStart).toFixed(2)}ms` });
      }

      const clickEnd = performance.now();
      logger.debug('🏁 FIELD CLICK END:', { fieldKey, totalDuration: `${(clickEnd - clickStart).toFixed(2)}ms` });
    };

    // Handle field double-click
    const handleDoubleClick = (event: React.SyntheticEvent) => {
      if (event) {
        event.stopPropagation();
      }
      logger.debug('=== FIELD DOUBLE-CLICKED ===');
      logger.debug('Field Key:', fieldKey);
      logger.debug('Geometry Passed:', geometry);

      let actualGeometry = geometry;

      // Try to extract geometry from explainabilityInfo if not provided
      if (!actualGeometry && explainabilityInfo && Array.isArray(explainabilityInfo) && explainabilityInfo[0]) {
        const [firstExplainabilityItem] = explainabilityInfo;
        const fieldInfo = firstExplainabilityItem[fieldKey];

        if (fieldInfo && fieldInfo.geometry && Array.isArray(fieldInfo.geometry) && fieldInfo.geometry[0]) {
          actualGeometry = fieldInfo.geometry[0];
        }
      }

      if (actualGeometry && onFieldDoubleClick) {
        logger.debug('Calling onFieldDoubleClick with geometry:', actualGeometry);
        onFieldDoubleClick(actualGeometry);
      } else {
        logger.debug('No geometry found for field double-click:', fieldKey);
      }
      logger.debug('=== END FIELD DOUBLE-CLICK ===');
    };

    // Check if this specific field has been edited (for visual highlighting)
    // Note: path already INCLUDES the current field key (from recursive calls like path={[...path, key]})
    // So we should NOT add fieldKey again - just filter the path to exclude array indices and structural keys
    const trackingPath = path.filter((p: unknown) => typeof p !== 'number' && p !== undefined && p !== 'Document Data');
    const fieldPathStr = trackingPath.join('.');
    // Same path with indices kept, so one edited table row does not speak for the
    // whole column. See the editedFieldPaths prop.
    const exactPathStr = path.filter((p: unknown) => p !== undefined && p !== 'Document Data').join('.');
    const isPredictionChanged = editedFieldPaths
      ? (editedFieldPaths as { has: (key: string) => boolean }).has(exactPathStr)
      : predictionChanges.has(fieldPathStr);
    const isBaselineChanged = baselineChanges.has(fieldPathStr);
    const hasLocalEdit = isPredictionChanged || isBaselineChanged;

    // Debug logging for change tracking - only for leaf fields (strings/numbers)
    if ((predictionChanges.size > 0 || baselineChanges.size > 0) && (typeof value === 'string' || typeof value === 'number')) {
      logger.debug('🔍 Change tracking check:', {
        fieldKey,
        path,
        trackingPath,
        fieldPathStr,
        isPredictionChanged,
        isBaselineChanged,
        predictionKeys: [...predictionChanges.keys()],
        fieldType: typeof value,
      });
    }

    // Render based on field type
    switch (fieldType) {
      case 'string':
        return (
          <div
            /* Only a double-click, which is a mouse-only gesture by definition and
               duplicates nothing: every value row below carries its own click
               handler, and the locate control carries the keyboard path. */
            onDoubleClick={handleDoubleClick}
            /* Deliberately not role="button"/tabIndex: this div wraps a FormField
               containing a text input and, in comparison mode, a copy button.
               ARIA forbids interactive descendants of a `button`, and screen
               readers computed the wrapper's name from its own subtree — so every
               field announced as a button named after its label, then again as the
               textbox with the same label. The click handler stays as a mouse
               convenience; keyboard and AT users get the explicit locate control
               below, which works in read-only mode too (where there is no input to
               focus) and is only offered when the field actually has a bounding box
               to show. */
            {...({ 'data-field-path': buildComparisonKey(path) || undefined } as Record<string, string | undefined>)}
            style={{
              cursor: geometry ? 'pointer' : 'default',
              backgroundColor: hasMismatch && !hasLocalEdit ? 'rgba(255, 153, 0, 0.05)' : 'transparent',
              padding: '4px',
              borderRadius: '4px',
              borderLeft: hasMismatch && !hasLocalEdit ? '3px solid #ff9900' : '3px solid transparent',
            }}
          >
            <FormField
              label={
                <ExtBox>
                  <SpaceBetween direction="horizontal" size="xs">
                    <span>{fieldKey}:</span>
                    {isPredictionChanged && (
                      <ExtBox color="text-status-info" fontSize="body-s" fontWeight="bold">
                        ✏️ Edited
                      </ExtBox>
                    )}
                    {isBaselineChanged && !isPredictionChanged && (
                      <ExtBox color="text-status-warning" fontSize="body-s" fontWeight="bold">
                        ✏️ Baseline Edited
                      </ExtBox>
                    )}
                    {hasMismatch && !hasLocalEdit && (
                      <ExtBox color="text-status-warning" fontSize="body-s" fontWeight="bold">
                        ⚠ Mismatch
                      </ExtBox>
                    )}
                    {!hasMismatch && !hasLocalEdit && showComparison && baselineValue !== null && (
                      <ExtBox color="text-status-success" fontSize="body-s">
                        ✓ Match
                      </ExtBox>
                    )}
                  </SpaceBetween>
                  {/* Suppressed once a human has overwritten the value: the model
                      never produced this text, so its confidence says nothing about
                      it. Leaving it attached decorated hand-typed ground truth with
                      a score of, typically, 100%. */}
                  {confidenceInfo.hasConfidenceInfo && !hasLocalEdit && (
                    <ExtBox fontSize="body-s" padding={{ top: 'xxxs' }} color={confidenceColor} style={confidenceStyle}>
                      {confidenceInfo.displayMode === 'with-threshold'
                        ? `Confidence: ${((confidenceInfo.confidence ?? 0) * 100).toFixed(1)}% / Threshold: ${(
                            (confidenceInfo.confidenceThreshold ?? 0) * 100
                          ).toFixed(1)}%`
                        : `Confidence: ${((confidenceInfo.confidence ?? 0) * 100).toFixed(1)}%`}
                    </ExtBox>
                  )}
                  {confidenceInfo.hasConfidenceInfo && hasLocalEdit && (
                    <ExtBox fontSize="body-s" padding={{ top: 'xxxs' }} color="text-body-secondary">
                      Edited — the model&apos;s confidence no longer applies
                    </ExtBox>
                  )}
                  {showComparison && evalScore !== undefined && (
                    <ExtBox fontSize="body-s" padding={{ top: 'xxxs' }} color={hasMismatch ? 'text-status-warning' : 'text-status-success'}>
                      {`Eval Score: ${(evalScore * 100).toFixed(1)}%`}
                      {evalReason && ` - ${evalReason}`}
                    </ExtBox>
                  )}
                  {showComparison && (evalMethod || evalWeight !== undefined) && (
                    <ExtBox fontSize="body-s" padding={{ top: 'xxxs' }} color="text-body-secondary">
                      {evalMethod && `Method: ${evalMethod}`}
                      {evalMethod && evalWeight !== undefined && ' · '}
                      {evalWeight !== undefined && `Weight: ${evalWeight.toFixed(2)}`}
                    </ExtBox>
                  )}
                </ExtBox>
              }
            >
              <SpaceBetween size="xxs">
                <ExtBox onClick={handleClick} style={{ cursor: 'pointer' }}>
                  <SpaceBetween direction="horizontal" size="xxs" alignItems="center">
                    {/* Not "Predicted" once a human has replaced it — the label is a
                        provenance claim, and it was still asserting the model's. */}
                    <ExtBox fontSize="body-s" color="text-body-secondary">
                      {isPredictionChanged ? 'Your value:' : 'Predicted:'}
                    </ExtBox>
                    {isPredictionChanged && (
                      <ExtBox fontSize="body-s" color="text-status-info" fontWeight="bold">
                        ✏️
                      </ExtBox>
                    )}
                    {/* The only keyboard-reachable way to ask "where did this come
                        from?", and the only visible sign that asking is possible.
                        The whole field was a click target before, with no
                        affordance: invisible to sighted users, unreachable to AT
                        ones. */}
                    {geometry && (
                      <Button
                        variant="inline-icon"
                        iconName="search"
                        ariaLabel={`Show ${fieldKey} on the page`}
                        onClick={(e) => {
                          e.stopPropagation();
                          handleClick();
                        }}
                      />
                    )}
                  </SpaceBetween>
                  <div
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: '4px',
                      pointerEvents: isReadOnly ? 'none' : 'auto',
                      borderLeft: isPredictionChanged ? '3px solid #0073bb' : '3px solid transparent',
                      paddingLeft: '4px',
                      backgroundColor: isPredictionChanged ? 'rgba(0, 115, 187, 0.08)' : 'transparent',
                      borderRadius: '2px',
                    }}
                  >
                    <div style={{ flex: 1 }}>
                      {isReadOnly ? (
                        <div
                          style={{
                            backgroundColor: '#e9ebed',
                            border: '1px solid #d5dbdb',
                            borderRadius: '4px',
                            minHeight: '32px',
                            display: 'flex',
                            alignItems: 'center',
                            color: '#16191f',
                            padding: '4px 8px',
                            fontSize: '14px',
                          }}
                        >
                          {value || ''}
                        </div>
                      ) : (
                        <Input value={value || ''} onChange={({ detail }) => onChange(detail.value)} onFocus={handleFocus} />
                      )}
                    </div>
                    {!isReadOnly && showComparison && baselineValue !== null && (
                      <Button
                        variant="inline-icon"
                        iconName="arrow-down"
                        ariaLabel="Copy predicted value to baseline"
                        onClick={(e) => {
                          e.stopPropagation();
                          if (onBaselineChange) {
                            onBaselineChange(value);
                          }
                        }}
                      />
                    )}
                  </div>
                </ExtBox>
                {showComparison && baselineValue !== null && (
                  <ExtBox onClick={handleClick} style={{ cursor: 'pointer' }}>
                    <SpaceBetween direction="horizontal" size="xxs" alignItems="center">
                      <ExtBox fontSize="body-s" color="text-body-secondary">
                        Expected (baseline):
                      </ExtBox>
                      {isBaselineChanged && (
                        <ExtBox fontSize="body-s" color="text-status-warning" fontWeight="bold">
                          ✏️
                        </ExtBox>
                      )}
                    </SpaceBetween>
                    <div
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: '4px',
                        pointerEvents: isReadOnly ? 'none' : 'auto',
                        borderLeft: isBaselineChanged ? '3px solid #ff9900' : '3px solid transparent',
                        paddingLeft: '4px',
                        backgroundColor: isBaselineChanged ? 'rgba(255, 153, 0, 0.08)' : 'transparent',
                        borderRadius: '2px',
                      }}
                    >
                      <div style={{ flex: 1 }}>
                        {isReadOnly ? (
                          <div
                            style={{
                              backgroundColor: '#e9ebed',
                              border: '1px solid #d5dbdb',
                              borderRadius: '4px',
                              minHeight: '32px',
                              display: 'flex',
                              alignItems: 'center',
                              color: '#16191f',
                              padding: '4px 8px',
                              fontSize: '14px',
                            }}
                          >
                            {String(baselineValue ?? '')}
                          </div>
                        ) : (
                          <Input
                            value={String(baselineValue ?? '')}
                            onChange={({ detail }) => {
                              if (onBaselineChange) {
                                onBaselineChange(detail.value);
                              }
                            }}
                          />
                        )}
                      </div>
                      {!isReadOnly && (
                        <Button
                          variant="inline-icon"
                          iconName="arrow-up"
                          ariaLabel="Copy baseline value to predicted"
                          onClick={(e) => {
                            e.stopPropagation();
                            onChange(baselineValue);
                          }}
                        />
                      )}
                    </div>
                  </ExtBox>
                )}
              </SpaceBetween>
            </FormField>
          </div>
        );

      case 'number':
        return (
          <div
            /* Only a double-click, which is a mouse-only gesture by definition and
               duplicates nothing: every value row below carries its own click
               handler, and the locate control carries the keyboard path. */
            onDoubleClick={handleDoubleClick}
            /* See the note on the string case: not a button, because it contains one. */
            {...({ 'data-field-path': buildComparisonKey(path) || undefined } as Record<string, string | undefined>)}
            style={{
              cursor: geometry ? 'pointer' : 'default',
              backgroundColor: hasMismatch && !hasLocalEdit ? 'rgba(255, 153, 0, 0.05)' : 'transparent',
              padding: '4px',
              borderRadius: '4px',
              borderLeft: hasMismatch && !hasLocalEdit ? '3px solid #ff9900' : '3px solid transparent',
            }}
          >
            <FormField
              label={
                <ExtBox>
                  <SpaceBetween direction="horizontal" size="xs">
                    <span>{fieldKey}:</span>
                    {isPredictionChanged && (
                      <ExtBox color="text-status-info" fontSize="body-s" fontWeight="bold">
                        ✏️ Edited
                      </ExtBox>
                    )}
                    {isBaselineChanged && !isPredictionChanged && (
                      <ExtBox color="text-status-warning" fontSize="body-s" fontWeight="bold">
                        ✏️ Baseline Edited
                      </ExtBox>
                    )}
                    {hasMismatch && !hasLocalEdit && (
                      <ExtBox color="text-status-warning" fontSize="body-s" fontWeight="bold">
                        ⚠ Mismatch
                      </ExtBox>
                    )}
                    {!hasMismatch && !hasLocalEdit && showComparison && baselineValue !== null && (
                      <ExtBox color="text-status-success" fontSize="body-s">
                        ✓ Match
                      </ExtBox>
                    )}
                  </SpaceBetween>
                  {/* Suppressed once a human has overwritten the value: the model
                      never produced this text, so its confidence says nothing about
                      it. Leaving it attached decorated hand-typed ground truth with
                      a score of, typically, 100%. */}
                  {confidenceInfo.hasConfidenceInfo && !hasLocalEdit && (
                    <ExtBox fontSize="body-s" padding={{ top: 'xxxs' }} color={confidenceColor} style={confidenceStyle}>
                      {confidenceInfo.displayMode === 'with-threshold'
                        ? `Confidence: ${((confidenceInfo.confidence ?? 0) * 100).toFixed(1)}% / Threshold: ${(
                            (confidenceInfo.confidenceThreshold ?? 0) * 100
                          ).toFixed(1)}%`
                        : `Confidence: ${((confidenceInfo.confidence ?? 0) * 100).toFixed(1)}%`}
                    </ExtBox>
                  )}
                  {confidenceInfo.hasConfidenceInfo && hasLocalEdit && (
                    <ExtBox fontSize="body-s" padding={{ top: 'xxxs' }} color="text-body-secondary">
                      Edited — the model&apos;s confidence no longer applies
                    </ExtBox>
                  )}
                  {showComparison && evalScore !== undefined && (
                    <ExtBox fontSize="body-s" padding={{ top: 'xxxs' }} color={hasMismatch ? 'text-status-warning' : 'text-status-success'}>
                      {`Eval Score: ${(evalScore * 100).toFixed(1)}%`}
                      {evalReason && ` - ${evalReason}`}
                    </ExtBox>
                  )}
                  {showComparison && (evalMethod || evalWeight !== undefined) && (
                    <ExtBox fontSize="body-s" padding={{ top: 'xxxs' }} color="text-body-secondary">
                      {evalMethod && `Method: ${evalMethod}`}
                      {evalMethod && evalWeight !== undefined && ' · '}
                      {evalWeight !== undefined && `Weight: ${evalWeight.toFixed(2)}`}
                    </ExtBox>
                  )}
                </ExtBox>
              }
            >
              <SpaceBetween size="xxs">
                <ExtBox onClick={handleClick} style={{ cursor: 'pointer' }}>
                  <SpaceBetween direction="horizontal" size="xxs" alignItems="center">
                    {/* Not "Predicted" once a human has replaced it — the label is a
                        provenance claim, and it was still asserting the model's. */}
                    <ExtBox fontSize="body-s" color="text-body-secondary">
                      {isPredictionChanged ? 'Your value:' : 'Predicted:'}
                    </ExtBox>
                    {isPredictionChanged && (
                      <ExtBox fontSize="body-s" color="text-status-info" fontWeight="bold">
                        ✏️
                      </ExtBox>
                    )}
                    {/* The only keyboard-reachable way to ask "where did this come
                        from?", and the only visible sign that asking is possible.
                        The whole field was a click target before, with no
                        affordance: invisible to sighted users, unreachable to AT
                        ones. */}
                    {geometry && (
                      <Button
                        variant="inline-icon"
                        iconName="search"
                        ariaLabel={`Show ${fieldKey} on the page`}
                        onClick={(e) => {
                          e.stopPropagation();
                          handleClick();
                        }}
                      />
                    )}
                  </SpaceBetween>
                  <div
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: '4px',
                      pointerEvents: isReadOnly ? 'none' : 'auto',
                      borderLeft: isPredictionChanged ? '3px solid #0073bb' : '3px solid transparent',
                      paddingLeft: '4px',
                      backgroundColor: isPredictionChanged ? 'rgba(0, 115, 187, 0.08)' : 'transparent',
                      borderRadius: '2px',
                    }}
                  >
                    <div style={{ flex: 1 }}>
                      {isReadOnly ? (
                        <div
                          style={{
                            backgroundColor: '#e9ebed',
                            border: '1px solid #d5dbdb',
                            borderRadius: '4px',
                            minHeight: '32px',
                            display: 'flex',
                            alignItems: 'center',
                            color: '#16191f',
                            padding: '4px 8px',
                            fontSize: '14px',
                          }}
                        >
                          {String(value)}
                        </div>
                      ) : (
                        <Input
                          type="number"
                          value={String(value)}
                          onChange={({ detail }) => {
                            const numValue = Number(detail.value);
                            onChange(Number.isNaN(numValue) ? 0 : numValue);
                          }}
                          onFocus={handleFocus}
                        />
                      )}
                    </div>
                    {!isReadOnly && showComparison && baselineValue !== null && (
                      <Button
                        variant="inline-icon"
                        iconName="arrow-down"
                        ariaLabel="Copy predicted value to baseline"
                        onClick={(e) => {
                          e.stopPropagation();
                          if (onBaselineChange) {
                            onBaselineChange(value);
                          }
                        }}
                      />
                    )}
                  </div>
                </ExtBox>
                {showComparison && baselineValue !== null && (
                  <ExtBox onClick={handleClick} style={{ cursor: 'pointer' }}>
                    <SpaceBetween direction="horizontal" size="xxs" alignItems="center">
                      <ExtBox fontSize="body-s" color="text-body-secondary">
                        Expected (baseline):
                      </ExtBox>
                      {isBaselineChanged && (
                        <ExtBox fontSize="body-s" color="text-status-warning" fontWeight="bold">
                          ✏️
                        </ExtBox>
                      )}
                    </SpaceBetween>
                    <div
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: '4px',
                        pointerEvents: isReadOnly ? 'none' : 'auto',
                        borderLeft: isBaselineChanged ? '3px solid #ff9900' : '3px solid transparent',
                        paddingLeft: '4px',
                        backgroundColor: isBaselineChanged ? 'rgba(255, 153, 0, 0.08)' : 'transparent',
                        borderRadius: '2px',
                      }}
                    >
                      <div style={{ flex: 1 }}>
                        {isReadOnly ? (
                          <div
                            style={{
                              backgroundColor: '#e9ebed',
                              border: '1px solid #d5dbdb',
                              borderRadius: '4px',
                              minHeight: '32px',
                              display: 'flex',
                              alignItems: 'center',
                              color: '#16191f',
                              padding: '4px 8px',
                              fontSize: '14px',
                            }}
                          >
                            {String(baselineValue ?? '')}
                          </div>
                        ) : (
                          <Input
                            type="number"
                            value={String(baselineValue ?? '')}
                            onChange={({ detail }) => {
                              if (onBaselineChange) {
                                const numValue = Number(detail.value);
                                onBaselineChange(Number.isNaN(numValue) ? 0 : numValue);
                              }
                            }}
                          />
                        )}
                      </div>
                      {!isReadOnly && (
                        <Button
                          variant="inline-icon"
                          iconName="arrow-up"
                          ariaLabel="Copy baseline value to predicted"
                          onClick={(e) => {
                            e.stopPropagation();
                            onChange(baselineValue);
                          }}
                        />
                      )}
                    </div>
                  </ExtBox>
                )}
              </SpaceBetween>
            </FormField>
          </div>
        );

      case 'boolean':
        return (
          <div
            /* Only a double-click, which is a mouse-only gesture by definition and
               duplicates nothing: every value row below carries its own click
               handler, and the locate control carries the keyboard path. */
            onDoubleClick={handleDoubleClick}
            /* See the note on the string case: not a button, because it contains one. */
            {...({ 'data-field-path': buildComparisonKey(path) || undefined } as Record<string, string | undefined>)}
            style={{
              cursor: geometry ? 'pointer' : 'default',
              backgroundColor: hasMismatch && !hasLocalEdit ? 'rgba(255, 153, 0, 0.05)' : 'transparent',
              padding: '4px',
              borderRadius: '4px',
              borderLeft: hasMismatch && !hasLocalEdit ? '3px solid #ff9900' : '3px solid transparent',
            }}
          >
            <FormField
              label={
                <ExtBox>
                  <SpaceBetween direction="horizontal" size="xs">
                    <span>{fieldKey}:</span>
                    {isPredictionChanged && (
                      <ExtBox color="text-status-info" fontSize="body-s" fontWeight="bold">
                        ✏️ Edited
                      </ExtBox>
                    )}
                    {isBaselineChanged && !isPredictionChanged && (
                      <ExtBox color="text-status-warning" fontSize="body-s" fontWeight="bold">
                        ✏️ Baseline Edited
                      </ExtBox>
                    )}
                    {hasMismatch && !hasLocalEdit && (
                      <ExtBox color="text-status-warning" fontSize="body-s" fontWeight="bold">
                        ⚠ Mismatch
                      </ExtBox>
                    )}
                    {!hasMismatch && !hasLocalEdit && showComparison && baselineValue !== null && (
                      <ExtBox color="text-status-success" fontSize="body-s">
                        ✓ Match
                      </ExtBox>
                    )}
                  </SpaceBetween>
                  {/* Suppressed once a human has overwritten the value: the model
                      never produced this text, so its confidence says nothing about
                      it. Leaving it attached decorated hand-typed ground truth with
                      a score of, typically, 100%. */}
                  {confidenceInfo.hasConfidenceInfo && !hasLocalEdit && (
                    <ExtBox fontSize="body-s" padding={{ top: 'xxxs' }} color={confidenceColor} style={confidenceStyle}>
                      {confidenceInfo.displayMode === 'with-threshold'
                        ? `Confidence: ${((confidenceInfo.confidence ?? 0) * 100).toFixed(1)}% / Threshold: ${(
                            (confidenceInfo.confidenceThreshold ?? 0) * 100
                          ).toFixed(1)}%`
                        : `Confidence: ${((confidenceInfo.confidence ?? 0) * 100).toFixed(1)}%`}
                    </ExtBox>
                  )}
                  {confidenceInfo.hasConfidenceInfo && hasLocalEdit && (
                    <ExtBox fontSize="body-s" padding={{ top: 'xxxs' }} color="text-body-secondary">
                      Edited — the model&apos;s confidence no longer applies
                    </ExtBox>
                  )}
                  {showComparison && evalScore !== undefined && (
                    <ExtBox fontSize="body-s" padding={{ top: 'xxxs' }} color={hasMismatch ? 'text-status-warning' : 'text-status-success'}>
                      {`Eval Score: ${(evalScore * 100).toFixed(1)}%`}
                      {evalReason && ` - ${evalReason}`}
                    </ExtBox>
                  )}
                  {showComparison && (evalMethod || evalWeight !== undefined) && (
                    <ExtBox fontSize="body-s" padding={{ top: 'xxxs' }} color="text-body-secondary">
                      {evalMethod && `Method: ${evalMethod}`}
                      {evalMethod && evalWeight !== undefined && ' · '}
                      {evalWeight !== undefined && `Weight: ${evalWeight.toFixed(2)}`}
                    </ExtBox>
                  )}
                </ExtBox>
              }
            >
              <SpaceBetween size="xxs">
                <ExtBox onClick={handleClick} style={{ cursor: 'pointer' }}>
                  <SpaceBetween direction="horizontal" size="xxs" alignItems="center">
                    {/* Not "Predicted" once a human has replaced it — the label is a
                        provenance claim, and it was still asserting the model's. */}
                    <ExtBox fontSize="body-s" color="text-body-secondary">
                      {isPredictionChanged ? 'Your value:' : 'Predicted:'}
                    </ExtBox>
                    {isPredictionChanged && (
                      <ExtBox fontSize="body-s" color="text-status-info" fontWeight="bold">
                        ✏️
                      </ExtBox>
                    )}
                    {/* The only keyboard-reachable way to ask "where did this come
                        from?", and the only visible sign that asking is possible.
                        The whole field was a click target before, with no
                        affordance: invisible to sighted users, unreachable to AT
                        ones. */}
                    {geometry && (
                      <Button
                        variant="inline-icon"
                        iconName="search"
                        ariaLabel={`Show ${fieldKey} on the page`}
                        onClick={(e) => {
                          e.stopPropagation();
                          handleClick();
                        }}
                      />
                    )}
                  </SpaceBetween>
                  <div
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: '4px',
                      pointerEvents: isReadOnly ? 'none' : 'auto',
                      borderLeft: isPredictionChanged ? '3px solid #0073bb' : '3px solid transparent',
                      paddingLeft: '4px',
                      backgroundColor: isPredictionChanged ? 'rgba(0, 115, 187, 0.08)' : 'transparent',
                      borderRadius: '2px',
                    }}
                  >
                    <div style={{ flex: 1 }}>
                      {isReadOnly ? (
                        <div
                          style={{
                            backgroundColor: '#e9ebed',
                            border: '1px solid #d5dbdb',
                            borderRadius: '4px',
                            minHeight: '32px',
                            display: 'flex',
                            alignItems: 'center',
                            color: '#16191f',
                            padding: '4px 8px',
                            fontSize: '14px',
                          }}
                        >
                          {String(value)}
                        </div>
                      ) : (
                        <Checkbox checked={Boolean(value)} onChange={({ detail }) => onChange(detail.checked)} onFocus={handleFocus}>
                          {String(value)}
                        </Checkbox>
                      )}
                    </div>
                    {!isReadOnly && showComparison && baselineValue !== null && (
                      <Button
                        variant="inline-icon"
                        iconName="arrow-down"
                        ariaLabel="Copy predicted value to baseline"
                        onClick={(e) => {
                          e.stopPropagation();
                          if (onBaselineChange) {
                            onBaselineChange(value);
                          }
                        }}
                      />
                    )}
                  </div>
                </ExtBox>
                {showComparison && baselineValue !== null && (
                  <ExtBox onClick={handleClick} style={{ cursor: 'pointer' }}>
                    <SpaceBetween direction="horizontal" size="xxs" alignItems="center">
                      <ExtBox fontSize="body-s" color="text-body-secondary">
                        Expected (baseline):
                      </ExtBox>
                      {isBaselineChanged && (
                        <ExtBox fontSize="body-s" color="text-status-warning" fontWeight="bold">
                          ✏️
                        </ExtBox>
                      )}
                    </SpaceBetween>
                    <div
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: '4px',
                        pointerEvents: isReadOnly ? 'none' : 'auto',
                        borderLeft: isBaselineChanged ? '3px solid #ff9900' : '3px solid transparent',
                        paddingLeft: '4px',
                        backgroundColor: isBaselineChanged ? 'rgba(255, 153, 0, 0.08)' : 'transparent',
                        borderRadius: '2px',
                      }}
                    >
                      <div style={{ flex: 1 }}>
                        {isReadOnly ? (
                          <div
                            style={{
                              backgroundColor: '#e9ebed',
                              border: '1px solid #d5dbdb',
                              borderRadius: '4px',
                              minHeight: '32px',
                              display: 'flex',
                              alignItems: 'center',
                              color: '#16191f',
                              padding: '4px 8px',
                              fontSize: '14px',
                            }}
                          >
                            {String(baselineValue ?? '')}
                          </div>
                        ) : (
                          <Checkbox
                            checked={Boolean(baselineValue)}
                            onChange={({ detail }) => {
                              if (onBaselineChange) {
                                onBaselineChange(detail.checked);
                              }
                            }}
                          >
                            {String(baselineValue ?? '')}
                          </Checkbox>
                        )}
                      </div>
                      {!isReadOnly && (
                        <Button
                          variant="inline-icon"
                          iconName="arrow-up"
                          ariaLabel="Copy baseline value to predicted"
                          onClick={(e) => {
                            e.stopPropagation();
                            onChange(baselineValue);
                          }}
                        />
                      )}
                    </div>
                  </ExtBox>
                )}
              </SpaceBetween>
            </FormField>
          </div>
        );

      case 'object': {
        if (value === null) {
          return (
            <FormField label={label}>
              <div
                style={{
                  backgroundColor: '#e9ebed',
                  border: '1px solid #d5dbdb',
                  borderRadius: '4px',
                  minHeight: '32px',
                  display: 'flex',
                  alignItems: 'center',
                  fontStyle: 'italic',
                  color: '#545b64',
                  padding: '4px 8px',
                  fontSize: '14px',
                }}
              >
                null
              </div>
            </FormField>
          );
        }

        // Look for group-level eval result (e.g., for bankInfo, personalInfo)
        let groupEvalResult = null;
        if (showComparison && evaluationResults?.section_results) {
          let sectionResult = evaluationResults.section_results.find(
            (sr: Record<string, unknown>) => String(sr.section_id) === String(sectionId),
          );
          if (!sectionResult && evaluationResults.section_results.length === 1) {
            sectionResult = evaluationResults.section_results[0];
          }
          if (sectionResult?.attributes?.length > 0) {
            for (const attr of sectionResult.attributes) {
              if (attr.field_comparison_details) {
                // Find detail that matches this object path (e.g., checks[0].bankInfo)
                const detail = attr.field_comparison_details.find((d: Record<string, unknown>) => {
                  const key = String(d.expected_key || '');
                  // Check if the detail's expected_key ends with our fieldKey
                  return key.endsWith(`.${fieldKey}`) || key === fieldKey;
                });
                if (detail) {
                  groupEvalResult = {
                    matched: detail.match,
                    score: detail.score,
                    reason: detail.reason,
                  };
                  break;
                }
              }
            }
          }
        }

        return (
          <ExtBox
            padding="xs"
            style={{
              backgroundColor: groupEvalResult && !groupEvalResult.matched ? 'rgba(255, 153, 0, 0.08)' : 'transparent',
              borderRadius: '4px',
            }}
          >
            <ExtBox fontSize="body-m" fontWeight="bold" padding="xxxs" onFocus={handleFocus}>
              <SpaceBetween direction="horizontal" size="xs">
                {onToggleCollapse && (
                  <span
                    onClick={() => onToggleCollapse(pathKey)}
                    onKeyDown={(e) => e.key === 'Enter' && onToggleCollapse(pathKey)}
                    role="button"
                    tabIndex={0}
                    style={{ cursor: 'pointer', userSelect: 'none' }}
                  >
                    {isCollapsed ? '▶' : '▼'}
                  </span>
                )}
                <span>{label}</span>
                {groupEvalResult && !groupEvalResult.matched && (
                  <ExtBox color="text-status-warning" fontSize="body-s">
                    ⚠
                  </ExtBox>
                )}
                {groupEvalResult && groupEvalResult.matched && showComparison && (
                  <ExtBox color="text-status-success" fontSize="body-s">
                    ✓
                  </ExtBox>
                )}
              </SpaceBetween>
              {showComparison && groupEvalResult && (
                <ExtBox fontSize="body-s" color={groupEvalResult.matched ? 'text-status-success' : 'text-status-warning'}>
                  {`Group Score: ${(groupEvalResult.score * 100).toFixed(1)}%`}
                  {groupEvalResult.reason && ` - ${groupEvalResult.reason}`}
                </ExtBox>
              )}
            </ExtBox>
            {!isCollapsed && (
              <ExtBox padding={{ left: 'l' }}>
                <SpaceBetween size="xs">
                  {Object.entries(value).map(([key, val]) => {
                    // Get confidence and geometry for this field from explainability_info
                    let fieldConfidence;
                    let fieldGeometry;

                    // Try to get from explainability_info if available
                    if (explainabilityInfo && Array.isArray(explainabilityInfo)) {
                      // Handle nested structure like explainabilityInfo[0].NAME_DETAILS.LAST_NAME
                      const currentPath = [...path, key];
                      const [firstExplainabilityItem] = explainabilityInfo;
                       
                      let fieldInfo = firstExplainabilityItem;

                      // Navigate through the path to find the field info
                      let pathFieldInfo = fieldInfo;
                      currentPath.forEach((pathPart: string | number) => {
                        if (pathFieldInfo && typeof pathFieldInfo === 'object' && (pathFieldInfo as Record<string, unknown>)[pathPart as string]) {
                          // nosemgrep: javascript.lang.security.audit.prototype-pollution.prototype-pollution-loop
                          pathFieldInfo = pathFieldInfo[pathPart];
                        } else {
                          pathFieldInfo = null;
                        }
                      });
                      fieldInfo = pathFieldInfo;

                      if (fieldInfo) {
                        fieldConfidence = fieldInfo.confidence;

                        // Extract geometry - handle both direct geometry and geometry arrays
                        if (fieldInfo.geometry && Array.isArray(fieldInfo.geometry) && fieldInfo.geometry.length > 0) {
                          const geomData = fieldInfo.geometry[0];
                          if (geomData.boundingBox && geomData.page !== undefined) {
                            fieldGeometry = {
                              boundingBox: geomData.boundingBox,
                              page: geomData.page,
                              vertices: geomData.vertices,
                            };
                          }
                        }
                      }
                    }

                    // Get baseline value for this nested field
                    const nestedBaselineValue =
                      showComparison && baselineValue !== null && typeof baselineValue === 'object' ? baselineValue[key] : null;

                    return (
                      <FormFieldRenderer
                        key={`obj-${fieldKey}-${path.join('.')}-${key}`}
                        fieldKey={key}
                        value={val}
                        onChange={(newVal: unknown) => {
                          if (!isReadOnly) {
                            const newObj = { ...value };
                            newObj[key] = newVal;
                            onChange(newObj);
                          }
                        }}
                        onBaselineChange={
                          onBaselineChange
                            ? (newVal: unknown) => {
                                if (!isReadOnly && baselineValue) {
                                  const newObj = { ...baselineValue };
                                  newObj[key] = newVal;
                                  onBaselineChange(newObj);
                                }
                              }
                            : undefined
                        }
                        isReadOnly={isReadOnly}
                        confidence={fieldConfidence}
                        geometry={fieldGeometry}
                        onFieldFocus={onFieldFocus}
                        onFieldPathSelect={onFieldPathSelect}
                        onFieldDoubleClick={onFieldDoubleClick}
                        path={[...path, key]}
                        explainabilityInfo={explainabilityInfo}
                        mergedConfig={mergedConfig}
                        baselineValue={nestedBaselineValue}
                        showComparison={showComparison}
                        evaluationResults={evaluationResults}
                        sectionId={sectionId}
                        collapsedPaths={collapsedPaths}
                        onToggleCollapse={onToggleCollapse}
                        filterMode={filterMode}
                        displayPath={[...displayPath, fieldKey]}
                        predictionChanges={predictionChanges}
                        baselineChanges={baselineChanges}
                      />
                    );
                  })}
                </SpaceBetween>
              </ExtBox>
            )}
          </ExtBox>
        );
      }

      case 'null':
        // Handle null values - make them editable like other field types
        return (
          <div
            /* Only a double-click, which is a mouse-only gesture by definition and
               duplicates nothing: every value row below carries its own click
               handler, and the locate control carries the keyboard path. */
            onDoubleClick={handleDoubleClick}
            /* Deliberately not role="button"/tabIndex: this div wraps a FormField
               containing a text input and, in comparison mode, a copy button.
               ARIA forbids interactive descendants of a `button`, and screen
               readers computed the wrapper's name from its own subtree — so every
               field announced as a button named after its label, then again as the
               textbox with the same label. The click handler stays as a mouse
               convenience; keyboard and AT users get the explicit locate control
               below, which works in read-only mode too (where there is no input to
               focus) and is only offered when the field actually has a bounding box
               to show. */
            {...({ 'data-field-path': buildComparisonKey(path) || undefined } as Record<string, string | undefined>)}
            style={{
              cursor: geometry ? 'pointer' : 'default',
              backgroundColor: hasMismatch && !hasLocalEdit ? 'rgba(255, 153, 0, 0.05)' : 'transparent',
              padding: '4px',
              borderRadius: '4px',
              borderLeft: hasMismatch && !hasLocalEdit ? '3px solid #ff9900' : '3px solid transparent',
            }}
          >
            <FormField
              label={
                <ExtBox>
                  <SpaceBetween direction="horizontal" size="xs">
                    <span>{fieldKey}:</span>
                    {isPredictionChanged && (
                      <ExtBox color="text-status-info" fontSize="body-s" fontWeight="bold">
                        ✏️ Edited
                      </ExtBox>
                    )}
                    {isBaselineChanged && !isPredictionChanged && (
                      <ExtBox color="text-status-warning" fontSize="body-s" fontWeight="bold">
                        ✏️ Baseline Edited
                      </ExtBox>
                    )}
                    {hasMismatch && !hasLocalEdit && (
                      <ExtBox color="text-status-warning" fontSize="body-s" fontWeight="bold">
                        ⚠ Mismatch
                      </ExtBox>
                    )}
                    {!hasMismatch && !hasLocalEdit && showComparison && baselineValue !== null && (
                      <ExtBox color="text-status-success" fontSize="body-s">
                        ✓ Match
                      </ExtBox>
                    )}
                  </SpaceBetween>
                  {/* Suppressed once a human has overwritten the value: the model
                      never produced this text, so its confidence says nothing about
                      it. Leaving it attached decorated hand-typed ground truth with
                      a score of, typically, 100%. */}
                  {confidenceInfo.hasConfidenceInfo && !hasLocalEdit && (
                    <ExtBox fontSize="body-s" padding={{ top: 'xxxs' }} color={confidenceColor} style={confidenceStyle}>
                      {confidenceInfo.displayMode === 'with-threshold'
                        ? `Confidence: ${((confidenceInfo.confidence ?? 0) * 100).toFixed(1)}% / Threshold: ${(
                            (confidenceInfo.confidenceThreshold ?? 0) * 100
                          ).toFixed(1)}%`
                        : `Confidence: ${((confidenceInfo.confidence ?? 0) * 100).toFixed(1)}%`}
                    </ExtBox>
                  )}
                  {confidenceInfo.hasConfidenceInfo && hasLocalEdit && (
                    <ExtBox fontSize="body-s" padding={{ top: 'xxxs' }} color="text-body-secondary">
                      Edited — the model&apos;s confidence no longer applies
                    </ExtBox>
                  )}
                  {showComparison && evalScore !== undefined && (
                    <ExtBox fontSize="body-s" padding={{ top: 'xxxs' }} color={hasMismatch ? 'text-status-warning' : 'text-status-success'}>
                      {`Eval Score: ${(evalScore * 100).toFixed(1)}%`}
                      {evalReason && ` - ${evalReason}`}
                    </ExtBox>
                  )}
                  {showComparison && (evalMethod || evalWeight !== undefined) && (
                    <ExtBox fontSize="body-s" padding={{ top: 'xxxs' }} color="text-body-secondary">
                      {evalMethod && `Method: ${evalMethod}`}
                      {evalMethod && evalWeight !== undefined && ' · '}
                      {evalWeight !== undefined && `Weight: ${evalWeight.toFixed(2)}`}
                    </ExtBox>
                  )}
                </ExtBox>
              }
            >
              <SpaceBetween size="xxs">
                <ExtBox onClick={handleClick} style={{ cursor: 'pointer' }}>
                  <SpaceBetween direction="horizontal" size="xxs" alignItems="center">
                    {/* Not "Predicted" once a human has replaced it — the label is a
                        provenance claim, and it was still asserting the model's. */}
                    <ExtBox fontSize="body-s" color="text-body-secondary">
                      {isPredictionChanged ? 'Your value:' : 'Predicted:'}
                    </ExtBox>
                    {isPredictionChanged && (
                      <ExtBox fontSize="body-s" color="text-status-info" fontWeight="bold">
                        ✏️
                      </ExtBox>
                    )}
                    {/* The only keyboard-reachable way to ask "where did this come
                        from?", and the only visible sign that asking is possible.
                        The whole field was a click target before, with no
                        affordance: invisible to sighted users, unreachable to AT
                        ones. */}
                    {geometry && (
                      <Button
                        variant="inline-icon"
                        iconName="search"
                        ariaLabel={`Show ${fieldKey} on the page`}
                        onClick={(e) => {
                          e.stopPropagation();
                          handleClick();
                        }}
                      />
                    )}
                  </SpaceBetween>
                  <div
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: '4px',
                      pointerEvents: isReadOnly ? 'none' : 'auto',
                      borderLeft: isPredictionChanged ? '3px solid #0073bb' : '3px solid transparent',
                      paddingLeft: '4px',
                      backgroundColor: isPredictionChanged ? 'rgba(0, 115, 187, 0.08)' : 'transparent',
                      borderRadius: '2px',
                    }}
                  >
                    <div style={{ flex: 1 }}>
                      {isReadOnly ? (
                        <div
                          style={{
                            backgroundColor: '#e9ebed',
                            border: '1px solid #d5dbdb',
                            borderRadius: '4px',
                            minHeight: '32px',
                            display: 'flex',
                            alignItems: 'center',
                            fontStyle: 'italic',
                            color: '#545b64',
                            padding: '4px 8px',
                            fontSize: '14px',
                          }}
                        >
                          null
                        </div>
                      ) : (
                        <Input
                          value=""
                          onChange={({ detail }) => {
                            // Convert empty string back to null, otherwise use the entered value
                            onChange(detail.value === '' ? null : detail.value);
                          }}
                          onFocus={handleFocus}
                          placeholder="null (enter value to change)"
                        />
                      )}
                    </div>
                    {!isReadOnly && showComparison && (
                      <Button
                        variant="inline-icon"
                        iconName="arrow-down"
                        ariaLabel="Copy predicted value to baseline"
                        onClick={(e) => {
                          e.stopPropagation();
                          if (onBaselineChange) {
                            onBaselineChange(value);
                          }
                        }}
                      />
                    )}
                  </div>
                </ExtBox>
                {showComparison && (
                  <ExtBox onClick={handleClick} style={{ cursor: 'pointer' }}>
                    <SpaceBetween direction="horizontal" size="xxs" alignItems="center">
                      <ExtBox fontSize="body-s" color="text-body-secondary">
                        Expected (baseline):
                      </ExtBox>
                      {isBaselineChanged && (
                        <ExtBox fontSize="body-s" color="text-status-warning" fontWeight="bold">
                          ✏️
                        </ExtBox>
                      )}
                    </SpaceBetween>
                    <div
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: '4px',
                        pointerEvents: isReadOnly ? 'none' : 'auto',
                        borderLeft: isBaselineChanged ? '3px solid #ff9900' : '3px solid transparent',
                        paddingLeft: '4px',
                        backgroundColor: isBaselineChanged ? 'rgba(255, 153, 0, 0.08)' : 'transparent',
                        borderRadius: '2px',
                      }}
                    >
                      <div style={{ flex: 1 }}>
                        {isReadOnly ? (
                          <div
                            style={{
                              backgroundColor: '#e9ebed',
                              border: '1px solid #d5dbdb',
                              borderRadius: '4px',
                              minHeight: '32px',
                              display: 'flex',
                              alignItems: 'center',
                              fontStyle: baselineValue === null ? 'italic' : 'normal',
                              color: baselineValue === null ? '#545b64' : '#16191f',
                              padding: '4px 8px',
                              fontSize: '14px',
                            }}
                          >
                            {baselineValue === null ? 'null' : String(baselineValue)}
                          </div>
                        ) : (
                          <Input
                            value={baselineValue === null ? '' : String(baselineValue)}
                            onChange={({ detail }) => {
                              if (onBaselineChange) {
                                // Convert empty string back to null, otherwise use the entered value
                                onBaselineChange(detail.value === '' ? null : detail.value);
                              }
                            }}
                            placeholder={baselineValue === null ? 'null (enter value to change)' : undefined}
                          />
                        )}
                      </div>
                      {!isReadOnly && (
                        <Button
                          variant="inline-icon"
                          iconName="arrow-up"
                          ariaLabel="Copy baseline value to predicted"
                          onClick={(e) => {
                            e.stopPropagation();
                            onChange(baselineValue);
                          }}
                        />
                      )}
                    </div>
                  </ExtBox>
                )}
              </SpaceBetween>
            </FormField>
          </div>
        );

      case 'array': {
        // Look for array-level eval result (e.g., for checks array)
        let arrayEvalResult = null;
        if (showComparison && evaluationResults?.section_results) {
          let sectionResult = evaluationResults.section_results.find(
            (sr: Record<string, unknown>) => String(sr.section_id) === String(sectionId),
          );
          if (!sectionResult && evaluationResults.section_results.length === 1) {
            sectionResult = evaluationResults.section_results[0];
          }
          if (sectionResult?.attributes?.length > 0) {
            // Look for top-level attribute that matches our array name
            const attr = sectionResult.attributes.find(
              (a: Record<string, unknown>) =>
                a.name === fieldKey || (a.name as string)?.toLowerCase() === fieldKey?.toLowerCase(),
            );
            if (attr) {
              arrayEvalResult = {
                matched: attr.matched,
                score: attr.score,
                reason: attr.reason,
                evaluationMethod: attr.evaluation_method,
              };
            }
          }
        }

        return (
          <ExtBox
            padding="xs"
            style={{
              backgroundColor: arrayEvalResult && !arrayEvalResult.matched ? 'rgba(255, 153, 0, 0.08)' : 'transparent',
              borderRadius: '4px',
            }}
          >
            <ExtBox fontSize="body-m" fontWeight="bold" padding="xxxs" onFocus={handleFocus}>
              <SpaceBetween direction="horizontal" size="xs">
                {onToggleCollapse && (
                  <span
                    onClick={() => onToggleCollapse(pathKey)}
                    onKeyDown={(e) => e.key === 'Enter' && onToggleCollapse(pathKey)}
                    role="button"
                    tabIndex={0}
                    style={{ cursor: 'pointer', userSelect: 'none' }}
                  >
                    {isCollapsed ? '▶' : '▼'}
                  </span>
                )}
                <span>
                  {label} ({value.length} items)
                </span>
                {arrayEvalResult && !arrayEvalResult.matched && (
                  <ExtBox color="text-status-warning" fontSize="body-s">
                    ⚠
                  </ExtBox>
                )}
                {arrayEvalResult && arrayEvalResult.matched && showComparison && (
                  <ExtBox color="text-status-success" fontSize="body-s">
                    ✓
                  </ExtBox>
                )}
              </SpaceBetween>
              {showComparison && arrayEvalResult && (
                <ExtBox fontSize="body-s" color={arrayEvalResult.matched ? 'text-status-success' : 'text-status-warning'}>
                  {`List Score: ${(arrayEvalResult.score * 100).toFixed(1)}%`}
                  {arrayEvalResult.reason && ` - ${arrayEvalResult.reason}`}
                  {arrayEvalResult.evaluationMethod && (
                    <ExtBox fontSize="body-s" color="text-body-secondary">
                      Method: {arrayEvalResult.evaluationMethod}
                    </ExtBox>
                  )}
                </ExtBox>
              )}
            </ExtBox>
            {!isCollapsed && (
              <ExtBox padding={{ left: 'l' }}>
                <SpaceBetween size="xs">
                  {value.map((item: unknown, index: number) => {
                    // Create a stable unique key for each array item
                    const itemKey = `arr-${fieldKey}-${path.join('.')}-${index}`;

                    // Extract confidence and geometry for array items
                    let itemConfidence;
                    let itemGeometry;

                    // Try to get from explainability_info if available
                    if (explainabilityInfo && Array.isArray(explainabilityInfo)) {
                      const [firstExplainabilityItem] = explainabilityInfo;

                      // Handle nested structure - navigate to the array field first
                      let arrayFieldInfo = firstExplainabilityItem;
                      path.forEach((pathPart: string | number) => {
                        if (arrayFieldInfo && typeof arrayFieldInfo === 'object' && (arrayFieldInfo as Record<string, unknown>)[pathPart as string]) {
                          // nosemgrep: javascript.lang.security.audit.prototype-pollution.prototype-pollution-loop
                          arrayFieldInfo = arrayFieldInfo[pathPart];
                        } else {
                          arrayFieldInfo = null;
                        }
                      });

                      // For arrays, the explainability info structure can be:
                      // 1. An array where each element has confidence/geometry (e.g., ENDORSEMENTS, RESTRICTIONS)
                      // 2. An object with nested structure
                      if (arrayFieldInfo && Array.isArray(arrayFieldInfo) && arrayFieldInfo[index]) {
                        const itemInfo = arrayFieldInfo[index];
                        if (itemInfo) {
                          itemConfidence = itemInfo.confidence;

                          // Extract geometry
                          if (itemInfo.geometry && Array.isArray(itemInfo.geometry) && itemInfo.geometry.length > 0) {
                            const geomData = itemInfo.geometry[0];
                            if (geomData.boundingBox && geomData.page !== undefined) {
                              itemGeometry = {
                                boundingBox: geomData.boundingBox,
                                page: geomData.page,
                                vertices: geomData.vertices,
                              };
                            }
                          }
                        }
                      }
                    }

                    // Get baseline value for this array item
                    const arrayBaselineValue =
                      showComparison && baselineValue !== null && Array.isArray(baselineValue) ? baselineValue[index] : null;

                    return (
                      <FormFieldRenderer
                        key={itemKey}
                        fieldKey={`[${index}]`}
                        value={item}
                        onChange={(newVal: unknown) => {
                          if (!isReadOnly) {
                            const newArray = [...value];
                            newArray[index] = newVal;
                            onChange(newArray);
                          }
                        }}
                        onBaselineChange={
                          onBaselineChange
                            ? (newVal: unknown) => {
                                if (!isReadOnly && baselineValue && Array.isArray(baselineValue)) {
                                  const newArray = [...baselineValue];
                                  newArray[index] = newVal;
                                  onBaselineChange(newArray);
                                }
                              }
                            : undefined
                        }
                        isReadOnly={isReadOnly}
                        confidence={itemConfidence}
                        geometry={itemGeometry}
                        onFieldFocus={onFieldFocus}
                        onFieldPathSelect={onFieldPathSelect}
                        onFieldDoubleClick={onFieldDoubleClick}
                        path={[...path, index]}
                        explainabilityInfo={explainabilityInfo}
                        mergedConfig={mergedConfig}
                        baselineValue={arrayBaselineValue}
                        showComparison={showComparison}
                        evaluationResults={evaluationResults}
                        sectionId={sectionId}
                        collapsedPaths={collapsedPaths}
                        onToggleCollapse={onToggleCollapse}
                        filterMode={filterMode}
                        displayPath={[...displayPath, fieldKey]}
                        predictionChanges={predictionChanges}
                        baselineChanges={baselineChanges}
                      />
                    );
                  })}
                </SpaceBetween>
              </ExtBox>
            )}
          </ExtBox>
        );
      }

      default:
        return (
          <div
            /* Only a double-click, which is a mouse-only gesture by definition and
               duplicates nothing: every value row below carries its own click
               handler, and the locate control carries the keyboard path. */
            onDoubleClick={handleDoubleClick}
            /* See the note on the string case: not a button, because it contains one. */
            {...({ 'data-field-path': buildComparisonKey(path) || undefined } as Record<string, string | undefined>)}
            style={{ cursor: geometry ? 'pointer' : 'default' }}
          >
            <FormField
              label={
                <ExtBox>
                  {fieldKey}:
                  {/* Suppressed once a human has overwritten the value: the model
                      never produced this text, so its confidence says nothing about
                      it. Leaving it attached decorated hand-typed ground truth with
                      a score of, typically, 100%. */}
                  {confidenceInfo.hasConfidenceInfo && !hasLocalEdit && (
                    <ExtBox fontSize="body-s" padding={{ top: 'xxxs' }} color={confidenceColor} style={confidenceStyle}>
                      {confidenceInfo.displayMode === 'with-threshold'
                        ? `Confidence: ${((confidenceInfo.confidence ?? 0) * 100).toFixed(1)}% / Threshold: ${(
                            (confidenceInfo.confidenceThreshold ?? 0) * 100
                          ).toFixed(1)}%`
                        : `Confidence: ${((confidenceInfo.confidence ?? 0) * 100).toFixed(1)}%`}
                    </ExtBox>
                  )}
                  {confidenceInfo.hasConfidenceInfo && hasLocalEdit && (
                    <ExtBox fontSize="body-s" padding={{ top: 'xxxs' }} color="text-body-secondary">
                      Edited — the model&apos;s confidence no longer applies
                    </ExtBox>
                  )}
                </ExtBox>
              }
              /* Not inside `label`: a label may not contain interactive content.
                 This is the fallback field shape, which has no value header row to
                 hang the locate control off. */
              secondaryControl={
                geometry ? (
                  <Button
                    variant="inline-icon"
                    iconName="search"
                    ariaLabel={`Show ${fieldKey} on the page`}
                    onClick={(e) => {
                      e.stopPropagation();
                      handleClick();
                    }}
                  />
                ) : undefined
              }
            >
              {isReadOnly ? (
                <div
                  style={{
                    backgroundColor: '#e9ebed',
                    border: '1px solid #d5dbdb',
                    borderRadius: '4px',
                    minHeight: '32px',
                    display: 'flex',
                    alignItems: 'center',
                    color: '#16191f',
                    padding: '4px 8px',
                    fontSize: '14px',
                  }}
                >
                  {String(value)}
                </div>
              ) : (
                <Input value={String(value)} onChange={({ detail }) => onChange(detail.value)} onFocus={handleFocus} />
              )}
            </FormField>
          </div>
        );
    }
  },
);

FormFieldRenderer.displayName = 'FormFieldRenderer';

export { buildComparisonKey, findNestedComparisonDetail };
export default FormFieldRenderer;
