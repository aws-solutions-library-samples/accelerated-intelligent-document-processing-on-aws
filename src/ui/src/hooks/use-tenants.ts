// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useCallback, useState } from 'react';
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from '../api/client-shim';
import { listTenants, putTenant, deleteTenant } from '../graphql/generated';

const logger = new ConsoleLogger('useTenants');
const client = generateClient();

/**
 * One tenant declaration: the Input-bucket prefixes and the Configuration Profile
 * pattern that belong to one tenant. See `idp_common/config/tenants.py`.
 */
export interface Tenant {
  tenantId: string;
  /** Folder prefixes, each ending in '/'. Disjoint from every other tenant's. */
  prefixes: string[];
  /** A literal stem ending in '-', '_' or '.', then '*' — e.g. `acme-*`. */
  profilePattern: string;
  /** Claim or entitlement values that grant this tenant (TenantGrantSource IdpClaim or Api). */
  grantedBy?: string[] | null;
  description?: string | null;
  createdAt?: string | null;
  createdBy?: string | null;
  updatedAt?: string | null;
  updatedBy?: string | null;
}

/**
 * The scope written for a user an identity provider or entitlement push grants no
 * tenant. No profile name can equal it, so it means "no access". Mirrors
 * `NO_TENANT_GRANTED` in `idp_common/user_scope.py`; never display it as a profile.
 */
export const NO_TENANT_GRANTED = '!no-tenant-granted';

/** The stack's `TenantGrantSource` parameter: who grants a user's tenant. */
export type GrantSource = 'Manual' | 'IdpClaim' | 'Api';

/** The stack's `TenantIsolationMode` parameter, as the server reports it. */
export type IsolationMode = 'Strict' | 'Off';

export interface PutTenantInput {
  tenantId: string;
  prefixes: string[];
  profilePattern: string;
  grantedBy?: string[];
  description?: string;
}

/**
 * Server errors are surfaced verbatim, as `useConfigPrefixMappings` does: a
 * boundary refusal ("Prefix 'clients/acme/' overlaps tenant 'globex'…", "Existing
 * prefix mapping conflicts with this tenant set…") names exactly what to change,
 * and paraphrasing it would throw that away.
 */
const errorMessage = (error: { type?: string | null; message?: string | null } | null | undefined, fallback: string) =>
  error?.message || fallback;

const useTenants = () => {
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [isolationMode, setIsolationMode] = useState<IsolationMode | null>(null);
  // `null` until read, and on a failed read: callers must not assume Manual then.
  const [grantSource, setGrantSource] = useState<GrantSource | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadTenants = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await client.graphql({ query: listTenants });
      const response = result.data.listTenants;
      if (!response?.success) {
        setError(errorMessage(response?.error, 'Failed to load tenants'));
        setTenants([]);
        return;
      }
      setTenants((response.tenants ?? []).filter(Boolean) as Tenant[]);
      setIsolationMode(response.isolationMode === 'Strict' ? 'Strict' : 'Off');
      const source = response.grantSource;
      // Anything other than the three known values is unknown, not Manual: reading
      // an absent field as Manual would offer scope edits the server then refuses.
      setGrantSource(source === 'Manual' || source === 'IdpClaim' || source === 'Api' ? source : null);
    } catch (err) {
      logger.error('Error loading tenants', err);
      setError('Failed to load tenants');
      setTenants([]);
    } finally {
      setLoading(false);
    }
  }, []);

  const saveTenant = useCallback(async (input: PutTenantInput): Promise<boolean> => {
    setError(null);
    try {
      const result = await client.graphql({
        query: putTenant,
        variables: {
          tenantId: input.tenantId,
          prefixes: input.prefixes,
          profilePattern: input.profilePattern,
          description: input.description || undefined,
          grantedBy: input.grantedBy ?? [],
        },
      });
      const response = result.data.putTenant;
      if (!response?.success) {
        setError(errorMessage(response?.error, 'Failed to save the tenant'));
        return false;
      }
      return true;
    } catch (err) {
      logger.error('Error saving a tenant', err);
      setError('Failed to save the tenant');
      return false;
    }
  }, []);

  const removeTenant = useCallback(async (tenantId: string): Promise<boolean> => {
    setError(null);
    try {
      const result = await client.graphql({ query: deleteTenant, variables: { tenantId } });
      const response = result.data.deleteTenant;
      if (!response?.success) {
        setError(errorMessage(response?.error, 'Failed to delete the tenant'));
        return false;
      }
      return true;
    } catch (err) {
      logger.error('Error deleting a tenant', err);
      setError('Failed to delete the tenant');
      return false;
    }
  }, []);

  return { tenants, isolationMode, grantSource, loading, error, setError, loadTenants, saveTenant, removeTenant };
};

export default useTenants;
