// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useEffect, useState } from 'react';
import type { SelectProps, IconProps } from '@cloudscape-design/components';
import { Container, Header, SpaceBetween, Button, FormField, Select, Alert, Textarea, Input } from '@cloudscape-design/components';
import { generateClient } from '../../api/client-shim';
import { ConsoleLogger } from 'aws-amplify/utils';
import { startTestRun, getTestSets, getTestSetVersions } from '../../graphql/generated';
import handlePrint from './PrintUtils';
import useConfigurationVersions from '../../hooks/use-configuration-versions';
import ConfigRevisionSelector from '../common/ConfigRevisionSelector';
import { getErrorMessage } from '../../utils/errorUtils';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type GqlResult = { data: Record<string, any> };

const client = generateClient();
const logger = new ConsoleLogger('TestRunner');

interface ActiveTestRun {
  testRunId: string;
  testSetName: string;
  startTime: Date;
}

interface TestSetData {
  id: string;
  name: string;
  filePattern?: string;
  fileCount: number;
  status: string;
  /** Config profile this test set declares for itself (optional). */
  configVersion?: string | null;
}

interface TestRunnerProps {
  onTestStart: (testRunId: string, testSetName: string, context: string, filesCount: number, configVersion?: string) => void;
  onTestComplete: (testRunId: string) => void;
  activeTestRuns: ActiveTestRun[];
}

const TestRunner = ({
  onTestStart,
  onTestComplete: _onTestComplete,
  activeTestRuns: _activeTestRuns,
}: TestRunnerProps): React.JSX.Element => {
  const [testSets, setTestSets] = useState<TestSetData[]>([]);
  const [testSetsLoading, setTestSetsLoading] = useState(true);
  const [selectedTestSet, setSelectedTestSet] = useState<SelectProps.Option | null>(null);
  const [selectedVersion, setSelectedVersion] = useState<SelectProps.Option | null>(null);
  // null = the profile's current configuration. Pinning an explicit revision is
  // what makes two runs of the same profile comparable.
  const [selectedRevision, setSelectedRevision] = useState<number | null>(null);
  /**
   * Which labels to score against. Null is the set's current labels, including any
   * annotation in progress — the loop the review-effort panel invites. A number pins a
   * published version, the counterpart of pinning a configuration revision above:
   * both are what make two runs comparable once the thing they measure has moved.
   */
  const [testSetVersions, setTestSetVersions] = useState<{ version: number; label?: string | null }[]>([]);
  const [selectedTestSetVersion, setSelectedTestSetVersion] = useState<number | null>(null);

  useEffect(() => {
    setSelectedTestSetVersion(null);
    setTestSetVersions([]);
    const id = selectedTestSet?.value;
    if (!id) return;
    let cancelled = false;
    (async () => {
      try {
        const result = (await client.graphql({ query: getTestSetVersions, variables: { testSetId: id } })) as {
          data?: { getTestSetVersions?: ({ version?: number | null; label?: string | null } | null)[] | null };
        };
        if (cancelled) return;
        const versions = (result.data?.getTestSetVersions ?? [])
          .filter((v): v is { version: number; label?: string | null } => v?.version != null)
          .sort((a, b) => b.version - a.version);
        setTestSetVersions(versions);
      } catch (err) {
        // The picker degrades to "current labels" only; a run is still possible.
        logger.debug('Could not load test set versions:', err);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedTestSet?.value]);

  const CURRENT_LABELS = '__current__';
  const testSetVersionOptions: SelectProps.Option[] = [
    { value: CURRENT_LABELS, label: 'Current labels', description: 'Including any annotation in progress' },
    ...testSetVersions.map((v) => ({
      value: String(v.version),
      label: `v${v.version}`,
      description: v.label ?? undefined,
    })),
  ];
  const selectedTestSetVersionOption =
    testSetVersionOptions.find((o) => o.value === (selectedTestSetVersion === null ? CURRENT_LABELS : String(selectedTestSetVersion))) ??
    testSetVersionOptions[0];
  const [numberOfFiles, setNumberOfFiles] = useState('');
  const [context, setContext] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const { versions, loading: _versionsLoading, getVersionOptions } = useConfigurationVersions();

  // Set default to active version (or first scoped version) when versions are loaded
  React.useEffect(() => {
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

  // Set default context when test set, version, or numberOfFiles changes
  React.useEffect(() => {
    if (selectedTestSet && selectedVersion) {
      const testSetName = (selectedTestSet.label ?? '').split(' - ')[0]; // Extract name without file count
      const versionName = selectedVersion.value; // Use value instead of label to avoid "(Active)"
      const testSetData = testSets.find((ts) => ts.id === selectedTestSet.value);
      const totalFiles = testSetData?.fileCount || 0;
      const filesToProcess = numberOfFiles.trim() ? parseInt(numberOfFiles.trim(), 10) : totalFiles;
      const defaultContext = `Test set: ${testSetName} using version (${versionName}) with ${filesToProcess} files`;
      setContext(defaultContext);
    }
  }, [selectedTestSet, selectedVersion, testSets, numberOfFiles]);

  const loadTestSets = async () => {
    try {
      console.log('TestRunner: Loading test sets...');
      const result = (await client.graphql({ query: getTestSets })) as GqlResult;
      console.log('TestRunner: GraphQL result:', result);
      const testSetsData = result.data.getTestSets || [];
      console.log('TestRunner: Test sets data:', testSetsData);
      setTestSets(testSetsData);
    } catch (err) {
      console.error('TestRunner: Failed to load test sets:', err);
      setError(`Failed to load test sets: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setTestSetsLoading(false);
    }
  };

  React.useEffect(() => {
    loadTestSets();
  }, []);

  const handleRunTest = async () => {
    if (!selectedTestSet) {
      setError('Please select a test set');
      return;
    }

    // Get the selected test set data to validate numberOfFiles
    const testSetData = testSets.find((ts) => ts.id === selectedTestSet.value);
    const maxFiles = testSetData?.fileCount || 0;

    let _filesToProcess = maxFiles;
    if (numberOfFiles.trim()) {
      const numFiles = parseInt(numberOfFiles.trim(), 10);
      if (isNaN(numFiles) || numFiles <= 0) {
        setError('Number of files must be a positive integer');
        return;
      }
      if (numFiles > maxFiles) {
        setError(`Number of files cannot exceed ${maxFiles} (total files in test set)`);
        return;
      }
      _filesToProcess = numFiles;
    }

    setLoading(true);
    try {
      const input = {
        testSetId: selectedTestSet.value ?? '',
        ...(context && { context }),
        ...(numberOfFiles.trim() && { numberOfFiles: parseInt(numberOfFiles.trim(), 10) }),
        ...(selectedVersion && { configVersion: selectedVersion.value }),
        ...(selectedRevision !== null && { configRevision: selectedRevision }),
        ...(selectedTestSetVersion !== null && { testSetVersion: selectedTestSetVersion }),
      };
      console.log('TestRunner: Starting test run with input:', input);

      const result = (await client.graphql({
        query: startTestRun,
        variables: { input },
      })) as GqlResult;

      console.log('TestRunner: GraphQL result:', result);

      if (!result?.data?.startTestRun) {
        throw new Error('No response data from startTestRun mutation');
      }

      logger.info('Test run started:', result.data.startTestRun);
      onTestStart(
        result.data.startTestRun.testRunId,
        result.data.startTestRun.testSetName,
        context,
        result.data.startTestRun.filesCount,
        selectedVersion?.value,
      );
      setError('');
    } catch (err) {
      logger.error('Failed to start test run:', err);
      const errObj = err as { message?: string; errors?: { message: string }[]; networkError?: unknown; graphQLErrors?: unknown };
      console.error('TestRunner: Error details:', {
        message: errObj.message,
        errors: errObj.errors,
        networkError: errObj.networkError,
        graphQLErrors: errObj.graphQLErrors,
      });

      setError(getErrorMessage(err));
    } finally {
      setLoading(false);
    }
  };

  const testSetOptions = testSets
    .filter((testSet) => testSet.status === 'COMPLETED')
    .map((testSet) => ({
      label: `${testSet.name}${testSet.filePattern ? ` (${testSet.filePattern})` : ''} - ${testSet.fileCount} ${
        testSet.fileCount === 1 ? 'file' : 'files'
      }`,
      value: testSet.id,
      description: testSet.filePattern ? `Pattern: ${testSet.filePattern}` : 'Uploaded test set',
    }));

  return (
    <Container
      header={
        <Header
          variant="h2"
          description="Select a test set and execute test runs for document processing"
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="primary" onClick={handleRunTest} loading={loading} disabled={!selectedTestSet}>
                Run Test
              </Button>
              <Button onClick={handlePrint} iconName={'print' as unknown as IconProps.Name}>
                Print
              </Button>
            </SpaceBetween>
          }
        >
          Run Test Set
        </Header>
      }
    >
      <SpaceBetween size="l">
        {error && (
          <Alert type="error" dismissible onDismiss={() => setError('')}>
            {error}
          </Alert>
        )}

        <FormField label="Select Test Set" description="Choose an existing test set to run">
          <Select
            selectedOption={selectedTestSet}
            onChange={({ detail }) => {
              setSelectedTestSet(detail.selectedOption);
              setNumberOfFiles(''); // Reset numberOfFiles when test set changes
              // Auto-select the configuration profile for the chosen test set, in
              // priority order:
              //
              //   1. `configVersion` declared ON the test set record. Lets a test
              //      set name its own config explicitly — required for test sets
              //      deployed by an extension, whose config presets are named
              //      `<featureId>-v<version>` by the Feature Platform and so can
              //      never equal the test set id.
              //   2. A configuration profile whose name EQUALS the test set id. The
              //      convention the stack-managed benchmark sets rely on
              //      (e.g. "fake-w2", "docsplit").
              //   3. The active version — nothing specific applies.
              //
              // A declared version that isn't in the dropdown (deleted, or out of
              // the caller's config-version scope) falls through to 2 then 3
              // rather than leaving the field empty.
              const testSetData = testSets.find((ts) => ts.id === detail.selectedOption.value);
              const versionOptions = getVersionOptions();
              for (const candidate of [testSetData?.configVersion, testSetData?.id]) {
                if (!candidate) continue;
                const matchOption = versionOptions.find((opt) => opt.value === candidate);
                if (matchOption) {
                  setSelectedVersion(matchOption);
                  return;
                }
              }
              const activeVersion = versions.find((v) => v.isActive);
              const activeOption = activeVersion ? versionOptions.find((opt) => opt.value === activeVersion.versionName) : null;
              setSelectedVersion(activeOption ?? versionOptions[0] ?? null);
            }}
            options={testSetOptions}
            placeholder="Choose a test set..."
            statusType={testSetsLoading ? 'loading' : 'finished'}
            loadingText="Loading test sets..."
            empty="No test sets available"
          />
        </FormField>

        <FormField
          label="Test set version"
          description="Defaults to the set’s current labels, including any annotation in progress. Pin a published version to score exactly the labels it preserved — that is how two runs of the same set stay comparable once its ground truth has moved."
        >
          <Select
            selectedOption={selectedTestSetVersionOption}
            onChange={({ detail }) =>
              setSelectedTestSetVersion(detail.selectedOption.value === CURRENT_LABELS ? null : Number(detail.selectedOption.value))
            }
            options={testSetVersionOptions}
            disabled={loading || !selectedTestSet}
          />
        </FormField>

        <FormField
          label="Configuration Profile"
          description="Select which configuration profile to use for processing these test documents"
        >
          <Select
            selectedOption={selectedVersion}
            onChange={({ detail }) => setSelectedVersion(detail.selectedOption)}
            options={getVersionOptions()}
            placeholder={versions.length === 0 ? 'Loading profiles...' : 'Select configuration profile'}
            disabled={loading || versions.length === 0}
            loadingText="Loading profiles..."
          />
        </FormField>

        <ConfigRevisionSelector
          profileName={selectedVersion?.value}
          value={selectedRevision}
          onChange={setSelectedRevision}
          description="Defaults to the profile’s current configuration. Pick an earlier revision to score exactly what it recorded — that is how two runs of the same profile stay comparable."
          disabled={loading}
        />

        <FormField
          label="Number of Files"
          // 'max: 0' before a set is chosen reads as a hard limit of zero, i.e.
          // "you cannot run this" — when in fact no maximum is known yet.
          description={(() => {
            if (!selectedTestSet) return 'Optional: Limit the number of files to process. Choose a test set to see its maximum.';
            const fileCount = testSets.find((ts) => ts.id === selectedTestSet.value)?.fileCount;
            return fileCount
              ? `Optional: Limit the number of files to process (max: ${fileCount})`
              : 'Optional: Limit the number of files to process.';
          })()}
        >
          <Input
            value={numberOfFiles}
            onChange={({ detail }) => {
              const value = detail.value;
              const maxFiles = selectedTestSet ? testSets.find((ts) => ts.id === selectedTestSet.value)?.fileCount || 0 : 0;

              // Allow empty value
              if (value === '') {
                setNumberOfFiles('');
                return;
              }

              // Only allow digits (reject any non-digit characters)
              if (!/^\d+$/.test(value)) {
                return; // Don't update state if invalid characters
              }

              // Check range
              const num = parseInt(value, 10);
              if (num > 0 && num <= maxFiles) {
                setNumberOfFiles(value);
              }
              // If number is too large, don't update the state (prevents typing)
            }}
            placeholder={
              selectedTestSet
                ? `Enter 1-${testSets.find((ts) => ts.id === selectedTestSet.value)?.fileCount || 0}`
                : 'Select a test set first'
            }
            disabled={!selectedTestSet}
            type="text"
            inputMode="numeric"
          />
        </FormField>

        <FormField
          label="Context"
          description="Optional context information for this test run"
          errorText={context && context.length > 500 ? 'Context cannot exceed 500 characters' : ''}
        >
          <Textarea
            value={context}
            onChange={({ detail }) => setContext(detail.value)}
            placeholder="Enter context information..."
            rows={2}
            invalid={!!context && context.length > 500}
          />
        </FormField>
      </SpaceBetween>
    </Container>
  );
};

export default TestRunner;
