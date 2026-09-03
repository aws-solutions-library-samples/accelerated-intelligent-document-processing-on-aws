// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useState, useCallback } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import type { SelectProps } from '@cloudscape-design/components';
import {
  Container,
  Header,
  SpaceBetween,
  Button,
  ButtonDropdown,
  Table,
  Box,
  Modal,
  FormField,
  Input,
  Alert,
  Badge,
  ExpandableSection,
  Select,
  DatePicker,
  TimeInput,
  StatusIndicator,
  Link,
} from '@cloudscape-design/components';
import { generateClient } from '../../api/client-shim';
import {
  addDocumentsToTestSet,
  addDocumentsToTestSetFromUpload,
  deleteTestSets,
  getTestSets,
  estimateReviewEffort,
  getDraftLabelJob,
  listBucketFiles,
  updateTestSet,
  publishTestSetVersion,
} from '../../graphql/generated';
import type { DocumentClassType } from '../../graphql/generated/schema-types';
import { getErrorMessage } from '../../utils/errorUtils';
import useSyntheticDataGenerator from '../../hooks/use-synthetic-data-generator';
import GenerateSyntheticDataModal from './GenerateSyntheticDataModal';
import CreateTestSetWizard from './CreateTestSetWizard';
import { LabelAccuracyLegend, renderLabelAccuracy } from './TestSetDetail';
import { testSetDetailHref, testSetAnnotateHref } from '../../routes/constants';

const client = generateClient();

// Constants
const MAX_ZIP_SIZE_BYTES = 1073741824; // 1 GB

const BUCKET_OPTIONS: SelectProps.Option[] = [
  { label: 'Input Bucket', value: 'input' },
  { label: 'Test Set Bucket', value: 'testset' },
];

const TIME_FILTER_OPTIONS: SelectProps.Option[] = [
  { label: 'No filter', value: '' },
  { label: 'Last 1 hour', value: '1' },
  { label: 'Last 4 hours', value: '4' },
  { label: 'Last 24 hours', value: '24' },
  { label: 'Last 7 days', value: '168' },
  { label: 'Last 30 days', value: '720' },
  { label: 'Custom date/time', value: 'custom' },
];

const DOCUMENT_CLASS_TYPE_OPTIONS: SelectProps.Option[] = [
  { label: 'Unspecified', value: '' },
  { label: 'Single Class', value: 'SINGLE_CLASS' },
  { label: 'Multi Class', value: 'MULTI_CLASS' },
  { label: 'Packet Splitting', value: 'PACKET_SPLITTING' },
];

interface TestSetItem {
  id: string;
  name: string;
  description?: string | null;
  filePattern?: string | null;
  fileCount?: number | null;
  source?: string | null;
  latestVersion?: number | null;
  activeReference?: number | null;
  labelState?: string | null;
  labelJobId?: string | null;
  labelJobStatus?: string | null;
  status?: string | null;
  createdAt: string;
  error?: string | null;
  documentClassType?: string | null;
}

const TestSets = (): React.JSX.Element => {
  const [testSets, setTestSets] = useState<TestSetItem[]>([]);
  const [selectedItems, setSelectedItems] = useState<TestSetItem[]>([]);
  const [showCreateWizard, setShowCreateWizard] = useState(false);
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [filePattern, setFilePattern] = useState('');
  const [selectedBucket, setSelectedBucket] = useState(BUCKET_OPTIONS[0]);
  const [matchingFiles, setMatchingFiles] = useState<string[]>([]);
  const [fileCount, setFileCount] = useState(0);
  const [showFilesModal, setShowFilesModal] = useState(false);
  const [loading, setLoading] = useState(false);
  const [initialLoading, setInitialLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState('');
  const [successMessage, setSuccessMessage] = useState('');
  // Sets seen mid-update, so a completion can be announced once when it lands.
  //
  // The outcome of the asynchronous add used to be stored on the record and rendered
  // from there, which made the notice immortal: dismissing it only cleared React
  // state, so the next poll brought it back, and nothing ever deleted it. Detecting
  // the transition here matches how synthetic-generation jobs already report
  // completion — one transient message, nothing persisted, dismissal final.
  const updatingRef = React.useRef<Set<string>>(new Set());
  const [showGenerateModal, setShowGenerateModal] = useState(false);
  const [genInitial, setGenInitial] = useState<{ tab?: 'prompt' | 'config'; version?: string; className?: string }>({});
  const location = useLocation();
  const navigate = useNavigate();
  // The synthetic-data generator is an optional extension; the button is only
  // shown when it's installed (available).
  const { available: generatorAvailable, getJobStatus, listActiveJobs } = useSyntheticDataGenerator();
  const [genJobs, setGenJobs] = useState<Record<string, { name: string; status: string; message: string; testSetId?: string }>>({});
  const [showFileStructure, setShowFileStructure] = useState(() => {
    return localStorage.getItem('testset-show-file-structure') !== 'false';
  });
  const [showAddDocsPatternModal, setShowAddDocsPatternModal] = useState(false);
  const [showAddDocsUploadModal, setShowAddDocsUploadModal] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);
  const [editDescription, setEditDescription] = useState('');
  const [editDocumentClassType, setEditDocumentClassType] = useState(DOCUMENT_CLASS_TYPE_OPTIONS[0]);
  const [selectedTimeFilter, setSelectedTimeFilter] = useState(TIME_FILTER_OPTIONS[0]);
  const [customDate, setCustomDate] = useState('');
  const [customTime, setCustomTime] = useState('00:00:00');
  const [addDocsZipFile, setAddDocsZipFile] = useState<File | null>(null);
  const addDocsFileInputRef = React.useRef<HTMLInputElement | null>(null);

  /**
   * Quality tier per test set, fetched lazily. The tier is derived from the
   * measured calibration curve by estimateReviewEffort rather than stored on the
   * set, so it cannot be asserted. One call per row; a larger list would want a
   * batch endpoint.
   */
  const [tiers, setTiers] = useState<Record<string, { tier: string; reason: string; accuracy: number | null }>>({});

  /**
   * Which sets have an estimate request in flight.
   *
   * Needed because a missing entry in `tiers` means three different things — not
   * fetched yet, not applicable (unlabeled), or fetched and no tier returned — and
   * only the last two are verdicts the column may state.
   */
  const [estimating, setEstimating] = useState<Record<string, boolean>>({});

  /**
   * Fetch each set's tier, keyed on the label state it was computed from. Must not
   * be called from loadTestSets, which runs on a 3s poll: that would fire one
   * estimateReviewEffort per labeled set every tick.
   */
  const loadTiers = useCallback(async (sets: TestSetItem[]) => {
    const client2 = generateClient();
    // Only labeled sets can have a tier; there is nothing to assess otherwise.
    const assessable = sets.filter((set) => set.labelState && set.labelState !== 'unlabeled');
    // Marked before the first await, so the column never has a window in which a
    // request is pending and the row looks settled.
    setEstimating((prev) => ({ ...prev, ...Object.fromEntries(assessable.map((set) => [set.id, true])) }));
    await Promise.all(
      assessable.map(async (set) => {
        try {
          const response = await client2.graphql({
            query: estimateReviewEffort,
            variables: { testSetId: set.id },
          });
          const est = response.data?.estimateReviewEffort;
          if (est?.qualityTier) {
            setTiers((prev) => ({
              ...prev,
              [set.id]: {
                tier: est.qualityTier as string,
                reason: (est.qualityTierReason as string) || '',
                accuracy: typeof est.baselineError === 'number' ? 1 - est.baselineError : null,
              },
            }));
          }
        } catch (err) {
          // A missing tier is not an error state; the column says "not assessed".
          console.debug(`No quality tier for ${set.id}:`, err);
        } finally {
          setEstimating((prev) => ({ ...prev, [set.id]: false }));
        }
      }),
    );
  }, []);

  const loadTestSets = async () => {
    try {
      console.log('TestSets: Loading test sets...');
      const result = await client.graphql({ query: getTestSets });
      console.log('TestSets: GraphQL result:', result);
      const backendTestSets = result.data.getTestSets || [];

      // Announce an asynchronous update once, as it lands. The add mutation returns
      // before the copy finishes, so this transition is the only moment the outcome
      // is known — and a transient message is the whole reason nothing needs to be
      // stored on the record to be re-rendered forever.
      const wasUpdating = updatingRef.current;
      const nowUpdating = new Set<string>();
      backendTestSets.forEach((ts) => {
        if (!ts) return;
        if (ts.status === 'UPDATING') {
          nowUpdating.add(ts.id);
        } else if (wasUpdating.has(ts.id) && ts.status === 'COMPLETED') {
          setSuccessMessage(`Test set "${ts.name}" updated — now ${ts.fileCount ?? 0} document(s).`);
        }
      });
      updatingRef.current = nowUpdating;

      // Upsert: merge backend data with existing UI state, deduplicating by id
      setTestSets((prevTestSets) => {
        const nonNullBackendTestSets = backendTestSets.filter((ts): ts is NonNullable<typeof ts> => ts !== null);
        const backendIds = new Set(nonNullBackendTestSets.map((ts) => ts.id));

        // Keep UI test sets that don't exist in backend (active processing)
        const uiOnlyTestSets = prevTestSets.filter((ts) => !backendIds.has(ts.id) && ts.status !== 'COMPLETED' && ts.status !== 'FAILED');

        // Combine backend test sets (always win) with UI-only active test sets
        return [...nonNullBackendTestSets, ...uiOnlyTestSets];
      });
    } catch (err) {
      console.error('TestSets: Failed to load test sets:', err);
      setError(`Failed to load test sets: ${err instanceof Error ? err.message : 'Unknown error'}`);
    } finally {
      setInitialLoading(false);
    }
  };

  // Preserve selections when testSets array changes
  React.useEffect(() => {
    if (selectedItems.length > 0) {
      const selectedIds = new Set(selectedItems.map((item) => item.id));
      const updatedSelections = testSets.filter((ts) => selectedIds.has(ts.id));
      if (updatedSelections.length !== selectedItems.length || !updatedSelections.every((item, index) => item === selectedItems[index])) {
        setSelectedItems(updatedSelections);
      }
    }
  }, [testSets]);

  React.useEffect(() => {
    loadTestSets();
  }, []);

  /**
   * Signature of the listed sets' label state. Includes labelJobStatus so a run
   * finishing re-rates the set, while an identical poll tick does not.
   */
  const labelStateSignature = testSets.map((ts) => `${ts.id}:${ts.labelState ?? ''}:${ts.labelJobStatus ?? ''}`).join('|');

  React.useEffect(() => {
    if (testSets.length > 0) loadTiers(testSets);
  }, [labelStateSignature, loadTiers]);

  /**
   * Drive any labeling job that is still RUNNING. Draft labels are harvested on
   * read, so a job only advances while something polls getDraftLabelJob; the
   * list's own getTestSets poll does not harvest.
   */
  React.useEffect(() => {
    const running = testSets.filter((ts) => ts.labelJobStatus === 'RUNNING' && ts.labelJobId);
    if (running.length === 0) return undefined;

    const client2 = generateClient();
    const interval = setInterval(() => {
      running.forEach((ts) => {
        client2.graphql({ query: getDraftLabelJob, variables: { testSetId: ts.id, jobId: ts.labelJobId as string } }).catch((err) => {
          // Best-effort: a failed poll must not break the list.
          console.debug(`Could not advance labeling job for ${ts.id}:`, err);
        });
      });
    }, 5000);

    return () => clearInterval(interval);
    // Keyed on the running set ids so this re-arms when a job starts or ends,
    // not on every list refresh.
  }, [
    testSets
      .filter((ts) => ts.labelJobStatus === 'RUNNING')
      .map((ts) => ts.id)
      .join(','),
  ]);

  // Simple polling for active test sets
  React.useEffect(() => {
    const hasActiveTestSets = testSets.some((testSet) => testSet.status !== 'COMPLETED' && testSet.status !== 'FAILED');

    if (!hasActiveTestSets) {
      console.log('No active test sets, no polling needed');
      return;
    }

    console.log('Starting polling for active test sets');
    const interval = setInterval(() => {
      console.log('Polling refresh...');
      loadTestSets();
    }, 3000);

    return () => {
      console.log('Cleaning up polling');
      clearInterval(interval);
    };
  }, [testSets]);

  // Poll in-flight synthetic-generation jobs until terminal.
  React.useEffect(() => {
    const jobIds = Object.keys(genJobs);
    if (jobIds.length === 0) return;
    const interval = setInterval(async () => {
      for (const jobId of jobIds) {
        const job = await getJobStatus(jobId);
        if (!job) continue;
        if (job.status === 'COMPLETED') {
          setGenJobs((prev) => {
            const { [jobId]: _done, ...rest } = prev;
            return rest;
          });
          setSuccessMessage(`Synthetic data generation complete: "${genJobs[jobId]?.name}".`);
          loadTestSets();
        } else if (job.status === 'FAILED') {
          setGenJobs((prev) => {
            const { [jobId]: _failed, ...rest } = prev;
            return rest;
          });
          setError(`Synthetic data generation failed for "${genJobs[jobId]?.name}": ${job.errorMessage || 'unknown error'}`);
        } else {
          setGenJobs((prev) =>
            prev[jobId]
              ? { ...prev, [jobId]: { ...prev[jobId], status: job.status, message: job.statusMessage || prev[jobId].message } }
              : prev,
          );
        }
      }
    }, 5000);
    return () => clearInterval(interval);
  }, [genJobs, getJobStatus]);

  // Adopt in-flight generation jobs the page did not itself start (e.g. started
  // from Quick Start) so they show as GENERATING rows here too.
  React.useEffect(() => {
    if (!generatorAvailable) return;
    let cancelled = false;
    const adopt = async () => {
      const jobs = await listActiveJobs();
      if (cancelled || jobs.length === 0) return;
      setGenJobs((prev) => {
        const next = { ...prev };
        for (const job of jobs) {
          if (!next[job.jobId]) {
            next[job.jobId] = {
              name: job.testSetId || job.configVersion || 'Synthetic documents',
              status: job.status,
              message: job.statusMessage || 'Generating…',
              testSetId: job.testSetId,
            };
          }
        }
        return next;
      });
    };
    adopt();
    const interval = setInterval(adopt, 15000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [generatorAvailable, listActiveJobs]);

  // Open the generate modal pre-filled when deep-linked (e.g. from the Schema
  // Builder "Generate test set for this class" button): ?generate=1&version=&className=
  React.useEffect(() => {
    const params = new URLSearchParams(location.search);
    if (params.get('generate') !== '1') return;
    const version = params.get('version') || undefined;
    const className = params.get('className') || undefined;
    setGenInitial({ tab: version ? 'config' : 'prompt', version, className });
    setShowGenerateModal(true);
    params.delete('generate');
    params.delete('version');
    params.delete('className');
    navigate({ search: params.toString() }, { replace: true });
  }, [location.search]);

  // Separate discovery polling for new test sets (less frequent)
  React.useEffect(() => {
    console.log('Starting discovery polling for new test sets');
    const discoveryInterval = setInterval(() => {
      console.log('Discovery polling...');
      loadTestSets();
    }, 60000); // Every 60 seconds (1 minute)

    return () => {
      console.log('Cleaning up discovery polling');
      clearInterval(discoveryInterval);
    };
  }, []); // No dependencies - always runs

  const getModifiedAfterTimestamp = (): string | undefined => {
    const filterValue = selectedTimeFilter.value;
    if (!filterValue) return undefined;
    if (filterValue === 'custom') {
      if (!customDate) return undefined;
      return `${customDate}T${customTime || '00:00:00'}.000Z`;
    }
    const date = new Date(Date.now() - parseInt(filterValue) * 60 * 60 * 1000);
    return date.toISOString();
  };

  // Cleanup polling on unmount
  const handleCheckFiles = async () => {
    if (!filePattern.trim()) return;

    setLoading(true);
    try {
      const result = await client.graphql({
        query: listBucketFiles,
        variables: {
          bucketType: selectedBucket.value ?? '',
          filePattern: filePattern.trim(),
          modifiedAfter: getModifiedAfterTimestamp(),
        },
      });

      const files = (result.data.listBucketFiles || []).filter((f): f is string => f !== null);
      setMatchingFiles(files);
      setFileCount(files.length);
      setShowFilesModal(true);
    } catch (err) {
      const errorMessage = getErrorMessage(err);
      setError(`Failed to check files: ${errorMessage}`);
    } finally {
      setLoading(false);
    }
  };

  const validateDescription = (desc: string): boolean => {
    return desc.length <= 500;
  };

  const handleRefresh = async () => {
    setRefreshing(true);
    setError('');
    setSuccessMessage('');
    try {
      const result = await client.graphql({ query: getTestSets });
      setTestSets((result.data.getTestSets || []).filter((ts): ts is NonNullable<typeof ts> => ts !== null));
    } catch (err) {
      console.error('Error refreshing test sets:', err);
      const errorMessage = getErrorMessage(err);
      setError(`Failed to refresh test sets: ${errorMessage}`);
    } finally {
      setRefreshing(false);
    }
  };

  const handleEditTestSet = async () => {
    const selected = selectedItems[0];
    if (!selected) {
      setError('No test set selected');
      return;
    }

    // Validate description
    if (editDescription && !validateDescription(editDescription.trim())) {
      setError('Description cannot exceed 500 characters');
      return;
    }

    setLoading(true);
    try {
      const input: {
        id: string;
        description?: string;
        documentClassType?: DocumentClassType | null;
      } = {
        id: selected.id,
        description: editDescription.trim(),
      };

      // Add documentClassType if specified (not "Unspecified")
      if (editDocumentClassType.value) {
        input.documentClassType = editDocumentClassType.value as DocumentClassType;
      } else {
        input.documentClassType = null;
      }

      const result = await client.graphql({
        query: updateTestSet,
        variables: { input },
      });

      const updatedTestSet = result.data.updateTestSet;

      if (updatedTestSet) {
        // Update the test set in the list
        setTestSets((prev) => prev.map((ts) => (ts.id === updatedTestSet.id ? updatedTestSet : ts)));
        setSuccessMessage(`Successfully updated test set "${updatedTestSet.name}"`);
        setError('');
        setShowEditModal(false);
        setSelectedItems([updatedTestSet]);
      } else {
        setError('Failed to update test set - no data returned');
      }
    } catch (err) {
      console.error('Error updating test set:', err);
      const errorMessage = getErrorMessage(err);
      setError(`Failed to update test set: ${errorMessage}`);
    } finally {
      setLoading(false);
    }
  };

  const handleDeleteTestSets = async () => {
    const testSetIds = selectedItems.map((item) => item.id);
    const deleteCount = testSetIds.length;

    setLoading(true);
    try {
      await client.graphql({
        query: deleteTestSets,
        variables: { testSetIds },
      });
      setTestSets(testSets.filter((testSet) => !testSetIds.includes(testSet.id)));
      setSelectedItems([]);
      setSuccessMessage(`Successfully deleted ${deleteCount} test set${deleteCount > 1 ? 's' : ''}`);
      setError('');
    } catch (err) {
      console.error('Error deleting test sets:', err);
      const errorMessage = getErrorMessage(err);
      setError(`Failed to delete test sets: ${errorMessage}`);
    } finally {
      setLoading(false);
    }
  };

  const handlePublishVersion = async () => {
    const target = selectedItems[0];
    if (!target) return;

    setLoading(true);
    try {
      const result = await client.graphql({
        query: publishTestSetVersion,
        variables: { input: { testSetId: target.id, setAsActiveReference: true } },
      });
      const published = result.data.publishTestSetVersion;
      setSuccessMessage(`Published ${target.name} version ${published?.version ?? ''} as the active reference`);
      setError('');
      loadTestSets();
    } catch (err) {
      console.error('Error publishing test set version:', err);
      setError(`Failed to publish version: ${getErrorMessage(err)}`);
    } finally {
      setLoading(false);
    }
  };

  const handleAddDocuments = async () => {
    if (!filePattern.trim()) {
      setError('File pattern is required');
      return;
    }

    const targetTestSet = selectedItems[0];
    if (!targetTestSet) return;

    setLoading(true);
    try {
      const result = await client.graphql({
        query: addDocumentsToTestSet,
        variables: {
          testSetId: targetTestSet.id,
          filePattern: filePattern.trim(),
          bucketType: selectedBucket.value ?? '',
          fileCount,
          modifiedAfter: getModifiedAfterTimestamp(),
        },
      });

      const updatedTestSet = result.data.addDocumentsToTestSet;

      if (updatedTestSet) {
        setTestSets((prev) => {
          const idx = prev.findIndex((ts) => ts.id === updatedTestSet.id);
          if (idx >= 0) {
            const updated = [...prev];
            updated[idx] = updatedTestSet;
            return updated;
          }
          return prev;
        });
        setFilePattern('');
        setSelectedBucket(BUCKET_OPTIONS[0]);
        setSelectedTimeFilter(TIME_FILTER_OPTIONS[0]);
        setCustomDate('');
        setCustomTime('00:00:00');
        setFileCount(0);
        setShowAddDocsPatternModal(false);
        setError('');
        setSuccessMessage(`Adding documents to test set "${targetTestSet.name}"...`);
      } else {
        setError('Failed to add documents - no data returned');
      }
    } catch (err) {
      console.error('Error adding documents to test set:', err);
      const errorMessage = getErrorMessage(err);
      setError(`Failed to add documents: ${errorMessage}`);
    } finally {
      setLoading(false);
    }
  };

  const handleAddDocumentsUpload = async () => {
    const targetTestSet = selectedItems[0];
    if (!targetTestSet) return;

    if (!addDocsZipFile) {
      setError('Zip file is required');
      return;
    }

    setLoading(true);
    try {
      const result = await client.graphql({
        query: addDocumentsToTestSetFromUpload,
        variables: {
          input: {
            testSetId: targetTestSet.id,
            fileName: addDocsZipFile.name,
            fileSize: addDocsZipFile.size,
          },
        },
      });

      const response = result.data.addDocumentsToTestSetFromUpload;

      if (!response || !response.presignedUrl) {
        throw new Error('Failed to get upload URL from server');
      }

      const presignedPostData = JSON.parse(response.presignedUrl);
      const formData = new FormData();

      Object.entries(presignedPostData.fields).forEach(([key, value]) => {
        formData.append(key, value as string);
      });
      formData.append('file', addDocsZipFile);

      const uploadResponse = await fetch(presignedPostData.url, {
        method: 'POST',
        body: formData,
      });

      if (!uploadResponse.ok) {
        throw new Error(`Upload failed: ${uploadResponse.status} ${uploadResponse.statusText}`);
      }

      // Update the test set status in UI
      setTestSets((prev) => {
        const idx = prev.findIndex((ts) => ts.id === targetTestSet.id);
        if (idx >= 0) {
          const updated = [...prev];
          updated[idx] = { ...updated[idx], status: 'UPDATING' };
          return updated;
        }
        return prev;
      });

      setSuccessMessage(`Uploading documents to test set "${targetTestSet.name}". Zip file is being processed.`);
      setError('');
      setShowAddDocsUploadModal(false);
      setAddDocsZipFile(null);
      if (addDocsFileInputRef.current) {
        addDocsFileInputRef.current.value = '';
      }
    } catch (err) {
      console.error('Error adding documents from upload:', err);
      const errorMessage = getErrorMessage(err);
      setError(`Failed to add documents: ${errorMessage}`);
    } finally {
      setLoading(false);
    }
  };

  // Suppress an optimistic gen: row once the real registered test set (same
  // id/name) shows up from getTestSets, to avoid a duplicate row.
  const realTestSetIds = new Set(testSets.map((ts) => ts.id));
  const generatingRows: TestSetItem[] = Object.entries(genJobs)
    .filter(([, j]) => !realTestSetIds.has(j.testSetId || j.name))
    .map(([jobId, j]) => ({
      id: `gen:${jobId}`,
      name: j.name,
      description: j.message,
      status: 'GENERATING',
      createdAt: new Date().toISOString(),
    }));

  const filteredTestSets = [...generatingRows, ...testSets]
    .filter((item) => item != null)
    .sort((a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime());
  console.log('Filtered testSets for Table:', filteredTestSets);

  const columnDefinitions = [
    {
      id: 'name',
      header: 'Test Set Name',
      cell: (item: TestSetItem) => (item.status === 'COMPLETED' ? <Link href={testSetDetailHref(item.id)}>{item.name}</Link> : item.name),
      sortingField: 'name',
    },
    {
      id: 'id',
      header: 'Test Set ID',
      cell: (item: TestSetItem) => item.id,
      sortingField: 'id',
    },
    {
      id: 'description',
      header: 'Description',
      cell: (item: TestSetItem) => item.description || '-',
      width: 200,
      minWidth: 120,
    },
    {
      id: 'filePattern',
      header: 'File Pattern',
      cell: (item: TestSetItem) => item.filePattern,
    },
    {
      id: 'fileCount',
      header: 'Files',
      cell: (item: TestSetItem) => item.fileCount,
    },
    {
      id: 'source',
      header: 'Source',
      cell: (item: TestSetItem) => {
        if (!item.source) {
          return '-';
        }
        const badges: Record<string, React.JSX.Element> = {
          synthetic: <Badge color="grey">Synthetic</Badge>,
          uploaded: <Badge color="blue">Uploaded</Badge>,
          mixed: <Badge color="green">Mixed</Badge>,
        };
        return badges[item.source] || item.source;
      },
      sortingField: 'source',
    },
    {
      id: 'labelState',
      header: 'Labels',
      cell: (item: TestSetItem) => {
        // A running job outranks the stored labelState: it is the live one.
        if (item.labelJobStatus === 'RUNNING') {
          return <StatusIndicator type="in-progress">Labeling</StatusIndicator>;
        }
        if (item.labelJobStatus === 'FAILED' && item.labelState !== 'labeled') {
          return <StatusIndicator type="error">Labeling failed</StatusIndicator>;
        }
        const badges: Record<string, React.JSX.Element> = {
          unlabeled: <Badge color="severity-neutral">Unlabeled</Badge>,
          draft: <Badge color="blue">Draft (machine)</Badge>,
          labeled: <Badge color="green">Labeled</Badge>,
        };
        // Sets predating labelState were created with ground truth, so a missing
        // value must not imply they need labeling.
        return item.labelState ? badges[item.labelState] || item.labelState : '-';
      },
      sortingField: 'labelState',
    },
    {
      id: 'qualityTier',
      // The tier names are meaningless without the scale, and the per-row popover
      // only ever explains the row you are hovering.
      header: <LabelAccuracyLegend />,
      cell: (item: TestSetItem) => renderLabelAccuracy(tiers[item.id], item.labelState, estimating[item.id]),
    },
    {
      id: 'version',
      header: 'Version',
      cell: (item: TestSetItem) => {
        if (!item.latestVersion) {
          return <span style={{ color: '#5f6b7a' }}>draft</span>;
        }
        // Shows the active reference, which is what runs score against, and notes
        // when the latest published version is ahead of it.
        const active = item.activeReference;
        const label = active ? `v${active}` : `v${item.latestVersion} (no ref)`;
        const behind = active && item.latestVersion > active ? ` · latest v${item.latestVersion}` : '';
        return (
          <span>
            <Badge color="blue">{label}</Badge>
            {behind && <span style={{ color: '#5f6b7a', fontSize: '0.85em' }}>{behind}</span>}
          </span>
        );
      },
      sortingField: 'activeReference',
    },
    {
      id: 'documentClassType',
      header: 'Classification Type',
      cell: (item: TestSetItem) => {
        if (!item.documentClassType) {
          return '-';
        }
        const badges: Record<string, React.JSX.Element> = {
          SINGLE_CLASS: <Badge color="blue">Single Class</Badge>,
          MULTI_CLASS: <Badge color="green">Multi Class</Badge>,
          PACKET_SPLITTING: <Badge>Packet Splitting</Badge>,
        };
        return badges[item.documentClassType] || item.documentClassType;
      },
    },
    {
      id: 'status',
      header: 'Status',
      cell: (item: TestSetItem) => {
        const status = item.status || '-';

        if (status === 'GENERATING') {
          return <StatusIndicator type="in-progress">{item.description || 'Generating…'}</StatusIndicator>;
        }

        if (status === 'UPDATING') {
          return <Badge color="blue">Updating...</Badge>;
        }

        if (status === 'FAILED' && item.error) {
          const truncatedError = item.error.length > 15 ? `${item.error.substring(0, 15)}...` : item.error;

          return (
            <div>
              <div style={{ color: '#d13212', fontWeight: 'bold' }}>FAILED</div>
              <div
                style={{
                  fontSize: '0.9em',
                  color: '#666',
                  marginTop: '2px',
                  cursor: 'help',
                  maxWidth: '200px',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
                title={item.error}
              >
                {truncatedError}
              </div>
            </div>
          );
        }

        return status;
      },
    },
    {
      id: 'createdAt',
      header: 'Created',
      cell: (item: TestSetItem) => new Date(item.createdAt).toLocaleDateString(),
      sortingField: 'createdAt',
    },
  ];

  return (
    <Container
      header={
        <Header
          variant="h2"
          description="Manage test sets for document processing"
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              {/* Everything that acts on the selected set belongs in the single
                  Actions menu, not as another header button. */}
              <Button iconName="refresh" loading={refreshing} onClick={handleRefresh} ariaLabel="Refresh test set list" />
              <ButtonDropdown
                disabled={selectedItems.length === 0 || loading}
                items={[
                  { id: 'annotate', text: 'Annotate ground truth', disabled: selectedItems.length !== 1 },
                  { id: 'browse', text: 'Browse documents', disabled: selectedItems.length !== 1 },
                  {
                    id: 'add-docs',
                    text: 'Add documents',
                    disabled: selectedItems.length !== 1 || selectedItems[0]?.status !== 'COMPLETED',
                    items: [
                      { id: 'docs-pattern', text: 'From files in a bucket' },
                      { id: 'docs-upload', text: 'From a zip upload' },
                    ],
                  },
                  {
                    id: 'publish',
                    text: 'Publish version',
                    disabled: selectedItems.length !== 1 || selectedItems[0]?.status !== 'COMPLETED',
                  },
                  { id: 'edit', text: 'Edit details', disabled: selectedItems.length !== 1 },
                  { id: 'delete', text: 'Delete' },
                ]}
                onItemClick={({ detail }) => {
                  const selected = selectedItems[0];
                  if (detail.id === 'annotate' && selected) {
                    window.location.hash = testSetAnnotateHref(selected.id).slice(1);
                  } else if (detail.id === 'browse' && selected) {
                    window.location.hash = testSetDetailHref(selected.id).slice(1);
                  } else if (detail.id === 'docs-pattern') {
                    setFilePattern(selected?.filePattern || '');
                    setSelectedBucket(BUCKET_OPTIONS[0]);
                    setSelectedTimeFilter(TIME_FILTER_OPTIONS[0]);
                    setCustomDate('');
                    setCustomTime('00:00:00');
                    setFileCount(0);
                    setError('');
                    setShowAddDocsPatternModal(true);
                  } else if (detail.id === 'docs-upload') {
                    setAddDocsZipFile(null);
                    setError('');
                    setShowAddDocsUploadModal(true);
                  } else if (detail.id === 'publish') {
                    handlePublishVersion();
                  } else if (detail.id === 'edit' && selected) {
                    setEditDescription(selected.description || '');
                    setEditDocumentClassType(
                      DOCUMENT_CLASS_TYPE_OPTIONS.find((opt) => opt.value === selected.documentClassType) || DOCUMENT_CLASS_TYPE_OPTIONS[0],
                    );
                    setShowEditModal(true);
                  } else if (detail.id === 'delete') {
                    setShowDeleteModal(true);
                  }
                }}
              >
                Actions
              </ButtonDropdown>
              <Button variant="primary" iconName="add-plus" onClick={() => setShowCreateWizard(true)}>
                Create test set
              </Button>
            </SpaceBetween>
          }
        >
          Test Sets ({filteredTestSets.length})
        </Header>
      }
    >
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError('')}>
          {error}
        </Alert>
      )}

      {successMessage && (
        <Alert type="success" dismissible onDismiss={() => setSuccessMessage('')}>
          {successMessage}
        </Alert>
      )}

      <Table
        resizableColumns
        wrapLines
        columnDefinitions={columnDefinitions}
        items={filteredTestSets}
        selectedItems={selectedItems}
        onSelectionChange={({ detail }) => setSelectedItems(detail.selectedItems)}
        selectionType="multi"
        loading={initialLoading}
        loadingText="Loading test sets..."
        isItemDisabled={(item) => item.status !== 'COMPLETED' && item.status !== 'FAILED'}
        empty={
          <Box textAlign="center" color="inherit">
            <b>No test sets</b>
            <Box padding={{ bottom: 's' }} variant="p" color="inherit">
              No test sets to display.
            </Box>
          </Box>
        }
      />

      {/* Creating a set goes through the wizard; adding documents to an existing
            set is a separate operation and keeps its own modals. */}
      <CreateTestSetWizard
        visible={showCreateWizard}
        onDismiss={() => setShowCreateWizard(false)}
        onCreated={(message) => {
          setSuccessMessage(message);
          setError('');
          setShowCreateWizard(false);
          loadTestSets();
        }}
        generatorAvailable={generatorAvailable}
        onGenerationStarted={(jobId, label, testSetId) => {
          setShowCreateWizard(false);
          setSuccessMessage(`Synthetic data generation started for "${label}". It will appear in the list when it completes.`);
          setGenJobs((prev) => ({
            ...prev,
            [jobId]: { name: label, status: 'GENERATING', message: 'Starting generation', testSetId },
          }));
        }}
      />

      <Modal
        visible={showAddDocsPatternModal}
        onDismiss={() => {
          setShowAddDocsPatternModal(false);
          setSelectedBucket(BUCKET_OPTIONS[0]);
          setSelectedTimeFilter(TIME_FILTER_OPTIONS[0]);
          setCustomDate('');
          setCustomTime('00:00:00');
          setFileCount(0);
          setFilePattern('');
          setError('');
        }}
        header={`Add Documents to "${selectedItems[0]?.name ?? ''}"`}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                onClick={() => {
                  setShowAddDocsPatternModal(false);
                  setSelectedBucket(BUCKET_OPTIONS[0]);
                  setSelectedTimeFilter(TIME_FILTER_OPTIONS[0]);
                  setCustomDate('');
                  setCustomTime('00:00:00');
                  setFileCount(0);
                  setFilePattern('');
                  setError('');
                }}
              >
                Cancel
              </Button>
              <Button variant="primary" loading={loading} onClick={handleAddDocuments} disabled={fileCount === 0}>
                Add Documents
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          {error && <Alert type="error">{error}</Alert>}

          <FormField label="Source Bucket" description="Select the bucket to search for files">
            <Select
              selectedOption={selectedBucket}
              onChange={({ detail }) => {
                setSelectedBucket(detail.selectedOption);
                setFileCount(0);
              }}
              options={BUCKET_OPTIONS}
            />
          </FormField>

          <FormField
            label="File Pattern"
            description={
              selectedBucket.value === 'testset'
                ? 'Use * for wildcards. Examples: test-set-name/input/*, test-set-prefix*/input/file-prefix*'
                : 'Use * for wildcards. Examples: prefix*, folder-name/*, folder-name/prefix*, folder-prefix*/file-prefix*'
            }
          >
            <SpaceBetween direction="horizontal" size="xs">
              <Input
                value={filePattern}
                onChange={({ detail }) => {
                  setFilePattern(detail.value);
                  setFileCount(0);
                }}
                placeholder={selectedBucket.value === 'testset' ? 'test-set-prefix*/input/*' : 'prefix*/*'}
              />
              <Button disabled={!filePattern.trim()} loading={loading} onClick={handleCheckFiles}>
                Check Files
              </Button>
            </SpaceBetween>
          </FormField>

          {selectedBucket.value === 'input' && (
            <FormField label="Modified after" description="Optional: only include files modified within this time period">
              <SpaceBetween size="xs">
                <Select
                  selectedOption={selectedTimeFilter}
                  onChange={({ detail }) => {
                    setSelectedTimeFilter(detail.selectedOption);
                    setFileCount(0);
                  }}
                  options={TIME_FILTER_OPTIONS}
                />
                {selectedTimeFilter.value === 'custom' && (
                  <SpaceBetween size="xs" direction="horizontal">
                    <DatePicker
                      value={customDate}
                      onChange={({ detail }) => {
                        setCustomDate(detail.value);
                        setFileCount(0);
                      }}
                      placeholder="YYYY/MM/DD"
                      openCalendarAriaLabel={(selectedDate) => `Choose date${selectedDate ? `, selected date is ${selectedDate}` : ''}`}
                    />
                    <TimeInput
                      value={customTime}
                      onChange={({ detail }) => {
                        setCustomTime(detail.value);
                        setFileCount(0);
                      }}
                      format="hh:mm:ss"
                      placeholder="HH:mm:ss"
                    />
                    <Box variant="small" padding={{ top: 'xs' }}>
                      UTC
                    </Box>
                  </SpaceBetween>
                )}
              </SpaceBetween>
            </FormField>
          )}

          {fileCount > 0 && (
            <Box>
              <Badge color="green">
                {fileCount} {fileCount === 1 ? 'file' : 'files'} found
              </Badge>
            </Box>
          )}

          {selectedBucket.value === 'input' && (
            <Alert type="info">Files without matching baseline data in the evaluation bucket will be automatically excluded.</Alert>
          )}
        </SpaceBetween>
      </Modal>

      <Modal
        visible={showAddDocsUploadModal}
        onDismiss={() => {
          setShowAddDocsUploadModal(false);
          setAddDocsZipFile(null);
          setError('');
          if (addDocsFileInputRef.current) {
            addDocsFileInputRef.current.value = '';
          }
        }}
        header={`Add Documents to "${selectedItems[0]?.name ?? ''}" from Upload`}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                onClick={() => {
                  setShowAddDocsUploadModal(false);
                  setAddDocsZipFile(null);
                  setError('');
                  if (addDocsFileInputRef.current) {
                    addDocsFileInputRef.current.value = '';
                  }
                }}
              >
                Cancel
              </Button>
              <Button variant="primary" loading={loading} onClick={handleAddDocumentsUpload} disabled={!addDocsZipFile}>
                Upload and Add Documents
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          {error && <Alert type="error">{error}</Alert>}

          <FormField label="Zip File" description="Select a zip file containing documents and baseline data to add">
            <ExpandableSection
              headerText="View required file structure"
              variant="footer"
              expanded={showFileStructure}
              onChange={({ detail }) => {
                setShowFileStructure(detail.expanded);
                localStorage.setItem('testset-show-file-structure', detail.expanded.toString());
              }}
            >
              <Box margin={{ bottom: 's' }}>
                <pre
                  style={{
                    backgroundColor: '#f8f9fa',
                    padding: '12px',
                    borderRadius: '4px',
                    fontSize: '12px',
                    overflow: 'auto',
                  }}
                >
                  {`documents.zip
└── documents/
    ├── input/
    │   ├── document1.pdf
    │   └── document2.pdf
    └── baseline/
        ├── document1.pdf/
        │   └── sections/
        │       └── 1/
        │           └── result.json
        └── document2.pdf/
            └── sections/
                └── 1/
                    └── result.json`}
                </pre>
              </Box>
              <Alert type="info">Each input file must have a corresponding baseline folder with the same name.</Alert>
            </ExpandableSection>
            <input
              ref={addDocsFileInputRef}
              type="file"
              accept=".zip"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) {
                  if (file.size > MAX_ZIP_SIZE_BYTES) {
                    setError(`Zip file size (${(file.size / 1024 / 1024 / 1024).toFixed(2)} GB) exceeds maximum limit of 1 GB`);
                    setAddDocsZipFile(null);
                    return;
                  }
                  setAddDocsZipFile(file);
                  setError('');
                } else {
                  setAddDocsZipFile(null);
                }
              }}
              style={{ width: '100%', padding: '8px' }}
            />
            {addDocsZipFile && (
              <Box margin={{ top: 'xs' }}>
                <Badge color="blue">
                  {addDocsZipFile.name} ({(addDocsZipFile.size / 1024 / 1024).toFixed(1)} MB)
                </Badge>
              </Box>
            )}
          </FormField>
        </SpaceBetween>
      </Modal>

      <Modal
        visible={showFilesModal}
        onDismiss={() => setShowFilesModal(false)}
        header={`Matching Files (${matchingFiles.length})`}
        footer={
          <Box float="right">
            <Button onClick={() => setShowFilesModal(false)}>Close</Button>
          </Box>
        }
      >
        <Box>
          {matchingFiles.length > 0 ? (
            <ul style={{ fontSize: '12px' }}>
              {matchingFiles.map((file) => (
                <li key={file}>{file}</li>
              ))}
            </ul>
          ) : (
            <Box textAlign="center">No matching files found</Box>
          )}
        </Box>
      </Modal>

      <Modal
        visible={showEditModal}
        onDismiss={() => {
          setShowEditModal(false);
          setError('');
        }}
        header="Edit Test Set"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                onClick={() => {
                  setShowEditModal(false);
                  setError('');
                }}
              >
                Cancel
              </Button>
              <Button variant="primary" loading={loading} onClick={handleEditTestSet}>
                Save
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          {error && <Alert type="error">{error}</Alert>}

          <FormField label="Test Set Name">
            <Input value={selectedItems[0]?.name || ''} disabled />
          </FormField>

          <FormField
            label="Description"
            description="Optional description for this test set"
            errorText={editDescription && !validateDescription(editDescription) ? 'Description cannot exceed 500 characters' : ''}
          >
            <Input
              value={editDescription}
              onChange={({ detail }) => setEditDescription(detail.value)}
              placeholder="Test set description"
              invalid={!!editDescription && !validateDescription(editDescription)}
            />
          </FormField>

          <FormField label="Document Classification Type" description="Specify the type of documents in this test set">
            <Select
              selectedOption={editDocumentClassType}
              onChange={({ detail }) => setEditDocumentClassType(detail.selectedOption)}
              options={DOCUMENT_CLASS_TYPE_OPTIONS}
            />
          </FormField>
        </SpaceBetween>
      </Modal>

      <Modal
        visible={showDeleteModal}
        onDismiss={() => setShowDeleteModal(false)}
        header="Confirm Delete"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShowDeleteModal(false)}>
                Cancel
              </Button>
              <Button
                variant="primary"
                loading={loading}
                onClick={() => {
                  handleDeleteTestSets();
                  setShowDeleteModal(false);
                }}
              >
                Delete
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <Box>
          <div>Are you sure you want to delete the following test set{selectedItems.length > 1 ? 's' : ''}?</div>
          <ul style={{ marginTop: '10px' }}>
            {selectedItems.map((item) => (
              <li key={item.id}>
                <strong>{item.name}</strong>
                {item.filePattern && ` (${item.filePattern})`}
              </li>
            ))}
          </ul>
        </Box>
      </Modal>

      <GenerateSyntheticDataModal
        visible={showGenerateModal}
        initialTab={genInitial.tab}
        initialVersion={genInitial.version}
        initialClassName={genInitial.className}
        onDismiss={() => {
          setShowGenerateModal(false);
          setGenInitial({});
        }}
        onStarted={(jobId, label, testSetId) => {
          setShowGenerateModal(false);
          setGenInitial({});
          setSuccessMessage(`Synthetic data generation started for "${label}". It will appear in the list when it completes.`);
          setGenJobs((prev) => ({ ...prev, [jobId]: { name: label, status: 'GENERATING', message: 'Starting generation', testSetId } }));
        }}
      />
    </Container>
  );
};

export default TestSets;
