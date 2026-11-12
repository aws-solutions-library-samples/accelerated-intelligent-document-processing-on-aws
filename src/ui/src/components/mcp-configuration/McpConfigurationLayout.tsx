// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Container,
  CopyToClipboard,
  Form,
  FormField,
  Header,
  Input,
  KeyValuePairs,
  Modal,
  Multiselect,
  Pagination,
  Select,
  SpaceBetween,
  StatusIndicator,
  Table,
  Tabs,
  TextFilter,
} from '@cloudscape-design/components';
import { useCollection } from '@cloudscape-design/collection-hooks';
import { ConsoleLogger } from 'aws-amplify/utils';

import { generateClient } from '../../api/client-shim';
import useAppContext from '../../contexts/app';
import useSettingsContext from '../../contexts/settings';
import useUserRole from '../../hooks/use-user-role';
import useConfigurationVersions from '../../hooks/use-configuration-versions';
import useTenants from '../../hooks/use-tenants';
import {
  createMcpCredential,
  listMcpCredentials,
  revokeMcpCredential,
  rotateMcpCredential,
  updateMcpCredential,
} from '../../graphql/generated';
import { getErrorMessage } from '../../utils/errorUtils';

const logger = new ConsoleLogger('McpConfigurationLayout');

export interface McpCredential {
  clientId: string;
  name?: string | null;
  role?: string | null;
  allowedConfigVersions?: (string | null)[] | null;
  tenant?: string | null;
  writeTools?: boolean | null;
  enabled?: boolean | null;
  status?: string | null;
  kind?: string | null;
  createdBy?: string | null;
  createdAt?: string | null;
  expiresAt?: string | null;
  lastUsedAt?: string | null;
  replacedBy?: string | null;
  secretArn?: string | null;
}

export interface McpCredentialIssued {
  clientId: string;
  clientSecret: string;
  tokenUrl?: string | null;
  gatewayUrl?: string | null;
  secretArn?: string | null;
  oauthScopes?: (string | null)[] | null;
  credential?: McpCredential | null;
}

type Option = { label: string; value: string; description?: string };

const WRITE_ROLES = ['Admin', 'Author'];
const EXPIRY_OPTIONS: Option[] = [
  { label: '30 days', value: '30' },
  { label: '90 days (recommended)', value: '90' },
  { label: '180 days', value: '180' },
  { label: '365 days', value: '365' },
  { label: 'Never', value: '0', description: 'The credential keeps working until it is revoked' },
];
const OVERLAP_OPTIONS: Option[] = [0, 1, 2, 3, 5, 7].map((d) => ({
  label: d === 0 ? 'Stop the old credential now' : `${d} day${d === 1 ? '' : 's'}`,
  value: String(d),
}));

const STATUS_TYPE: Record<string, 'success' | 'stopped' | 'error' | 'warning'> = {
  active: 'success',
  disabled: 'stopped',
  expired: 'warning',
  revoked: 'error',
};

const formatDate = (value?: string | null): string => (value ? new Date(value).toLocaleString() : '—');

/** The Quick "Service-to-service OAuth" fields for one issued credential. */
export const quickSnippet = (issued: McpCredentialIssued): string =>
  [
    `MCP server endpoint: ${issued.gatewayUrl ?? ''}`,
    `Client ID: ${issued.clientId}`,
    `Client secret: ${issued.clientSecret}`,
    `Token URL: ${issued.tokenUrl ?? ''}`,
  ].join('\n');

/** An MCP Connector entry (Cline, Kiro, Claude Desktop) for one issued credential. */
export const connectorSnippet = (issued: McpCredentialIssued): string =>
  JSON.stringify(
    {
      mcpServers: {
        'idp-mcp-server': {
          command: 'python3',
          args: ['-m', 'idp_mcp_connector'],
          env: {
            IDP_MCP_ENDPOINT: issued.gatewayUrl ?? '',
            IDP_MCP_TOKEN_URL: issued.tokenUrl ?? '',
            IDP_MCP_CLIENT_ID: issued.clientId,
            IDP_MCP_CLIENT_SECRET: issued.clientSecret,
          },
        },
      },
    },
    null,
    2,
  );

export const IssuedSecretModal = ({ issued, onDismiss }: { issued: McpCredentialIssued; onDismiss: () => void }): React.JSX.Element => (
  <Modal
    visible
    size="large"
    onDismiss={onDismiss}
    header="Credential created"
    footer={
      <Box float="right">
        <Button variant="primary" onClick={onDismiss}>
          I have stored the secret
        </Button>
      </Box>
    }
  >
    <SpaceBetween size="m">
      <Alert type="warning" header="This secret will not be shown again">
        Copy it now. It is also stored in AWS Secrets Manager, where an operator with access to that secret can read it.
      </Alert>
      <KeyValuePairs
        columns={1}
        items={[
          {
            label: 'Client ID',
            value: (
              <CopyToClipboard
                variant="inline"
                textToCopy={issued.clientId}
                copySuccessText="Client ID copied"
                copyErrorText="Could not copy"
                copyButtonAriaLabel="Copy client ID"
              />
            ),
          },
          {
            label: 'Client secret',
            value: (
              <CopyToClipboard
                variant="inline"
                textToCopy={issued.clientSecret}
                copySuccessText="Client secret copied"
                copyErrorText="Could not copy"
                copyButtonAriaLabel="Copy client secret"
              />
            ),
          },
          { label: 'Token URL', value: issued.tokenUrl ?? '—' },
          { label: 'MCP server endpoint', value: issued.gatewayUrl ?? '—' },
          { label: 'Secrets Manager ARN', value: issued.secretArn ?? '—' },
        ]}
      />
      <Tabs
        tabs={[
          {
            id: 'quick',
            label: 'Amazon Quick (service-to-service OAuth)',
            content: (
              <SpaceBetween size="xs">
                <Box variant="p">Add the IDP server as an MCP integration in Amazon Quick and choose service-to-service OAuth.</Box>
                <CopyToClipboard
                  variant="button"
                  textToCopy={quickSnippet(issued)}
                  copySuccessText="Copied"
                  copyErrorText="Could not copy"
                  copyButtonText="Copy fields"
                />
              </SpaceBetween>
            ),
          },
          {
            id: 'connector',
            label: 'MCP Connector',
            content: (
              <SpaceBetween size="xs">
                <Box variant="p">An MCP client settings entry for the IDP MCP Connector (Cline, Kiro or another stdio MCP client).</Box>
                <CopyToClipboard
                  variant="button"
                  textToCopy={connectorSnippet(issued)}
                  copySuccessText="Copied"
                  copyErrorText="Could not copy"
                  copyButtonText="Copy settings"
                />
              </SpaceBetween>
            ),
          },
        ]}
      />
    </SpaceBetween>
  </Modal>
);

const McpConfigurationLayout = (): React.JSX.Element => {
  const { awsConfig } = useAppContext();
  const { settings } = useSettingsContext();
  const { isAdmin, loading: roleLoading } = useUserRole();
  const { isolationMode, grantSource, loadTenants } = useTenants();
  const { versions, fetchVersions } = useConfigurationVersions();
  const [items, setItems] = useState<McpCredential[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [selected, setSelected] = useState<McpCredential[]>([]);
  const [issued, setIssued] = useState<McpCredentialIssued | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [showEdit, setShowEdit] = useState(false);
  const [showRotate, setShowRotate] = useState(false);
  const [showRevoke, setShowRevoke] = useState(false);
  const [busy, setBusy] = useState(false);

  // Form state, shared by create and edit.
  const [name, setName] = useState('');
  const [role, setRole] = useState<Option>({ label: 'Viewer', value: 'Viewer' });
  const [scope, setScope] = useState<readonly Option[]>([]);
  const [writeTools, setWriteTools] = useState(false);
  const [expiry, setExpiry] = useState<Option>(EXPIRY_OPTIONS[1]);
  const [enabled, setEnabled] = useState(true);
  const [overlap, setOverlap] = useState<Option>(OVERLAP_OPTIONS[5]);

  const mcpEnabled = Boolean((settings as Record<string, unknown>)?.McpServerEndpoint);
  // Admin credentials are cross-tenant, so they are offered only where an unscoped
  // caller is unrestricted. The server refuses one anywhere else.
  const adminAllowed = isolationMode === 'Off' && grantSource === 'Manual';
  const apiGrants = grantSource === 'Api';
  const scopeRequired = isolationMode === 'Strict' || grantSource === 'IdpClaim';

  const roleOptions: Option[] = useMemo(
    () => [
      { label: 'Viewer', value: 'Viewer', description: 'Reads documents and results; may use search when unscoped' },
      { label: 'Reviewer', value: 'Reviewer', description: 'Reads documents and results' },
      { label: 'Author', value: 'Author', description: 'Reads, and with write tools can process and reprocess' },
      ...(adminAllowed ? [{ label: 'Admin', value: 'Admin', description: 'Every tool, every profile' }] : []),
    ],
    [adminAllowed],
  );

  const profileOptions: Option[] = useMemo(
    () =>
      [...versions]
        .sort((a, b) => a.versionName.localeCompare(b.versionName, undefined, { numeric: true, sensitivity: 'base' }))
        .map((v) => ({ label: v.versionName, value: v.versionName })),
    [versions],
  );

  const load = useCallback(async () => {
    if (!awsConfig) return;
    setLoading(true);
    setError('');
    try {
      const result = await generateClient().graphql({ query: listMcpCredentials });
      setItems(((result.data?.listMcpCredentials ?? []) as (McpCredential | null)[]).filter((c): c is McpCredential => Boolean(c)));
    } catch (err) {
      logger.error('Failed to load MCP credentials', err);
      setError(`Failed to load MCP credentials: ${getErrorMessage(err)}`);
    } finally {
      setLoading(false);
    }
  }, [awsConfig]);

  useEffect(() => {
    if (isAdmin && mcpEnabled) {
      load();
      fetchVersions();
      loadTenants();
    }
  }, [isAdmin, mcpEnabled, load]);

  const {
    items: shown,
    collectionProps,
    filterProps,
    paginationProps,
    filteredItemsCount,
  } = useCollection(items, {
    filtering: {
      empty: (
        <Box textAlign="center" color="inherit">
          <SpaceBetween size="xs">
            <b>No MCP credentials</b>
            <Box variant="p" color="inherit">
              Create a credential for an MCP client that signs in as a service, such as a shared Amazon Quick workflow or the MCP Connector.
            </Box>
            <Button onClick={() => openCreate()}>Create credential</Button>
          </SpaceBetween>
        </Box>
      ),
      noMatch: (
        <Box textAlign="center" color="inherit">
          <b>No matching credentials</b>
        </Box>
      ),
    },
    pagination: { pageSize: 20 },
    sorting: {},
    selection: {},
  });

  const openCreate = () => {
    setName('');
    setRole(roleOptions[0]);
    setScope([]);
    setWriteTools(false);
    setExpiry(EXPIRY_OPTIONS[1]);
    setShowCreate(true);
  };

  const openEdit = () => {
    const c = selected[0];
    if (!c) return;
    setRole({ label: c.role ?? 'Viewer', value: c.role ?? 'Viewer' });
    setScope((c.allowedConfigVersions ?? []).filter((v): v is string => Boolean(v)).map((v) => ({ label: v, value: v })));
    setWriteTools(Boolean(c.writeTools));
    setEnabled(Boolean(c.enabled));
    setShowEdit(true);
  };

  const run = async (label: string, fn: () => Promise<void>) => {
    setBusy(true);
    setError('');
    setSuccess('');
    try {
      await fn();
    } catch (err) {
      logger.error(`${label} failed`, err);
      setError(`${label} failed: ${getErrorMessage(err)}`);
    } finally {
      setBusy(false);
    }
  };

  const submitCreate = () =>
    run('Creating the credential', async () => {
      const result = await generateClient().graphql({
        query: createMcpCredential,
        variables: {
          name: name.trim(),
          role: role.value,
          allowedConfigVersions: apiGrants ? null : scope.map((s) => s.value),
          writeTools: WRITE_ROLES.includes(role.value) ? writeTools : false,
          expiresInDays: Number(expiry.value),
        },
      });
      setShowCreate(false);
      setIssued((result.data?.createMcpCredential ?? null) as McpCredentialIssued | null);
      await load();
    });

  const submitEdit = () =>
    run('Saving the credential', async () => {
      const c = selected[0];
      await generateClient().graphql({
        query: updateMcpCredential,
        variables: {
          clientId: c.clientId,
          role: role.value,
          allowedConfigVersions: apiGrants || scope.length === 0 ? null : scope.map((s) => s.value),
          clearScope: !apiGrants && scope.length === 0,
          writeTools: WRITE_ROLES.includes(role.value) ? writeTools : false,
          enabled,
        },
      });
      setShowEdit(false);
      setSuccess(`Saved ${c.name ?? c.clientId}.`);
      await load();
    });

  const submitRotate = () =>
    run('Rotating the credential', async () => {
      const c = selected[0];
      const result = await generateClient().graphql({
        query: rotateMcpCredential,
        variables: { clientId: c.clientId, overlapDays: Number(overlap.value) },
      });
      setShowRotate(false);
      setIssued((result.data?.rotateMcpCredential ?? null) as McpCredentialIssued | null);
      await load();
    });

  const submitRevoke = () =>
    run('Revoking the credential', async () => {
      const c = selected[0];
      await generateClient().graphql({ query: revokeMcpCredential, variables: { clientId: c.clientId } });
      setShowRevoke(false);
      setSelected([]);
      setSuccess(`Revoked ${c.name ?? c.clientId}. Its callers are refused from their next request.`);
      await load();
    });

  if (roleLoading) {
    return <StatusIndicator type="loading">Loading…</StatusIndicator>;
  }
  if (!isAdmin) {
    return <Alert type="error">MCP Configuration is available to Admin users only.</Alert>;
  }
  if (!mcpEnabled) {
    return (
      <Alert type="info" header="MCP is not enabled on this stack">
        Update the stack with EnableMCP set to true to use the MCP server and manage its credentials here.
      </Alert>
    );
  }

  const one = selected.length === 1 ? selected[0] : null;
  const changeable = one && one.status !== 'revoked';
  const scopeField = (
    <FormField
      label="Configuration profiles"
      description={
        apiGrants
          ? 'Tenants are granted by your entitlement system on this stack (setMcpCredentialTenant), not here.'
          : role.value === 'Admin'
            ? 'An Admin credential is not limited to profiles.'
            : scopeRequired
              ? 'Required: the profiles of one tenant this credential may see.'
              : 'Leave empty for every profile, or choose the profiles of one tenant.'
      }
    >
      <Multiselect
        selectedOptions={scope}
        onChange={({ detail }) => setScope(detail.selectedOptions as Option[])}
        options={profileOptions}
        disabled={apiGrants || role.value === 'Admin'}
        placeholder={scopeRequired ? 'Choose profiles' : 'All profiles'}
        filteringType="auto"
      />
    </FormField>
  );
  const writeField = WRITE_ROLES.includes(role.value) ? (
    <FormField label="Write tools" description="Lets callers start processing with the process and reprocess tools.">
      <Checkbox checked={writeTools} onChange={({ detail }) => setWriteTools(detail.checked)}>
        Allow process and reprocess
      </Checkbox>
    </FormField>
  ) : null;
  // A refusal is shown inside the open dialog too: the page-level alert sits under
  // it, where an Admin working in the dialog cannot see it.
  const modalError = error ? <Alert type="error">{error}</Alert> : null;
  const formFooter = (onCancel: () => void, onSubmit: () => void, label: string, disabled = false) => (
    <Box float="right">
      <SpaceBetween direction="horizontal" size="xs">
        <Button variant="link" onClick={onCancel}>
          Cancel
        </Button>
        <Button variant="primary" loading={busy} disabled={disabled} onClick={onSubmit}>
          {label}
        </Button>
      </SpaceBetween>
    </Box>
  );

  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError('')}>
          {error}
        </Alert>
      )}
      {success && (
        <Alert type="success" dismissible onDismiss={() => setSuccess('')}>
          {success}
        </Alert>
      )}
      <Container header={<Header variant="h2">Signing in to the MCP server</Header>}>
        <SpaceBetween size="s">
          <Box variant="p">
            People sign in to the MCP server as themselves, through your MCP client&apos;s user authentication with the MCP client ID from
            the stack outputs; they then get their own role and configuration profiles, as in this console.
          </Box>
          <Box variant="p">
            Clients that sign in as a service, such as a shared Amazon Quick workflow or the MCP Connector, use a credential from this page.
            Each credential has its own role, profiles, expiry and secret, and is refused from its next request once revoked.
          </Box>
          <KeyValuePairs
            columns={2}
            items={[
              { label: 'MCP server endpoint', value: String((settings as Record<string, unknown>)?.McpServerEndpoint ?? '') },
              { label: 'Token URL', value: String((settings as Record<string, unknown>)?.McpTokenUrl ?? '—') },
            ]}
          />
        </SpaceBetween>
      </Container>
      <Table
        {...collectionProps}
        selectionType="single"
        selectedItems={selected}
        onSelectionChange={({ detail }) => setSelected(detail.selectedItems)}
        trackBy="clientId"
        loading={loading}
        loadingText="Loading MCP credentials"
        items={shown}
        header={
          <Header
            variant="h2"
            counter={`(${items.length})`}
            description="Service credentials for MCP clients. Secrets are shown once, when a credential is created or rotated."
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button iconName="refresh" ariaLabel="Refresh" onClick={() => load()} />
                <Button disabled={!changeable} onClick={openEdit}>
                  Edit
                </Button>
                <Button disabled={!changeable || one?.status !== 'active'} onClick={() => setShowRotate(true)}>
                  Rotate
                </Button>
                <Button disabled={!changeable} onClick={() => setShowRevoke(true)}>
                  Revoke
                </Button>
                <Button variant="primary" onClick={openCreate}>
                  Create credential
                </Button>
              </SpaceBetween>
            }
          >
            MCP credentials
          </Header>
        }
        filter={<TextFilter {...filterProps} filteringPlaceholder="Find credentials" countText={`${filteredItemsCount ?? 0} matches`} />}
        pagination={<Pagination {...paginationProps} />}
        columnDefinitions={[
          { id: 'name', header: 'Name', cell: (c) => c.name ?? '—', sortingField: 'name' },
          { id: 'clientId', header: 'Client ID', cell: (c) => c.clientId },
          { id: 'role', header: 'Role', cell: (c) => c.role ?? '—', sortingField: 'role' },
          {
            id: 'scope',
            header: 'Configuration profiles',
            cell: (c) => (c.allowedConfigVersions ?? []).filter(Boolean).join(', ') || (c.role === 'Admin' ? 'All' : 'All profiles'),
          },
          { id: 'write', header: 'Write tools', cell: (c) => (c.writeTools ? 'Yes' : 'No') },
          {
            id: 'status',
            header: 'Status',
            cell: (c) => <StatusIndicator type={STATUS_TYPE[c.status ?? ''] ?? 'info'}>{c.status ?? 'unknown'}</StatusIndicator>,
          },
          { id: 'expires', header: 'Expires', cell: (c) => (c.expiresAt ? formatDate(c.expiresAt) : 'Never') },
          { id: 'lastUsed', header: 'Last used', cell: (c) => formatDate(c.lastUsedAt) },
          { id: 'createdBy', header: 'Created by', cell: (c) => c.createdBy ?? '—' },
        ]}
      />

      {showCreate && (
        <Modal
          visible
          onDismiss={() => setShowCreate(false)}
          header="Create MCP credential"
          footer={formFooter(
            () => setShowCreate(false),
            submitCreate,
            'Create',
            !name.trim() || (scopeRequired && role.value !== 'Admin' && scope.length === 0 && !apiGrants),
          )}
        >
          <Form>
            <SpaceBetween size="m">
              {modalError}
              <FormField label="Name" description="Shown on this page and in the audit log.">
                <Input value={name} onChange={({ detail }) => setName(detail.value)} placeholder="nightly-quick-workflow" />
              </FormField>
              <FormField label="Role" description="What its callers may do, as for a user with that role.">
                <Select selectedOption={role} onChange={({ detail }) => setRole(detail.selectedOption as Option)} options={roleOptions} />
              </FormField>
              {role.value === 'Admin' && (
                <Alert type="warning">An Admin credential can use every tool on every profile. Prefer a narrower role.</Alert>
              )}
              {scopeField}
              {writeField}
              <FormField label="Expires">
                <Select
                  selectedOption={expiry}
                  onChange={({ detail }) => setExpiry(detail.selectedOption as Option)}
                  options={EXPIRY_OPTIONS}
                />
              </FormField>
              {expiry.value === '0' && (
                <Alert type="warning">A credential that never expires keeps working until someone revokes it.</Alert>
              )}
            </SpaceBetween>
          </Form>
        </Modal>
      )}

      {showEdit && one && (
        <Modal
          visible
          onDismiss={() => setShowEdit(false)}
          header={`Edit ${one.name ?? one.clientId}`}
          footer={formFooter(() => setShowEdit(false), submitEdit, 'Save')}
        >
          <SpaceBetween size="m">
            {modalError}
            <FormField label="Role">
              <Select selectedOption={role} onChange={({ detail }) => setRole(detail.selectedOption as Option)} options={roleOptions} />
            </FormField>
            {scopeField}
            {writeField}
            <FormField label="Enabled" description="A disabled credential is refused until it is enabled again.">
              <Checkbox checked={enabled} onChange={({ detail }) => setEnabled(detail.checked)}>
                Enabled
              </Checkbox>
            </FormField>
          </SpaceBetween>
        </Modal>
      )}

      {showRotate && one && (
        <Modal
          visible
          onDismiss={() => setShowRotate(false)}
          header={`Rotate ${one.name ?? one.clientId}`}
          footer={formFooter(() => setShowRotate(false), submitRotate, 'Rotate')}
        >
          <SpaceBetween size="m">
            {modalError}
            <Box variant="p">
              A new credential with the same role and profiles is created and its secret shown once. The current one keeps working for the
              time you choose, so callers can move across.
            </Box>
            <FormField label="Keep the current credential working for">
              <Select
                selectedOption={overlap}
                onChange={({ detail }) => setOverlap(detail.selectedOption as Option)}
                options={OVERLAP_OPTIONS}
              />
            </FormField>
          </SpaceBetween>
        </Modal>
      )}

      {showRevoke && one && (
        <Modal
          visible
          onDismiss={() => setShowRevoke(false)}
          header={`Revoke ${one.name ?? one.clientId}`}
          footer={formFooter(() => setShowRevoke(false), submitRevoke, 'Revoke')}
        >
          {modalError}
          Callers using this credential are refused from their next request. Its app client is deleted and its secret is scheduled for
          deletion. This cannot be undone.
        </Modal>
      )}

      {issued && <IssuedSecretModal issued={issued} onDismiss={() => setIssued(null)} />}
    </SpaceBetween>
  );
};

export default McpConfigurationLayout;
