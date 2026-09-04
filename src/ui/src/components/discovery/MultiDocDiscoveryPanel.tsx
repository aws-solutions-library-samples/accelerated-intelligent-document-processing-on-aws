// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/* eslint-disable @typescript-eslint/no-explicit-any, @typescript-eslint/no-unused-vars, react/no-array-index-key */

/**
 * Multi-Document Discovery Panel
 *
 * Enables users to discover document classes from a collection of documents.
 * Supports two input modes:
 *   1. S3 Path — point to an existing S3 prefix with documents
 *   2. Zip Upload — upload a zip file of documents
 *
 * The backend pipeline: Embed → Cluster → Analyze → Save
 * discovers document types, generates JSON Schemas, and saves to config.
 */

import React, { useState, useEffect, useCallback } from 'react';
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
  ColumnLayout,
  Select,
  ExpandableSection,
  Badge,
  Link,
  Tiles,
  RadioGroup,
  TextContent,
  TextFilter,
  Pagination,
  CollectionPreferences,
} from '@cloudscape-design/components';
import type { SelectProps } from '@cloudscape-design/components';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { generateClient } from '../../api/client-shim';

import { startMultiDocDiscovery, uploadMultiDocDiscoveryZip, listDiscoveryJobs, deleteDiscoveryJob } from '../../graphql/generated';
import { useNavigate } from 'react-router-dom';
import { DISCOVERY_JOB_PATH } from '../../routes/constants';
import useSettingsContext from '../../contexts/settings';
import useConfigurationVersions from '../../hooks/use-configuration-versions';
import { formatConfigVersionLink } from '../test-studio/utils/configVersionUtils';
import type { ConfigVersion } from '../test-studio/utils/configVersionUtils';
import CreateConfigProfileModal from '../common/CreateConfigProfileModal';

const client = generateClient();

// Pipeline steps for progress display
const PIPELINE_STEPS = [
  { key: 'QUEUED', label: 'Queued' },
  { key: 'PREPARING', label: 'Listing Documents' },
  { key: 'EMBEDDING', label: 'Generating Embeddings' },
  { key: 'CLUSTERING', label: 'Clustering Documents' },
  { key: 'ANALYZING', label: 'Analyzing Clusters' },
  { key: 'COMPLETED', label: 'Complete' },
];

interface MultiDocJob {
  jobId: string;
  status: string;
  createdAt?: string;
  updatedAt?: string;
  errorMessage?: string;
  version?: string;
  jobType?: string;
  currentStep?: string;
  totalDocuments?: number;
  clustersFound?: number;
  discoveredClasses?: string;
  reflectionReport?: string;
  documentKey?: string;
}

interface DiscoveredClass {
  cluster_id: number;
  classification: string;
  json_schema: Record<string, unknown>;
  document_count: number;
  sample_doc_ids?: string[];
  error?: string;
}

/**
 * Parse an ISO timestamp, normalizing timezone.
 * Backend timestamps may or may not have a Z suffix — treat all as UTC.
 */
function parseUtcTimestamp(ts?: string): Date | null {
  if (!ts) return null;
  const hasTimezone = /[Zz]$|[+-]\d{2}:\d{2}$|[+-]\d{4}$|[+-]\d{2}$/.test(ts);
  const normalized = hasTimezone ? ts : `${ts}Z`;
  const d = new Date(normalized);
  return isNaN(d.getTime()) ? null : d;
}

function formatElapsed(startStr?: string, endStr?: string): string {
  const start = parseUtcTimestamp(startStr);
  if (!start) return '—';
  const end = endStr ? parseUtcTimestamp(endStr) : new Date();
  if (!end) return '—';
  const diffMs = end.getTime() - start.getTime();
  const secs = Math.max(0, Math.floor(diffMs / 1000));
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  const remainSecs = secs % 60;
  return `${mins}m ${remainSecs}s`;
}

function getStepIndex(status: string): number {
  const idx = PIPELINE_STEPS.findIndex((s) => s.key === status);
  return idx >= 0 ? idx : 0;
}

function parseDiscoveredClasses(json?: string): DiscoveredClass[] {
  if (!json) return [];
  try {
    return JSON.parse(json);
  } catch {
    return [];
  }
}

// Build bucket options from settings
const getBucketOptions = (settings: Record<string, unknown>): SelectProps.Option[] => {
  const options: SelectProps.Option[] = [];
  if (settings.DiscoveryBucket)
    options.push({ label: 'Discovery Bucket', value: settings.DiscoveryBucket as string, description: settings.DiscoveryBucket as string });
  if (settings.TestSetBucket)
    options.push({ label: 'Test Set Bucket', value: settings.TestSetBucket as string, description: settings.TestSetBucket as string });
  if (settings.InputBucket)
    options.push({ label: 'Input Bucket', value: settings.InputBucket as string, description: settings.InputBucket as string });
  return options;
};

const TIME_RANGE_OPTIONS: SelectProps.Option[] = [
  { value: '1', label: 'Last hour' },
  { value: '24', label: 'Last 24 hours' },
  { value: '48', label: 'Last 2 days' },
  { value: '168', label: 'Last 7 days' },
  { value: 'all', label: 'All time' },
];

const DEFAULT_PAGE_SIZE = 10;

const MultiDocDiscoveryPanel = () => {
  const navigate = useNavigate();
  // Settings & configuration profiles
  const { settings } = useSettingsContext();
  const { versions, loading: versionsLoading, getVersionOptions, fetchVersions } = useConfigurationVersions();
  const [selectedVersion, setSelectedVersion] = useState<SelectProps.Option | null>(null);
  // Save mode: 'augment' (default) adds to the profile's existing schema;
  // 'replace' clears it first so discovery rebuilds the schema from scratch.
  const [saveMode, setSaveMode] = useState<'augment' | 'replace'>('augment');
  const [showCreateVersionModal, setShowCreateVersionModal] = useState(false);

  // Input mode
  const [inputMode, setInputMode] = useState<string>('s3path');
  const [selectedBucket, setSelectedBucket] = useState<SelectProps.Option | null>(null);
  const [s3Prefix, setS3Prefix] = useState('');
  const bucketOptions = getBucketOptions(settings);
  const [zipFile, setZipFile] = useState<File | null>(null);

  // State
  const [jobs, setJobs] = useState<MultiDocJob[]>([]);
  const [loading, setLoading] = useState(false);
  const [starting, setStarting] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedJobs, setSelectedJobs] = useState<MultiDocJob[]>([]);
  const [isDeleting, setIsDeleting] = useState(false);

  // Table controls: search, time range, pagination, preferences, sorting
  const [filterText, setFilterText] = useState('');
  const [currentPage, setCurrentPage] = useState(1);
  const [selectedTimeRange, setSelectedTimeRange] = useState<SelectProps.Option>(TIME_RANGE_OPTIONS[2]); // Default: 2 days
  const [sortingColumn, setSortingColumn] = useState<{ sortingField: string }>({ sortingField: 'createdAt' });
  const [sortingDescending, setSortingDescending] = useState(true);
  const [tablePreferences, setTablePreferences] = useState({
    pageSize: DEFAULT_PAGE_SIZE,
    visibleContent: ['source', 'status', 'currentStep', 'totalDocuments', 'clustersFound', 'version', 'createdAt', 'duration', 'result'],
  });

  // Timer for elapsed time
  const [, setTick] = useState(0);

  // `silent` skips the loading spinner so the status poll doesn't flicker the
  // table on every tick.
  const loadJobs = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const response = await client.graphql({ query: listDiscoveryJobs });
      const allJobs = (response as any)?.data?.listDiscoveryJobs?.DiscoveryJobs || [];
      // Filter to multi-document jobs only
      const multiDocJobs = allJobs.filter((j: any) => j.jobType === 'multi-document');
      setJobs(multiDocJobs);
    } catch (err: any) {
      console.error('Failed to load jobs:', err);
      if (!silent) setError('Failed to load discovery jobs');
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  // Load jobs on mount
  useEffect(() => {
    loadJobs();
  }, [loadJobs]);

  // Poll for status updates + refresh the elapsed-time display while any job is
  // active. Real-time GraphQL subscriptions were removed with AppSync (the REST
  // transport has no push channel), so polling is the only way active jobs
  // advance past PENDING here.
  useEffect(() => {
    const hasActiveJobs = jobs.some((j) => !['COMPLETED', 'FAILED'].includes(j.status));
    if (!hasActiveJobs) return;
    const timer = setInterval(() => {
      setTick((t) => t + 1);
      loadJobs(true);
    }, 5000);
    return () => clearInterval(timer);
  }, [jobs, loadJobs]);

  const handleStartDiscovery = async () => {
    if (!selectedVersion) {
      setError('Please select a configuration profile');
      return;
    }
    if (selectedVersion.value === 'default') {
      setError('The "default" configuration profile is read-only. Create a new profile to save the discovered schema.');
      return;
    }

    setStarting(true);
    setError(null);

    try {
      if (inputMode === 'zip' && zipFile) {
        // Step 1: Get presigned URL for zip upload
        setUploading(true);
        const uploadResponse = await client.graphql({
          query: uploadMultiDocDiscoveryZip,
          variables: {
            fileName: zipFile.name,
            fileSize: zipFile.size,
            configVersion: selectedVersion.value!,
          },
        });

        const uploadData = (uploadResponse as any)?.data?.uploadMultiDocDiscoveryZip;
        if (!uploadData?.presignedUrl) {
          throw new Error('Failed to get upload URL');
        }

        // Step 2: Upload zip file via presigned URL
        const uploadResult = await fetch(uploadData.presignedUrl, {
          method: 'PUT',
          body: zipFile,
          headers: { 'Content-Type': 'application/zip' },
        });
        if (!uploadResult.ok) {
          throw new Error(`Upload failed: ${uploadResult.statusText}`);
        }
        setUploading(false);

        // Step 3: Start discovery with zip reference
        const startResponse = await client.graphql({
          query: startMultiDocDiscovery,
          variables: {
            configVersion: selectedVersion.value!,
            zipFileName: zipFile.name,
            zipFileSize: zipFile.size,
            saveMode,
          },
        });

        const job = (startResponse as any)?.data?.startMultiDocDiscovery;
        if (job) {
          setJobs((prev) => [job, ...prev]);
        }
      } else {
        // S3 path mode
        if (!selectedBucket && !s3Prefix) {
          setError('Please select a bucket and provide a prefix');
          setStarting(false);
          return;
        }

        const startResponse = await client.graphql({
          query: startMultiDocDiscovery,
          variables: {
            s3Bucket: selectedBucket?.value || undefined,
            s3Prefix: s3Prefix || undefined,
            configVersion: selectedVersion.value!,
            saveMode,
          },
        });

        const job = (startResponse as any)?.data?.startMultiDocDiscovery;
        if (job) {
          setJobs((prev) => [job, ...prev]);
        }
      }

      // Reset form
      setZipFile(null);
      setSelectedBucket(null);
      setS3Prefix('');
    } catch (err: any) {
      console.error('Failed to start discovery:', err);
      setError(err?.errors?.[0]?.message || err?.message || 'Failed to start discovery');
    } finally {
      setStarting(false);
      setUploading(false);
    }
  };

  const handleDeleteJobs = async () => {
    if (selectedJobs.length === 0) return;
    setIsDeleting(true);
    setError(null);
    try {
      await Promise.all(selectedJobs.map((job) => client.graphql({ query: deleteDiscoveryJob, variables: { jobId: job.jobId } })));
      setJobs((prev) => prev.filter((j) => !selectedJobs.find((s) => s.jobId === j.jobId)));
      setSelectedJobs([]);
    } catch (err: any) {
      console.error('Failed to delete jobs:', err);
      setError('Failed to delete selected jobs');
    } finally {
      setIsDeleting(false);
    }
  };

  const renderStatus = (status: string) => {
    switch (status) {
      case 'COMPLETED':
        return <StatusIndicator type="success">Completed</StatusIndicator>;
      case 'FAILED':
        return <StatusIndicator type="error">Failed</StatusIndicator>;
      case 'QUEUED':
        return <StatusIndicator type="pending">Queued</StatusIndicator>;
      default:
        return <StatusIndicator type="in-progress">{status}</StatusIndicator>;
    }
  };

  /** Derive a human-readable source description for the job. */
  const getJobSource = (item: MultiDocJob): string => {
    // If documentKey is populated (e.g., S3 prefix path), show it
    if (item.documentKey) {
      return item.documentKey;
    }
    // Fallback: show doc count if available
    if (item.totalDocuments) {
      return `${item.totalDocuments} documents`;
    }
    return 'Multi-doc discovery';
  };

  /** Render the Result column — show discovered class names as badges, errors, or progress. */
  const renderResultCell = (item: MultiDocJob): React.JSX.Element => {
    // SUCCESS: show discovered class names
    if (item.status === 'COMPLETED') {
      const classes = parseDiscoveredClasses(item.discoveredClasses);
      if (classes.length > 0) {
        const classNames = classes.filter((c) => !c.error).map((c) => c.classification);
        const errorCount = classes.filter((c) => c.error).length;
        return (
          <Box>
            <SpaceBetween size="xxs" direction="horizontal">
              {classNames.slice(0, 5).map((name, idx) => (
                <Badge key={idx} color="green">
                  {name}
                </Badge>
              ))}
              {classNames.length > 5 && <Badge color="grey">+{classNames.length - 5} more</Badge>}
            </SpaceBetween>
            {errorCount > 0 && (
              <Box fontSize="body-s" color="text-status-error" margin={{ top: 'xxs' }}>
                {errorCount} cluster{errorCount !== 1 ? 's' : ''} failed
              </Box>
            )}
          </Box>
        );
      }
      return (
        <Box fontSize="body-s" color="text-body-secondary">
          Discovery completed
        </Box>
      );
    }

    // FAILED: show error
    if (item.status === 'FAILED') {
      const errorMsg = item.errorMessage || 'Unknown error';
      return (
        <ExpandableSection variant="footer" headerText="Show error details" defaultExpanded={false}>
          <Box fontSize="body-s" color="text-status-error">
            {errorMsg}
          </Box>
        </ExpandableSection>
      );
    }

    // IN-PROGRESS: show current step
    if (item.currentStep) {
      const step = PIPELINE_STEPS.find((s) => s.key === item.currentStep);
      return (
        <Box fontSize="body-s" color="text-body-secondary">
          <StatusIndicator type="in-progress">{step?.label || item.currentStep}</StatusIndicator>
        </Box>
      );
    }

    // QUEUED
    if (item.status === 'QUEUED') {
      return (
        <Box fontSize="body-s" color="text-body-secondary">
          Waiting in queue...
        </Box>
      );
    }

    return <span>—</span>;
  };

  /** Get the current step label for display. */
  const getCurrentStepLabel = (item: MultiDocJob): string => {
    if (item.status === 'COMPLETED') return 'Complete';
    if (item.status === 'FAILED') return 'Failed';
    if (item.currentStep) {
      const step = PIPELINE_STEPS.find((s) => s.key === item.currentStep);
      return step?.label || item.currentStep;
    }
    return '—';
  };

  const renderPipelineProgress = (job: MultiDocJob) => {
    const currentIdx = getStepIndex(job.status);
    const isFailed = job.status === 'FAILED';

    return (
      <Box padding={{ top: 'xs' }}>
        <SpaceBetween size="xxs" direction="horizontal">
          {PIPELINE_STEPS.map((step, idx) => {
            let type: 'success' | 'error' | 'in-progress' | 'pending' = 'pending';
            if (isFailed && idx === currentIdx) type = 'error';
            else if (idx < currentIdx) type = 'success';
            else if (idx === currentIdx) type = job.status === 'COMPLETED' ? 'success' : 'in-progress';

            return (
              <Box key={step.key} textAlign="center" display="inline-block" margin={{ right: 's' }}>
                <StatusIndicator type={type}>
                  <Box variant="small">{step.label}</Box>
                </StatusIndicator>
              </Box>
            );
          })}
        </SpaceBetween>
      </Box>
    );
  };

  const renderDiscoveredClasses = (job: MultiDocJob) => {
    const classes = parseDiscoveredClasses(job.discoveredClasses);
    if (classes.length === 0) return <Box color="text-status-inactive">No classes discovered yet</Box>;

    return (
      <SpaceBetween size="s">
        {classes.map((dc) => (
          <Container
            key={dc.cluster_id}
            header={
              <Header
                variant="h3"
                description={`Cluster ${dc.cluster_id} — ${dc.document_count} documents`}
                counter={dc.error ? '❌' : '✅'}
              >
                {dc.classification || `Cluster ${dc.cluster_id}`}
              </Header>
            }
          >
            {dc.error ? (
              <Alert type="error">{dc.error}</Alert>
            ) : (
              <ExpandableSection headerText="View JSON Schema" variant="footer">
                <Box>
                  <pre
                    style={{
                      fontSize: '12px',
                      maxHeight: '400px',
                      overflow: 'auto',
                      background: '#f8f8f8',
                      padding: '12px',
                      borderRadius: '4px',
                    }}
                  >
                    {JSON.stringify(dc.json_schema, null, 2)}
                  </pre>
                </Box>
              </ExpandableSection>
            )}
          </Container>
        ))}
      </SpaceBetween>
    );
  };

  const renderReflectionReport = (job: MultiDocJob) => {
    if (!job.reflectionReport) return null;
    return (
      <ExpandableSection headerText="Quality Review Report" variant="container">
        <div style={{ maxHeight: '600px', overflow: 'auto', padding: '8px' }}>
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{job.reflectionReport}</ReactMarkdown>
        </div>
      </ExpandableSection>
    );
  };

  const renderJobDetail = (job: MultiDocJob) => {
    return (
      <SpaceBetween size="m">
        {/* Pipeline Progress */}
        {renderPipelineProgress(job)}

        {/* Stats */}
        <ColumnLayout columns={4} variant="text-grid">
          <div>
            <Box variant="awsui-key-label">Total Documents</Box>
            <Box>{job.totalDocuments ?? '—'}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Clusters Found</Box>
            <Box>{job.clustersFound ?? '—'}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Config Profile</Box>
            <Box>{job.version || '—'}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Duration</Box>
            <Box>{formatElapsed(job.createdAt, job.status === 'COMPLETED' || job.status === 'FAILED' ? job.updatedAt : undefined)}</Box>
          </div>
        </ColumnLayout>

        {/* Error */}
        {job.errorMessage && (
          <Alert type="error" header="Error">
            {job.errorMessage}
          </Alert>
        )}

        {/* Discovered Classes */}
        {job.discoveredClasses && (
          <ExpandableSection
            headerText={`Discovered Classes (${parseDiscoveredClasses(job.discoveredClasses).length})`}
            variant="container"
            defaultExpanded={job.status === 'COMPLETED'}
          >
            {renderDiscoveredClasses(job)}
          </ExpandableSection>
        )}

        {/* Reflection Report */}
        {renderReflectionReport(job)}
      </SpaceBetween>
    );
  };

  // Sort jobs by the selected column
  const sortedJobs = [...jobs].sort((a, b) => {
    const field = sortingColumn.sortingField as keyof MultiDocJob;
    let valA = a[field];
    let valB = b[field];

    // Handle date fields
    if (field === 'createdAt' || field === 'updatedAt') {
      const dateA = valA ? new Date(valA as string).getTime() : 0;
      const dateB = valB ? new Date(valB as string).getTime() : 0;
      return sortingDescending ? dateB - dateA : dateA - dateB;
    }

    // Handle numeric fields
    if (field === 'totalDocuments' || field === 'clustersFound') {
      const numA = (valA as number) ?? 0;
      const numB = (valB as number) ?? 0;
      return sortingDescending ? numB - numA : numA - numB;
    }

    // Handle string fields
    valA = (valA as string) || '';
    valB = (valB as string) || '';
    const cmp = (valA as string).localeCompare(valB as string);
    return sortingDescending ? -cmp : cmp;
  });

  // Compute time range cutoff
  const timeRangeCutoff = selectedTimeRange.value === 'all' ? 0 : Date.now() - Number(selectedTimeRange.value) * 60 * 60 * 1000;

  // Filter by time range and search text
  const filteredJobs = sortedJobs.filter((job) => {
    // Time range filter
    if (timeRangeCutoff > 0 && job.createdAt) {
      const jobTime = parseUtcTimestamp(job.createdAt)?.getTime();
      if (jobTime && jobTime < timeRangeCutoff) return false;
    }
    // Text filter
    if (filterText) {
      const search = filterText.toLowerCase();
      const classes = parseDiscoveredClasses(job.discoveredClasses);
      const classNames = classes
        .map((c) => c.classification)
        .join(' ')
        .toLowerCase();
      const matchesText =
        (job.jobId || '').toLowerCase().includes(search) ||
        (job.version || '').toLowerCase().includes(search) ||
        (job.status || '').toLowerCase().includes(search) ||
        (job.currentStep || '').toLowerCase().includes(search) ||
        (job.errorMessage || '').toLowerCase().includes(search) ||
        (job.documentKey || '').toLowerCase().includes(search) ||
        classNames.includes(search);
      if (!matchesText) return false;
    }
    return true;
  });

  // Paginate
  const pageSize = tablePreferences.pageSize || DEFAULT_PAGE_SIZE;
  const totalPages = Math.max(1, Math.ceil(filteredJobs.length / pageSize));
  const paginatedJobs = filteredJobs.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  const columnDefinitions = [
    {
      id: 'source',
      header: 'Source',
      cell: (item: MultiDocJob) => {
        const source = getJobSource(item);
        return <Link onFollow={() => navigate(`${DISCOVERY_JOB_PATH}/${item.jobId}`)}>{source}</Link>;
      },
      sortingField: 'documentKey',
    },
    {
      id: 'status',
      header: 'Status',
      cell: (item: MultiDocJob) => renderStatus(item.status),
      sortingField: 'status',
      width: 130,
    },
    {
      id: 'currentStep',
      header: 'Current Step',
      cell: (item: MultiDocJob) => getCurrentStepLabel(item),
      width: 170,
    },
    {
      id: 'totalDocuments',
      header: 'Documents',
      cell: (item: MultiDocJob) => item.totalDocuments ?? '—',
      width: 100,
    },
    {
      id: 'clustersFound',
      header: 'Clusters',
      cell: (item: MultiDocJob) => item.clustersFound ?? '—',
      width: 90,
    },
    {
      id: 'version',
      header: 'Config Profile',
      cell: (item: MultiDocJob) => formatConfigVersionLink(item.version, versions as unknown as ConfigVersion[]),
      width: 140,
    },
    {
      id: 'createdAt',
      header: 'Created',
      cell: (item: MultiDocJob) => {
        const d = parseUtcTimestamp(item.createdAt);
        if (!d) return '—';
        return d.toLocaleString(undefined, {
          month: 'short',
          day: 'numeric',
          hour: '2-digit',
          minute: '2-digit',
        });
      },
      sortingField: 'createdAt',
      width: 150,
    },
    {
      id: 'duration',
      header: 'Duration',
      cell: (item: MultiDocJob) =>
        formatElapsed(item.createdAt, ['COMPLETED', 'FAILED'].includes(item.status) ? item.updatedAt : undefined),
      width: 90,
    },
    {
      id: 'result',
      header: 'Result',
      cell: (item: MultiDocJob) => renderResultCell(item),
      minWidth: 250,
    },
    {
      id: 'jobId',
      header: 'Job ID',
      cell: (item: MultiDocJob) => (
        <Box fontSize="body-s" color="text-body-secondary">
          {item.jobId.substring(0, 12)}...
        </Box>
      ),
      width: 140,
    },
  ];

  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      <Alert type="warning" header="Important Notice">
        Use the Discovery feature in non-production environments to discover class models from documents and images. Discovery creates a
        starting point, not a final class model config. Be sure to inspect, test and refine the generated custom class configuration before
        exporting it to production.
      </Alert>

      <TextContent>
        <p>
          Upload a collection of single-class documents (PDF, PNG, JPG, TIFF) — each document should contain only one document type (not
          multi-section packets). The system will use AI to automatically identify document types by clustering similar documents together,
          then generate a JSON Schema for each discovered class. You can provide documents via an S3 path or upload a zip file.
          <strong> Requires at least 2 documents per expected class</strong> — clusters with fewer than 2 documents are filtered as noise.
        </p>
      </TextContent>

      {/* Start New Discovery */}
      <Container
        header={
          <Header
            variant="h2"
            description="Automatically discover document classes from a collection of documents. The pipeline will embed, cluster, and analyze your documents to generate JSON Schemas for each document type."
          >
            Start Multi-Document Discovery
          </Header>
        }
      >
        <SpaceBetween size="m">
          {/* Config Profile */}
          <FormField
            label="Configuration Profile"
            description="Discovered classes will be saved to this configuration profile, or create a new one"
          >
            <SpaceBetween size="xs" direction="horizontal" alignItems="end">
              <Select
                selectedOption={selectedVersion}
                onChange={({ detail }) => setSelectedVersion(detail.selectedOption)}
                options={getVersionOptions()}
                placeholder="Select a configuration profile"
                loadingText="Loading profiles..."
                statusType={versionsLoading ? 'loading' : 'finished'}
              />
              <Button iconName="add-plus" onClick={() => setShowCreateVersionModal(true)} disabled={starting}>
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

          {/* Save Mode */}
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
                  description:
                    'Keep the existing document classes and add/update discovered ones (classes with the same name are overwritten).',
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
              <strong>immediately, before discovery runs</strong>, then saves the newly discovered schema. If discovery fails, the profile
              is left with no classes. This cannot be undone — consider creating a new profile instead.
            </Alert>
          )}

          {/* Input Mode */}
          <FormField label="Document Source">
            <Tiles
              value={inputMode}
              onChange={({ detail }) => setInputMode(detail.value)}
              items={[
                {
                  value: 's3path',
                  label: 'S3 Path',
                  description: 'Point to documents already in S3',
                },
                {
                  value: 'zip',
                  label: 'Zip Upload',
                  description: 'Upload a zip file of documents',
                },
              ]}
            />
          </FormField>

          {/* S3 Path inputs */}
          {inputMode === 's3path' && (
            <ColumnLayout columns={2}>
              <FormField label="Source Bucket" description="Select the bucket containing your documents">
                <Select
                  selectedOption={selectedBucket}
                  onChange={({ detail }) => setSelectedBucket(detail.selectedOption)}
                  options={bucketOptions}
                  placeholder="Select a bucket"
                />
              </FormField>
              <FormField label="S3 Prefix" description="Folder prefix containing documents">
                <Input value={s3Prefix} onChange={({ detail }) => setS3Prefix(detail.value)} placeholder="documents/my-collection/" />
              </FormField>
            </ColumnLayout>
          )}

          {/* Zip Upload */}
          {inputMode === 'zip' && (
            <FormField
              label="Document Zip File"
              description="Upload a .zip containing PDF, PNG, JPG, TIFF, or WebP files (max 500 documents)"
            >
              <SpaceBetween size="xs" direction="horizontal">
                <input type="file" accept=".zip" onChange={(e) => setZipFile(e.target.files?.[0] || null)} style={{ fontSize: '14px' }} />
                {zipFile && (
                  <Badge color="green">
                    {zipFile.name} ({(zipFile.size / 1024 / 1024).toFixed(1)} MB)
                  </Badge>
                )}
              </SpaceBetween>
            </FormField>
          )}

          {/* Start Button */}
          <Box float="right">
            <Button
              variant="primary"
              onClick={handleStartDiscovery}
              loading={starting || uploading}
              disabled={
                !selectedVersion ||
                selectedVersion.value === 'default' ||
                (inputMode === 'zip' && !zipFile) ||
                (inputMode === 's3path' && !s3Prefix)
              }
            >
              {uploading ? 'Uploading...' : starting ? 'Starting...' : '🔍 Start Discovery'}
            </Button>
          </Box>
        </SpaceBetween>
      </Container>

      {/* Jobs Table */}
      <Table
        columnDefinitions={columnDefinitions}
        items={paginatedJobs}
        loading={loading}
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
        onSelectionChange={({ detail }) => setSelectedJobs(detail.selectedItems as MultiDocJob[])}
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
            <b>No multi-document discovery jobs found</b>
            <Box padding={{ bottom: 's' }} variant="p" color="inherit">
              {filterText
                ? 'No jobs match the current filter.'
                : 'Start a discovery above to automatically find document classes in your collection.'}
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
                <Button iconName="refresh" variant="icon" onClick={() => loadJobs()} loading={loading} ariaLabel="Refresh discovery jobs" />
                <Button
                  iconName="remove"
                  variant="icon"
                  onClick={handleDeleteJobs}
                  loading={isDeleting}
                  disabled={selectedJobs.length === 0}
                  ariaLabel="Delete selected discovery jobs"
                />
              </SpaceBetween>
            }
          >
            Multi-Document Discovery Jobs
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
                    { id: 'source', label: 'Source' },
                    { id: 'status', label: 'Status' },
                    { id: 'currentStep', label: 'Current Step' },
                    { id: 'totalDocuments', label: 'Documents' },
                    { id: 'clustersFound', label: 'Clusters' },
                    { id: 'version', label: 'Config Profile' },
                    { id: 'createdAt', label: 'Created' },
                    { id: 'duration', label: 'Duration' },
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

export default MultiDocDiscoveryPanel;
