// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Calls the HOST's API directly from the feature UI through the host's
 * REST-backed, GraphQL-shaped client (`window.IdpFeatureHost.generateClient`).
 *
 * AppSync was removed from the accelerator — the host UI now POSTs operations to
 * a REST dispatcher (`/op/<field>`). The host exposes its shim client as a
 * window global (see src/ui/src/components/feature-page/feature-host-globals.ts)
 * so features use the SAME transport, carrying the user's Cognito token and
 * group memberships. (Calling `aws-amplify/api`'s generateClient().graphql()
 * here instead throws "No GraphQL endpoint configured in Amplify.configure()".)
 *
 * The Config Pairing wizard uses four host operations:
 *   getConfigVersions                      — list existing configuration profiles
 *   getConfigVersion(versionName)          — fetch a version's config JSON
 *   updateConfiguration(versionName, ...)  — create/update a version
 *                                            (saveAsVersion:true creates a new one)
 *   setActiveVersion(versionName)          — activate a version
 *
 * updateConfiguration + saveAsVersion require the Admin role (enforced
 * server-side in configuration_resolver.py). A non-Admin sees a friendly error.
 */

interface GraphqlClient {
  graphql: (operation: {
    query: string;
    variables?: Record<string, unknown>;
  }) => Promise<unknown>;
}

interface FeatureHostWindow {
  IdpFeatureHost?: { generateClient?: () => GraphqlClient };
}

let _client: GraphqlClient | null = null;
function getClient(): GraphqlClient {
  // Lazily created on first use (NOT at module eval) — the UMD bundle can load
  // before the host installs its globals.
  if (!_client) {
    const host = (window as unknown as FeatureHostWindow).IdpFeatureHost;
    if (!host?.generateClient) {
      throw new Error(
        'Host API client not available (window.IdpFeatureHost.generateClient). ' +
          'The host UI may be older than the version that exposes it.',
      );
    }
    _client = host.generateClient();
  }
  return _client;
}

const GET_CONFIG_VERSIONS = /* GraphQL */ `
  query GetConfigVersions {
    getConfigVersions {
      success
      versions { versionName isActive description }
      error { message }
    }
  }
`;

const GET_CONFIG_VERSION = /* GraphQL */ `
  query GetConfigVersion($versionName: String!) {
    getConfigVersion(versionName: $versionName) {
      success
      Custom
      Default
      error { message }
    }
  }
`;

const UPDATE_CONFIGURATION = /* GraphQL */ `
  mutation UpdateConfiguration(
    $versionName: String!
    $customConfig: AWSJSON!
    $description: String
  ) {
    updateConfiguration(
      versionName: $versionName
      customConfig: $customConfig
      description: $description
    ) {
      success
      message
      error { message }
    }
  }
`;

const SET_ACTIVE_VERSION = /* GraphQL */ `
  mutation SetActiveVersion($versionName: String!) {
    setActiveVersion(versionName: $versionName) {
      success
      message
      error { message }
    }
  }
`;

type GraphQLResult<T> = { data?: T; errors?: Array<{ message: string }> };

/** Turn an Amplify rejection (a plain {errors:[{message}]} object) into text. */
export function graphqlErrorMessage(err: unknown): string {
  if (err instanceof Error) return err.message;
  if (err && typeof err === 'object') {
    const e = err as { errors?: Array<{ message?: string }>; message?: string };
    if (Array.isArray(e.errors) && e.errors.length) {
      return e.errors.map((x) => x?.message ?? String(x)).join('; ');
    }
    if (typeof e.message === 'string') return e.message;
    try {
      return JSON.stringify(err);
    } catch {
      /* fall through */
    }
  }
  return String(err);
}

function unwrap<T>(result: unknown): T {
  const r = result as GraphQLResult<T>;
  if (r.errors?.length) throw new Error(r.errors.map((e) => e.message).join('; '));
  if (!r.data) throw new Error('Empty GraphQL response');
  return r.data;
}

export interface ConfigVersion {
  versionName: string;
  isActive?: boolean;
  description?: string;
}

export async function listConfigVersions(): Promise<ConfigVersion[]> {
  const result = await getClient().graphql({ query: GET_CONFIG_VERSIONS });
  const data = unwrap<{
    getConfigVersions?: {
      success: boolean;
      versions?: ConfigVersion[];
      error?: { message?: string };
    };
  }>(result);
  const r = data.getConfigVersions;
  if (!r?.success) throw new Error(r?.error?.message || 'Could not list configuration profiles');
  return (r.versions || []).filter((v) => !!v.versionName);
}

/**
 * Fetch a version's *effective* config as a plain object. The resolver returns
 * Custom (overrides) and Default (built-in) as JSON strings; we merge shallowly
 * with Custom winning, matching how the host presents an "effective" config.
 */
export async function getConfig(versionName: string): Promise<Record<string, unknown>> {
  const result = await getClient().graphql({
    query: GET_CONFIG_VERSION,
    variables: { versionName },
  });
  const data = unwrap<{
    getConfigVersion?: {
      success: boolean;
      Custom?: string;
      Default?: string;
      error?: { message?: string };
    };
  }>(result);
  const r = data.getConfigVersion;
  if (!r?.success) throw new Error(r?.error?.message || `Could not read ${versionName}`);
  const parse = (s?: string): Record<string, unknown> => {
    if (!s) return {};
    try {
      const v = JSON.parse(s) as unknown;
      return v && typeof v === 'object' ? (v as Record<string, unknown>) : {};
    } catch {
      return {};
    }
  };
  return { ...parse(r.Default), ...parse(r.Custom) };
}

/**
 * Create (or overwrite) a configuration profile with the given full config object.
 * Sets saveAsVersion:true so the host writes it as a NEW non-active version.
 */
export async function saveConfigVersion(
  versionName: string,
  config: Record<string, unknown>,
  description: string,
): Promise<void> {
  const customConfig = JSON.stringify({ ...config, saveAsVersion: true });
  const result = await getClient().graphql({
    query: UPDATE_CONFIGURATION,
    variables: { versionName, customConfig, description },
  });
  const data = unwrap<{
    updateConfiguration?: { success: boolean; error?: { message?: string } };
  }>(result);
  if (!data.updateConfiguration?.success) {
    throw new Error(
      data.updateConfiguration?.error?.message || `Could not save ${versionName}`,
    );
  }
}

export async function activateVersion(versionName: string): Promise<void> {
  const result = await getClient().graphql({
    query: SET_ACTIVE_VERSION,
    variables: { versionName },
  });
  const data = unwrap<{
    setActiveVersion?: { success: boolean; error?: { message?: string } };
  }>(result);
  if (!data.setActiveVersion?.success) {
    throw new Error(
      data.setActiveVersion?.error?.message || `Could not activate ${versionName}`,
    );
  }
}
