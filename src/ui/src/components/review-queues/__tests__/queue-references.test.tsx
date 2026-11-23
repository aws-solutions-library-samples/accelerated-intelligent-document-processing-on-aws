// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The routing rules that send documents to a review queue: the lookup reads every page
 * (a page covers some configuration profiles, so it can be empty and not the last); the
 * hook answers for the queue it was asked about and no other; and a reference reads as
 * a revision ("published r3", "current") and a rule (label and id, id, or the default
 * outcome).
 */

import { act, renderHook, waitFor } from '@testing-library/react';
import { createIntl } from 'react-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }),
}));

import { referenceRevisionText, referenceRuleText } from '../QueueReferences';
import { getReviewQueueReferences } from '../reviewer-assignment';
import useQueueReferences from '../use-queue-references';

const reference = (profile: string, ruleId = 'rule') => ({ profile, revision: 1, published: true, ruleId, ruleLabel: null });
const page = (profiles: string[], nextToken: string | null = null) => ({
  data: { getReviewQueueReferences: { items: profiles.map((profile) => reference(profile)), nextToken } },
});
const variablesOf = (call: number) => (graphql.mock.calls[call][0] as { variables: Record<string, unknown> }).variables;

beforeEach(() => {
  graphql.mockReset();
});

describe('getReviewQueueReferences', () => {
  it('asks for the queue and returns its references', async () => {
    graphql.mockResolvedValueOnce(page(['lending', 'mortgage']));

    const references = await getReviewQueueReferences('claims');

    expect(references.map((r) => r.profile)).toEqual(['lending', 'mortgage']);
    expect(graphql).toHaveBeenCalledTimes(1);
    expect(variablesOf(0)).toEqual({ queueId: 'claims', nextToken: undefined });
  });

  it('reads on past a page that holds no reference', async () => {
    graphql
      .mockResolvedValueOnce(page([], 'default'))
      .mockResolvedValueOnce(page(['lending'], 'lending'))
      .mockResolvedValueOnce(page([], 'mortgage'))
      .mockResolvedValueOnce(page(['zeta']));

    const references = await getReviewQueueReferences('claims');

    expect(references.map((r) => r.profile)).toEqual(['lending', 'zeta']);
    expect(graphql.mock.calls.map((_call, index) => variablesOf(index).nextToken)).toEqual([undefined, 'default', 'lending', 'mortgage']);
  });

  it('fails when a page fails, so a partial list is not read as the whole', async () => {
    graphql.mockResolvedValueOnce(page(['lending'], 'lending')).mockRejectedValueOnce(new Error('boom'));

    await expect(getReviewQueueReferences('claims')).rejects.toThrow('boom');
  });
});

describe('useQueueReferences', () => {
  it('looks nothing up without a queue', () => {
    const { result } = renderHook(() => useQueueReferences(null));

    expect(result.current).toMatchObject({ references: null, loading: false, error: null });
    expect(graphql).not.toHaveBeenCalled();
  });

  it('is loading until the lookup answers, then holds the references', async () => {
    graphql.mockResolvedValueOnce(page(['lending']));
    const { result } = renderHook(() => useQueueReferences('claims'));

    expect(result.current).toMatchObject({ references: null, loading: true, error: null });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.references?.map((r) => r.profile)).toEqual(['lending']);
    expect(graphql).toHaveBeenCalledTimes(1);
  });

  it('holds the error of a failed lookup, and clears it when a reload answers', async () => {
    graphql.mockRejectedValueOnce(new Error('boom')).mockResolvedValueOnce(page([]));
    const { result } = renderHook(() => useQueueReferences('claims'));
    await waitFor(() => expect(result.current.error).toBeInstanceOf(Error));
    expect(result.current).toMatchObject({ references: null, loading: false });

    act(() => result.current.reload());

    expect(result.current).toMatchObject({ loading: true, error: null });
    await waitFor(() => expect(result.current.references).toEqual([]));
    expect(result.current).toMatchObject({ loading: false, error: null });
  });

  it('keeps the references on screen while a reload of the same queue runs', async () => {
    let answer: (value: unknown) => void = () => undefined;
    graphql.mockResolvedValueOnce(page(['lending'])).mockReturnValueOnce(new Promise((resolve) => (answer = resolve)));
    const { result } = renderHook(() => useQueueReferences('claims'));
    await waitFor(() => expect(result.current.references).toHaveLength(1));

    act(() => result.current.reload());

    expect(result.current.loading).toBe(true);
    expect(result.current.references?.map((r) => r.profile)).toEqual(['lending']);
    await act(async () => answer(page(['lending', 'mortgage'])));
    expect(result.current.references?.map((r) => r.profile)).toEqual(['lending', 'mortgage']);
  });

  it('shows nothing of the queue before when the queue changes, and looks the new one up', async () => {
    graphql.mockResolvedValueOnce(page(['lending'])).mockResolvedValueOnce(page(['mortgage']));
    const { result, rerender } = renderHook(({ queueId }: { queueId: string | null }) => useQueueReferences(queueId), {
      initialProps: { queueId: 'claims' as string | null },
    });
    await waitFor(() => expect(result.current.references).toHaveLength(1));

    rerender({ queueId: 'other' });

    expect(result.current).toMatchObject({ references: null, loading: true });
    await waitFor(() => expect(result.current.references?.map((r) => r.profile)).toEqual(['mortgage']));
    expect(variablesOf(1).queueId).toBe('other');

    rerender({ queueId: null });
    expect(result.current).toMatchObject({ references: null, loading: false, error: null });
  });

  it('never returns, on any render, the references of a queue other than the one asked about', async () => {
    // The effect that starts the new lookup runs after the first render with the new
    // queue; that render must not hand back what was loaded for the queue before.
    graphql.mockResolvedValueOnce(page(['lending'])).mockResolvedValueOnce(page(['mortgage']));
    const renders: { queueId: string; profiles: string[] | null }[] = [];
    const { result, rerender } = renderHook(
      ({ queueId }: { queueId: string }) => {
        const state = useQueueReferences(queueId);
        renders.push({ queueId, profiles: state.references?.map((r) => r.profile) ?? null });
        return state;
      },
      { initialProps: { queueId: 'claims' } },
    );
    await waitFor(() => expect(result.current.references).toHaveLength(1));

    rerender({ queueId: 'other' });
    await waitFor(() => expect(result.current.references?.map((r) => r.profile)).toEqual(['mortgage']));

    const forOther = renders.filter((seen) => seen.queueId === 'other');
    expect(forOther.length).toBeGreaterThan(1);
    expect(forOther.filter((seen) => seen.profiles?.includes('lending'))).toEqual([]);
    expect(forOther[0].profiles).toBeNull();
  });

  it('ignores an answer for a queue that is no longer the one asked about', async () => {
    let slow: (value: unknown) => void = () => undefined;
    graphql.mockReturnValueOnce(new Promise((resolve) => (slow = resolve))).mockResolvedValueOnce(page(['mortgage']));
    const { result, rerender } = renderHook(({ queueId }: { queueId: string }) => useQueueReferences(queueId), {
      initialProps: { queueId: 'claims' },
    });

    rerender({ queueId: 'other' });
    await waitFor(() => expect(result.current.references?.map((r) => r.profile)).toEqual(['mortgage']));
    await act(async () => slow(page(['lending'])));

    expect(result.current.references?.map((r) => r.profile)).toEqual(['mortgage']);
  });
});

describe('a reference in words', () => {
  const intl = createIntl({ locale: 'en' });

  it('names the published revision by its number, and a newer one as current', () => {
    expect(referenceRevisionText(intl, { published: true, revision: 3 })).toBe('published r3');
    expect(referenceRevisionText(intl, { published: true, revision: 0 })).toBe('published r0');
    expect(referenceRevisionText(intl, { published: true, revision: null })).toBe('published');
    expect(referenceRevisionText(intl, { published: false, revision: 4 })).toBe('current');
    expect(referenceRevisionText(intl, { published: false, revision: null })).toBe('current');
  });

  it('names a rule by its label and id, by its id, or as the default outcome', () => {
    expect(referenceRuleText(intl, { ruleId: 'big', ruleLabel: 'Big loans' })).toBe('Big loans (big)');
    expect(referenceRuleText(intl, { ruleId: 'big', ruleLabel: null })).toBe('big');
    expect(referenceRuleText(intl, { ruleId: 'big', ruleLabel: '' })).toBe('big');
    expect(referenceRuleText(intl, { ruleId: '', ruleLabel: null })).toBe('Default outcome');
  });
});
