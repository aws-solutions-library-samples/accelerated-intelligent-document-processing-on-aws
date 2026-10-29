// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Which Configuration Profile Capacity Planning reads for a caller whose
 * `allowedConfigVersions` scope excludes `default`.
 *
 * The profile list, role, settings and documents hooks are mocked at the module
 * boundary; `useConfiguration` runs for real over a mocked transport that refuses
 * `default` to a scoped caller and leaves every other profile pending, so the
 * assertions are on the profiles the page actually asks the API for.
 *
 * The last block holds every request until the test answers it, so it can make
 * an earlier request answer after a later one and assert which profile's
 * configuration the page then uses.
 */

import React from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { act, render, screen } from '@testing-library/react';

interface ListedVersion {
  versionName: string;
  isActive?: boolean;
}

const state = vi.hoisted(() => ({
  graphql: vi.fn(),
  list: { versions: [] as ListedVersion[], loaded: false, loading: true, error: null as string | null },
  role: { allowedConfigVersions: null as string[] | null, loading: false },
  refused: new Set<string>(),
}));

vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({ graphql: state.graphql }),
}));

vi.mock('../../../hooks/use-configuration-versions', () => ({
  default: () => ({
    ...state.list,
    getVersionOptions: () =>
      [...state.list.versions]
        .sort((a, b) => a.versionName.localeCompare(b.versionName))
        .map((v) => ({ label: v.isActive ? `${v.versionName} (Active)` : v.versionName, value: v.versionName })),
  }),
}));

vi.mock('../../../hooks/use-user-role', () => ({
  default: () => state.role,
}));

vi.mock('../../../contexts/settings', () => ({ default: () => ({ settings: {} }) }));

vi.mock('../../../contexts/documents', () => ({
  default: () => ({
    documents: [],
    isDocumentsListLoading: false,
    setIsDocumentsListLoading: vi.fn(),
    periodsToLoad: 1,
    setPeriodsToLoad: vi.fn(),
    customDateRange: null,
    setCustomDateRange: vi.fn(),
  }),
}));

vi.mock('../DocumentPickerModal', () => ({ default: () => null }));
vi.mock('../../common/DateRangeModal', () => ({ default: () => null }));

import CapacityPlanningLayout from '../CapacityPlanningLayout';

const SCOPE = ['tenant-a_*'];

const listAnswered = (versions: ListedVersion[]) => {
  state.list = { versions, loaded: true, loading: false, error: null };
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
      Default: JSON.stringify({ classification: { combined: { llm: { model: `${name}-classifier` } } } }),
      Custom: JSON.stringify({}),
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
  vi.spyOn(console, 'log').mockImplementation(() => {});
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
  state.list = { versions: [], loaded: false, loading: true, error: null };
  scopedTo(null);
});

describe('CapacityPlanningLayout — a scoped caller', () => {
  it('reads only the in-scope profile it selects, never default', async () => {
    scopedTo(SCOPE);
    const { rerender } = render(<CapacityPlanningLayout />);
    await settle();
    expect(requestedProfiles()).toEqual([]);

    listAnswered([{ versionName: 'tenant-a_prod' }, { versionName: 'tenant-a_dev' }]);
    rerender(<CapacityPlanningLayout />);
    await settle();

    expect(requestedProfiles()).toEqual(['tenant-a_dev']);
  });

  it('says plainly that the scope holds no profile instead of saying the configuration is not loaded', async () => {
    scopedTo(SCOPE);
    listAnswered([]);
    render(<CapacityPlanningLayout />);
    await settle();

    expect(screen.getByText('No configuration profiles in your scope')).toBeInTheDocument();
    expect(screen.queryByText(/Configuration not loaded/)).not.toBeInTheDocument();
    expect(requestedProfiles()).toEqual([]);
  });
});

describe('CapacityPlanningLayout — an unscoped caller', () => {
  it('reads default on arrival, then the active profile once the list answers', async () => {
    const { rerender } = render(<CapacityPlanningLayout />);
    await settle();
    expect(requestedProfiles()).toEqual(['default']);

    listAnswered([{ versionName: 'default' }, { versionName: 'lending', isActive: true }]);
    rerender(<CapacityPlanningLayout />);
    await settle();

    expect(requestedProfiles()).toEqual(['default', 'lending']);
    expect(screen.queryByText('No configuration profiles in your scope')).not.toBeInTheDocument();
  });
});

describe('CapacityPlanningLayout — answers that arrive out of order', () => {
  it('uses the selected profile when default, asked for first, answers last', async () => {
    const release = holdEveryRequest();
    state.role = { allowedConfigVersions: null, loading: true };
    const { rerender } = render(<CapacityPlanningLayout />);
    await settle();
    listAnswered([{ versionName: 'default' }, { versionName: 'lending', isActive: true }]);
    rerender(<CapacityPlanningLayout />);
    await settle();
    state.role = { allowedConfigVersions: null, loading: false };
    rerender(<CapacityPlanningLayout />);
    await settle();
    expect(requestedProfiles()).toEqual(['default', 'lending']);

    await release('lending', answer('lending'));
    await release('default', answer('default'));

    expect(screen.getByText('Model: lending-classifier')).toBeInTheDocument();
    expect(screen.queryByText('Model: default-classifier')).not.toBeInTheDocument();
  });

  it('does not read default once it has selected a profile, even when the scope then reads as unrestricted', async () => {
    const release = holdEveryRequest();
    state.role = { allowedConfigVersions: null, loading: true };
    const { rerender } = render(<CapacityPlanningLayout />);
    await settle();
    listAnswered([{ versionName: 'tenant-a_dev', isActive: true }]);
    rerender(<CapacityPlanningLayout />);
    await settle();
    expect(requestedProfiles()).toEqual(['tenant-a_dev']);

    state.role = { allowedConfigVersions: null, loading: false };
    rerender(<CapacityPlanningLayout />);
    await settle();
    await release('tenant-a_dev', answer('tenant-a_dev'));

    expect(requestedProfiles()).toEqual(['tenant-a_dev']);
    expect(screen.getByText('Model: tenant-a_dev-classifier')).toBeInTheDocument();
    expect(screen.queryByText(/Configuration not loaded/)).not.toBeInTheDocument();
  });
});
