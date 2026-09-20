// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useState, useEffect, useCallback } from 'react';
import { generateClient } from '../../api/client-shim';
import {
  Container,
  Header,
  SpaceBetween,
  Box,
  Button,
  Table,
  Alert,
  Badge,
  Flashbar,
  Modal,
  FormField,
  Input,
  Select,
  Link,
  Pagination,
} from '@cloudscape-design/components';

// Types
interface FinetuningJob {
  jobId: string;
  jobName: string;
  status: string;
  baseModelId: string;
  customModelName: string;
  customModelArn?: string;
  customModelDeploymentArn?: string;
  testSetId: string;
  testSetName: string;
  createdAt: string;
  updatedAt?: string;
  completedAt?: string;
  errorMessage?: string;
  trainingMetrics?: string;
}

interface TestSet {
  id: string;
  name: string;
  fileCount: number;
}

interface Notification {
  id: string;
  type: 'success' | 'error' | 'warning' | 'info';
  content: React.ReactNode;
  header?: string;
  dismissible: boolean;
  onDismiss: () => void;
}

interface SelectOption {
  label: string;
  value: string;
  description?: string;
}

// Base models that support fine-tuning (Nova 2.x recommended)
const SUPPORTED_BASE_MODELS: SelectOption[] = [
  { label: 'Amazon Nova 2 Pro', value: 'us.amazon.nova-2-pro-v1:0', description: 'High-performance model for complex tasks' },
  { label: 'Amazon Nova 2 Lite', value: 'us.amazon.nova-2-lite-v1:0', description: 'Balanced performance and cost' },
];

// Status badge colors
const getStatusBadgeColor = (status: string): 'blue' | 'green' | 'red' | 'grey' => {
  switch (status?.toUpperCase()) {
    case 'COMPLETED':
      return 'green';
    case 'IN_PROGRESS':
    case 'TRAINING':
    case 'CREATING':
      return 'blue';
    case 'FAILED':
    case 'ERROR':
      return 'red';
    default:
      return 'grey';
  }
};

const CustomModelsLayout = (): React.JSX.Element => {
  const [jobs, setJobs] = useState<FinetuningJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedJobs, setSelectedJobs] = useState<FinetuningJob[]>([]);
  const [notifications, setNotifications] = useState<Notification[]>([]);

  // Create job modal state
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [createLoading, setCreateLoading] = useState(false);
  const [newJobName, setNewJobName] = useState('');
  const [selectedBaseModel, setSelectedBaseModel] = useState<SelectOption | null>(null);
  const [selectedTestSet, setSelectedTestSet] = useState<SelectOption | null>(null);
  const [testSets, setTestSets] = useState<TestSet[]>([]);
  const [testSetsLoading, setTestSetsLoading] = useState(false);

  // Delete confirmation modal state
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [jobsToDelete, setJobsToDelete] = useState<FinetuningJob[]>([]);

  // Pagination
  const [currentPage, setCurrentPage] = useState(1);
  const pageSize = 10;

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

  /**
   * How many pages of fine-tuning jobs to follow before giving up and saying so.
   *
   * The resolver bounds each call by read capacity, so a deployment with a long document
   * history needs several round trips. A ceiling here keeps a pathological table from
   * turning one page load into an unbounded request loop; reaching it surfaces a
   * "Partial list" notice rather than silently showing a truncated table.
   */
  const MAX_JOB_PAGES = 10;

  // Fetch fine-tuning jobs.
  //
  // Paged rather than fetched once. `listFinetuningJobs` scans a table that holds a
  // row per document in the deployment, so the resolver stops on a read-capacity
  // budget and returns a `nextToken` plus `complete: false`. Asking for the token and
  // discarding it would show a partial list with no error and no indication — so the
  // token is followed, and if the pages run out before the answer does the partial
  // state is surfaced rather than hidden.
  const fetchJobs = useCallback(async () => {
    setLoading(true);
    try {
      const client = generateClient();
      const collected: FinetuningJob[] = [];
      let token: string | null = null;
      let pages = 0;
      let complete = true;
      do {
        const response = (await client.graphql({
          query: `
          query ListFinetuningJobs($limit: Int, $nextToken: String) {
            listFinetuningJobs(limit: $limit, nextToken: $nextToken) {
              items {
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
              }
              nextToken
              complete
            }
          }
        `,
          variables: { limit: 100, nextToken: token },
        })) as {
          data: {
            listFinetuningJobs?: { items: FinetuningJob[]; nextToken?: string | null; complete?: boolean | null };
          };
        };

        const page = response.data?.listFinetuningJobs;
        if (page?.items) {
          collected.push(...page.items);
        }
        token = page?.nextToken ?? null;
        complete = page?.complete ?? true;
        pages += 1;
      } while (token && pages < MAX_JOB_PAGES);

      setJobs(collected);
      // Only when the server still had more AND we stopped asking. A server-reported
      // partial page whose token we followed to the end is a complete answer.
      if (token && !complete) {
        addNotification(
          'warning',
          `Showing the first ${collected.length} fine-tuning jobs. There are more than this page can load.`,
          'Partial list',
        );
      }
    } catch (error) {
      console.error('Error fetching fine-tuning jobs:', error);
      addNotification('error', 'Failed to fetch fine-tuning jobs. Please try again.', 'Error');
    } finally {
      setLoading(false);
    }
  }, [addNotification]);

  // Fetch test sets for the create modal
  const fetchTestSets = useCallback(async () => {
    setTestSetsLoading(true);
    try {
      const client = generateClient();
      const response = (await client.graphql({
        query: `
          query GetTestSets {
            getTestSets {
              id
              name
              fileCount
            }
          }
        `,
      })) as { data: { getTestSets?: TestSet[] } };

      const data = response.data;
      if (data?.getTestSets) {
        setTestSets(data.getTestSets);
      }
    } catch (error) {
      console.error('Error fetching test sets:', error);
      addNotification('error', 'Failed to fetch test sets.', 'Error');
    } finally {
      setTestSetsLoading(false);
    }
  }, [addNotification]);

  // Create fine-tuning job
  const handleCreateJob = async () => {
    if (!newJobName || !selectedBaseModel || !selectedTestSet) {
      addNotification('warning', 'Please fill in all required fields.', 'Validation Error');
      return;
    }

    setCreateLoading(true);
    try {
      const client = generateClient();
      await client.graphql({
        query: `
          mutation CreateFinetuningJob($input: CreateFinetuningJobInput!) {
            createFinetuningJob(input: $input) {
              jobId
              jobName
              status
            }
          }
        `,
        variables: {
          input: {
            jobName: newJobName,
            baseModel: selectedBaseModel.value,
            testSetId: selectedTestSet.value,
          },
        },
      });

      addNotification('success', `Fine-tuning job "${newJobName}" created successfully.`, 'Job Created');
      setShowCreateModal(false);
      setNewJobName('');
      setSelectedBaseModel(null);
      setSelectedTestSet(null);
      fetchJobs();
    } catch (error) {
      console.error('Error creating fine-tuning job:', error);
      addNotification('error', 'Failed to create fine-tuning job. Please try again.', 'Error');
    } finally {
      setCreateLoading(false);
    }
  };

  // Delete fine-tuning job
  const handleDeleteJob = async (jobId: string) => {
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
      fetchJobs();
    } catch (error) {
      console.error('Error deleting fine-tuning job:', error);
      addNotification('error', 'Failed to delete fine-tuning job.', 'Error');
    }
  };

  // Initial fetch
  useEffect(() => {
    fetchJobs();
  }, [fetchJobs]);

  // Fetch test sets when create modal opens
  useEffect(() => {
    if (showCreateModal) {
      fetchTestSets();
    }
  }, [showCreateModal, fetchTestSets]);

  // Pagination
  const paginatedJobs = jobs.slice((currentPage - 1) * pageSize, currentPage * pageSize);
  const totalPages = Math.ceil(jobs.length / pageSize);

  // Format date
  const formatDate = (dateString?: string) => {
    if (!dateString) return '-';
    return new Date(dateString).toLocaleString();
  };

  // Get model display name
  const getModelDisplayName = (modelId: string) => {
    const model = SUPPORTED_BASE_MODELS.find((m) => m.value === modelId);
    return model?.label || modelId;
  };

  return (
    <SpaceBetween size="l">
      {/* Notifications */}
      {notifications.length > 0 && (
        <Flashbar items={notifications as unknown as React.ComponentProps<typeof Flashbar>['items']} stackItems />
      )}

      {/* Header */}
      <Header
        variant="h1"
        description="Fine-tune Amazon Nova models using your validated document data from Test Studio"
        actions={
          <SpaceBetween direction="horizontal" size="s">
            <Button iconName="refresh" onClick={fetchJobs} loading={loading}>
              Refresh
            </Button>
            <Button variant="primary" iconName="add-plus" onClick={() => setShowCreateModal(true)}>
              Create Fine-tuning Job
            </Button>
          </SpaceBetween>
        }
      >
        Custom Models <Badge color="blue">Preview</Badge>
      </Header>

      {/* Info Alert */}
      <Alert type="info">
        <strong>Custom Model Fine-tuning:</strong> Create fine-tuned versions of Amazon Nova models using your validated document extraction
        data from Test Studio. Fine-tuned models can improve extraction accuracy for your specific document types. Once training completes,
        the custom model is automatically deployed and available for on-demand inference (pay-per-token).
        <Box margin={{ top: 'xs' }}>
          <Link href="/docs/custom-model-finetuning.md" external>
            Learn more about custom model fine-tuning
          </Link>
        </Box>
      </Alert>

      {/* Jobs Table */}
      <Container>
        <Table
          columnDefinitions={[
            {
              id: 'jobName',
              header: 'Job Name',
              cell: (item) => <Link href={`#/documents/custom-models/${item.jobId}`}>{item.jobName}</Link>,
              sortingField: 'jobName',
            },
            {
              id: 'status',
              header: 'Status',
              cell: (item) => <Badge color={getStatusBadgeColor(item.status)}>{item.status}</Badge>,
              sortingField: 'status',
            },
            {
              id: 'baseModel',
              header: 'Base Model',
              cell: (item) => getModelDisplayName(item.baseModelId),
              sortingField: 'baseModelId',
            },
            {
              id: 'deployment',
              header: 'On-Demand Deployment',
              cell: (item) => {
                if (item.customModelDeploymentArn) {
                  return (
                    <SpaceBetween direction="vertical" size="xxxs">
                      <Badge color="green">Deployed</Badge>
                      <code style={{ fontSize: '0.75em', wordBreak: 'break-all' }}>{item.customModelDeploymentArn}</code>
                    </SpaceBetween>
                  );
                }
                if (item.status === 'DEPLOYING') {
                  return <Badge color="blue">Deploying...</Badge>;
                }
                if (item.status === 'COMPLETED' && !item.customModelDeploymentArn) {
                  return <Badge color="grey">Pending</Badge>;
                }
                return '-';
              },
            },
            {
              id: 'testSet',
              header: 'Training Data',
              cell: (item) => item.testSetName || item.testSetId,
              sortingField: 'testSetName',
            },
            {
              id: 'createdAt',
              header: 'Created',
              cell: (item) => formatDate(item.createdAt),
              sortingField: 'createdAt',
            },
            {
              id: 'actions',
              header: 'Actions',
              cell: (item) => (
                <SpaceBetween direction="horizontal" size="xs">
                  <Button
                    variant="icon"
                    iconName="remove"
                    onClick={() => {
                      setJobsToDelete([item]);
                      setShowDeleteModal(true);
                    }}
                    disabled={item.status === 'IN_PROGRESS' || item.status === 'TRAINING'}
                    ariaLabel="Delete job"
                  />
                </SpaceBetween>
              ),
            },
          ]}
          items={paginatedJobs}
          loading={loading}
          loadingText="Loading fine-tuning jobs..."
          selectionType="multi"
          selectedItems={selectedJobs}
          onSelectionChange={({ detail }) => setSelectedJobs(detail.selectedItems)}
          empty={
            <Box textAlign="center" color="inherit">
              <SpaceBetween size="m">
                <b>No fine-tuning jobs</b>
                <Box variant="p" color="inherit">
                  Create a fine-tuning job to get started with custom models.
                </Box>
                <Button variant="primary" onClick={() => setShowCreateModal(true)}>
                  Create Fine-tuning Job
                </Button>
              </SpaceBetween>
            </Box>
          }
          header={
            <Header
              counter={`(${jobs.length})`}
              actions={
                selectedJobs.length > 0 && (
                  <Button
                    variant="normal"
                    onClick={() => {
                      setJobsToDelete(selectedJobs);
                      setShowDeleteModal(true);
                    }}
                  >
                    Delete Selected
                  </Button>
                )
              }
            >
              Fine-tuning Jobs
            </Header>
          }
          pagination={
            totalPages > 1 && (
              <Pagination
                currentPageIndex={currentPage}
                pagesCount={totalPages}
                onChange={({ detail }) => setCurrentPage(detail.currentPageIndex)}
              />
            )
          }
        />
      </Container>

      {/* Delete Confirmation Modal */}
      <Modal
        visible={showDeleteModal}
        onDismiss={() => setShowDeleteModal(false)}
        header="Delete Fine-tuning Job(s)"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShowDeleteModal(false)}>
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={async () => {
                  for (const job of jobsToDelete) {
                    await handleDeleteJob(job.jobId);
                  }
                  setShowDeleteModal(false);
                  setJobsToDelete([]);
                  setSelectedJobs([]);
                }}
              >
                Delete
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Box>
            Are you sure you want to delete{' '}
            {jobsToDelete.length === 1 ? `the fine-tuning job "${jobsToDelete[0]?.jobName}"` : `${jobsToDelete.length} fine-tuning jobs`}?
          </Box>
          <Alert type="warning">
            This action cannot be undone. Any associated custom model deployments will also be deleted. Fine-tuning jobs can take hours and
            incur costs — please confirm this is intentional.
          </Alert>
        </SpaceBetween>
      </Modal>

      {/* Create Job Modal */}
      <Modal
        visible={showCreateModal}
        onDismiss={() => setShowCreateModal(false)}
        header="Create Fine-tuning Job"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShowCreateModal(false)}>
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleCreateJob}
                loading={createLoading}
                disabled={!newJobName || !selectedBaseModel || !selectedTestSet}
              >
                Create Job
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="l">
          <FormField label="Job Name" description="A unique name for this fine-tuning job">
            <Input value={newJobName} onChange={({ detail }) => setNewJobName(detail.value)} placeholder="e.g., invoice-extraction-v1" />
          </FormField>

          <FormField label="Base Model" description="The Amazon Nova model to fine-tune">
            <Select
              selectedOption={selectedBaseModel}
              onChange={({ detail }) => setSelectedBaseModel(detail.selectedOption as SelectOption)}
              options={SUPPORTED_BASE_MODELS}
              placeholder="Select a base model"
            />
          </FormField>

          <FormField
            label="Training Data (Test Set)"
            description="Select a test set with validated extraction results to use as training data"
          >
            <Select
              selectedOption={selectedTestSet}
              onChange={({ detail }) => setSelectedTestSet(detail.selectedOption as SelectOption)}
              options={testSets.map((ts) => ({
                label: ts.name,
                value: ts.id,
                description: `${ts.fileCount || 0} documents`,
              }))}
              placeholder="Select a test set"
              loadingText="Loading test sets..."
              statusType={testSetsLoading ? 'loading' : 'finished'}
              empty="No test sets available"
            />
          </FormField>

          <Alert type="info">
            <strong>Training Data Requirements:</strong>
            <ul style={{ margin: '8px 0 0 0', paddingLeft: '20px' }}>
              <li>Minimum 100 validated documents recommended</li>
              <li>Documents should have human-reviewed extraction results</li>
              <li>Training typically takes 2-4 hours depending on data size</li>
              <li>Once complete, the model is available for on-demand inference</li>
            </ul>
          </Alert>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
};

export default CustomModelsLayout;
