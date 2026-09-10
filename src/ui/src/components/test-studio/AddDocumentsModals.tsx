// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * AddDocumentsModals — the two "Add documents" dialogs for an existing test set
 * (files matching a bucket pattern, or a zip upload) plus the matching-files
 * preview. Owns its own form state so the Test Sets table and a set's detail page
 * open the very same dialogs rather than two drifting copies.
 */

import React, { useEffect, useRef, useState } from 'react';
import type { SelectProps } from '@cloudscape-design/components';
import {
  Alert,
  Badge,
  Box,
  Button,
  DatePicker,
  ExpandableSection,
  FormField,
  Input,
  Modal,
  Select,
  SpaceBetween,
  TimeInput,
} from '@cloudscape-design/components';
import { generateClient } from '../../api/client-shim';
import { addDocumentsToTestSet, addDocumentsToTestSetFromUpload, listBucketFiles } from '../../graphql/generated';
import { getErrorMessage } from '../../utils/errorUtils';
import { BUCKET_OPTIONS, TIME_FILTER_OPTIONS } from './testSetOptions';

const client = generateClient();

const MAX_ZIP_SIZE_BYTES = 1073741824; // 1 GB

const REQUIRED_STRUCTURE = `documents.zip
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
                    └── result.json`;

export type AddDocumentsMode = 'pattern' | 'upload';

export interface AddDocumentsTarget {
  id: string;
  name: string;
  /** Prefills the pattern dialog, so the pattern that built the set can be reused. */
  filePattern?: string | null;
}

/** What the caller learns when a dialog submits; enough to update its own view of the set. */
export interface AddDocumentsResult {
  kind: AddDocumentsMode;
  message: string;
  testSet: {
    id: string;
    name?: string;
    fileCount?: number | null;
    status?: string | null;
    lastAddResult?: string | null;
  };
}

interface AddDocumentsModalsProps {
  testSet: AddDocumentsTarget | null;
  /** Which dialog is open; null closes both. */
  mode: AddDocumentsMode | null;
  onDismiss: () => void;
  onSubmitted: (result: AddDocumentsResult) => void;
}

const AddDocumentsModals = ({ testSet, mode, onDismiss, onSubmitted }: AddDocumentsModalsProps): React.JSX.Element => {
  const [filePattern, setFilePattern] = useState('');
  const [selectedBucket, setSelectedBucket] = useState<SelectProps.Option>(BUCKET_OPTIONS[0]);
  const [selectedTimeFilter, setSelectedTimeFilter] = useState<SelectProps.Option>(TIME_FILTER_OPTIONS[0]);
  const [customDate, setCustomDate] = useState('');
  const [customTime, setCustomTime] = useState('00:00:00');
  const [matchingFiles, setMatchingFiles] = useState<string[]>([]);
  const [fileCount, setFileCount] = useState(0);
  const [showFilesModal, setShowFilesModal] = useState(false);
  const [zipFile, setZipFile] = useState<File | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [showFileStructure, setShowFileStructure] = useState(() => localStorage.getItem('testset-show-file-structure') !== 'false');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  // Fresh form each time a dialog opens, seeded from the target set.
  useEffect(() => {
    if (!mode) return;
    setFilePattern(mode === 'pattern' ? (testSet?.filePattern ?? '') : '');
    setSelectedBucket(BUCKET_OPTIONS[0]);
    setSelectedTimeFilter(TIME_FILTER_OPTIONS[0]);
    setCustomDate('');
    setCustomTime('00:00:00');
    setMatchingFiles([]);
    setFileCount(0);
    setZipFile(null);
    if (fileInputRef.current) fileInputRef.current.value = '';
    setError('');
  }, [mode, testSet?.id]);

  const close = () => {
    setError('');
    onDismiss();
  };

  const getModifiedAfterTimestamp = (): string | undefined => {
    const filterValue = selectedTimeFilter.value;
    if (!filterValue) return undefined;
    if (filterValue === 'custom') {
      if (!customDate) return undefined;
      return `${customDate}T${customTime || '00:00:00'}.000Z`;
    }
    const date = new Date(Date.now() - parseInt(filterValue, 10) * 60 * 60 * 1000);
    return date.toISOString();
  };

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
      setError(`Failed to check files: ${getErrorMessage(err)}`);
    } finally {
      setLoading(false);
    }
  };

  const handleAddByPattern = async () => {
    if (!testSet) return;
    if (!filePattern.trim()) {
      setError('File pattern is required');
      return;
    }
    setLoading(true);
    try {
      const result = await client.graphql({
        query: addDocumentsToTestSet,
        variables: {
          testSetId: testSet.id,
          filePattern: filePattern.trim(),
          bucketType: selectedBucket.value ?? '',
          fileCount,
          modifiedAfter: getModifiedAfterTimestamp(),
        },
      });
      const updated = result.data.addDocumentsToTestSet;
      if (!updated) {
        setError('Failed to add documents - no data returned');
        return;
      }
      setError('');
      onSubmitted({
        kind: 'pattern',
        message: `Adding documents to test set "${testSet.name}"...`,
        testSet: updated,
      });
    } catch (err) {
      setError(`Failed to add documents: ${getErrorMessage(err)}`);
    } finally {
      setLoading(false);
    }
  };

  const handleAddByUpload = async () => {
    if (!testSet) return;
    if (!zipFile) {
      setError('Zip file is required');
      return;
    }
    setLoading(true);
    try {
      const result = await client.graphql({
        query: addDocumentsToTestSetFromUpload,
        variables: { input: { testSetId: testSet.id, fileName: zipFile.name, fileSize: zipFile.size } },
      });
      const response = result.data.addDocumentsToTestSetFromUpload;
      if (!response || !response.presignedUrl) {
        throw new Error('Failed to get upload URL from server');
      }
      const presigned = JSON.parse(response.presignedUrl);
      const formData = new FormData();
      Object.entries(presigned.fields as Record<string, string>).forEach(([key, value]) => formData.append(key, value));
      formData.append('file', zipFile);
      const uploadResponse = await fetch(presigned.url, { method: 'POST', body: formData });
      if (!uploadResponse.ok) {
        throw new Error(`Upload failed: ${uploadResponse.status} ${uploadResponse.statusText}`);
      }
      setError('');
      onSubmitted({
        kind: 'upload',
        message: `Uploading documents to test set "${testSet.name}". Zip file is being processed.`,
        // The extractor flips the set back to COMPLETED when it has recounted.
        testSet: { id: testSet.id, status: 'UPDATING' },
      });
    } catch (err) {
      setError(`Failed to add documents: ${getErrorMessage(err)}`);
    } finally {
      setLoading(false);
    }
  };

  const targetName = testSet?.name ?? '';

  return (
    <>
      <Modal
        visible={mode === 'pattern'}
        onDismiss={close}
        header={`Add Documents to "${targetName}"`}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={close}>
                Cancel
              </Button>
              <Button variant="primary" loading={loading} onClick={handleAddByPattern} disabled={fileCount === 0}>
                Add Documents
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          {mode === 'pattern' && error && <Alert type="error">{error}</Alert>}

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
                ? 'Use * for one folder level and ** for any depth; the folder path before the first wildcard is exact, the rest ignores case. Examples: test-set-name/input/*, test-set-name/input/**, test-set-prefix*/input/file-prefix*'
                : 'Use * for one folder level and ** for any depth; the folder path before the first wildcard is exact, the rest ignores case. Examples: prefix*, folder-name/*, folder-name/**/*.pdf, folder-prefix*/file-prefix*'
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
        visible={mode === 'upload'}
        onDismiss={close}
        header={`Add Documents to "${targetName}" from Upload`}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={close}>
                Cancel
              </Button>
              <Button variant="primary" loading={loading} onClick={handleAddByUpload} disabled={!zipFile}>
                Upload and Add Documents
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          {mode === 'upload' && error && <Alert type="error">{error}</Alert>}

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
                  {REQUIRED_STRUCTURE}
                </pre>
              </Box>
              <Alert type="info">Each input file must have a corresponding baseline folder with the same name.</Alert>
            </ExpandableSection>
            <input
              ref={fileInputRef}
              type="file"
              accept=".zip"
              aria-label="Zip file"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (!file) {
                  setZipFile(null);
                  return;
                }
                if (file.size > MAX_ZIP_SIZE_BYTES) {
                  setError(`Zip file size (${(file.size / 1024 / 1024 / 1024).toFixed(2)} GB) exceeds maximum limit of 1 GB`);
                  setZipFile(null);
                  return;
                }
                setZipFile(file);
                setError('');
              }}
              style={{ width: '100%', padding: '8px' }}
            />
            {zipFile && (
              <Box margin={{ top: 'xs' }}>
                <Badge color="blue">
                  {zipFile.name} ({(zipFile.size / 1024 / 1024).toFixed(1)} MB)
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
    </>
  );
};

export default AddDocumentsModals;
