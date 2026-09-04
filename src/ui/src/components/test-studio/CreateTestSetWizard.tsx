// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * CreateTestSetWizard — the single guided path for creating a test set.
 *
 * Steps are named for the outcome (what you end up with) rather than the
 * mechanism, and the source is one explicit choice. Every create/add entry point
 * routes through here so the flows cannot drift apart.
 */

import React, { useState } from 'react';
import {
  Alert,
  Box,
  Button,
  ColumnLayout,
  ExpandableSection,
  FileUpload,
  FormField,
  Input,
  KeyValuePairs,
  Link,
  Modal,
  Select,
  SpaceBetween,
  StatusIndicator,
  Tiles,
  Wizard,
} from '@cloudscape-design/components';
import type { SelectProps } from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from '../../api/client-shim';
import { addTestSet, addTestSetFromUpload, listBucketFiles, validateTestFileName } from '../../graphql/generated';
import { getErrorMessage } from '../../utils/errorUtils';
import { DISCOVERY_PATH } from '../../routes/constants';
import useGenerateSyntheticForm from './useGenerateSyntheticForm';
import {
  BUCKET_OPTIONS,
  CREATE_SOURCES,
  DOCUMENT_CLASS_TYPE_OPTIONS,
  TIME_FILTER_OPTIONS,
  validateDescription,
  validateTestSetName,
} from './testSetOptions';
import type { CreateSource } from './testSetOptions';

const client = generateClient();
const logger = new ConsoleLogger('CreateTestSetWizard');

type DocumentClassType = 'SINGLE_CLASS' | 'MULTI_CLASS' | 'PACKET_SPLITTING';

interface CreateTestSetWizardProps {
  visible: boolean;
  onDismiss: () => void;
  /** Called with a human-readable summary after a successful create. */
  onCreated: (message: string) => void;
  /** True when the synthetic-data generator extension is installed. */
  generatorAvailable: boolean;
  /**
   * Called when a generation job starts. Generation is async, so the wizard
   * hands the job back for the caller's progress banner rather than reporting a
   * finished test set.
   */
  onGenerationStarted: (jobId: string, label: string, testSetId: string) => void;
}

const REQUIRED_STRUCTURE = `my-test-set.zip
├── input/
│   ├── document1.pdf
│   └── document2.pdf
└── baseline/                    (omit for "documents only")
    ├── document1.pdf/
    │   └── sections/1/result.json
    └── document2.pdf/
        └── sections/1/result.json`;

const CreateTestSetWizard = ({
  visible,
  onDismiss,
  onCreated,
  generatorAvailable,
  onGenerationStarted,
}: CreateTestSetWizardProps): React.JSX.Element => {
  const [activeStep, setActiveStep] = useState(0);
  const [source, setSource] = useState<CreateSource>('upload-labeled');

  // Shared fields
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [documentClassType, setDocumentClassType] = useState<SelectProps.Option>(DOCUMENT_CLASS_TYPE_OPTIONS[0]);

  // Upload fields
  const [files, setFiles] = useState<File[]>([]);

  // Pattern fields
  const [filePattern, setFilePattern] = useState('');
  const [bucket, setBucket] = useState<SelectProps.Option>(BUCKET_OPTIONS[0]);
  const [timeFilter, setTimeFilter] = useState<SelectProps.Option>(TIME_FILTER_OPTIONS[0]);
  const [customDate, setCustomDate] = useState('');
  const [customTime, setCustomTime] = useState('00:00:00');
  const [fileCount, setFileCount] = useState(0);
  const [isChecking, setIsChecking] = useState(false);

  const [error, setError] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);

  const isUpload = source === 'upload-labeled' || source === 'upload-documents';
  const isGenerate = source === 'generate';

  // Shared with the standalone deep-link modal so the two entry points cannot
  // drift. Gated on the branch: inactive it fetches no estimates or test sets.
  const generateForm = useGenerateSyntheticForm({ active: visible && isGenerate });

  const reset = () => {
    setActiveStep(0);
    setSource('upload-labeled');
    setName('');
    setDescription('');
    setDocumentClassType(DOCUMENT_CLASS_TYPE_OPTIONS[0]);
    setFiles([]);
    setFilePattern('');
    setBucket(BUCKET_OPTIONS[0]);
    setTimeFilter(TIME_FILTER_OPTIONS[0]);
    setCustomDate('');
    setCustomTime('00:00:00');
    setFileCount(0);
    setError('');
  };

  const close = () => {
    reset();
    onDismiss();
  };

  const getModifiedAfter = (): string | undefined => {
    if (!timeFilter.value) return undefined;
    if (timeFilter.value === 'custom') {
      return customDate ? new Date(`${customDate}T${customTime}`).toISOString() : undefined;
    }
    const hours = parseInt(timeFilter.value, 10);
    return new Date(Date.now() - hours * 3600 * 1000).toISOString();
  };

  const handleCheckFiles = async () => {
    if (!filePattern.trim()) {
      setError('Enter a file pattern first');
      return;
    }
    setIsChecking(true);
    setError('');
    try {
      const result = await client.graphql({
        query: listBucketFiles,
        variables: {
          bucketType: bucket.value ?? '',
          filePattern: filePattern.trim(),
          modifiedAfter: getModifiedAfter(),
        },
      });
      const matched = (result.data?.listBucketFiles ?? []).filter((f): f is string => f !== null);
      setFileCount(matched.length);
      if (matched.length === 0) setError('No files matched that pattern');
    } catch (err) {
      logger.error('Error checking files:', err);
      setError(`Could not check files: ${getErrorMessage(err)}`);
    } finally {
      setIsChecking(false);
    }
  };

  /** Server-side name check; returns false when the caller should stop. */
  const nameIsFree = async (): Promise<boolean> => {
    try {
      const result = await client.graphql({
        query: validateTestFileName,
        variables: { fileName: name.trim() },
      });
      const validation = result.data.validateTestFileName;
      if (validation?.exists) {
        setError(
          `A test set with the id "${validation.testSetId}" already exists. Choose a different name, or delete the existing set first.`,
        );
        return false;
      }
      return true;
    } catch (err) {
      logger.error('Error validating test set name:', err);
      setError(`Could not validate the name: ${getErrorMessage(err)}`);
      return false;
    }
  };

  const submitUpload = async () => {
    const zip = files[0];
    if (!zip) {
      setError('Choose a zip file');
      return;
    }
    // `name` is load-bearing: without it the resolver derives the set's name from the
    // zip's filename, so "my-test-set" + Archive.zip produced a set called "Archive"
    // while the toast below reported the name the server never received.
    const input: {
      fileName: string;
      fileSize: number;
      name: string;
      description: string;
      documentClassType?: DocumentClassType;
    } = { fileName: zip.name, fileSize: zip.size, name: name.trim(), description: description.trim() };
    if (documentClassType.value) {
      input.documentClassType = documentClassType.value as DocumentClassType;
    }

    const result = await client.graphql({ query: addTestSetFromUpload, variables: { input } });
    const response = result.data.addTestSetFromUpload;
    if (!response?.presignedUrl) throw new Error('The server did not return an upload URL');

    const presigned = JSON.parse(response.presignedUrl);
    const formData = new FormData();
    Object.entries(presigned.fields as Record<string, string>).forEach(([key, value]) => formData.append(key, value));
    formData.append('file', zip);
    const uploadResponse = await fetch(presigned.url, { method: 'POST', body: formData });
    if (!uploadResponse.ok) {
      throw new Error(`Upload failed: ${uploadResponse.status} ${uploadResponse.statusText}`);
    }

    // Named from the response, not from local state. This previously interpolated the
    // form's `name` — which was never sent — so it confirmed a name the server had not
    // used. Reporting the id the server actually created cannot drift from what happened.
    const createdId = response.testSetId;
    onCreated(
      source === 'upload-documents'
        ? `Test set "${name.trim()}" created as ${createdId}. Once the zip is processed, use "Generate draft labels" to label it.`
        : `Test set "${name.trim()}" created as ${createdId}. The zip is being processed.`,
    );
  };

  const submitPattern = async () => {
    const variables: {
      name: string;
      description: string;
      filePattern: string;
      bucketType: string;
      fileCount: number;
      modifiedAfter: string | undefined;
      documentClassType?: DocumentClassType;
    } = {
      name: name.trim(),
      description: description.trim(),
      filePattern: filePattern.trim(),
      bucketType: bucket.value ?? '',
      fileCount,
      modifiedAfter: getModifiedAfter(),
    };
    if (documentClassType.value) {
      variables.documentClassType = documentClassType.value as DocumentClassType;
    }
    await client.graphql({ query: addTestSet, variables });
    onCreated(`Test set "${name.trim()}" created from ${fileCount} matching file(s).`);
  };

  const handleSubmit = async () => {
    setError('');
    if (isGenerate) {
      const started = await generateForm.submit();
      if (started) {
        onGenerationStarted(started.jobId, started.label, started.testSetId);
        close();
      }
      return;
    }
    if (!name.trim()) {
      setError('A name is required');
      return;
    }
    if (!validateTestSetName(name.trim())) {
      setError('Name can only contain letters, numbers, spaces, hyphens and underscores (max 50 characters)');
      return;
    }
    if (description && !validateDescription(description.trim())) {
      setError('Description cannot exceed 500 characters');
      return;
    }
    if (!(await nameIsFree())) return;

    setIsSubmitting(true);
    try {
      if (isUpload) await submitUpload();
      else await submitPattern();
      close();
    } catch (err) {
      logger.error('Error creating test set:', err);
      setError(`Could not create the test set: ${getErrorMessage(err)}`);
    } finally {
      setIsSubmitting(false);
    }
  };

  const sourceMeta = CREATE_SOURCES.find((s) => s.value === source);

  /**
   * The configuration prerequisite, stated on Configure rather than the source
   * step: by then the source is known, so the note names what that source needs.
   * Uploading verified labels needs no configuration; uploading bare documents
   * does, because draft labeling has to be told what to extract.
   */
  const configPrerequisite =
    source === 'upload-labeled' ? null : (
      <Alert type="info" header={source === 'generate' ? 'Generation needs a configuration' : 'Labeling needs a configuration'}>
        <SpaceBetween size="xxs">
          <Box>
            {source === 'generate'
              ? 'Synthetic documents are generated from a configuration — the document classes and fields it defines are what gets created and labeled.'
              : source === 'existing-files'
                ? 'Documents without an existing baseline arrive unlabeled. Generating draft labels for those needs a configuration that says what to extract: the fields you care about, per document class.'
                : 'These documents arrive without ground truth. Generating draft labels for them needs a configuration that says what to extract: the fields you care about, per document class.'}
          </Box>
          <Box fontSize="body-s">
            No configuration yet? <Link href={`#${DISCOVERY_PATH}`}>Discovery</Link> infers classes and fields from example documents, or
            use Quick Start from the welcome page to describe them in your own words.
          </Box>
        </SpaceBetween>
      </Alert>
    );

  const sourceStep = (
    <SpaceBetween size="l">
      <FormField label="How do you want to create this test set?" stretch>
        <Tiles
          value={source}
          onChange={({ detail }) => {
            setSource(detail.value as CreateSource);
            setError('');
          }}
          items={CREATE_SOURCES.filter((s) => s.value !== 'generate' || generatorAvailable).map((s) => ({
            value: s.value,
            label: s.label,
            description: `${s.description} → ${s.outcome}`,
          }))}
        />
      </FormField>
      {!generatorAvailable && (
        <Box fontSize="body-s" color="text-body-secondary">
          Synthetic generation needs the data-generator extension installed.
        </Box>
      )}
    </SpaceBetween>
  );

  // Generation collects its own destination name, which may be an existing set, so
  // it skips the shared name/description/class fields.
  const configureStep = isGenerate ? (
    <SpaceBetween size="l">
      {configPrerequisite}
      {generateForm.fields}
    </SpaceBetween>
  ) : (
    <SpaceBetween size="l">
      {error && <Alert type="error">{error}</Alert>}
      {configPrerequisite}

      <FormField
        label="Name"
        description="Letters, numbers, spaces, hyphens and underscores. Becomes the test set id."
        errorText={name && !validateTestSetName(name) ? 'Invalid name' : ''}
      >
        <Input value={name} onChange={({ detail }) => setName(detail.value)} placeholder="my-invoices-benchmark" />
      </FormField>

      <FormField label="Description — optional" errorText={description && !validateDescription(description) ? 'Too long' : ''}>
        <Input value={description} onChange={({ detail }) => setDescription(detail.value)} placeholder="What this set covers" />
      </FormField>

      <FormField label="Document classification type — optional" description="Metadata describing the mix of documents in this set.">
        <Select
          selectedOption={documentClassType}
          onChange={({ detail }) => setDocumentClassType(detail.selectedOption)}
          options={DOCUMENT_CLASS_TYPE_OPTIONS}
        />
      </FormField>

      {isUpload && (
        <FormField
          label="Zip file"
          description={
            source === 'upload-documents'
              ? 'A zip with an input/ folder. No baseline/ folder is needed — you will generate draft labels next.'
              : 'A zip with input/ and matching baseline/ folders.'
          }
        >
          <SpaceBetween size="s">
            <FileUpload
              onChange={({ detail }) => {
                setFiles(detail.value);
                setError('');
              }}
              value={files}
              accept=".zip"
              showFileSize
              constraintText="Single .zip file"
              i18nStrings={{
                uploadButtonText: () => 'Choose zip file',
                dropzoneText: () => 'Drop a zip file to upload',
                removeFileAriaLabel: (i: number) => `Remove file ${i + 1}`,
                errorIconAriaLabel: 'Error',
                warningIconAriaLabel: 'Warning',
              }}
            />
            <ExpandableSection headerText="Required zip structure" variant="footer">
              <Box variant="code">
                <pre style={{ margin: 0, fontSize: 12, whiteSpace: 'pre-wrap' }}>{REQUIRED_STRUCTURE}</pre>
              </Box>
            </ExpandableSection>
          </SpaceBetween>
        </FormField>
      )}

      {source === 'existing-files' && (
        <>
          <FormField label="Bucket" description="Where to look for the documents.">
            <Select selectedOption={bucket} onChange={({ detail }) => setBucket(detail.selectedOption)} options={BUCKET_OPTIONS} />
          </FormField>
          <FormField label="File pattern" description="For example *.pdf, or invoices/2024-*.pdf">
            <Input value={filePattern} onChange={({ detail }) => setFilePattern(detail.value)} placeholder="*.pdf" />
          </FormField>
          <FormField label="Modified after — optional" description="Useful for picking up only recently reviewed documents.">
            <Select
              selectedOption={timeFilter}
              onChange={({ detail }) => setTimeFilter(detail.selectedOption)}
              options={TIME_FILTER_OPTIONS}
            />
          </FormField>
          {timeFilter.value === 'custom' && (
            <ColumnLayout columns={2}>
              <FormField label="Date">
                <Input value={customDate} onChange={({ detail }) => setCustomDate(detail.value)} placeholder="YYYY-MM-DD" />
              </FormField>
              <FormField label="Time">
                <Input value={customTime} onChange={({ detail }) => setCustomTime(detail.value)} placeholder="00:00:00" />
              </FormField>
            </ColumnLayout>
          )}
          <SpaceBetween direction="horizontal" size="xs" alignItems="center">
            <Button onClick={handleCheckFiles} loading={isChecking}>
              Check matching files
            </Button>
            {fileCount > 0 && <StatusIndicator type="success">{fileCount} file(s) match</StatusIndicator>}
          </SpaceBetween>
          {bucket.value === 'input' && (
            <Alert type="info">
              Documents without ground truth in the evaluation baseline bucket are skipped rather than failing, so a broad pattern is safe.
            </Alert>
          )}
        </>
      )}
    </SpaceBetween>
  );

  const reviewStep = isGenerate ? (
    <SpaceBetween size="l">
      {generateForm.error && (
        <Alert type="error" header="Generation failed">
          {generateForm.error}
        </Alert>
      )}
      <KeyValuePairs columns={2} items={generateForm.summary} />
      <Alert type="info" header="This starts a background job">
        Generation runs on Amazon Bedrock and takes a few minutes. The test set appears in the list when it completes; you can close this
        and keep working.
      </Alert>
    </SpaceBetween>
  ) : (
    <SpaceBetween size="l">
      {error && <Alert type="error">{error}</Alert>}
      <KeyValuePairs
        columns={2}
        items={[
          { label: 'Source', value: sourceMeta?.label ?? source },
          { label: 'You will have', value: sourceMeta?.outcome ?? '' },
          { label: 'Name', value: name || '—' },
          { label: 'Description', value: description || '—' },
          { label: 'Classification type', value: documentClassType.label ?? 'Unspecified' },
          ...(isUpload
            ? [{ label: 'Zip file', value: files[0]?.name ?? '—' }]
            : [
                { label: 'Bucket', value: bucket.label ?? '' },
                { label: 'Pattern', value: filePattern || '—' },
                { label: 'Matching files', value: fileCount > 0 ? String(fileCount) : 'not checked' },
              ]),
        ]}
      />
      {source === 'upload-documents' && (
        <Alert type="info" header="Next step after this">
          This set arrives without ground truth. Open it and choose <strong>Generate draft labels</strong>, then review the documents with
          the most confidence alerts.
        </Alert>
      )}
    </SpaceBetween>
  );

  return (
    <Modal visible={visible} onDismiss={close} header="Create or update test set" size="large">
      <Wizard
        activeStepIndex={activeStep}
        onNavigate={({ detail }) => {
          // Validate on forward navigation so incomplete input is caught per step
          // rather than at submit.
          if (detail.requestedStepIndex > activeStep) {
            if (activeStep === 1 && isGenerate) {
              if (!generateForm.canSubmit) {
                setError('Complete the required fields to continue');
                return;
              }
            } else if (activeStep === 1) {
              if (!name.trim() || !validateTestSetName(name.trim())) {
                setError('Enter a valid name to continue');
                return;
              }
              if (isUpload && files.length === 0) {
                setError('Choose a zip file to continue');
                return;
              }
              if (source === 'existing-files' && !filePattern.trim()) {
                setError('Enter a file pattern to continue');
                return;
              }
            }
          }
          setError('');
          setActiveStep(detail.requestedStepIndex);
        }}
        onCancel={close}
        onSubmit={handleSubmit}
        isLoadingNextStep={isSubmitting || generateForm.submitting}
        i18nStrings={{
          stepNumberLabel: (n) => `Step ${n}`,
          collapsedStepsLabel: (n, total) => `Step ${n} of ${total}`,
          cancelButton: 'Cancel',
          previousButton: 'Previous',
          nextButton: 'Next',
          // "Create", not "Create test set": the page behind this wizard has its own
          // "Create test set" button, so both were on screen at once reading identically.
          // Position disambiguates them for a sighted user and nothing does for a screen
          // reader — and it is genuinely ambiguous, since one opens the wizard and the
          // other commits it. The wizard's own footer has all the context it needs.
          submitButton: isGenerate ? 'Generate documents' : 'Create',
          optional: 'optional',
        }}
        steps={[
          { title: 'Choose a source', content: sourceStep },
          { title: 'Configure', content: configureStep },
          { title: 'Review and create', content: reviewStep },
        ]}
      />
    </Modal>
  );
};

export default CreateTestSetWizard;
