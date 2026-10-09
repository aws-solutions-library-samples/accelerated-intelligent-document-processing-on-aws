// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Tests for the config-prefix-mappings hook.
 *
 * The properties worth pinning here are the ones a future "cleanup" would
 * plausibly break:
 *
 * - **The server's order is preserved.** The API returns mappings
 *   most-specific-first, which is the order resolution evaluates. Re-sorting the
 *   list (by prefix, by profile, by date) would make a longest-prefix rule
 *   unreadable in the table while looking tidier.
 * - **Server messages reach the user verbatim.** A validation refusal is written
 *   for the admin ("a mapping prefix must not start with '/'…"); replacing it
 *   with "Failed to save" throws away the only thing that says what to change.
 * - **The dry run never sets the shared error state.** It runs on every keystroke
 *   behind a debounce in the upload panel, so a transient failure must not paint
 *   an error banner over a form someone is still filling in.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';

const { graphql } = vi.hoisted(() => ({ graphql: vi.fn() }));

vi.mock('../../api/client-shim', () => ({
  generateClient: () => ({ graphql }),
}));

vi.mock('../../graphql/generated', () => ({
  listConfigPrefixMappings: 'listConfigPrefixMappings',
  putConfigPrefixMapping: 'putConfigPrefixMapping',
  deleteConfigPrefixMapping: 'deleteConfigPrefixMapping',
  resolveConfigPrefixMapping: 'resolveConfigPrefixMapping',
}));

import useConfigPrefixMappings from '../use-config-prefix-mappings';

describe('useConfigPrefixMappings', () => {
  beforeEach(() => {
    graphql.mockReset();
  });

  it('keeps the order the server returned, which is the order resolution uses', async () => {
    graphql.mockResolvedValue({
      data: {
        listConfigPrefixMappings: {
          success: true,
          mappings: [
            { prefix: 'acme/invoices/2026/', configProfile: 'q1' },
            { prefix: 'acme/invoices/', configProfile: 'invoices' },
            { prefix: 'acme/', configProfile: 'broad' },
          ],
        },
      },
    });

    const { result } = renderHook(() => useConfigPrefixMappings());
    await act(async () => {
      await result.current.loadMappings();
    });

    expect(result.current.mappings.map((m) => m.prefix)).toEqual(['acme/invoices/2026/', 'acme/invoices/', 'acme/']);
  });

  it('surfaces a server validation message verbatim', async () => {
    graphql.mockResolvedValue({
      data: {
        putConfigPrefixMapping: {
          success: false,
          error: { type: 'ValidationError', message: "A mapping prefix must not start with '/'." },
        },
      },
    });

    const { result } = renderHook(() => useConfigPrefixMappings());
    let ok = true;
    await act(async () => {
      ok = await result.current.saveMapping({ prefix: '/acme/', configProfile: 'lending' });
    });

    expect(ok).toBe(false);
    expect(result.current.error).toBe("A mapping prefix must not start with '/'.");
  });

  it('defaults a new mapping to mapping-wins and enabled', async () => {
    graphql.mockResolvedValue({ data: { putConfigPrefixMapping: { success: true } } });

    const { result } = renderHook(() => useConfigPrefixMappings());
    await act(async () => {
      await result.current.saveMapping({ prefix: 'acme/', configProfile: 'lending' });
    });

    expect(graphql).toHaveBeenCalledWith(
      expect.objectContaining({
        variables: expect.objectContaining({ metadataPrecedence: 'mapping', enabled: true }),
      }),
    );
  });

  it('reports a deletion that found nothing rather than claiming success', async () => {
    graphql.mockResolvedValue({
      data: {
        deleteConfigPrefixMapping: {
          success: false,
          error: { type: 'NotFound', message: "No configuration prefix mapping for 'nope/'" },
        },
      },
    });

    const { result } = renderHook(() => useConfigPrefixMappings());
    let ok = true;
    await act(async () => {
      ok = await result.current.removeMapping('nope/');
    });

    expect(ok).toBe(false);
    expect(result.current.error).toContain('nope/');
  });

  it('returns the dry-run assignment', async () => {
    graphql.mockResolvedValue({
      data: {
        resolveConfigPrefixMapping: {
          success: true,
          assignment: {
            objectKey: 'acme/x.pdf',
            configProfile: 'lending',
            configRevision: 7,
            source: 'prefix-mapping',
            mappingPrefix: 'acme/',
            conflict: false,
            rejected: false,
            outOfScope: false,
            reason: "Prefix mapping 'acme/' assigned profile 'lending' r7.",
          },
        },
      },
    });

    const { result } = renderHook(() => useConfigPrefixMappings());
    let assignment = null as Awaited<ReturnType<typeof result.current.previewAssignment>>;
    await act(async () => {
      assignment = await result.current.previewAssignment('acme/x.pdf');
    });

    expect(assignment?.configProfile).toBe('lending');
    expect(assignment?.mappingPrefix).toBe('acme/');
  });

  it('does not set the error banner when the dry run fails', async () => {
    graphql.mockRejectedValue(new Error('network'));

    const { result } = renderHook(() => useConfigPrefixMappings());
    let assignment = null as Awaited<ReturnType<typeof result.current.previewAssignment>>;
    await act(async () => {
      assignment = await result.current.previewAssignment('acme/x.pdf');
    });

    expect(assignment).toBeNull();
    await waitFor(() => expect(result.current.error).toBeNull());
  });

  it('does not call the API for an empty key', async () => {
    const { result } = renderHook(() => useConfigPrefixMappings());
    await act(async () => {
      await result.current.previewAssignment('');
    });
    expect(graphql).not.toHaveBeenCalled();
  });
});
