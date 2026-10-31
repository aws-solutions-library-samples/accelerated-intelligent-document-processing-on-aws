// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Utility functions for handling confidence threshold alerts
 */

interface MergedConfig {
  // v0.6: HITL threshold moved to the top-level `hitl` block. `assessment`
  // retained for pre-migration configs / backward compatibility.
  hitl?: {
    enabled?: boolean;
    confidence_threshold?: number | string;
  };
  assessment?: {
    hitl_confidence_score?: string;
    default_confidence_threshold?: number;
  };
}

interface ConfidenceFieldData {
  confidence?: number;
  confidence_threshold?: number;
  [key: string]: unknown;
}

interface FieldBelowThreshold {
  fieldName: string;
  fieldPath: string;
  confidence: number;
  confidenceThreshold: number;
}

interface ConfidenceThresholdAlert {
  attributeName: string;
  confidence: number;
  confidenceThreshold: number;
}

interface DocumentSection {
  explainabilityData?: Record<string, unknown>;
  Output?: Record<string, unknown>;
  ConfidenceThresholdAlerts?: ConfidenceThresholdAlert[];
  Id?: string;
}

interface FieldHighlightInfo {
  shouldHighlight: boolean;
  confidence?: number;
  confidenceThreshold?: number;
  alert?: ConfidenceThresholdAlert;
}

interface FieldConfidenceInfo {
  hasConfidenceInfo: boolean;
  /**
   * Whether `explainability_info` holds an entry for this field at all,
   * regardless of whether it carried a usable confidence number.
   *
   * Distinct from `hasConfidenceInfo` because the two answer different
   * questions, and a caller that needs "was anything recorded about this
   * field?" gets the wrong answer from the other one. A field assessed at a
   * finer granularity — a scalar value whose entry is an object of sub-field
   * confidences — has an entry and no number of its own.
   */
  hasEntry?: boolean;
  confidence?: number;
  confidenceThreshold?: number;
  isAboveThreshold?: boolean;
  shouldHighlight?: boolean;
  textColor?: string;
  displayMode?: string;
}

export const getHitlConfidenceThreshold = (mergedConfig: MergedConfig | null): number => {
  // v0.6: threshold lives in the top-level `hitl` block as a 0.0-1.0 decimal
  // (was assessment.default_confidence_threshold). Fall back to the legacy
  // assessment.* keys for configs read before migration completes.
  const hitl = mergedConfig?.hitl as { confidence_threshold?: number | string } | undefined;
  if (hitl && hitl.confidence_threshold !== undefined && hitl.confidence_threshold !== null) {
    const t = parseFloat(String(hitl.confidence_threshold));
    if (!Number.isNaN(t)) {
      // Stored as a 0.0-1.0 decimal; tolerate a legacy 1-100 percentage.
      return t > 1 ? t / 100 : t;
    }
  }

  const legacy = mergedConfig?.assessment as
    | { default_confidence_threshold?: number | string; hitl_confidence_score?: number | string }
    | undefined;
  const legacyVal = legacy?.default_confidence_threshold ?? legacy?.hitl_confidence_score;
  if (legacyVal !== undefined && legacyVal !== null) {
    const t = parseFloat(String(legacyVal));
    if (!Number.isNaN(t)) {
      return t > 1 ? t / 100 : t;
    }
  }

  return 0.8; // Default threshold of 80%
};

/**
 * Enhanced function to find explainability data in various possible locations within a section
 * @param {Object} section - Document section
 * @returns {Object|null} Explainability data if found, null otherwise
 */
const findExplainabilityData = (section: DocumentSection | null): Record<string, unknown> | null => {
  if (!section || typeof section !== 'object') {
    return null;
  }

  // Check direct explainabilityData property
  if (section.explainabilityData) {
    return section.explainabilityData;
  }

  // Check Output.explainabilityData
  if (section.Output && section.Output.explainabilityData) {
    return section.Output.explainabilityData as Record<string, unknown>;
  }

  // Check if Output itself contains confidence data
  if (section.Output && typeof section.Output === 'object') {
    // Look for any key that might contain explainability data
    const outputKeys = Object.keys(section.Output);

    // Check for keys that might contain explainability data
    const explainabilityKey = outputKeys.find(
      (key) =>
        key.toLowerCase().includes('explainability') ||
        key.toLowerCase().includes('confidence') ||
        key.toLowerCase().includes('assessment'),
    );

    if (explainabilityKey && section.Output[explainabilityKey]) {
      return section.Output[explainabilityKey] as Record<string, unknown>;
    }

    // If no specific explainability key found, check if Output contains field-level confidence data
    // Look for nested objects that might contain confidence scores
    const outputKeysArray = Object.keys(section.Output);

    for (const key of outputKeysArray) {
      const value = section.Output[key];
      if (value && typeof value === 'object' && !Array.isArray(value)) {
        // Check if this object contains fields with confidence scores
        const valueRecord = value as Record<string, unknown>;
        const hasConfidenceData = Object.values(valueRecord).some(
          (fieldValue) =>
            fieldValue && typeof fieldValue === 'object' && typeof (fieldValue as Record<string, unknown>).confidence === 'number',
        );

        if (hasConfidenceData) {
          return valueRecord;
        }
      }
    }
  }

  return null;
};

/** Marks "no extracted values were supplied", as distinct from an absent value. */
export const VALUES_UNKNOWN: unique symbol = Symbol('values-unknown');
type ExtractedValues = unknown | typeof VALUES_UNKNOWN;

/**
 * True when an extracted value holds something extraction actually found — the
 * backend's `has_extracted_value` (#1527): null, a blank string and an empty
 * container are absent, and so is a container of nothing but those.
 */
export const hasExtractedValue = (value: unknown): boolean => {
  if (value === null || value === undefined) return false;
  if (typeof value === 'string') return value.trim() !== '';
  if (Array.isArray(value)) return value.some(hasExtractedValue);
  if (typeof value === 'object') return Object.values(value as Record<string, unknown>).some(hasExtractedValue);
  return true;
};

/**
 * The value under `key`, mirroring the backend walk: a populated scalar under
 * an assessment node with children is a value the model split into fragments,
 * so each fragment inherits it (and counts as present).
 */
const childValue = (values: ExtractedValues, key: string | number): ExtractedValues => {
  if (values === VALUES_UNKNOWN) return VALUES_UNKNOWN;
  if (typeof key === 'number') {
    if (Array.isArray(values)) return values[key];
  } else if (values && typeof values === 'object' && !Array.isArray(values)) {
    return (values as Record<string, unknown>)[key];
  }
  if ((values && typeof values === 'object') || !hasExtractedValue(values)) return undefined;
  return values;
};

/** The section's extracted values (`inference_result`), when it carries them. */
const findExtractedValues = (section: DocumentSection | null): ExtractedValues => {
  const result = section?.Output?.inference_result;
  return result && typeof result === 'object' ? result : VALUES_UNKNOWN;
};

/**
 * Get fields that are below the HITL confidence threshold from explainability data
 * @param {Object} explainabilityData - Explainability data containing confidence scores
 * @param {number} hitlThreshold - HITL confidence threshold (0.0-1.0)
 * @param {string} path - Current path in the data structure
 * @param values - The extracted values at the same position. A field whose value
 *   is absent is not an alert (#1527), as on the backend; when the values are
 *   not known (`VALUES_UNKNOWN`, the default) nothing is excluded.
 * @returns {Array} Array of field objects with confidence below threshold
 */
export const getFieldsBelowThreshold = (
  explainabilityData: Record<string, unknown> | null,
  hitlThreshold: number,
  path: string = '',
  values: ExtractedValues = VALUES_UNKNOWN,
): FieldBelowThreshold[] => {
  const fieldsBelow: FieldBelowThreshold[] = [];

  if (!explainabilityData || typeof explainabilityData !== 'object') {
    return fieldsBelow;
  }

  Object.entries(explainabilityData).forEach(([fieldName, fieldData]) => {
    if (fieldData && typeof fieldData === 'object') {
      const { confidence } = fieldData as ConfidenceFieldData;
      const fieldValues = childValue(values, fieldName);

      if (typeof confidence === 'number') {
        const fieldPath = path ? `${path}.${fieldName}` : fieldName;
        const absent = fieldValues !== VALUES_UNKNOWN && !hasExtractedValue(fieldValues);

        if (confidence < hitlThreshold && !absent) {
          fieldsBelow.push({
            fieldName,
            fieldPath,
            confidence,
            confidenceThreshold: hitlThreshold,
          });
        }
      }

      // Recursively check nested objects and arrays
      if (Array.isArray(fieldData)) {
        fieldData.forEach((item, index) => {
          if (item && typeof item === 'object') {
            const nestedPath = path ? `${path}.${fieldName}[${index}]` : `${fieldName}[${index}]`;
            const nestedFields = getFieldsBelowThreshold(item, hitlThreshold, nestedPath, childValue(fieldValues, index));
            fieldsBelow.push(...nestedFields);
          }
        });
      } else if (typeof fieldData === 'object' && fieldData !== null && !('confidence' in fieldData)) {
        // This is a nested object without confidence, recurse into it
        const nestedPath = path ? `${path}.${fieldName}` : fieldName;
        const nestedFields = getFieldsBelowThreshold(fieldData as Record<string, unknown>, hitlThreshold, nestedPath, fieldValues);
        fieldsBelow.push(...nestedFields);
      }
    }
  });

  return fieldsBelow;
};

/**
 * Calculate the total count of confidence threshold alerts for a document using dynamic threshold
 * @param {Array} sections - Array of document sections
 * @param {Object} mergedConfig - Merged configuration object
 * @returns {number} Total count of confidence threshold alerts
 */
export const getDocumentConfidenceAlertCount = (sections: DocumentSection[] | null, mergedConfig: MergedConfig | null = null): number => {
  if (!sections || !Array.isArray(sections)) {
    return 0;
  }

  // If mergedConfig is provided, use dynamic threshold calculation
  if (mergedConfig) {
    const hitlThreshold = getHitlConfidenceThreshold(mergedConfig);

    return sections.reduce((total, section) => {
      const explainabilityData = findExplainabilityData(section);

      if (explainabilityData) {
        const fieldsBelow = getFieldsBelowThreshold(explainabilityData, hitlThreshold, '', findExtractedValues(section));
        return total + fieldsBelow.length;
      }

      // Fallback to existing ConfidenceThresholdAlerts if no explainability data
      if (section.ConfidenceThresholdAlerts && Array.isArray(section.ConfidenceThresholdAlerts)) {
        return total + section.ConfidenceThresholdAlerts.length;
      }

      return total;
    }, 0);
  }

  // Fallback to original logic for backward compatibility
  return sections.reduce((total, section) => {
    if (section.ConfidenceThresholdAlerts && Array.isArray(section.ConfidenceThresholdAlerts)) {
      return total + section.ConfidenceThresholdAlerts.length;
    }
    return total;
  }, 0);
};

/**
 * Calculate the count of confidence threshold alerts for a specific section using dynamic threshold
 * @param {Object} section - Document section
 * @param {Object} mergedConfig - Merged configuration object
 * @returns {number} Count of confidence threshold alerts for the section
 */
export const getSectionConfidenceAlertCount = (section: DocumentSection | null, mergedConfig: MergedConfig | null = null): number => {
  if (!section) {
    return 0;
  }

  // If mergedConfig is provided, use dynamic threshold calculation
  if (mergedConfig) {
    const hitlThreshold = getHitlConfidenceThreshold(mergedConfig);
    const explainabilityData = findExplainabilityData(section);

    if (explainabilityData) {
      const fieldsBelow = getFieldsBelowThreshold(explainabilityData, hitlThreshold, '', findExtractedValues(section));
      return fieldsBelow.length;
    }
  }

  // Fallback to existing ConfidenceThresholdAlerts
  if (!section.ConfidenceThresholdAlerts || !Array.isArray(section.ConfidenceThresholdAlerts)) {
    return 0;
  }
  return section.ConfidenceThresholdAlerts.length;
};

/**
 * Get detailed confidence alerts for a section using dynamic threshold
 * @param {Object} section - Document section
 * @param {Object} mergedConfig - Merged configuration object
 * @returns {Array} Array of detailed confidence alert objects
 */
export const getSectionConfidenceAlerts = (
  section: DocumentSection | null,
  mergedConfig: MergedConfig | null = null,
): FieldBelowThreshold[] => {
  if (!section) {
    return [];
  }

  // If mergedConfig is provided, use dynamic threshold calculation
  if (mergedConfig) {
    const hitlThreshold = getHitlConfidenceThreshold(mergedConfig);
    const explainabilityData = findExplainabilityData(section);

    if (explainabilityData) {
      const fieldsBelow = getFieldsBelowThreshold(explainabilityData, hitlThreshold, '', findExtractedValues(section));
      return fieldsBelow;
    }
  }

  // Fallback to existing ConfidenceThresholdAlerts
  if (!section.ConfidenceThresholdAlerts || !Array.isArray(section.ConfidenceThresholdAlerts)) {
    return [];
  }

  return section.ConfidenceThresholdAlerts.map((alert) => ({
    fieldName: alert.attributeName,
    fieldPath: alert.attributeName,
    confidence: alert.confidence,
    confidenceThreshold: alert.confidenceThreshold,
  }));
};

/**
 * Check if a field should be highlighted due to low confidence
 * @param {string} fieldName - Name of the field
 * @param {number} fieldConfidence - Confidence value for the field
 * @param {Array} confidenceThresholdAlerts - Array of confidence threshold alerts
 * @returns {Object} Object with highlight flag and threshold info
 */
export const getFieldHighlightInfo = (
  fieldName: string,
  fieldConfidence: number | undefined,
  confidenceThresholdAlerts: ConfidenceThresholdAlert[] | null,
): FieldHighlightInfo => {
  if (!confidenceThresholdAlerts || !Array.isArray(confidenceThresholdAlerts) || !fieldName) {
    return { shouldHighlight: false };
  }

  const alertMatch = confidenceThresholdAlerts.find((alert) => alert.attributeName === fieldName);

  if (alertMatch) {
    return {
      shouldHighlight: true,
      confidence: alertMatch.confidence,
      confidenceThreshold: alertMatch.confidenceThreshold,
      alert: alertMatch,
    };
  }

  return { shouldHighlight: false };
};

/**
 * Get confidence information for a field from explainability data with dynamic threshold support
 * @param {string} fieldName - Name of the field
 * @param {Object} explainabilityInfo - Explainability info object containing confidence data for all fields
 * @param {Array} path - Optional path array for nested fields (e.g., ['FederalTaxes', 0, 'YTD'])
 * @param {Object} mergedConfig - Merged configuration object for dynamic threshold
 * @returns {Object} Object with confidence info and display properties
 */

/**
 * Aggregate confidence from the child assessments of a decomposed-string field.
 *
 * When the assessment LLM splits a plain-string field into sub-keyed child
 * assessments, the field object has no top-level `confidence` but each direct child
 * does. Returns the minimum child confidence (worst case — the value that should
 * drive review highlighting) together with that child's threshold, or null when no
 * direct child carries a numeric confidence.
 */
const aggregateChildConfidence = (fieldData: Record<string, unknown>): { confidence: number; confidenceThreshold?: number } | null => {
  let minConfidence: number | null = null;
  let thresholdForMin: number | undefined;

  for (const value of Object.values(fieldData)) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      continue;
    }
    const child = value as ConfidenceFieldData;
    if (typeof child.confidence === 'number' && (minConfidence === null || child.confidence < minConfidence)) {
      minConfidence = child.confidence;
      thresholdForMin = typeof child.confidence_threshold === 'number' ? child.confidence_threshold : undefined;
    }
  }

  if (minConfidence === null) {
    return null;
  }
  return { confidence: minConfidence, confidenceThreshold: thresholdForMin };
};

export const getFieldConfidenceInfo = (
  fieldName: string,
  explainabilityInfo: Record<string, unknown> | Record<string, unknown>[] | null,
  path: (string | number)[] = [],
  mergedConfig: MergedConfig | null = null,
): FieldConfidenceInfo => {
  if (!explainabilityInfo || !fieldName) {
    return { hasConfidenceInfo: false, hasEntry: false };
  }

  // explainabilityInfo is typically an array, get the first element
  const explainabilityData = Array.isArray(explainabilityInfo) ? explainabilityInfo[0] : explainabilityInfo;

  if (!explainabilityData || typeof explainabilityData !== 'object') {
    return { hasConfidenceInfo: false, hasEntry: false };
  }

  // Navigate to the nested location in explainabilityData using the path
  let currentExplainabilityData: Record<string, unknown> = explainabilityData;

  // Traverse the path to find the nested explainability data

  for (const pathSegment of path) {
    if (currentExplainabilityData && typeof currentExplainabilityData === 'object') {
      if (Array.isArray(currentExplainabilityData)) {
        // Handle array indices
        const index = parseInt(String(pathSegment), 10);
        if (!Number.isNaN(index) && index >= 0 && index < currentExplainabilityData.length) {
          // nosemgrep: javascript.lang.security.audit.prototype-pollution.prototype-pollution-loop - Controlled data source, input validation performed upstream
          currentExplainabilityData = currentExplainabilityData[index] as Record<string, unknown>;
        } else {
          return { hasConfidenceInfo: false, hasEntry: false };
        }
      } else {
        // Handle object properties
        // nosemgrep: javascript.lang.security.audit.prototype-pollution.prototype-pollution-loop - Controlled data source, input validation performed upstream
        currentExplainabilityData = currentExplainabilityData[pathSegment] as Record<string, unknown>;
      }
    } else {
      return { hasConfidenceInfo: false, hasEntry: false };
    }
  }

  // Now look for the field in the current explainability data location
  if (!currentExplainabilityData || typeof currentExplainabilityData !== 'object') {
    return { hasConfidenceInfo: false, hasEntry: false };
  }

  /**
   * An array item's own entry is the node the path already reached.
   *
   * The renderer names a primitive list item `[0]` and passes the index in the
   * path, so the navigation above has already arrived at the element; looking
   * for a child key literally called "[0]" finds nothing, and the field reads as
   * having no assessment entry when it has one. Array items that are objects are
   * unaffected — their leaves are named fields resolved the usual way.
   */
  const isArrayItemKey = /^\[\d+\]$/.test(fieldName);
  const fieldData = (isArrayItemKey ? currentExplainabilityData : currentExplainabilityData[fieldName]) as
    | ConfidenceFieldData
    | null
    | undefined;
  if (!fieldData || typeof fieldData !== 'object') {
    return { hasConfidenceInfo: false, hasEntry: false };
  }

  let { confidence } = fieldData;
  let confidenceThreshold = fieldData.confidence_threshold;

  // Some assessments decompose a single string field into sub-keyed child
  // assessments (e.g. "Insurance Company": { "Fake Insurance Co": {confidence...},
  // "650 Davis Street": {confidence...} }) instead of returning a flat
  // {confidence} on the field itself. In that case aggregate the children: use the
  // *minimum* child confidence (worst case is the right signal for review/alerts)
  // and the matching child's threshold.
  if (typeof confidence !== 'number') {
    const aggregated = aggregateChildConfidence(fieldData);
    if (aggregated) {
      confidence = aggregated.confidence;
      if (confidenceThreshold === undefined || confidenceThreshold === null) {
        confidenceThreshold = aggregated.confidenceThreshold;
      }
    }
  }

  // Check if we have confidence data
  if (typeof confidence !== 'number') {
    return { hasConfidenceInfo: false, hasEntry: true };
  }
  const hasConfidence = true;
  const hasEntry = true;

  // Use dynamic threshold from configuration if available and no field-specific threshold
  if (mergedConfig && (confidenceThreshold === undefined || confidenceThreshold === null)) {
    confidenceThreshold = getHitlConfidenceThreshold(mergedConfig);
  }

  const hasThreshold = typeof confidenceThreshold === 'number';

  // Case 1: Both confidence and threshold available
  if (hasConfidence && hasThreshold) {
    const isAboveThreshold = confidence >= confidenceThreshold!;
    return {
      hasConfidenceInfo: true,
      hasEntry,
      confidence,
      confidenceThreshold,
      isAboveThreshold,
      shouldHighlight: !isAboveThreshold,
      textColor: isAboveThreshold ? '#16794d' : '#d13313', // Green for good, red for poor
      displayMode: 'with-threshold',
    };
  }

  // Case 2: Only confidence available (no threshold)
  if (hasConfidence && !hasThreshold) {
    return {
      hasConfidenceInfo: true,
      hasEntry,
      confidence,
      confidenceThreshold: undefined,
      isAboveThreshold: undefined,
      shouldHighlight: false,
      textColor: '#000000', // Black font when no threshold to compare
      displayMode: 'confidence-only',
    };
  }

  // Case 3: Neither available (handled by the hasConfidence check above)
  return { hasConfidenceInfo: false, hasEntry: true };
};

/**
 * Get all confidence threshold alerts for a section as a map by attribute name
 * @param {Object} section - Document section
 * @returns {Object} Map of attribute names to alert objects
 */
export const getConfidenceAlertsMap = (section: DocumentSection | null): Record<string, ConfidenceThresholdAlert> => {
  if (!section || !section.ConfidenceThresholdAlerts || !Array.isArray(section.ConfidenceThresholdAlerts)) {
    return {};
  }

  const alertsMap: Record<string, ConfidenceThresholdAlert> = {};
  section.ConfidenceThresholdAlerts.forEach((alert: ConfidenceThresholdAlert) => {
    if (alert.attributeName) {
      alertsMap[alert.attributeName] = alert;
    }
  });

  return alertsMap;
};
