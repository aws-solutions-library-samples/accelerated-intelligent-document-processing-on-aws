// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Saving a direct edit. What has to hold: a reviewer-only caller saves the prediction
 * through saveSectionReviewEdits and never writes the baseline; an Admin or Author
 * uploads the prediction to its output key and the baseline to the baseline bucket at
 * the same key; both files get the same `_editHistory` entry; and changed paths drop
 * array indices.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

const graphql = vi.fn();
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: (...a: unknown[]) => graphql(...a) }) }));

import { diffLeafPaths, saveDirectSectionEdits } from '../direct-edit-save';
import type { SaveDirectSectionEditsOptions } from '../direct-edit-save';

const fetchMock = vi.fn();

const opts = (over: Partial<SaveDirectSectionEditsOptions> = {}): SaveDirectSectionEditsOptions => ({
  objectKey: 'in/doc.pdf',
  sectionId: '1',
  outputUri: 's3://out-bucket/in/doc.pdf/sections/1/result.json',
  username: 'alice',
  reviewerOnly: false,
  originalPrediction: { inference_result: { Name: 'A' } },
  prediction: { inference_result: { Name: 'B' } },
  originalBaseline: { inference_result: { Name: 'A' } },
  baseline: { inference_result: { Name: 'C' } },
  baselineBucket: 'baseline-bucket',
  ...over,
});

const queryName = (call: unknown[]) => String((call[0] as { query: string }).query);
const uploads = () => graphql.mock.calls.filter((c) => queryName(c).includes('uploadDocument')).map((c) => c[0].variables);
const putBodies = () => fetchMock.mock.calls.map((c) => JSON.parse((c[1] as { body: string }).body));

beforeEach(() => {
  graphql.mockReset().mockImplementation(async ({ query, variables }: { query: string; variables: Record<string, string> }) => {
    if (query.includes('uploadDocument')) {
      return {
        data: { uploadDocument: { presignedUrl: `https://put/${variables.bucket}/${variables.fileName}`, usePostMethod: 'false' } },
      };
    }
    return { data: { saveSectionReviewEdits: { success: true } } };
  });
  fetchMock.mockReset().mockResolvedValue({ ok: true });
  vi.stubGlobal('fetch', fetchMock);
});

describe('saveDirectSectionEdits', () => {
  it('saves a reviewer-only prediction through saveSectionReviewEdits and skips the baseline', async () => {
    const saved = await saveDirectSectionEdits(opts({ reviewerOnly: true }));
    expect(graphql).toHaveBeenCalledTimes(1);
    expect(queryName(graphql.mock.calls[0])).toContain('saveSectionReviewEdits');
    const sent = graphql.mock.calls[0][0].variables;
    expect([sent.objectKey, sent.sectionId]).toEqual(['in/doc.pdf', '1']);
    expect(JSON.parse(sent.editedData).inference_result).toEqual({ Name: 'B' });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(saved.baselineSkipped).toBe(true);
  });

  it('uploads an Admin prediction and baseline to the same key in their buckets', async () => {
    const saved = await saveDirectSectionEdits(opts());
    expect(uploads()).toEqual([
      { fileName: 'result.json', prefix: 'in/doc.pdf/sections/1', contentType: 'application/json', bucket: 'out-bucket' },
      { fileName: 'result.json', prefix: 'in/doc.pdf/sections/1', contentType: 'application/json', bucket: 'baseline-bucket' },
    ]);
    expect(fetchMock.mock.calls.map((c) => c[0])).toEqual([
      'https://put/out-bucket/result.json',
      'https://put/baseline-bucket/result.json',
    ]);
    expect(saved.baselineSkipped).toBe(false);
  });

  it('appends the same edit entry to the prediction and the baseline', async () => {
    const saved = await saveDirectSectionEdits(
      opts({
        originalPrediction: { inference_result: { Name: 'A' }, _editHistory: [{ old: 1 }] },
        prediction: { inference_result: { Name: 'B' }, _editHistory: [{ old: 1 }] },
      }),
    );
    const [prediction, baseline] = putBodies();
    expect(prediction._editHistory).toHaveLength(2);
    expect(baseline._editHistory).toHaveLength(1);
    const entry = prediction._editHistory[1];
    expect(baseline._editHistory[0]).toEqual(entry);
    expect(entry.editedBy).toBe('alice');
    expect(entry.predictionEdits.diffs).toEqual({ Name: { originalValue: 'A', newValue: 'B' } });
    expect(entry.baselineEdits.diffs).toEqual({ Name: { originalValue: 'A', newValue: 'C' } });
    expect(saved.prediction._editHistory).toEqual(prediction._editHistory);
  });

  it('writes nothing for a side that did not change', async () => {
    await saveDirectSectionEdits(opts({ baseline: { inference_result: { Name: 'A' } } }));
    expect(uploads().map((u) => u.bucket)).toEqual(['out-bucket']);
  });
});

describe('diffLeafPaths', () => {
  it('drops array indices from the changed paths', () => {
    const changes = diffLeafPaths(
      { inference_result: { Rows: [{ Amount: 1 }, { Amount: 2 }], Address: { City: 'X' } } },
      { inference_result: { Rows: [{ Amount: 1 }, { Amount: 3 }], Address: { City: 'Y' } } },
    );
    expect([...changes.keys()].sort()).toEqual(['Address.City', 'Rows.Amount']);
    expect(changes.get('Rows.Amount')).toEqual({ original: 2, current: 3 });
  });
});
