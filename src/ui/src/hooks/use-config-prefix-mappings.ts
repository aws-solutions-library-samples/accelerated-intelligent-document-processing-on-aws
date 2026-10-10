// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useCallback, useState } from 'react';
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from '../api/client-shim';
import {
  listConfigPrefixMappings,
  putConfigPrefixMapping,
  deleteConfigPrefixMapping,
  resolveConfigPrefixMapping,
} from '../graphql/generated';

const logger = new ConsoleLogger('useConfigPrefixMappings');
const client = generateClient();

/** What a mapping does when the object also carries conflicting upload metadata. */
export type MetadataPrecedence = 'mapping' | 'metadata' | 'reject';

/**
 * One config prefix mapping: everything landing under an S3 prefix in the Input
 * bucket processes under one Configuration Profile.
 */
export interface ConfigPrefixMapping {
  /**
   * The identity of the mapping. A trailing '/' makes it a prefix match; without
   * one it matches that single exact key. Never normalize this in either
   * direction — the slash IS the mode selector, so appending one would make an
   * exact-key mapping impossible to express.
   */
  prefix: string;
  /** Derived from `prefix`: 'prefix' or 'exact'. */
  matchKind?: string | null;
  configProfile: string;
  /** null means the profile's published revision, so the mapping follows promotions. */
  configRevision?: number | null;
  metadataPrecedence?: string | null;
  enabled?: boolean | null;
  description?: string | null;
  createdAt?: string | null;
  createdBy?: string | null;
  updatedAt?: string | null;
  updatedBy?: string | null;
}

/** What an object at a given key would process under. A dry run. */
export interface ConfigAssignmentPreview {
  objectKey: string;
  /**
   * The resolved profile is outside the caller's scope. Every naming field is
   * then null — the API deliberately does not return a profile name a scoped
   * caller is not entitled to see.
   */
  outOfScope?: boolean | null;
  configProfile?: string | null;
  configRevision?: number | null;
  source?: string | null;
  mappingPrefix?: string | null;
  conflict?: boolean | null;
  rejected?: boolean | null;
  reason?: string | null;
}

/**
 * What a save did, which is three outcomes rather than two.
 *
 * `saved-with-warning` is the partial success the server marks `PartialSuccess`:
 * the mapping was written and the retention pin on its revision was not, so the
 * row exists and the admin still has something to do about it. A caller that
 * collapses it into `failed` leaves the saved row off the table.
 */
export type SaveOutcome = 'saved' | 'saved-with-warning' | 'failed';

export interface PutMappingInput {
  prefix: string;
  configProfile: string;
  configRevision?: number | null;
  metadataPrecedence?: MetadataPrecedence;
  enabled?: boolean;
  description?: string;
}

/**
 * Server errors are surfaced verbatim. A validation refusal here is written for
 * the admin ("a mapping prefix must not start with '/'…"), and paraphrasing it
 * into "Failed to save" would throw away the only thing that tells them what to
 * change.
 */
const errorMessage = (error: { type?: string | null; message?: string | null } | null | undefined, fallback: string) =>
  error?.message || fallback;

const useConfigPrefixMappings = () => {
  const [mappings, setMappings] = useState<ConfigPrefixMapping[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /**
   * Every mapping, in the order the server returns them — most-specific-first,
   * which is the order resolution evaluates. Do not re-sort in the table: a
   * longest-prefix rule shown in any other order cannot be read correctly.
   */
  const loadMappings = useCallback(async (preserveError = false) => {
    setLoading(true);
    // `preserveError` exists for exactly one caller: the partial-success save,
    // where the mapping WAS written but its revision pin failed. That row has to
    // appear in the table while the warning explaining what to do about it stays
    // on screen, and clearing the error would drop the only notice that a pinned
    // revision is unprotected. A genuine load failure below still reports itself.
    if (!preserveError) setError(null);
    try {
      const result = await client.graphql({ query: listConfigPrefixMappings });
      const response = result.data.listConfigPrefixMappings;
      if (!response?.success) {
        setError(errorMessage(response?.error, 'Failed to load configuration prefix mappings'));
        setMappings([]);
        return;
      }
      setMappings((response.mappings ?? []).filter(Boolean) as ConfigPrefixMapping[]);
    } catch (err) {
      logger.error('Error loading configuration prefix mappings', err);
      setError('Failed to load configuration prefix mappings');
      setMappings([]);
    } finally {
      setLoading(false);
    }
  }, []);

  const saveMapping = useCallback(async (input: PutMappingInput): Promise<SaveOutcome> => {
    setError(null);
    try {
      const result = await client.graphql({
        query: putConfigPrefixMapping,
        variables: {
          prefix: input.prefix,
          configProfile: input.configProfile,
          configRevision: input.configRevision ?? undefined,
          metadataPrecedence: input.metadataPrecedence ?? 'mapping',
          enabled: input.enabled ?? true,
          description: input.description || undefined,
        },
      });
      const response = result.data.putConfigPrefixMapping;
      if (!response?.success) {
        setError(errorMessage(response?.error, 'Failed to save the mapping'));
        // `PartialSuccess` is the server's marker for the one failure where the
        // mapping exists anyway: the put landed and the retention pin on its
        // revision did not. The caller has to treat that differently from a
        // refusal, so it is reported rather than flattened into `false`.
        return response?.error?.type === 'PartialSuccess' ? 'saved-with-warning' : 'failed';
      }
      return 'saved';
    } catch (err) {
      logger.error('Error saving a configuration prefix mapping', err);
      setError('Failed to save the mapping');
      return 'failed';
    }
  }, []);

  const removeMapping = useCallback(async (prefix: string): Promise<boolean> => {
    setError(null);
    try {
      const result = await client.graphql({ query: deleteConfigPrefixMapping, variables: { prefix } });
      const response = result.data.deleteConfigPrefixMapping;
      if (!response?.success) {
        setError(errorMessage(response?.error, 'Failed to delete the mapping'));
        return false;
      }
      return true;
    } catch (err) {
      logger.error('Error deleting a configuration prefix mapping', err);
      setError('Failed to delete the mapping');
      return false;
    }
  }, []);

  /**
   * The dry run. `metadataProfile` / `metadataRevision` declare what the caller
   * intends to send, because there is no object yet to read metadata from — so an
   * answer given without them describes an upload that specifies no profile.
   *
   * Deliberately does not set the shared `error` state: this runs on every
   * keystroke behind a debounce in the upload panel, and a transient failure
   * there must not paint an error banner over a form the user is still filling
   * in. Returns null and the caller shows nothing.
   */
  const previewAssignment = useCallback(
    async (
      objectKey: string,
      metadataProfile?: string | null,
      metadataRevision?: number | null,
    ): Promise<ConfigAssignmentPreview | null> => {
      if (!objectKey) return null;
      try {
        const result = await client.graphql({
          query: resolveConfigPrefixMapping,
          variables: {
            objectKey,
            metadataProfile: metadataProfile || undefined,
            metadataRevision: metadataRevision ?? undefined,
          },
        });
        const response = result.data.resolveConfigPrefixMapping;
        if (!response?.success || !response.assignment) return null;
        return response.assignment as ConfigAssignmentPreview;
      } catch (err) {
        logger.debug('Could not preview the configuration assignment', err);
        return null;
      }
    },
    [],
  );

  return { mappings, loading, error, setError, loadMappings, saveMapping, removeMapping, previewAssignment };
};

export default useConfigPrefixMappings;
