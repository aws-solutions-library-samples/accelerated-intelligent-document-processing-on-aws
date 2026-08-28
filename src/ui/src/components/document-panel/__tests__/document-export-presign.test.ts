// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Tests for how export assets are signed.
 *
 * The browser's Cognito identity role grants s3:GetObject on the Output and Input
 * buckets only — the EvaluationBaselineBucket is deliberately absent — so signing
 * a baseline URI with the browser's own credentials produces a URL that 403s at
 * fetch time. Baseline (and any other non-granted bucket) must therefore be
 * signed by the backend getFilePresignedUrl resolver, which holds read access to
 * every IDP bucket behind its own allow-list.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';

const { mockClientSign, mockBrowserSign } = vi.hoisted(() => ({
  mockClientSign: vi.fn(),
  mockBrowserSign: vi.fn(),
}));

vi.mock('../../common/generate-s3-presigned-url', () => ({ default: mockBrowserSign }));
vi.mock('../../../graphql/generated', () => ({ getFilePresignedUrl: 'query GetFilePresignedUrl' }));
vi.mock('../../../api/client-shim', () => ({ generateClient: () => ({ graphql: mockClientSign }) }));

import { exportDocuments, presignForExport, type ExportableDocument, type ExportSettings } from '../document-export';

const SETTINGS: ExportSettings = {
  InputBucket: 'input-bkt',
  OutputBucket: 'output-bkt',
  EvaluationBaselineBucket: 'baseline-bkt',
};

const CREDENTIALS = { accessKeyId: 'AKIA', secretAccessKey: 'shh' };

beforeEach(() => {
  mockBrowserSign.mockReset().mockResolvedValue('https://browser-signed.example/');
  mockClientSign.mockReset().mockResolvedValue({ data: { getFilePresignedUrl: { presignedUrl: 'https://resolver-signed.example/' } } });
});

describe('presignForExport', () => {
  it('signs OutputBucket and InputBucket objects client-side (no extra round trip)', async () => {
    await expect(presignForExport('s3://output-bkt/a/b.json', CREDENTIALS, SETTINGS)).resolves.toBe('https://browser-signed.example/');
    await expect(presignForExport('s3://input-bkt/a.pdf', CREDENTIALS, SETTINGS)).resolves.toBe('https://browser-signed.example/');
    expect(mockBrowserSign).toHaveBeenCalledTimes(2);
    expect(mockClientSign).not.toHaveBeenCalled();
  });

  it('signs baseline-bucket objects through the backend resolver', async () => {
    await expect(presignForExport('s3://baseline-bkt/a/b.json', CREDENTIALS, SETTINGS)).resolves.toBe('https://resolver-signed.example/');
    expect(mockBrowserSign).not.toHaveBeenCalled();
    expect(mockClientSign).toHaveBeenCalledWith({
      query: 'query GetFilePresignedUrl',
      variables: { s3Uri: 's3://baseline-bkt/a/b.json' },
    });
  });

  it('routes unknown buckets and unloaded settings through the resolver rather than guessing', async () => {
    await expect(presignForExport('s3://surprise/a.json', CREDENTIALS, SETTINGS)).resolves.toBe('https://resolver-signed.example/');
    await expect(presignForExport('s3://output-bkt/a.json', CREDENTIALS, null)).resolves.toBe('https://resolver-signed.example/');
    expect(mockBrowserSign).not.toHaveBeenCalled();
  });

  it('fails loudly when the resolver returns no URL', async () => {
    mockClientSign.mockResolvedValue({ data: { getFilePresignedUrl: null } });
    await expect(presignForExport('s3://baseline-bkt/a.json', CREDENTIALS, SETTINGS)).rejects.toThrow(/returned no URL/);
  });
});

describe('exportDocuments default signing', () => {
  it('uses the resolver for baselines and client signing for predictions in the same archive', async () => {
    const doc: ExportableDocument = {
      objectKey: 'doc.pdf',
      evaluationStatus: 'COMPLETED',
      sections: [{ Id: 's1', OutputJSONUri: 's3://output-bkt/doc.pdf/sections/s1/result.json' }],
    };
    const fetchFn = vi.fn(async () => ({
      ok: true,
      status: 200,
      statusText: 'OK',
      arrayBuffer: async () => new ArrayBuffer(2),
    }));

    const result = await exportDocuments([doc], SETTINGS, {
      scope: 'all',
      credentials: CREDENTIALS,
      fetchFn,
    });

    expect(result.errors).toEqual([]);
    // Prediction JSON signed in-browser, baseline JSON signed by the resolver
    expect(mockBrowserSign.mock.calls.map((c) => c[0])).toContain('s3://output-bkt/doc.pdf/sections/s1/result.json');
    expect(mockClientSign.mock.calls.map((c) => c[0].variables.s3Uri)).toEqual(['s3://baseline-bkt/doc.pdf/sections/s1/result.json']);
  });
});
