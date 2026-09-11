// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// src/components/upload-document/UploadDocumentPanel.jsx
import React, { useState, useEffect } from 'react';
import {
  Button,
  Container,
  Header,
  SpaceBetween,
  FormField,
  StatusIndicator,
  Alert,
  Input,
  FileUpload,
  Select,
  SegmentedControl,
  Checkbox,
  Box,
} from '@cloudscape-design/components';
import type { SelectProps } from '@cloudscape-design/components';
import yaml from 'js-yaml';
import { generateClient } from '../../api/client-shim';

import { uploadDocument } from '../../graphql/generated';

import useConfigurationVersions from '../../hooks/use-configuration-versions';
import ConfigRevisionSelector from '../common/ConfigRevisionSelector';
import useSampleDocuments, { type SampleDocument } from '../../hooks/use-sample-documents';
import useConfigurationLibrary from '../../hooks/use-configuration-library';

import useSettingsContext from '../../contexts/settings';
import { SUPPORTED_UPLOAD_EXTENSIONS } from '../common/constants';

const client = generateClient();

interface UploadStatusItem {
  file: string;
  status: 'success' | 'error';
  objectKey?: string;
  error?: string;
}

type UploadSource = 'local' | 'sample';

// Derive the config_library/unified pattern directory the sample's config lives
// under. Samples currently only ship for the unified architecture, so this is
// always 'unified'; kept as a helper to mirror ConfigurationLayout's mapping.
const SAMPLE_CONFIG_PATTERN_DIR = 'unified';

const UploadDocumentPanel = (): React.JSX.Element => {
  const { settings } = useSettingsContext();
  const { versions, getVersionOptions, saveAsNewVersion } = useConfigurationVersions();
  const { listSamples, uploadSample } = useSampleDocuments();
  const { getFile } = useConfigurationLibrary();

  const [uploadSourceType, setUploadSourceType] = useState<UploadSource>('local');
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadStatus, setUploadStatus] = useState<UploadStatusItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [prefix, setPrefix] = useState('');
  const [selectedVersion, setSelectedVersion] = useState<SelectProps.Option | null>(null);
  // null = the profile's current configuration.
  const [selectedRevision, setSelectedRevision] = useState<number | null>(null);

  // Sample-browser state
  const [samples, setSamples] = useState<SampleDocument[]>([]);
  const [samplesLoading, setSamplesLoading] = useState(false);
  const [selectedSample, setSelectedSample] = useState<SelectProps.Option | null>(null);
  const [autoImportConfig, setAutoImportConfig] = useState(true);

  // Set default to active version (or first scoped version) when versions are loaded
  useEffect(() => {
    if (versions.length > 0 && !selectedVersion) {
      const versionOptions = getVersionOptions();
      const activeVersion = versions.find((v) => v.isActive);
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

  // Load the bundled sample manifest the first time the user switches to it.
  useEffect(() => {
    if (uploadSourceType === 'sample' && samples.length === 0 && !samplesLoading) {
      setSamplesLoading(true);
      listSamples()
        .then((items) => setSamples(items))
        .finally(() => setSamplesLoading(false));
    }
  }, [uploadSourceType]);

  if (!(settings as Record<string, unknown>).InputBucket) {
    return (
      <Container header={<Header variant="h2">Upload Documents</Header>}>
        <Alert type="error">Input bucket not configured</Alert>
      </Container>
    );
  }

  const handleFileChange = (files: File[]): void => {
    setSelectedFiles(files);
    setUploadStatus([]);
    setError(null);
  };

  const handlePrefixChange = ({ detail }: { detail: { value: string } }): void => {
    setPrefix(detail.value);
  };

  // The selected sample's manifest entry (or undefined).
  const activeSample = selectedSample ? samples.find((s) => s.id === selectedSample.value) : undefined;

  // Whether the sample's associated config is already present as a version.
  const configAlreadyImported = !!activeSample?.configId && versions.some((v) => v.versionName === activeSample.configId);

  // Show the auto-import checkbox only when there is an association that is not
  // yet imported.
  const showAutoImport = !!activeSample?.configId && !configAlreadyImported;

  const sampleOptions: SelectProps.Option[] = samples.map((s) => ({
    value: s.id,
    label: s.name,
    description: s.description ?? undefined,
    labelTag: s.kind === 'batch' ? `${s.fileCount ?? ''} files` : undefined,
  }));

  // When a sample with an already-imported config is selected, preselect that
  // version so the document processes with the matching config.
  const handleSampleChange = (option: SelectProps.Option | null): void => {
    setSelectedSample(option);
    setUploadStatus([]);
    setError(null);
    setAutoImportConfig(true);
    if (option) {
      const sample = samples.find((s) => s.id === option.value);
      if (sample?.configId && versions.some((v) => v.versionName === sample.configId)) {
        const opt = getVersionOptions().find((o) => o.value === sample.configId);
        if (opt) setSelectedVersion(opt);
      }
    }
  };

  // Fetch + parse a library preset (config.yaml, falling back to config.json)
  // and save it as a new, non-active configuration profile named after the
  // config folder. Returns the version name to use for processing.
  const importConfigVersion = async (configId: string): Promise<string> => {
    let file = await getFile(SAMPLE_CONFIG_PATTERN_DIR, configId, 'config.yaml');
    let isJson = false;
    if (!file) {
      file = await getFile(SAMPLE_CONFIG_PATTERN_DIR, configId, 'config.json');
      isJson = true;
    }
    if (!file) {
      throw new Error(`Could not load configuration '${configId}' from the library`);
    }
    const parsed = isJson ? JSON.parse(file.content) : yaml.load(file.content);
    if (!parsed || typeof parsed !== 'object') {
      throw new Error(`Invalid configuration format for '${configId}'`);
    }
    const result = await saveAsNewVersion(
      parsed as Record<string, unknown>,
      configId,
      `Imported from configuration library for sample document`,
    );
    if (!result.success) {
      throw new Error(result.error || `Failed to import configuration '${configId}'`);
    }
    return configId;
  };

  const uploadLocalFiles = async () => {
    if (selectedFiles.length === 0) {
      setError('Please select at least one file to upload');
      return;
    }

    setIsUploading(true);
    setUploadStatus([]);
    setError(null);

    const newUploadStatus: UploadStatusItem[] = [];

    try {
      // Use array reduce to process files sequentially
      await selectedFiles.reduce(async (previousPromise: Promise<void>, file: File) => {
        // Wait for the previous file to finish
        await previousPromise;

        try {
          // Step 1: Get presigned URL data
          console.log(`Getting upload credentials for ${file.name}...`);
          console.log(`Using prefix: ${prefix || 'none'}`);

          const response = await client.graphql({
            query: uploadDocument,
            variables: {
              fileName: file.name,
              contentType: file.type,
              prefix: prefix || '', // Use the user-provided prefix or empty string
              bucket: (settings as Record<string, unknown>).InputBucket as string, // Explicitly pass the input bucket
              version: selectedVersion?.value, // Pass selected version (optional)
              revision: selectedRevision ?? undefined, // Pin a revision (optional)
            },
          });

          const { presignedUrl, objectKey, usePostMethod } = response.data.uploadDocument;
          const usePost = usePostMethod?.toLowerCase() === 'true';

          if (!usePost) {
            throw new Error('Server returned PUT method which is not supported. Please update your backend code.');
          }

          console.log('Received presigned POST data for:', objectKey);

          // Parse the presigned post data
          const presignedPostData = JSON.parse(presignedUrl);
          console.log('Parsed presigned POST data:', presignedPostData);

          // Step 2: Upload file using FormData and POST
          console.log(`Uploading ${file.name} to S3 using POST method...`);

          const formData = new FormData();

          // Add all the fields from the presigned POST data to the form
          Object.entries(presignedPostData.fields).forEach(([key, value]) => {
            formData.append(key, value as string);
          });

          // Append the file last
          formData.append('file', file);

          // Post the form to S3
          const uploadResponse = await fetch(presignedPostData.url, {
            method: 'POST',
            body: formData,
          });

          console.log(`Upload response status: ${uploadResponse.status}`);

          if (!uploadResponse.ok) {
            console.error(`Upload failed with status: ${uploadResponse.status}`);
            // Try to get more error details
            const errorText = await uploadResponse.text().catch(() => 'Could not read error response');
            console.error(`Error details: ${errorText}`);
            throw new Error(`HTTP error! status: ${uploadResponse.status}`);
          }

          console.log(`Successfully uploaded ${file.name}`);
          newUploadStatus.push({
            file: file.name,
            status: 'success',
            objectKey,
          });
        } catch (err) {
          console.error(`Error uploading ${file.name}:`, err);
          newUploadStatus.push({
            file: file.name,
            status: 'error',
            error: err instanceof Error ? err.message : String(err),
          });
        }

        // Update status after each file
        setUploadStatus([...newUploadStatus]);
      }, Promise.resolve() as Promise<void>);
    } catch (err) {
      console.error('Error in overall upload process:', err);
      setError(`Upload process failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setIsUploading(false);
    }
  };

  const uploadSelectedSample = async () => {
    if (!activeSample) {
      setError('Please select a sample document to upload');
      return;
    }

    setIsUploading(true);
    setUploadStatus([]);
    setError(null);

    try {
      // Default to the version selected in the dropdown. When the sample has an
      // already-imported config, handleSampleChange pre-selects it, but the user
      // can override that choice — so we honor whatever is currently selected.
      let versionToUse = selectedVersion?.value;
      // Only when the sample's associated config is not yet imported and the user
      // opted into auto-import do we import it and use it for processing.
      if (activeSample.configId && autoImportConfig && !configAlreadyImported) {
        try {
          versionToUse = await importConfigVersion(activeSample.configId);
        } catch (importErr) {
          setError(`Configuration import failed: ${importErr instanceof Error ? importErr.message : String(importErr)}`);
          setIsUploading(false);
          return;
        }
      }

      const result = await uploadSample(activeSample.id, prefix || '', versionToUse);

      if (result.success) {
        setUploadStatus(result.objectKeys.map((key) => ({ file: key, status: 'success' as const, objectKey: key })));
      } else {
        setUploadStatus([{ file: activeSample.name, status: 'error', error: result.error }]);
      }
    } catch (err) {
      setError(`Upload process failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setIsUploading(false);
    }
  };

  const isSampleMode = uploadSourceType === 'sample';

  return (
    <Container header={<Header variant="h2">Upload Documents</Header>}>
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      <SpaceBetween size="l">
        <FormField label="Document source">
          <SegmentedControl
            selectedId={uploadSourceType}
            onChange={({ detail }) => {
              setUploadSourceType(detail.selectedId as UploadSource);
              setUploadStatus([]);
              setError(null);
            }}
            options={[
              { id: 'local', text: 'From my computer' },
              { id: 'sample', text: 'Sample documents' },
            ]}
          />
        </FormField>

        <FormField label="Optional folder prefix (e.g., invoices/2024)">
          <Input value={prefix} onChange={handlePrefixChange} placeholder="Leave empty for root folder" disabled={isUploading} />
        </FormField>

        <FormField label="Configuration Profile" description="Select which configuration profile to use for processing these documents">
          <Select
            selectedOption={selectedVersion}
            onChange={({ detail }) => setSelectedVersion(detail.selectedOption)}
            options={getVersionOptions()}
            placeholder={versions.length === 0 ? 'Loading profiles...' : 'Select configuration profile'}
            disabled={isUploading || versions.length === 0 || (isSampleMode && showAutoImport && autoImportConfig)}
            loadingText="Loading profiles..."
          />
        </FormField>

        <ConfigRevisionSelector
          profileName={selectedVersion?.value}
          value={selectedRevision}
          onChange={setSelectedRevision}
          disabled={isUploading}
        />

        {!isSampleMode && (
          <FormField
            label="Select files to upload"
            constraintText="Multiple files allowed. Spreadsheet, Word, and text files are converted on the backend."
          >
            <FileUpload
              onChange={({ detail }) => handleFileChange(detail.value)}
              value={selectedFiles}
              i18nStrings={{
                uploadButtonText: (multiple: boolean) => (multiple ? 'Choose files' : 'Choose file'),
                dropzoneText: (multiple: boolean) => (multiple ? 'Drop files to upload' : 'Drop file to upload'),
                removeFileAriaLabel: (fileIndex: number) => `Remove file ${fileIndex + 1}`,
                errorIconAriaLabel: 'Error',
                warningIconAriaLabel: 'Warning',
              }}
              accept={SUPPORTED_UPLOAD_EXTENSIONS}
              multiple
              showFileSize
              showFileLastModified
              {...({ showFileThumbnail: true, tokenLimit: 10, disabled: isUploading } as Record<string, unknown>)}
            />
          </FormField>
        )}

        {isSampleMode && (
          <FormField label="Select a sample document" description="Bundled sample documents you can process without downloading them first">
            <Select
              selectedOption={selectedSample}
              onChange={({ detail }) => handleSampleChange(detail.selectedOption)}
              options={sampleOptions}
              placeholder={samplesLoading ? 'Loading samples...' : 'Select a sample document'}
              disabled={isUploading || samplesLoading}
              loadingText="Loading samples..."
              statusType={samplesLoading ? 'loading' : 'finished'}
              filteringType="auto"
              empty="No sample documents available"
            />
          </FormField>
        )}

        {isSampleMode && showAutoImport && (
          <Checkbox checked={autoImportConfig} onChange={({ detail }) => setAutoImportConfig(detail.checked)} disabled={isUploading}>
            Also import and use its configuration ({activeSample?.configId})
            <Box variant="small" color="text-body-secondary">
              This sample is tuned for the &ldquo;{activeSample?.configId}&rdquo; library configuration, which is not yet imported. It will
              be saved as a new configuration profile and selected for processing.
            </Box>
          </Checkbox>
        )}

        {isSampleMode && activeSample?.configId && configAlreadyImported && (
          <Alert type="info">
            This sample is tuned for the already-imported &ldquo;{activeSample.configId}&rdquo; configuration profile, which has been
            pre-selected above. You can choose a different version if you prefer.
          </Alert>
        )}

        <Button
          variant="primary"
          onClick={isSampleMode ? uploadSelectedSample : uploadLocalFiles}
          loading={isUploading}
          disabled={isUploading || (isSampleMode ? !selectedSample : selectedFiles.length === 0)}
        >
          {isSampleMode ? 'Process sample' : `Upload ${selectedFiles.length > 0 ? `(${selectedFiles.length} files)` : ''}`}
        </Button>

        {uploadStatus.length > 0 && (
          <div>
            <h3>Upload Results:</h3>
            <SpaceBetween size="s">
              {uploadStatus.map((item, index) => (
                // eslint-disable-next-line react/no-array-index-key
                <div key={index}>
                  <StatusIndicator type={item.status === 'success' ? 'success' : 'error'}>
                    {item.file}: {item.status === 'success' ? 'Uploaded successfully' : `Failed - ${item.error}`}
                    {item.status === 'success' && item.objectKey && (
                      <div>
                        <small>Object Key: {item.objectKey}</small>
                      </div>
                    )}
                  </StatusIndicator>
                </div>
              ))}
            </SpaceBetween>
          </div>
        )}
      </SpaceBetween>
    </Container>
  );
};

export default UploadDocumentPanel;
