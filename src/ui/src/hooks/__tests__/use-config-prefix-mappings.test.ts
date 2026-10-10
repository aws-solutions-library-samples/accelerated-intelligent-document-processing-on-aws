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
 * - **A revision of 0 reaches the wire.** `0` is falsy and legitimately
 *   numbered, so a `||` anywhere on these paths turns "pinned to r0" into
 *   "follow the published revision" — a different configuration, chosen by
 *   nobody.
 * - **The prefix reaches the wire byte for byte.** The trailing slash is the
 *   prefix/exact mode selector, so normalizing it in either direction changes
 *   which objects the mapping governs.
 *
 * Where a case asserts an *absence* — no error banner, no API call — it also
 * asserts that the call was attempted, because an absence on its own is equally
 * satisfied by a function that does nothing.
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

    // The call has to have been ATTEMPTED for the swallowed-error path to be
    // what was measured. Without this line the case passes against a
    // `previewAssignment` that is simply `async () => null` and never reaches
    // the network at all, which is a different function with the same outcome.
    expect(graphql).toHaveBeenCalledTimes(1);
    expect(assignment).toBeNull();
    await waitFor(() => expect(result.current.error).toBeNull());
  });

  it('does not set the error banner when the dry run is refused rather than thrown', async () => {
    // The other failure shape: a well-formed response that says no. It must be
    // as silent as a thrown one, for the same reason.
    graphql.mockResolvedValue({
      data: { resolveConfigPrefixMapping: { success: false, error: { type: 'Throttled', message: 'Rate exceeded' } } },
    });

    const { result } = renderHook(() => useConfigPrefixMappings());
    let assignment = null as Awaited<ReturnType<typeof result.current.previewAssignment>>;
    await act(async () => {
      assignment = await result.current.previewAssignment('acme/x.pdf');
    });

    expect(graphql).toHaveBeenCalledTimes(1);
    expect(assignment).toBeNull();
    await waitFor(() => expect(result.current.error).toBeNull());
  });

  it('calls the API for a key, and not for an empty one', async () => {
    // Both halves in one case on purpose. Asserting only "not called for ''"
    // passes against a `previewAssignment` that never calls anything, so the
    // positive half is what gives the negative half its meaning.
    graphql.mockResolvedValue({
      data: { resolveConfigPrefixMapping: { success: true, assignment: { objectKey: 'acme/x.pdf' } } },
    });

    const { result } = renderHook(() => useConfigPrefixMappings());
    await act(async () => {
      await result.current.previewAssignment('');
    });
    expect(graphql).not.toHaveBeenCalled();

    await act(async () => {
      await result.current.previewAssignment('acme/x.pdf', 'lending', 7);
    });
    expect(graphql).toHaveBeenCalledTimes(1);
    expect(graphql).toHaveBeenCalledWith(
      expect.objectContaining({
        query: 'resolveConfigPrefixMapping',
        variables: { objectKey: 'acme/x.pdf', metadataProfile: 'lending', metadataRevision: 7 },
      }),
    );
  });

  it('sends revision 0 to the wire, because 0 is a revision and not an absence', async () => {
    // `0` is falsy and legitimately numbered, so `|| undefined` anywhere on
    // either of these paths silently converts "pinned to r0" into "follow the
    // published revision" — a different configuration, chosen by nobody. `??`
    // is the only correct operator here and this is what holds it in place.
    graphql.mockResolvedValue({ data: { putConfigPrefixMapping: { success: true } } });

    const { result } = renderHook(() => useConfigPrefixMappings());
    await act(async () => {
      await result.current.saveMapping({ prefix: 'acme/', configProfile: 'lending', configRevision: 0 });
    });

    expect(graphql).toHaveBeenCalledWith(expect.objectContaining({ variables: expect.objectContaining({ configRevision: 0 }) }));

    graphql.mockReset();
    graphql.mockResolvedValue({ data: { resolveConfigPrefixMapping: { success: true, assignment: { objectKey: 'acme/x.pdf' } } } });
    await act(async () => {
      await result.current.previewAssignment('acme/x.pdf', 'lending', 0);
    });

    expect(graphql).toHaveBeenCalledWith(expect.objectContaining({ variables: expect.objectContaining({ metadataRevision: 0 }) }));
  });

  it('sends the prefix with its trailing slash intact', async () => {
    // The trailing slash IS the prefix/exact mode selector. Normalizing it in
    // either direction — appending one for tidiness, or stripping one — changes
    // which objects the mapping governs, and stripping it makes a folder mapping
    // into a mapping on a single object that will never exist.
    graphql.mockResolvedValue({ data: { putConfigPrefixMapping: { success: true } } });

    const { result } = renderHook(() => useConfigPrefixMappings());
    await act(async () => {
      await result.current.saveMapping({ prefix: 'acme/invoices/', configProfile: 'lending' });
    });

    expect(graphql).toHaveBeenCalledWith(expect.objectContaining({ variables: expect.objectContaining({ prefix: 'acme/invoices/' }) }));

    // And the exact-match spelling reaches the wire unslashed, which is the
    // same property seen from the other side.
    graphql.mockReset();
    graphql.mockResolvedValue({ data: { putConfigPrefixMapping: { success: true } } });
    await act(async () => {
      await result.current.saveMapping({ prefix: 'acme/invoices/january.pdf', configProfile: 'lending' });
    });

    expect(graphql).toHaveBeenCalledWith(
      expect.objectContaining({ variables: expect.objectContaining({ prefix: 'acme/invoices/january.pdf' }) }),
    );
  });

  it('deletes by the prefix it was given, slash and all', async () => {
    graphql.mockResolvedValue({ data: { deleteConfigPrefixMapping: { success: true } } });

    const { result } = renderHook(() => useConfigPrefixMappings());
    await act(async () => {
      await result.current.removeMapping('acme/invoices/');
    });

    expect(graphql).toHaveBeenCalledWith(expect.objectContaining({ variables: { prefix: 'acme/invoices/' } }));
  });
});
