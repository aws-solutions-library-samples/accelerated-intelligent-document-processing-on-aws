// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  Container,
  ExpandableSection,
  Form,
  FormField,
  Header,
  Input,
  Modal,
  RadioGroup,
  Select,
  SelectProps,
  SpaceBetween,
  StatusIndicator,
  Table,
  Textarea,
  Toggle,
} from '@cloudscape-design/components';

import useUserRole from '../../hooks/use-user-role';
import useConfigurationVersions from '../../hooks/use-configuration-versions';
import useConfigPrefixMappings, {
  ConfigAssignmentPreview,
  ConfigPrefixMapping,
  MetadataPrecedence,
} from '../../hooks/use-config-prefix-mappings';
import ConfigRevisionSelector from '../common/ConfigRevisionSelector';

/**
 * Admin page for config prefix mappings: an S3 prefix in the Input bucket to a
 * Configuration Profile.
 *
 * Two deliberate choices in how this presents:
 *
 * - **The table is not re-sorted.** The server returns mappings
 *   most-specific-first, which is the order resolution evaluates them in, so the
 *   page reads as the decision procedure. A longest-prefix rule shown in any
 *   other order cannot be read correctly.
 * - **The prefix field does not "help".** It never appends or strips a trailing
 *   slash, because the slash is the prefix/exact mode selector; it shows which
 *   mode the typed value selected instead. Normalizing it would make an
 *   exact-key mapping unexpressible and would silently change what the admin
 *   asked for.
 */

const PRECEDENCE_OPTIONS: { value: MetadataPrecedence; label: string; description: string }[] = [
  {
    value: 'mapping',
    label: 'Mapping wins (default)',
    description:
      'The mapping decides. If the upload also names a profile, that choice is ignored and the override is recorded on the document.',
  },
  {
    value: 'metadata',
    label: 'Upload metadata wins',
    description: 'A profile named by the uploader takes precedence; the mapping applies only when none was named.',
  },
  {
    value: 'reject',
    label: 'Refuse the conflict',
    description:
      'A document that names a different profile is refused at ingest rather than processed under a guess. For prefixes where running under the wrong configuration is worse than not running.',
  },
];

const PRECEDENCE_LABELS: Record<string, string> = {
  mapping: 'Mapping wins',
  metadata: 'Upload wins',
  reject: 'Refuse conflict',
};

interface FormState {
  prefix: string;
  configProfile: string;
  configRevision: number | null;
  metadataPrecedence: MetadataPrecedence;
  enabled: boolean;
  description: string;
}

const EMPTY_FORM: FormState = {
  prefix: '',
  configProfile: '',
  configRevision: null,
  metadataPrecedence: 'mapping',
  enabled: true,
  description: '',
};

/** Which mode the typed prefix selects, shown live so it is never a surprise. */
const describePrefix = (prefix: string): React.ReactNode => {
  if (!prefix) return null;
  if (prefix.startsWith('/') || prefix.includes('//')) {
    return (
      <Box color="text-status-error">
        S3 keys do not start with &lsquo;/&rsquo; and never contain &lsquo;//&rsquo;, so this mapping would never match.
      </Box>
    );
  }
  if (prefix.endsWith('/')) {
    return (
      <Box color="text-body-secondary">
        Prefix match: every object whose key starts with <strong>{prefix}</strong>.
      </Box>
    );
  }
  return (
    <Box color="text-body-secondary">
      Exact match: only the single object at <strong>{prefix}</strong>. Add a trailing &lsquo;/&rsquo; to match a folder instead.
    </Box>
  );
};

/**
 * Which longer prefixes shadow this one. Shadowing is the one thing a
 * longest-prefix rule makes genuinely hard to eyeball, and the consequence —
 * "I added `acme/` and nothing under `acme/invoices/` changed" — reads as a bug.
 */
const shadowedBy = (mapping: ConfigPrefixMapping, all: ConfigPrefixMapping[]): string[] =>
  all
    .filter(
      (other) =>
        other.prefix !== mapping.prefix &&
        other.enabled !== false &&
        mapping.prefix.endsWith('/') &&
        other.prefix.startsWith(mapping.prefix),
    )
    .map((other) => other.prefix);

const ConfigPrefixMappingsLayout = (): React.JSX.Element => {
  const { isAdmin, loading: roleLoading } = useUserRole();
  const { versions, fetchVersions } = useConfigurationVersions();
  const { mappings, loading, error, setError, loadMappings, saveMapping, removeMapping, previewAssignment } = useConfigPrefixMappings();

  const [showForm, setShowForm] = useState(false);
  const [editingPrefix, setEditingPrefix] = useState<string | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState<ConfigPrefixMapping | null>(null);
  const [testKey, setTestKey] = useState('');
  const [testResult, setTestResult] = useState<ConfigAssignmentPreview | null>(null);
  const [testing, setTesting] = useState(false);

  useEffect(() => {
    if (isAdmin && !roleLoading) {
      loadMappings();
      fetchVersions();
    }
  }, [isAdmin, roleLoading, loadMappings, fetchVersions]);

  const profileOptions: SelectProps.Option[] = useMemo(
    () => versions.map((v) => ({ value: v.versionName, label: v.versionName })),
    [versions],
  );

  const openCreate = useCallback(() => {
    setError(null);
    setForm(EMPTY_FORM);
    setEditingPrefix(null);
    setShowForm(true);
  }, [setError]);

  const openEdit = useCallback(
    (mapping: ConfigPrefixMapping) => {
      setError(null);
      setForm({
        prefix: mapping.prefix,
        configProfile: mapping.configProfile,
        configRevision: mapping.configRevision ?? null,
        metadataPrecedence: (mapping.metadataPrecedence as MetadataPrecedence) ?? 'mapping',
        enabled: mapping.enabled !== false,
        description: mapping.description ?? '',
      });
      setEditingPrefix(mapping.prefix);
      setShowForm(true);
    },
    [setError],
  );

  const submit = useCallback(async () => {
    setSaving(true);
    const ok = await saveMapping({
      prefix: form.prefix.trim(),
      configProfile: form.configProfile,
      configRevision: form.configRevision,
      metadataPrecedence: form.metadataPrecedence,
      enabled: form.enabled,
      description: form.description,
    });
    setSaving(false);
    if (ok) {
      setShowForm(false);
      await loadMappings();
    }
  }, [form, saveMapping, loadMappings]);

  const confirmDelete = useCallback(async () => {
    if (!deleting) return;
    const ok = await removeMapping(deleting.prefix);
    setDeleting(null);
    if (ok) await loadMappings();
  }, [deleting, removeMapping, loadMappings]);

  const runTest = useCallback(async () => {
    setTesting(true);
    setTestResult(await previewAssignment(testKey.trim()));
    setTesting(false);
  }, [testKey, previewAssignment]);

  if (roleLoading) {
    return (
      <Container>
        <Box textAlign="center" padding="xxl">
          <StatusIndicator type="loading">Loading configuration prefix mappings...</StatusIndicator>
        </Box>
      </Container>
    );
  }

  if (!isAdmin) {
    return (
      <Container>
        <Alert type="error">
          Access Denied: You must be an administrator to manage configuration prefix mappings. A mapping decides which Configuration Profile
          documents are processed under, and therefore which users can see them.
        </Alert>
      </Container>
    );
  }

  const columnDefinitions = [
    {
      id: 'prefix',
      header: 'S3 prefix',
      cell: (m: ConfigPrefixMapping) => {
        const shadows = shadowedBy(m, mappings);
        return (
          <SpaceBetween size="xxs">
            <Box fontWeight="bold">
              <code>{m.prefix}</code>
            </Box>
            <SpaceBetween direction="horizontal" size="xxs">
              <Badge color={m.matchKind === 'exact' ? 'severity-neutral' : 'blue'}>{m.matchKind ?? 'prefix'}</Badge>
              {m.enabled === false && <Badge color="grey">disabled</Badge>}
            </SpaceBetween>
            {shadows.length > 0 && (
              <Box fontSize="body-s" color="text-status-warning">
                Does not apply under{' '}
                {shadows.map((s) => (
                  <code key={s}>{s} </code>
                ))}{' '}
                — a longer match wins there.
              </Box>
            )}
          </SpaceBetween>
        );
      },
    },
    {
      id: 'profile',
      header: 'Configuration Profile',
      cell: (m: ConfigPrefixMapping) => (
        <SpaceBetween size="xxs">
          <Box>{m.configProfile}</Box>
          <Box fontSize="body-s" color="text-body-secondary">
            {m.configRevision != null ? `r${m.configRevision} (pinned)` : 'Published revision (follows promotions)'}
          </Box>
        </SpaceBetween>
      ),
    },
    {
      id: 'precedence',
      header: 'On conflict',
      cell: (m: ConfigPrefixMapping) => (
        <Badge color={m.metadataPrecedence === 'reject' ? 'red' : 'grey'}>
          {PRECEDENCE_LABELS[m.metadataPrecedence ?? 'mapping'] ?? m.metadataPrecedence}
        </Badge>
      ),
    },
    {
      id: 'description',
      header: 'Description',
      cell: (m: ConfigPrefixMapping) => m.description || <Box color="text-body-secondary">—</Box>,
    },
    {
      id: 'updated',
      header: 'Last changed',
      cell: (m: ConfigPrefixMapping) => (
        <SpaceBetween size="xxs">
          <Box fontSize="body-s">{m.updatedAt ? new Date(m.updatedAt).toLocaleString() : '—'}</Box>
          <Box fontSize="body-s" color="text-body-secondary">
            {m.updatedBy ?? ''}
          </Box>
        </SpaceBetween>
      ),
    },
    {
      id: 'actions',
      header: 'Actions',
      cell: (m: ConfigPrefixMapping) => (
        <SpaceBetween direction="horizontal" size="xs">
          <Button variant="inline-link" onClick={() => openEdit(m)}>
            Edit
          </Button>
          <Button variant="inline-link" onClick={() => setDeleting(m)}>
            Delete
          </Button>
        </SpaceBetween>
      ),
    },
  ];

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h1"
            description="Assign a Configuration Profile to everything uploaded under an S3 prefix, so the destination decides the configuration instead of each uploader having to specify one."
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button iconName="refresh" onClick={loadMappings} loading={loading} />
                <Button variant="primary" onClick={openCreate}>
                  Create mapping
                </Button>
              </SpaceBetween>
            }
          >
            Configuration Prefix Mappings
          </Header>
        }
      >
        <SpaceBetween size="m">
          {error && (
            <Alert type="error" dismissible onDismiss={() => setError(null)}>
              {error}
            </Alert>
          )}

          <Alert type="info" header="How a mapping is chosen">
            The <strong>longest matching prefix wins</strong>, and an exact-key mapping outranks every prefix mapping. Matching is{' '}
            <strong>case-sensitive</strong>, because S3 keys are — <code>Invoices/</code> and <code>invoices/</code> are different folders.
            Mappings do not apply to documents this deployment submits itself (Test Studio runs, PII-redacted copies), which pin their own
            configuration.
          </Alert>

          <Table
            columnDefinitions={columnDefinitions}
            items={mappings}
            loading={loading}
            loadingText="Loading mappings..."
            variant="embedded"
            empty={
              <Box textAlign="center" padding="l">
                <SpaceBetween size="s">
                  <b>No prefix mappings</b>
                  <Box color="text-body-secondary">
                    Uploads are processed under the profile the uploader chooses, or under the active profile.
                  </Box>
                  <Button onClick={openCreate}>Create mapping</Button>
                </SpaceBetween>
              </Box>
            }
            header={
              <Header counter={`(${mappings.length})`} description="Ordered the way resolution evaluates them: most specific first.">
                Mappings
              </Header>
            }
          />

          <ExpandableSection headerText="Test a key" variant="footer">
            <SpaceBetween size="s">
              <Box color="text-body-secondary">
                Check what an object would be processed under before anything is uploaded. Useful for prefixes a client generates, which
                often do not look the way you expect — a batch uploaded by the SDK lands under <code>my-batch-20260101-120000/</code>, not{' '}
                <code>my-batch/</code>.
              </Box>
              <FormField label="Object key">
                <Input value={testKey} onChange={({ detail }) => setTestKey(detail.value)} placeholder="acme/invoices/january.pdf" />
              </FormField>
              <Button onClick={runTest} loading={testing} disabled={!testKey.trim()}>
                Resolve
              </Button>
              {testResult && (
                <Alert type={testResult.rejected ? 'error' : testResult.conflict ? 'warning' : 'success'}>
                  <SpaceBetween size="xxs">
                    <Box>{testResult.reason}</Box>
                    {testResult.configProfile && (
                      <Box fontSize="body-s" color="text-body-secondary">
                        Profile <strong>{testResult.configProfile}</strong>
                        {testResult.configRevision != null ? ` r${testResult.configRevision}` : ''} ({testResult.source})
                      </Box>
                    )}
                  </SpaceBetween>
                </Alert>
              )}
            </SpaceBetween>
          </ExpandableSection>
        </SpaceBetween>
      </Container>

      <Modal
        visible={showForm}
        onDismiss={() => setShowForm(false)}
        header={editingPrefix ? `Edit mapping for ${editingPrefix}` : 'Create prefix mapping'}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShowForm(false)}>
                Cancel
              </Button>
              <Button variant="primary" onClick={submit} loading={saving} disabled={!form.prefix.trim() || !form.configProfile}>
                Save
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <Form>
          <SpaceBetween size="m">
            {error && <Alert type="error">{error}</Alert>}

            <FormField
              label="S3 prefix"
              description="Relative to the Input bucket root. End with '/' to match a folder; omit it to match one exact object key."
              constraintText={describePrefix(form.prefix)}
            >
              <Input
                value={form.prefix}
                disabled={!!editingPrefix}
                onChange={({ detail }) => setForm((f) => ({ ...f, prefix: detail.value }))}
                placeholder="acme/invoices/"
              />
            </FormField>

            <FormField label="Configuration Profile" description="Documents landing here are processed under this profile.">
              <Select
                selectedOption={form.configProfile ? { value: form.configProfile, label: form.configProfile } : null}
                options={profileOptions}
                onChange={({ detail }) =>
                  setForm((f) => ({ ...f, configProfile: detail.selectedOption.value ?? '', configRevision: null }))
                }
                placeholder="Choose a profile"
              />
            </FormField>

            <ConfigRevisionSelector
              profileName={form.configProfile || null}
              value={form.configRevision}
              onChange={(revision) => setForm((f) => ({ ...f, configRevision: revision }))}
              label="Revision"
              description="Leave as the published revision so the mapping follows promotions, the way the active profile does. Pinning a revision protects it from retention, and it will not change until you change the mapping."
            />

            <FormField
              label="If the upload also specifies a profile"
              description="Uploads can carry a profile in their S3 metadata — from the UI, the CLI or the SDK. This decides what happens when that disagrees with the mapping."
            >
              <RadioGroup
                value={form.metadataPrecedence}
                onChange={({ detail }) => setForm((f) => ({ ...f, metadataPrecedence: detail.value as MetadataPrecedence }))}
                items={PRECEDENCE_OPTIONS.map((o) => ({
                  value: o.value,
                  label: o.label,
                  description: o.description,
                }))}
              />
            </FormField>

            {form.metadataPrecedence === 'reject' && (
              <Alert type="warning">
                Documents uploaded here that name a different profile will be <strong>refused</strong> and recorded as failed, with the
                reason shown on the document. An upload that names the same profile is not a conflict and still processes.
              </Alert>
            )}

            <FormField
              label="Enabled"
              description="A disabled mapping never matches, but is kept so you can turn it back on without losing who created it."
            >
              <Toggle checked={form.enabled} onChange={({ detail }) => setForm((f) => ({ ...f, enabled: detail.checked }))}>
                {form.enabled ? 'Active' : 'Disabled'}
              </Toggle>
            </FormField>

            <FormField label="Description" description="Optional. Why this mapping exists.">
              <Textarea
                value={form.description}
                onChange={({ detail }) => setForm((f) => ({ ...f, description: detail.value }))}
                rows={2}
              />
            </FormField>
          </SpaceBetween>
        </Form>
      </Modal>

      <Modal
        visible={!!deleting}
        onDismiss={() => setDeleting(null)}
        header="Delete prefix mapping"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setDeleting(null)}>
                Cancel
              </Button>
              <Button variant="primary" onClick={confirmDelete}>
                Delete
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="s">
          <Box>
            Documents uploaded to <code>{deleting?.prefix}</code> will no longer be assigned <strong>{deleting?.configProfile}</strong>.
            Future uploads there fall back to the profile the uploader chooses, or to the active profile.
          </Box>
          <Box color="text-body-secondary">
            Documents already processed are unaffected. If this mapping pinned a revision, that revision stays protected from retention —
            deleting the mapping does not release it, because a test run may depend on the same one.
          </Box>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
};

export default ConfigPrefixMappingsLayout;
