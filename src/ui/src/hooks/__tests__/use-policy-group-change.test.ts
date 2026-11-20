// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';

const { graphql } = vi.hoisted(() => ({ graphql: vi.fn() }));

vi.mock('../../api/client-shim', () => ({
  generateClient: () => ({ graphql }),
}));

vi.mock('../../graphql/generated', () => ({
  getConfigVersion: 'getConfigVersion',
}));

import usePolicyGroupChange from '../use-policy-group-change';

const profile = (policyClasses: unknown[]) => ({
  data: { getConfigVersion: { success: true, Custom: JSON.stringify({ policy_classes: policyClasses }) } },
});

const GROUP_CLASS = {
  'x-aws-idp-policy-type': 'NCCI_Manual',
  'x-aws-idp-policy-group': 'NCCI Manual',
  rule_properties: { visit_limit: {}, modifier_25: {} },
};

beforeEach(() => {
  graphql.mockReset();
});

describe('usePolicyGroupChange', () => {
  it("reads the selected profile and finds the typed group's rules", async () => {
    graphql.mockResolvedValue(profile([GROUP_CLASS]));
    const { result } = renderHook(() => usePolicyGroupChange({ enabled: true, version: 'rules-profile', policyGroup: 'ncci manual' }));
    await waitFor(() => expect(result.current.lookup.status).toBe('found'));
    expect(graphql).toHaveBeenCalledWith({ query: 'getConfigVersion', variables: { versionName: 'rules-profile' } });
    expect(result.current.lookup.status === 'found' && result.current.lookup.ruleNames).toEqual(['visit_limit', 'modifier_25']);
    expect(result.current.variables).toEqual({});
    expect(result.current.problem).toBeNull();
  });

  it('builds the supersede variables, and refuses one with no rules', async () => {
    graphql.mockResolvedValue(profile([GROUP_CLASS]));
    const { result } = renderHook(() => usePolicyGroupChange({ enabled: true, version: 'rules-profile', policyGroup: 'NCCI Manual' }));
    await waitFor(() => expect(result.current.lookup.status).toBe('found'));
    act(() => result.current.setMode('supersede'));
    expect(result.current.problem).toBe('noRulesChosen');
    act(() => result.current.setSupersededRules(['visit_limit']));
    expect(result.current.problem).toBeNull();
    expect(result.current.variables).toEqual({ policyGroupMode: 'supersede', supersededRules: ['visit_limit'] });
  });

  it('starts again from Add when the group is renamed', async () => {
    graphql.mockResolvedValue(profile([GROUP_CLASS]));
    const { result, rerender } = renderHook((props) => usePolicyGroupChange(props), {
      initialProps: { enabled: true, version: 'rules-profile', policyGroup: 'NCCI Manual' },
    });
    await waitFor(() => expect(result.current.lookup.status).toBe('found'));
    act(() => {
      result.current.setMode('supersede');
      result.current.setSupersededRules(['visit_limit']);
    });
    rerender({ enabled: true, version: 'rules-profile', policyGroup: 'Other Manual' });
    expect(result.current.mode).toBe('append');
    expect(result.current.supersededRules).toEqual([]);
    expect(result.current.lookup.status).toBe('none');
  });

  it('refuses a replace of a group the profile does not hold', async () => {
    graphql.mockResolvedValue(profile([]));
    const { result } = renderHook(() => usePolicyGroupChange({ enabled: true, version: 'rules-profile', policyGroup: 'NCCI Manual' }));
    await waitFor(() => expect(graphql).toHaveBeenCalled());
    act(() => result.current.setMode('replace'));
    expect(result.current.problem).toBe('groupMissing');
  });

  it('reads nothing for a classes discovery or the read-only default profile', () => {
    renderHook(() => usePolicyGroupChange({ enabled: false, version: 'rules-profile', policyGroup: 'G' }));
    renderHook(() => usePolicyGroupChange({ enabled: true, version: 'default', policyGroup: 'G' }));
    expect(graphql).not.toHaveBeenCalled();
  });

  it('reports a profile it could not read, and still allows an append', async () => {
    graphql.mockResolvedValue({ data: { getConfigVersion: { success: false, error: { message: 'not in scope' } } } });
    const { result } = renderHook(() => usePolicyGroupChange({ enabled: true, version: 'rules-profile', policyGroup: 'G' }));
    await waitFor(() => expect(result.current.loadError).toBe('not in scope'));
    expect(result.current.problem).toBeNull();
  });
});
