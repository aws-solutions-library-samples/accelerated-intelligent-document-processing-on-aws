// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * User Management under an external `TenantGrantSource` (IdpClaim or Api).
 *
 * The identity provider or an entitlement system owns the configuration scope, and
 * the server refuses an Admin edit of it. What is pinned:
 *
 * - the deny marker is shown as "No tenant granted", never as a profile name;
 * - a granted tenant is shown with where it came from;
 * - the scope selector is read-only, and a save sends no configuration axis,
 *   because its presence means "change this axis" to the server;
 * - under Manual the selector stays editable and the axis is sent.
 */

import React from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const { mockGraphql } = vi.hoisted(() => ({ mockGraphql: vi.fn() }));
vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({ graphql: mockGraphql }),
}));
const stable = vi.hoisted(() => ({
  role: { isAdmin: true, loading: false },
  app: { awsConfig: { region: 'us-east-1' } },
  settings: { settings: {} },
  versions: { versions: [{ versionName: 'acme-invoices' }, { versionName: 'globex_w2' }], fetchVersions: () => {} },
}));
vi.mock('../../../hooks/use-user-role', () => ({ default: () => stable.role }));
vi.mock('../../../contexts/app', () => ({ default: () => stable.app }));
vi.mock('../../../contexts/settings', () => ({ default: () => stable.settings }));
vi.mock('../../../hooks/use-configuration-versions', () => ({ default: () => stable.versions }));

import UserManagementLayout from '../UserManagementLayout';

const USERS = [
  {
    userId: 'u1',
    email: 'denied@example.com',
    persona: 'Viewer',
    allowedConfigVersions: ['!no-tenant-granted'],
    scopeSource: 'idp',
  },
  {
    userId: 'u2',
    email: 'granted@example.com',
    persona: 'Viewer',
    allowedConfigVersions: ['acme-*'],
    scopeSource: 'idp',
    scopeTenant: 'acme',
  },
];

let grantSource = 'IdpClaim';

const callsFor = (field: string) => mockGraphql.mock.calls.filter(([args]) => String(args.query).includes(field));

describe('UserManagementLayout under an external grant source', () => {
  beforeEach(() => {
    grantSource = 'IdpClaim';
    mockGraphql.mockReset();
    mockGraphql.mockImplementation(({ query }: { query: string }) => {
      if (query.includes('listUsers')) {
        return Promise.resolve({ data: { listUsers: { users: USERS } } });
      }
      if (query.includes('listTenants')) {
        return Promise.resolve({ data: { listTenants: { success: true, isolationMode: 'Off', grantSource, tenants: [] } } });
      }
      if (query.includes('updateUser')) {
        return Promise.resolve({ data: { updateUser: USERS[1] } });
      }
      return Promise.resolve({ data: {} });
    });
  });

  it('shows the deny marker as "No tenant granted"', async () => {
    render(<UserManagementLayout />);
    expect(await screen.findByText('No tenant granted')).toBeInTheDocument();
    expect(screen.queryByText('!no-tenant-granted')).not.toBeInTheDocument();
  });

  it('shows a granted tenant and where it came from', async () => {
    render(<UserManagementLayout />);
    expect(await screen.findByText('Tenant: acme')).toBeInTheDocument();
    expect(screen.getAllByText(/via identity provider/).length).toBeGreaterThan(0);
  });

  it('makes the configuration scope read-only and sends no configuration axis', async () => {
    render(<UserManagementLayout />);
    await screen.findByText('Tenant: acme');
    await waitFor(() => expect(callsFor('listTenants').length).toBeGreaterThan(0));
    fireEvent.click((await screen.findAllByRole('button', { name: 'Edit scope' }))[1]);
    expect((await screen.findAllByText(/Granted by the identity provider's claim/)).length).toBeGreaterThan(0);
    fireEvent.click(await screen.findByRole('button', { name: 'Save Scope' }));
    await waitFor(() => expect(callsFor('updateUser')).toHaveLength(1));
    expect(callsFor('updateUser')[0][0].variables).not.toHaveProperty('allowedConfigVersions');
  });

  it('keeps the configuration axis under Manual', async () => {
    grantSource = 'Manual';
    render(<UserManagementLayout />);
    await screen.findByText('granted@example.com');
    await waitFor(() => expect(callsFor('listTenants').length).toBeGreaterThan(0));
    fireEvent.click((await screen.findAllByRole('button', { name: 'Edit scope' }))[1]);
    fireEvent.click(await screen.findByRole('button', { name: 'Save Scope' }));
    await waitFor(() => expect(callsFor('updateUser')).toHaveLength(1));
    expect(callsFor('updateUser')[0][0].variables).toHaveProperty('allowedConfigVersions');
  });
});
