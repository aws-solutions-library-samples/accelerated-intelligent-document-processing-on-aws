// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Component tests for the config-prefix-mappings admin page.
 *
 * The RBAC hook, the configuration-profiles hook and the revision-history hook
 * are mocked at the module boundary; the GraphQL transport is mocked one level
 * lower, at `client-shim`, so the page's own `useConfigPrefixMappings` really
 * runs. That split is deliberate: two of the properties below are about *how
 * many times* the page talks to the API, and a mocked hook cannot show that.
 *
 * What is pinned here:
 *
 * - **The page issues one list request.** The mount effect must not depend on
 *   `fetchVersions`: it is a plain arrow function rather than a `useCallback`, so
 *   each render gives it a new identity, which re-fires the effect, which sets
 *   state — continuous requests for as long as an Admin leaves the tab open,
 *   with the table never leaving `loading`. `useConfigurationVersions` already
 *   fetches on its own mount, so the page does not need to ask.
 * - **Opening the edit modal preserves the pinned revision.** The revision
 *   selector resets its value when the profile changes, and populating the form
 *   looked like a profile change. Since `putConfigPrefixMapping` is a full
 *   replace, that turned "edit the description" into "unpin the revision".
 * - **A duplicate prefix is refused on create.** The mutation is
 *   create-or-replace, so without a check the Create modal silently overwrote an
 *   existing mapping's profile, revision and conflict mode.
 * - **The non-admin and unknown-session gates are different messages.** Telling
 *   an Admin whose session read failed to go and ask an administrator for access
 *   sends them to someone with nothing to fix.
 */

import React from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent, act, within } from '@testing-library/react';

const { mockGraphql } = vi.hoisted(() => ({ mockGraphql: vi.fn() }));
vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({ graphql: mockGraphql }),
}));

const roleState = vi.hoisted(() => ({
  isAdmin: true,
  loading: false,
  sessionError: false,
  retrySession: vi.fn(),
}));
vi.mock('../../../hooks/use-user-role', () => ({
  default: () => roleState,
}));

// The profiles hook is mocked whole. Its real `fetchVersions` is the unstable
// identity the loop above was about, and the page must no longer touch it — see
// `does not offer fetchVersions a way back into the effect` below.
const versionsState = vi.hoisted(() => ({
  versions: [{ versionName: 'lending', isActive: true }],
  loading: false,
  error: null as string | null,
  fetchVersions: vi.fn(),
}));
vi.mock('../../../hooks/use-configuration-versions', () => ({
  // ⚠️ A NEW `fetchVersions` identity on every call, deliberately, because that
  // is what the real hook does — it is a plain arrow function, not a
  // `useCallback`. A mock handing back one stable function would make the loop
  // this file is here to catch impossible to reproduce: the effect would settle
  // on the second render and the test would pass against the broken code.
  // `versions` keeps its array identity, as the real hook's does on an
  // unrestricted scope.
  default: () => ({ ...versionsState, fetchVersions: (...args: unknown[]) => versionsState.fetchVersions(...args) }),
}));

// The revision selector renders nothing unless the profile has history, so the
// edit-modal case needs two revisions here to have anything to assert against.
const revisionState = vi.hoisted(() => ({
  revisions: [
    { revision: 9, published: true, createdAt: '2026-09-01T10:00:00Z' },
    { revision: 7, published: false, createdAt: '2026-08-29T10:00:00Z' },
  ],
  loading: false,
  error: null as string | null,
  loadRevisions: vi.fn(),
}));
vi.mock('../../../hooks/use-config-profile-revisions', () => ({
  default: () => revisionState,
}));

import ConfigPrefixMappingsLayout from '../ConfigPrefixMappingsLayout';

const MAPPINGS = [
  {
    prefix: 'acme/invoices/',
    matchKind: 'prefix',
    configProfile: 'lending',
    configRevision: 7,
    metadataPrecedence: 'mapping',
    enabled: true,
    description: 'Invoice intake',
    updatedAt: '2026-09-01T12:00:00Z',
    updatedBy: 'admin@example.com',
  },
];

const listCalls = () => mockGraphql.mock.calls.filter(([args]) => String(args.query).includes('listConfigPrefixMappings')).length;

/**
 * Hard stop for the loop this file is here to catch.
 *
 * A re-firing effect whose work is a resolved promise floods the microtask
 * queue, and that starves vitest's own `testTimeout` — measured: the suite ran
 * past five minutes against the broken code instead of failing at ten seconds.
 * So past this many list requests the transport stops feeding the loop and
 * returns a promise that never settles, which lets React go quiet and lets the
 * assertion on the call count be what reports the failure. A hanging red mark is
 * the most expensive shape one can have.
 */
const LIST_CALL_CAP = 5;

/** Route each operation by the field name embedded in the generated document. */
const routeGraphql = ({ query }: { query: string }) => {
  if (query.includes('listConfigPrefixMappings')) {
    if (listCalls() > LIST_CALL_CAP) return new Promise(() => {});
    return Promise.resolve({ data: { listConfigPrefixMappings: { success: true, mappings: MAPPINGS } } });
  }
  if (query.includes('putConfigPrefixMapping')) {
    return Promise.resolve({ data: { putConfigPrefixMapping: { success: true } } });
  }
  if (query.includes('deleteConfigPrefixMapping')) {
    return Promise.resolve({ data: { deleteConfigPrefixMapping: { success: true } } });
  }
  if (query.includes('resolveConfigPrefixMapping')) {
    return Promise.resolve({ data: { resolveConfigPrefixMapping: { success: false } } });
  }
  return Promise.resolve({ data: {} });
};

const putCalls = () => mockGraphql.mock.calls.filter(([args]) => String(args.query).includes('putConfigPrefixMapping'));

describe('ConfigPrefixMappingsLayout', () => {
  beforeEach(() => {
    roleState.isAdmin = true;
    roleState.loading = false;
    roleState.sessionError = false;
    roleState.retrySession = vi.fn();
    versionsState.versions = [{ versionName: 'lending', isActive: true }];
    versionsState.loading = false;
    versionsState.error = null;
    revisionState.error = null;
    revisionState.loading = false;
    mockGraphql.mockReset();
    mockGraphql.mockImplementation(routeGraphql);
  });

  it('refuses a non-admin, naming what a mapping decides', async () => {
    roleState.isAdmin = false;
    render(<ConfigPrefixMappingsLayout />);
    expect(await screen.findByText(/must be an administrator/i)).toBeInTheDocument();
    // And it does not ask the API anything it has no business asking.
    expect(mockGraphql).not.toHaveBeenCalled();
  });

  it('reports an unreadable session as unknown rather than as a refusal', async () => {
    // Distinct from the case above because the remedies are opposite: this one
    // usually clears on a retry, and the retry is offered rather than only a
    // page reload.
    roleState.isAdmin = false;
    roleState.sessionError = true;
    render(<ConfigPrefixMappingsLayout />);

    expect(await screen.findByText(/could not be read/i)).toBeInTheDocument();
    expect(screen.queryByText(/must be an administrator/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(roleState.retrySession).toHaveBeenCalled();
  });

  it('lists the mappings in the order the server returned them', async () => {
    render(<ConfigPrefixMappingsLayout />);
    expect(await screen.findByText('acme/invoices/')).toBeInTheDocument();
    expect(screen.getByText('lending')).toBeInTheDocument();
    expect(screen.getByText('r7 (pinned)')).toBeInTheDocument();
  });

  it('issues exactly one list request and does not loop', async () => {
    // The regression this file exists for. The failing shape was an effect whose
    // dependency array held a function with a new identity every render, so each
    // state update it caused re-fired it: requests for as long as the tab was
    // open. Settling and then staying settled across further timer and
    // microtask turns is the observable form of "not looping".
    render(<ConfigPrefixMappingsLayout />);
    await screen.findByText('acme/invoices/');
    expect(listCalls()).toBe(1);

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    expect(listCalls()).toBe(1);

    // The table must also have left `loading` — under the loop it never did,
    // because every re-fire set it back.
    expect(screen.queryByText('Loading mappings...')).not.toBeInTheDocument();
  });

  it('does not offer fetchVersions a way back into the effect', async () => {
    // `fetchVersions` is a plain arrow function in `use-configuration-versions`,
    // so its identity changes every render and it cannot appear in any
    // dependency array here. The hook fetches once on its own mount; this page
    // calling it again was the loop. Six other components share that hook, so
    // the fix belongs here rather than in a `useCallback` there.
    render(<ConfigPrefixMappingsLayout />);
    await screen.findByText('acme/invoices/');
    expect(versionsState.fetchVersions).not.toHaveBeenCalled();
  });

  it('keeps the pinned revision when the edit modal is opened', async () => {
    // r7 is pinned on the stored mapping. Opening the form must show r7 still
    // selected: `putConfigPrefixMapping` is a full replace, so a revision
    // cleared here is a revision unpinned on the next save, with the admin
    // having asked for nothing of the kind.
    render(<ConfigPrefixMappingsLayout />);
    await screen.findByText('acme/invoices/');

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));

    await screen.findByText(/Edit mapping for/);
    expect(await screen.findByText('r7')).toBeInTheDocument();
    expect(screen.queryByText('Current (r9)')).not.toBeInTheDocument();
  });

  it('saves an edited mapping with its revision still pinned', async () => {
    render(<ConfigPrefixMappingsLayout />);
    await screen.findByText('acme/invoices/');

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    await screen.findByText(/Edit mapping for/);

    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(putCalls().length).toBe(1));
    expect(putCalls()[0][0].variables).toMatchObject({ prefix: 'acme/invoices/', configProfile: 'lending', configRevision: 7 });
  });

  it('refuses a prefix that already has a mapping, pointing at Edit', async () => {
    render(<ConfigPrefixMappingsLayout />);
    await screen.findByText('acme/invoices/');

    fireEvent.click(screen.getByRole('button', { name: 'Create mapping' }));
    const prefixInput = await screen.findByPlaceholderText('acme/invoices/');
    fireEvent.change(prefixInput, { target: { value: 'acme/invoices/' } });

    expect(await screen.findByText(/already exists/i)).toBeInTheDocument();
    expect(screen.getByText(/Use Edit on that row/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();

    // Nothing was sent: the create-or-replace mutation never got the chance to
    // overwrite the stored mapping's profile, revision and conflict mode.
    expect(putCalls().length).toBe(0);
  });

  it('refuses a prefix that could never match, as an error rather than a hint', async () => {
    // `constraintText` is advisory — it is not announced as a problem and it
    // leaves Save enabled. A prefix S3 can never produce is not advice.
    render(<ConfigPrefixMappingsLayout />);
    await screen.findByText('acme/invoices/');

    fireEvent.click(screen.getByRole('button', { name: 'Create mapping' }));
    const prefixInput = await screen.findByPlaceholderText('acme/invoices/');
    fireEvent.change(prefixInput, { target: { value: 'acme//invoices/' } });

    expect(await screen.findByText(/must not contain '\/\/'/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
  });

  it('says why the profile list is empty instead of offering an empty dropdown', async () => {
    versionsState.versions = [];
    versionsState.error = 'Failed to fetch versions';
    render(<ConfigPrefixMappingsLayout />);
    await screen.findByText('acme/invoices/');

    fireEvent.click(screen.getByRole('button', { name: 'Create mapping' }));
    // The message appears on the field and inside the dropdown's status slot,
    // so this asserts presence rather than a single occurrence.
    expect((await screen.findAllByText('Failed to fetch versions')).length).toBeGreaterThan(0);
  });

  it('reports a failed key resolution instead of stopping the spinner silently', async () => {
    // `previewAssignment` swallows its errors and returns null by design, so the
    // admin box has to supply its own failure path or a 403 reads as "resolved,
    // nothing to say".
    render(<ConfigPrefixMappingsLayout />);
    await screen.findByText('acme/invoices/');

    fireEvent.change(screen.getByPlaceholderText('acme/invoices/january.pdf'), {
      target: { value: 'acme/invoices/january.pdf' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Resolve' }));

    expect(await screen.findByText(/Could not resolve that key/i)).toBeInTheDocument();
  });

  it('sends one delete however many times Delete is clicked', async () => {
    // The second of two clicks is answered "No configuration prefix mapping for
    // 'acme/invoices/'", so a delete that worked reports as a failure.
    let resolveDelete: (value: unknown) => void = () => {};
    mockGraphql.mockImplementation((args: { query: string }) => {
      if (String(args.query).includes('deleteConfigPrefixMapping')) {
        return new Promise((resolve) => {
          resolveDelete = resolve;
        });
      }
      return routeGraphql(args);
    });

    render(<ConfigPrefixMappingsLayout />);
    await screen.findByText('acme/invoices/');

    // Two buttons are named "Delete": the row's inline link and the
    // confirmation modal's primary action. Both are in the DOM from the first
    // render, because Cloudscape's Modal hides its children with a CSS class
    // rather than unmounting them — so they are told apart by which dialog
    // contains them, not by waiting for one to appear.
    const deleteDialog = screen.getByText('Delete prefix mapping').closest('[role="dialog"]') as HTMLElement;
    const rowDelete = screen.getAllByRole('button', { name: 'Delete' }).find((button) => !deleteDialog.contains(button)) as HTMLElement;

    fireEvent.click(rowDelete);
    const confirm = within(deleteDialog).getByRole('button', { name: 'Delete' });
    fireEvent.click(confirm);
    fireEvent.click(confirm);

    const deleteCalls = () => mockGraphql.mock.calls.filter(([a]) => String(a.query).includes('deleteConfigPrefixMapping')).length;
    await waitFor(() => expect(deleteCalls()).toBe(1));

    await act(async () => {
      resolveDelete({ data: { deleteConfigPrefixMapping: { success: true } } });
    });
    expect(deleteCalls()).toBe(1);
  });

  it('gives the icon-only refresh control a name', async () => {
    render(<ConfigPrefixMappingsLayout />);
    await screen.findByText('acme/invoices/');
    expect(screen.getByRole('button', { name: 'Refresh mappings' })).toBeInTheDocument();
  });
});
