// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useState, useEffect } from 'react';
import { generateClient } from '../api/client-shim';
import { ConsoleLogger } from 'aws-amplify/utils';
import { getConfigVersion, updateConfiguration as updateConfigurationOp } from '../graphql/generated';
import { deepMerge } from '../utils/configUtils';
import { parseConfigurationData } from '../graphql/awsjson-parsers';

const client = generateClient();
const logger = new ConsoleLogger('useConfiguration');

/** Return type for the useConfiguration hook */
interface UseConfigurationReturn {
  schema: Record<string, unknown> | null;
  defaultConfig: Record<string, unknown> | null;
  customConfig: Record<string, unknown> | null;
  mergedConfig: Record<string, unknown> | null;
  loading: boolean;
  refreshing: boolean;
  error: string | null;
  fetchConfiguration: (fetchVersionName: string, silent?: boolean) => Promise<void>;
  updateConfiguration: (targetVersionName: string, newCustomConfig: unknown, description?: string | null) => Promise<boolean>;
  resetToDefault: (path: string) => { path: string; defaultValue: unknown } | false;
  isCustomized: (path: string) => boolean;
}

// Utility function to check if two values are numerically equivalent
// Handles cases where 5 and 5.0, or "5" and 5 should be considered equal
const areNumericValuesEqual = (val1: unknown, val2: unknown): boolean => {
  // If both are numbers, direct comparison
  if (typeof val1 === 'number' && typeof val2 === 'number') {
    return val1 === val2;
  }

  // Try to parse both as numbers
  const num1 = typeof val1 === 'number' ? val1 : parseFloat(val1 as string);
  const num2 = typeof val2 === 'number' ? val2 : parseFloat(val2 as string);

  // Both must be valid numbers for numeric comparison
  if (!Number.isNaN(num1) && !Number.isNaN(num2)) {
    return num1 === num2;
  }

  return false;
};

// Check if a value could be interpreted as a number
const isNumericValue = (val: unknown): boolean => {
  if (typeof val === 'number') return true;
  if (typeof val === 'string' && val.trim() !== '') {
    return !Number.isNaN(parseFloat(val)) && isFinite(Number(val));
  }
  return false;
};

/** Describes a JSON Schema property, used for recursive normalization of config values. */
interface SchemaProperty {
  type?: string;
  properties?: Record<string, SchemaProperty>;
  items?: SchemaProperty;
  [key: string]: unknown;
}

// Utility function to normalize boolean values from strings
const normalizeBooleans = (obj: Record<string, unknown>, schema: Record<string, unknown>): Record<string, unknown> => {
  if (!obj || !schema) return obj;

  const normalizeValue = (value: unknown, propertySchema: SchemaProperty): unknown => {
    // Handle boolean fields that might be strings
    if (propertySchema?.type === 'boolean') {
      if (typeof value === 'string') {
        if (value.toLowerCase() === 'true') return true;
        if (value.toLowerCase() === 'false') return false;
      }
      return value;
    }

    // Handle objects recursively
    if (value && typeof value === 'object' && !Array.isArray(value) && propertySchema?.properties) {
      const normalized = { ...(value as Record<string, unknown>) };
      Object.keys(normalized).forEach((key) => {
        if (propertySchema.properties![key]) {
          normalized[key] = normalizeValue(normalized[key], propertySchema.properties![key]);
        }
      });
      return normalized;
    }

    // Handle arrays
    if (Array.isArray(value) && propertySchema?.items) {
      return value.map((item: unknown) => normalizeValue(item, propertySchema.items!));
    }

    return value;
  };

  const normalized: Record<string, unknown> = { ...obj };
  const schemaProperties = (schema as SchemaProperty).properties;
  if (schemaProperties) {
    Object.keys(normalized).forEach((key) => {
      if (schemaProperties[key]) {
        normalized[key] = normalizeValue(normalized[key], schemaProperties[key]);
      }
    });
  }

  return normalized;
};

// Utility: Get value at path in nested object
const getValueAtPath = (obj: Record<string, unknown>, path: string): unknown => {
  if (!obj || !path) return undefined;
  const segments = path.split(/[.[\]]+/).filter(Boolean);
  return segments.reduce((acc: unknown, segment) => {
    if (acc === null || acc === undefined) return undefined;
    return (acc as Record<string, unknown>)[segment];
  }, obj as unknown);
};

// Utility: Set value at path in nested object (immutable)
const _setValueAtPath = (obj: Record<string, unknown>, path: string, value: unknown): Record<string, unknown> => {
  if (!obj || !path) return obj;
  const segments = path.split(/[.[\]]+/).filter(Boolean);
  const result = JSON.parse(JSON.stringify(obj)); // Deep clone

  let current: Record<string, unknown> = result;
  for (let i = 0; i < segments.length - 1; i += 1) {
    const segment = segments[i];
    if (!(segment in current)) {
      // Create intermediate object or array
      const nextSegment = segments[i + 1];
      current[segment] = /^\d+$/.test(nextSegment) ? [] : {};
    }
    // nosemgrep: javascript.lang.security.audit.prototype-pollution.prototype-pollution-loop
    current = current[segment] as Record<string, unknown>;
  }

  current[segments[segments.length - 1]] = value;
  return result;
};

// Utility: Remove value at path from nested object (immutable)
// Returns new object with the path removed, and cleans up empty parent objects
const _removeValueAtPath = (obj: Record<string, unknown>, path: string): Record<string, unknown> => {
  if (!obj || !path) return obj;
  const segments = path.split(/[.[\]]+/).filter(Boolean);
  const result = JSON.parse(JSON.stringify(obj)); // Deep clone

  // Helper to remove empty parent objects recursively
  const cleanupEmptyParents = (object: Record<string, unknown>, segs: string[], depth: number = 0): void => {
    if (depth >= segs.length - 1) {
      // At the target level, delete the key
      delete object[segs[depth]];
      return;
    }

    const segment = segs[depth];
    if (!(segment in object)) return;

    cleanupEmptyParents(object[segment] as Record<string, unknown>, segs, depth + 1);

    // If parent is now empty, delete it too
    if (typeof object[segment] === 'object' && Object.keys(object[segment] as Record<string, unknown>).length === 0) {
      delete object[segment];
    }
  };

  cleanupEmptyParents(result, segments);
  return result;
};

// Utility: Compute diff between two configs (returns only changes)
// Note: This only returns CHANGED values, never deletions
// Custom config is always complete, never has missing keys
const _getDiff = (oldConfig: Record<string, unknown>, newConfig: Record<string, unknown>): Record<string, unknown> => {
  const diff: Record<string, unknown> = {};

  const setDiffValue = (obj: Record<string, unknown>, path: string[], value: unknown): void => {
    let current: Record<string, unknown> = obj;
    for (let i = 0; i < path.length - 1; i += 1) {
      const segment = path[i];
      if (!(segment in current)) {
        current[segment] = {};
      }
      // nosemgrep: javascript.lang.security.audit.prototype-pollution.prototype-pollution-loop
      current = current[segment] as Record<string, unknown>;
    }
    current[path[path.length - 1]] = value;
  };

  const computeDiff = (oldObj: Record<string, unknown> | undefined, newObj: Record<string, unknown>, path: string[] = []): void => {
    // Only check for new or changed keys (no deletions)
    Object.keys(newObj).forEach((key) => {
      const newValue = newObj[key];
      const oldValue = oldObj ? oldObj[key] : undefined;
      const currentPath = [...path, key];

      // Nested objects - recurse
      if (
        newValue &&
        oldValue &&
        typeof newValue === 'object' &&
        typeof oldValue === 'object' &&
        !Array.isArray(newValue) &&
        !Array.isArray(oldValue)
      ) {
        computeDiff(oldValue as Record<string, unknown>, newValue as Record<string, unknown>, currentPath);
      }
      // Value changed or is new
      else if (JSON.stringify(newValue) !== JSON.stringify(oldValue)) {
        setDiffValue(diff, currentPath, newValue);
      }
    });

    // Note: We do NOT check for deleted keys
    // Custom config should always be complete
    // "Reset to default" means setting the default VALUE, not deleting the key
  };

  computeDiff(oldConfig, newConfig);
  return diff;
};

/**
 * Load one Configuration Profile's schema, default and custom configuration.
 *
 * `versionName` is fetched on mount and whenever it changes. Pass null while the
 * profile is not yet known to be one the caller may read: nothing is fetched and
 * `loading` stays true.
 */
const useConfiguration = (versionName: string | null): UseConfigurationReturn => {
  const [schema, setSchema] = useState<Record<string, unknown> | null>(null);
  const [defaultConfig, setDefaultConfig] = useState<Record<string, unknown> | null>(null);
  const [customConfig, setCustomConfig] = useState<Record<string, unknown> | null>(null);
  const [mergedConfig, setMergedConfig] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [refreshing, setRefreshing] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const fetchConfiguration = async (fetchVersionName: string, silent: boolean = false): Promise<void> => {
    // Use different loading states for initial load vs background refresh
    if (silent) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }
    setError(null);
    try {
      logger.debug('Fetching configuration for versionName:', fetchVersionName);
      const result = await client.graphql({
        query: getConfigVersion,
        variables: { versionName: fetchVersionName },
      });
      logger.debug('API response version', fetchVersionName, result);

      const response = result.data.getConfigVersion;

      if (!response?.success) {
        const errorMsg = response?.error?.message || 'Failed to load configuration';
        throw new Error(errorMsg);
      }

      const { Schema, Default, Custom } = response;

      // Log raw data types
      logger.debug('Raw data types:', {
        Schema: typeof Schema,
        Default: typeof Default,
        Custom: typeof Custom,
      });

      // Parse AWSJSON fields using typed parsers
      let schemaObj = parseConfigurationData(Schema as string) as Record<string, unknown>;
      const defaultObj = parseConfigurationData(Default as string) as Record<string, unknown>;
      let customObj = parseConfigurationData(Custom as string) as Record<string, unknown>;

      // Unwrap nested Schema object if present
      if (schemaObj && schemaObj.Schema) {
        schemaObj = schemaObj.Schema as Record<string, unknown>;
        logger.debug('Unwrapped nested Schema object');
      }

      // Use empty object if Custom parsing returned null
      if (!customObj) {
        logger.warn('Version configuration is empty or missing, using empty object');
        customObj = {};
      }

      // Debug the parsed objects
      logger.debug('Parsed schema:', schemaObj);
      logger.debug('Parsed default config:', defaultObj);
      logger.debug('Parsed version config:', customObj);

      // Validate the parsed objects
      if (!schemaObj || typeof schemaObj !== 'object') {
        throw new Error(`Invalid schema data structure ${typeof schemaObj}`);
      }

      if (!defaultObj || typeof defaultObj !== 'object') {
        throw new Error('Invalid default configuration data structure');
      }

      setSchema(schemaObj);

      // Normalize boolean values in both default and version configs
      const normalizedDefaultObj = normalizeBooleans(defaultObj, schemaObj);
      const normalizedCustomObj = normalizeBooleans(customObj, schemaObj);

      setDefaultConfig(normalizedDefaultObj);
      setCustomConfig(normalizedCustomObj);

      // IMPORTANT: Frontend merges Default + Custom for display
      // DESIGN PATTERN:
      // - Default = full stack baseline (from deployment)
      // - Custom = SPARSE DELTAS ONLY (only user-modified fields)
      // - mergedConfig = Default deep-updated with Custom = what we display
      //
      // This design allows:
      // - Stack upgrades to safely update Default without losing user customizations
      // - Empty Custom = all defaults (clean reset capability)
      // - User customizations survive stack deployments
      const activeConfig = deepMerge(normalizedDefaultObj, normalizedCustomObj);

      logger.debug('Merged configuration (Default + Custom deltas):', activeConfig);
      // Double check the classification and extraction sections
      if (activeConfig.classification) {
        logger.debug('Final classification data:', activeConfig.classification);
      }
      if (activeConfig.extraction) {
        logger.debug('Final extraction data:', activeConfig.extraction);
      }
      if (activeConfig.classes) {
        logger.debug('Final classes (JSON Schema) data:', activeConfig.classes);
      }
      setMergedConfig(activeConfig);
    } catch (err: unknown) {
      logger.error('Error fetching configuration', err);
      // Extract meaningful error message from various error shapes:
      // - Error instances: use .message
      // - Amplify GraphQL errors: { errors: [{ message: "..." }] }
      // - Plain objects: JSON.stringify to avoid [object Object]
      let errorMsg: string;
      if (err instanceof Error) {
        errorMsg = err.message;
      } else if (err && typeof err === 'object' && 'errors' in err && Array.isArray((err as Record<string, unknown>).errors)) {
        const gqlErrors = (err as Record<string, unknown>).errors as Array<Record<string, unknown>>;
        errorMsg = gqlErrors.map((e) => (e.message as string) || 'Unknown GraphQL error').join('; ');
      } else if (err && typeof err === 'object') {
        errorMsg = JSON.stringify(err);
      } else {
        errorMsg = String(err);
      }
      setError(`Failed to load configuration: ${errorMsg}`);
    } finally {
      if (silent) {
        setRefreshing(false);
      } else {
        setLoading(false);
      }
    }
  };

  const updateConfiguration = async (
    targetVersionName: string,
    newCustomConfig: unknown,
    description: string | null = null,
  ): Promise<boolean> => {
    setError(null);
    try {
      logger.debug('Updating config - versionName:', targetVersionName, 'description:', description, 'config:', newCustomConfig);

      // Make sure we have a valid object to update with
      const configToUpdate =
        !newCustomConfig || (typeof newCustomConfig === 'object' && Object.keys(newCustomConfig as Record<string, unknown>).length === 0)
          ? {} // Use empty object fallback
          : newCustomConfig;

      if (configToUpdate !== newCustomConfig) {
        logger.warn('Attempting to update with empty configuration, using {} as fallback');
      }

      // Ensure we're sending a JSON string
      const configString = typeof configToUpdate === 'string' ? configToUpdate : JSON.stringify(configToUpdate);

      logger.debug('Sending customConfig string for version', targetVersionName, ':', configString);

      const result = await client.graphql({
        query: updateConfigurationOp,
        variables: {
          versionName: targetVersionName,
          customConfig: configString,
          description,
        },
      });

      const response = result.data.updateConfiguration;

      if (!response?.success) {
        const errorMsg = response?.error?.message || 'Failed to update configuration';
        throw new Error(errorMsg);
      }

      // Refetch silently to ensure backend and frontend are in sync
      // Silent mode prevents loading state changes that cause re-renders
      // The component will handle rehydration without full re-render
      await fetchConfiguration(targetVersionName, true);

      return true;
    } catch (err: unknown) {
      logger.error('Error updating configuration for version', targetVersionName, ':', err);
      let errorMsg: string;
      if (err instanceof Error) {
        errorMsg = err.message;
      } else if (err && typeof err === 'object' && 'errors' in err && Array.isArray((err as Record<string, unknown>).errors)) {
        const gqlErrors = (err as Record<string, unknown>).errors as Array<Record<string, unknown>>;
        errorMsg = gqlErrors.map((e) => (e.message as string) || 'Unknown GraphQL error').join('; ');
      } else if (err && typeof err === 'object') {
        errorMsg = JSON.stringify(err);
      } else {
        errorMsg = String(err);
      }
      setError(`Failed to update configuration for version ${targetVersionName}: ${errorMsg}`);
      return false;
    }
  };

  // Reset a specific configuration path back to its default value (LOCAL ONLY)
  // This updates local form state only - user must click Save to persist.
  // This makes "Restore to default" consistent with all other field edits.
  const resetToDefault = (path: string): { path: string; defaultValue: unknown } | false => {
    if (!path || !defaultConfig) return false;

    try {
      logger.debug(`Restoring path to default value (local): ${path}`);

      // Get the default value for this path
      const defaultValue = getValueAtPath(defaultConfig, path);
      logger.debug(`Default value at ${path}:`, defaultValue);

      if (defaultValue === undefined) {
        logger.warn(`No default value found for path: ${path}`);
        return false;
      }

      // Return the default value - the caller (ConfigBuilder) will call updateValue()
      // to set it in formValues, which triggers hasUnsavedChanges detection
      return { path, defaultValue };
    } catch (err: unknown) {
      logger.error('Error getting default value for path', err);
      return false;
    }
  };

  // REMOVED: Old 287-line complex reset logic
  // Now uses simple diff-based approach above

  // Check if a value is customized or default
  const isCustomized = (path: string): boolean => {
    if (!customConfig || !path) {
      return false;
    }

    try {
      // Split the path into segments, handling array indices properly
      const pathSegments = path.split(/[.[\]]+/).filter(Boolean);

      // Helper function to get value at path segments for comparison
      const getValueAtPathSegments = (obj: Record<string, unknown> | null, segments: string[]): unknown => {
        return segments.reduce((acc: unknown, segment) => {
          if (acc === null || acc === undefined || !Object.hasOwn(acc as object, segment)) {
            return undefined;
          }
          return (acc as Record<string, unknown>)[segment];
        }, obj as unknown);
      };

      // Get values from both version and default configs
      const customValue = getValueAtPathSegments(customConfig, pathSegments);
      const defaultValue = getValueAtPathSegments(defaultConfig, pathSegments);

      // First check if the version value exists
      const customValueExists = customValue !== undefined;

      // Special case for empty objects - they should count as not customized
      if (
        customValueExists &&
        typeof customValue === 'object' &&
        customValue !== null &&
        !Array.isArray(customValue) &&
        Object.keys(customValue as Record<string, unknown>).length === 0
      ) {
        return false;
      }

      // Special case for arrays
      if (customValueExists && Array.isArray(customValue)) {
        // Compare arrays for deep equality
        if (Array.isArray(defaultValue)) {
          // Different lengths means customized (including empty vs non-empty)
          if (customValue.length !== defaultValue.length) return true;

          // Deep compare each element
          for (let i = 0; i < customValue.length; i += 1) {
            if (JSON.stringify(customValue[i]) !== JSON.stringify(defaultValue[i])) {
              return true;
            }
          }
          return false; // Arrays are identical
        }
        return true; // Version is array, default isn't or is undefined
      }

      // Deep compare objects
      if (
        customValueExists &&
        typeof customValue === 'object' &&
        customValue !== null &&
        typeof defaultValue === 'object' &&
        defaultValue !== null
      ) {
        return JSON.stringify(customValue) !== JSON.stringify(defaultValue);
      }

      // Check for numeric equivalence (handles 5 vs 5.0, "5" vs 5, etc.)
      // This prevents false positives when Pydantic converts int to float
      if (customValueExists && isNumericValue(customValue) && isNumericValue(defaultValue)) {
        return !areNumericValuesEqual(customValue, defaultValue);
      }

      // Simple value comparison for non-numeric values
      return customValueExists && customValue !== defaultValue;
    } catch (err: unknown) {
      logger.error(`Error in isCustomized for path: ${path}`, err);
      return false;
    }
  };

  useEffect(() => {
    if (versionName === null) return;
    fetchConfiguration(versionName);
  }, [versionName]); // Re-fetch when version changes

  return {
    schema,
    defaultConfig,
    customConfig,
    mergedConfig,
    loading,
    refreshing,
    error,
    fetchConfiguration,
    updateConfiguration,
    resetToDefault,
    isCustomized,
  };
};

export default useConfiguration;
