// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Component tests for the Tenants section of the prefix-mappings admin page.
 *
 * The transport is mocked at `client-shim`, as in ConfigPrefixMappingsLayout.test,
 * so the real `useTenants` hook runs and the assertions are about what reaches the
 * wire. What is pinned:
 *
 * - the isolation mode the server reports is stated, in both modes;
 * - a create sends the prefixes and pattern the admin entered, blank rows dropped;
 * - a server refusal is shown verbatim inside the still-open modal, because it
 *   names the tenant or mapping that conflicts and is the only thing that tells
 *   the admin what to change;
 * - a delete is sent once however many times it is confirmed.
 */

import React from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent, act, within } from '@testing-library/react';

const { mockGraphql } = vi.hoisted(() => ({ mockGraphql: vi.fn() }));
vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({ graphql: mockGraphql }),
}));

import TenantsSection from '../TenantsSection';

const TENANTS = [
  {
    tenantId: 'acme',
    prefixes: ['clients/acme/'],
    profilePattern: 'acme-*',
    description: 'ACME Corp',
    updatedAt: '2026-10-01T12:00:00Z',
    updatedBy: 'admin@example.com',
  },
];

let isolationMode = 'Off';

const routeGraphql = ({ query }: { query: string }) => {
  if (query.includes('listTenants')) {
    return Promise.resolve({ data: { listTenants: { success: true, isolationMode, tenants: TENANTS } } });
  }
  if (query.includes('putTenant')) {
    return Promise.resolve({ data: { putTenant: { success: true } } });
  }
  if (query.includes('deleteTenant')) {
    return Promise.resolve({ data: { deleteTenant: { success: true } } });
  }
  return Promise.resolve({ data: {} });
};

const callsFor = (field: string) => mockGraphql.mock.calls.filter(([args]) => String(args.query).includes(field));

const openCreateForm = async () => {
  await screen.findByText('acme');
  // Two "Create tenant" buttons exist only when the table is empty; here there is
  // one, in the header.
  fireEvent.click(screen.getByRole('button', { name: 'Create tenant' }));
  return screen.findByPlaceholderText('acme');
};

describe('TenantsSection', () => {
  beforeEach(() => {
    isolationMode = 'Off';
    mockGraphql.mockReset();
    mockGraphql.mockImplementation(routeGraphql);
  });

  it('lists the declared tenants', async () => {
    render(<TenantsSection />);
    expect(await screen.findByText('acme')).toBeInTheDocument();
    expect(screen.getByText('clients/acme/')).toBeInTheDocument();
    expect(screen.getByText('acme-*')).toBeInTheDocument();
    expect(callsFor('listTenants')).toHaveLength(1);
  });

  it('states that isolation is off, and what that leaves unchecked', async () => {
    render(<TenantsSection />);
    expect(await screen.findByText('Isolation: Off')).toBeInTheDocument();
    expect(screen.getByText(/uploads are not checked at ingest/i)).toBeInTheDocument();
  });

  it('states that strict isolation is on, and what it enforces', async () => {
    isolationMode = 'Strict';
    render(<TenantsSection />);
    expect(await screen.findByText('Isolation: Strict')).toBeInTheDocument();
    expect(screen.getByText(/belong to the same tenant/i)).toBeInTheDocument();
  });

  it('explains an empty tenant list', async () => {
    mockGraphql.mockImplementation(({ query }: { query: string }) =>
      query.includes('listTenants')
        ? Promise.resolve({ data: { listTenants: { success: true, isolationMode: 'Off', tenants: [] } } })
        : routeGraphql({ query }),
    );
    render(<TenantsSection />);
    expect(await screen.findByText('No tenants')).toBeInTheDocument();
  });

  it('says what an empty tenant list means under strict isolation', async () => {
    mockGraphql.mockImplementation(({ query }: { query: string }) =>
      query.includes('listTenants')
        ? Promise.resolve({ data: { listTenants: { success: true, isolationMode: 'Strict', tenants: [] } } })
        : routeGraphql({ query }),
    );
    render(<TenantsSection />);
    expect(await screen.findByText(/Once the first tenant is declared, every upload outside/)).toBeInTheDocument();
  });

  it('shows a failed list instead of an empty table', async () => {
    mockGraphql.mockImplementation(({ query }: { query: string }) =>
      query.includes('listTenants')
        ? Promise.resolve({ data: { listTenants: { success: false, error: { type: 'Error', message: 'Failed to list tenants: boom' } } } })
        : routeGraphql({ query }),
    );
    render(<TenantsSection />);
    expect(await screen.findByText('Failed to list tenants: boom')).toBeInTheDocument();
  });

  it('creates a tenant with the prefixes and pattern entered, then reloads', async () => {
    render(<TenantsSection />);
    const idInput = await openCreateForm();
    fireEvent.change(idInput, { target: { value: 'globex' } });
    fireEvent.change(screen.getByLabelText('Prefix 1'), { target: { value: 'clients/globex/' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add prefix' }));
    // A second, blank prefix row is dropped rather than sent as ''.
    fireEvent.change(screen.getByPlaceholderText('acme-*'), { target: { value: 'globex-*' } });

    fireEvent.click(screen.getByRole('button', { name: 'Save tenant' }));

    await waitFor(() => expect(callsFor('putTenant')).toHaveLength(1));
    expect(callsFor('putTenant')[0][0].variables).toEqual({
      tenantId: 'globex',
      prefixes: ['clients/globex/'],
      profilePattern: 'globex-*',
      grantedBy: [],
      description: undefined,
    });
    await waitFor(() => expect(callsFor('listTenants')).toHaveLength(2));
  });

  it('sends the Granted by values as a list, trimmed, blanks dropped', async () => {
    render(<TenantsSection />);
    const idInput = await openCreateForm();
    fireEvent.change(idInput, { target: { value: 'globex' } });
    fireEvent.change(screen.getByLabelText('Prefix 1'), { target: { value: 'clients/globex/' } });
    fireEvent.change(screen.getByPlaceholderText('acme-*'), { target: { value: 'globex-*' } });
    fireEvent.change(screen.getByPlaceholderText('tenant.acme'), { target: { value: ' tenant.globex , ,Globex Users ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save tenant' }));
    await waitFor(() => expect(callsFor('putTenant')).toHaveLength(1));
    expect(callsFor('putTenant')[0][0].variables.grantedBy).toEqual(['tenant.globex', 'Globex Users']);
  });

  it("shows each tenant's Granted by values and who grants tenants", async () => {
    mockGraphql.mockImplementation(({ query }: { query: string }) =>
      query.includes('listTenants')
        ? Promise.resolve({
            data: {
              listTenants: {
                success: true,
                isolationMode: 'Off',
                grantSource: 'IdpClaim',
                tenants: [{ ...TENANTS[0], grantedBy: ['tenant.acme'] }],
              },
            },
          })
        : routeGraphql({ query }),
    );
    render(<TenantsSection />);
    expect(await screen.findByText('tenant.acme')).toBeInTheDocument();
    expect(screen.getByText('Tenants are granted by the identity provider')).toBeInTheDocument();
    expect(screen.getByText(/granted more than one tenant is granted none/)).toBeInTheDocument();
  });

  it('shows no grant-source notice under Manual', async () => {
    render(<TenantsSection />);
    await screen.findByText('acme');
    expect(screen.queryByText(/Tenants are granted by/)).not.toBeInTheDocument();
  });

  it('shows a server refusal verbatim inside the open modal', async () => {
    const refusal =
      "Prefix 'clients/acme/eu/' overlaps tenant 'acme''s prefix 'clients/acme/'. Tenant prefixes must be disjoint so that every document belongs to at most one tenant.";
    mockGraphql.mockImplementation(({ query }: { query: string }) =>
      query.includes('putTenant')
        ? Promise.resolve({ data: { putTenant: { success: false, error: { type: 'ValidationError', message: refusal } } } })
        : routeGraphql({ query }),
    );
    render(<TenantsSection />);
    const idInput = await openCreateForm();
    fireEvent.change(idInput, { target: { value: 'eu' } });
    fireEvent.change(screen.getByLabelText('Prefix 1'), { target: { value: 'clients/acme/eu/' } });
    fireEvent.change(screen.getByPlaceholderText('acme-*'), { target: { value: 'eu-*' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save tenant' }));

    const dialog = (await screen.findByText(refusal)).closest('[role="dialog"]');
    // Inside the modal, which stays open, and the list was not reloaded over it.
    expect(dialog).not.toBeNull();
    expect(screen.getByText('Create tenant', { selector: 'h2 *, h2' })).toBeInTheDocument();
    expect(callsFor('listTenants')).toHaveLength(1);
  });

  it('flags a pattern without a separator before anything is sent', async () => {
    render(<TenantsSection />);
    const idInput = await openCreateForm();
    fireEvent.change(idInput, { target: { value: 'globex' } });
    fireEvent.change(screen.getByLabelText('Prefix 1'), { target: { value: 'clients/globex/' } });
    fireEvent.change(screen.getByPlaceholderText('acme-*'), { target: { value: 'globex*' } });

    expect(await screen.findByText(/would also match 'acmecorp-invoices'/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save tenant' })).toBeDisabled();
    expect(callsFor('putTenant')).toHaveLength(0);
  });

  it('flags a prefix without its trailing slash', async () => {
    render(<TenantsSection />);
    await openCreateForm();
    fireEvent.change(screen.getByLabelText('Prefix 1'), { target: { value: 'clients/globex' } });
    expect(await screen.findByText(/must end in '\/'/)).toBeInTheDocument();
  });

  it('opens an existing tenant for editing with its ID locked', async () => {
    render(<TenantsSection />);
    await screen.findByText('acme');
    fireEvent.click(screen.getByRole('button', { name: 'Edit tenant acme' }));
    const idInput = await screen.findByPlaceholderText('acme');
    expect(idInput).toBeDisabled();
    expect(idInput).toHaveValue('acme');
    fireEvent.click(screen.getByRole('button', { name: 'Save tenant' }));
    await waitFor(() => expect(callsFor('putTenant')).toHaveLength(1));
    expect(callsFor('putTenant')[0][0].variables).toMatchObject({
      tenantId: 'acme',
      prefixes: ['clients/acme/'],
      profilePattern: 'acme-*',
    });
  });

  it('says that deleting the last tenant leaves no boundary at ingest', async () => {
    render(<TenantsSection />);
    await screen.findByText('acme');
    fireEvent.click(screen.getByRole('button', { name: 'Delete tenant acme' }));
    expect(await screen.findByText(/This is the last tenant, so once it is deleted no tenant boundary is checked/)).toBeInTheDocument();
    expect(screen.queryByText(/are refused until they belong to a tenant again/)).not.toBeInTheDocument();
  });

  it('sends one delete however many times it is confirmed', async () => {
    let resolveDelete: (value: unknown) => void = () => {};
    mockGraphql.mockImplementation((args: { query: string }) => {
      if (String(args.query).includes('deleteTenant')) {
        return new Promise((resolve) => {
          resolveDelete = resolve;
        });
      }
      return routeGraphql(args);
    });
    render(<TenantsSection />);
    await screen.findByText('acme');

    fireEvent.click(screen.getByRole('button', { name: 'Delete tenant acme' }));
    const dialog = screen.getByText('Delete tenant', { selector: 'h2 *, h2' }).closest('[role="dialog"]') as HTMLElement;
    const confirm = within(dialog).getByRole('button', { name: 'Delete tenant' });
    fireEvent.click(confirm);
    fireEvent.click(confirm);

    await waitFor(() => expect(callsFor('deleteTenant')).toHaveLength(1));
    expect(callsFor('deleteTenant')[0][0].variables).toEqual({ tenantId: 'acme' });
    await act(async () => {
      resolveDelete({ data: { deleteTenant: { success: true } } });
    });
    expect(callsFor('deleteTenant')).toHaveLength(1);
  });
});
