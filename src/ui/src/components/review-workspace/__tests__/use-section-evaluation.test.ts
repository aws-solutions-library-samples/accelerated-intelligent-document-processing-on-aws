// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The direct edit's evaluation baseline. What has to hold: nothing is read unless the
 * document has a baseline (BASELINE_AVAILABLE or COMPLETED); the baseline is read from
 * the evaluation baseline bucket at the section's output key and the results from the
 * output bucket; a different section reloads; and a reviewer-only caller can't edit
 * the baseline.
 */

import { renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }) }));
vi.mock('../../../contexts/settings', () => ({
  default: () => ({ settings: { OutputBucket: 'out-bucket', EvaluationBaselineBucket: 'baseline-bucket' } }),
}));

import useSectionEvaluation from '../use-section-evaluation';
import type { UseSectionEvaluationOptions } from '../use-section-evaluation';

const uris = () => graphql.mock.calls.map((c) => (c[0] as { variables: { s3Uri: string } }).variables.s3Uri);

const options = (over: Partial<UseSectionEvaluationOptions> = {}): UseSectionEvaluationOptions => ({
  enabled: true,
  objectKey: 'in/doc.pdf',
  outputUri: 's3://out-bucket/in/doc.pdf/sections/1/result.json',
  evaluationStatus: 'COMPLETED',
  reviewerOnly: false,
  ...over,
});

beforeEach(() => {
  graphql.mockReset().mockImplementation(async ({ variables }: { variables: { s3Uri: string } }) => ({
    data: { getFileContents: { isBinary: false, content: JSON.stringify({ uri: variables.s3Uri }) } },
  }));
});

describe('useSectionEvaluation', () => {
  it.each([null, 'RUNNING', 'NO_BASELINE'])('reads nothing when the evaluation status is %j', async (status) => {
    const { result } = renderHook(() => useSectionEvaluation(options({ evaluationStatus: status })));
    expect(result.current.available).toBe(false);
    expect(result.current.baseline).toBeNull();
    expect(graphql).not.toHaveBeenCalled();
  });

  it('builds the baseline and results URIs from the settings', async () => {
    const { result } = renderHook(() => useSectionEvaluation(options()));
    await waitFor(() => expect(result.current.baseline).not.toBeNull());
    expect(uris().sort()).toEqual([
      's3://baseline-bucket/in/doc.pdf/sections/1/result.json',
      's3://out-bucket/in/doc.pdf/evaluation/results.json',
    ]);
    expect(result.current.baseline).toEqual({ uri: 's3://baseline-bucket/in/doc.pdf/sections/1/result.json' });
    expect(result.current.originalBaseline).toEqual(result.current.baseline);
    expect(result.current.evaluationResults).toEqual({ uri: 's3://out-bucket/in/doc.pdf/evaluation/results.json' });
  });

  it('reloads when the section changes', async () => {
    const { result, rerender } = renderHook((props: UseSectionEvaluationOptions) => useSectionEvaluation(props), {
      initialProps: options(),
    });
    await waitFor(() => expect(result.current.baseline).not.toBeNull());
    rerender(options({ outputUri: 's3://out-bucket/in/doc.pdf/sections/2/result.json' }));
    await waitFor(() => expect(result.current.baseline).toEqual({ uri: 's3://baseline-bucket/in/doc.pdf/sections/2/result.json' }));
  });

  it('does not let a reviewer-only caller edit the baseline', async () => {
    const { result } = renderHook(() => useSectionEvaluation(options({ reviewerOnly: true })));
    await waitFor(() => expect(result.current.baseline).not.toBeNull());
    expect(result.current.canEditBaseline).toBe(false);
    expect(renderHook(() => useSectionEvaluation(options())).result.current.canEditBaseline).toBe(true);
  });
});
