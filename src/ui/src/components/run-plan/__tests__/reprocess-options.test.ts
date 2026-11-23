// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * One start-step choice for several documents: a step is offered when every selected
 * document can start there, and a step some cannot start at says how many and why.
 */

import { describe, expect, it } from 'vitest';

import type { ReprocessDocumentResult, ReprocessOption } from '../../../graphql/generated/operation-types';
import { canStartAt, combineReprocessOptions, notQueued } from '../reprocess-options';
import { stepsForMode } from '../run-steps';

const PIPELINE = stepsForMode('pipeline').map((step) => step.id);
const BDA = stepsForMode('bda').map((step) => step.id);

/** A document's answer: every step of its mode available, except those given a reason. */
const answer = (objectKey: string, mode: 'pipeline' | 'bda', refused: Record<string, string | null> = {}): ReprocessOption => ({
  objectKey,
  mode,
  defaultStep: 'beginning',
  steps: (mode === 'bda' ? BDA : PIPELINE).map((id) => ({
    id,
    available: !(id in refused),
    skippable: false,
    reason: refused[id] ?? null,
  })),
});
const available = (options: { id: string; available: boolean }[]) => options.filter((o) => o.available).map((o) => o.id);
const option = (options: { id: string }[], id: string) => options.find((o) => o.id === id);

describe('combineReprocessOptions', () => {
  it('offers every step of the mode when every document can start anywhere', () => {
    const combined = combineReprocessOptions(['a', 'b'], [answer('a', 'pipeline'), answer('b', 'pipeline')]);

    expect(combined.mode).toBe('pipeline');
    expect(available(combined.options)).toEqual(PIPELINE);
    expect(combined.options.every((o) => o.blocked === undefined && o.reason === undefined)).toBe(true);
  });

  it('lists BDA mode only when every document is in it', () => {
    expect(combineReprocessOptions(['a', 'b'], [answer('a', 'bda'), answer('b', 'bda')])).toMatchObject({ mode: 'bda' });
    expect(available(combineReprocessOptions(['a'], [answer('a', 'bda')]).options)).toEqual(BDA);
    expect(combineReprocessOptions(['a', 'b'], [answer('a', 'bda'), answer('b', 'pipeline')]).mode).toBe('pipeline');
  });

  it('withholds a step one document cannot start at, with how many and the first reason', () => {
    const combined = combineReprocessOptions(
      ['a', 'b', 'c'],
      [
        answer('a', 'pipeline'),
        answer('b', 'pipeline', { extraction: 'OUTPUT_MISSING' }),
        answer('c', 'pipeline', { extraction: 'RUN_IN_PROGRESS' }),
      ],
    );

    expect(option(combined.options, 'extraction')).toEqual({
      id: 'extraction',
      available: false,
      reason: 'OUTPUT_MISSING',
      blocked: { count: 2, total: 3 },
    });
    expect(available(combined.options)).toEqual(PIPELINE.filter((id) => id !== 'extraction'));
  });

  it('withholds a step a BDA document does not have, among Pipeline documents', () => {
    const combined = combineReprocessOptions(['a', 'b'], [answer('a', 'pipeline'), answer('b', 'bda')]);

    expect(available(combined.options)).toEqual(BDA);
    for (const id of PIPELINE.filter((step) => !BDA.includes(step))) {
      expect(option(combined.options, id)).toEqual({ id, available: false, reason: 'STEP_NOT_IN_MODE', blocked: { count: 1, total: 2 } });
    }
  });

  it('offers nothing a document the server did not answer for could start at', () => {
    const combined = combineReprocessOptions(['a', 'missing'], [answer('a', 'pipeline')]);

    expect(available(combined.options)).toEqual([]);
    expect(option(combined.options, 'beginning')).toEqual({
      id: 'beginning',
      available: false,
      reason: null,
      blocked: { count: 1, total: 2 },
    });
    // An unanswered document is not known to be in BDA mode.
    expect(combineReprocessOptions(['missing'], []).mode).toBe('pipeline');
  });

  it('ignores an answer for a document that is not selected', () => {
    const combined = combineReprocessOptions(['a'], [answer('a', 'pipeline'), answer('other', 'pipeline', { beginning: 'MALWARE_GATE' })]);

    expect(available(combined.options)).toEqual(PIPELINE);
  });

  it('adds resume, which the server does not list, only when asked', () => {
    const answers = [answer('a', 'pipeline')];

    expect(option(combineReprocessOptions(['a'], answers).options, 'resume')).toBeUndefined();
    expect(option(combineReprocessOptions(['a'], answers, true).options, 'resume')).toEqual({ id: 'resume', available: true });
  });
});

describe('canStartAt', () => {
  const { options } = combineReprocessOptions(['a'], [answer('a', 'pipeline', { extraction: 'OUTPUT_MISSING' })]);

  it('is true for an available step, false for a withheld one and for one not listed', () => {
    expect(canStartAt(options, 'beginning')).toBe(true);
    expect(canStartAt(options, 'extraction')).toBe(false);
    expect(canStartAt(options, 'resume')).toBe(false);
  });
});

describe('notQueued', () => {
  it('keeps the documents no run was queued for, in order', () => {
    const results: ReprocessDocumentResult[] = [
      { objectKey: 'a', status: 'QUEUED' },
      { objectKey: 'b', status: 'REFUSED', reason: 'RUN_IN_PROGRESS' },
      { objectKey: 'c', status: 'FAILED' },
    ];

    expect(notQueued(results).map((r) => r.objectKey)).toEqual(['b', 'c']);
    expect(notQueued(results.slice(0, 1))).toEqual([]);
  });
});
