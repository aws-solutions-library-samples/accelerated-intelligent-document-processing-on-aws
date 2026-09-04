// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/* eslint-disable prettier/prettier */
/* eslint-disable react/no-array-index-key */
/* eslint-disable @typescript-eslint/no-explicit-any */

// src/components/discovery/DiscoveryPanel.jsx
import React, { useState, useEffect, useCallback, useRef } from 'react';
import {
  Button,
  Container,
  Header,
  SpaceBetween,
  FormField,
  StatusIndicator,
  Alert,
  Input,
  Table,
  Box,
  TextContent,
  ColumnLayout,
  Select,
  ExpandableSection,
  Badge,
  Link,
  TextFilter,
  Tiles,
  RadioGroup,
  Pagination,
  CollectionPreferences,
} from '@cloudscape-design/components';
import type { SelectProps } from '@cloudscape-design/components';
import { generateClient } from '../../api/client-shim';

import { uploadDiscoveryDocument, listDiscoveryJobs, deleteDiscoveryJob, autoDetectSections } from '../../graphql/generated';
import useSettingsContext from '../../contexts/settings';
import useConfigurationVersions from '../../hooks/use-configuration-versions';
import { getJsonValidationError } from '../common/utilities';
import { formatConfigVersionLink } from '../test-studio/utils/configVersionUtils';
import type { ConfigVersion } from '../test-studio/utils/configVersionUtils';
import { useNavigate } from 'react-router-dom';
import { DISCOVERY_JOB_PATH } from '../../routes/constants';
import { SUPPORTED_DISCOVERY_EXTENSIONS } from '../common/constants';
import PdfPageSelector from './PdfPageSelector';
import type { PageRange } from './PdfPageSelector';
import CreateConfigProfileModal from '../common/CreateConfigProfileModal';

const client = generateClient();

interface UploadStatusItem {
  file: string;
  type: string;
  status: 'success' | 'error';
  objectKey?: string;
  error?: string;
}

interface DiscoveryJob {
  jobId: string;
  documentKey?: string;
  groundTruthKey?: string;
  version?: string;
  status: string;
  createdAt?: string;
  updatedAt?: string;
  errorMessage?: string;
  discoveredClassName?: string;
  statusMessage?: string;
  pageRange?: string;
}

/**
 * Parse an ISO timestamp, normalizing timezone.
 * Backend timestamps may or may not have a Z suffix — treat all as UTC.
 */
const parseUtcTimestamp = (iso: string): number => {
  // If no timezone info at the end of the string, append Z to treat as UTC (Lambda runs in UTC)
  // Check for: trailing Z, or +HH:MM / -HH:MM offset at end
  const hasTimezone = /[Zz]$|[+-]\d{2}:\d{2}$|[+-]\d{4}$|[+-]\d{2}$/.test(iso);
  const normalized = hasTimezone ? iso : `${iso}Z`;
  return new Date(normalized).getTime();
};

/** Format elapsed time since a given ISO timestamp. Returns e.g. "0:45" or "2:15". */
const formatElapsed = (startIso: string | undefined): string => {
  if (!startIso) return '—';
  const start = parseUtcTimestamp(startIso);
  if (Number.isNaN(start)) return '—';
  const elapsed = Math.max(0, Math.floor((Date.now() - start) / 1000));
  const mins = Math.floor(elapsed / 60);
  const secs = elapsed % 60;
  return `${mins}:${secs.toString().padStart(2, '0')}`;
};

interface DiscoveryPanelProps {
  discoveryType?: 'classes' | 'rules';
}

/**
 * Determine whether classes-discovery-only controls (mode selector, ground
 * truth file input, page range selector) should be visible.
 *
 * Exported for unit testing — Policy Discovery (discoveryType='rules') hides
 * all three controls unconditionally because they don't apply to whole-
 * document rule extraction.
 */
export const shouldShowClassesDiscoveryControls = (discoveryType: 'classes' | 'rules' = 'classes'): boolean =>
  discoveryType !== 'rules';

const DiscoveryPanel = ({ discoveryType = 'classes' }: DiscoveryPanelProps = {}): React.JSX.Element => {
  const navigate = useNavigate();
  const { settings } = useSettingsContext();
  const { versions, loading: versionsLoading, getVersionOptions, fetchVersions } = useConfigurationVersions();
  const [documentFile, setDocumentFile] = useState<File | null>(null);
  const [groundTruthFile, setGroundTruthFile] = useState<File | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadStatus, setUploadStatus] = useState<UploadStatusItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [prefix, setPrefix] = useState('');
  const [discoveryJobs, setDiscoveryJobs] = useState<DiscoveryJob[]>([]);
  const [isLoadingJobs, setIsLoadingJobs] = useState(false);
  const [isValidatingJson, setIsValidatingJson] = useState(false);
  const [selectedVersion, setSelectedVersion] = useState<SelectProps.Option | null>(null);
  // Save mode: 'augment' (default) adds to the profile's existing schema;
  // 'replace' clears it first so discovery rebuilds the schema from scratch.
  const [saveMode, setSaveMode] = useState<'augment' | 'replace'>('augment');
  const [showCreateVersionModal, setShowCreateVersionModal] = useState(false);
  const [, setTick] = useState(0); // Force re-render for elapsed time
  const tickRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [filterText, setFilterText] = useState('');
  const [currentPage, setCurrentPage] = useState(1);
  const PAGE_SIZE = 10;
  const TIME_RANGE_OPTIONS: SelectProps.Option[] = [
    { value: '1', label: 'Last hour' },
    { value: '24', label: 'Last 24 hours' },
    { value: '48', label: 'Last 2 days' },
    { value: '168', label: 'Last 7 days' },
    { value: 'all', label: 'All time' },
  ];
  const [selectedTimeRange, setSelectedTimeRange] = useState<SelectProps.Option>(TIME_RANGE_OPTIONS[2]); // Default: 2 days
  const [sortingColumn, setSortingColumn] = useState<{ sortingField: string }>({ sortingField: 'createdAt' });
  const [sortingDescending, setSortingDescending] = useState(true);
  const [selectedJobs, setSelectedJobs] = useState<DiscoveryJob[]>([]);
  const [isDeleting, setIsDeleting] = useState(false);
  const [pageRanges, setPageRanges] = useState<PageRange[]>([]);
  const [uploadPhase, setUploadPhase] = useState<string>('');
  const [discoveryMode, setDiscoveryMode] = useState<string>('single');
  const [isAutoDetecting, setIsAutoDetecting] = useState(false);
  const [autoDetectDocKey, setAutoDetectDocKey] = useState<string | null>(null);
  const isPdf = documentFile?.name.toLowerCase().endsWith('.pdf') ?? false;
  const [tablePreferences, setTablePreferences] = useState({
    pageSize: PAGE_SIZE,
    visibleContent: ['documentKey', 'version', 'status', 'createdAt', 'elapsed', 'result'],
  });

  // Set default to active version (or first scoped version) when versions load
  useEffect(() => {
    if (versions.length > 0 && !selectedVersion) {
      const versionOptions = getVersionOptions();
      const activeVersion = versions.find((version) => version.isActive);
      if (activeVersion) {
        const activeVersionOption = versionOptions.find((option) => option.value === activeVersion.versionName);
        if (activeVersionOption) {
          setSelectedVersion(activeVersionOption);
          return;
        }
      }
      // Fallback: select first available (scoped) version
      if (versionOptions.length > 0) {
        setSelectedVersion(versionOptions[0]);
      }
    }
  }, [versions, selectedVersion, getVersionOptions]);

  // Debounced status update to prevent rapid DOM changes
  const debouncedSetUploadStatus = useCallback((statusArray: UploadStatusItem[]) => {
    setTimeout(() => {
      setUploadStatus([...statusArray]);
    }, 50);
  }, []);

  // `silent` skips the loading spinner so the 5s status poll doesn't flicker
  // the table / refresh button on every tick.
  const loadDiscoveryJobs = async (silent = false) => {
    if (!silent) setIsLoadingJobs(true);
    try {
      const response = await client.graphql({ query: listDiscoveryJobs });
      type ListJobsResp = Record<string, Record<string, Record<string, DiscoveryJob[]>>>;
      const allJobs = (response as unknown as ListJobsResp).data.listDiscoveryJobs?.DiscoveryJobs || [];
      // Filter out multi-document jobs — those belong to the MultiDocDiscoveryPanel
      const singleDocJobs = allJobs.filter((j: any) => j.jobType !== 'multi-document');
      setDiscoveryJobs(singleDocJobs);
    } catch (err) {
      console.error('Error loading discovery jobs:', err);
      if (!silent) setError(`Failed to load discovery jobs: ${(err as Error).message}`);
    } finally {
      if (!silent) setIsLoadingJobs(false);
    }
  };

  // Suppress ResizeObserver errors
  useEffect(() => {
    const originalError = console.error;
    const originalWindowError = window.onerror;

    console.error = (...args) => {
      if (args[0]?.includes?.('ResizeObserver loop completed with undelivered notifications')) {
        return;
      }
      originalError.apply(console, args);
    };

    window.onerror = (message, source, lineno, colno, errorObj) => {
      if (typeof message === 'string' && message?.includes?.('ResizeObserver loop completed with undelivered notifications')) {
        return true;
      }
      if (originalWindowError) {
        return originalWindowError(message, source, lineno, colno, errorObj);
      }
      return false;
    };

    const handleUnhandledRejection = (event: PromiseRejectionEvent) => {
      if (event.reason?.message?.includes?.('ResizeObserver loop completed with undelivered notifications')) {
        event.preventDefault();
      }
    };

    window.addEventListener('unhandledrejection', handleUnhandledRejection);

    return () => {
      console.error = originalError;
      window.onerror = originalWindowError;
      window.removeEventListener('unhandledrejection', handleUnhandledRejection);
    };
  }, []);

  // Load discovery jobs on component mount
  useEffect(() => {
    loadDiscoveryJobs();
  }, []);

  // Poll for status updates + refresh the elapsed-time display while any job is
  // active. Real-time GraphQL subscriptions were removed with AppSync (the REST
  // transport has no push channel), so polling is the only way active jobs
  // advance past PENDING here.
  useEffect(() => {
    const hasActiveJobs = discoveryJobs.some((j) => j.status === 'PENDING' || j.status === 'IN_PROGRESS' || j.status === 'OPTIMIZATION_IN_PROGRESS');
    if (hasActiveJobs && !tickRef.current) {
      tickRef.current = setInterval(() => {
        setTick((t) => t + 1);
        loadDiscoveryJobs(true);
      }, 5000);
    } else if (!hasActiveJobs && tickRef.current) {
      clearInterval(tickRef.current);
      tickRef.current = null;
    }
    return () => {
      if (tickRef.current) {
        clearInterval(tickRef.current);
        tickRef.current = null;
      }
    };
  }, [discoveryJobs]);


  if (!settings.DiscoveryBucket) {
    return (
      <Container header={<Header variant="h2">Discovery</Header>}>
        <Alert type="error">Discovery bucket not configured</Alert>
      </Container>
    );
  }

  const handleDocumentFileChange = (e: React.ChangeEvent<HTMLInputElement>): void => {
    const file = e.target.files?.[0] || null;
    setDocumentFile(file);
    setPageRanges([]); // Reset page ranges when document changes
    setAutoDetectDocKey(null); // Clear cached S3 key so auto-detect re-uploads the new document
    setUploadStatus([]);
    setError(null);
  };

  const handleGroundTruthFileChange = (e: React.ChangeEvent<HTMLInputElement>): void => {
    const file = e.target.files?.[0];
    if (!file) {
      setGroundTruthFile(null);
      setUploadStatus([]);
      setError(null);
      setIsValidatingJson(false);
      return;
    }

    if (!file.name.toLowerCase().endsWith('.json')) {
      setError('Ground truth file must be a JSON file');
      setIsValidatingJson(false);
      return;
    }

    setIsValidatingJson(true);
    setError(null);

    const reader = new FileReader();
    reader.onload = (event: ProgressEvent<FileReader>) => {
      try {
        const content = event.target?.result as string;

        if (!content || content.trim().length === 0) {
          setError('Ground truth file is empty. Please select a valid JSON file.');
          setGroundTruthFile(null);
          setIsValidatingJson(false);
          e.target.value = '';
          return;
        }

        JSON.parse(content);
        setGroundTruthFile(file);
        setUploadStatus([]);
        setError(null);
        setIsValidatingJson(false);
      } catch (jsonError) {
        const friendlyError = getJsonValidationError(jsonError as { message?: string; toString: () => string });
        setError(`Invalid JSON format in ground truth file: ${friendlyError}`);
        setGroundTruthFile(null);
        setIsValidatingJson(false);
        e.target.value = '';
      }
    };

    reader.onerror = () => {
      setError('Failed to read ground truth file');
      setGroundTruthFile(null);
      setIsValidatingJson(false);
      e.target.value = '';
    };

    reader.readAsText(file);
  };

  const handlePrefixChange = ({ detail }: { detail: { value: string } }): void => {
    setPrefix(detail.value);
  };

  const uploadFileToS3 = async (
    file: File, presignedUrl: string, objectKey: string, fileType: string, statusArray: UploadStatusItem[],
  ): Promise<void> => {
    try {
      const presignedPostData = JSON.parse(presignedUrl);

      const formData = new FormData();
      Object.entries(presignedPostData.fields).forEach(([key, value]) => {
        formData.append(key, value as string);
      });
      formData.append('file', file);

      const uploadResponse = await fetch(presignedPostData.url, {
        method: 'POST',
        body: formData,
      });

      if (!uploadResponse.ok) {
        const errorText = await uploadResponse.text().catch(() => 'Could not read error response');
        console.error(`Upload failed: ${errorText}`);
        throw new Error(`HTTP error! status: ${uploadResponse.status}`);
      }

      statusArray.push({
        file: file.name,
        type: fileType,
        status: 'success',
        objectKey,
      });
    } catch (err) {
      console.error(`Error uploading ${fileType} ${file.name}:`, err);
      statusArray.push({
        file: file.name,
        type: fileType,
        status: 'error',
        error: (err as Error).message,
      });
    }

    debouncedSetUploadStatus(statusArray);
  };

  const uploadFiles = async () => {
    if (!documentFile) {
      setError('Please select a document file to upload');
      return;
    }
    if (selectedVersion?.value === 'default') {
      setError('The "default" configuration profile is read-only. Create a new profile to save the discovered schema.');
      return;
    }

    setIsUploading(true);
    setUploadStatus([]);
    setUploadPhase('');
    setError(null);

    const newUploadStatus: UploadStatusItem[] = [];

    try {
      let groundTruthFileName = null;
      if (groundTruthFile) {
        groundTruthFileName = groundTruthFile.name;
      }

      // Convert page ranges to string format for the GraphQL mutation (e.g., ["1-3", "4-6"])
      const pageRangeStrings = pageRanges.length > 0
        ? pageRanges.map((r) => `${r.start}-${r.end}`)
        : undefined;

      // Convert page labels (parallel array to pageRanges) — only non-empty labels
      const pageLabelStrings = pageRanges.length > 0
        ? pageRanges.map((r) => r.label || '')
        : undefined;

      // Phase 1: Create discovery jobs
      const jobLabel = pageRangeStrings ? `${pageRangeStrings.length} discovery jobs` : 'discovery job';
      setUploadPhase(`Creating ${jobLabel}...`);

      const documentResponse = await client.graphql({
        query: uploadDiscoveryDocument,
        variables: {
          fileName: documentFile.name,
          contentType: documentFile.type,
          prefix: prefix || '',
          bucket: settings.DiscoveryBucket as string,
          groundTruthFileName: groundTruthFileName || '',
          version: selectedVersion?.value,
          pageRanges: pageRangeStrings,
          pageLabels: pageLabelStrings,
          discoveryType,
          saveMode,
        },
      });

      const uploadResult = documentResponse.data.uploadDiscoveryDocument;
      const docPresignedUrl = uploadResult.presignedUrl;
      const docObjectKey = uploadResult.objectKey;
      const docUsePost = uploadResult.usePostMethod?.toLowerCase() === 'true';
      const docGroundTruthObjectKey = uploadResult.groundTruthObjectKey;
      const docGroundTruthPresignedUrl = uploadResult.groundTruthPresignedUrl;

      if (!docUsePost) {
        throw new Error('Server returned PUT method which is not supported. Please update your backend code.');
      }

      // Phase 2: Upload document to S3
      const fileSizeMB = (documentFile.size / (1024 * 1024)).toFixed(1);
      setUploadPhase(`Uploading document to S3 (${fileSizeMB} MB)...`);

      await uploadFileToS3(documentFile, docPresignedUrl, docObjectKey, 'document', newUploadStatus);

      if (groundTruthFile) {
        setUploadPhase('Uploading ground truth file...');
        await uploadFileToS3(
          groundTruthFile,
          docGroundTruthPresignedUrl ?? '',
          docGroundTruthObjectKey ?? '',
          'ground truth',
          newUploadStatus,
        );
      }

      // Phase 3: Refresh job list
      setUploadPhase('Refreshing jobs list...');
      await loadDiscoveryJobs();
      setUploadPhase('');
    } catch (err) {
      console.error('Error in overall upload process:', err);
      setError(`Upload process failed: ${(err as Error).message}`);
      setUploadPhase('');
    } finally {
      setIsUploading(false);
    }
  };

  const handleDeleteSelectedJobs = async () => {
    if (selectedJobs.length === 0) return;
    setIsDeleting(true);
    setError(null);
    try {
      await Promise.all(
        selectedJobs.map((job) =>
          client.graphql({ query: deleteDiscoveryJob, variables: { jobId: job.jobId } })
        )
      );
      setSelectedJobs([]);
      await loadDiscoveryJobs();
    } catch (err) {
      console.error('Error deleting discovery jobs:', err);
      setError(`Failed to delete jobs: ${(err as Error).message}`);
    } finally {
      setIsDeleting(false);
    }
  };

  const getStatusIcon = (status: string): React.JSX.Element => {
    switch (status) {
      case 'COMPLETED':
        return <StatusIndicator type="success">Completed</StatusIndicator>;
      case 'FAILED':
        return <StatusIndicator type="error">Failed</StatusIndicator>;
      case 'IN_PROGRESS':
        return <StatusIndicator type="in-progress">In Progress</StatusIndicator>;
      case 'PENDING':
        return <StatusIndicator type="pending">Pending</StatusIndicator>;
      case 'OPTIMIZATION_IN_PROGRESS':
        return <StatusIndicator type="in-progress">Optimizing</StatusIndicator>;
      case 'OPTIMIZATION_COMPLETED':
        return <StatusIndicator type="success">Optimized</StatusIndicator>;
      case 'OPTIMIZATION_FAILED':
        return <StatusIndicator type="error">Optimization Failed</StatusIndicator>;
      default:
        return <StatusIndicator type="info">{status}</StatusIndicator>;
    }
  };

  /** Render the Result / Details column — the key UX improvement. */
  const renderResultCell = (item: DiscoveryJob): React.JSX.Element => {
    // SUCCESS: show discovered class name prominently
    if (item.status === 'COMPLETED' && item.discoveredClassName) {
      return (
        <Box>
          <Badge color="green">{item.discoveredClassName}</Badge>
        </Box>
      );
    }

    // Optimization completed: show class name + optimization result
    if (item.status === 'OPTIMIZATION_COMPLETED' && item.discoveredClassName) {
      return (
        <Box>
          <Badge color="green">{item.discoveredClassName}</Badge>
          <Box fontSize="body-s" color="text-body-secondary" margin={{ top: 'xxs' }}>
            {item.statusMessage || 'Blueprint optimization completed'}
          </Box>
        </Box>
      );
    }

    // Optimization completed without class name
    if (item.status === 'OPTIMIZATION_COMPLETED') {
      return (
        <Box fontSize="body-s" color="text-body-secondary">
          {item.statusMessage || 'Blueprint optimization completed'}
        </Box>
      );
    }

    // COMPLETED but no class name (backward compatibility with old jobs)
    if (item.status === 'COMPLETED') {
      return (
        <Box fontSize="body-s" color="text-body-secondary">
          {item.statusMessage || 'Discovery completed'}
        </Box>
      );
    }

    // FAILED: show error message prominently
    if (item.status === 'FAILED' || item.status === 'OPTIMIZATION_FAILED') {
      const errorMsg = item.errorMessage || item.statusMessage || 'Unknown error';
      return (
        <ExpandableSection
          variant="footer"
          headerText="Show error details"
          defaultExpanded={false}
        >
          <Box fontSize="body-s" color="text-status-error">
            {errorMsg}
          </Box>
        </ExpandableSection>
      );
    }

    // Optimization in progress: show status message
    if (item.status === 'OPTIMIZATION_IN_PROGRESS') {
      return (
        <Box fontSize="body-s" color="text-body-secondary">
          <StatusIndicator type="in-progress">{item.statusMessage || 'Optimizing blueprint...'}</StatusIndicator>
        </Box>
      );
    }

    // IN_PROGRESS or PENDING: show live status message
    if (item.statusMessage) {
      return (
        <Box fontSize="body-s" color="text-body-secondary">
          <StatusIndicator type="in-progress">{item.statusMessage}</StatusIndicator>
        </Box>
      );
    }

    // Default for PENDING with no message yet
    if (item.status === 'PENDING') {
      return (
        <Box fontSize="body-s" color="text-body-secondary">
          Waiting in queue...
        </Box>
      );
    }

    return <span>—</span>;
  };

  /** Extract the original filename from the document key, stripping the timestamp prefix added by the upload resolver. */
  const getOriginalFileName = (documentKey: string | undefined): string => {
    if (!documentKey) return '—';
    // Key format: "document/20260316_204035_OriginalName.pdf" or "prefix/document/20260316_204035_OriginalName.pdf"
    const fileName = documentKey.split('/').pop() || documentKey;
    // Strip leading YYYYMMDD_HHMMSS_ prefix if present
    const stripped = fileName.replace(/^\d{8}_\d{6}_/, '');
    return stripped || fileName;
  };

  // Sort jobs by the selected column
  const sortedJobs = [...discoveryJobs].sort((a, b) => {
    const field = sortingColumn.sortingField as keyof DiscoveryJob;
    const valA = a[field];
    const valB = b[field];

    // Handle date fields
    if (field === 'createdAt' || field === 'updatedAt') {
      const dateA = valA ? new Date(valA as string).getTime() : 0;
      const dateB = valB ? new Date(valB as string).getTime() : 0;
      return sortingDescending ? dateB - dateA : dateA - dateB;
    }

    // Handle string fields
    const strA = (valA as string) || '';
    const strB = (valB as string) || '';
    const cmp = strA.localeCompare(strB);
    return sortingDescending ? -cmp : cmp;
  });

  // Compute time range cutoff
  const timeRangeCutoff = selectedTimeRange.value === 'all'
    ? 0
    : Date.now() - (Number(selectedTimeRange.value) * 60 * 60 * 1000);

  const filteredJobs = sortedJobs.filter((job) => {
    // Time range filter
    if (timeRangeCutoff > 0 && job.createdAt) {
      const jobTime = parseUtcTimestamp(job.createdAt);
      if (!Number.isNaN(jobTime) && jobTime < timeRangeCutoff) return false;
    }
    // Text filter
    if (filterText) {
      const search = filterText.toLowerCase();
      const docName = getOriginalFileName(job.documentKey).toLowerCase();
      const matchesText = (
        docName.includes(search) ||
        (job.version || '').toLowerCase().includes(search) ||
        (job.status || '').toLowerCase().includes(search) ||
        (job.discoveredClassName || '').toLowerCase().includes(search) ||
        (job.errorMessage || '').toLowerCase().includes(search)
      );
      if (!matchesText) return false;
    }
    return true;
  });

  // Paginate
  const totalPages = Math.max(1, Math.ceil(filteredJobs.length / PAGE_SIZE));
  const paginatedJobs = filteredJobs.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);

  const discoveryJobsColumns = [
    {
      id: 'documentKey',
      header: 'Document',
      cell: (item: DiscoveryJob) => {
        const name = getOriginalFileName(item.documentKey);
        const content = item.pageRange ? (
          <span>
            {name} <Badge color="blue">pp {item.pageRange}</Badge>
          </span>
        ) : (
          name
        );
        return (
          <Link onFollow={() => navigate(`${DISCOVERY_JOB_PATH}/${item.jobId}`)}>
            {content}
          </Link>
        );
      },
      sortingField: 'documentKey',
    },
    {
      id: 'version',
      header: 'Config Profile',
      cell: (item: DiscoveryJob) => formatConfigVersionLink(item.version, versions as unknown as ConfigVersion[]),
      width: 140,
    },
    {
      id: 'status',
      header: 'Status',
      cell: (item: DiscoveryJob) => getStatusIcon(item.status),
      sortingField: 'status',
      width: 140,
    },
    {
      id: 'createdAt',
      header: 'Created',
      cell: (item: DiscoveryJob) => {
        if (!item.createdAt) return '—';
        try {
          return new Date(item.createdAt).toLocaleString(undefined, {
            month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
          });
        } catch {
          return item.createdAt;
        }
      },
      sortingField: 'createdAt',
      width: 150,
    },
    {
      id: 'elapsed',
      header: 'Duration',
      cell: (item: DiscoveryJob) => {
        if (item.status === 'COMPLETED' || item.status === 'FAILED' || item.status === 'OPTIMIZATION_COMPLETED' || item.status === 'OPTIMIZATION_FAILED') {
          // Show total duration: from createdAt to updatedAt
          if (item.createdAt && item.updatedAt) {
            const start = parseUtcTimestamp(item.createdAt);
            const end = parseUtcTimestamp(item.updatedAt);
            if (!Number.isNaN(start) && !Number.isNaN(end)) {
              const elapsed = Math.max(0, Math.floor((end - start) / 1000));
              const mins = Math.floor(elapsed / 60);
              const secs = elapsed % 60;
              return `${mins}:${secs.toString().padStart(2, '0')}`;
            }
          }
          return '—';
        }
        // Show live elapsed time for active jobs
        return formatElapsed(item.createdAt);
      },
      width: 90,
    },
    {
      id: 'result',
      header: 'Result',
      cell: (item: DiscoveryJob) => renderResultCell(item),
      minWidth: 250,
    },
    {
      id: 'jobId',
      header: 'Job ID',
      cell: (item: DiscoveryJob) => (
        <Box fontSize="body-s" color="text-body-secondary">{item.jobId.substring(0, 12)}...</Box>
      ),
      width: 140,
    },
  ];

  return (
    <SpaceBetween size="l">
      <Container header={<Header variant="h2">Discovery</Header>}>
        <Alert type="warning" header="Important Notice">
          Use the Discovery feature in non-production environments to discover class models from documents and images. 
          Discovery creates a starting point, not a final class model config. Be sure to inspect, test and 
          refine the generated custom class configuration before exporting it to production.
        </Alert>

        {error && (
          <Alert type="error" dismissible onDismiss={() => setError(null)}>
            {error}
          </Alert>
        )}

        <SpaceBetween size="l">
          <TextContent>
            <p>
              Upload a document to start a discovery analysis. The system will use AI to analyze the document structure
              and generate a document class schema. Optionally upload ground truth data (JSON) to guide the discovery.
            </p>
          </TextContent>

          <FormField
            label="Configuration Profile"
            description="Select which configuration profile to save the discovered document schema to, or create a new one"
          >
            <SpaceBetween size="xs" direction="horizontal" alignItems="end">
              <Select
                selectedOption={selectedVersion}
                onChange={({ detail }) => setSelectedVersion(detail.selectedOption)}
                options={getVersionOptions()}
                placeholder={versions.length === 0 ? 'Loading profiles...' : 'Select configuration profile'}
                disabled={isUploading || versionsLoading || versions.length === 0}
                loadingText="Loading profiles..."
              />
              <Button iconName="add-plus" onClick={() => setShowCreateVersionModal(true)} disabled={isUploading}>
                Create profile
              </Button>
            </SpaceBetween>
          </FormField>

          {selectedVersion?.value === 'default' && (
            <Alert type="warning">
              The <strong>default</strong> configuration profile is read-only and cannot be overwritten by discovery. Click{' '}
              <strong>Create profile</strong> to save the discovered schema to a new configuration profile (it will be seeded from{' '}
              <strong>default</strong>).
            </Alert>
          )}

          <FormField
            label="Save mode"
            description="Choose whether discovered classes are added to the profile's existing schema or replace it"
          >
            <RadioGroup
              value={saveMode}
              onChange={({ detail }) => setSaveMode(detail.value as 'augment' | 'replace')}
              items={[
                {
                  value: 'augment',
                  label: 'Add to existing schema',
                  description: 'Keep the existing document classes and add/update discovered ones (classes with the same name are overwritten).',
                },
                {
                  value: 'replace',
                  label: 'Replace existing schema',
                  description: 'Remove all existing document classes in the selected profile, then save only the newly discovered ones.',
                },
              ]}
            />
          </FormField>

          {saveMode === 'replace' && selectedVersion && (
            <Alert type="warning">
              Replace mode removes all existing document classes in profile <strong>{selectedVersion.value}</strong>{' '}
              <strong>immediately, before discovery runs</strong>, then saves the newly discovered schema. If discovery fails, the profile is
              left with no classes. This cannot be undone — consider creating a new profile instead.
            </Alert>
          )}

          <ColumnLayout columns={2}>
            <FormField label="Document File" description="Select the document to analyze">
              <input
                type="file"
                onChange={handleDocumentFileChange}
                disabled={isUploading}
                accept={SUPPORTED_DISCOVERY_EXTENSIONS}
              />
              {documentFile && (
                <Box margin={{ top: 'xs' }}>
                  <StatusIndicator type="success">Selected: {documentFile.name}</StatusIndicator>
                </Box>
              )}
            </FormField>
            <div />
          </ColumnLayout>

          {/* Discovery Mode selector — only shown for PDFs in classes (not rules/policy) mode */}
          {isPdf && documentFile && shouldShowClassesDiscoveryControls(discoveryType) && (
            <FormField label="Discovery Mode">
              <Tiles
                value={discoveryMode}
                onChange={({ detail }) => {
                  setDiscoveryMode(detail.value);
                  // Clear mode-specific state when switching
                  if (detail.value === 'single') {
                    setPageRanges([]);
                  } else {
                    setGroundTruthFile(null);
                  }
                }}
                columns={2}
                items={[
                  {
                    value: 'single',
                    label: 'Single Section Document',
                    description: 'Discover one class from the entire document, with optional ground truth',
                  },
                  {
                    value: 'multi',
                    label: 'Multi-Section Package',
                    description: 'Define page ranges to discover multiple classes from different sections',
                  },
                ]}
              />
            </FormField>
          )}

          {/* Single-class mode: Ground Truth file input — only shown after document is selected (not for policy/rules) */}
          {documentFile && (discoveryMode === 'single' || !isPdf) && shouldShowClassesDiscoveryControls(discoveryType) && (
            <FormField label="Ground Truth File (optional)" description="JSON file with expected field structure to guide discovery">
              <input
                type="file"
                onChange={handleGroundTruthFileChange}
                disabled={isUploading || isValidatingJson}
                accept=".json"
              />
              {isValidatingJson && (
                <Box margin={{ top: 'xs' }}>
                  <StatusIndicator type="in-progress">Validating JSON format...</StatusIndicator>
                </Box>
              )}
              {groundTruthFile && !isValidatingJson && (
                <Box margin={{ top: 'xs' }}>
                  <StatusIndicator type="success">Selected: {groundTruthFile.name} (Valid JSON)</StatusIndicator>
                </Box>
              )}
            </FormField>
          )}

          {/* Multi-section mode: Page Range Selector */}
          {discoveryMode === 'multi' && isPdf && documentFile && shouldShowClassesDiscoveryControls(discoveryType) && (
            <PdfPageSelector
              file={documentFile}
              pageRanges={pageRanges}
              onPageRangesChange={setPageRanges}
              disabled={isUploading}
              isAutoDetecting={isAutoDetecting}
              onAutoDetect={async () => {
                if (!documentFile) return;
                setIsAutoDetecting(true);
                setError(null);
                try {
                  // Step 1: Upload file to S3 if not already uploaded
                  let docKey = autoDetectDocKey;
                  if (!docKey) {
                    const uploadResp = await client.graphql({
                      query: uploadDiscoveryDocument,
                      variables: {
                        fileName: documentFile.name,
                        contentType: documentFile.type,
                        prefix: prefix || '',
                        bucket: settings.DiscoveryBucket as string,
                        groundTruthFileName: '',
                        version: selectedVersion?.value,
                        skipJobCreation: true, // Only need presigned URL — don't create discovery jobs
                      },
                    });
                    const uploadResult = uploadResp.data.uploadDiscoveryDocument;
                    docKey = uploadResult.objectKey;
                    // Upload to S3
                    const presignedPostData = JSON.parse(uploadResult.presignedUrl);
                    const formData = new FormData();
                    Object.entries(presignedPostData.fields).forEach(([k, v]) => formData.append(k, v as string));
                    formData.append('file', documentFile);
                    await fetch(presignedPostData.url, { method: 'POST', body: formData });
                    setAutoDetectDocKey(docKey);
                  }

                  // Step 2: Call auto-detect sections
                  const detectResp = await client.graphql({
                    query: autoDetectSections,
                    variables: {
                      documentKey: docKey,
                      bucket: settings.DiscoveryBucket as string,
                      version: selectedVersion?.value,
                    },
                  });
                  const sectionsJson = (detectResp as { data: { autoDetectSections: string } }).data.autoDetectSections;
                  const sections = JSON.parse(sectionsJson) as Array<{ start: number; end: number; type?: string }>;

                  // Step 3: Convert to page ranges with labels from LLM
                  const detectedRanges: PageRange[] = sections.map((s) => ({
                    start: s.start,
                    end: s.end,
                    label: s.type || '',
                  }));
                  setPageRanges(detectedRanges);
                  console.log('Auto-detected sections:', sections);
                } catch (err) {
                  console.error('Auto-detect sections failed:', err);
                  setError(`Auto-detect failed: ${(err as Error).message}`);
                } finally {
                  setIsAutoDetecting(false);
                }
              }}
            />
          )}

          {/* Advanced options */}
          <ExpandableSection headerText="Advanced options" variant="footer" defaultExpanded={false}>
            <FormField label="Folder prefix" description="Optional S3 folder prefix (e.g., experiments/batch1)">
              <Input
                value={prefix}
                onChange={handlePrefixChange}
                placeholder="Leave empty for root folder"
                disabled={isUploading}
              />
            </FormField>
          </ExpandableSection>

          <SpaceBetween size="xs" direction="horizontal" alignItems="center">
            <Button
              variant="primary"
              onClick={uploadFiles}
              loading={isUploading}
              disabled={
                !documentFile || !selectedVersion || selectedVersion.value === 'default' || isUploading || isValidatingJson
              }
            >
              {pageRanges.length > 0
                ? `Start Discovery (${pageRanges.length} section${pageRanges.length !== 1 ? 's' : ''})`
                : 'Start Discovery'}
            </Button>
            {isUploading && uploadPhase && (
              <StatusIndicator type="in-progress">{uploadPhase}</StatusIndicator>
            )}
          </SpaceBetween>

          {uploadStatus.length > 0 && (
            <Container header={<Header variant="h3">Upload Results</Header>}>
              <SpaceBetween size="s">
                {uploadStatus.map((item, index) => (
                  <div key={`upload-status-${item.file}-${index}`}>
                    <StatusIndicator type={item.status === 'success' ? 'success' : 'error'}>
                      {item.type}: {item.file}{' '}
                      {item.status === 'success' ? 'Uploaded successfully' : `Failed - ${item.error}`}
                    </StatusIndicator>
                  </div>
                ))}
              </SpaceBetween>
            </Container>
          )}
        </SpaceBetween>
      </Container>

      <Table
        columnDefinitions={discoveryJobsColumns}
        items={paginatedJobs}
        loading={isLoadingJobs}
        loadingText="Loading discovery jobs..."
        resizableColumns
        sortingColumn={sortingColumn}
        sortingDescending={sortingDescending}
        onSortingChange={({ detail }) => {
          setSortingColumn(detail.sortingColumn as { sortingField: string });
          setSortingDescending(detail.isDescending ?? false);
          setCurrentPage(1);
        }}
        selectionType="multi"
        selectedItems={selectedJobs}
        onSelectionChange={({ detail }) => setSelectedJobs(detail.selectedItems as DiscoveryJob[])}
        trackBy="jobId"
        visibleColumns={tablePreferences.visibleContent}
        variant="container"
        filter={
          <TextFilter
            filteringPlaceholder="Find discovery jobs"
            filteringText={filterText}
            onChange={({ detail }) => {
              setFilterText(detail.filteringText);
              setCurrentPage(1);
            }}
          />
        }
        pagination={
          <Pagination
            currentPageIndex={currentPage}
            pagesCount={totalPages}
            onChange={({ detail }) => setCurrentPage(detail.currentPageIndex)}
          />
        }
        empty={
          <Box textAlign="center" color="inherit">
            <b>No discovery jobs found</b>
            <Box padding={{ bottom: 's' }} variant="p" color="inherit">
              {filterText ? 'No jobs match the current filter.' : 'Upload documents above to start discovery analysis.'}
            </Box>
          </Box>
        }
        header={
          <Header
            counter={`(${filteredJobs.length})`}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Select
                  selectedOption={selectedTimeRange}
                  onChange={({ detail }) => {
                    setSelectedTimeRange(detail.selectedOption);
                    setCurrentPage(1);
                  }}
                  options={TIME_RANGE_OPTIONS}
                  triggerVariant="option"
                />
                <Button
                  iconName="refresh"
                  variant="icon"
                  onClick={() => loadDiscoveryJobs()}
                  loading={isLoadingJobs}
                  ariaLabel="Refresh discovery jobs"
                />
                <Button
                  iconName="remove"
                  variant="icon"
                  onClick={handleDeleteSelectedJobs}
                  loading={isDeleting}
                  disabled={selectedJobs.length === 0}
                  ariaLabel="Delete selected discovery jobs"
                />
              </SpaceBetween>
            }
          >
            Discovery Jobs
          </Header>
        }
        preferences={
          <CollectionPreferences
            title="Preferences"
            confirmLabel="Confirm"
            cancelLabel="Cancel"
            preferences={tablePreferences}
            onConfirm={({ detail }) => setTablePreferences(detail as typeof tablePreferences)}
            pageSizePreference={{
              title: 'Page size',
              options: [
                { value: 10, label: '10 jobs' },
                { value: 25, label: '25 jobs' },
                { value: 50, label: '50 jobs' },
              ],
            }}
            visibleContentPreference={{
              title: 'Visible columns',
              options: [
                {
                  label: 'Job properties',
                  options: [
                    { id: 'documentKey', label: 'Document' },
                    { id: 'version', label: 'Config Profile' },
                    { id: 'status', label: 'Status' },
                    { id: 'createdAt', label: 'Created' },
                    { id: 'elapsed', label: 'Duration' },
                    { id: 'result', label: 'Result' },
                    { id: 'jobId', label: 'Job ID' },
                  ],
                },
              ],
            }}
          />
        }
      />

      <CreateConfigProfileModal
        visible={showCreateVersionModal}
        onDismiss={() => setShowCreateVersionModal(false)}
        defaultSourceVersion={selectedVersion?.value ?? null}
        infoText="Creates a new configuration profile that inherits its settings and document classes from the selected source profile. Discovered schema will then be saved to this new profile."
        onCreated={async (versionName) => {
          setShowCreateVersionModal(false);
          await fetchVersions();
          setSelectedVersion({ label: versionName, value: versionName });
        }}
      />
    </SpaceBetween>
  );
};

export default DiscoveryPanel;
