// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The MCP Configuration page: Admin-only, hidden without MCP, lists credentials
 * without secrets, and shows a new secret once in a dialog that also carries the
 * Quick and MCP Connector snippets.
 */

import React from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react';
import createWrapper from '@cloudscape-design/components/test-utils/dom';

const { mockGraphql } = vi.hoisted(() => ({ mockGraphql: vi.fn() }));
vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({ graphql: mockGraphql }),
}));
// Every hook returns the SAME object on every call, or the load effect loops.
const stable = vi.hoisted(() => ({
  role: { isAdmin: true, loading: false },
  app: { awsConfig: { region: 'us-east-1' } },
  settings: { settings: { McpServerEndpoint: 'https://gw.example/mcp', McpTokenUrl: 'https://auth.example/oauth2/token' } },
  versions: { versions: [{ versionName: 'acme-invoices' }], fetchVersions: () => {} },
  tenants: { isolationMode: 'Off', grantSource: 'Manual', loadTenants: () => {} },
}));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => stable.role }));
vi.mock('../../../contexts/app', () => ({ default: () => stable.app }));
vi.mock('../../../contexts/settings', () => ({ default: () => stable.settings }));
vi.mock('../../../hooks/use-configuration-versions', () => ({ default: () => stable.versions }));
vi.mock('../../../hooks/use-tenants', () => ({ default: () => stable.tenants }));

import McpConfigurationLayout, { connectorSnippet, quickSnippet } from '../McpConfigurationLayout';

const SECRET = 'sEcReT-value-1234';
const LISTED = {
  clientId: 'client-1',
  name: 'nightly',
  role: 'Viewer',
  allowedConfigVersions: ['acme-invoices'],
  writeTools: false,
  enabled: true,
  status: 'active',
};
const ISSUED = {
  clientId: 'client-2',
  clientSecret: SECRET,
  tokenUrl: 'https://auth.example/oauth2/token',
  gatewayUrl: 'https://gw.example/mcp',
  secretArn: 'arn:aws:secretsmanager:us-east-1:111122223333:secret:stk/mcp/credentials/client-2',
  credential: { ...LISTED, clientId: 'client-2', name: 'quick' },
};

describe('McpConfigurationLayout', () => {
  beforeEach(() => {
    stable.role.isAdmin = true;
    stable.settings.settings.McpServerEndpoint = 'https://gw.example/mcp';
    mockGraphql.mockReset();
    mockGraphql.mockImplementation(({ query }: { query: string }) => {
      if (query.includes('listMcpCredentials')) return Promise.resolve({ data: { listMcpCredentials: [LISTED] } });
      if (query.includes('createMcpCredential')) return Promise.resolve({ data: { createMcpCredential: ISSUED } });
      return Promise.resolve({ data: {} });
    });
  });

  it('lists credentials with their role, profiles and status', async () => {
    render(<McpConfigurationLayout />);
    expect(await screen.findByText('nightly')).toBeInTheDocument();
    expect(screen.getByText('acme-invoices')).toBeInTheDocument();
    expect(screen.getByText('active')).toBeInTheDocument();
  });

  it('shows a new secret once, with the Quick and connector snippets', async () => {
    render(<McpConfigurationLayout />);
    await screen.findByText('nightly');
    fireEvent.click(screen.getByRole('button', { name: 'Create credential' }));
    const dialog = (await screen.findByText('Create MCP credential')).closest('[role="dialog"]') as HTMLElement;
    fireEvent.change(within(dialog).getByPlaceholderText('nightly-quick-workflow'), { target: { value: 'quick' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create' }));
    expect(await screen.findByText('This secret will not be shown again')).toBeInTheDocument();
    expect(screen.getAllByText(SECRET).length).toBeGreaterThan(0);
    const call = mockGraphql.mock.calls.find(([arg]) => (arg as { query: string }).query.includes('createMcpCredential'));
    expect((call?.[0] as { variables: Record<string, unknown> }).variables).toMatchObject({
      name: 'quick',
      role: 'Viewer',
      expiresInDays: 90,
    });

    fireEvent.click(screen.getByRole('button', { name: 'I have stored the secret' }));
    await waitFor(() => expect(screen.queryByText(SECRET)).toBeNull());
  });

  it('builds snippets naming the issued values', () => {
    expect(quickSnippet(ISSUED)).toContain(`Client secret: ${SECRET}`);
    const connector = JSON.parse(connectorSnippet(ISSUED));
    expect(connector.mcpServers['idp-mcp-server'].env).toEqual({
      IDP_MCP_ENDPOINT: 'https://gw.example/mcp',
      IDP_MCP_TOKEN_URL: 'https://auth.example/oauth2/token',
      IDP_MCP_CLIENT_ID: 'client-2',
      IDP_MCP_CLIENT_SECRET: SECRET,
    });
  });

  it('shows a refusal inside the open dialog', async () => {
    mockGraphql.mockImplementation(({ query }: { query: string }) => {
      if (query.includes('listMcpCredentials')) return Promise.resolve({ data: { listMcpCredentials: [] } });
      if (query.includes('createMcpCredential'))
        return Promise.reject({ errors: [{ message: 'This scope spans tenants acme, globex.', errorType: 'ValidationError' }] });
      return Promise.resolve({ data: {} });
    });
    render(<McpConfigurationLayout />);
    fireEvent.click((await screen.findAllByRole('button', { name: 'Create credential' }))[0]);
    const dialog = (await screen.findByText('Create MCP credential')).closest('[role="dialog"]') as HTMLElement;
    const input = within(dialog).getByPlaceholderText('nightly-quick-workflow');
    fireEvent.change(input, { target: { value: 'x' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Create' }));
    expect(await within(dialog).findByText(/spans tenants acme, globex/)).toBeInTheDocument();
  });

  const roleOptions = async (): Promise<string[]> => {
    render(<McpConfigurationLayout />);
    await screen.findByText('nightly');
    fireEvent.click(screen.getByRole('button', { name: 'Create credential' }));
    await screen.findByText('Create MCP credential');
    const select = createWrapper(document.body).findModal()!.findContent().findSelect()!;
    select.openDropdown();
    return select
      .findDropdown()
      .findOptions()
      .map((o) => o.getElement().textContent ?? '');
  };

  it('offers the Admin role only where an unscoped caller is unrestricted', async () => {
    expect((await roleOptions()).some((o) => o.startsWith('Admin'))).toBe(true);
  });

  it('offers no Admin role where tenant isolation applies', async () => {
    stable.tenants.isolationMode = 'Strict';
    try {
      const options = await roleOptions();
      expect(options.some((o) => o.startsWith('Viewer'))).toBe(true);
      expect(options.some((o) => o.startsWith('Admin'))).toBe(false);
    } finally {
      stable.tenants.isolationMode = 'Off';
    }
  });

  it('is Admin-only', () => {
    stable.role.isAdmin = false;
    render(<McpConfigurationLayout />);
    expect(screen.getByText(/available to Admin users only/)).toBeInTheDocument();
    expect(mockGraphql).not.toHaveBeenCalled();
  });

  it('says MCP is not enabled when the stack has no MCP server', () => {
    stable.settings.settings.McpServerEndpoint = '';
    render(<McpConfigurationLayout />);
    expect(screen.getByText('MCP is not enabled on this stack')).toBeInTheDocument();
    expect(mockGraphql).not.toHaveBeenCalled();
  });
});
