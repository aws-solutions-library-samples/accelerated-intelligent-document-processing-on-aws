// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useCallback, useRef, useState } from 'react';
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from '../api/client-shim';
import {
  listConfigProfileRevisions,
  getConfigProfileRevision,
  restoreConfigProfileRevision,
  labelConfigProfileRevision,
  deleteConfigProfileRevision,
} from '../graphql/generated';

const logger = new ConsoleLogger('useConfigProfileRevisions');
const client = generateClient();

/**
 * One immutable snapshot of a Configuration Profile's configuration, cut on
 * every save.
 */
export interface ConfigProfileRevision {
  revision: number;
  createdAt?: string | null;
  createdBy?: string | null;
  label?: string | null;
  notes?: string | null;
  sizeBytes?: number | null;
  classFingerprint?: string | null;
  /** Pinned by a test run; exempt from retention pruning. */
  pinned?: boolean | null;
  /** The revision the profile's current configuration reflects. */
  published?: boolean | null;
}

/** Server errors are surfaced verbatim: scope denials must not look like bugs. */
const errorMessage = (error: { type?: string | null; message?: string | null } | null | undefined, fallback: string) =>
  error?.message || fallback;

const useConfigProfileRevisions = () => {
  const [revisions, setRevisions] = useState<ConfigProfileRevision[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /**
   * False when the profile's configuration was changed without recording a
   * revision: new documents then run the published revision, not what the
   * editor shows. Null when there is nothing to compare.
   */
  const [profileMatchesPublished, setProfileMatchesPublished] = useState<boolean | null>(null);

  // Only the newest request may set state: switching profiles quickly can
  // resolve an older request last, and its verdict would then be shown under the
  // newer profile's name.
  const latestRequest = useRef(0);

  const loadRevisions = useCallback(async (profileName: string) => {
    if (!profileName) return;
    latestRequest.current += 1;
    const request = latestRequest.current;
    const isStale = () => request !== latestRequest.current;
    setLoading(true);
    setError(null);
    setProfileMatchesPublished(null);
    try {
      const result = await client.graphql({ query: listConfigProfileRevisions, variables: { profileName } });
      if (isStale()) return;
      const response = result.data.listConfigProfileRevisions;
      if (!response?.success) {
        setError(errorMessage(response?.error, 'Failed to load revision history'));
        setRevisions([]);
        setProfileMatchesPublished(null);
        return;
      }
      setRevisions((response.revisions ?? []).filter(Boolean) as ConfigProfileRevision[]);
      setProfileMatchesPublished(response.profileMatchesPublished ?? null);
    } catch (err) {
      if (isStale()) return;
      logger.error('Error loading configuration profile revisions', err);
      setError('Failed to load revision history');
      setRevisions([]);
      setProfileMatchesPublished(null);
    } finally {
      if (!isStale()) setLoading(false);
    }
  }, []);

  /** Full configuration recorded in one revision, or null when unavailable. */
  const fetchRevisionConfig = useCallback(async (profileName: string, revision: number): Promise<Record<string, unknown> | null> => {
    try {
      const result = await client.graphql({ query: getConfigProfileRevision, variables: { profileName, revision } });
      const response = result.data.getConfigProfileRevision;
      if (!response?.success || !response.config) {
        setError(errorMessage(response?.error, `Failed to load r${revision}`));
        return null;
      }
      return JSON.parse(response.config) as Record<string, unknown>;
    } catch (err) {
      logger.error('Error loading revision configuration', err);
      setError(`Failed to load r${revision}`);
      return null;
    }
  }, []);

  const restoreRevision = useCallback(
    async (profileName: string, revision: number): Promise<number | null> => {
      setError(null);
      try {
        const result = await client.graphql({ query: restoreConfigProfileRevision, variables: { profileName, revision } });
        const response = result.data.restoreConfigProfileRevision;
        if (!response?.success) {
          setError(errorMessage(response?.error, `Failed to restore r${revision}`));
          return null;
        }
        await loadRevisions(profileName);
        return response.revision ?? null;
      } catch (err) {
        logger.error('Error restoring revision', err);
        setError(`Failed to restore r${revision}`);
        return null;
      }
    },
    [loadRevisions],
  );

  const labelRevision = useCallback(
    async (profileName: string, revision: number, label: string, notes: string): Promise<boolean> => {
      setError(null);
      try {
        const result = await client.graphql({
          query: labelConfigProfileRevision,
          variables: { profileName, revision, label, notes },
        });
        const response = result.data.labelConfigProfileRevision;
        if (!response?.success) {
          setError(errorMessage(response?.error, `Failed to update r${revision}`));
          return false;
        }
        await loadRevisions(profileName);
        return true;
      } catch (err) {
        logger.error('Error labeling revision', err);
        setError(`Failed to update r${revision}`);
        return false;
      }
    },
    [loadRevisions],
  );

  const deleteRevision = useCallback(
    async (profileName: string, revision: number): Promise<boolean> => {
      setError(null);
      try {
        const result = await client.graphql({ query: deleteConfigProfileRevision, variables: { profileName, revision } });
        const response = result.data.deleteConfigProfileRevision;
        if (!response?.success) {
          setError(errorMessage(response?.error, `Failed to delete r${revision}`));
          return false;
        }
        await loadRevisions(profileName);
        return true;
      } catch (err) {
        logger.error('Error deleting revision', err);
        setError(`Failed to delete r${revision}`);
        return false;
      }
    },
    [loadRevisions],
  );

  return {
    revisions,
    profileMatchesPublished,
    loading,
    error,
    setError,
    loadRevisions,
    fetchRevisionConfig,
    restoreRevision,
    labelRevision,
    deleteRevision,
  };
};

export default useConfigProfileRevisions;
