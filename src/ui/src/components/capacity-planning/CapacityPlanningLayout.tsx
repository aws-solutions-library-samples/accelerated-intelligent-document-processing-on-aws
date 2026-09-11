// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
/* eslint-disable react/no-array-index-key */
import React, { useState, useMemo, useEffect } from 'react';
import { generateClient } from '../../api/client-shim';
import { calculateCapacity as calculateCapacityOp } from '../../graphql/generated';
import {
  Container,
  Header,
  SpaceBetween,
  Box,
  Button,
  ButtonDropdown,
  FormField,
  Input,
  Select,
  Table,
  Alert,
  Cards,
  ColumnLayout,
  Badge,
  Flashbar,
  ExpandableSection,
} from '@cloudscape-design/components';
import useConfiguration from '../../hooks/use-configuration';
import useConfigurationVersions from '../../hooks/use-configuration-versions';
import useSettingsContext from '../../contexts/settings';
import useDocumentsContext from '../../contexts/documents';
import type { Document as UIDocument, Section } from '../../types/documents';
import DocumentPickerModal from './DocumentPickerModal';
import DateRangeModal from '../common/DateRangeModal';
import { parseMetering } from '../../graphql/awsjson-parsers';

// Type definitions for configuration and state
interface Configuration {
  ocr?: { backend?: string; model_id?: string; model?: string };
  classification?: { model?: string };
  extraction?: {
    model?: string;
    // v0.6: confidence assessment (model + granular) lives under extraction
    confidence?: { enabled?: boolean; model?: string; granular?: { enabled?: boolean } };
  };
  assessment?: { enabled?: boolean; model?: string; granular?: { enabled?: boolean } };
  summarization?: { model?: string };
  classes?: Array<{
    name?: string;
    $id?: string;
    'x-aws-idp-document-type'?: string;
    description?: string;
  }>;
  [key: string]: unknown;
}

interface DocumentConfig {
  type: string;
  avgPages: string | number;
  ocrTokens: string | number;
  classificationTokens: string | number;
  extractionTokens: string | number;
  summarizationTokens: string | number;
  assessmentTokens: string | number;
  ocrRequests?: string | number;
  classificationRequests?: string | number;
  extractionRequests?: string | number;
  assessmentRequests?: string | number;
  summarizationRequests?: string | number;
  index?: number;
}

interface TimeSlot {
  hour: string;
  documentType: string;
  docsPerHour: string;
}

interface ProcessingConfig {
  timeSlots: TimeSlot[];
  maxLatency: string;
  missingConfig?: string;
}

interface CapacityMetric {
  label: string;
  value: string;
}

interface QuotaRequirement {
  service: string;
  category: string;
  currentQuota: string;
  requiredQuota: string;
  statusText: string;
  modelId?: string;
  inferenceType?: string;
  modelDisplayName?: string;
  usedFor?: string;
  status?: string;
}

interface LatencyDistribution {
  p50?: string;
  p75?: string;
  p90?: string;
  p95?: string;
  p99?: string;
  baseLatency?: string;
  queueLatency?: string;
  totalLatency?: string;
  exceedsLimit?: boolean;
  maxAllowed?: string;
  actualProcessingTime?: string;
  dataSource?: string;
  quotaOverloaded?: boolean;
  [key: string]: unknown;
}

interface CapacityResult {
  success: boolean;
  errorMessage?: string;
  metrics?: CapacityMetric[];
  quotaRequirements?: QuotaRequirement[];
  latencyDistribution?: LatencyDistribution;
  calculationDetails?: Record<string, unknown>;
  recommendations?: string[];
}

interface Notification {
  id: string;
  type: string;
  content: React.ReactNode;
  header?: string;
  dismissible: boolean;
  onDismiss: () => void;
}

interface RecentDocument {
  ObjectKey: string;
  documentClass: string;
  documentClasses?: string[];
  isMultiClass?: boolean;
  InitialEventTime?: string;
  ObjectStatus?: string;
  metering: Record<string, number>;
}

interface MeteringMetrics {
  totalTokens?: number;
  inputTokens?: number;
  outputTokens?: number;
  requests?: number;
  pages?: number;
  pageCount?: number;
  PageCount?: number;
  [key: string]: unknown;
}

interface HourlyBreakdown {
  hour: number;
  ocrTokens: number;
  classificationTokens: number;
  extractionTokens: number;
  summarizationTokens: number;
  assessmentTokens: number;
  totalTokens: number;
  documentTypes?: string[];
}

interface SelectOption {
  label: string;
  value: string;
  description?: string;
  disabled?: boolean;
}

// Time period constants from main Document List
const _DOCUMENT_LIST_SHARDS_PER_DAY = 6;

const CapacityPlanningLayout = () => {
  const { mergedConfig: configurationUntyped, fetchConfiguration } = useConfiguration();
  const configuration = configurationUntyped as Configuration | null;
  const { versions, getVersionOptions } = useConfigurationVersions();
  const { settings: deploymentSettings } = useSettingsContext();
  const {
    documents,
    isDocumentsListLoading,
    setIsDocumentsListLoading,
    periodsToLoad,
    setPeriodsToLoad,
    customDateRange,
    setCustomDateRange,
  } = useDocumentsContext();

  const [selectedConfigVersion, setSelectedConfigVersion] = useState<SelectOption | null>(null);
  const [manualPattern, setManualPattern] = useState<string | null>(null);

  // Log documents availability for debugging
  useEffect(() => {
    console.log('[CapacityPlanning] Documents from context:', documents?.length || 0, 'documents');
    console.log('[CapacityPlanning] Loading state:', isDocumentsListLoading);
  }, [documents, isDocumentsListLoading]);

  // Log configuration availability for debugging
  useEffect(() => {
    console.log('[CapacityPlanning] Configuration loaded:', configuration ? 'Yes' : 'No');
    if (configuration) {
      console.log('[CapacityPlanning] Config classes:', configuration.classes?.length || 0);
    }
  }, [configuration]);

  // Set default to active version (or first scoped version) when versions are loaded
  useEffect(() => {
    if (versions.length > 0 && !selectedConfigVersion) {
      const versionOptions = getVersionOptions();
      const activeVersion = versions.find((v) => v.isActive);
      if (activeVersion) {
        const activeVersionOption = versionOptions.find((option) => option.value === activeVersion.versionName);
        if (activeVersionOption) {
          console.log('Setting selected configuration profile to active:', activeVersionOption.value);
          setSelectedConfigVersion(activeVersionOption);
          return;
        }
      }
      // Fallback: select first available (scoped) version
      if (versionOptions.length > 0) {
        console.log('Using first available scoped version:', versionOptions[0].value);
        setSelectedConfigVersion(versionOptions[0]);
      }
    }
  }, [versions, selectedConfigVersion]); // getVersionOptions is not memoized

  // Fetch configuration when selected version changes
  // Note: useConfiguration hook already fetches 'default' on mount, so this will fetch the active version after it's determined
  useEffect(() => {
    if (selectedConfigVersion) {
      console.log('Fetching configuration for version:', selectedConfigVersion.value);
      fetchConfiguration(selectedConfigVersion.value, true); // Use silent=true to avoid showing loading state
    }
  }, [selectedConfigVersion]); // fetchConfiguration is not memoized, so we only depend on selectedConfigVersion

  // Auto-populate avgPages from processed documents on component mount
  useEffect(() => {
    // Remove auto-population to avoid errors
  }, []); // Remove documents dependency

  const [documentConfigs, setDocumentConfigs] = useState<DocumentConfig[]>([
    {
      type: '',
      avgPages: '',
      ocrTokens: '',
      classificationTokens: '',
      extractionTokens: '',
      summarizationTokens: '',
      assessmentTokens: '',
    },
  ]);

  const [_configurationError, _setConfigurationError] = useState<string | null>(null);

  const [processingConfig, setProcessingConfig] = useState<ProcessingConfig>(() => {
    const defaultMaxLatency = import.meta.env.VITE_DEFAULT_MAX_LATENCY;
    if (!defaultMaxLatency) {
      return {
        timeSlots: [{ hour: '9', documentType: '', docsPerHour: '' }],
        maxLatency: '7', // Temporary fallback for initialization
        missingConfig: 'VITE_DEFAULT_MAX_LATENCY',
      };
    }
    return {
      timeSlots: [{ hour: '9', documentType: '', docsPerHour: '' }], // Start with 9 AM
      maxLatency: defaultMaxLatency,
    };
  });

  const [loading, setLoading] = useState(false);
  const [hasCalculated, setHasCalculated] = useState(false);
  const [results, setResults] = useState<CapacityResult | null>(null);
  const [recentDocuments, setRecentDocuments] = useState<RecentDocument[]>([]);
  const [showDocumentPicker, setShowDocumentPicker] = useState(false);
  const [selectedDocuments, setSelectedDocuments] = useState<RecentDocument[]>([]);
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [isDateRangeModalVisible, setIsDateRangeModalVisible] = useState(false);
  const [latencyTimeRange, setLatencyTimeRange] = useState<SelectOption>({ label: 'Last 24 hours', value: '24' });

  // Helper function to add notifications
  const addNotification = (type: string, content: React.ReactNode, header: string | null = null, dismissible = true) => {
    const id = Date.now().toString();
    const notification: Notification = {
      id,
      type, // 'success', 'error', 'warning', 'info'
      content,
      dismissible,
      onDismiss: () => setNotifications((prev) => prev.filter((n) => n.id !== id)),
      ...(header ? { header } : {}),
    };
    setNotifications((prev) => [...prev, notification]);

    // Auto-dismiss success/info notifications after 5 seconds
    if (type === 'success' || type === 'info') {
      setTimeout(() => {
        setNotifications((prev) => prev.filter((n) => n.id !== id));
      }, 5000);
    }
  };

  // Handle time period changes - uses context's time range state
  const handlePeriodChange = (shardCount: number) => {
    if (setPeriodsToLoad) {
      setPeriodsToLoad(shardCount);
    }
  };

  // Handle custom date range apply - uses context's time range state
  const handleCustomDateRangeApply = (dateRange: { startDateTime: string; endDateTime: string }) => {
    setIsDateRangeModalVisible(false);
    if (setCustomDateRange) {
      setCustomDateRange(dateRange);
    }
  };

  // Check if granular assessment is enabled in configuration
  // v0.6: confidence enable + granular live under extraction.confidence
  // (fall back to legacy assessment.* for pre-migration configs).
  const isGranularAssessmentEnabled = () => {
    const confidence = configuration?.extraction?.confidence;
    // Check if confidence assessment is enabled at all
    if ((confidence?.enabled ?? configuration?.assessment?.enabled) === false) {
      return false;
    }
    // Check if granular assessment specifically is enabled
    if ((confidence?.granular?.enabled ?? configuration?.assessment?.granular?.enabled) === false) {
      return false;
    }
    // Default to true if not explicitly disabled
    return true;
  };

  // Helper function to extract token and request values from metering data structure
  const extractTokensAndRequestsFromMetering = (meteringData: Record<string, unknown>) => {
    // Calculate estimated request count based on token usage patterns
    // Large token counts typically indicate multiple API requests due to chunking
    const estimateRequestsFromTokens = (tokens: number, stepType: string) => {
      if (!tokens || tokens === 0) return 0;

      // Get max tokens per request from environment or use defaults
      const maxTokensConfig = import.meta.env.VITE_MAX_TOKENS_PER_REQUEST;
      let maxTokensPerRequest;

      if (maxTokensConfig) {
        try {
          const config = JSON.parse(maxTokensConfig);
          maxTokensPerRequest = config[stepType] || config.default || parseInt(import.meta.env.VITE_DEFAULT_MAX_TOKENS_PER_REQUEST, 10);
        } catch (e) {
          maxTokensPerRequest = parseInt(import.meta.env.VITE_DEFAULT_MAX_TOKENS_PER_REQUEST, 10);
        }
      } else {
        // Get default max tokens per request by step type from environment - required for capacity planning
        const defaultTokensConfig = import.meta.env.VITE_DEFAULT_TOKENS_BY_STEP;
        if (!defaultTokensConfig) {
          console.warn('VITE_DEFAULT_TOKENS_BY_STEP environment variable is not configured');
          return 1; // Return minimal value when config missing
        }

        let defaults;
        try {
          defaults = JSON.parse(defaultTokensConfig);
        } catch (e) {
          console.error('VITE_DEFAULT_TOKENS_BY_STEP contains invalid JSON');
          return 1;
        }

        maxTokensPerRequest = defaults[stepType];
        if (!maxTokensPerRequest) {
          const defaultMaxTokens = import.meta.env.VITE_DEFAULT_MAX_TOKENS_PER_REQUEST;
          if (!defaultMaxTokens) {
            console.warn(`No token configuration found for step type: ${stepType}`);
            return 1;
          }
          maxTokensPerRequest = parseInt(defaultMaxTokens, 10);
        }
      }

      // Estimate requests based on token chunking
      return Math.max(1, Math.ceil(tokens / maxTokensPerRequest));
    };

    const data = {
      ocrTokens: 0,
      classificationTokens: 0,
      extractionTokens: 0,
      assessmentTokens: 0,
      summarizationTokens: 0,
      ocrRequests: 0,
      classificationRequests: 0,
      extractionRequests: 0,
      assessmentRequests: 0,
      summarizationRequests: 0,
      avgPages: 0, // Extract actual page count from OCR requests
    };

    // Extract tokens and estimate requests from context-prefixed keys
    Object.entries(meteringData).forEach(([key, value]) => {
      if (typeof value === 'object' && value !== null) {
        const metrics = value as MeteringMetrics;
        if (key.startsWith('OCR/')) {
          const tokenCount = metrics.totalTokens || (metrics.inputTokens || 0) + (metrics.outputTokens || 0);
          data.ocrTokens += Number(tokenCount) || 0;
          data.ocrRequests += estimateRequestsFromTokens(tokenCount, 'OCR');
          // Extract page count from OCR bedrock requests (OCR processes each page as a separate API call)
          if (key.includes('/bedrock/') && metrics.requests) {
            const pageCount = Number(metrics.requests) || 0;
            data.avgPages = Math.max(data.avgPages, pageCount);
            console.log(`📄 Extracted page count from ${key}: ${pageCount} pages`);
          }
        } else if (key.startsWith('Classification/')) {
          const tokenCount = metrics.totalTokens || (metrics.inputTokens || 0) + (metrics.outputTokens || 0);
          data.classificationTokens += Number(tokenCount) || 0;
          data.classificationRequests += estimateRequestsFromTokens(tokenCount, 'Classification');
        } else if (key.startsWith('Extraction/')) {
          const tokenCount = metrics.totalTokens || (metrics.inputTokens || 0) + (metrics.outputTokens || 0);
          data.extractionTokens += Number(tokenCount) || 0;
          data.extractionRequests += estimateRequestsFromTokens(tokenCount, 'Extraction');
        } else if (key.startsWith('Assessment/')) {
          const tokenCount = metrics.totalTokens || (metrics.inputTokens || 0) + (metrics.outputTokens || 0);
          data.assessmentTokens += Number(tokenCount) || 0;
          data.assessmentRequests += estimateRequestsFromTokens(tokenCount, 'Assessment');
        } else if (key.startsWith('GranularAssessment/')) {
          // GranularAssessment tokens are included only when granular assessment is enabled in config
          // When disabled, these tokens should not be counted for capacity planning
          if (isGranularAssessmentEnabled()) {
            const tokenCount = metrics.totalTokens || (metrics.inputTokens || 0) + (metrics.outputTokens || 0);
            data.assessmentTokens += Number(tokenCount) || 0;
            data.assessmentRequests += estimateRequestsFromTokens(tokenCount, 'Assessment');
          }
        } else if (key.startsWith('Summarization/')) {
          const tokenCount = metrics.totalTokens || (metrics.inputTokens || 0) + (metrics.outputTokens || 0);
          data.summarizationTokens += Number(tokenCount) || 0;
          data.summarizationRequests += estimateRequestsFromTokens(tokenCount, 'Summarization');
        } else if (key.startsWith('BDAProject/bda/') && metrics.pages) {
          const pages = Number(metrics.pages) || 0;
          // Extract page count from BDA pattern
          data.avgPages = Math.max(data.avgPages, pages);
          console.log(`📄 Extracted page count from BDA ${key}: ${pages} pages`);
          const tokensPerPageConfig = import.meta.env.VITE_BDA_TOKENS_PER_PAGE;
          if (!tokensPerPageConfig) {
            console.warn('VITE_BDA_TOKENS_PER_PAGE environment variable is not configured for BDA pattern');
            return; // Skip this entry in forEach
          }
          const estimatedTokensPerPage = parseInt(tokensPerPageConfig, 10);
          if (Number.isNaN(estimatedTokensPerPage)) {
            console.error('VITE_BDA_TOKENS_PER_PAGE must be a valid number');
            return; // Skip this entry in forEach
          }
          const totalTokens = pages * estimatedTokensPerPage;
          data.summarizationTokens += totalTokens;
          data.summarizationRequests += estimateRequestsFromTokens(totalTokens, 'Summarization');
        }
        // Also check for PageCount field in metrics (common pattern)
        if (metrics.pages && !key.startsWith('BDAProject/')) {
          const pageCount = Number(metrics.pages) || 0;
          data.avgPages = Math.max(data.avgPages, pageCount);
          console.log(`📄 Extracted page count from ${key}.pages: ${pageCount} pages`);
        }
        if (metrics.pageCount) {
          const pageCount = Number(metrics.pageCount) || 0;
          data.avgPages = Math.max(data.avgPages, pageCount);
          console.log(`📄 Extracted page count from ${key}.pageCount: ${pageCount} pages`);
        }
        if (metrics.PageCount) {
          const pageCount = Number(metrics.PageCount) || 0;
          data.avgPages = Math.max(data.avgPages, pageCount);
          console.log(`📄 Extracted page count from ${key}.PageCount: ${pageCount} pages`);
        }
      }
    });

    return data;
  };

  // Helper function to get configured document types from configuration
  const getConfiguredDocumentTypes = () => {
    const configuredTypes = new Set();

    if (configuration?.classes && Array.isArray(configuration.classes)) {
      configuration.classes.forEach((docClass) => {
        // Handle both legacy format and JSON Schema format
        if (docClass['x-aws-idp-document-type']) {
          configuredTypes.add(docClass['x-aws-idp-document-type']);
        } else if (docClass.name) {
          configuredTypes.add(docClass.name);
        } else if (docClass.$id) {
          configuredTypes.add(docClass.$id);
        }
      });
    }

    return configuredTypes;
  };

  // Helper function to validate a document for capacity planning
  // Returns { valid: boolean, error?: string, documentClass?: string }
  const validateDocumentForCapacityPlanning = (doc: UIDocument) => {
    const configuredTypes = getConfiguredDocumentTypes();

    // Check if document has sections
    const sections: Section[] = doc.Sections || [];

    // Get unique classes from sections
    const uniqueClasses = new Set<string>();
    sections.forEach((section: Section) => {
      if (section.Class) {
        uniqueClasses.add(section.Class);
      }
    });

    // Also check DocumentClass field
    if (doc.DocumentClass) {
      uniqueClasses.add(doc.DocumentClass);
    }

    // Add diagnostic logging
    console.log(`🔍 Validating document: ${doc.ObjectKey}`);
    console.log(`  - Sections: ${sections.length}`);
    console.log(`  - DocumentClass field: ${doc.DocumentClass || 'none'}`);
    console.log(
      `  - Classes from Sections: ${
        sections
          .map((s: Section) => s.Class)
          .filter(Boolean)
          .join(', ') || 'none'
      }`,
    );
    console.log(`  - Unique classes found: ${Array.from(uniqueClasses).join(', ') || 'none'}`);
    console.log(`  - Configured types: ${Array.from(configuredTypes).join(', ')}`);

    // Allow multi-class documents but flag them
    const isMultiClass = uniqueClasses.size > 1;
    const documentClasses = Array.from(uniqueClasses);

    // If no classification, return error
    if (uniqueClasses.size === 0) {
      return {
        valid: false,
        error: `Document "${doc.ObjectKey}" has no classification. Please ensure documents are classified before using for capacity planning.`,
        documentClass: null as string | null,
        documentClasses: [] as string[],
        isMultiClass: false,
      };
    }

    if (isMultiClass) {
      console.log(`  ⚠️  Multi-class document with ${documentClasses.length} classes: ${documentClasses.join(', ')}`);
    }

    // For single-class, documentClass is the one class; for multi-class, it's a concatenated string for display
    const documentClass = isMultiClass ? documentClasses.join(', ') : documentClasses[0];

    // Verify the class exists in configured document types
    // NOTE: For capacity planning, we allow documents with any classification as long as they have metering data
    // This allows using previously processed documents even if they were processed with a different configuration
    if (configuredTypes.size > 0) {
      if (isMultiClass) {
        // Check each class for multi-class documents
        documentClasses.forEach((cls) => {
          if (!configuredTypes.has(cls)) {
            console.log(`  ⚠️  Document class "${cls}" NOT found in configured types (${Array.from(configuredTypes).join(', ')})`);
            console.log(`  ℹ️  Allowing for capacity planning since document has metering data`);
          }
        });
      } else {
        if (!configuredTypes.has(documentClass)) {
          console.log(`  ⚠️  Document class "${documentClass}" NOT found in configured types (${Array.from(configuredTypes).join(', ')})`);
          console.log(`  ℹ️  Allowing for capacity planning since document has metering data`);
        } else {
          console.log(`  ✅ Document class "${documentClass}" found in configured types`);
        }
      }
    }

    console.log(`  ✅ Document validated successfully with class${isMultiClass ? 'es' : ''}: ${documentClass}`);
    return {
      valid: true,
      documentClass,
      documentClasses, // Array of all classes for multi-class documents
      isMultiClass, // Flag indicating if this is a multi-class document
    };
  };

  const fetchRecentDocuments = async () => {
    console.log('fetchRecentDocuments called');
    console.log('documents:', documents);

    try {
      if (!documents || documents.length === 0) {
        addNotification(
          'warning',
          'Please visit the Documents tab first to load document data, then return to Capacity Planning.',
          'No documents available',
        );
        return;
      }

      // Get configured document types for validation
      const configuredTypes = getConfiguredDocumentTypes();
      console.log('Configured document types:', Array.from(configuredTypes));

      console.log('Total documents:', documents.length);
      if (documents.length > 0) {
        console.log('Sample document:', documents[0]);
        console.log('Sample document keys:', Object.keys(documents[0]));
        console.log('Sample document Metering:', documents[0].Metering);
        console.log('Sample document ObjectStatus:', documents[0].ObjectStatus);
      }

      // Check different document statuses
      const completedDocs = documents.filter((doc) => doc.ObjectStatus === 'COMPLETED');
      const docsWithMetering = documents.filter((doc) => doc.Metering);
      const completedWithMetering = documents.filter((doc) => doc.ObjectStatus === 'COMPLETED' && doc.Metering);

      console.log('Completed documents:', completedDocs.length);
      console.log('Documents with metering:', docsWithMetering.length);
      console.log('Completed with metering:', completedWithMetering.length);

      // Log first completed document details
      if (completedDocs.length > 0) {
        console.log('First completed document:', completedDocs[0]);
        console.log('First completed document Metering field:', completedDocs[0].Metering);
      }

      // Try to find any documents with metering data, regardless of status
      let candidateDocuments = completedWithMetering;

      // If no completed documents with metering, try any documents with metering
      if (candidateDocuments.length === 0) {
        candidateDocuments = docsWithMetering;
        console.log('No completed documents with metering, trying any documents with metering');
      }

      // If still no documents, show detailed error
      if (candidateDocuments.length === 0) {
        const statusCounts: Record<string, number> = {};
        documents.forEach((doc) => {
          const status = String(doc.ObjectStatus ?? 'Unknown');
          statusCounts[status] = (statusCounts[status] || 0) + 1;
        });

        addNotification(
          'error',
          <>
            <Box>No documents with metering data found.</Box>
            <Box variant="p">
              Document status breakdown:
              <ul style={{ marginTop: '8px', paddingLeft: '20px' }}>
                {Object.entries(statusCounts).map(([status, count]) => (
                  <li key={status}>
                    {status}: {count}
                  </li>
                ))}
              </ul>
            </Box>
            <Box variant="small">Documents may be missing metering information. Check the browser console for details.</Box>
          </>,
          'No Metering Data Found',
        );
        return;
      }

      // Map documents with metering data and validate for capacity planning
      const validationErrors: string[] = [];
      const validDocuments = candidateDocuments
        .map((doc) => {
          const meteringData = parseMetering(doc.Metering as string) || {};
          if (!meteringData || Object.keys(meteringData).length === 0) {
            console.warn('Failed to parse metering data for', doc.ObjectKey);
            return null;
          }

          // Validate document for capacity planning (single-class only)
          const validation = validateDocumentForCapacityPlanning(doc);

          if (!validation.valid) {
            validationErrors.push(validation.error ?? 'Unknown validation error');
            console.warn('Document validation failed:', validation.error);
            return null;
          }

          // Extract token and request values from the metering data structure
          const extractedData = extractTokensAndRequestsFromMetering(meteringData);

          // Also check document-level page count fields
          if (extractedData.avgPages === 0) {
            // Check for PageCount at document level
            if (doc.PageCount) {
              extractedData.avgPages = Number(doc.PageCount) || 0;
              console.log(`📄 Extracted page count from doc.PageCount: ${extractedData.avgPages} pages`);
            } else if (doc.pageCount) {
              extractedData.avgPages = Number(doc.pageCount) || 0;
              console.log(`📄 Extracted page count from doc.pageCount: ${extractedData.avgPages} pages`);
            } else if (doc.Pages) {
              extractedData.avgPages = Number(doc.Pages) || 0;
              console.log(`📄 Extracted page count from doc.Pages: ${extractedData.avgPages} pages`);
            } else if (doc.Sections && doc.Sections.length > 0) {
              // Try to get page count from sections
              const maxEndPage = Math.max(
                ...(doc.Sections as unknown as Array<Record<string, unknown>>).map(
                  (s) => (s.EndPage as number) || (s.endPage as number) || 0,
                ),
              );
              if (maxEndPage > 0) {
                extractedData.avgPages = maxEndPage;
                console.log(`📄 Extracted page count from Sections EndPage: ${extractedData.avgPages} pages`);
              }
            }
          }

          console.log('Extracted data for', doc.ObjectKey, ':', extractedData);

          return {
            ObjectKey: String(doc.ObjectKey),
            documentClass: validation.documentClass,
            documentClasses: validation.documentClasses as string[], // Array of all classes
            isMultiClass: validation.isMultiClass, // Flag for multi-class documents
            InitialEventTime: doc.InitialEventTime as string | undefined,
            ObjectStatus: doc.ObjectStatus as string | undefined,
            metering: extractedData,
          } as RecentDocument;
        })
        .filter((doc) => doc !== null);

      // Show validation errors if any documents were skipped
      if (validationErrors.length > 0) {
        console.warn('Validation errors:', validationErrors);
        addNotification(
          'warning',
          <>
            <Box>{validationErrors.length} document(s) were skipped due to validation errors:</Box>
            <Box variant="p">
              <ul style={{ marginTop: '8px', paddingLeft: '20px', maxHeight: '200px', overflowY: 'auto' }}>
                {validationErrors.slice(0, 5).map((error, idx) => (
                  <li key={idx} style={{ marginBottom: '8px' }}>
                    {error}
                  </li>
                ))}
                {validationErrors.length > 5 && <li>... and {validationErrors.length - 5} more errors</li>}
              </ul>
            </Box>
            <Box variant="small">
              Note: Capacity planning only supports single-class documents with types defined in your configuration.
            </Box>
          </>,
          'Capacity Planning Validation',
        );
      }

      console.log('Valid documents found:', validDocuments.length);
      setRecentDocuments(validDocuments);
      setSelectedDocuments([]);
      setShowDocumentPicker(true);
    } catch (error) {
      console.error('Error processing documents:', error);
      addNotification('error', 'Failed to process document data. Please try again.', 'Processing Error');
    }
  };

  const populateTokensFromDocument = (selectedDoc: RecentDocument) => {
    if (!selectedDoc.metering) return;

    const metering = selectedDoc.metering;
    const isMultiClass = selectedDoc.isMultiClass || false;
    const documentClasses = selectedDoc.documentClasses || [selectedDoc.documentClass];

    let updatedConfigs = [...documentConfigs];

    if (isMultiClass) {
      // Multi-class document: divide tokens equally among all classes
      const numClasses = documentClasses.length;
      console.log(`📊 Populating multi-class document with ${numClasses} classes:`, documentClasses);

      documentClasses.forEach((docType) => {
        const existingIndex = updatedConfigs.findIndex((config) => config.type === docType);

        // Divide tokens by number of classes for estimation
        const newConfig = {
          type: docType,
          avgPages: Math.round((metering.avgPages || 0) / numClasses) || '',
          ocrTokens: metering.ocrTokens !== undefined ? Math.round(metering.ocrTokens / numClasses) : '',
          classificationTokens: metering.classificationTokens !== undefined ? Math.round(metering.classificationTokens / numClasses) : '',
          extractionTokens: metering.extractionTokens !== undefined ? Math.round(metering.extractionTokens / numClasses) : '',
          assessmentTokens: metering.assessmentTokens !== undefined ? Math.round(metering.assessmentTokens / numClasses) : '',
          summarizationTokens: metering.summarizationTokens !== undefined ? Math.round(metering.summarizationTokens / numClasses) : '',
          ocrRequests: metering.ocrRequests !== undefined ? Math.round(metering.ocrRequests / numClasses) : '',
          classificationRequests:
            metering.classificationRequests !== undefined ? Math.round(metering.classificationRequests / numClasses) : '',
          extractionRequests: metering.extractionRequests !== undefined ? Math.round(metering.extractionRequests / numClasses) : '',
          assessmentRequests: metering.assessmentRequests !== undefined ? Math.round(metering.assessmentRequests / numClasses) : '',
          summarizationRequests:
            metering.summarizationRequests !== undefined ? Math.round(metering.summarizationRequests / numClasses) : '',
        };

        if (existingIndex >= 0) {
          updatedConfigs[existingIndex] = { ...updatedConfigs[existingIndex], ...newConfig };
        } else {
          updatedConfigs = [...updatedConfigs, newConfig];
        }
      });

      // Show warning notification for multi-class documents
      addNotification(
        'info',
        <>
          <Box>
            Multi-section document populated: <strong>{selectedDoc.ObjectKey}</strong>
          </Box>
          <Box variant="p" margin={{ top: 'xs' }}>
            Created {numClasses} rows (one per document class: {documentClasses.join(', ')}). Token values are divided equally as an
            estimate.
          </Box>
          <Box variant="small" color="text-status-warning" margin={{ top: 'xs' }}>
            ⚠️ Note: Actual token usage per class may vary. These are averaged estimates from the total document tokens.
          </Box>
        </>,
        'Multi-Section Document',
      );
    } else {
      // Single-class document: use values as-is
      const docType = documentClasses[0];
      const existingIndex = updatedConfigs.findIndex((config) => config.type === docType);

      const newConfig = {
        type: docType,
        avgPages: metering.avgPages || '',
        ocrTokens: metering.ocrTokens !== undefined ? metering.ocrTokens : '',
        classificationTokens: metering.classificationTokens !== undefined ? metering.classificationTokens : '',
        extractionTokens: metering.extractionTokens !== undefined ? metering.extractionTokens : '',
        assessmentTokens: metering.assessmentTokens !== undefined ? metering.assessmentTokens : '',
        summarizationTokens: metering.summarizationTokens !== undefined ? metering.summarizationTokens : '',
        ocrRequests: metering.ocrRequests !== undefined ? metering.ocrRequests : '',
        classificationRequests: metering.classificationRequests !== undefined ? metering.classificationRequests : '',
        extractionRequests: metering.extractionRequests !== undefined ? metering.extractionRequests : '',
        assessmentRequests: metering.assessmentRequests !== undefined ? metering.assessmentRequests : '',
        summarizationRequests: metering.summarizationRequests !== undefined ? metering.summarizationRequests : '',
      };

      if (existingIndex >= 0) {
        updatedConfigs[existingIndex] = { ...updatedConfigs[existingIndex], ...newConfig };
      } else {
        updatedConfigs = [...updatedConfigs, newConfig];
      }

      addNotification('success', `Token usage populated from document: ${selectedDoc.ObjectKey}`, `Updated ${docType}`);
    }

    // Remove empty document configurations
    const filteredConfigs = updatedConfigs.filter((config) => {
      const hasType = config.type && config.type.trim() !== '';
      const hasTokens =
        config.ocrTokens !== undefined ||
        config.classificationTokens !== undefined ||
        config.extractionTokens !== undefined ||
        config.assessmentTokens !== undefined ||
        config.summarizationTokens !== undefined;
      return hasType && hasTokens;
    });

    setDocumentConfigs(filteredConfigs);
    setShowDocumentPicker(false);
    setSelectedDocuments([]);
  };

  const populateTokensFromMultipleDocuments = () => {
    if (selectedDocuments.length === 0) return;

    let updatedConfigs = [...documentConfigs];
    let multiClassCount = 0;

    selectedDocuments.forEach((selectedDoc) => {
      if (!selectedDoc.metering) return;

      const metering = selectedDoc.metering;
      const isMultiClass = selectedDoc.isMultiClass || false;
      const documentClasses = selectedDoc.documentClasses || [selectedDoc.documentClass];

      if (isMultiClass) {
        multiClassCount++;
        // Multi-class document: divide tokens equally among all classes
        const numClasses = documentClasses.length;

        documentClasses.forEach((docType) => {
          const existingIndex = updatedConfigs.findIndex((config) => config.type === docType);

          const newConfig = {
            type: docType,
            avgPages: Math.round((metering.avgPages || 0) / numClasses) || '',
            ocrTokens: metering.ocrTokens !== undefined ? Math.round(metering.ocrTokens / numClasses) : '',
            classificationTokens: metering.classificationTokens !== undefined ? Math.round(metering.classificationTokens / numClasses) : '',
            extractionTokens: metering.extractionTokens !== undefined ? Math.round(metering.extractionTokens / numClasses) : '',
            assessmentTokens: metering.assessmentTokens !== undefined ? Math.round(metering.assessmentTokens / numClasses) : '',
            summarizationTokens: metering.summarizationTokens !== undefined ? Math.round(metering.summarizationTokens / numClasses) : '',
            ocrRequests: metering.ocrRequests !== undefined ? Math.round(metering.ocrRequests / numClasses) : '',
            classificationRequests:
              metering.classificationRequests !== undefined ? Math.round(metering.classificationRequests / numClasses) : '',
            extractionRequests: metering.extractionRequests !== undefined ? Math.round(metering.extractionRequests / numClasses) : '',
            assessmentRequests: metering.assessmentRequests !== undefined ? Math.round(metering.assessmentRequests / numClasses) : '',
            summarizationRequests:
              metering.summarizationRequests !== undefined ? Math.round(metering.summarizationRequests / numClasses) : '',
          };

          if (existingIndex >= 0) {
            updatedConfigs[existingIndex] = { ...updatedConfigs[existingIndex], ...newConfig };
          } else {
            updatedConfigs = [...updatedConfigs, newConfig];
          }
        });
      } else {
        // Single-class document: use values as-is
        const docType = documentClasses[0];
        const existingIndex = updatedConfigs.findIndex((config) => config.type === docType);

        const newConfig = {
          type: docType,
          avgPages: metering.avgPages || '',
          ocrTokens: metering.ocrTokens !== undefined ? metering.ocrTokens : '',
          classificationTokens: metering.classificationTokens !== undefined ? metering.classificationTokens : '',
          extractionTokens: metering.extractionTokens !== undefined ? metering.extractionTokens : '',
          assessmentTokens: metering.assessmentTokens !== undefined ? metering.assessmentTokens : '',
          summarizationTokens: metering.summarizationTokens !== undefined ? metering.summarizationTokens : '',
          ocrRequests: metering.ocrRequests !== undefined ? metering.ocrRequests : '',
          classificationRequests: metering.classificationRequests !== undefined ? metering.classificationRequests : '',
          extractionRequests: metering.extractionRequests !== undefined ? metering.extractionRequests : '',
          assessmentRequests: metering.assessmentRequests !== undefined ? metering.assessmentRequests : '',
          summarizationRequests: metering.summarizationRequests !== undefined ? metering.summarizationRequests : '',
        };

        if (existingIndex >= 0) {
          updatedConfigs[existingIndex] = { ...updatedConfigs[existingIndex], ...newConfig };
        } else {
          updatedConfigs = [...updatedConfigs, newConfig];
        }
      }
    });

    const filteredConfigs = updatedConfigs.filter((config) => {
      const hasType = config.type && config.type.trim() !== '';
      const hasTokens =
        config.ocrTokens !== undefined ||
        config.classificationTokens !== undefined ||
        config.extractionTokens !== undefined ||
        config.assessmentTokens !== undefined ||
        config.summarizationTokens !== undefined;
      return hasType && hasTokens;
    });

    setDocumentConfigs(filteredConfigs);
    setShowDocumentPicker(false);
    setSelectedDocuments([]);

    // Show notification with info about multi-class documents if any were included
    if (multiClassCount > 0) {
      addNotification(
        'info',
        <>
          <Box>
            Populated token usage for{' '}
            <strong>
              {selectedDocuments.length} document{selectedDocuments.length > 1 ? 's' : ''}
            </strong>
          </Box>
          <Box variant="p" margin={{ top: 'xs' }}>
            {multiClassCount} multi-section document{multiClassCount > 1 ? 's were' : ' was'} included. Token values for multi-section
            documents are divided equally among their document classes.
          </Box>
          <Box variant="small" color="text-status-warning" margin={{ top: 'xs' }}>
            ⚠️ Note: Multi-section token estimates may vary from actual per-class usage.
          </Box>
        </>,
        'Documents Updated',
      );
    } else {
      addNotification(
        'success',
        `Token usage populated for ${selectedDocuments.length} document${selectedDocuments.length > 1 ? 's' : ''}`,
        'Documents Updated',
      );
    }
  };

  const _handleDocumentSelection = (document: RecentDocument, isSelected: boolean) => {
    if (isSelected) {
      setSelectedDocuments([...selectedDocuments, document]);
    } else {
      setSelectedDocuments(selectedDocuments.filter((doc) => doc.ObjectKey !== document.ObjectKey));
    }
  };

  // Helper function to get readable model display name
  const getModelDisplayName = (modelId: string | undefined) => {
    if (!modelId) return 'Not configured';

    // Extract readable name from model ID
    let displayName = modelId;

    // Remove region prefix (e.g., "us.amazon.nova-lite-v1:0" -> "amazon.nova-lite-v1:0")
    if (displayName.includes('.')) {
      const parts = displayName.split('.');
      if (parts.length > 2) {
        displayName = parts.slice(1).join('.');
      }
    }

    // Remove version suffix (e.g., "amazon.nova-lite-v1:0" -> "amazon.nova-lite-v1")
    if (displayName.includes(':')) {
      displayName = displayName.split(':')[0];
    }

    // Clean up common prefixes
    displayName = displayName.replace(/^amazon\./, '').replace(/^anthropic\./, '');

    return displayName;
  };

  const timeSlotOptions = Array.from({ length: 24 }, (_, i) => {
    const currentHour = String(i).padStart(2, '0');
    const nextHour = String((i + 1) % 24).padStart(2, '0');
    return {
      label: `${currentHour}:00 - ${nextHour}:00`,
      value: String(i),
    };
  });

  const documentTypeOptions = useMemo(() => {
    const defaultOption = { label: '-- Select Document Type --', value: '', disabled: true };

    // Only show document types from View/Edit Configuration (configuration.classes)
    let classOptions: SelectOption[] = [];
    if (configuration?.classes && Array.isArray(configuration.classes)) {
      classOptions = configuration.classes
        .map((docClass) => {
          // Handle both legacy format and JSON Schema format
          let documentTypeName;
          let description;

          if (docClass['x-aws-idp-document-type']) {
            // JSON Schema format - use x-aws-idp-document-type for the document type name
            documentTypeName = docClass['x-aws-idp-document-type'];
            description = docClass.description || `${documentTypeName} document processing`;
          } else if (docClass.name) {
            // Legacy format - use name field
            documentTypeName = docClass.name;
            description = docClass.description || `${documentTypeName} document processing`;
          } else if (docClass.$id) {
            // JSON Schema format fallback - use $id if x-aws-idp-document-type is missing
            documentTypeName = docClass.$id;
            description = docClass.description || `${documentTypeName} document processing`;
          } else {
            // Unknown format - skip this class
            return null;
          }

          return {
            label: documentTypeName,
            value: documentTypeName,
            description,
          };
        })
        .filter((item): item is NonNullable<typeof item> => item !== null); // Remove null entries
    }

    // If no classes configured, show a helpful message
    if (classOptions.length === 0) {
      return [
        defaultOption,
        {
          label: 'No document types configured',
          value: '',
          disabled: true,
          description: 'Add document types in View/Edit Configuration first',
        },
      ];
    }

    return [defaultOption, ...classOptions];
  }, [configuration]);

  // Processing Schedule should only show configured document types
  const scheduleDocumentTypeOptions = useMemo(() => {
    const defaultOption = { label: '-- Select Document Type --', value: '', disabled: true };

    // Only include document types that have been configured in Document Processing
    const configuredOptions = documentConfigs
      .filter((config) => config.type && config.type !== '')
      .map((config) => ({
        label: config.type,
        value: config.type,
        description: `Configure processing schedule for ${config.type}`,
      }));

    if (configuredOptions.length === 0) {
      return [
        defaultOption,
        {
          label: 'No document types configured',
          value: '',
          disabled: true,
          description: 'Add document types in Expected Token Usage section first',
        },
      ];
    }

    return [defaultOption, ...configuredOptions];
  }, [documentConfigs]);

  const getDeployedPattern = () => {
    // Manual override takes precedence over everything
    if (manualPattern) {
      return manualPattern;
    }

    // Use the same logic as the left panel - simple and reliable
    if (deploymentSettings?.IDPPattern) {
      const pattern = String(deploymentSettings.IDPPattern).split(' ')[0]; // Extract just "Pattern1", "Pattern2", "Unified", etc.

      if (pattern === 'Pattern1') {
        return 'PATTERN-1';
      }
      if (pattern === 'Pattern3') {
        return 'PATTERN-3';
      }
      if (pattern === 'Pattern2') {
        return 'PATTERN-2';
      }
      // Unified pattern uses the same Bedrock pipeline as Pattern-2
      if (pattern.toLowerCase() === 'unified') {
        return 'PATTERN-2';
      }
    }

    // If data is still loading, return loading state
    if (!deploymentSettings) {
      return 'LOADING...';
    }

    // No fallback - require explicit pattern configuration
    return 'CONFIGURATION_REQUIRED';
  };

  // Initialize tokensPerDoc when component mounts or pattern changes
  // Removed automatic recalculation to keep tokens fully editable

  // Effect to refresh when configuration changes
  useEffect(() => {
    // Clear previous results when configuration changes to force recalculation
    if (configuration) {
      setResults(null);
      setHasCalculated(false);
    }
  }, [configuration]);

  const updateDocumentConfig = async (index: number, field: keyof DocumentConfig, value: string | number) => {
    const updated = [...documentConfigs];
    updated[index] = { ...updated[index], [field]: value };

    setDocumentConfigs(updated);
  };

  const removeDocumentConfig = (index: number) => {
    const updated = documentConfigs.filter((_, i) => i !== index);
    setDocumentConfigs(updated);
  };

  const addTimeSlot = () => {
    const updated = { ...processingConfig };
    updated.timeSlots.push({ hour: '9', documentType: '', docsPerHour: '' }); // Start empty
    setProcessingConfig(updated);
  };

  const addAllDocumentTypesAt9AM = () => {
    const updated = { ...processingConfig };
    // Get configured document types from the token usage section
    const configuredTypes = documentConfigs.filter((config) => config.type && config.type.trim() !== '').map((config) => config.type);

    if (configuredTypes.length === 0) {
      addNotification('warning', 'Please configure document types in the Expected Token Usage section first.', 'No Document Types');
      return;
    }

    // Add one time slot per document type at 9 AM with 0 docs/hour
    configuredTypes.forEach((docType) => {
      updated.timeSlots.push({ hour: '9', documentType: docType, docsPerHour: '0' });
    });

    setProcessingConfig(updated);
    addNotification('success', `Added ${configuredTypes.length} time slots at 9:00 AM`, 'Schedule Updated');
  };

  const autoFillBusinessHours = () => {
    const updated = { ...processingConfig };
    const configuredTypes = documentConfigs.filter((config) => config.type && config.type.trim() !== '').map((config) => config.type);

    if (configuredTypes.length === 0) {
      addNotification('warning', 'Please configure document types in the Expected Token Usage section first.', 'No Document Types');
      return;
    }

    // Business hours: 8 AM to 6 PM (8-18)
    const businessHours = ['8', '9', '10', '11', '12', '13', '14', '15', '16', '17', '18'];

    // Add one time slot per document type per business hour with 0 docs/hour
    businessHours.forEach((hour) => {
      configuredTypes.forEach((docType) => {
        updated.timeSlots.push({ hour, documentType: docType, docsPerHour: '0' });
      });
    });

    setProcessingConfig(updated);
    addNotification(
      'info',
      `Added ${
        businessHours.length * configuredTypes.length
      } time slots for business hours (8 AM - 6 PM). Update docs/hour values as needed.`,
      'Schedule Auto-filled',
    );
  };

  const autoFillFullDay = () => {
    const updated = { ...processingConfig };
    const configuredTypes = documentConfigs.filter((config) => config.type && config.type.trim() !== '').map((config) => config.type);

    if (configuredTypes.length === 0) {
      addNotification('warning', 'Please configure document types in the Expected Token Usage section first.', 'No Document Types');
      return;
    }

    // All 24 hours (0-23)
    const allHours = Array.from({ length: 24 }, (_, i) => i.toString());

    // Add one time slot per document type per hour with 0 docs/hour
    allHours.forEach((hour) => {
      configuredTypes.forEach((docType) => {
        updated.timeSlots.push({ hour, documentType: docType, docsPerHour: '0' });
      });
    });

    setProcessingConfig(updated);
    addNotification(
      'info',
      `Added ${allHours.length * configuredTypes.length} time slots for full day (24 hours). Update docs/hour values as needed.`,
      'Schedule Auto-filled',
    );
  };

  const clearAllTimeSlots = () => {
    const updated = { ...processingConfig };
    updated.timeSlots = [{ hour: '9', documentType: '', docsPerHour: '' }]; // Reset to one empty slot
    setProcessingConfig(updated);
    addNotification('success', 'All time slots cleared', 'Schedule Reset');
  };

  const updateTimeSlot = (index: number, field: keyof TimeSlot, value: string) => {
    const updated = { ...processingConfig };
    updated.timeSlots[index] = { ...updated.timeSlots[index], [field]: value };
    setProcessingConfig(updated);
  };

  const removeTimeSlot = (index: number) => {
    const updated = { ...processingConfig };
    updated.timeSlots = updated.timeSlots.filter((_, i) => i !== index);
    setProcessingConfig(updated);
  };

  const calculateCapacityRequirements = async () => {
    setLoading(true);
    setHasCalculated(false);
    try {
      // Fetch the latest configuration before calculating
      // IMPORTANT: Use the selected configuration profile, not 'default'
      if (selectedConfigVersion) {
        await fetchConfiguration(selectedConfigVersion.value, true);
      }

      // Validate OCR tokens if Bedrock OCR is configured
      // Note: 0 is a valid value for tokens, only check for undefined/null/empty string/NaN
      if (configuration?.ocr?.backend === 'bedrock') {
        const hasDocumentsWithMissingOcrTokens = documentConfigs.some(
          (config) =>
            config.ocrTokens === undefined ||
            config.ocrTokens === null ||
            config.ocrTokens === '' ||
            (typeof config.ocrTokens === 'string' && config.ocrTokens.trim() === '') ||
            Number.isNaN(parseFloat(String(config.ocrTokens))),
        );

        if (hasDocumentsWithMissingOcrTokens) {
          setResults({
            success: false,
            errorMessage:
              'OCR tokens are required for all document types when Bedrock OCR is configured. ' +
              'Please specify OCR token values in the Expected Token Usage section (0 is a valid value).',
            metrics: [{ label: 'Validation Error', value: 'Missing OCR Tokens' }],
            quotaRequirements: [],
          });
          setHasCalculated(true);
          setLoading(false);
          return;
        }
      }

      // Calculate total docs per hour from processing schedule time slots
      const totalDocsPerHour = processingConfig.timeSlots.reduce(
        (sum, slot) => sum + parseInt(String(slot.docsPerHour || 0), 10), // Default to 0 if empty
        0,
      );

      // Aggregate document configs from processing schedule
      const aggregatedDocConfigs: Record<
        string,
        {
          type: string;
          avgPages: number;
          ocrTokens: number;
          classificationTokens: number;
          extractionTokens: number;
          summarizationTokens: number;
          assessmentTokens: number;
          docsPerHour: number;
          ocrRequests?: number;
          classificationRequests?: number;
          extractionRequests?: number;
          assessmentRequests?: number;
          summarizationRequests?: number;
        }
      > = {};
      processingConfig.timeSlots
        .filter((slot) => slot && slot.documentType && slot.documentType.trim() !== '') // Only process slots with valid document types
        .forEach((slot) => {
          const docType = slot.documentType;
          const docsPerHour = parseInt(String(slot.docsPerHour || 0), 10); // Default to 0 if empty

          if (!aggregatedDocConfigs[docType]) {
            // Find the document config for this type
            const docConfig = documentConfigs.find((config) => config.type === docType);

            if (!docConfig) {
              console.warn(`Document type "${docType}" in schedule not found in Expected Token Usage. Skipping.`);
              return; // Skip this time slot if document config not found
            }

            aggregatedDocConfigs[docType] = {
              type: docType,
              avgPages: parseFloat(String(docConfig.avgPages)) || 0, // Use actual pages, 0 if not available
              ocrTokens: parseFloat(String(docConfig.ocrTokens || 0)),
              classificationTokens: parseFloat(String(docConfig.classificationTokens || 0)),
              extractionTokens: parseFloat(String(docConfig.extractionTokens || 0)),
              summarizationTokens: parseFloat(String(docConfig.summarizationTokens || 0)),
              assessmentTokens: parseFloat(String(docConfig.assessmentTokens || 0)),
              docsPerHour,
            };
          } else {
            // Add to existing aggregation
            aggregatedDocConfigs[docType].docsPerHour += docsPerHour;
          }
        });

      // Convert aggregated configs to array format expected by API
      // Filter out any configs without a valid type or avgPages
      const documentConfigsForAPI = Object.values(aggregatedDocConfigs).filter(
        (config) => config.type && config.type.trim() !== '' && config.avgPages > 0,
      );

      // Get dynamic model configuration from deployment settings and configuration
      // Only include OCR model when OCR backend is Bedrock (not Textract)
      const modelConfig = {
        extraction_model: configuration?.extraction?.model,
        classification_model: configuration?.classification?.model,
        assessment_model: configuration?.extraction?.confidence?.model ?? configuration?.assessment?.model,
        summarization_model: configuration?.summarization?.model,
        ocr_model: configuration?.ocr?.backend === 'bedrock' ? configuration?.ocr?.model_id : null,
      };

      // Add request count data to document configs for API
      const documentConfigsWithRequests = documentConfigsForAPI.map((config) => ({
        ...config,
        // Add request counts if available from metering data
        ocrRequests: config.ocrRequests || 1,
        classificationRequests: config.classificationRequests || 1,
        extractionRequests: config.extractionRequests || 1,
        assessmentRequests: config.assessmentRequests || 1,
        summarizationRequests: config.summarizationRequests || 1,
      }));

      // Convert timeSlots docsPerHour values to integers for API
      const timeSlotsForAPI = processingConfig.timeSlots.map((slot) => ({
        ...slot,
        docsPerHour: parseInt(String(slot.docsPerHour || 0), 10),
      }));

      const input = {
        documentConfigs: documentConfigsWithRequests,
        maxAllowedLatency: parseFloat(processingConfig.maxLatency),
        totalDocsPerHour,
        userConfig: JSON.stringify(modelConfig),
        pattern: getDeployedPattern().toLowerCase(), // API expects lowercase pattern name
        timeSlots: JSON.stringify(timeSlotsForAPI),
        granularAssessmentEnabled: isGranularAssessmentEnabled(),
        latencyMetricsHours: parseInt(latencyTimeRange.value, 10),
      };

      // Validate input before sending
      if (!input.documentConfigs || input.documentConfigs.length === 0) {
        setResults({
          success: false,
          errorMessage:
            'No valid document configurations found. Please ensure: \n' +
            '1. Document types are configured in "Expected Token Usage by Document Type" section\n' +
            '2. Each document type has "Avg Pages/Doc" filled in (use "Populate tokens from Documents" button)\n' +
            '3. Processing Schedule has time slots with valid document types selected',
          metrics: [{ label: 'Validation Error', value: 'Missing Configuration' }],
          quotaRequirements: [],
        });
        setHasCalculated(true);
        setLoading(false);
        return;
      }

      if (!input.pattern || input.pattern === 'loading...' || input.pattern === 'configuration_required') {
        throw new Error('Pattern not loaded yet, please wait and try again');
      }

      if (!input.timeSlots || input.timeSlots === '[]') {
        throw new Error('No processing schedule configured');
      }

      // Always ensure we show the UI components - remove validation blocks that prevent rendering
      // Set hasCalculated to true FIRST so UI shows regardless of validation
      setHasCalculated(true);

      // Validate input but continue processing even with warnings
      if (!input.pattern || input.pattern === 'loading...' || input.pattern === 'configuration_required') {
        setResults({
          success: false,
          errorMessage: 'Pattern not detected. Please ensure deployment is complete.',
          metrics: [
            { label: 'Total Docs', value: '0' },
            { label: 'Total Pages', value: '0' },
            { label: 'Total Tokens', value: '0M' },
          ],
          quotaRequirements: [],
        });
        setLoading(false);
        return;
      }

      if (!input.documentConfigs || input.documentConfigs.length === 0) {
        setResults({
          success: false,
          errorMessage: 'No document configurations provided. Please add at least one document type.',
          metrics: [
            { label: 'Total Docs', value: '0' },
            { label: 'Total Pages', value: '0' },
            { label: 'Total Tokens', value: '0M' },
          ],
          quotaRequirements: [],
        });
        setLoading(false);
        return;
      }

      if (input.totalDocsPerHour === 0) {
        setResults({
          success: false,
          errorMessage: 'Total documents per hour is 0. Please specify processing volume in the schedule.',
          metrics: [
            { label: 'Total Docs', value: '0' },
            { label: 'Total Pages', value: '0' },
            { label: 'Total Tokens', value: '0M' },
          ],
          quotaRequirements: [],
        });
        setLoading(false);
        return;
      }

      console.log('🔍 Sending capacity calculation request:', input);

      // Create client inside the function to ensure Amplify is configured
      const client = generateClient();
      const response = await client.graphql({
        query: calculateCapacityOp,
        variables: { input: JSON.stringify(input) },
      });

      console.log('📊 Capacity calculation response:', response);

      const responseData = response.data;
      if (responseData?.calculateCapacity) {
        // The response now has the proper GraphQL structure
        const result = responseData.calculateCapacity;

        console.log('✅ API result:', result);

        // Check if API returned success
        if (result.success) {
          setResults(result as unknown as CapacityResult);
          setHasCalculated(true);
          return; // Exit early on success
        }
        // API returned structured error
        console.warn('⚠️ API returned error:', result.errorMessage);
        setResults({
          success: false,
          errorMessage: result.errorMessage || 'API calculation failed',
          metrics: [
            { label: 'Status', value: 'Calculation Failed' },
            { label: 'Error', value: result.errorMessage || 'Unknown error' },
          ],
          quotaRequirements: [],
        });
      } else {
        // API returned null - Lambda function failed
        console.warn('⚠️ API returned null - Lambda function may have failed');
        setResults({
          success: false,
          errorMessage: 'Capacity calculation service is unavailable. The Lambda function may have encountered an error.',
          metrics: [
            { label: 'Status', value: 'Service Unavailable' },
            { label: 'Reason', value: 'Lambda function returned null' },
          ],
          quotaRequirements: [],
        });
      }
    } catch (error) {
      console.error('❌ Capacity calculation error:', error);

      // Log GraphQL errors specifically
      const gqlErr = error as { errors?: unknown[] };
      if (gqlErr.errors) {
        gqlErr.errors.forEach((gqlError: unknown, index: number) => {
          console.error(`GraphQL Error ${index + 1}:`, gqlError);
        });
      }

      // Show user-friendly error message without throwing
      setResults({
        success: false,
        errorMessage: 'Capacity calculation service is temporarily unavailable. Please try again later.',
        metrics: [
          { label: 'Status', value: 'Service Error' },
          { label: 'Details', value: 'The calculation service encountered an error' },
        ],
        quotaRequirements: [],
      });
      setHasCalculated(true);
    } finally {
      setLoading(false);
      // Ensure hasCalculated is ALWAYS set to true when calculation completes
      setHasCalculated(true);
    }
  };

  // Memoized capacity calculations that update when config changes
  const capacityMetrics = useMemo(() => {
    // Helper function to safely parse numbers
    const safeParseInt = (value: string | number, defaultValue = 0) => {
      const parsed = parseInt(String(value), 10);
      return Number.isNaN(parsed) ? defaultValue : parsed;
    };

    const safeParseFloat = (value: string | number, defaultValue = 0): number => {
      const parsed = parseFloat(String(value));
      return Number.isNaN(parsed) ? defaultValue : parsed;
    };

    // Calculate totals from processing schedule
    const totalDocsPerHour = processingConfig.timeSlots.reduce((sum, slot) => {
      return sum + safeParseInt(slot.docsPerHour, 0);
    }, 0);

    // Calculate aggregated values from processing schedule
    let totalPagesPerHour = 0;
    let totalTokensPerHour = 0;

    processingConfig.timeSlots
      .filter((slot) => slot)
      .forEach((slot) => {
        const docType = slot.documentType || 'Other';
        const docsPerHour = safeParseInt(slot.docsPerHour, 0);

        const docConfig = documentConfigs.find((config) => config.type === docType) || {
          avgPages: '', // No default - must be calculated from actual documents
          ocrTokens: 0,
          classificationTokens: 0,
          extractionTokens: 0,
          summarizationTokens: 0,
          assessmentTokens: 0,
        };

        const pages = safeParseFloat(docConfig.avgPages, 0); // Use actual pages, 0 if not available
        const ocrTokens = safeParseFloat(docConfig.ocrTokens, 0);
        const classificationTokens = safeParseFloat(docConfig.classificationTokens, 0);
        const extractionTokens = safeParseFloat(docConfig.extractionTokens, 0);
        const summarizationTokens = safeParseFloat(docConfig.summarizationTokens, 0);
        const assessmentTokens = safeParseFloat(docConfig.assessmentTokens, 0);
        const totalDocTokens = ocrTokens + classificationTokens + extractionTokens + summarizationTokens + assessmentTokens;

        totalPagesPerHour += docsPerHour * pages;
        totalTokensPerHour += docsPerHour * totalDocTokens;
      });

    const safeTokensPerHour = Number.isNaN(totalTokensPerHour) ? 0 : totalTokensPerHour;

    // Use API response data if available and calculation has been performed
    if (results?.success && results?.metrics && hasCalculated) {
      return results.metrics.map((metric) => ({
        ...metric,
        // Ensure values are properly formatted
        value: metric.value || '0',
      }));
    }

    // Show calculated values or placeholders
    if (hasCalculated && results?.success === false) {
      // No cost calculation
    } else if (hasCalculated) {
      // No cost calculation
    }

    return [
      {
        label: 'Total Docs',
        value: totalDocsPerHour > 0 ? totalDocsPerHour.toString() : '0',
      },
      {
        label: 'Total Pages',
        value: totalPagesPerHour > 0 ? Math.round(totalPagesPerHour).toString() : '0',
      },
      {
        label: 'Total Tokens',
        value: safeTokensPerHour > 0 ? `${(safeTokensPerHour / 1000000).toFixed(2)}M` : '0M',
      },
    ];
  }, [documentConfigs, processingConfig, results, hasCalculated]);

  // Memoized quota data that updates when config changes
  const quotaData = useMemo(() => {
    // Use API response data if available - check for successful calculation
    if (results?.success && hasCalculated) {
      // Check for quotaRequirements array in the GraphQL response
      if (results.quotaRequirements && Array.isArray(results.quotaRequirements) && results.quotaRequirements.length > 0) {
        // Process the quota requirements to ensure proper display
        return results.quotaRequirements.map((quota) => {
          // Extract inference type from service name (e.g., "Classification (claude-3-haiku) - Tokens per Minute")
          const stepMatch = quota.service.match(/^(\w+)\s*\(/);
          const inferenceType = stepMatch ? stepMatch[1] : quota.usedFor || '';

          // Clean up model display name
          const modelDisplayName = quota.modelId ? (quota.modelId.split('.').pop() ?? '').split(':')[0] : '';

          // Transform category names to spell out abbreviations
          let category = quota.category || 'Bedrock Models';
          category = category.replace(/\bTPM\b/g, 'Tokens per Minute');
          category = category.replace(/\bRPM\b/g, 'Requests per Minute');

          return {
            ...quota,
            inferenceType,
            modelDisplayName,
            service: quota.service,
            category,
          };
        });
      }

      // Enhanced debugging for empty quota requirements
      if (results.quotaRequirements && Array.isArray(results.quotaRequirements) && results.quotaRequirements.length === 0) {
        // Intentionally empty: quota requirements may be empty when the backend has no quota data
      }

      // Fallback: build quota data from configuration if no quotaRequirements
      const quotaList: QuotaRequirement[] = [];

      // Get the models from configuration including OCR
      const models = [];

      // Add OCR model first if configured for document processing
      const hasOcrTokens = documentConfigs.some((config) => config.ocrTokens && parseFloat(String(config.ocrTokens)) > 0);
      if (hasOcrTokens && configuration?.ocr?.backend === 'bedrock') {
        const ocrModelId = configuration?.ocr?.model_id || configuration?.ocr?.model || 'us.amazon.nova-lite-v1:0';
        models.push({ id: ocrModelId, step: 'OCR' });
      }

      // Add other models
      [
        { id: configuration?.classification?.model, step: 'Classification' },
        { id: configuration?.extraction?.model, step: 'Extraction' },
        { id: configuration?.extraction?.confidence?.model ?? configuration?.assessment?.model, step: 'Assessment' },
        { id: configuration?.summarization?.model, step: 'Summarization' },
      ].forEach((model) => {
        if (model.id) models.push(model);
      });

      // Add quota values for each configured model - use only API data
      models.forEach(({ id: modelId, step }) => {
        const _displayName = getModelDisplayName(modelId);

        // Calculate required quota for this specific inference step
        let peakTokensPerMinute = 0;
        processingConfig.timeSlots.forEach((slot) => {
          const docsPerHour = parseInt(String(slot.docsPerHour || 0), 10);
          const docType = slot.documentType || '';

          if (docsPerHour > 0 && docType) {
            const docConfig = documentConfigs.find((config) => config.type === docType);
            if (docConfig) {
              let tokensPerDoc = 0;

              if (step === 'Classification') tokensPerDoc = parseFloat(String(docConfig.classificationTokens || 0));
              else if (step === 'Extraction') tokensPerDoc = parseFloat(String(docConfig.extractionTokens || 0));
              else if (step === 'Assessment') tokensPerDoc = parseFloat(String(docConfig.assessmentTokens || 0));
              else if (step === 'Summarization') tokensPerDoc = parseFloat(String(docConfig.summarizationTokens || 0));
              else if (step === 'OCR') tokensPerDoc = parseFloat(String(docConfig.ocrTokens || 0));

              const slotTokensPerMinute = (docsPerHour / 60) * tokensPerDoc;
              peakTokensPerMinute = Math.max(peakTokensPerMinute, slotTokensPerMinute);
            }
          }
        });

        const requiredQuota = Math.ceil(peakTokensPerMinute).toLocaleString();

        quotaList.push({
          service: `${modelId} (${step}) - Tokens per Minute`,
          category: 'Bedrock Models',
          currentQuota: 'API Required',
          requiredQuota,
          statusText: '⚠️ Check AWS Console',
          modelId,
        });
      });

      return quotaList;
    }
    return [];
  }, [documentConfigs, processingConfig, configuration, results, manualPattern, deploymentSettings]);

  const groupQuotasByCategory = (quotas: QuotaRequirement[]) => {
    const grouped: Record<string, QuotaRequirement[]> = {};
    quotas.forEach((quota) => {
      const category = quota.category || 'Other Services';

      // Show ALL Bedrock models, not just classification
      if (!grouped[category]) {
        grouped[category] = [];
      }
      grouped[category].push(quota);
    });
    return grouped;
  };

  const aggregateQuotasByModel = (quotas: QuotaRequirement[]) => {
    const aggregated: Record<
      string,
      {
        modelId: string;
        totalRequiredTPM: number;
        totalRequiredRPM: number;
        currentQuotaTPM: string;
        currentQuotaRPM: string;
        needsIncreaseTPM: boolean;
        needsIncreaseRPM: boolean;
        steps: string[];
      }
    > = {};

    quotas.forEach((quota) => {
      const modelId = quota.modelId;
      if (!modelId) return;

      // Determine if this is TPM or RPM based on category
      const isTPM = quota.category && quota.category.includes('Tokens per Minute');
      const isRPM = quota.category && quota.category.includes('Requests per Minute');

      // Parse required quota (remove commas and convert to number)
      const requiredNum = parseInt((quota.requiredQuota || '0').replace(/,/g, ''), 10);

      if (!aggregated[modelId]) {
        aggregated[modelId] = {
          modelId,
          totalRequiredTPM: 0,
          totalRequiredRPM: 0,
          currentQuotaTPM: 'API Required',
          currentQuotaRPM: 'API Required',
          needsIncreaseTPM: false,
          needsIncreaseRPM: false,
          steps: [],
        };
      }

      // Check if this quota needs increase
      const needsIncrease = quota.statusText.includes('⚠️') || quota.statusText.includes('Increase Needed');

      // Aggregate TPM or RPM separately
      if (isTPM) {
        aggregated[modelId].totalRequiredTPM += requiredNum;
        aggregated[modelId].currentQuotaTPM = quota.currentQuota;
        if (needsIncrease) {
          aggregated[modelId].needsIncreaseTPM = true;
        }
      } else if (isRPM) {
        aggregated[modelId].totalRequiredRPM += requiredNum;
        aggregated[modelId].currentQuotaRPM = quota.currentQuota;
        if (needsIncrease) {
          aggregated[modelId].needsIncreaseRPM = true;
        }
      }

      aggregated[modelId].steps.push(quota.inferenceType || quota.service);
    });

    // Convert to array and format
    return Object.values(aggregated).map((item) => {
      // Calculate utilization percentages
      let utilizationTPM = 0;
      let utilizationRPM = 0;

      if (item.totalRequiredTPM > 0 && item.currentQuotaTPM !== 'API Required') {
        const currentTPM = parseInt(item.currentQuotaTPM.replace(/,/g, ''), 10);
        if (currentTPM > 0) {
          utilizationTPM = Math.round((item.totalRequiredTPM / currentTPM) * 100);
        }
      }

      if (item.totalRequiredRPM > 0 && item.currentQuotaRPM !== 'API Required') {
        const currentRPM = parseInt(item.currentQuotaRPM.replace(/,/g, ''), 10);
        if (currentRPM > 0) {
          utilizationRPM = Math.round((item.totalRequiredRPM / currentRPM) * 100);
        }
      }

      return {
        ...item,
        totalRequiredTPM: item.totalRequiredTPM > 0 ? item.totalRequiredTPM.toLocaleString() : '-',
        totalRequiredRPM: item.totalRequiredRPM > 0 ? item.totalRequiredRPM.toLocaleString() : '-',
        stepsUsed: [...new Set(item.steps)].join(', '), // Remove duplicates
        utilizationTPM,
        utilizationRPM,
      };
    });
  };

  const getDetailedRequirementsByModel = (quotas: QuotaRequirement[]) => {
    const byModel: Record<
      string,
      Array<{
        modelId: string;
        inferenceType: string;
        requiredTPM: string;
        currentTPM: string;
        statusTPM: string;
        requiredRPM: string;
        currentRPM: string;
        statusRPM: string;
      }>
    > = {};

    quotas.forEach((quota) => {
      const modelId = quota.modelId;
      if (!modelId) return;

      const isTPM = quota.category && quota.category.includes('Tokens per Minute');
      const isRPM = quota.category && quota.category.includes('Requests per Minute');
      const inferenceType = quota.inferenceType || quota.service.split('(')[0].trim();

      if (!byModel[modelId]) {
        byModel[modelId] = [];
      }

      // Find or create entry for this inference type
      let entry = byModel[modelId].find((e) => e.inferenceType === inferenceType);
      if (!entry) {
        entry = {
          modelId,
          inferenceType,
          requiredTPM: '-',
          currentTPM: 'API Required',
          statusTPM: '',
          requiredRPM: '-',
          currentRPM: 'API Required',
          statusRPM: '',
        };
        byModel[modelId].push(entry);
      }

      // Populate TPM or RPM data
      if (isTPM) {
        entry.requiredTPM = quota.requiredQuota;
        entry.currentTPM = quota.currentQuota;
        entry.statusTPM = quota.statusText;
      } else if (isRPM) {
        entry.requiredRPM = quota.requiredQuota;
        entry.currentRPM = quota.currentQuota;
        entry.statusRPM = quota.statusText;
      }
    });

    return byModel;
  };

  const exportCapacityPlan = () => {
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
    const filename = prompt('Enter filename for export:', `capacity-plan-${getDeployedPattern()}-${timestamp}`);
    if (!filename) return;

    let csvContent = '';

    // ===== HEADER SECTION =====
    csvContent += '==============================================\n';
    csvContent += 'CAPACITY PLANNING EXPORT\n';
    csvContent += '==============================================\n\n';

    csvContent += 'GENERAL INFORMATION\n';
    csvContent += '-------------------\n';
    csvContent += `Export Date:,${new Date().toISOString()}\n`;
    csvContent += `Pattern:,${getDeployedPattern().toUpperCase()}\n`;

    // Configuration profile information
    const currentVersion = versions.find((v) => v.versionName === selectedConfigVersion?.value);
    csvContent += `Configuration Profile:,${selectedConfigVersion?.value || 'Not selected'}\n`;
    if (currentVersion?.description) {
      csvContent += `Version Description:,"${currentVersion.description}"\n`;
    }
    csvContent += `Version Active:,${currentVersion?.isActive ? 'Yes' : 'No'}\n`;
    csvContent += '\n';

    // ===== MODEL CONFIGURATION =====
    csvContent += 'MODEL CONFIGURATION\n';
    csvContent += '-------------------\n';
    csvContent += 'Processing Step,Model ID\n';
    if (configuration?.ocr?.backend === 'bedrock') {
      csvContent += `OCR,"${configuration?.ocr?.model_id || 'Not configured'}"\n`;
    } else {
      csvContent += `OCR,Textract (No model)\n`;
    }
    csvContent += `Classification,"${configuration?.classification?.model || 'Not configured'}"\n`;
    csvContent += `Extraction,"${configuration?.extraction?.model || 'Not configured'}"\n`;
    csvContent += `Assessment,"${(configuration?.extraction?.confidence?.model ?? configuration?.assessment?.model) || 'Not configured'}"\n`;
    csvContent += `Summarization,"${configuration?.summarization?.model || 'Not configured'}"\n`;
    csvContent += '\n';

    // ===== EXPECTED TOKEN USAGE BY DOCUMENT TYPE =====
    csvContent += 'EXPECTED TOKEN USAGE BY DOCUMENT TYPE\n';
    csvContent += '--------------------------------------\n';
    csvContent += 'Document Type,Avg Pages/Doc,';
    if (configuration?.ocr?.backend === 'bedrock') csvContent += 'OCR Tokens,';
    csvContent += 'Classification Tokens,Extraction Tokens,Assessment Tokens,Summarization Tokens\n';

    documentConfigs.forEach((config) => {
      let row = `"${config.type}","${config.avgPages}",`;
      if (configuration?.ocr?.backend === 'bedrock') row += `"${config.ocrTokens || '0'}",`;
      row += `"${config.classificationTokens || '0'}","${config.extractionTokens || '0'}","${config.assessmentTokens || '0'}","${
        config.summarizationTokens || '0'
      }"\n`;
      csvContent += row;
    });
    csvContent += '\n';

    // ===== PROCESSING SCHEDULE =====
    csvContent += 'PROCESSING SCHEDULE\n';
    csvContent += '-------------------\n';
    csvContent += 'Hour,Document Type,Documents Per Hour\n';
    processingConfig.timeSlots.forEach((slot) => {
      csvContent += `"${slot.hour}:00","${slot.documentType}","${slot.docsPerHour}"\n`;
    });
    csvContent += '\n';

    // ===== CAPACITY REQUIREMENTS =====
    if (hasCalculated) {
      csvContent += 'CAPACITY REQUIREMENTS SUMMARY\n';
      csvContent += '-----------------------------\n';
      csvContent += `Maximum Allowed Latency:,${processingConfig.maxLatency} minutes\n`;
      capacityMetrics.forEach((metric) => {
        csvContent += `"${metric.label}","${metric.value}"\n`;
      });
      csvContent += '\n';
    }

    // ===== AGGREGATE QUOTA REQUIREMENTS (What to request in AWS) =====
    if (quotaData.length > 0) {
      const bedrockQuotas = quotaData.filter((q) => q.category && q.category.includes('Bedrock'));
      if (bedrockQuotas.length > 0) {
        const aggregatedModels = aggregateQuotasByModel(bedrockQuotas);

        csvContent += 'AGGREGATE QUOTA REQUIREMENTS BY MODEL\n';
        csvContent += '======================================\n';
        csvContent += '*** Request these values in AWS Service Quotas ***\n';
        csvContent += '\n';
        csvContent += 'Model ID,Used For,Required TPM,Current TPM,TPM Status,Required RPM,Current RPM,RPM Status\n';

        aggregatedModels.forEach((model) => {
          const tpmStatus = model.totalRequiredTPM === '-' ? 'N/A' : model.needsIncreaseTPM ? 'Insufficient' : 'Sufficient';
          const rpmStatus = model.totalRequiredRPM === '-' ? 'N/A' : model.needsIncreaseRPM ? 'Insufficient' : 'Sufficient';
          const tpmUtilization = model.utilizationTPM > 0 ? ` (${model.utilizationTPM}%)` : '';
          const rpmUtilization = model.utilizationRPM > 0 ? ` (${model.utilizationRPM}%)` : '';

          csvContent += `"${model.modelId}","${model.stepsUsed}","${model.totalRequiredTPM}","${model.currentQuotaTPM}","${tpmStatus}${tpmUtilization}","${model.totalRequiredRPM}","${model.currentQuotaRPM}","${rpmStatus}${rpmUtilization}"\n`;
        });
        csvContent += '\n';
      }

      // ===== DETAILED QUOTA REQUIREMENTS =====
      csvContent += 'DETAILED QUOTA REQUIREMENTS BY PROCESSING STEP\n';
      csvContent += '----------------------------------------------\n';
      csvContent += 'Model ID,Processing Step,Category,Current Quota,Required Quota,Status\n';
      quotaData.forEach((quota) => {
        const inferenceType = quota.inferenceType || quota.service.split('(')[0].trim();
        csvContent += `"${quota.modelId || ''}","${inferenceType}","${quota.category}","${quota.currentQuota}","${quota.requiredQuota}","${
          quota.statusText
        }"\n`;
      });
      csvContent += '\n';
    }

    // ===== FOOTER =====
    csvContent += '==============================================\n';
    csvContent += 'END OF CAPACITY PLANNING EXPORT\n';
    csvContent += '==============================================\n';

    const dataBlob = new Blob([csvContent], { type: 'text/csv' });
    const url = URL.createObjectURL(dataBlob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `${filename}.csv`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  };

  return (
    <Container>
      <SpaceBetween size="l">
        {/* Notifications */}
        {notifications.length > 0 && (
          <Flashbar items={notifications as unknown as React.ComponentProps<typeof Flashbar>['items']} stackItems />
        )}

        {/* Header */}
        <Header
          variant="h1"
          actions={
            <SpaceBetween direction="horizontal" size="s">
              <FormField label="Configuration Profile">
                <Select
                  selectedOption={selectedConfigVersion}
                  onChange={({ detail }) => setSelectedConfigVersion(detail.selectedOption as SelectOption)}
                  options={getVersionOptions()}
                  placeholder={versions.length === 0 ? 'Loading profiles...' : 'Select configuration profile'}
                  disabled={versions.length === 0}
                  loadingText="Loading profiles..."
                />
              </FormField>
              <Button variant="primary" iconName="download" onClick={exportCapacityPlan}>
                Export Capacity Plan
              </Button>
            </SpaceBetween>
          }
        >
          Capacity Planning <Badge color="grey">Beta</Badge>
        </Header>

        {/* Configuration Status Alert */}
        {configuration && (
          <Alert type="success">
            <strong>✓</strong> Using configuration profile: <Badge color="green">{selectedConfigVersion?.value || 'default'}</Badge>
            {selectedConfigVersion?.label?.includes('Active') && <Badge color="blue">Active</Badge>}
            <Box margin={{ top: 'xs' }}>
              Token calculations and processing times are loaded from your pattern configuration. Update models in{' '}
              <strong>View/Edit Configuration</strong> to see changes reflected immediately, or select a different configuration profile
              above.
            </Box>
          </Alert>
        )}

        {!configuration && (
          <Alert type="warning">
            <strong>⚠️ Configuration not loaded.</strong> Please visit the <strong>View/Edit Configuration</strong> tab first to load your
            pattern configuration, then return to Capacity Planning.
          </Alert>
        )}

        {getDeployedPattern() === 'PATTERN-2' && !configuration?.classification?.model && !manualPattern && (
          <Alert type="warning">
            <strong>⚠️ Pattern Detection Used Fallback.</strong> Using Pattern 2 default. Configure models in{' '}
            <strong>View/Edit Configuration</strong> for accurate calculations.
            <Box margin={{ top: 'm' }}>
              <strong>Manual Override Available:</strong> If you know your actual deployment pattern:
              <Box margin={{ top: 'xs' }}>
                <SpaceBetween direction="horizontal" size="s">
                  <Button variant="normal" onClick={() => setManualPattern('PATTERN-1')}>
                    Pattern 1 (BDA)
                  </Button>
                  <Button variant="normal" onClick={() => setManualPattern('PATTERN-2')}>
                    Pattern 2 (Bedrock)
                  </Button>
                  <Button variant="normal" onClick={() => setManualPattern('PATTERN-3')}>
                    Pattern 3 (SageMaker)
                  </Button>
                </SpaceBetween>
              </Box>
            </Box>
          </Alert>
        )}

        {manualPattern && (
          <Alert type="warning">
            <strong>⚠️ Manual Pattern Override Active:</strong> Using manually selected {manualPattern}.
            <Button variant="link" onClick={() => setManualPattern(null)}>
              Reset to Automatic Detection
            </Button>
          </Alert>
        )}

        {/* Beta Feedback Alert */}
        <Alert type="info">
          <strong>🚀 Beta Feature:</strong> Capacity Planning is currently in beta. We&apos;re actively improving this feature based on user
          feedback.
          <Box margin={{ top: 'xs' }}>
            <strong>Help us improve:</strong> Share your feedback, report issues, or request enhancements on{' '}
            <a
              href="https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues"
              target="_blank"
              rel="noopener noreferrer"
            >
              GitHub Issues
            </a>
            . We appreciate your input!
          </Box>
        </Alert>

        {/* Expected Token Usage by Document Type */}
        <Container header={<Header variant="h2">Expected Token Usage by Document Type</Header>}>
          <SpaceBetween size="m">
            <Alert type="info">
              <strong>Token Usage Configuration:</strong> Enter the expected number of tokens per document type for each processing step.
              These values represent the tokens used for OCR (when using Bedrock), classification, extraction, assessment, and
              summarization. You can manually enter expected values, or use the &quot;Populate tokens from Documents&quot; button below to
              auto-fill from actual processed documents.
            </Alert>

            <Table
              columnDefinitions={[
                {
                  id: 'type',
                  header: (
                    <div style={{ fontSize: '0.85em', lineHeight: '1.2' }}>
                      <div>Document</div>
                      <div>Type</div>
                    </div>
                  ),
                  width: 220,
                  cell: (item) => (
                    <Select
                      selectedOption={documentTypeOptions.find((opt) => opt.value === item.type && !opt.disabled) ?? null}
                      onChange={({ detail }) => updateDocumentConfig(item.index ?? 0, 'type', detail.selectedOption.value ?? '')}
                      options={documentTypeOptions}
                      placeholder="Select document type"
                      expandToViewport
                    />
                  ),
                },
                {
                  id: 'avgPages',
                  header: (
                    <div style={{ fontSize: '0.85em', lineHeight: '1.2' }}>
                      <div>Avg Pages/</div>
                      <div>Doc</div>
                    </div>
                  ),
                  width: 110,
                  cell: (item) => (
                    <Input
                      type="number"
                      value={String(item.avgPages)}
                      onChange={({ detail }) => updateDocumentConfig(item.index ?? 0, 'avgPages', detail.value)}
                      step={0.1}
                      placeholder="Pages"
                    />
                  ),
                },
                ...(configuration?.ocr?.backend === 'bedrock'
                  ? [
                      {
                        id: 'ocrTokens',
                        header: (
                          <div>
                            <div>OCR</div>
                            <div style={{ fontSize: '0.8em', color: '#2f3b4a', fontWeight: 'normal' }}>
                              Model: {configuration?.ocr?.model_id || 'Not configured'}
                            </div>
                          </div>
                        ),
                        cell: (item: DocumentConfig) => (
                          <Input
                            type="number"
                            value={item.ocrTokens !== undefined && item.ocrTokens !== '' ? String(item.ocrTokens) : ''}
                            placeholder="OCR tokens"
                            onChange={({ detail }) =>
                              updateDocumentConfig(item.index ?? 0, 'ocrTokens', detail.value === '' ? '' : parseFloat(detail.value))
                            }
                          />
                        ),
                      },
                    ]
                  : []),
                {
                  id: 'classificationTokens',
                  header: (
                    <div>
                      <div>Classification</div>
                      <div style={{ fontSize: '0.8em', color: '#2f3b4a', fontWeight: 'normal' }}>
                        Model: {configuration?.classification?.model || 'Not configured'}
                      </div>
                    </div>
                  ),
                  cell: (item) => (
                    <Input
                      type="number"
                      value={
                        item.classificationTokens !== undefined && item.classificationTokens !== '' ? String(item.classificationTokens) : ''
                      }
                      placeholder="Classification tokens"
                      onChange={({ detail }) =>
                        updateDocumentConfig(item.index ?? 0, 'classificationTokens', detail.value === '' ? '' : parseFloat(detail.value))
                      }
                    />
                  ),
                },
                {
                  id: 'extractionTokens',
                  header: (
                    <div>
                      <div>Extraction</div>
                      <div style={{ fontSize: '0.8em', color: '#2f3b4a', fontWeight: 'normal' }}>
                        Model: {configuration?.extraction?.model || 'Not configured'}
                      </div>
                    </div>
                  ),
                  cell: (item) => (
                    <Input
                      type="number"
                      value={item.extractionTokens !== undefined && item.extractionTokens !== '' ? String(item.extractionTokens) : ''}
                      placeholder="Extraction tokens"
                      onChange={({ detail }) =>
                        updateDocumentConfig(item.index ?? 0, 'extractionTokens', detail.value === '' ? '' : parseFloat(detail.value))
                      }
                    />
                  ),
                },
                {
                  id: 'assessmentTokens',
                  header: (
                    <div>
                      <div>Assessment</div>
                      <div style={{ fontSize: '0.8em', color: '#2f3b4a', fontWeight: 'normal' }}>
                        Model: {(configuration?.extraction?.confidence?.model ?? configuration?.assessment?.model) || 'Not configured'}
                      </div>
                    </div>
                  ),
                  cell: (item) => (
                    <Input
                      type="number"
                      value={item.assessmentTokens !== undefined && item.assessmentTokens !== '' ? String(item.assessmentTokens) : ''}
                      placeholder="Assessment tokens"
                      onChange={({ detail }) =>
                        updateDocumentConfig(item.index ?? 0, 'assessmentTokens', detail.value === '' ? '' : parseFloat(detail.value))
                      }
                    />
                  ),
                },
                {
                  id: 'summarizationTokens',
                  header: (
                    <div>
                      <div>Summarization</div>
                      <div style={{ fontSize: '0.8em', color: '#2f3b4a', fontWeight: 'normal' }}>
                        Model: {configuration?.summarization?.model || 'Not configured'}
                      </div>
                    </div>
                  ),
                  cell: (item) => (
                    <Input
                      type="number"
                      value={
                        item.summarizationTokens !== undefined && item.summarizationTokens !== '' ? String(item.summarizationTokens) : ''
                      }
                      placeholder="Summarization tokens"
                      onChange={({ detail }) =>
                        updateDocumentConfig(item.index ?? 0, 'summarizationTokens', detail.value === '' ? '' : parseFloat(detail.value))
                      }
                    />
                  ),
                },
                {
                  id: 'actions',
                  header: '',
                  cell: (item) => (
                    <Button
                      variant="icon"
                      iconName="close"
                      onClick={() => removeDocumentConfig(item.index ?? 0)}
                      ariaLabel="Remove document configuration"
                    />
                  ),
                },
              ]}
              items={documentConfigs.map((config, index) => ({ ...config, index }))}
              empty={<Box textAlign="center">No document configurations</Box>}
              variant="embedded"
              resizableColumns
            />

            <SpaceBetween direction="horizontal" size="s">
              <Button
                variant="link"
                onClick={() => {
                  const newConfig = {
                    type: '',
                    avgPages: '', // No default - must be calculated from actual documents
                    ocrTokens: '',
                    classificationTokens: '',
                    extractionTokens: '',
                    summarizationTokens: '',
                    assessmentTokens: '',
                  };
                  setDocumentConfigs([...documentConfigs, newConfig]);
                }}
              >
                + Add Document Type
              </Button>

              <Button variant="normal" iconName="refresh" onClick={fetchRecentDocuments}>
                Populate tokens from Documents
              </Button>
            </SpaceBetween>

            <Button
              variant="normal"
              iconName="upload"
              onClick={() => {
                const input = document.createElement('input');
                input.type = 'file';
                input.accept = '.csv';
                input.onchange = (e) => {
                  const file = (e.target as HTMLInputElement).files?.[0];
                  if (!file) return;

                  const reader = new FileReader();
                  reader.onload = (event) => {
                    try {
                      const csv = event.target?.result as string;
                      const lines = csv.split('\n').filter((line) => line.trim());

                      if (lines.length < 2) {
                        alert('Error: CSV file must have at least a header row and one data row.');
                        return;
                      }

                      // Check CSV structure
                      const headerLine = lines[0];
                      const headers = headerLine.split(',').map((h) => h.trim().toLowerCase());
                      const sampleDataLine = lines[1];
                      const _sampleValues = sampleDataLine.split(',').map((v: string) => v.trim());

                      // Check if OCR column exists by looking for 'ocr' in headers
                      const ocrColumnIndex = headers.findIndex((header) => header.includes('ocr'));
                      const hasValidOcrColumn = ocrColumnIndex !== -1;

                      // Check if OCR column is missing when Bedrock OCR is configured
                      const isBedrockOcrConfigured = configuration?.ocr?.backend === 'bedrock';

                      if (isBedrockOcrConfigured && !hasValidOcrColumn) {
                        alert(
                          'Error: OCR tokens column is missing from CSV file.\n\n' +
                            'When Bedrock OCR is configured, your CSV must include an "OCR" column.\n' +
                            'Expected columns: Document Type, Average Pages, OCR Tokens, ' +
                            'Classification Tokens, ...\n\n' +
                            'Please add the OCR tokens column to your CSV file.',
                        );
                        return;
                      }

                      const importedConfigs = [];

                      for (let i = 1; i < lines.length; i += 1) {
                        const values = lines[i].split(',').map((v) => v.trim());
                        if (values.length >= 2) {
                          // At least type and avgPages required
                          const config = {
                            type: values[0] || '',
                            avgPages: parseFloat(values[1]) || 0, // Use actual pages from CSV, 0 if not provided
                            ocrTokens: hasValidOcrColumn && values[ocrColumnIndex] ? values[ocrColumnIndex] : '',
                            classificationTokens: values[hasValidOcrColumn ? ocrColumnIndex + 1 : 2] || '',
                            extractionTokens: values[hasValidOcrColumn ? ocrColumnIndex + 2 : 3] || '',
                            assessmentTokens: values[hasValidOcrColumn ? ocrColumnIndex + 3 : 4] || '',
                            summarizationTokens: values[hasValidOcrColumn ? ocrColumnIndex + 4 : 5] || '',
                          };
                          importedConfigs.push(config);
                        }
                      }

                      // Additional validation for OCR tokens if Bedrock OCR is configured
                      if (isBedrockOcrConfigured && importedConfigs.length > 0) {
                        const missingOcrTokens = importedConfigs.some(
                          (config) => !config.ocrTokens || config.ocrTokens === '' || Number.isNaN(parseFloat(String(config.ocrTokens))),
                        );
                        if (missingOcrTokens) {
                          alert(
                            'Error: OCR tokens are required in CSV when Bedrock OCR is configured.\n\n' +
                              'Please ensure all rows have valid numeric OCR token values ' +
                              'in the third column of your CSV file.\n\n' +
                              'Example CSV format:\n' +
                              'Document Type,Avg Pages,OCR Tokens,Classification Tokens,...\n' +
                              'Invoice,2,1500,800,...',
                          );
                          return;
                        }
                      }

                      if (importedConfigs.length > 0) {
                        setDocumentConfigs(importedConfigs);
                        alert(`Imported ${importedConfigs.length} document configurations`);
                      }
                    } catch (error) {
                      alert('Error parsing CSV file. Please check format.');
                    }
                  };
                  reader.readAsText(file);
                };
                input.click();
              }}
            >
              Import CSV
            </Button>

            <Button
              variant="normal"
              iconName="download"
              onClick={() => {
                // Create CSV content for document configurations
                let csvContent =
                  'Document Type,Average Pages,OCR Tokens,Classification Tokens,Extraction Tokens,Assessment Tokens,Summarization Tokens\n';

                documentConfigs.forEach((config) => {
                  const row = [
                    `"${config.type || ''}"`,
                    `"${config.avgPages || ''}"`, // Export actual pages, empty if not calculated
                    `"${config.ocrTokens || ''}"`,
                    `"${config.classificationTokens || ''}"`,
                    `"${config.extractionTokens || ''}"`,
                    `"${config.assessmentTokens || ''}"`,
                    `"${config.summarizationTokens || ''}"`,
                  ].join(',');
                  csvContent += `${row}\n`;
                });

                const dataBlob = new Blob([csvContent], { type: 'text/csv' });
                const url = URL.createObjectURL(dataBlob);
                const link = document.createElement('a');
                link.href = url;
                link.download = 'document-configurations.csv';
                document.body.appendChild(link);
                link.click();
                document.body.removeChild(link);
                URL.revokeObjectURL(url);
              }}
            >
              Export CSV
            </Button>
          </SpaceBetween>
        </Container>

        {/* Processing Schedule */}
        <Container header={<Header variant="h2">Processing Schedule</Header>}>
          <SpaceBetween size="m">
            <Alert type="info">
              <strong>Processing Schedule Configuration:</strong> Define your expected document processing load by hour. Enter the number of
              documents you expect to process per hour for each time slot and document type. Leave empty if no processing is expected during
              that time.
            </Alert>
            <Table
              columnDefinitions={[
                {
                  id: 'hour',
                  header: 'Processing Hours',
                  cell: (item) => (
                    <Select
                      selectedOption={item.hour ? (timeSlotOptions.find((opt) => opt.value === item.hour) ?? null) : null}
                      onChange={({ detail }) => updateTimeSlot(item.index, 'hour', detail.selectedOption.value ?? '')}
                      options={timeSlotOptions}
                      placeholder="Select processing hour"
                      expandToViewport
                    />
                  ),
                },
                {
                  id: 'documentType',
                  header: 'Document Type',
                  cell: (item) => (
                    <Select
                      selectedOption={
                        item.documentType && item.documentType !== ''
                          ? (scheduleDocumentTypeOptions.find(
                              (opt) => opt.value === item.documentType && !(opt as { disabled?: boolean }).disabled,
                            ) ?? null)
                          : null
                      }
                      onChange={({ detail }) => updateTimeSlot(item.index, 'documentType', detail.selectedOption.value ?? '')}
                      options={scheduleDocumentTypeOptions}
                      placeholder="Select document type"
                      expandToViewport
                    />
                  ),
                },
                {
                  id: 'docsPerHour',
                  header: 'Docs/Hour',
                  cell: (item) => (
                    <Input
                      type="number"
                      value={item.docsPerHour || ''}
                      onChange={({ detail }) => updateTimeSlot(item.index, 'docsPerHour', detail.value)}
                      placeholder="Enter docs per hour"
                    />
                  ),
                },
                {
                  id: 'actions',
                  header: 'Actions',
                  cell: (item) => (
                    <Button
                      variant="icon"
                      iconName="close"
                      onClick={() => removeTimeSlot(item.index)}
                      disabled={processingConfig.timeSlots.length === 1}
                      ariaLabel="Remove time slot"
                    />
                  ),
                },
              ]}
              items={processingConfig.timeSlots.map((slot, index) => ({ ...slot, index }))}
              empty={
                <Box textAlign="center" color="text-status-info">
                  No active processing hours configured. Add time slots with Docs/Hour &gt; 0 to see them here.
                </Box>
              }
              variant="embedded"
              resizableColumns
            />
            <SpaceBetween direction="horizontal" size="s">
              <ButtonDropdown
                variant="normal"
                onItemClick={({ detail }) => {
                  if (detail.id === 'add-single') {
                    addTimeSlot();
                  } else if (detail.id === 'add-all-9am') {
                    addAllDocumentTypesAt9AM();
                  } else if (detail.id === 'auto-business') {
                    autoFillBusinessHours();
                  } else if (detail.id === 'auto-fullday') {
                    autoFillFullDay();
                  }
                }}
                items={[
                  { id: 'add-single', text: '+ Add Single Time Slot', description: 'Add one empty time slot row' },
                  {
                    id: 'add-all-9am',
                    text: 'Add All Document Types at 9 AM',
                    description: 'Create one row per document type at 9:00 AM',
                  },
                  {
                    id: 'auto-business',
                    text: 'Auto-fill Business Hours (8 AM - 6 PM)',
                    description: 'Create rows for each document type for each business hour',
                  },
                  {
                    id: 'auto-fullday',
                    text: 'Auto-fill Full Day (24 hours)',
                    description: 'Create rows for each document type for all 24 hours',
                  },
                ]}
              >
                Add Time Slots
              </ButtonDropdown>
              <Button variant="normal" onClick={clearAllTimeSlots}>
                Clear All
              </Button>
            </SpaceBetween>

            <SpaceBetween direction="horizontal" size="s">
              <Button
                variant="normal"
                iconName="upload"
                onClick={() => {
                  const input = document.createElement('input');
                  input.type = 'file';
                  input.accept = '.csv';
                  input.onchange = (e) => {
                    const file = (e.target as HTMLInputElement).files?.[0];
                    if (!file) return;

                    const reader = new FileReader();
                    reader.onload = (event) => {
                      try {
                        const csv = event.target?.result as string;
                        const lines = csv.split('\n').filter((line) => line.trim());

                        if (lines.length < 2) {
                          alert('Error: CSV file must have at least a header row and one data row.');
                          return;
                        }

                        const importedSlots: TimeSlot[] = [];
                        for (let i = 1; i < lines.length; i += 1) {
                          const values = lines[i].split(',').map((v) => v.trim().replace(/"/g, ''));
                          if (values.length >= 3) {
                            // Extract hour from time format (e.g., "09:00" -> "9")
                            const hourStr = values[0].split(':')[0];
                            const hour = parseInt(hourStr, 10).toString();

                            const docsPerHour = values[2] || '';

                            // Skip rows with empty docs per hour
                            if (!docsPerHour || docsPerHour.trim() === '') {
                              continue;
                            }

                            const slot = {
                              hour,
                              documentType: values[1] || '',
                              docsPerHour,
                            };
                            importedSlots.push(slot);
                          }
                        }

                        if (importedSlots.length > 0) {
                          setProcessingConfig({
                            ...processingConfig,
                            timeSlots: importedSlots,
                          });
                          alert(`Imported ${importedSlots.length} schedule entries`);
                        }
                      } catch (error) {
                        alert('Error parsing CSV file. Please check format.\nExpected format: Hour,Document Type,Docs Per Hour');
                      }
                    };
                    reader.readAsText(file);
                  };
                  input.click();
                }}
              >
                Import Schedule CSV
              </Button>

              <Button
                variant="normal"
                iconName="download"
                onClick={() => {
                  // Create CSV content for processing schedule
                  let csvContent = 'Hour,Document Type,Docs Per Hour\n';

                  processingConfig.timeSlots.forEach((slot) => {
                    const hourDisplay = slot.hour ? `${String(slot.hour).padStart(2, '0')}:00` : '00:00';
                    const row = [`"${hourDisplay}"`, `"${slot.documentType || ''}"`, `"${slot.docsPerHour || ''}"`].join(',');
                    csvContent += `${row}\n`;
                  });

                  const dataBlob = new Blob([csvContent], { type: 'text/csv' });
                  const url = URL.createObjectURL(dataBlob);
                  const link = document.createElement('a');
                  link.href = url;
                  link.download = 'processing-schedule.csv';
                  document.body.appendChild(link);
                  link.click();
                  document.body.removeChild(link);
                  URL.revokeObjectURL(url);
                }}
              >
                Export Schedule CSV
              </Button>
            </SpaceBetween>
          </SpaceBetween>
        </Container>

        {/* Processing Configuration */}
        <Container header={<Header variant="h2">Processing Configuration</Header>}>
          <SpaceBetween size="m">
            <FormField
              label="Max Latency per Document (seconds)"
              description="Maximum acceptable processing time per document in seconds. This is the SLA threshold - documents should complete within this time. Use the Processing Latency Distribution chart below to see what percentage of documents meet your SLA."
              constraintText="Enter a value between 1 and 3600 seconds (1 hour). For example: 60 = 1 minute, 300 = 5 minutes."
            >
              <SpaceBetween direction="horizontal" size="s">
                <Input
                  type="number"
                  value={processingConfig.maxLatency}
                  onChange={({ detail }) => {
                    const value = parseInt(detail.value, 10);
                    if (!Number.isNaN(value) && value >= 1 && value <= 3600) {
                      setProcessingConfig({ ...processingConfig, maxLatency: detail.value });
                    } else if (detail.value === '') {
                      setProcessingConfig({ ...processingConfig, maxLatency: '' });
                    }
                  }}
                  placeholder="Enter seconds (e.g., 60 for 1 minute)"
                  inputMode="numeric"
                />
                <Box variant="span" color="text-body-secondary" padding={{ top: 'xs' }}>
                  = {processingConfig.maxLatency ? `${(parseFloat(processingConfig.maxLatency) / 60).toFixed(1)} minutes` : '-- minutes'}
                </Box>
              </SpaceBetween>
            </FormField>
            <Alert type="info">
              <strong>Understanding Max Latency:</strong> This is the maximum time (in seconds) that a single document should take to
              process end-to-end. After calculating capacity, check the <strong>Processing Latency Distribution</strong> section to see what
              percentage of your documents will complete within this SLA target (P50, P75, P90, P95, P99 percentiles).
              <Box margin={{ top: 'xs' }}>
                <strong>Quick reference:</strong> 60s = 1 min | 120s = 2 min | 300s = 5 min | 600s = 10 min | 900s = 15 min | 3600s = 1 hour
              </Box>
            </Alert>
          </SpaceBetween>
        </Container>

        {/* Calculate Button */}
        <Button
          variant="primary"
          onClick={calculateCapacityRequirements}
          loading={loading}
          disabled={!selectedConfigVersion || !configuration}
        >
          {!selectedConfigVersion ? 'Loading configuration profile...' : 'Calculate Capacity Requirements'}
        </Button>

        {results?.success === false && results?.metrics && (
          <Alert type="warning">
            <strong>⚠️ API Unavailable - Using Local Calculations:</strong> {results.errorMessage}
            <Box margin={{ top: 'xs' }}>
              The capacity calculation service is temporarily unavailable. Showing estimated values based on your configuration. Please try
              again later for precise calculations.
            </Box>
          </Alert>
        )}

        {/* Capacity Results - Only show after calculation */}
        {hasCalculated && (
          <Container header={<Header variant="h2">Capacity Results</Header>}>
            <Cards
              cardDefinition={{
                header: (item) => item.label,
                sections: [
                  {
                    content: (item) => (
                      <Box fontSize="display-l" fontWeight="bold" color="text-status-info">
                        {item.value}
                      </Box>
                    ),
                  },
                ],
              }}
              items={capacityMetrics.filter((metric) => metric.label !== 'Cost')}
              cardsPerRow={[{ cards: 3 }]}
            />
          </Container>
        )}

        {/* AWS Service Quota Analysis - Only show after calculation */}
        {hasCalculated && (
          <Container header={<Header variant="h2">AWS Service Quota Analysis</Header>}>
            <SpaceBetween size="m">
              <Box>Review current quota against projected requirements</Box>
              <Box>
                <strong>Max Allowed Latency (seconds):</strong> {processingConfig.maxLatency}
              </Box>

              {/* Debug information for empty quota data */}
              {quotaData.length === 0 && (
                <Alert type="warning" header="No Quota Requirements Found">
                  <SpaceBetween size="s">
                    <div>No quota requirements were returned from the capacity calculation.</div>
                    <div>
                      <strong>Possible causes:</strong>
                    </div>
                    <ul>
                      <li>No token usage configured (check Expected Token Usage section)</li>
                      <li>All OCR token values are zero</li>
                      <li>Backend calculation returned empty quota requirements</li>
                      <li>Pattern detection issue (check browser console for logs)</li>
                    </ul>
                    <div>
                      <strong>Debug info:</strong>
                    </div>
                    <div>Results success: {results?.success ? 'true' : 'false'}</div>
                    <div>Quota requirements length: {results?.quotaRequirements?.length || 0}</div>
                    <div>Has calculated: {hasCalculated ? 'true' : 'false'}</div>
                    <div>Pattern: {getDeployedPattern()}</div>
                    <div>
                      Document configs with OCR tokens:{' '}
                      {documentConfigs.filter((config) => config.ocrTokens && parseFloat(String(config.ocrTokens)) > 0).length}
                    </div>
                    <details>
                      <summary>Full API Response</summary>
                      <pre style={{ fontSize: '12px', maxHeight: '200px', overflow: 'auto' }}>{JSON.stringify(results, null, 2)}</pre>
                    </details>
                  </SpaceBetween>
                </Alert>
              )}

              <Button
                variant="primary"
                iconName="download"
                onClick={() => {
                  const filename = prompt('Enter filename for export:', `capacity-planning-${getDeployedPattern()}`);
                  if (!filename) return;

                  // Create CSV content
                  let csvContent = 'Service,Category,Current Quota,Required Quota,Status\n';

                  quotaData.forEach((quota) => {
                    const row = [
                      `"${quota.service}"`,
                      `"${quota.category}"`,
                      `"${quota.currentQuota}"`,
                      `"${quota.requiredQuota}"`,
                      `"${quota.statusText}"`,
                    ].join(',');
                    csvContent += `${row}\n`;
                  });

                  // Add capacity metrics section
                  csvContent += '\nCapacity Metrics\n';
                  csvContent += 'Metric,Value\n';
                  capacityMetrics.forEach((metric) => {
                    csvContent += `"${metric.label}","${metric.value}"\n`;
                  });

                  // Add processing schedule section
                  csvContent += '\nProcessing Schedule\n';
                  csvContent += 'Hour,Document Type,Docs Per Hour\n';
                  processingConfig.timeSlots.forEach((slot) => {
                    csvContent += `"${slot.hour}:00","${slot.documentType}","${slot.docsPerHour}"\n`;
                  });

                  const dataBlob = new Blob([csvContent], { type: 'text/csv' });
                  const url = URL.createObjectURL(dataBlob);
                  const link = document.createElement('a');
                  link.href = url;
                  link.download = `${filename}.csv`;
                  document.body.appendChild(link);
                  link.click();
                  document.body.removeChild(link);
                  URL.revokeObjectURL(url);
                }}
              >
                Export Quota Requirements
              </Button>

              {/* Aggregated Quota Requirements by Model */}
              {(() => {
                const bedrockQuotas = quotaData.filter((q) => q.category && q.category.includes('Bedrock'));
                if (bedrockQuotas.length === 0) return null;

                const aggregatedModels = aggregateQuotasByModel(bedrockQuotas);

                return (
                  <div>
                    <Header variant="h3">Total Quota Requirements by Model</Header>
                    <Alert type="info">
                      These are the aggregated quota values you should request in AWS Service Quotas. Each model&apos;s total includes all
                      processing steps (OCR, Classification, Extraction, Assessment, Summarization). Both TPM (Tokens per Minute) and RPM
                      (Requests per Minute) quotas may need to be increased.
                      <Box margin={{ top: 'xs' }}>
                        <strong>Note:</strong> All calculated quotas include a 10% safety buffer to account for burst traffic, token/request
                        variations, and system overhead.
                      </Box>
                    </Alert>
                    <Table
                      columnDefinitions={[
                        {
                          id: 'model',
                          header: 'Model',
                          cell: (item) => (
                            <div>
                              <div style={{ fontWeight: 'bold' }}>{item.modelId}</div>
                              <div style={{ fontSize: '12px', color: '#687078' }}>Used for: {item.stepsUsed}</div>
                            </div>
                          ),
                          width: 250,
                        },
                        {
                          id: 'requiredTPM',
                          header: 'Required TPM',
                          cell: (item) => <strong>{item.totalRequiredTPM}</strong>,
                          width: 140,
                        },
                        {
                          id: 'currentTPM',
                          header: 'Current TPM',
                          cell: (item) => item.currentQuotaTPM,
                          width: 140,
                        },
                        {
                          id: 'statusTPM',
                          header: 'TPM Status',
                          width: 150,
                          cell: (item) => {
                            // Only show status if TPM is used (not "-")
                            if (item.totalRequiredTPM === '-') {
                              return <span style={{ color: '#687078' }}>N/A</span>;
                            }
                            const badge = item.needsIncreaseTPM ? (
                              <Badge color="red">✗ Insufficient</Badge>
                            ) : (
                              <Badge color="green">✓ Sufficient</Badge>
                            );
                            const utilizationText = item.utilizationTPM > 0 ? `${item.utilizationTPM}%` : '';
                            return (
                              <div>
                                <div style={{ fontWeight: 'bold', marginBottom: '4px' }}>{badge}</div>
                                {utilizationText && (
                                  <div style={{ fontSize: '12px', color: '#687078' }}>
                                    {item.needsIncreaseTPM ? `${utilizationText} of current` : `${utilizationText} utilization`}
                                  </div>
                                )}
                              </div>
                            );
                          },
                        },
                        {
                          id: 'requiredRPM',
                          header: 'Required RPM',
                          cell: (item) => <strong>{item.totalRequiredRPM}</strong>,
                          width: 140,
                        },
                        {
                          id: 'currentRPM',
                          header: 'Current RPM',
                          cell: (item) => item.currentQuotaRPM,
                          width: 140,
                        },
                        {
                          id: 'statusRPM',
                          header: 'RPM Status',
                          width: 150,
                          cell: (item) => {
                            // Only show status if RPM is used (not "-")
                            if (item.totalRequiredRPM === '-') {
                              return <span style={{ color: '#687078' }}>N/A</span>;
                            }
                            const badge = item.needsIncreaseRPM ? (
                              <Badge color="red">✗ Insufficient</Badge>
                            ) : (
                              <Badge color="green">✓ Sufficient</Badge>
                            );
                            const utilizationText = item.utilizationRPM > 0 ? `${item.utilizationRPM}%` : '';
                            return (
                              <div>
                                <div style={{ fontWeight: 'bold', marginBottom: '4px' }}>{badge}</div>
                                {utilizationText && (
                                  <div style={{ fontSize: '12px', color: '#687078' }}>
                                    {item.needsIncreaseRPM ? `${utilizationText} of current` : `${utilizationText} utilization`}
                                  </div>
                                )}
                              </div>
                            );
                          },
                        },
                      ]}
                      items={aggregatedModels}
                      empty={<Box textAlign="center">No quota data available</Box>}
                      variant="embedded"
                      resizableColumns
                    />
                  </div>
                );
              })()}

              {/* Detailed Quota Requirements by Processing Step */}
              <ExpandableSection headerText="Detailed Requirements by Processing Step" variant="container">
                {(() => {
                  const bedrockQuotas = quotaData.filter((q) => q.category && q.category.includes('Bedrock'));
                  if (bedrockQuotas.length === 0) return null;

                  const detailedByModel = getDetailedRequirementsByModel(bedrockQuotas);

                  return Object.entries(detailedByModel).map(([modelId, steps], index) => (
                    <div key={modelId} style={{ marginTop: index > 0 ? '32px' : '0' }}>
                      <Box padding={{ vertical: 's', horizontal: 'm' }}>
                        <div style={{ fontSize: '16px', fontWeight: 'bold', color: '#0972D3' }}>Model: {modelId}</div>
                      </Box>
                      <Table
                        columnDefinitions={[
                          {
                            id: 'inferenceType',
                            header: 'Processing Step',
                            cell: (item) => <strong>{item.inferenceType}</strong>,
                            width: 200,
                          },
                          {
                            id: 'requiredTPM',
                            header: 'Required TPM',
                            cell: (item) => item.requiredTPM,
                            width: 140,
                          },
                          {
                            id: 'currentTPM',
                            header: 'Current TPM',
                            cell: (item) => item.currentTPM,
                            width: 140,
                          },
                          {
                            id: 'statusTPM',
                            header: 'TPM Status',
                            cell: (item) => {
                              if (item.requiredTPM === '-') return <span style={{ color: '#687078' }}>N/A</span>;
                              const needsIncrease = item.statusTPM.includes('⚠️') || item.statusTPM.includes('Increase Needed');
                              return needsIncrease ? <Badge color="red">✗ Insufficient</Badge> : <Badge color="green">✓ Sufficient</Badge>;
                            },
                            width: 130,
                          },
                          {
                            id: 'requiredRPM',
                            header: 'Required RPM',
                            cell: (item) => item.requiredRPM,
                            width: 140,
                          },
                          {
                            id: 'currentRPM',
                            header: 'Current RPM',
                            cell: (item) => item.currentRPM,
                            width: 140,
                          },
                          {
                            id: 'statusRPM',
                            header: 'RPM Status',
                            cell: (item) => {
                              if (item.requiredRPM === '-') return <span style={{ color: '#687078' }}>N/A</span>;
                              const needsIncrease = item.statusRPM.includes('⚠️') || item.statusRPM.includes('Increase Needed');
                              return needsIncrease ? <Badge color="red">✗ Insufficient</Badge> : <Badge color="green">✓ Sufficient</Badge>;
                            },
                            width: 130,
                          },
                        ]}
                        items={steps}
                        empty={<Box textAlign="center">No quota data available</Box>}
                        variant="embedded"
                        resizableColumns
                      />
                    </div>
                  ));
                })()}
              </ExpandableSection>

              {/* Show message when no quota data */}
              {quotaData.length === 0 && (
                <Box textAlign="center" color="text-status-info">
                  <SpaceBetween size="s">
                    <div>No quota requirements to display</div>
                    <div>Check the debug information above for details</div>
                  </SpaceBetween>
                </Box>
              )}

              {/* Show "Request Quota Increases" button only if we have quota data */}
              {quotaData.length > 0 && (
                <Button
                  variant="primary"
                  onClick={() => {
                    // Open AWS Service Quotas console for all quota increases needed
                    const quotasNeedingIncrease = [];

                    Object.entries(groupQuotasByCategory(quotaData)).forEach(([category, quotas]) => {
                      quotas.forEach((quota) => {
                        if (quota.statusText.includes('⚠️') || quota.statusText.includes('Increase Needed')) {
                          quotasNeedingIncrease.push({
                            service: quota.service,
                            current: quota.currentQuota,
                            required: quota.requiredQuota,
                            category,
                            modelId: quota.modelId,
                          });
                        }
                      });
                    });

                    if (quotasNeedingIncrease.length === 0) {
                      alert('All quotas are currently sufficient for your capacity requirements.');
                      return;
                    }

                    // Open AWS Service Quotas console for Bedrock service using current region
                    const region = import.meta.env.VITE_AWS_REGION;

                    if (region) {
                      // User has AWS region - direct to Service Quotas console
                      const serviceQuotasUrl = `https://${region}.console.aws.amazon.com/servicequotas/home/services/bedrock/quotas`;
                      window.open(serviceQuotasUrl, '_blank');
                    } else {
                      // No region detected - direct to AWS Support Center for general quota requests
                      const supportUrl = 'https://console.aws.amazon.com/support/home#/case/create?issueType=service-limit-increase';
                      window.open(supportUrl, '_blank');
                    }
                  }}
                >
                  Request Quota Increases
                </Button>
              )}
            </SpaceBetween>
          </Container>
        )}

        {hasCalculated && (!results || !results.success) && (
          <Alert type="warning">
            <strong>Calculation Incomplete:</strong> The capacity calculation completed but returned incomplete data. This may be due to
            missing AWS service quotas. Please check your IAM permissions and try again.
          </Alert>
        )}

        {/* Tokens per Hour Analysis */}
        {hasCalculated && (
          <>
            <Container header={<Header variant="h2">Tokens per Hour Analysis</Header>}>
              <SpaceBetween size="m">
                <Header variant="h3">📊 Hourly Token Distribution</Header>
                <Box>
                  <div
                    style={{
                      height: '350px',
                      background: '#f9f9f9',
                      border: '1px solid #d5dbdb',
                      borderRadius: '4px',
                      position: 'relative',
                      padding: '30px',
                      overflowX: 'auto',
                    }}
                  >
                    {hasCalculated ? (
                      <>
                        {/* Y-axis labels */}
                        <div
                          style={{
                            position: 'absolute',
                            left: '5px',
                            top: '25px',
                            fontSize: '10px',
                            color: '#687078',
                          }}
                        >
                          Max
                        </div>
                        <div
                          style={{
                            position: 'absolute',
                            left: '5px',
                            top: '65px',
                            fontSize: '10px',
                            color: '#687078',
                          }}
                        >
                          50%
                        </div>
                        <div
                          style={{
                            position: 'absolute',
                            left: '5px',
                            top: '125px',
                            fontSize: '10px',
                            color: '#687078',
                          }}
                        >
                          0
                        </div>

                        {/* Hourly token bars */}
                        {(() => {
                          // Generate hourly breakdown from processing schedule
                          const hourlyBreakdown: Record<number, HourlyBreakdown> = {};

                          // Initialize all 24 hours
                          for (let hour = 0; hour < 24; hour += 1) {
                            hourlyBreakdown[hour] = {
                              hour,
                              ocrTokens: 0,
                              classificationTokens: 0,
                              extractionTokens: 0,
                              summarizationTokens: 0,
                              assessmentTokens: 0,
                              totalTokens: 0,
                              documentTypes: [],
                            };
                          }

                          // Aggregate by hour from time slots
                          processingConfig.timeSlots.forEach((slot) => {
                            const hour = parseInt(String(slot.hour || 0), 10);
                            const docType = slot.documentType || 'Other';
                            const docsPerHour = parseInt(String(slot.docsPerHour || 0), 10);

                            const hourEntry = hourlyBreakdown[hour];
                            if (docsPerHour > 0 && hourEntry) {
                              const docConfig = documentConfigs.find((config) => config.type === docType) || {
                                ocrTokens: 0,
                                classificationTokens: 0,
                                extractionTokens: 0,
                                summarizationTokens: 0,
                                assessmentTokens: 0,
                              };

                              const ocrTokens = parseFloat(String(docConfig.ocrTokens || 0));
                              const classificationTokens = parseFloat(String(docConfig.classificationTokens || 0));
                              const extractionTokens = parseFloat(String(docConfig.extractionTokens || 0));
                              const summarizationTokens = parseFloat(String(docConfig.summarizationTokens || 0));
                              const assessmentTokens = parseFloat(String(docConfig.assessmentTokens || 0));

                              // Debug logging
                              console.log(`DEBUG: Hour ${hour}, DocType: ${docType}, DocsPerHour: ${docsPerHour}`);
                              console.log(
                                `DEBUG: Token config - OCR: ${ocrTokens}, Class: ${classificationTokens}, Extract: ${extractionTokens}, Assess: ${assessmentTokens}, Summ: ${summarizationTokens}`,
                              );

                              // Only include OCR tokens if Bedrock OCR is configured
                              const effectiveOcrTokens = configuration?.ocr?.backend === 'bedrock' ? ocrTokens : 0;

                              // Calculate tokens for this specific slot
                              const slotOcrTokens = effectiveOcrTokens * docsPerHour;
                              const slotClassificationTokens = classificationTokens * docsPerHour;
                              const slotExtractionTokens = extractionTokens * docsPerHour;
                              const slotSummarizationTokens = summarizationTokens * docsPerHour;
                              const slotAssessmentTokens = assessmentTokens * docsPerHour;

                              console.log(
                                `DEBUG: Slot tokens - OCR: ${slotOcrTokens}, Class: ${slotClassificationTokens}, Extract: ${slotExtractionTokens}, Assess: ${slotAssessmentTokens}, Summ: ${slotSummarizationTokens}`,
                              );

                              // Accumulate tokens for this hour (multiple slots can add to same hour)
                              hourEntry.ocrTokens += slotOcrTokens;
                              hourEntry.classificationTokens += slotClassificationTokens;
                              hourEntry.extractionTokens += slotExtractionTokens;
                              hourEntry.summarizationTokens += slotSummarizationTokens;
                              hourEntry.assessmentTokens += slotAssessmentTokens;
                              hourEntry.totalTokens +=
                                slotOcrTokens +
                                slotClassificationTokens +
                                slotExtractionTokens +
                                slotSummarizationTokens +
                                slotAssessmentTokens;

                              console.log(`DEBUG: Hour ${hour} accumulated total: ${hourEntry.totalTokens}`);

                              // Track document types
                              if (hourEntry.documentTypes && !hourEntry.documentTypes.includes(docType)) {
                                hourEntry.documentTypes.push(docType);
                              }
                            }
                          });

                          // Find max tokens for scaling
                          const maxTokens = Math.max(...Object.values(hourlyBreakdown).map((h) => h.totalTokens), 1);
                          console.log(`DEBUG: Max tokens for scaling: ${maxTokens}`);

                          // Only show hours with processing activity
                          const activeHours = Object.values(hourlyBreakdown).filter((h) => h.totalTokens > 0);
                          console.log(
                            `DEBUG: Active hours:`,
                            activeHours.map((h) => `Hour ${h.hour}: ${h.totalTokens} tokens`),
                          );

                          return activeHours.map((hourData, index) => {
                            const height = maxTokens > 0 ? Math.max((hourData.totalTokens / maxTokens) * 150, 8) : 8;
                            const leftPos = 60 + index * 80;

                            // Calculate proportional heights for each inference type
                            const ocrHeight = maxTokens > 0 ? (hourData.ocrTokens / maxTokens) * 150 : 0;
                            const classificationHeight = maxTokens > 0 ? (hourData.classificationTokens / maxTokens) * 150 : 0;
                            const extractionHeight = maxTokens > 0 ? (hourData.extractionTokens / maxTokens) * 150 : 0;
                            const assessmentHeight = maxTokens > 0 ? (hourData.assessmentTokens / maxTokens) * 150 : 0;
                            const summarizationHeight = maxTokens > 0 ? (hourData.summarizationTokens / maxTokens) * 150 : 0;

                            return (
                              <div key={hourData.hour}>
                                {/* Stacked bar segments */}
                                {configuration?.ocr?.backend === 'bedrock' && ocrHeight > 0 && (
                                  <div
                                    style={{
                                      position: 'absolute',
                                      bottom: '60px',
                                      left: `${leftPos}px`,
                                      width: '50px',
                                      height: `${ocrHeight}px`,
                                      background: '#9333ea', // Purple for OCR
                                      borderRadius: '3px 3px 0 0',
                                      cursor: 'pointer',
                                    }}
                                    title={`Hour ${hourData.hour}: OCR ${hourData.ocrTokens.toLocaleString()} tokens`}
                                  />
                                )}
                                {classificationHeight > 0 && (
                                  <div
                                    style={{
                                      position: 'absolute',
                                      bottom: `${60 + (configuration?.ocr?.backend === 'bedrock' ? ocrHeight : 0)}px`,
                                      left: `${leftPos}px`,
                                      width: '50px',
                                      height: `${classificationHeight}px`,
                                      background: '#f59e0b', // Orange for classification
                                      borderRadius:
                                        classificationHeight > 0 && !(configuration?.ocr?.backend === 'bedrock') ? '3px 3px 0 0' : '0',
                                      cursor: 'pointer',
                                    }}
                                    title={`Hour ${hourData.hour}: Classification ${hourData.classificationTokens.toLocaleString()} tokens`}
                                  />
                                )}
                                {extractionHeight > 0 && (
                                  <div
                                    style={{
                                      position: 'absolute',
                                      bottom: `${
                                        60 + (configuration?.ocr?.backend === 'bedrock' ? ocrHeight : 0) + classificationHeight
                                      }px`,
                                      left: `${leftPos}px`,
                                      width: '50px',
                                      height: `${extractionHeight}px`,
                                      background: '#10b981', // Green for extraction
                                      cursor: 'pointer',
                                    }}
                                    title={`Hour ${hourData.hour}: Extraction ${hourData.extractionTokens.toLocaleString()} tokens`}
                                  />
                                )}
                                {assessmentHeight > 0 && (
                                  <div
                                    style={{
                                      position: 'absolute',
                                      bottom: `${
                                        60 +
                                        (configuration?.ocr?.backend === 'bedrock' ? ocrHeight : 0) +
                                        classificationHeight +
                                        extractionHeight
                                      }px`,
                                      left: `${leftPos}px`,
                                      width: '50px',
                                      height: `${assessmentHeight}px`,
                                      background: '#3b82f6', // Blue for assessment
                                      cursor: 'pointer',
                                    }}
                                    title={`Hour ${hourData.hour}: Assessment ${hourData.assessmentTokens.toLocaleString()} tokens`}
                                  />
                                )}
                                {summarizationHeight > 0 && (
                                  <div
                                    style={{
                                      position: 'absolute',
                                      bottom: `${
                                        60 +
                                        (configuration?.ocr?.backend === 'bedrock' ? ocrHeight : 0) +
                                        classificationHeight +
                                        extractionHeight +
                                        assessmentHeight
                                      }px`,
                                      left: `${leftPos}px`,
                                      width: '50px',
                                      height: `${summarizationHeight}px`,
                                      background: '#ef4444', // Red for summarization
                                      borderRadius: '3px 3px 0 0',
                                      cursor: 'pointer',
                                    }}
                                    title={`Hour ${hourData.hour}: Summarization ${hourData.summarizationTokens.toLocaleString()} tokens`}
                                  />
                                )}
                                <div
                                  style={{
                                    position: 'absolute',
                                    bottom: '35px',
                                    left: `${leftPos - 10}px`,
                                    fontSize: '12px',
                                    color: '#687078',
                                    width: '70px',
                                    textAlign: 'center',
                                  }}
                                >
                                  {String(hourData.hour).padStart(2, '0')}:00
                                </div>
                                <div
                                  style={{
                                    position: 'absolute',
                                    bottom: `${70 + height}px`,
                                    left: `${leftPos - 15}px`,
                                    fontSize: '11px',
                                    color: '#232f3e',
                                    fontWeight: '600',
                                    width: '80px',
                                    textAlign: 'center',
                                  }}
                                >
                                  {hourData.totalTokens > 0 ? `${(hourData.totalTokens / 1000).toFixed(1)}K` : '0'}
                                </div>
                              </div>
                            );
                          });
                        })()}

                        {/* Legend - Dynamic based on pattern */}
                        <div
                          style={{
                            position: 'absolute',
                            bottom: '5px',
                            right: '10px',
                            fontSize: '8px',
                            color: '#687078',
                          }}
                        >
                          {(() => {
                            const pattern = getDeployedPattern();
                            const isBedrockOcr = configuration?.ocr?.backend === 'bedrock';

                            if (pattern === 'PATTERN-1') {
                              return (
                                <>
                                  <span style={{ color: '#ef4444' }}>■</span> Summarization
                                </>
                              );
                            }
                            return (
                              <>
                                {isBedrockOcr && (
                                  <>
                                    <span style={{ color: '#9333ea' }}>■</span> OCR
                                    <span style={{ marginLeft: '8px' }} />
                                  </>
                                )}
                                <span style={{ color: '#f59e0b' }}>■</span> Classification
                                <span style={{ color: '#10b981', marginLeft: '8px' }}>■</span> Extraction
                                <span style={{ color: '#3b82f6', marginLeft: '8px' }}>■</span> Assessment
                                <span style={{ color: '#ef4444', marginLeft: '8px' }}>■</span> Summarization
                              </>
                            );
                          })()}
                        </div>
                      </>
                    ) : (
                      <div
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'center',
                          height: '100%',
                          color: '#687078',
                          fontSize: '14px',
                        }}
                      />
                    )}
                  </div>

                  {/* Peak Hour Analysis */}
                  {hasCalculated && (
                    <div style={{ marginTop: '15px', padding: '10px', background: '#f9f9f9', borderRadius: '4px' }}>
                      <div style={{ fontWeight: '600', marginBottom: '8px' }}>📈 Peak Hour Analysis</div>
                      {(() => {
                        const hourlyBreakdown: Record<number, HourlyBreakdown> = {};
                        for (let hour = 0; hour < 24; hour += 1) {
                          hourlyBreakdown[hour] = {
                            hour,
                            ocrTokens: 0,
                            classificationTokens: 0,
                            extractionTokens: 0,
                            summarizationTokens: 0,
                            assessmentTokens: 0,
                            totalTokens: 0,
                          };
                        }

                        processingConfig.timeSlots.forEach((slot) => {
                          const hour = parseInt(String(slot.hour || 0), 10);
                          const docType = slot.documentType || 'Other';
                          const docsPerHour = parseInt(String(slot.docsPerHour || 0), 10);
                          if (docsPerHour > 0) {
                            const docConfig = documentConfigs.find((config) => config.type === docType) || {
                              ocrTokens: 0,
                              classificationTokens: 0,
                              extractionTokens: 0,
                              summarizationTokens: 0,
                              assessmentTokens: 0,
                            };
                            const ocrTokensPerDoc = parseFloat(String(docConfig.ocrTokens || 0));
                            const classificationTokensPerDoc = parseFloat(String(docConfig.classificationTokens || 0));
                            const extractionTokensPerDoc = parseFloat(String(docConfig.extractionTokens || 0));
                            const summarizationTokensPerDoc = parseFloat(String(docConfig.summarizationTokens || 0));
                            const assessmentTokensPerDoc = parseFloat(String(docConfig.assessmentTokens || 0));

                            // Only include OCR tokens if Bedrock OCR is configured
                            const effectiveOcrTokensPerDoc = configuration?.ocr?.backend === 'bedrock' ? ocrTokensPerDoc : 0;

                            // Calculate slot tokens (tokens per doc * docs per hour)
                            const slotOcrTokens = effectiveOcrTokensPerDoc * docsPerHour;
                            const slotClassificationTokens = classificationTokensPerDoc * docsPerHour;
                            const slotExtractionTokens = extractionTokensPerDoc * docsPerHour;
                            const slotSummarizationTokens = summarizationTokensPerDoc * docsPerHour;
                            const slotAssessmentTokens = assessmentTokensPerDoc * docsPerHour;

                            // Accumulate tokens for this hour
                            hourlyBreakdown[hour].ocrTokens += slotOcrTokens;
                            hourlyBreakdown[hour].classificationTokens += slotClassificationTokens;
                            hourlyBreakdown[hour].extractionTokens += slotExtractionTokens;
                            hourlyBreakdown[hour].summarizationTokens += slotSummarizationTokens;
                            hourlyBreakdown[hour].assessmentTokens += slotAssessmentTokens;
                            hourlyBreakdown[hour].totalTokens +=
                              slotOcrTokens +
                              slotClassificationTokens +
                              slotExtractionTokens +
                              slotSummarizationTokens +
                              slotAssessmentTokens;
                          }
                        });

                        const activeHours = Object.values(hourlyBreakdown).filter((h) => h.totalTokens > 0);
                        if (activeHours.length === 0) return <div>No processing hours configured</div>;

                        const peakHour = activeHours.reduce((max, hour) => (hour.totalTokens > max.totalTokens ? hour : max));
                        const avgTokens = activeHours.reduce((sum, hour) => sum + hour.totalTokens, 0) / activeHours.length;

                        const peakInference = Math.max(
                          peakHour.ocrTokens,
                          peakHour.classificationTokens,
                          peakHour.extractionTokens,
                          peakHour.assessmentTokens,
                          peakHour.summarizationTokens,
                        );
                        let peakInferenceType = 'Classification';
                        if (peakInference === peakHour.ocrTokens) peakInferenceType = 'OCR';
                        else if (peakInference === peakHour.extractionTokens) peakInferenceType = 'Extraction';
                        else if (peakInference === peakHour.assessmentTokens) peakInferenceType = 'Assessment';
                        else if (peakInference === peakHour.summarizationTokens) peakInferenceType = 'Summarization';

                        return (
                          <div style={{ fontSize: '12px', lineHeight: '1.4' }}>
                            <div>
                              Peak: {String(peakHour.hour).padStart(2, '0')}:00-
                              {}
                              {String(peakHour.hour + 1).padStart(2, '0')}:00 ({peakHour.totalTokens.toLocaleString()} tokens)
                            </div>
                            <div>
                              ⚡ <strong>Peak:</strong> {peakInferenceType} ({peakInference.toLocaleString()} tokens)
                            </div>
                            <div>
                              📊 <strong>Average Load:</strong> {avgTokens.toLocaleString()} tokens/hour across {activeHours.length} active
                              hours
                            </div>
                            <div>
                              📈 <strong>Peak vs Average:</strong> {((peakHour.totalTokens / avgTokens - 1) * 100).toFixed(1)}% above
                              average
                            </div>
                          </div>
                        );
                      })()}
                    </div>
                  )}
                </Box>
              </SpaceBetween>
            </Container>

            {/* Latency Distribution Histogram */}
            {results?.success && results?.latencyDistribution && (
              <Container
                header={
                  <Header
                    variant="h2"
                    actions={
                      <Select
                        selectedOption={latencyTimeRange}
                        onChange={({ detail }) => setLatencyTimeRange(detail.selectedOption as SelectOption)}
                        options={[
                          { label: 'Last 4 hours', value: '4' },
                          { label: 'Last 12 hours', value: '12' },
                          { label: 'Last 24 hours', value: '24' },
                          { label: 'Last 2 days', value: '48' },
                          { label: 'Last 7 days', value: '168' },
                          { label: 'Last 30 days', value: '720' },
                          { label: 'All time', value: '8760' },
                        ]}
                      />
                    }
                  >
                    Processing Latency Distribution
                  </Header>
                }
              >
                <SpaceBetween size="l">
                  <Box>
                    <div style={{ marginBottom: '16px' }}>
                      <strong>Expected Processing Times:</strong> Based on actual processing metrics from your processed documents
                    </div>

                    {/* Stacked Latency Bars: Processing (blue) + Queue (orange) with SLA marker */}
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                      {(() => {
                        const latency = results.latencyDistribution;
                        const percentiles = [
                          {
                            label: 'P50 (Median)',
                            pKey: 'procP50',
                            qKey: 'queueP50',
                            tKey: 'p50',
                            description: '50% of documents complete within',
                          },
                          { label: 'P75', pKey: 'procP75', qKey: 'queueP75', tKey: 'p75', description: '75% of documents complete within' },
                          { label: 'P90', pKey: 'procP90', qKey: 'queueP90', tKey: 'p90', description: '90% of documents complete within' },
                          { label: 'P95', pKey: 'procP95', qKey: 'queueP95', tKey: 'p95', description: '95% of documents complete within' },
                          {
                            label: 'P99 (Worst Case)',
                            pKey: 'procP99',
                            qKey: 'queueP99',
                            tKey: 'p99',
                            description: '99% of documents complete within',
                          },
                        ];

                        const maxAllowed = parseFloat(latency.maxAllowed?.replace('s', '') || '300');
                        // Scale against max value across all percentiles OR maxAllowed, whichever is larger
                        const allTotals = percentiles.map((p) => parseFloat(String(latency[p.tKey] || '0').replace('s', '')));
                        const maxValue = Math.max(...allTotals, maxAllowed);
                        const scalingReference = maxValue > 0 ? maxValue : 1;
                        // SLA line position as percentage of the scale
                        const slaPosition = (maxAllowed / scalingReference) * 100;

                        return percentiles.map((percentile) => {
                          const totalFromApi = parseFloat(String(latency[percentile.tKey] || '0').replace('s', ''));
                          const rawProcVal = parseFloat(String(latency[percentile.pKey] || '0').replace('s', ''));
                          const rawQueueVal = parseFloat(String(latency[percentile.qKey] || '0').replace('s', ''));
                          // Fallback: if separate proc/queue fields aren't available, use total as processing
                          const hasSeparateFields = rawProcVal > 0 || rawQueueVal > 0;
                          const procVal = hasSeparateFields ? rawProcVal : totalFromApi;
                          const queueVal = hasSeparateFields ? rawQueueVal : 0;
                          const totalVal = hasSeparateFields ? procVal + queueVal : totalFromApi;
                          const exceedsLimit = totalVal > maxAllowed;

                          // Calculate widths as percentage of the scaling reference
                          const procWidth = (procVal / scalingReference) * 100;
                          const queueWidth = (queueVal / scalingReference) * 100;

                          return (
                            <div key={percentile.tKey} style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                              <div style={{ minWidth: '120px', fontSize: '14px', fontWeight: '500' }}>{percentile.label}</div>
                              <div
                                style={{
                                  flex: 1,
                                  position: 'relative',
                                  height: '28px',
                                  background: '#f0f0f0',
                                  borderRadius: '4px',
                                  overflow: 'visible',
                                }}
                              >
                                {/* Processing time segment (blue) */}
                                <div
                                  style={{
                                    position: 'absolute',
                                    left: 0,
                                    top: 0,
                                    bottom: 0,
                                    width: `${Math.max(procWidth, 2)}%`,
                                    backgroundColor: '#0972D3',
                                    borderRadius: queueVal > 0 ? '4px 0 0 4px' : '4px',
                                    zIndex: 2,
                                  }}
                                  title={`Processing: ${procVal.toFixed(1)}s`}
                                />
                                {/* Queue delay segment (orange) */}
                                {queueVal > 0 && (
                                  <div
                                    style={{
                                      position: 'absolute',
                                      left: `${procWidth}%`,
                                      top: 0,
                                      bottom: 0,
                                      width: `${Math.max(queueWidth, 1)}%`,
                                      backgroundColor: '#f89256',
                                      borderRadius: '0 4px 4px 0',
                                      zIndex: 2,
                                    }}
                                    title={`Queue: ${queueVal.toFixed(1)}s`}
                                  />
                                )}
                                {/* SLA vertical marker line */}
                                <div
                                  style={{
                                    position: 'absolute',
                                    left: `${Math.min(slaPosition, 100)}%`,
                                    top: '-4px',
                                    bottom: '-4px',
                                    width: '2px',
                                    backgroundColor: '#d13212',
                                    zIndex: 3,
                                  }}
                                  title={`SLA Target: ${maxAllowed}s`}
                                />
                                {/* Total value label */}
                                <div
                                  style={{
                                    position: 'absolute',
                                    left: `${Math.min(procWidth + queueWidth + 1, 85)}%`,
                                    top: '50%',
                                    transform: 'translateY(-50%)',
                                    fontSize: '12px',
                                    fontWeight: '600',
                                    color: exceedsLimit ? '#d13212' : '#037f0c',
                                    zIndex: 4,
                                    whiteSpace: 'nowrap',
                                    paddingLeft: '6px',
                                  }}
                                >
                                  {totalVal.toFixed(1)}s {exceedsLimit ? '❌' : '✅'}
                                </div>
                              </div>
                              <div style={{ minWidth: '200px', fontSize: '12px', color: '#5f6b7a' }}>{percentile.description}</div>
                            </div>
                          );
                        });
                      })()}
                      {/* Legend */}
                      <div
                        style={{ display: 'flex', gap: '16px', fontSize: '12px', color: '#5f6b7a', marginTop: '4px', paddingLeft: '132px' }}
                      >
                        <span>
                          <span
                            style={{
                              display: 'inline-block',
                              width: '12px',
                              height: '12px',
                              backgroundColor: '#0972D3',
                              borderRadius: '2px',
                              marginRight: '4px',
                              verticalAlign: 'middle',
                            }}
                          />{' '}
                          Processing
                        </span>
                        <span>
                          <span
                            style={{
                              display: 'inline-block',
                              width: '12px',
                              height: '12px',
                              backgroundColor: '#f89256',
                              borderRadius: '2px',
                              marginRight: '4px',
                              verticalAlign: 'middle',
                            }}
                          />{' '}
                          Queue Delay
                        </span>
                        <span>
                          <span
                            style={{
                              display: 'inline-block',
                              width: '2px',
                              height: '14px',
                              backgroundColor: '#d13212',
                              marginRight: '4px',
                              verticalAlign: 'middle',
                            }}
                          />{' '}
                          SLA Target ({results.latencyDistribution?.maxAllowed})
                        </span>
                      </div>
                    </div>

                    {/* Summary Information - Only Real Measured Data */}
                    <Box margin={{ top: 'l' }}>
                      <ColumnLayout columns={4} variant="text-grid">
                        <div>
                          <Box variant="awsui-key-label">Base Processing Time</Box>
                          <div style={{ fontSize: '16px', fontWeight: '600' }}>
                            {results.latencyDistribution.actualProcessingTime || results.latencyDistribution.baseLatency}
                          </div>
                          <div style={{ fontSize: '12px', color: '#5f6b7a' }}>
                            Measured from{' '}
                            {results.latencyDistribution.dataSource === 'document_timestamps' ? 'document timestamps' : 'Lambda durations'}
                          </div>
                        </div>
                        <div>
                          <Box variant="awsui-key-label">Queue Delay</Box>
                          <div style={{ fontSize: '16px', fontWeight: '600' }}>{results.latencyDistribution.queueLatency || '0.00s'}</div>
                          <div style={{ fontSize: '12px', color: '#5f6b7a' }}>
                            {parseFloat(String(results.latencyDistribution.queueLatency)) > 0
                              ? 'Time waiting in queue before processing'
                              : 'No queue delay measured (QueuedTime/WorkflowStartTime timestamps needed)'}
                          </div>
                        </div>
                        <div>
                          <Box variant="awsui-key-label">Quota Status</Box>
                          {(() => {
                            // Check if ANY model quota is exceeded (from quotaRequirements)
                            const anyQuotaExceeded = results.quotaRequirements?.some(
                              (req) => req.statusText?.includes('Increase Needed') || req.status === 'warning',
                            );
                            const quotaOverloaded = results.latencyDistribution.quotaOverloaded || anyQuotaExceeded;

                            return (
                              <>
                                <div
                                  style={{
                                    fontSize: '16px',
                                    fontWeight: '600',
                                    color: quotaOverloaded ? '#d13212' : '#037f0c',
                                  }}
                                >
                                  {quotaOverloaded ? '⚠️ Quota Exceeded' : '✅ Within Quota'}
                                </div>
                                <div style={{ fontSize: '12px', color: '#5f6b7a' }}>
                                  {quotaOverloaded ? 'Request quota increases above' : 'All model quotas sufficient'}
                                </div>
                              </>
                            );
                          })()}
                        </div>
                        <div>
                          <Box variant="awsui-key-label">SLA Target</Box>
                          <div style={{ fontSize: '16px', fontWeight: '600' }}>{results.latencyDistribution.maxAllowed}</div>
                          <div style={{ fontSize: '12px', color: '#5f6b7a' }}>Maximum acceptable processing time</div>
                        </div>
                      </ColumnLayout>
                    </Box>

                    {/* Performance Alert */}
                    {results.latencyDistribution.exceedsLimit && (
                      <Alert type="warning">
                        <strong>⚠️ Performance Warning:</strong> Your P99 latency exceeds the configured SLA target. Consider increasing
                        Bedrock quotas or reducing document volume during peak hours.
                      </Alert>
                    )}

                    {/* Overload State Warning */}
                    {results.latencyDistribution.quotaOverloaded && (
                      <Alert type="error">
                        <strong>🚨 Quota Overload Detected:</strong> Scheduled demand exceeds available quota capacity. This will cause
                        indefinite queue growth if sustained.
                        <Box margin={{ top: 'xs' }}>
                          <strong>Queue Delay Note:</strong> The queue delays shown above are historical (from actual processed documents).
                          If overload persists, future queue delays will grow continuously until quotas are increased or demand is reduced.
                        </Box>
                        <Box margin={{ top: 'xs' }}>
                          <strong>Action Required:</strong> Review the &quot;Total Quota Requirements by Model&quot; table below and request
                          quota increases for models showing &quot;✗ Insufficient&quot; status.
                        </Box>
                      </Alert>
                    )}
                  </Box>
                </SpaceBetween>
              </Container>
            )}
          </>
        )}

        {hasCalculated && (!results || !results.success) && (
          <Alert type="warning">
            <strong>Calculation Incomplete:</strong> The capacity calculation completed but returned incomplete data. This may be due to
            missing AWS service quotas. Please check your IAM permissions and try again.
          </Alert>
        )}
      </SpaceBetween>

      {/* Document Picker Modal - styled like Documents List */}
      <DocumentPickerModal
        visible={showDocumentPicker}
        onDismiss={() => {
          setShowDocumentPicker(false);
          setSelectedDocuments([]);
        }}
        recentDocuments={recentDocuments as unknown as React.ComponentProps<typeof DocumentPickerModal>['recentDocuments']}
        selectedDocuments={selectedDocuments as unknown as React.ComponentProps<typeof DocumentPickerModal>['selectedDocuments']}
        setSelectedDocuments={setSelectedDocuments as unknown as React.ComponentProps<typeof DocumentPickerModal>['setSelectedDocuments']}
        onUseSelectedDocuments={populateTokensFromMultipleDocuments}
        onUseDocument={populateTokensFromDocument as unknown as React.ComponentProps<typeof DocumentPickerModal>['onUseDocument']}
        configuration={configuration}
        periodsToLoad={periodsToLoad}
        setPeriodsToLoad={handlePeriodChange}
        customDateRange={customDateRange}
        onCustomDateRange={() => setIsDateRangeModalVisible(true)}
        loading={isDocumentsListLoading}
        onRefresh={() => setIsDocumentsListLoading && setIsDocumentsListLoading(true)}
      />

      {/* Date Range Modal for custom time range selection */}
      <DateRangeModal
        visible={isDateRangeModalVisible}
        onDismiss={() => setIsDateRangeModalVisible(false)}
        onApply={handleCustomDateRangeApply}
      />
    </Container>
  );
};

export default CapacityPlanningLayout;
