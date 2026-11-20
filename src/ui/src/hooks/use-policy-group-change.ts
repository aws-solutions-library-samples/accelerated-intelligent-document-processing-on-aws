// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The Policy Discovery form's view of a policy group in the selected profile (#608):
// reads the profile's policy classes, finds the class carrying the typed group, and
// holds how the upload changes it (add, replace, or supersede chosen rules). The
// upload resolver checks the same things again; this only lets the form offer the
// group's current rules and refuse early.
import { useCallback, useEffect, useMemo, useState } from 'react';
import { generateClient } from '../api/client-shim';
import { getConfigVersion } from '../graphql/generated';
import {
  findPolicyGroupClass,
  groupChangeProblem,
  groupChangeVariables,
  policyGroupKey,
  type GroupChangeProblem,
  type GroupLookup,
  type PolicyGroupMode,
} from '../components/discovery/policyGroup';

const client = generateClient();

interface Options {
  /** Policy Discovery only. */
  enabled: boolean;
  /** The configuration profile the upload saves into. */
  version: string | null | undefined;
  /** The typed policy group (not yet normalised). */
  policyGroup: string;
}

export interface PolicyGroupChange {
  lookup: GroupLookup;
  loading: boolean;
  loadError: string | null;
  mode: PolicyGroupMode;
  setMode: (mode: PolicyGroupMode) => void;
  supersededRules: string[];
  setSupersededRules: (rules: string[]) => void;
  problem: GroupChangeProblem | null;
  variables: ReturnType<typeof groupChangeVariables>;
  refresh: () => void;
}

const readPolicyClasses = (custom: unknown): unknown => {
  const parsed = typeof custom === 'string' ? JSON.parse(custom) : custom;
  return parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>).policy_classes : undefined;
};

const usePolicyGroupChange = ({ enabled, version, policyGroup }: Options): PolicyGroupChange => {
  const [policyClasses, setPolicyClasses] = useState<unknown>(undefined);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [mode, setMode] = useState<PolicyGroupMode>('append');
  const [supersededRules, setSupersededRules] = useState<string[]>([]);
  const [reloads, setReloads] = useState(0);
  const groupKey = policyGroupKey(policyGroup);
  const readable = enabled && !!version && version !== 'default';

  useEffect(() => {
    if (!readable) {
      setPolicyClasses(undefined);
      setLoadError(null);
      return undefined;
    }
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    (async () => {
      try {
        const res = await client.graphql({ query: getConfigVersion, variables: { versionName: version as string } });
        const cfg = (res as { data?: { getConfigVersion?: { success?: boolean; Custom?: unknown; error?: { message?: string } } } })?.data
          ?.getConfigVersion;
        if (!cfg?.success) throw new Error(cfg?.error?.message || 'the profile could not be read');
        if (!cancelled) setPolicyClasses(readPolicyClasses(cfg.Custom));
      } catch (e) {
        if (!cancelled) {
          setPolicyClasses(undefined);
          setLoadError(e instanceof Error ? e.message : String(e));
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [readable, version, reloads]);

  // A different group or profile is a different set of rules: start again from Add.
  useEffect(() => {
    setMode('append');
    setSupersededRules([]);
  }, [groupKey, version]);

  const lookup = useMemo(
    () => (groupKey ? findPolicyGroupClass(policyClasses, policyGroup) : { status: 'none' as const }),
    [policyClasses, policyGroup, groupKey],
  );
  const refresh = useCallback(() => setReloads((n) => n + 1), []);

  return {
    lookup,
    loading,
    loadError,
    mode,
    setMode,
    supersededRules,
    setSupersededRules,
    problem: enabled && groupKey ? groupChangeProblem(mode, lookup, supersededRules) : null,
    variables: enabled && groupKey ? groupChangeVariables(mode, supersededRules) : {},
    refresh,
  };
};

export default usePolicyGroupChange;
