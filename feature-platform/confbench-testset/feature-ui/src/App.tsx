// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  Checkbox,
  ColumnLayout,
  Container,
  ExpandableSection,
  Header,
  KeyValuePairs,
  Link,
  Modal,
  ProgressBar,
  SpaceBetween,
  Spinner,
  StatusIndicator,
  Table,
  Tiles,
} from '@cloudscape-design/components';

import {
  deleteDataset,
  estimateMonthlyStorageUsd,
  formatBytes,
  formatUsd,
  getCatalog,
  listJobs,
  startIngest,
  type IngestJob,
  type VariantCatalog,
} from './api';
import type { FeatureContext } from './types';

/** Configuration Profile the extension's install created — shown so the admin
 *  knows which configuration to select in Test Studio. See the ui-deployer's
 *  "Config-version naming and Test Studio" note for why this isn't automatic.
 *  One profile per feature: an upgrade cuts a revision of it rather than creating
 *  `confbench-testset-v<version>` afresh, so this no longer varies by version. */
const configProfileName = () => 'confbench-testset';

const TERMINAL = new Set(['COMPLETED', 'FAILED']);

const statusIndicator = (status: string) => {
  if (status === 'COMPLETED') return <StatusIndicator type="success">Completed</StatusIndicator>;
  if (status === 'FAILED') return <StatusIndicator type="error">Failed</StatusIndicator>;
  if (status === 'RUNNING') return <StatusIndicator type="in-progress">Running</StatusIndicator>;
  return <StatusIndicator type="pending">{status}</StatusIndicator>;
};

const App: React.FC<FeatureContext> = ({ featureApiEndpoint, getAuthToken }) => {
  const [catalog, setCatalog] = useState<VariantCatalog | null>(null);
  const [jobs, setJobs] = useState<IngestJob[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  // Selection state: a tier id, or 'custom' with an explicit variant set.
  const [tier, setTier] = useState<string>('representative');
  const [custom, setCustom] = useState<Set<string>>(new Set());
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);

  const endpoint = featureApiEndpoint ?? '';

  const refresh = useCallback(
    async (opts: { quiet?: boolean } = {}) => {
      if (!endpoint) {
        setError('No feature API endpoint configured for this extension.');
        setLoading(false);
        return;
      }
      if (!opts.quiet) setLoading(true);
      try {
        const [cat, jobList] = await Promise.all([
          getCatalog(endpoint, getAuthToken),
          listJobs(endpoint, getAuthToken),
        ]);
        setCatalog(cat);
        setJobs(jobList.jobs ?? []);
        setError(null);
      } catch (exc) {
        setError(exc instanceof Error ? exc.message : String(exc));
      } finally {
        setLoading(false);
      }
    },
    [endpoint, getAuthToken],
  );

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Poll while any job is in flight, so progress advances without a manual
  // refresh. Stops as soon as everything is terminal.
  const hasActiveJob = useMemo(
    () => jobs.some((j) => !TERMINAL.has(j.jobStatus)),
    [jobs],
  );
  useEffect(() => {
    if (!hasActiveJob) return undefined;
    const timer = setInterval(() => void refresh({ quiet: true }), 10_000);
    return () => clearInterval(timer);
  }, [hasActiveJob, refresh]);

  // ------------------------------------------------------------------
  // Derived selection totals
  // ------------------------------------------------------------------
  const selectedVariants = useMemo<string[]>(() => {
    if (tier !== 'custom') {
      return catalog?.tiers.find((t) => t.id === tier)?.variants ?? [];
    }
    return [...custom].sort();
  }, [tier, custom, catalog]);

  const selectionTotals = useMemo(() => {
    if (!catalog) return { files: 0, bytes: 0 };
    const byName = new Map(catalog.variants.map((v) => [v.name, v]));
    return selectedVariants.reduce(
      (acc, name) => {
        const v = byName.get(name);
        return v ? { files: acc.files + v.files, bytes: acc.bytes + v.bytes } : acc;
      },
      { files: 0, bytes: 0 },
    );
  }, [catalog, selectedVariants]);

  const targetTestSetId = useMemo(() => {
    if (tier !== 'custom') {
      return catalog?.tiers.find((t) => t.id === tier)?.testSetId ?? '';
    }
    // Mirror the backend's resolve_selection: a hand-picked set that exactly
    // matches a tier targets that tier's test set rather than a near-duplicate.
    const sorted = [...custom].sort().join(',');
    const match = catalog?.tiers.find((t) => [...t.variants].sort().join(',') === sorted);
    return match?.testSetId ?? 'confbench-custom';
  }, [tier, custom, catalog]);

  const alreadyDeployed = (catalog?.deployed ?? {})[targetTestSetId] ?? 0;

  // ------------------------------------------------------------------
  // Actions
  // ------------------------------------------------------------------
  const doStart = useCallback(async () => {
    setConfirming(false);
    setBusy(true);
    setNotice(null);
    try {
      const body = tier === 'custom' ? { variants: selectedVariants } : { tier };
      const job = await startIngest(endpoint, getAuthToken, body);
      setNotice(
        `Started ingest of ${job.plannedFiles ?? selectionTotals.files} documents ` +
          `into test set "${job.testSetId}". This runs in the background — you can ` +
          `leave this page.`,
      );
      await refresh({ quiet: true });
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  }, [endpoint, getAuthToken, tier, selectedVariants, selectionTotals.files, refresh]);

  const doDelete = useCallback(
    async (testSetId: string) => {
      setDeleting(null);
      setBusy(true);
      setNotice(null);
      try {
        const res = await deleteDataset(endpoint, getAuthToken, testSetId);
        setNotice(`Deleted test set "${testSetId}" (${res.objectsDeleted} objects removed).`);
        await refresh({ quiet: true });
      } catch (exc) {
        setError(exc instanceof Error ? exc.message : String(exc));
      } finally {
        setBusy(false);
      }
    },
    [endpoint, getAuthToken, refresh],
  );

  if (loading) {
    return (
      <Box padding="l" textAlign="center">
        <Spinner size="large" />
        <Box variant="p" padding={{ top: 's' }}>
          Loading the ConfBench variant catalog…
        </Box>
      </Box>
    );
  }

  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)} header="Something went wrong">
          {error}
        </Alert>
      )}
      {notice && (
        <Alert type="success" dismissible onDismiss={() => setNotice(null)}>
          {notice}
        </Alert>
      )}

      <Container
        header={
          <Header
            variant="h2"
            description={
              <>
                Deploy the{' '}
                <Link external href="https://huggingface.co/datasets/amazon/ConfBench">
                  amazon/ConfBench
                </Link>{' '}
                benchmark into Test Studio. 75 verified FCC invoices, each degraded by up to 21
                Augraphy noise pipelines — {catalog?.totalFiles ?? 0} documents,{' '}
                {formatBytes(catalog?.totalBytes ?? 0)} in total. Choose how much to ingest;
                nothing is downloaded until you start a job.
              </>
            }
          >
            Choose a dataset size
          </Header>
        }
      >
        <SpaceBetween size="l">
          <Tiles
            value={tier}
            onChange={({ detail }) => setTier(detail.value)}
            columns={2}
            items={[
              ...(catalog?.tiers ?? []).map((t) => ({
                value: t.id,
                label: `${t.label} — ${formatBytes(t.bytes)}`,
                description: `${t.files} documents (${t.variants.length} variant${
                  t.variants.length === 1 ? '' : 's'
                }). ${t.summary}`,
              })),
              {
                value: 'custom',
                label: 'Choose variants',
                description:
                  'Hand-pick individual noise pipelines — useful when targeting one specific ' +
                  'degradation type. Sizes per variant are shown below.',
              },
            ]}
          />

          {tier === 'custom' && (
            <ExpandableSection
              defaultExpanded
              headerText={`Noise variants (${custom.size} of ${catalog?.variants.length ?? 0} selected)`}
              headerDescription="Ordered smallest first. Sizes are exact, measured from the published dataset."
            >
              <SpaceBetween size="xs">
                <SpaceBetween size="xs" direction="horizontal">
                  <Button
                    onClick={() => setCustom(new Set((catalog?.variants ?? []).map((v) => v.name)))}
                  >
                    Select all
                  </Button>
                  <Button onClick={() => setCustom(new Set())}>Clear</Button>
                </SpaceBetween>
                {(catalog?.variants ?? []).map((v) => (
                  <Checkbox
                    key={v.name}
                    checked={custom.has(v.name)}
                    onChange={({ detail }) => {
                      const next = new Set(custom);
                      if (detail.checked) next.add(v.name);
                      else next.delete(v.name);
                      setCustom(next);
                    }}
                  >
                    <Box variant="span">
                      <strong>{v.name}</strong> — {formatBytes(v.bytes)}, {v.files} documents{' '}
                      <Box variant="span" color="text-status-inactive">
                        ({v.note})
                      </Box>
                    </Box>
                  </Checkbox>
                ))}
              </SpaceBetween>
            </ExpandableSection>
          )}

          <ColumnLayout columns={4} variant="text-grid">
            <KeyValuePairs
              items={[{ label: 'Documents', value: selectionTotals.files.toLocaleString() }]}
            />
            <KeyValuePairs items={[{ label: 'Download size', value: formatBytes(selectionTotals.bytes) }]} />
            <KeyValuePairs
              items={[
                {
                  label: 'Est. S3 storage',
                  value: `${formatUsd(estimateMonthlyStorageUsd(selectionTotals.bytes))}/month`,
                },
              ]}
            />
            <KeyValuePairs items={[{ label: 'Test set', value: targetTestSetId || '—' }]} />
          </ColumnLayout>

          <Box variant="small" color="text-status-inactive">
            Storage estimate uses S3 Standard list price ($
            {estimateMonthlyStorageUsd(1e9).toFixed(3)}/GB-month, us-east-1) and excludes request
            and data-transfer charges. Your actual cost depends on region and on the Test Set
            bucket&apos;s retention policy.
          </Box>

          {alreadyDeployed > 0 && (
            <Alert type="info" header={`"${targetTestSetId}" already has data`}>
              {alreadyDeployed.toLocaleString()} objects are already in this test set. Running an
              ingest again re-downloads and overwrites the same documents; delete the test set
              first if you want a clean slate.
            </Alert>
          )}

          <SpaceBetween size="xs" direction="horizontal">
            <Button
              variant="primary"
              disabled={busy || selectedVariants.length === 0}
              loading={busy}
              onClick={() => setConfirming(true)}
            >
              Deploy test set
            </Button>
            <Button disabled={busy} onClick={() => void refresh()}>
              Refresh
            </Button>
          </SpaceBetween>
        </SpaceBetween>
      </Container>

      <Container
        header={
          <Header
            variant="h2"
            description="Ingest runs in the background. Progress updates every 10 seconds while a job is active."
            counter={jobs.length ? `(${jobs.length})` : undefined}
          >
            Ingest jobs
          </Header>
        }
      >
        <Table
          variant="embedded"
          items={jobs}
          empty={
            <Box textAlign="center" padding="m" color="text-status-inactive">
              No ingest jobs yet. Choose a size above and select <b>Deploy test set</b>.
            </Box>
          }
          columnDefinitions={[
            {
              id: 'testSet',
              header: 'Test set',
              cell: (j) => (
                <SpaceBetween size="xxs">
                  <Box variant="strong">{j.testSetId}</Box>
                  <Box variant="small" color="text-status-inactive">
                    {j.tier === 'custom' ? `${j.variants?.length ?? 0} variants` : j.tier}
                  </Box>
                </SpaceBetween>
              ),
            },
            { id: 'status', header: 'Status', cell: (j) => statusIndicator(j.jobStatus) },
            {
              id: 'progress',
              header: 'Progress',
              cell: (j) => {
                const planned = j.plannedFiles ?? 0;
                const done = (j.filesUploaded ?? 0) + (j.filesSkipped ?? 0) + (j.filesFailed ?? 0);
                if (j.jobStatus === 'COMPLETED') {
                  return (
                    <Box variant="span">
                      {(j.filesInBucket ?? j.filesUploaded ?? 0).toLocaleString()} documents in
                      bucket
                    </Box>
                  );
                }
                if (!planned) return <Box variant="span">—</Box>;
                return (
                  <ProgressBar
                    value={Math.min(100, Math.round((done / planned) * 100))}
                    additionalInfo={`${done.toLocaleString()} of ${planned.toLocaleString()} documents`}
                    variant="key-value"
                  />
                );
              },
            },
            {
              id: 'issues',
              header: 'Skipped / failed',
              cell: (j) => {
                const skipped = j.filesSkipped ?? 0;
                const failed = j.filesFailed ?? 0;
                if (!skipped && !failed) return <Box variant="span">—</Box>;
                return (
                  <SpaceBetween size="xxs" direction="horizontal">
                    {skipped > 0 && <Badge>{skipped} skipped</Badge>}
                    {failed > 0 && <Badge color="red">{failed} failed</Badge>}
                  </SpaceBetween>
                );
              },
            },
            {
              id: 'started',
              header: 'Started',
              cell: (j) => (j.createdAt ? new Date(j.createdAt).toLocaleString() : '—'),
            },
            {
              id: 'actions',
              header: 'Actions',
              cell: (j) => (
                <Button
                  variant="inline-link"
                  disabled={busy || !TERMINAL.has(j.jobStatus)}
                  onClick={() => setDeleting(j.testSetId)}
                >
                  Delete test set
                </Button>
              ),
            },
          ]}
        />
      </Container>

      <Container
        header={
          <Header variant="h2" description="How to evaluate against an ingested ConfBench test set.">
            Running a test
          </Header>
        }
      >
        <SpaceBetween size="s">
          <Box variant="p">
            Once a job completes, open <b>Test Studio</b> and select the test set (for example{' '}
            <Box variant="code">confbench-representative</Box>). The <b>Configuration version</b> is
            preselected to <Box variant="code">{configProfileName()}</Box> — the
            Invoice extraction schema this extension installed — because each ConfBench test set
            records that configuration on itself.
          </Box>
          <Alert type="info">
            That schema is identical to <Box variant="code">realkie-fcc-verified</Box>, which is what
            makes accuracy on the degraded variants directly comparable to the clean baseline. You
            can of course pick a different version to compare configurations against the same
            documents — activate your own and select it here.
          </Alert>
        </SpaceBetween>
      </Container>

      <Modal
        visible={confirming}
        onDismiss={() => setConfirming(false)}
        header="Deploy ConfBench test set"
        footer={
          <Box float="right">
            <SpaceBetween size="xs" direction="horizontal">
              <Button variant="link" onClick={() => setConfirming(false)}>
                Cancel
              </Button>
              <Button variant="primary" loading={busy} onClick={() => void doStart()}>
                Start ingest
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <KeyValuePairs
            columns={2}
            items={[
              { label: 'Test set', value: targetTestSetId },
              { label: 'Noise variants', value: String(selectedVariants.length) },
              { label: 'Documents', value: selectionTotals.files.toLocaleString() },
              { label: 'Download size', value: formatBytes(selectionTotals.bytes) },
              {
                label: 'Est. S3 storage',
                value: `${formatUsd(estimateMonthlyStorageUsd(selectionTotals.bytes))}/month`,
              },
            ]}
          />
          <Alert type="warning">
            This downloads {formatBytes(selectionTotals.bytes)} from HuggingFace into your Test Set
            bucket. Large selections can take a while — the full dataset moves 32.71 GB and
            typically runs for an hour or more. The job continues in the background if you navigate
            away.
          </Alert>
        </SpaceBetween>
      </Modal>

      <Modal
        visible={deleting !== null}
        onDismiss={() => setDeleting(null)}
        header="Delete test set"
        footer={
          <Box float="right">
            <SpaceBetween size="xs" direction="horizontal">
              <Button variant="link" onClick={() => setDeleting(null)}>
                Cancel
              </Button>
              <Button
                variant="primary"
                loading={busy}
                onClick={() => deleting && void doDelete(deleting)}
              >
                Delete
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Box variant="p">
            Permanently delete <Box variant="code">{deleting}</Box> — every ingested document and
            its ground-truth baseline — from the Test Set bucket, and remove it from Test Studio.
          </Box>
          <Alert type="warning">
            Test results that already reference this test set are not deleted, but its documents
            will no longer be browsable. You can re-ingest later.
          </Alert>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
};

export default App;
