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

/**
 * Why the typed prefix can never work as a mapping, or null if it can.
 *
 * A client-side mirror of the server's `prefix_rejection_reason`
 * (`lib/idp_common_pkg/idp_common/config/prefix_mappings.py`) — the server stays
 * the authority, this only saves a round trip. It is returned as a **string**
 * because it is rendered as the form field's `errorText`, not its
 * `constraintText`: constraint text is advisory, so a screen reader never
 * announces it as a problem and Save stays enabled. A prefix that can never
 * match is not advice.
 *
 * The checks are in the server's order on purpose. They overlap — `'/'` fails
 * three of them — so the order decides which sentence the admin reads, and the
 * two sides disagreeing about that is a confusing way to be consistent.
 */
const prefixError = (prefix: string): string | null => {
  if (!prefix) return null;
  const ROOT_REFUSAL =
    'A root mapping is not allowed: it would change the configuration of every unmapped upload in this deployment. The active Configuration Profile already serves that purpose.';
  if (prefix === '/' || prefix === '//') return ROOT_REFUSAL;
  if (prefix.startsWith('/')) {
    return "A mapping prefix must not start with '/'. S3 keys do not, so the mapping would never match.";
  }
  if (prefix.includes('//')) {
    return "A mapping prefix must not contain '//'. S3 treats 'acme//invoices/' as a different folder from 'acme/invoices/', so the mapping would never match.";
  }
  if (prefix.split('/').some((segment) => segment === '.' || segment === '..')) {
    return "A mapping prefix must not contain '.' or '..' path segments.";
  }
  // Last, as on the server: anything left that canonicalizes to nothing at all.
  if (prefix.split('/').every((segment) => !segment)) return ROOT_REFUSAL;
  return null;
};

/** Which mode the typed prefix selects, shown live so it is never a surprise. */
const describePrefix = (prefix: string): React.ReactNode => {
  if (!prefix || prefixError(prefix)) return null;
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
  const { isAdmin, loading: roleLoading, sessionError, retrySession } = useUserRole();
  // `fetchVersions` is deliberately NOT taken from this hook. It is a plain
  // arrow function rather than a `useCallback`, so it has a new identity on
  // every render; calling it from an effect that also depends on it set state,
  // produced a new identity, and re-fired the effect — an unbounded request loop
  // for as long as the page was open, with the table never leaving `loading`.
  // The hook already fetches once on mount, which is what this page needs.
  const { versions, loading: versionsLoading, error: versionsError } = useConfigurationVersions();
  const { mappings, loading, error, setError, loadMappings, saveMapping, removeMapping, previewAssignment } = useConfigPrefixMappings();

  const [showForm, setShowForm] = useState(false);
  const [editingPrefix, setEditingPrefix] = useState<string | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState<ConfigPrefixMapping | null>(null);
  const [deleteInFlight, setDeleteInFlight] = useState(false);
  const [testKey, setTestKey] = useState('');
  const [testResult, setTestResult] = useState<ConfigAssignmentPreview | null>(null);
  const [testError, setTestError] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);

  useEffect(() => {
    if (isAdmin && !roleLoading) loadMappings();
  }, [isAdmin, roleLoading, loadMappings]);

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

  /**
   * Why the prefix in the form cannot be saved, or null if it can.
   *
   * Two reasons, and the second is the one that is not obvious.
   * `putConfigPrefixMapping` is a **create-or-replace** on the prefix, so typing
   * a prefix that already has a mapping overwrites its profile, pinned revision,
   * conflict mode and description — under a modal headed "Create prefix
   * mapping", with no indication that anything was replaced. The prefix field is
   * disabled while editing, so this only has to hold for the create path.
   */
  const formPrefixError = useMemo(() => {
    const typed = form.prefix.trim();
    const invalid = prefixError(typed);
    if (invalid) return invalid;
    if (!editingPrefix && typed && mappings.some((m) => m.prefix === typed)) {
      return `A mapping for '${typed}' already exists. Use Edit on that row to change it — saving here would replace its profile, pinned revision and conflict mode.`;
    }
    return null;
  }, [form.prefix, editingPrefix, mappings]);

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

  // The in-flight guard is not cosmetic: the second of two clicks sends a second
  // delete, which the server answers "No configuration prefix mapping for
  // 'acme/'" — so a delete that worked reports as a failure, and the admin is
  // left unsure whether it happened.
  const confirmDelete = useCallback(async () => {
    if (!deleting || deleteInFlight) return;
    setDeleteInFlight(true);
    const ok = await removeMapping(deleting.prefix);
    setDeleteInFlight(false);
    setDeleting(null);
    if (ok) await loadMappings();
  }, [deleting, deleteInFlight, removeMapping, loadMappings]);

  const runTest = useCallback(async () => {
    setTesting(true);
    setTestError(null);
    setTestResult(null);
    const result = await previewAssignment(testKey.trim());
    // `previewAssignment` swallows its own errors and returns null, on purpose:
    // the same function runs on every keystroke behind the upload panel's
    // debounce, where an error banner over a half-filled form is worse than
    // silence. Here the key is non-empty (the button is disabled otherwise), so
    // null can only mean the call failed — and without this branch the spinner
    // just stopped and nothing appeared, which is indistinguishable from a
    // resolution that returned nothing.
    if (result) setTestResult(result);
    else setTestError('Could not resolve that key — the request failed. Check that you are still signed in, then try again.');
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

  // "I could not find out what your groups are" is a different statement from
  // "you have none", and only one of them is the reader's problem to act on.
  // Without this branch a failed session read fell through to the message below
  // and told an entitled Admin to go and ask an administrator for access —
  // sending them to someone with nothing to fix, over a condition that usually
  // clears on a retry.
  if (sessionError) {
    return (
      <Container>
        <Alert type="error" header="Could not read your session" action={<Button onClick={retrySession}>Retry</Button>}>
          Your group membership could not be read, so this page cannot tell whether you may manage configuration prefix mappings. This is
          usually transient.
        </Alert>
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
                {/* Icon-only, so the label is the only thing a screen reader has to go on. */}
                <Button iconName="refresh" ariaLabel="Refresh mappings" onClick={loadMappings} loading={loading} />
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
              {testError && <Alert type="error">{testError}</Alert>}
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
              <Button
                variant="primary"
                onClick={submit}
                loading={saving}
                disabled={!form.prefix.trim() || !form.configProfile || !!formPrefixError}
              >
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
              errorText={formPrefixError ?? undefined}
            >
              <Input
                value={form.prefix}
                disabled={!!editingPrefix}
                onChange={({ detail }) => setForm((f) => ({ ...f, prefix: detail.value }))}
                placeholder="acme/invoices/"
              />
            </FormField>

            {/* The profile list comes from `useConfigurationVersions`, whose
                `loading` and `error` were previously dropped on the floor: on a
                failed fetch the dropdown was simply empty, with no status and no
                reason, and Save could never enable because no profile could be
                picked. */}
            <FormField
              label="Configuration Profile"
              description="Documents landing here are processed under this profile."
              errorText={versionsError ?? undefined}
            >
              <Select
                selectedOption={form.configProfile ? { value: form.configProfile, label: form.configProfile } : null}
                options={profileOptions}
                onChange={({ detail }) =>
                  setForm((f) => ({ ...f, configProfile: detail.selectedOption.value ?? '', configRevision: null }))
                }
                statusType={versionsLoading ? 'loading' : versionsError ? 'error' : 'finished'}
                loadingText="Loading configuration profiles..."
                errorText={versionsError ?? undefined}
                empty="No configuration profiles"
                placeholder="Choose a profile"
              />
            </FormField>

            {/* Mounted only while the form is open, which is what preserves a
                stored revision. Cloudscape's Modal keeps hidden children
                MOUNTED (it toggles a CSS class, it does not unmount), so a
                selector rendered unconditionally here lives for the life of the
                page and sees `openEdit` populating the form as a profile
                *change* — and a profile change legitimately clears the
                revision. Remounting per open means each open starts from that
                mapping's own profile. */}
            {showForm && (
              <ConfigRevisionSelector
                profileName={form.configProfile || null}
                value={form.configRevision}
                onChange={(revision) => setForm((f) => ({ ...f, configRevision: revision }))}
                label="Revision"
                description="Leave as the published revision so the mapping follows promotions, the way the active profile does. Pinning a revision protects it from retention, and it will not change until you change the mapping."
              />
            )}

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
        onDismiss={() => {
          if (!deleteInFlight) setDeleting(null);
        }}
        header="Delete prefix mapping"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setDeleting(null)} disabled={deleteInFlight}>
                Cancel
              </Button>
              <Button variant="primary" onClick={confirmDelete} loading={deleteInFlight}>
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
