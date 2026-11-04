// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * A scope the server refuses must be readable while the Edit scope modal is open.
 *
 * With tenants declared, `updateUser` refuses a configuration scope that spans two
 * tenants, and its message names the tenants. A failed save leaves the modal open,
 * and the page-level alert sits underneath it, so without an alert inside the
 * modal the refusal was on the page but covered by the dialog the admin was
 * working in.
 */

import React from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';

const { mockGraphql } = vi.hoisted(() => ({ mockGraphql: vi.fn() }));
vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({ graphql: mockGraphql }),
}));
// Every hook returns the SAME object on every call. The page's load effect depends
// on `awsConfig` through `loadUsers`, so a mock that builds a new object per render
// re-fires that effect after each load: an unbounded loop that hangs the run rather
// than failing it.
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

const REFUSAL = 'This scope spans tenants acme, globex.';

describe('UserManagementLayout scope refusal', () => {
  beforeEach(() => {
    mockGraphql.mockReset();
    mockGraphql.mockImplementation(({ query }: { query: string }) => {
      if (query.includes('listUsers')) {
        return Promise.resolve({
          data: {
            listUsers: {
              users: [
                { userId: 'u1', email: 'viewer@example.com', persona: 'Viewer', allowedConfigVersions: ['acme-invoices', 'globex_w2'] },
              ],
            },
          },
        });
      }
      if (query.includes('updateUser')) {
        // The REST dispatcher's error shape, which the client passes through.
        return Promise.reject({ errors: [{ message: REFUSAL, errorType: 'ValidationError' }] });
      }
      return Promise.resolve({ data: {} });
    });
  });

  it('shows the server refusal inside the still-open modal', async () => {
    render(<UserManagementLayout />);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit scope' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Save Scope' }));

    const shown = await screen.findAllByText(/This scope spans tenants acme, globex\./);
    const dialog = screen.getByText(/Edit access scope/).closest('[role="dialog"]') as HTMLElement;
    expect(shown.some((el) => dialog.contains(el))).toBe(true);
    expect(within(dialog).getByRole('button', { name: 'Save Scope' })).toBeInTheDocument();
  });

  it('does not describe an empty scope as unrestricted when the tenant settings cannot be read', async () => {
    // listTenants answers with no data in this harness, so neither the isolation
    // mode nor the grant source is known, and the scope is shown read-only.
    render(<UserManagementLayout />);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit scope' }));
    expect((await screen.findAllByText(/could not be read, so scope is not editable here/)).length).toBeGreaterThan(0);
    expect(screen.queryByText(/unrestricted access to all versions/)).not.toBeInTheDocument();
  });

  it('says an empty scope gives no access under strict isolation', async () => {
    const base = mockGraphql.getMockImplementation() as (args: { query: string }) => Promise<unknown>;
    mockGraphql.mockImplementation((args: { query: string }) =>
      args.query.includes('listTenants')
        ? Promise.resolve({ data: { listTenants: { success: true, isolationMode: 'Strict', grantSource: 'Manual', tenants: [] } } })
        : base(args),
    );
    render(<UserManagementLayout />);
    fireEvent.click(await screen.findByRole('button', { name: 'Edit scope' }));
    expect((await screen.findAllByText(/a non-admin user with no scope sees no documents/)).length).toBeGreaterThan(0);
  });
});
