// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useState, useEffect, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { generateClient } from '../../api/client-shim';
import {
  Container,
  Header,
  SpaceBetween,
  Box,
  Button,
  Badge,
  Flashbar,
  ColumnLayout,
  StatusIndicator,
  Spinner,
  Alert,
  KeyValuePairs,
  BreadcrumbGroup,
  ContentLayout,
  Tabs,
  Modal,
} from '@cloudscape-design/components';
import CreateConfigVersionModal from './CreateConfigVersionModal';

// Types
interface FinetuningJob {
  jobId: string;
  jobName: string;
  status: string;
  baseModelId: string;
  customModelName?: string;
  customModelArn?: string;
  customModelDeploymentArn?: string;
  testSetId: string;
  testSetName?: string;
  createdAt: string;
  updatedAt?: string;
  completedAt?: string;
  errorMessage?: string;
  trainingMetrics?: string;
  hyperparameters?: string;
  trainingDataConfig?: string;
  validationDataConfig?: string;
  outputDataConfig?: string;
  deploymentId?: string;
  deploymentStatus?: string;
  deploymentEndpoint?: string;
  provisionedModelArn?: string;
}

interface Notification {
  id: string;
  type: 'success' | 'error' | 'warning' | 'info';
  content: React.ReactNode;
  header?: string;
  dismissible: boolean;
  onDismiss: () => void;
}

// Base models that support fine-tuning (Nova 2.x + legacy v1 for display)
const SUPPORTED_BASE_MODELS: Record<string, string> = {
  'us.amazon.nova-2-pro-v1:0': 'Amazon Nova 2 Pro',
  'us.amazon.nova-2-lite-v1:0': 'Amazon Nova 2 Lite',
  'us.amazon.nova-pro-v1:0': 'Amazon Nova Pro (v1)',
  'us.amazon.nova-lite-v1:0': 'Amazon Nova Lite (v1)',
};

// Status badge colors
const getStatusBadgeColor = (status: string): 'blue' | 'green' | 'red' | 'grey' => {
  switch (status?.toUpperCase()) {
    case 'COMPLETED':
    case 'DEPLOYED':
    case 'IN_SERVICE':
      return 'green';
    case 'IN_PROGRESS':
    case 'TRAINING':
    case 'CREATING':
    case 'DEPLOYING':
    case 'GENERATING_DATA':
    case 'VALIDATING':
      return 'blue';
    case 'FAILED':
    case 'ERROR':
      return 'red';
    default:
      return 'grey';
  }
};

const getStatusIndicatorType = (status: string): 'success' | 'error' | 'in-progress' | 'pending' | 'stopped' => {
  switch (status?.toUpperCase()) {
    case 'COMPLETED':
    case 'DEPLOYED':
    case 'IN_SERVICE':
      return 'success';
    case 'IN_PROGRESS':
    case 'TRAINING':
    case 'CREATING':
    case 'DEPLOYING':
    case 'GENERATING_DATA':
    case 'VALIDATING':
      return 'in-progress';
    case 'FAILED':
    case 'ERROR':
      return 'error';
    case 'STOPPED':
    case 'CANCELLED':
      return 'stopped';
    default:
      return 'pending';
  }
};

const FinetuningJobDetail = (): React.JSX.Element => {
  const { jobId } = useParams<{ jobId: string }>();
  const navigate = useNavigate();
  const [job, setJob] = useState<FinetuningJob | null>(null);
  const [loading, setLoading] = useState(true);
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [activeTab, setActiveTab] = useState('overview');
  const [showCreateConfigModal, setShowCreateConfigModal] = useState(false);
  const [showDeleteModal, setShowDeleteModal] = useState(false);

  // Helper function to add notifications
  const addNotification = useCallback((type: 'success' | 'error' | 'warning' | 'info', content: React.ReactNode, header?: string) => {
    const id = Date.now().toString();
    const notification: Notification = {
      id,
      type,
      content,
      dismissible: true,
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
  }, []);

  // Fetch job details
  const fetchJobDetails = useCallback(async () => {
    if (!jobId) return;

    setLoading(true);
    try {
      const client = generateClient();
      const response = (await client.graphql({
        query: `
          query GetFinetuningJob($jobId: ID!) {
            getFinetuningJob(jobId: $jobId) {
              jobId
              jobName
              status
              baseModelId
              customModelName
              customModelArn
              customModelDeploymentArn
              testSetId
              testSetName
              createdAt
              updatedAt
              completedAt
              errorMessage
              trainingMetrics
              hyperparameters
              trainingDataConfig
              validationDataConfig
              outputDataConfig
              deploymentId
              deploymentStatus
              deploymentEndpoint
              provisionedModelArn
            }
          }
        `,
        variables: { jobId },
      })) as { data: { getFinetuningJob?: FinetuningJob } };

      const data = response.data;
      if (data?.getFinetuningJob) {
        setJob(data.getFinetuningJob);
      } else {
        addNotification('error', 'Fine-tuning job not found.', 'Error');
      }
    } catch (error) {
      console.error('Error fetching fine-tuning job:', error);
      addNotification('error', 'Failed to fetch fine-tuning job details.', 'Error');
    } finally {
      setLoading(false);
    }
  }, [jobId, addNotification]);

  // Delete job
  const handleDeleteJob = async () => {
    if (!jobId) return;

    try {
      const client = generateClient();
      await client.graphql({
        query: `
          mutation DeleteFinetuningJob($jobId: ID!) {
            deleteFinetuningJob(jobId: $jobId)
          }
        `,
        variables: { jobId },
      });

      addNotification('success', 'Fine-tuning job deleted successfully.', 'Job Deleted');
      navigate('/documents/custom-models');
    } catch (error) {
      console.error('Error deleting fine-tuning job:', error);
      addNotification('error', 'Failed to delete fine-tuning job.', 'Error');
    }
  };

  // Initial fetch
  useEffect(() => {
    fetchJobDetails();
  }, [fetchJobDetails]);

  // Auto-refresh for in-progress jobs
  useEffect(() => {
    if (job && ['IN_PROGRESS', 'TRAINING', 'CREATING', 'DEPLOYING', 'GENERATING_DATA', 'VALIDATING'].includes(job.status?.toUpperCase())) {
      const interval = setInterval(fetchJobDetails, 30000); // Refresh every 30 seconds
      return () => clearInterval(interval);
    }
    return undefined;
  }, [job, fetchJobDetails]);

  // Format date
  const formatDate = (dateString?: string) => {
    if (!dateString) return '-';
    return new Date(dateString).toLocaleString();
  };

  // Get model display name
  const getModelDisplayName = (modelId: string) => {
    return SUPPORTED_BASE_MODELS[modelId] || modelId;
  };

  // Parse JSON safely
  const parseJson = (jsonString?: string) => {
    if (!jsonString) return null;
    try {
      return JSON.parse(jsonString);
    } catch {
      return null;
    }
  };

  if (loading) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" />
        <Box variant="p" margin={{ top: 's' }}>
          Loading fine-tuning job details...
        </Box>
      </Box>
    );
  }

  if (!job) {
    return (
      <ContentLayout
        header={
          <Header
            variant="h1"
            actions={
              <Button variant="normal" onClick={() => navigate('/documents/custom-models')}>
                Back to Custom Models
              </Button>
            }
          >
            Fine-tuning Job Not Found
          </Header>
        }
      >
        <Alert type="error">
          The fine-tuning job with ID &quot;{jobId}&quot; was not found. It may have been deleted or the ID is incorrect.
        </Alert>
      </ContentLayout>
    );
  }

  const hyperparameters = parseJson(job.hyperparameters);

  return (
    <ContentLayout
      header={
        <SpaceBetween size="m">
          <BreadcrumbGroup
            items={[
              { text: 'Documents', href: '#/documents' },
              { text: 'Custom Models', href: '#/documents/custom-models' },
              { text: job.jobName, href: `#/documents/custom-models/${job.jobId}` },
            ]}
          />
          <Header
            variant="h1"
            actions={
              <SpaceBetween direction="horizontal" size="s">
                <Button iconName="refresh" onClick={fetchJobDetails}>
                  Refresh
                </Button>
                <Button
                  variant="normal"
                  onClick={() => setShowDeleteModal(true)}
                  disabled={['IN_PROGRESS', 'TRAINING', 'CREATING', 'DEPLOYING'].includes(job.status?.toUpperCase())}
                >
                  Delete
                </Button>
              </SpaceBetween>
            }
          >
            {job.jobName} <Badge color={getStatusBadgeColor(job.status)}>{job.status}</Badge>
          </Header>
        </SpaceBetween>
      }
    >
      <SpaceBetween size="l">
        {/* Notifications */}
        {notifications.length > 0 && (
          <Flashbar items={notifications as unknown as React.ComponentProps<typeof Flashbar>['items']} stackItems />
        )}

        {/* Error Alert */}
        {job.errorMessage && (
          <Alert type="error" header="Job Failed">
            {job.errorMessage}
          </Alert>
        )}

        {/* Status Alert for in-progress jobs */}
        {['IN_PROGRESS', 'TRAINING', 'CREATING', 'DEPLOYING', 'GENERATING_DATA', 'VALIDATING'].includes(job.status?.toUpperCase()) && (
          <Alert type="info" header="Job In Progress">
            This fine-tuning job is currently running. The page will automatically refresh every 30 seconds.
          </Alert>
        )}

        {/* Tabs */}
        <Tabs
          activeTabId={activeTab}
          onChange={({ detail }) => setActiveTab(detail.activeTabId)}
          tabs={[
            {
              id: 'overview',
              label: 'Overview',
              content: (
                <SpaceBetween size="l">
                  {/* Job Details */}
                  <Container header={<Header variant="h2">Job Details</Header>}>
                    <ColumnLayout columns={3} variant="text-grid">
                      <div>
                        <Box variant="awsui-key-label">Job ID</Box>
                        <div>{job.jobId}</div>
                      </div>
                      <div>
                        <Box variant="awsui-key-label">Job Name</Box>
                        <div>{job.jobName}</div>
                      </div>
                      <div>
                        <Box variant="awsui-key-label">Status</Box>
                        <StatusIndicator type={getStatusIndicatorType(job.status)}>{job.status}</StatusIndicator>
                      </div>
                      <div>
                        <Box variant="awsui-key-label">Base Model</Box>
                        <div>{getModelDisplayName(job.baseModelId)}</div>
                      </div>
                      <div>
                        <Box variant="awsui-key-label">Training Data</Box>
                        <div>{job.testSetName || job.testSetId}</div>
                      </div>
                      <div>
                        <Box variant="awsui-key-label">Custom Model Name</Box>
                        <div>{job.customModelName || '-'}</div>
                      </div>
                      <div>
                        <Box variant="awsui-key-label">Created</Box>
                        <div>{formatDate(job.createdAt)}</div>
                      </div>
                      <div>
                        <Box variant="awsui-key-label">Last Updated</Box>
                        <div>{formatDate(job.updatedAt)}</div>
                      </div>
                      <div>
                        <Box variant="awsui-key-label">Completed</Box>
                        <div>{formatDate(job.completedAt)}</div>
                      </div>
                    </ColumnLayout>
                  </Container>

                  {/* Custom Model Info (if completed) */}
                  {job.customModelArn && (
                    <Container header={<Header variant="h2">Custom Model</Header>}>
                      <ColumnLayout columns={2} variant="text-grid">
                        <div>
                          <Box variant="awsui-key-label">Custom Model ARN</Box>
                          <div style={{ wordBreak: 'break-all' }}>{job.customModelArn}</div>
                        </div>
                        <div>
                          <Box variant="awsui-key-label">Custom Model Name</Box>
                          <div>{job.customModelName || '-'}</div>
                        </div>
                      </ColumnLayout>
                    </Container>
                  )}

                  {/* On-Demand Deployment Info */}
                  {(job.customModelDeploymentArn || job.status === 'DEPLOYING' || job.status === 'COMPLETED') && (
                    <Container header={<Header variant="h2">On-Demand Deployment</Header>}>
                      <ColumnLayout columns={2} variant="text-grid">
                        <div>
                          <Box variant="awsui-key-label">Deployment Status</Box>
                          {job.customModelDeploymentArn ? (
                            <StatusIndicator type="success">Deployed</StatusIndicator>
                          ) : job.status === 'DEPLOYING' ? (
                            <StatusIndicator type="in-progress">Deploying</StatusIndicator>
                          ) : (
                            <StatusIndicator type="pending">Pending</StatusIndicator>
                          )}
                        </div>
                        <div>
                          <Box variant="awsui-key-label">Deployment Type</Box>
                          <div>On-Demand (pay-per-token)</div>
                        </div>
                        {job.customModelDeploymentArn && (
                          <>
                            <div>
                              <Box variant="awsui-key-label">Custom Model Deployment ARN</Box>
                              <div style={{ wordBreak: 'break-all' }}>
                                <code>{job.customModelDeploymentArn}</code>
                              </div>
                            </div>
                            <div>
                              <Box variant="awsui-key-label">Deployment Name</Box>
                              <div>{job.customModelDeploymentArn.split('/').pop() || '-'}</div>
                            </div>
                          </>
                        )}
                      </ColumnLayout>
                      {job.customModelDeploymentArn && (
                        <Box margin={{ top: 'm' }}>
                          <SpaceBetween size="m">
                            <Alert type="info">
                              <strong>Using this model:</strong> You can invoke this custom model using the deployment ARN above with the
                              Bedrock InvokeModel API. The model is billed on-demand (pay-per-token) with no hourly charges.
                            </Alert>
                            <Button variant="primary" iconName="add-plus" onClick={() => setShowCreateConfigModal(true)}>
                              Create Config Profile
                            </Button>
                          </SpaceBetween>
                        </Box>
                      )}
                    </Container>
                  )}
                </SpaceBetween>
              ),
            },
            {
              id: 'config',
              label: 'Configuration',
              content: (
                <SpaceBetween size="l">
                  <Container header={<Header variant="h2">Hyperparameters</Header>}>
                    {hyperparameters ? (
                      <KeyValuePairs
                        columns={3}
                        items={Object.entries(hyperparameters).map(([key, value]) => ({
                          label: key,
                          value: String(value),
                        }))}
                      />
                    ) : (
                      <Box textAlign="center" color="text-status-inactive" padding="l">
                        No hyperparameters configured.
                      </Box>
                    )}
                  </Container>

                  <Container header={<Header variant="h2">Data Configuration</Header>}>
                    <ColumnLayout columns={2} variant="text-grid">
                      <div>
                        <Box variant="awsui-key-label">Training Data Config</Box>
                        <div style={{ wordBreak: 'break-all' }}>{job.trainingDataConfig || '-'}</div>
                      </div>
                      <div>
                        <Box variant="awsui-key-label">Validation Data Config</Box>
                        <div style={{ wordBreak: 'break-all' }}>{job.validationDataConfig || '-'}</div>
                      </div>
                      <div>
                        <Box variant="awsui-key-label">Output Data Config</Box>
                        <div style={{ wordBreak: 'break-all' }}>{job.outputDataConfig || '-'}</div>
                      </div>
                    </ColumnLayout>
                  </Container>
                </SpaceBetween>
              ),
            },
          ]}
        />

        {/* Delete Confirmation Modal */}
        <Modal
          visible={showDeleteModal}
          onDismiss={() => setShowDeleteModal(false)}
          header="Delete Fine-tuning Job"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="link" onClick={() => setShowDeleteModal(false)}>
                  Cancel
                </Button>
                <Button
                  variant="primary"
                  onClick={async () => {
                    setShowDeleteModal(false);
                    await handleDeleteJob();
                  }}
                >
                  Delete
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="m">
            <Box>Are you sure you want to delete the fine-tuning job &quot;{job.jobName}&quot;?</Box>
            <Alert type="warning">
              This action cannot be undone. Any associated custom model deployments will also be deleted. Fine-tuning jobs can take hours
              and incur costs — please confirm this is intentional.
            </Alert>
          </SpaceBetween>
        </Modal>

        {/* Create Config Profile Modal */}
        {job.customModelDeploymentArn && (
          <CreateConfigVersionModal
            visible={showCreateConfigModal}
            onDismiss={() => setShowCreateConfigModal(false)}
            deploymentArn={job.customModelDeploymentArn}
            jobName={job.jobName}
            onSuccess={(versionName) => {
              setShowCreateConfigModal(false);
              addNotification(
                'success',
                <>
                  Configuration profile <strong>{versionName}</strong> created successfully with the custom model deployment ARN. You can
                  view and edit it in the{' '}
                  <a href="#/configuration" style={{ color: 'inherit' }}>
                    Configuration
                  </a>{' '}
                  page.
                </>,
                'Config Profile Created',
              );
            }}
          />
        )}
      </SpaceBetween>
    </ContentLayout>
  );
};

export default FinetuningJobDetail;
