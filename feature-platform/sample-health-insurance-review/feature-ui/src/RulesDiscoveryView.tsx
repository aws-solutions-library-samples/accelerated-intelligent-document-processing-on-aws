// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Container,
  ExpandableSection,
  FileUpload,
  Header,
  SpaceBetween,
  StatusIndicator,
  Table,
} from '@cloudscape-design/components';
import type { StatusIndicatorProps } from '@cloudscape-design/components';

import {
  getPolicyClasses,
  graphqlErrorMessage,
  listRulesDiscoveryJobs,
  uploadDiscoveryDocument,
  uploadToS3,
  type DiscoveryJob,
  type PolicyClass,
} from './hostGraphql';

interface RulesDiscoveryViewProps {
  /** Discovery bucket name (from the feature API's /config route). */
  discoveryBucket: string | null;
  /** Config version the discovered policy classes are written into. */
  configVersion: string;
  enabled: boolean;
}

const TERMINAL_OK = new Set(['COMPLETED', 'SUCCEEDED', 'SUCCESS']);
const TERMINAL_BAD = new Set(['FAILED', 'ERROR']);

function jobIndicator(status: string): StatusIndicatorProps.Type {
  if (TERMINAL_OK.has(status)) return 'success';
  if (TERMINAL_BAD.has(status)) return 'error';
  return 'in-progress';
}

const RulesDiscoveryView: React.FC<RulesDiscoveryViewProps> = ({
  discoveryBucket,
  configVersion,
  enabled,
}) => {
  const [files, setFiles] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  const [phase, setPhase] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [jobs, setJobs] = useState<DiscoveryJob[]>([]);
  const [policyClasses, setPolicyClasses] = useState<PolicyClass[]>([]);
  const [loadingRules, setLoadingRules] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const loadJobs = useCallback(async () => {
    try {
      setJobs(await listRulesDiscoveryJobs());
    } catch (e) {
      setError(graphqlErrorMessage(e));
    }
  }, []);

  const loadRules = useCallback(async () => {
    setLoadingRules(true);
    try {
      setPolicyClasses(await getPolicyClasses(configVersion));
    } catch (e) {
      setError(graphqlErrorMessage(e));
    } finally {
      setLoadingRules(false);
    }
  }, [configVersion]);

  useEffect(() => {
    if (enabled) {
      loadJobs();
      loadRules();
    }
  }, [enabled, loadJobs, loadRules]);

  // Poll the job list while any rules job is still running (simpler than the
  // onDiscoveryJobStatusChange subscription for a sample; the subscription is
  // the production-grade alternative). Reload rules when a job completes.
  useEffect(() => {
    const anyRunning = jobs.some(
      (j) => !TERMINAL_OK.has(j.status) && !TERMINAL_BAD.has(j.status),
    );
    if (anyRunning && !pollRef.current) {
      pollRef.current = setInterval(() => {
        loadJobs();
      }, 5000);
    } else if (!anyRunning && pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
      loadRules();
    }
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [jobs, loadJobs, loadRules]);

  const startDiscovery = useCallback(async () => {
    if (files.length === 0) return;
    if (!discoveryBucket) {
      setError('No Discovery bucket available from the host.');
      return;
    }
    const file = files[0];
    setUploading(true);
    setError(null);
    try {
      setPhase('Requesting upload…');
      const upload = await uploadDiscoveryDocument({
        fileName: file.name,
        contentType: file.type || 'application/pdf',
        bucket: discoveryBucket,
        version: configVersion,
      });
      if (upload.usePostMethod?.toLowerCase() !== 'true') {
        throw new Error('Host returned an unsupported upload method (expected POST).');
      }
      setPhase('Uploading document to S3…');
      await uploadToS3(file, upload.presignedUrl);
      setPhase('Discovery job started. Refreshing…');
      setFiles([]);
      await loadJobs();
    } catch (e) {
      const msg = graphqlErrorMessage(e);
      // uploadDiscoveryDocument is Admin/Author only — surface a friendly hint.
      setError(
        /unauthor|not author|forbidden|access denied/i.test(msg)
          ? 'Rules Discovery requires the Admin or Author role. ' +
              'Ask an administrator to run discovery, or switch to an ' +
              'Admin/Author account.'
          : msg,
      );
    } finally {
      setUploading(false);
      setPhase('');
    }
  }, [files, discoveryBucket, configVersion, loadJobs]);

  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)}>
          {error}
        </Alert>
      )}

      <Container
        header={
          <Header
            variant="h2"
            description={
              `Upload a payer policy document of your own. ` +
              `The host runs Rules Discovery and writes the extracted ` +
              `validation rules into config version "${configVersion}".`
            }
          >
            Discover rules from a policy document
          </Header>
        }
      >
        <SpaceBetween size="m">
          <FileUpload
            onChange={({ detail }) => setFiles(detail.value)}
            value={files}
            accept="application/pdf"
            i18nStrings={{
              uploadButtonText: () => 'Choose policy PDF',
              dropzoneText: () => 'Drop a policy PDF here',
              removeFileAriaLabel: (i) => `Remove file ${i + 1}`,
              limitShowFewer: 'Show fewer',
              limitShowMore: 'Show more',
              errorIconAriaLabel: 'Error',
            }}
            constraintText="A single PDF policy document, such as a payer's coding policy manual."
          />
          <Box>
            <Button
              variant="primary"
              onClick={startDiscovery}
              loading={uploading}
              disabled={!enabled || files.length === 0}
            >
              Start rules discovery
            </Button>
            {phase && (
              <Box display="inline-block" padding={{ left: 's' }}>
                <StatusIndicator type="in-progress">{phase}</StatusIndicator>
              </Box>
            )}
          </Box>
        </SpaceBetween>
      </Container>

      <Table<DiscoveryJob>
        items={jobs}
        trackBy="jobId"
        columnDefinitions={[
          { id: 'documentKey', header: 'Document', cell: (j) => j.documentKey || j.jobId },
          {
            id: 'status',
            header: 'Status',
            cell: (j) => (
              <StatusIndicator type={jobIndicator(j.status)}>
                {j.statusMessage || j.status}
              </StatusIndicator>
            ),
          },
          {
            id: 'updatedAt',
            header: 'Updated',
            cell: (j) =>
              j.updatedAt ? new Date(j.updatedAt).toLocaleString() : '—',
          },
        ]}
        header={
          <Header
            counter={`(${jobs.length})`}
            actions={<Button iconName="refresh" onClick={loadJobs} />}
          >
            Rules discovery jobs
          </Header>
        }
        empty={<Box textAlign="center" padding="m">No discovery jobs yet</Box>}
      />

      <Container
        header={
          <Header
            variant="h2"
            counter={`(${policyClasses.length})`}
            actions={
              <Button iconName="refresh" onClick={loadRules} loading={loadingRules}>
                Reload
              </Button>
            }
            description={`Validation rules currently defined in config version "${configVersion}".`}
          >
            Discovered rules
          </Header>
        }
      >
        {policyClasses.length === 0 ? (
          <Box textAlign="center" padding="m">
            No policy classes in this config version yet. Run discovery above,
            or activate a config version that already defines{' '}
            <code>policy_classes</code>.
          </Box>
        ) : (
          <SpaceBetween size="s">
            {policyClasses.map((pc) => (
              <ExpandableSection
                key={pc.policyType}
                headerText={`${pc.policyType} (${pc.rules.length} rule${pc.rules.length === 1 ? '' : 's'})`}
              >
                <SpaceBetween size="xs">
                  {pc.description && <Box variant="p">{pc.description}</Box>}
                  {pc.rules.map((r) => (
                    <Box key={r.name}>
                      <Box variant="awsui-key-label">{r.name}</Box>
                      <Box variant="p">{r.description || '—'}</Box>
                    </Box>
                  ))}
                </SpaceBetween>
              </ExpandableSection>
            ))}
          </SpaceBetween>
        )}
      </Container>
    </SpaceBetween>
  );
};

export default RulesDiscoveryView;
