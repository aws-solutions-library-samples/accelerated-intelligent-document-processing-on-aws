// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/* eslint-disable @typescript-eslint/no-explicit-any */

/**
 * Discovery Job Details Page
 *
 * Displays detailed results for a discovery job (single-doc or multi-doc).
 * Accessed via /documents/discovery/job/:jobId
 */

import React, { useState, useEffect, useCallback } from 'react';
import {
  Button,
  Container,
  Header,
  SpaceBetween,
  StatusIndicator,
  Alert,
  Box,
  ColumnLayout,
  ExpandableSection,
  Badge,
  BreadcrumbGroup,
  Link,
  Spinner,
} from '@cloudscape-design/components';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useParams, useNavigate } from 'react-router-dom';
import { generateClient } from '../../api/client-shim';

import { listDiscoveryJobs, onDiscoveryJobStatusChange } from '../../graphql/generated';
import { DISCOVERY_PATH, CONFIGURATION_PATH } from '../../routes/constants';
import useConfigurationVersions from '../../hooks/use-configuration-versions';
import { formatConfigVersionLink } from '../test-studio/utils/configVersionUtils';
import type { ConfigVersion } from '../test-studio/utils/configVersionUtils';

const client = generateClient();

// Pipeline steps for multi-doc progress display
const PIPELINE_STEPS = [
  { key: 'QUEUED', label: 'Queued' },
  { key: 'PREPARING', label: 'Listing Documents' },
  { key: 'EMBEDDING', label: 'Generating Embeddings' },
  { key: 'CLUSTERING', label: 'Clustering Documents' },
  { key: 'ANALYZING', label: 'Analyzing Clusters' },
  { key: 'COMPLETED', label: 'Complete' },
];

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
  jobType?: string;
  currentStep?: string;
  totalDocuments?: number;
  clustersFound?: number;
  discoveredClasses?: string;
  reflectionReport?: string;
}

interface DiscoveredClass {
  cluster_id: number;
  classification: string;
  json_schema: Record<string, unknown>;
  document_count: number;
  sample_doc_ids?: string[];
  error?: string;
}

function parseUtcTimestamp(ts?: string): Date | null {
  if (!ts) return null;
  const hasTimezone = /[Zz]$|[+-]\d{2}:\d{2}$|[+-]\d{4}$|[+-]\d{2}$/.test(ts);
  const normalized = hasTimezone ? ts : `${ts}Z`;
  const d = new Date(normalized);
  return isNaN(d.getTime()) ? null : d;
}

function formatDuration(startStr?: string, endStr?: string): string {
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

function parseDiscoveredClasses(json?: string): DiscoveredClass[] {
  if (!json) return [];
  try {
    return JSON.parse(json);
  } catch {
    return [];
  }
}

function getStepIndex(status: string): number {
  const idx = PIPELINE_STEPS.findIndex((s) => s.key === status);
  return idx >= 0 ? idx : 0;
}

/** Extract the original filename from the document key. */
function getOriginalFileName(documentKey: string | undefined): string {
  if (!documentKey) return '—';
  const fileName = documentKey.split('/').pop() || documentKey;
  const stripped = fileName.replace(/^\d{8}_\d{6}_/, '');
  return stripped || fileName;
}

const DiscoveryJobDetails = (): React.JSX.Element => {
  const { jobId } = useParams<{ jobId: string }>();
  const navigate = useNavigate();
  const { versions } = useConfigurationVersions();
  const [job, setJob] = useState<DiscoveryJob | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadJob = useCallback(async () => {
    if (!jobId) return;
    setLoading(true);
    try {
      const response = await client.graphql({ query: listDiscoveryJobs });
      const allJobs = (response as any)?.data?.listDiscoveryJobs?.DiscoveryJobs || [];
      const found = allJobs.find((j: any) => j.jobId === jobId);
      if (found) {
        setJob(found);
      } else {
        setError(`Job ${jobId} not found`);
      }
    } catch (err: any) {
      console.error('Failed to load job:', err);
      setError(`Failed to load job: ${err.message}`);
    } finally {
      setLoading(false);
    }
  }, [jobId]);

  // Load job on mount
  useEffect(() => {
    loadJob();
  }, [loadJob]);

  // Subscribe to updates for active jobs
  useEffect(() => {
    if (!job || ['COMPLETED', 'FAILED', 'OPTIMIZATION_COMPLETED', 'OPTIMIZATION_FAILED'].includes(job.status)) return;

    const observable = client.graphql({
      query: onDiscoveryJobStatusChange,
      variables: { jobId: job.jobId },
    });
    const sub = (observable as any).subscribe({
      next: ({ data }: any) => {
        const update = data?.onDiscoveryJobStatusChange;
        if (update) {
          setJob((prev) => (prev ? { ...prev, ...update } : prev));
        }
      },
      error: (err: any) => console.error('Subscription error:', err),
    });

    return () => sub.unsubscribe();
  }, [job?.jobId, job?.status]);

  // Auto-refresh for active jobs
  useEffect(() => {
    if (!job || ['COMPLETED', 'FAILED', 'OPTIMIZATION_COMPLETED', 'OPTIMIZATION_FAILED'].includes(job.status)) return;
    const timer = setInterval(loadJob, 10000);
    return () => clearInterval(timer);
  }, [job?.status, loadJob]);

  if (loading) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" />
        <Box margin={{ top: 's' }}>Loading job details...</Box>
      </Box>
    );
  }

  if (error || !job) {
    return (
      <SpaceBetween size="l">
        <BreadcrumbGroup
          items={[
            { text: 'Discovery', href: `#${DISCOVERY_PATH}` },
            { text: 'Job Details', href: '#' },
          ]}
        />
        <Alert type="error">{error || 'Job not found'}</Alert>
        <Button onClick={() => navigate(DISCOVERY_PATH)}>Back to Discovery</Button>
      </SpaceBetween>
    );
  }

  const isMultiDoc = job.jobType === 'multi-document';
  const isPolicyJob =
    job.jobType === 'rules' || (typeof job.discoveredClassName === 'string' && job.discoveredClassName.startsWith('policy_rules'));
  const isTerminal = ['COMPLETED', 'FAILED', 'OPTIMIZATION_COMPLETED', 'OPTIMIZATION_FAILED'].includes(job.status);
  const classes = isMultiDoc ? parseDiscoveredClasses(job.discoveredClasses) : [];

  const getStatusIndicator = () => {
    switch (job.status) {
      case 'COMPLETED':
        return <StatusIndicator type="success">Completed</StatusIndicator>;
      case 'OPTIMIZATION_COMPLETED':
        return <StatusIndicator type="success">Optimized</StatusIndicator>;
      case 'FAILED':
      case 'OPTIMIZATION_FAILED':
        return <StatusIndicator type="error">Failed</StatusIndicator>;
      case 'PENDING':
      case 'QUEUED':
        return <StatusIndicator type="pending">{job.status === 'QUEUED' ? 'Queued' : 'Pending'}</StatusIndicator>;
      default:
        return <StatusIndicator type="in-progress">{job.statusMessage || job.currentStep || 'In Progress'}</StatusIndicator>;
    }
  };

  /**
   * Build a link to the config editor for a given class.
   * Rules-discovery classes (whose name starts with "policy_rules") are saved
   * into `policy_classes`, which is rendered by the Policy Schema tab.
   * Regular discovery classes go to the Document Schema tab.
   */
  const getConfigLink = (className: string): string => {
    const params = new URLSearchParams();
    if (job.version) params.set('version', job.version);
    params.set('tab', isPolicyJob ? 'rule-schema' : 'extraction-schema');
    params.set('highlight', className);
    return `#${CONFIGURATION_PATH}?${params.toString()}`;
  };

  return (
    <SpaceBetween size="l">
      {/* Breadcrumbs */}
      <BreadcrumbGroup
        items={[
          { text: 'Discovery', href: `#${DISCOVERY_PATH}` },
          {
            text: isPolicyJob ? 'Policy Discovery Job' : isMultiDoc ? 'Multi-Document Job' : 'Single Document Job',
            href: '#',
          },
        ]}
      />

      {/* Job Summary Header */}
      <Container
        header={
          <Header
            variant="h1"
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button iconName="refresh" onClick={loadJob}>
                  Refresh
                </Button>
                <Button onClick={() => navigate(DISCOVERY_PATH)}>Back to Discovery</Button>
              </SpaceBetween>
            }
          >
            Discovery Job Details
          </Header>
        }
      >
        <ColumnLayout columns={isMultiDoc ? 4 : 3} variant="text-grid">
          <div>
            <Box variant="awsui-key-label">Status</Box>
            <Box>{getStatusIndicator()}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">{isMultiDoc ? 'Source' : 'Document'}</Box>
            <Box>
              {isMultiDoc ? job.documentKey || `${job.totalDocuments ?? '—'} documents` : getOriginalFileName(job.documentKey)}
              {job.pageRange && (
                <Box margin={{ left: 'xs' }} display="inline-block">
                  <Badge color="blue">pp {job.pageRange}</Badge>
                </Box>
              )}
            </Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Config Profile</Box>
            <Box>{formatConfigVersionLink(job.version, versions as unknown as ConfigVersion[])}</Box>
          </div>
          {isMultiDoc && (
            <div>
              <Box variant="awsui-key-label">Clusters Found</Box>
              <Box>{job.clustersFound ?? '—'}</Box>
            </div>
          )}
        </ColumnLayout>

        <Box margin={{ top: 'm' }}>
          <ColumnLayout columns={3} variant="text-grid">
            <div>
              <Box variant="awsui-key-label">Created</Box>
              <Box>
                {parseUtcTimestamp(job.createdAt)?.toLocaleString(undefined, {
                  month: 'short',
                  day: 'numeric',
                  hour: '2-digit',
                  minute: '2-digit',
                  second: '2-digit',
                }) ?? '—'}
              </Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Duration</Box>
              <Box>{formatDuration(job.createdAt, isTerminal ? job.updatedAt : undefined)}</Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Job ID</Box>
              <Box fontSize="body-s" color="text-body-secondary">
                {job.jobId}
              </Box>
            </div>
          </ColumnLayout>
        </Box>
      </Container>

      {/* Error Alert */}
      {job.errorMessage && (
        <Alert type="error" header="Error">
          {job.errorMessage}
        </Alert>
      )}

      {/* Multi-doc: Pipeline Progress */}
      {isMultiDoc && !isTerminal && (
        <Container header={<Header variant="h2">Pipeline Progress</Header>}>
          <Box padding={{ top: 'xs' }}>
            <SpaceBetween size="xxs" direction="horizontal">
              {PIPELINE_STEPS.map((step, idx) => {
                const currentIdx = getStepIndex(job.currentStep || job.status);
                const isFailed = job.status === 'FAILED';
                let type: 'success' | 'error' | 'in-progress' | 'pending' = 'pending';
                if (isFailed && idx === currentIdx) type = 'error';
                else if (idx < currentIdx) type = 'success';
                else if (idx === currentIdx) type = job.status === 'COMPLETED' ? 'success' : 'in-progress';

                return (
                  <Box key={step.key} textAlign="center" display="inline-block" margin={{ right: 'l' }}>
                    <StatusIndicator type={type}>{step.label}</StatusIndicator>
                  </Box>
                );
              })}
            </SpaceBetween>
          </Box>
        </Container>
      )}

      {/* Discovered Classes */}
      {isMultiDoc && classes.length > 0 && (
        <Container
          header={
            <Header variant="h2" counter={`(${classes.length})`}>
              Discovered Classes
            </Header>
          }
        >
          <SpaceBetween size="m">
            {classes.map((dc) => (
              <Container
                key={dc.cluster_id}
                header={
                  <Header
                    variant="h3"
                    description={`Cluster ${dc.cluster_id} — ${dc.document_count} documents`}
                    actions={
                      !dc.error ? (
                        <Link href={getConfigLink(dc.classification)} external={false}>
                          View in Configuration →
                        </Link>
                      ) : undefined
                    }
                  >
                    <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                      {dc.error ? (
                        <StatusIndicator type="error">{dc.classification || `Cluster ${dc.cluster_id}`}</StatusIndicator>
                      ) : (
                        <StatusIndicator type="success">{dc.classification || `Cluster ${dc.cluster_id}`}</StatusIndicator>
                      )}
                    </SpaceBetween>
                  </Header>
                }
              >
                {dc.error ? (
                  <Alert type="error">{dc.error}</Alert>
                ) : (
                  <ExpandableSection headerText="View JSON Schema" variant="footer" defaultExpanded={false}>
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
        </Container>
      )}

      {/* Single-doc: Discovered Class */}
      {!isMultiDoc && job.status === 'COMPLETED' && job.discoveredClassName && (
        <Container
          header={
            <Header
              variant="h2"
              actions={
                <Link href={getConfigLink(job.discoveredClassName)} external={false}>
                  View in Configuration →
                </Link>
              }
            >
              Discovered Class
            </Header>
          }
        >
          <SpaceBetween size="s">
            <Box>
              <Badge color="green">{job.discoveredClassName}</Badge>
            </Box>
            {job.statusMessage && (
              <Box fontSize="body-s" color="text-body-secondary">
                {job.statusMessage}
              </Box>
            )}
          </SpaceBetween>
        </Container>
      )}

      {/* Single-doc: Optimization result */}
      {!isMultiDoc && (job.status === 'OPTIMIZATION_COMPLETED' || job.status === 'OPTIMIZATION_FAILED') && (
        <Container header={<Header variant="h2">Optimization Result</Header>}>
          <SpaceBetween size="s">
            {job.discoveredClassName && <Badge color="green">{job.discoveredClassName}</Badge>}
            <Box fontSize="body-s" color="text-body-secondary">
              {job.statusMessage || (job.status === 'OPTIMIZATION_COMPLETED' ? 'Blueprint optimization completed' : 'Optimization failed')}
            </Box>
          </SpaceBetween>
        </Container>
      )}

      {/* Multi-doc: Quality Review Report */}
      {isMultiDoc && job.reflectionReport && (
        <Container header={<Header variant="h2">Quality Review Report</Header>}>
          <div style={{ maxHeight: '600px', overflow: 'auto', padding: '16px' }}>
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{job.reflectionReport}</ReactMarkdown>
          </div>
        </Container>
      )}

      {/* In-progress status message */}
      {!isTerminal && job.statusMessage && (
        <Container header={<Header variant="h2">Current Status</Header>}>
          <StatusIndicator type="in-progress">{job.statusMessage}</StatusIndicator>
        </Container>
      )}
    </SpaceBetween>
  );
};

export default DiscoveryJobDetails;
