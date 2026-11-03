// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  AttributeEditor,
  Badge,
  Box,
  Button,
  Container,
  Form,
  FormField,
  Header,
  Input,
  Modal,
  SpaceBetween,
  Table,
  Textarea,
} from '@cloudscape-design/components';

import useTenants, { Tenant } from '../../hooks/use-tenants';

/**
 * Tenant declarations, shown above the prefix mappings they constrain.
 *
 * A tenant ties a set of Input-bucket folders to a Configuration Profile name
 * pattern. Once declared, the server refuses any prefix mapping, tenant or user
 * scope that would cross a tenant boundary, and in strict isolation mode it also
 * refuses a document whose folder and profile belong to different tenants. All of
 * those checks are server-side; the hints in this form only catch typing mistakes
 * early, and every server refusal is shown verbatim because it names what to
 * change.
 */

/** Mirrors `TENANT_ID_RE` in `lib/idp_common_pkg/idp_common/config/tenants.py`. */
const TENANT_ID_RE = /^[a-z0-9][a-z0-9-]{0,62}$/;
/** Mirrors `PROFILE_PATTERN_RE` in the same module. */
const PROFILE_PATTERN_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,46}[-_.]\*$/;

interface FormState {
  tenantId: string;
  prefixes: string[];
  profilePattern: string;
  description: string;
}

const EMPTY_FORM: FormState = { tenantId: '', prefixes: [''], profilePattern: '', description: '' };

const tenantIdError = (value: string): string | null =>
  !value || TENANT_ID_RE.test(value) ? null : 'Lowercase letters, digits and hyphens only, starting with a letter or digit (at most 63).';

const prefixHint = (value: string): string | null => {
  if (!value) return null;
  if (value.startsWith('/')) return "A prefix must not start with '/'. S3 keys do not.";
  if (!value.endsWith('/')) return "A tenant prefix is a folder, so it must end in '/' — otherwise 'acme' would also cover 'acmecorp/'.";
  return null;
};

const patternError = (value: string): string | null =>
  !value || PROFILE_PATTERN_RE.test(value)
    ? null
    : "Use a literal stem ending in '-', '_' or '.', then '*' — for example acme-*. 'acme*' would also match 'acmecorp-invoices'.";

const TenantsSection = (): React.JSX.Element => {
  const { tenants, isolationMode, loading, error, setError, loadTenants, saveTenant, removeTenant } = useTenants();

  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState<Tenant | null>(null);
  const [deleteInFlight, setDeleteInFlight] = useState(false);

  useEffect(() => {
    loadTenants();
  }, [loadTenants]);

  const cleanedPrefixes = useMemo(() => form.prefixes.map((p) => p.trim()).filter(Boolean), [form.prefixes]);

  const formInvalid = useMemo(
    () =>
      !form.tenantId ||
      !!tenantIdError(form.tenantId) ||
      cleanedPrefixes.length === 0 ||
      cleanedPrefixes.some((p) => !!prefixHint(p)) ||
      !form.profilePattern ||
      !!patternError(form.profilePattern),
    [form, cleanedPrefixes],
  );

  const openCreate = useCallback(() => {
    setError(null);
    setForm(EMPTY_FORM);
    setEditingId(null);
    setShowForm(true);
  }, [setError]);

  const openEdit = useCallback(
    (tenant: Tenant) => {
      setError(null);
      setForm({
        tenantId: tenant.tenantId,
        prefixes: tenant.prefixes.length ? [...tenant.prefixes] : [''],
        profilePattern: tenant.profilePattern,
        description: tenant.description ?? '',
      });
      setEditingId(tenant.tenantId);
      setShowForm(true);
    },
    [setError],
  );

  const submit = useCallback(async () => {
    setSaving(true);
    const ok = await saveTenant({
      tenantId: form.tenantId,
      prefixes: cleanedPrefixes,
      profilePattern: form.profilePattern,
      description: form.description,
    });
    setSaving(false);
    if (!ok) return;
    setShowForm(false);
    await loadTenants();
  }, [form, cleanedPrefixes, saveTenant, loadTenants]);

  // Guarded for the reason the mapping delete is: a second click sends a second
  // delete, which the server answers "not found", so a delete that worked would
  // report as a failure.
  const confirmDelete = useCallback(async () => {
    if (!deleting || deleteInFlight) return;
    setDeleteInFlight(true);
    const ok = await removeTenant(deleting.tenantId);
    setDeleteInFlight(false);
    setDeleting(null);
    if (ok) await loadTenants();
  }, [deleting, deleteInFlight, removeTenant, loadTenants]);

  const columnDefinitions = [
    {
      id: 'tenantId',
      header: 'Tenant',
      cell: (t: Tenant) => <Box fontWeight="bold">{t.tenantId}</Box>,
    },
    {
      id: 'prefixes',
      header: 'Input prefixes',
      cell: (t: Tenant) => (
        <SpaceBetween size="xxs">
          {t.prefixes.map((p) => (
            <code key={p}>{p}</code>
          ))}
        </SpaceBetween>
      ),
    },
    {
      id: 'profilePattern',
      header: 'Profile pattern',
      cell: (t: Tenant) => <code>{t.profilePattern}</code>,
    },
    {
      id: 'description',
      header: 'Description',
      cell: (t: Tenant) => t.description || <Box color="text-body-secondary">—</Box>,
    },
    {
      id: 'updated',
      header: 'Last changed',
      cell: (t: Tenant) => (
        <SpaceBetween size="xxs">
          <Box fontSize="body-s">{t.updatedAt ? new Date(t.updatedAt).toLocaleString() : '—'}</Box>
          <Box fontSize="body-s" color="text-body-secondary">
            {t.updatedBy ?? ''}
          </Box>
        </SpaceBetween>
      ),
    },
    {
      id: 'actions',
      header: 'Actions',
      cell: (t: Tenant) => (
        <SpaceBetween direction="horizontal" size="xs">
          <Button variant="inline-link" ariaLabel={`Edit tenant ${t.tenantId}`} onClick={() => openEdit(t)}>
            Edit
          </Button>
          <Button variant="inline-link" ariaLabel={`Delete tenant ${t.tenantId}`} onClick={() => setDeleting(t)}>
            Delete
          </Button>
        </SpaceBetween>
      ),
    },
  ];

  return (
    <>
      <Container
        header={
          <Header
            variant="h2"
            counter={`(${tenants.length})`}
            description="A tenant owns a set of Input-bucket folders and the Configuration Profiles whose names match its pattern. Mappings, tenants and user scopes that would cross a tenant boundary are refused when saved."
            info={
              isolationMode ? <Badge color={isolationMode === 'Strict' ? 'green' : 'grey'}>Isolation: {isolationMode}</Badge> : undefined
            }
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button iconName="refresh" ariaLabel="Refresh tenants" onClick={() => loadTenants()} loading={loading} />
                <Button onClick={openCreate}>Create tenant</Button>
              </SpaceBetween>
            }
          >
            Tenants
          </Header>
        }
      >
        <SpaceBetween size="m">
          {error && !showForm && (
            <Alert type="error" dismissible onDismiss={() => setError(null)}>
              {error}
            </Alert>
          )}

          {isolationMode === 'Strict' && (
            <Alert type="info" header="Strict tenant isolation is on">
              Non-admin users with no configuration scope see no documents, and a document is processed only when its folder and its
              Configuration Profile belong to the same tenant. This is set by the stack&apos;s TenantIsolationMode parameter.
            </Alert>
          )}
          {isolationMode === 'Off' && (
            <Alert type="info" header="Tenant isolation mode is Off">
              Tenant boundaries are checked when mappings, tenants and user scopes are saved, but uploads are not checked at ingest and
              users with no configuration scope still see every document. Deploy with TenantIsolationMode set to Strict to enforce both.
            </Alert>
          )}

          <Table
            columnDefinitions={columnDefinitions}
            items={tenants}
            loading={loading}
            loadingText="Loading tenants..."
            variant="embedded"
            empty={
              <Box textAlign="center" padding="l">
                <SpaceBetween size="s">
                  <b>No tenants</b>
                  <Box color="text-body-secondary">
                    Without tenants, prefix mappings and user scopes are not checked against any boundary.
                  </Box>
                  <Button onClick={openCreate}>Create tenant</Button>
                </SpaceBetween>
              </Box>
            }
          />
        </SpaceBetween>
      </Container>

      <Modal
        visible={showForm}
        onDismiss={() => setShowForm(false)}
        header={editingId ? `Edit tenant ${editingId}` : 'Create tenant'}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShowForm(false)}>
                Cancel
              </Button>
              <Button variant="primary" onClick={submit} loading={saving} disabled={formInvalid}>
                Save tenant
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <Form>
          <SpaceBetween size="m">
            {error && showForm && <Alert type="error">{error}</Alert>}

            <FormField
              label="Tenant ID"
              description="An identifier rather than a display name. It cannot be changed after the tenant is created."
              errorText={tenantIdError(form.tenantId) ?? undefined}
            >
              <Input
                value={form.tenantId}
                disabled={!!editingId}
                onChange={({ detail }) => setForm((f) => ({ ...f, tenantId: detail.value }))}
                placeholder="acme"
              />
            </FormField>

            <FormField
              label="Input prefixes"
              description="Folders in the Input bucket that belong to this tenant, each ending in '/'. They must not overlap another tenant's folders."
            >
              <AttributeEditor
                items={form.prefixes.map((value) => ({ value }))}
                addButtonText="Add prefix"
                removeButtonText="Remove"
                onAddButtonClick={() => setForm((f) => ({ ...f, prefixes: [...f.prefixes, ''] }))}
                onRemoveButtonClick={({ detail }) =>
                  setForm((f) => ({ ...f, prefixes: f.prefixes.filter((_, i) => i !== detail.itemIndex) }))
                }
                definition={[
                  {
                    label: 'Prefix',
                    control: (item, index) => (
                      <Input
                        value={item.value}
                        ariaLabel={`Prefix ${index + 1}`}
                        placeholder="clients/acme/"
                        onChange={({ detail }) =>
                          setForm((f) => ({ ...f, prefixes: f.prefixes.map((p, i) => (i === index ? detail.value : p)) }))
                        }
                      />
                    ),
                    errorText: (item) => prefixHint(item.value.trim()) ?? undefined,
                  },
                ]}
                empty="No prefixes"
              />
            </FormField>

            <FormField
              label="Profile pattern"
              description="Configuration Profiles whose names match this pattern belong to the tenant. Prefix mappings inside the tenant's folders must name one of them, and a user scoped to them is confined to this tenant."
              errorText={patternError(form.profilePattern) ?? undefined}
            >
              <Input
                value={form.profilePattern}
                onChange={({ detail }) => setForm((f) => ({ ...f, profilePattern: detail.value }))}
                placeholder="acme-*"
              />
            </FormField>

            <FormField label="Description" description="Optional.">
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
        header="Delete tenant"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setDeleting(null)} disabled={deleteInFlight}>
                Cancel
              </Button>
              <Button variant="primary" onClick={confirmDelete} loading={deleteInFlight}>
                Delete tenant
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="s">
          <Box>
            Tenant <strong>{deleting?.tenantId}</strong> will no longer constrain the mappings and user scopes for its folders and profiles.
          </Box>
          <Box color="text-body-secondary">
            Documents, prefix mappings and profiles are not changed. In strict isolation mode, new uploads under this tenant&apos;s folders
            are refused until they belong to a tenant again.
          </Box>
        </SpaceBetween>
      </Modal>
    </>
  );
};

export default TenantsSection;
