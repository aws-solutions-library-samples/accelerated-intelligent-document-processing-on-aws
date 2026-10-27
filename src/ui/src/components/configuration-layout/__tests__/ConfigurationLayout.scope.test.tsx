// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Which Configuration Profile the View/Edit Configuration page opens for a caller
 * whose `allowedConfigVersions` scope excludes `default`.
 *
 * The profile list, the role and the library hooks are mocked at the module
 * boundary; `useConfiguration` runs for real over a mocked transport that refuses
 * `default` the way the server refuses it to a scoped caller and leaves every
 * other profile pending, so the page under test either asks for a profile it may
 * not read and shows the refusal, or does not.
 *
 * The last block holds every request until the test answers it, so it can make
 * an earlier request answer after a later one and assert what the editor is
 * given.
 */

import React from 'react';
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { act, render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

interface ListedVersion {
  versionName: string;
  isActive?: boolean;
}

const state = vi.hoisted(() => ({
  graphql: vi.fn(),
  list: { versions: [] as ListedVersion[], loaded: false, loading: true, error: null as string | null },
  role: { allowedConfigVersions: null as string[] | null, loading: false },
  refused: new Set<string>(),
  fetchVersions: vi.fn(),
  builder: [] as { currentVersionName: string; mergedConfig: unknown }[],
}));

vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({ graphql: state.graphql }),
}));

vi.mock('@monaco-editor/react', () => ({ default: () => null }));

vi.mock('../../../hooks/use-configuration-versions', () => ({
  default: () => ({
    ...state.list,
    fetchVersions: state.fetchVersions,
    fetchVersion: vi.fn(),
    setActiveVersion: vi.fn(),
    deleteVersion: vi.fn(),
    saveAsNewVersion: vi.fn(),
    getVersionOptions: () => [],
  }),
}));

vi.mock('../../../hooks/use-user-role', () => ({
  default: () => ({ isAdmin: false, canWrite: true, ...state.role }),
}));

vi.mock('../../../hooks/use-configuration-library', () => ({
  default: () => ({ listConfigurations: vi.fn(), getFile: vi.fn() }),
}));

vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));
vi.mock('../../../contexts/app', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../../contexts/app')>()),
  default: () => ({ setSuccessMessage: vi.fn() }),
}));

vi.mock('../ConfigBuilder', () => ({
  default: ({ currentVersionName, mergedConfig }: { currentVersionName: string; mergedConfig: unknown }) => {
    state.builder.push({ currentVersionName, mergedConfig });
    return null;
  },
}));
vi.mock('../ConfigurationVersionsTable', () => ({ default: () => null }));
vi.mock('../ConfigRevisionHistoryPanel', () => ({ default: () => null }));
vi.mock('../ConfigurationComparison', () => ({ default: () => null }));
vi.mock('../../common/CreateConfigProfileModal', () => ({ default: () => null }));

import ConfigurationLayout from '../ConfigurationLayout';

const SCOPE = ['tenant-a_*'];
const IN_SCOPE: ListedVersion[] = [{ versionName: 'tenant-a_dev' }, { versionName: 'tenant-a_prod' }];

const page = () => (
  <MemoryRouter>
    <ConfigurationLayout />
  </MemoryRouter>
);

const listLoading = () => {
  state.list = { versions: [], loaded: false, loading: true, error: null };
};

const listAnswered = (versions: ListedVersion[], error: string | null = null) => {
  state.list = { versions, loaded: true, loading: false, error };
};

const scopedTo = (scope: string[] | null) => {
  state.role = { allowedConfigVersions: scope, loading: false };
  state.refused = new Set(scope ? ['default'] : []);
};

const requestedProfiles = () => state.graphql.mock.calls.map(([args]) => args?.variables?.versionName).filter((name) => name !== undefined);

const settle = () =>
  act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });

const answer = (name: string) => ({
  data: {
    getConfigVersion: {
      success: true,
      Schema: JSON.stringify({ type: 'object', properties: {} }),
      Default: JSON.stringify({ profile: name }),
      Custom: JSON.stringify({}),
    },
  },
});

const refusal = (name: string) => ({
  data: {
    getConfigVersion: {
      success: false,
      error: { type: 'Unauthorized', message: `Access denied: version '${name}' is not in your allowed scope` },
    },
  },
});

const holdEveryRequest = () => {
  const held = new Map<string, (value: unknown) => void>();
  state.graphql.mockImplementation(
    ({ variables }: { variables: { versionName: string } }) => new Promise((resolve) => held.set(variables.versionName, resolve)),
  );
  return (name: string, value: unknown) =>
    act(async () => {
      held.get(name)!(value);
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
};

beforeEach(() => {
  state.graphql.mockReset();
  state.graphql.mockImplementation(({ variables }: { variables?: { versionName?: string } }) => {
    const name = variables?.versionName ?? '';
    if (state.refused.has(name)) {
      return Promise.resolve({
        data: {
          getConfigVersion: {
            success: false,
            error: { type: 'Unauthorized', message: `Access denied: version '${name}' is not in your allowed scope` },
          },
        },
      });
    }
    return new Promise(() => {});
  });
  state.fetchVersions.mockReset();
  state.builder = [];
  window.location.hash = '#/documents/config';
  listLoading();
  scopedTo(null);
});

afterEach(() => {
  window.location.hash = '';
});

describe('ConfigurationLayout — a scoped caller', () => {
  it('shows loading, not an error, while the in-scope list is loading', async () => {
    scopedTo(SCOPE);
    render(page());
    await settle();

    expect(screen.getByText('Loading configuration...')).toBeInTheDocument();
    expect(screen.queryByText('Error loading configuration')).not.toBeInTheDocument();
    expect(requestedProfiles()).toEqual([]);
  });

  it('opens the first in-scope profile when the active one is out of scope, and never asks for default', async () => {
    scopedTo(SCOPE);
    const { rerender } = render(page());
    await settle();
    listAnswered(IN_SCOPE);
    rerender(page());
    await settle();

    expect(requestedProfiles()).toEqual(['tenant-a_dev']);
    expect(screen.getByText('Loading configuration...')).toBeInTheDocument();
    expect(screen.queryByText('Error loading configuration')).not.toBeInTheDocument();
  });

  it('opens the active profile when it is in scope', async () => {
    scopedTo(SCOPE);
    listAnswered([{ versionName: 'tenant-a_dev' }, { versionName: 'tenant-a_prod', isActive: true }]);
    render(page());
    await settle();

    expect(requestedProfiles()).toEqual(['tenant-a_prod']);
  });

  it('still opens the profile the URL names straight away, before the list answers', async () => {
    scopedTo(SCOPE);
    window.location.hash = '#/documents/config?version=tenant-a_prod';
    render(page());
    await settle();

    expect(requestedProfiles()).toEqual(['tenant-a_prod']);
  });
});

describe('ConfigurationLayout — a scope that holds no profile', () => {
  it('says so plainly instead of showing an error', async () => {
    scopedTo(SCOPE);
    listAnswered([]);
    render(page());
    await settle();

    expect(screen.getByText('No configuration profiles in your scope')).toBeInTheDocument();
    expect(screen.queryByText('Error loading configuration')).not.toBeInTheDocument();
    expect(requestedProfiles()).toEqual([]);
  });

  it('reports a profile list that could not be read as that, with a retry', async () => {
    scopedTo(SCOPE);
    listAnswered([], 'Failed to fetch versions');
    render(page());
    await settle();

    expect(screen.getByText('Error loading configuration profiles')).toBeInTheDocument();
    expect(screen.getByText('Failed to fetch versions')).toBeInTheDocument();
    expect(screen.queryByText('No configuration profiles in your scope')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(state.fetchVersions).toHaveBeenCalledTimes(1);
    expect(requestedProfiles()).toEqual([]);
  });
});

describe('ConfigurationLayout — an unscoped caller', () => {
  it('opens default while the list loads, then the active profile once it answers', async () => {
    const { rerender } = render(page());
    await settle();
    expect(requestedProfiles()).toEqual(['default']);

    listAnswered([{ versionName: 'default' }, { versionName: 'lending', isActive: true }]);
    rerender(page());
    await settle();
    expect(requestedProfiles()).toEqual(['default', 'lending']);
  });

  it('still opens default when the list answers empty, and does not report an empty scope', async () => {
    listAnswered([]);
    render(page());
    await settle();

    expect(requestedProfiles()).toEqual(['default']);
    expect(screen.queryByText('No configuration profiles in your scope')).not.toBeInTheDocument();
  });

  it('asks for no profile until the scope of the caller has been read', async () => {
    state.role = { allowedConfigVersions: null, loading: true };
    render(page());
    await settle();

    expect(requestedProfiles()).toEqual([]);
    expect(screen.getByText('Loading configuration...')).toBeInTheDocument();
  });
});

describe('ConfigurationLayout — answers that arrive out of order', () => {
  it('gives the editor the active profile when default, asked for first, answers last', async () => {
    const release = holdEveryRequest();
    state.role = { allowedConfigVersions: null, loading: true };
    const { rerender } = render(page());
    await settle();
    state.role = { allowedConfigVersions: null, loading: false };
    rerender(page());
    await settle();
    listAnswered([{ versionName: 'default' }, { versionName: 'lending', isActive: true }]);
    rerender(page());
    await settle();
    expect(requestedProfiles()).toEqual(['default', 'lending']);

    await release('lending', answer('lending'));
    await release('default', answer('default'));

    expect(state.builder.at(-1)).toEqual({ currentVersionName: 'lending', mergedConfig: { profile: 'lending' } });
  });

  it('does not report a refusal of default that lands after the in-scope profile was asked for', async () => {
    const release = holdEveryRequest();
    const { rerender } = render(page());
    await settle();
    listAnswered(IN_SCOPE);
    rerender(page());
    await settle();
    expect(requestedProfiles()).toEqual(['default', 'tenant-a_dev']);

    await release('default', refusal('default'));
    await release('tenant-a_dev', answer('tenant-a_dev'));

    expect(screen.queryByText('Error loading configuration')).not.toBeInTheDocument();
    expect(state.builder.at(-1)).toEqual({ currentVersionName: 'tenant-a_dev', mergedConfig: { profile: 'tenant-a_dev' } });
  });
});
