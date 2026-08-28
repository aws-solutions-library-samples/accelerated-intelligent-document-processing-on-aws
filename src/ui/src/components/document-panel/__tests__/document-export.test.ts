// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it, vi } from 'vitest';
import JSZip from 'jszip';
import {
  constructBaselineUri,
  exportDocument,
  exportDocuments,
  parseS3Uri,
  sanitizeDocumentKey,
  uriToZipPath,
  type ExportableDocument,
  type ExportProgress,
  type ExportSettings,
} from '../document-export';

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const SETTINGS: ExportSettings = {
  InputBucket: 'input-bkt',
  OutputBucket: 'output-bkt',
  EvaluationBaselineBucket: 'baseline-bkt',
};

const DOC: ExportableDocument = {
  objectKey: 'tenant/one/lending.pdf',
  objectStatus: 'COMPLETED',
  configVersion: '1.2.3',
  pageCount: 3,
  evaluationStatus: 'COMPLETED',
  summaryReportUri: 's3://output-bkt/tenant/one/lending.pdf/summary/summary.md',
  evaluationReportUri: 's3://output-bkt/tenant/one/lending.pdf/evaluation/report.json',
  ruleValidationResultUri: 's3://output-bkt/tenant/one/lending.pdf/rule-validation/result.json',
  metering: { foo: { tokens: 123 } },
  sections: [
    { Id: 's1', Class: 'W2', PageIds: [1], OutputJSONUri: 's3://output-bkt/tenant/one/lending.pdf/sections/s1/result.json' },
    { Id: 's2', Class: 'Pay', PageIds: [2, 3], OutputJSONUri: 's3://output-bkt/tenant/one/lending.pdf/sections/s2/result.json' },
  ],
  pages: [
    {
      Id: 1,
      TextUri: 's3://output-bkt/tenant/one/lending.pdf/pages/1/result.json',
      TextConfidenceUri: 's3://output-bkt/tenant/one/lending.pdf/pages/1/textConfidence.json',
      OcrPageDataUri: 's3://output-bkt/tenant/one/lending.pdf/pages/1/pageData.json',
      ImageUri: 's3://output-bkt/tenant/one/lending.pdf/pages/1/image.jpg',
    },
    { Id: 2, TextUri: 's3://output-bkt/tenant/one/lending.pdf/pages/2/result.json' },
  ],
};

const textBody = (s: string): ArrayBuffer => {
  const encoded = new TextEncoder().encode(s);
  const out = new ArrayBuffer(encoded.byteLength);
  new Uint8Array(out).set(encoded);
  return out;
};
const bytesBody = (bytes: Uint8Array): ArrayBuffer => {
  const out = new ArrayBuffer(bytes.byteLength);
  new Uint8Array(out).set(bytes);
  return out;
};

type ResponseLike = { ok: boolean; status: number; statusText: string; arrayBuffer: () => Promise<ArrayBuffer> };

interface MockedFetchContext {
  responses: Record<string, ResponseLike>;
  fetched: string[];
  presigned: string[];
}

/** Creates a matched pair of `presignFn` + `fetchFn` that route by S3 URI. */
const makeMocks = (
  responses: Record<string, ResponseLike>,
): MockedFetchContext & {
  presignFn: NonNullable<Parameters<typeof exportDocument>[2]['presignFn']>;
  fetchFn: NonNullable<Parameters<typeof exportDocument>[2]['fetchFn']>;
} => {
  const ctx: MockedFetchContext = { responses, fetched: [], presigned: [] };
  const byUrl: Record<string, ResponseLike> = {};
  const presignFn = vi.fn(async (uri: string) => {
    ctx.presigned.push(uri);
    const url = `https://presigned.example/?uri=${encodeURIComponent(uri)}`;
    byUrl[url] = responses[uri] ?? {
      ok: false,
      status: 404,
      statusText: 'Not Found',
      arrayBuffer: async () => new ArrayBuffer(0),
    };
    return url;
  });
  const fetchFn = vi.fn(async (url: string) => {
    ctx.fetched.push(url);
    const resp = byUrl[url];
    if (!resp) throw new Error(`No mocked fetch response for ${url}`);
    return resp;
  });
  return Object.assign(ctx, { presignFn, fetchFn });
};

const ok = (body: ArrayBuffer): ResponseLike => ({
  ok: true,
  status: 200,
  statusText: 'OK',
  arrayBuffer: async () => body,
});

const readZip = async (blob: Blob): Promise<JSZip> => JSZip.loadAsync(await blob.arrayBuffer());
const zipFiles = (zip: JSZip): string[] =>
  Object.values(zip.files)
    .filter((f) => !f.dir)
    .map((f) => f.name)
    .sort();

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

describe('sanitizeDocumentKey', () => {
  it('replaces slashes with underscores', () => {
    expect(sanitizeDocumentKey('tenant/one/lending.pdf')).toBe('tenant_one_lending.pdf');
  });
  it('strips unsafe characters', () => {
    expect(sanitizeDocumentKey('a/b?c*d.pdf')).toBe('a_b_c_d.pdf');
  });
  it('returns "document" when empty', () => {
    expect(sanitizeDocumentKey('')).toBe('document');
    expect(sanitizeDocumentKey(undefined)).toBe('document');
  });
});

describe('parseS3Uri', () => {
  it('parses a valid URI', () => {
    expect(parseS3Uri('s3://bucket/key/a/b.json')).toEqual({ bucket: 'bucket', key: 'key/a/b.json' });
  });
  it('returns null for malformed URIs', () => {
    expect(parseS3Uri('https://host/key')).toBeNull();
    expect(parseS3Uri(undefined)).toBeNull();
  });
});

describe('constructBaselineUri', () => {
  it('swaps the output bucket for the baseline bucket', () => {
    expect(constructBaselineUri('s3://output-bkt/some/key.json', SETTINGS)).toBe('s3://baseline-bkt/some/key.json');
  });
  it('returns null when buckets are missing', () => {
    expect(constructBaselineUri('s3://output-bkt/k', {})).toBeNull();
  });
  it('returns null for malformed URI', () => {
    expect(constructBaselineUri('not-an-s3-uri', SETTINGS)).toBeNull();
  });
});

describe('uriToZipPath', () => {
  it('routes each bucket to its top-level folder', () => {
    expect(uriToZipPath('s3://output-bkt/a/b.json', SETTINGS)).toBe('output/a/b.json');
    expect(uriToZipPath('s3://baseline-bkt/a/b.json', SETTINGS)).toBe('baseline/a/b.json');
    expect(uriToZipPath('s3://input-bkt/a.pdf', SETTINGS)).toBe('input/a.pdf');
  });
  it('falls back to other/{bucket}/{key} for unknown buckets', () => {
    expect(uriToZipPath('s3://surprise/a/b.json', SETTINGS)).toBe('other/surprise/a/b.json');
  });
});

// ---------------------------------------------------------------------------
// exportDocument
// ---------------------------------------------------------------------------

describe('exportDocument', () => {
  it('predictions scope: only fetches section prediction JSONs and mirrors output/ layout', async () => {
    const mocks = makeMocks({
      's3://output-bkt/tenant/one/lending.pdf/sections/s1/result.json': ok(textBody('{"id":"s1"}')),
      's3://output-bkt/tenant/one/lending.pdf/sections/s2/result.json': ok(textBody('{"id":"s2"}')),
    });

    const result = await exportDocument(DOC, SETTINGS, {
      scope: 'predictions',
      credentials: { fake: true },
      presignFn: mocks.presignFn,
      fetchFn: mocks.fetchFn,
      now: () => new Date('2024-01-02T03:04:05Z'),
    });

    expect(result.errors).toEqual([]);
    expect(result.filename).toBe('tenant_one_lending.pdf_predictions.zip');
    expect(mocks.presigned).toEqual([
      's3://output-bkt/tenant/one/lending.pdf/sections/s1/result.json',
      's3://output-bkt/tenant/one/lending.pdf/sections/s2/result.json',
    ]);

    const zip = await readZip(result.blob);
    expect(zipFiles(zip)).toEqual(
      [
        'tenant_one_lending.pdf/document-attributes.json',
        'tenant_one_lending.pdf/manifest.json',
        'tenant_one_lending.pdf/output/tenant/one/lending.pdf/sections/s1/result.json',
        'tenant_one_lending.pdf/output/tenant/one/lending.pdf/sections/s2/result.json',
      ].sort(),
    );
    const manifest = JSON.parse((await zip.file('tenant_one_lending.pdf/manifest.json')!.async('string')) as string);
    expect(manifest.scope).toBe('predictions');
    expect(manifest.exportedAt).toBe('2024-01-02T03:04:05.000Z');
    expect(manifest.buckets).toEqual({ output: 'output-bkt', baseline: 'baseline-bkt', input: 'input-bkt' });
    expect(manifest.errors).toEqual([]);
  });

  it('baselines scope: swaps bucket, routes files under baseline/ prefix', async () => {
    const mocks = makeMocks({
      's3://baseline-bkt/tenant/one/lending.pdf/sections/s1/result.json': ok(textBody('{}')),
      's3://baseline-bkt/tenant/one/lending.pdf/sections/s2/result.json': ok(textBody('{}')),
    });

    const result = await exportDocument(DOC, SETTINGS, {
      scope: 'baselines',
      credentials: {},
      presignFn: mocks.presignFn,
      fetchFn: mocks.fetchFn,
    });

    expect(mocks.presigned.every((u) => u.startsWith('s3://baseline-bkt/'))).toBe(true);
    const zip = await readZip(result.blob);
    expect(zip.file('tenant_one_lending.pdf/baseline/tenant/one/lending.pdf/sections/s1/result.json')).toBeTruthy();
    expect(zip.file('tenant_one_lending.pdf/baseline/tenant/one/lending.pdf/sections/s2/result.json')).toBeTruthy();
    expect(zip.file('tenant_one_lending.pdf/output/tenant/one/lending.pdf/sections/s1/result.json')).toBeNull();
  });

  it('baselines scope: no baseline files when evaluationStatus disallows them', async () => {
    const mocks = makeMocks({});
    const doc = { ...DOC, evaluationStatus: 'NOT_EVALUATED' };
    const result = await exportDocument(doc, SETTINGS, {
      scope: 'baselines',
      credentials: {},
      presignFn: mocks.presignFn,
      fetchFn: mocks.fetchFn,
    });
    const zip = await readZip(result.blob);
    expect(zipFiles(zip).some((p) => p.includes('_baseline'))).toBe(false);
    expect(mocks.presignFn).not.toHaveBeenCalled();
  });

  it('all scope: includes top-level reports, sections, and page assets in bucket-mirrored layout', async () => {
    const mocks = makeMocks({
      's3://output-bkt/tenant/one/lending.pdf/summary/summary.md': ok(textBody('# Summary')),
      's3://output-bkt/tenant/one/lending.pdf/evaluation/report.json': ok(textBody('{"score":0.9}')),
      's3://output-bkt/tenant/one/lending.pdf/rule-validation/result.json': ok(textBody('{"ok":true}')),
      's3://output-bkt/tenant/one/lending.pdf/sections/s1/result.json': ok(textBody('{}')),
      's3://output-bkt/tenant/one/lending.pdf/sections/s2/result.json': ok(textBody('{}')),
      's3://baseline-bkt/tenant/one/lending.pdf/sections/s1/result.json': ok(textBody('{}')),
      's3://baseline-bkt/tenant/one/lending.pdf/sections/s2/result.json': ok(textBody('{}')),
      's3://output-bkt/tenant/one/lending.pdf/pages/1/result.json': ok(textBody('page1')),
      's3://output-bkt/tenant/one/lending.pdf/pages/1/textConfidence.json': ok(textBody('conf1')),
      's3://output-bkt/tenant/one/lending.pdf/pages/1/pageData.json': ok(textBody('{"lines":[]}')),
      's3://output-bkt/tenant/one/lending.pdf/pages/2/result.json': ok(textBody('page2')),
    });

    const result = await exportDocument(DOC, SETTINGS, {
      scope: 'all',
      includePageImages: false,
      includeSourceDocument: false,
      credentials: {},
      presignFn: mocks.presignFn,
      fetchFn: mocks.fetchFn,
    });

    const zip = await readZip(result.blob);
    const paths = zipFiles(zip);
    expect(paths).toContain('tenant_one_lending.pdf/document-attributes.json');
    expect(paths).toContain('tenant_one_lending.pdf/metering.json');
    expect(paths).toContain('tenant_one_lending.pdf/output/tenant/one/lending.pdf/summary/summary.md');
    expect(paths).toContain('tenant_one_lending.pdf/output/tenant/one/lending.pdf/evaluation/report.json');
    expect(paths).toContain('tenant_one_lending.pdf/output/tenant/one/lending.pdf/rule-validation/result.json');
    expect(paths).toContain('tenant_one_lending.pdf/output/tenant/one/lending.pdf/sections/s1/result.json');
    expect(paths).toContain('tenant_one_lending.pdf/baseline/tenant/one/lending.pdf/sections/s1/result.json');
    expect(paths).toContain('tenant_one_lending.pdf/output/tenant/one/lending.pdf/pages/1/result.json');
    expect(paths).toContain('tenant_one_lending.pdf/output/tenant/one/lending.pdf/pages/1/textConfidence.json');
    expect(paths).toContain('tenant_one_lending.pdf/output/tenant/one/lending.pdf/pages/1/pageData.json');
    expect(paths).toContain('tenant_one_lending.pdf/output/tenant/one/lending.pdf/pages/2/result.json');
    // page images and source doc should NOT be included
    expect(paths.some((p) => p.includes('/image'))).toBe(false);
    expect(paths.some((p) => p.startsWith('tenant_one_lending.pdf/input/'))).toBe(false);

    // Real summary content preserved as raw bytes (no base64 / text mangling)
    const summary = await zip.file('tenant_one_lending.pdf/output/tenant/one/lending.pdf/summary/summary.md')!.async('string');
    expect(summary).toBe('# Summary');
  });

  it('all scope with includePageImages: image bytes survive intact at their original key', async () => {
    const pngBytes = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
    const mocks = makeMocks({
      's3://output-bkt/tenant/one/lending.pdf/summary/summary.md': ok(textBody('s')),
      's3://output-bkt/tenant/one/lending.pdf/evaluation/report.json': ok(textBody('{}')),
      's3://output-bkt/tenant/one/lending.pdf/rule-validation/result.json': ok(textBody('{}')),
      's3://output-bkt/tenant/one/lending.pdf/sections/s1/result.json': ok(textBody('{}')),
      's3://output-bkt/tenant/one/lending.pdf/sections/s2/result.json': ok(textBody('{}')),
      's3://baseline-bkt/tenant/one/lending.pdf/sections/s1/result.json': ok(textBody('{}')),
      's3://baseline-bkt/tenant/one/lending.pdf/sections/s2/result.json': ok(textBody('{}')),
      's3://output-bkt/tenant/one/lending.pdf/pages/1/result.json': ok(textBody('t1')),
      's3://output-bkt/tenant/one/lending.pdf/pages/1/textConfidence.json': ok(textBody('c1')),
      's3://output-bkt/tenant/one/lending.pdf/pages/1/pageData.json': ok(textBody('{"lines":[]}')),
      's3://output-bkt/tenant/one/lending.pdf/pages/2/result.json': ok(textBody('t2')),
      's3://output-bkt/tenant/one/lending.pdf/pages/1/image.jpg': ok(bytesBody(pngBytes)),
    });

    const result = await exportDocument(DOC, SETTINGS, {
      scope: 'all',
      includePageImages: true,
      credentials: {},
      presignFn: mocks.presignFn,
      fetchFn: mocks.fetchFn,
    });

    const zip = await readZip(result.blob);
    const image = zip.file('tenant_one_lending.pdf/output/tenant/one/lending.pdf/pages/1/image.jpg');
    expect(image).toBeTruthy();
    const stored = new Uint8Array(await image!.async('uint8array'));
    expect(Array.from(stored)).toEqual(Array.from(pngBytes));
  });

  it('all scope with includeSourceDocument: fetches from the InputBucket and places under input/', async () => {
    const pdfBytes = new Uint8Array([0x25, 0x50, 0x44, 0x46]); // "%PDF"
    const mocks = makeMocks({
      's3://output-bkt/tenant/one/lending.pdf/summary/summary.md': ok(textBody('s')),
      's3://output-bkt/tenant/one/lending.pdf/evaluation/report.json': ok(textBody('{}')),
      's3://output-bkt/tenant/one/lending.pdf/rule-validation/result.json': ok(textBody('{}')),
      's3://output-bkt/tenant/one/lending.pdf/sections/s1/result.json': ok(textBody('{}')),
      's3://output-bkt/tenant/one/lending.pdf/sections/s2/result.json': ok(textBody('{}')),
      's3://baseline-bkt/tenant/one/lending.pdf/sections/s1/result.json': ok(textBody('{}')),
      's3://baseline-bkt/tenant/one/lending.pdf/sections/s2/result.json': ok(textBody('{}')),
      's3://output-bkt/tenant/one/lending.pdf/pages/1/result.json': ok(textBody('t1')),
      's3://output-bkt/tenant/one/lending.pdf/pages/1/textConfidence.json': ok(textBody('c1')),
      's3://output-bkt/tenant/one/lending.pdf/pages/1/pageData.json': ok(textBody('{"lines":[]}')),
      's3://output-bkt/tenant/one/lending.pdf/pages/2/result.json': ok(textBody('t2')),
      's3://input-bkt/tenant/one/lending.pdf': ok(bytesBody(pdfBytes)),
    });

    const result = await exportDocument(DOC, SETTINGS, {
      scope: 'all',
      includeSourceDocument: true,
      credentials: {},
      presignFn: mocks.presignFn,
      fetchFn: mocks.fetchFn,
    });

    const zip = await readZip(result.blob);
    const source = zip.file('tenant_one_lending.pdf/input/tenant/one/lending.pdf');
    expect(source).toBeTruthy();
    const stored = new Uint8Array(await source!.async('uint8array'));
    expect(Array.from(stored)).toEqual(Array.from(pdfBytes));

    const manifest = JSON.parse(await zip.file('tenant_one_lending.pdf/manifest.json')!.async('string'));
    expect(manifest.includeSourceDocument).toBe(true);
    expect(manifest.sourceFileUri).toBe('s3://input-bkt/tenant/one/lending.pdf');
  });

  it('records fetch failures (non-2xx) in manifest errors without aborting', async () => {
    const mocks = makeMocks({
      's3://output-bkt/tenant/one/lending.pdf/sections/s1/result.json': ok(textBody('{}')),
      's3://output-bkt/tenant/one/lending.pdf/sections/s2/result.json': {
        ok: false,
        status: 403,
        statusText: 'Forbidden',
        arrayBuffer: async () => new ArrayBuffer(0),
      },
    });

    const result = await exportDocument(DOC, SETTINGS, {
      scope: 'predictions',
      credentials: {},
      presignFn: mocks.presignFn,
      fetchFn: mocks.fetchFn,
    });

    expect(result.errors).toHaveLength(1);
    expect(result.errors[0].path).toContain('sections/s2/result.json');
    expect(result.errors[0].message).toContain('403');
    const zip = await readZip(result.blob);
    expect(zip.file('tenant_one_lending.pdf/output/tenant/one/lending.pdf/sections/s1/result.json')).toBeTruthy();
    expect(zip.file('tenant_one_lending.pdf/output/tenant/one/lending.pdf/sections/s2/result.json')).toBeNull();
    const manifest = JSON.parse(await zip.file('tenant_one_lending.pdf/manifest.json')!.async('string'));
    expect(manifest.errors).toHaveLength(1);
  });

  it('aborts via signal with an AbortError', async () => {
    const mocks = makeMocks({});
    const controller = new AbortController();
    controller.abort();
    await expect(
      exportDocument(DOC, SETTINGS, {
        scope: 'predictions',
        credentials: {},
        presignFn: mocks.presignFn,
        fetchFn: mocks.fetchFn,
        signal: controller.signal,
      }),
    ).rejects.toMatchObject({ name: 'AbortError' });
  });

  it('emits progress events covering every fetch plus the zip-generation step', async () => {
    const mocks = makeMocks({
      's3://output-bkt/tenant/one/lending.pdf/sections/s1/result.json': ok(textBody('{}')),
      's3://output-bkt/tenant/one/lending.pdf/sections/s2/result.json': ok(textBody('{}')),
    });
    const progress: number[] = [];
    await exportDocument(DOC, SETTINGS, {
      scope: 'predictions',
      credentials: {},
      presignFn: mocks.presignFn,
      fetchFn: mocks.fetchFn,
      onProgress: (p) => progress.push(p.completed),
    });
    expect(progress.at(-1)).toBeGreaterThanOrEqual(3);
  });

  it('throws when neither credentials nor presignFn are provided', async () => {
    await expect(exportDocument(DOC, SETTINGS, { scope: 'predictions' })).rejects.toThrow(/credentials are required/);
  });
});

// ---------------------------------------------------------------------------
// exportDocuments (bulk)
// ---------------------------------------------------------------------------

/** Second document, distinct key/sections, no baseline available. */
const DOC_B: ExportableDocument = {
  objectKey: 'tenant/two/statement.pdf',
  objectStatus: 'COMPLETED',
  evaluationStatus: 'NOT_EVALUATED',
  sections: [{ Id: 's1', Class: 'Bank', OutputJSONUri: 's3://output-bkt/tenant/two/statement.pdf/sections/s1/result.json' }],
  pages: [{ Id: 1, TextUri: 's3://output-bkt/tenant/two/statement.pdf/pages/1/result.json' }],
};

describe('exportDocuments', () => {
  it('packs multiple documents into one archive, each under its own folder', async () => {
    const mocks = makeMocks({
      's3://output-bkt/tenant/one/lending.pdf/sections/s1/result.json': ok(textBody('{"a":1}')),
      's3://output-bkt/tenant/one/lending.pdf/sections/s2/result.json': ok(textBody('{"a":2}')),
      's3://output-bkt/tenant/two/statement.pdf/sections/s1/result.json': ok(textBody('{"b":1}')),
    });

    const result = await exportDocuments([DOC, DOC_B], SETTINGS, {
      scope: 'predictions',
      credentials: {},
      presignFn: mocks.presignFn,
      fetchFn: mocks.fetchFn,
      now: () => new Date('2024-01-02T03:04:05Z'),
    });

    expect(result.filename).toBe('documents_2docs_predictions.zip');
    expect(result.errors).toEqual([]);

    const zip = await readZip(result.blob);
    expect(zipFiles(zip)).toEqual(
      [
        'manifest.json',
        'tenant_one_lending.pdf/document-attributes.json',
        'tenant_one_lending.pdf/manifest.json',
        'tenant_one_lending.pdf/output/tenant/one/lending.pdf/sections/s1/result.json',
        'tenant_one_lending.pdf/output/tenant/one/lending.pdf/sections/s2/result.json',
        'tenant_two_statement.pdf/document-attributes.json',
        'tenant_two_statement.pdf/manifest.json',
        'tenant_two_statement.pdf/output/tenant/two/statement.pdf/sections/s1/result.json',
      ].sort(),
    );
  });

  it('writes a root manifest indexing every document, plus intact per-document manifests', async () => {
    const mocks = makeMocks({
      's3://output-bkt/tenant/one/lending.pdf/sections/s1/result.json': ok(textBody('{}')),
      's3://output-bkt/tenant/one/lending.pdf/sections/s2/result.json': ok(textBody('{}')),
      's3://output-bkt/tenant/two/statement.pdf/sections/s1/result.json': ok(textBody('{}')),
    });

    const result = await exportDocuments([DOC, DOC_B], SETTINGS, {
      scope: 'predictions',
      credentials: {},
      presignFn: mocks.presignFn,
      fetchFn: mocks.fetchFn,
    });

    const zip = await readZip(result.blob);
    const root = JSON.parse(await zip.file('manifest.json')!.async('string'));
    expect(root.documentCount).toBe(2);
    expect(root.documents.map((d: { objectKey: string }) => d.objectKey)).toEqual(['tenant/one/lending.pdf', 'tenant/two/statement.pdf']);
    expect(root.documents.map((d: { folder: string }) => d.folder)).toEqual(['tenant_one_lending.pdf', 'tenant_two_statement.pdf']);
    expect(root.documents[0].fileCount).toBe(3); // attributes + 2 sections
    expect(root.errors).toEqual([]);

    // Per-document manifests stay scoped to their own document
    const docB = JSON.parse(await zip.file('tenant_two_statement.pdf/manifest.json')!.async('string'));
    expect(docB.document.objectKey).toBe('tenant/two/statement.pdf');
    expect(docB.files.every((f: { path: string }) => !f.path.includes('lending'))).toBe(true);
  });

  it('gives colliding sanitized keys distinct folders instead of overwriting', async () => {
    // 'a/b.pdf' and 'a_b.pdf' both sanitize to 'a_b.pdf'
    const docA: ExportableDocument = {
      objectKey: 'a/b.pdf',
      sections: [{ Id: 's1', OutputJSONUri: 's3://output-bkt/a/b.pdf/sections/s1/result.json' }],
    };
    const docB: ExportableDocument = {
      objectKey: 'a_b.pdf',
      sections: [{ Id: 's1', OutputJSONUri: 's3://output-bkt/a_b.pdf/sections/s1/result.json' }],
    };
    const mocks = makeMocks({
      's3://output-bkt/a/b.pdf/sections/s1/result.json': ok(textBody('first')),
      's3://output-bkt/a_b.pdf/sections/s1/result.json': ok(textBody('second')),
    });

    const result = await exportDocuments([docA, docB], SETTINGS, {
      scope: 'predictions',
      credentials: {},
      presignFn: mocks.presignFn,
      fetchFn: mocks.fetchFn,
    });

    const zip = await readZip(result.blob);
    expect(await zip.file('a_b.pdf/output/a/b.pdf/sections/s1/result.json')!.async('string')).toBe('first');
    expect(await zip.file('a_b.pdf__2/output/a_b.pdf/sections/s1/result.json')!.async('string')).toBe('second');
    const root = JSON.parse(await zip.file('manifest.json')!.async('string'));
    expect(root.documents.map((d: { folder: string }) => d.folder)).toEqual(['a_b.pdf', 'a_b.pdf__2']);
  });

  it('attributes soft errors to their document in the returned errors and root manifest', async () => {
    const mocks = makeMocks({
      's3://output-bkt/tenant/one/lending.pdf/sections/s1/result.json': ok(textBody('{}')),
      's3://output-bkt/tenant/one/lending.pdf/sections/s2/result.json': ok(textBody('{}')),
      's3://output-bkt/tenant/two/statement.pdf/sections/s1/result.json': {
        ok: false,
        status: 403,
        statusText: 'Forbidden',
        arrayBuffer: async () => new ArrayBuffer(0),
      },
    });

    const result = await exportDocuments([DOC, DOC_B], SETTINGS, {
      scope: 'predictions',
      credentials: {},
      presignFn: mocks.presignFn,
      fetchFn: mocks.fetchFn,
    });

    expect(result.errors).toHaveLength(1);
    expect(result.errors[0].document).toBe('tenant/two/statement.pdf');
    expect(result.errors[0].message).toContain('403');

    const zip = await readZip(result.blob);
    const root = JSON.parse(await zip.file('manifest.json')!.async('string'));
    expect(root.documents.find((d: { folder: string }) => d.folder === 'tenant_one_lending.pdf').errorCount).toBe(0);
    expect(root.documents.find((d: { folder: string }) => d.folder === 'tenant_two_statement.pdf').errorCount).toBe(1);
    // The healthy document is still fully exported
    expect(zip.file('tenant_one_lending.pdf/output/tenant/one/lending.pdf/sections/s1/result.json')).toBeTruthy();
  });

  it('skips baselines for documents without one while still exporting those that have one', async () => {
    const mocks = makeMocks({
      's3://baseline-bkt/tenant/one/lending.pdf/sections/s1/result.json': ok(textBody('{}')),
      's3://baseline-bkt/tenant/one/lending.pdf/sections/s2/result.json': ok(textBody('{}')),
    });

    const result = await exportDocuments([DOC, DOC_B], SETTINGS, {
      scope: 'baselines',
      credentials: {},
      presignFn: mocks.presignFn,
      fetchFn: mocks.fetchFn,
    });

    expect(result.errors).toEqual([]);
    expect(mocks.presigned.every((u) => u.startsWith('s3://baseline-bkt/tenant/one/'))).toBe(true);
    const zip = await readZip(result.blob);
    // DOC_B (evaluationStatus NOT_EVALUATED) contributes attributes + manifest only
    expect(zipFiles(zip).filter((p) => p.startsWith('tenant_two_statement.pdf/'))).toEqual([
      'tenant_two_statement.pdf/document-attributes.json',
      'tenant_two_statement.pdf/manifest.json',
    ]);
  });

  it('reports aggregate file and document progress across the whole selection', async () => {
    const mocks = makeMocks({
      's3://output-bkt/tenant/one/lending.pdf/sections/s1/result.json': ok(textBody('{}')),
      's3://output-bkt/tenant/one/lending.pdf/sections/s2/result.json': ok(textBody('{}')),
      's3://output-bkt/tenant/two/statement.pdf/sections/s1/result.json': ok(textBody('{}')),
    });
    const events: ExportProgress[] = [];

    await exportDocuments([DOC, DOC_B], SETTINGS, {
      scope: 'predictions',
      credentials: {},
      presignFn: mocks.presignFn,
      fetchFn: mocks.fetchFn,
      onProgress: (p) => events.push(p),
    });

    const last = events.at(-1)!;
    expect(last.total).toBe(4); // 3 fetches + zip generation
    expect(last.completed).toBe(4);
    expect(last.documentsTotal).toBe(2);
    expect(last.documentsCompleted).toBe(2);
    expect(events.every((e) => (e.documentsCompleted ?? 0) <= 2)).toBe(true);
  });

  it('honours the concurrency limit while fetching every file', async () => {
    const uris = Array.from({ length: 8 }, (_, i) => `s3://output-bkt/doc/sections/s${i}/result.json`);
    const docs: ExportableDocument[] = uris.map((uri, i) => ({
      objectKey: `doc-${i}.pdf`,
      sections: [{ Id: 's1', OutputJSONUri: uri }],
    }));

    let inFlight = 0;
    let peakInFlight = 0;
    const presignFn = vi.fn(async (uri: string) => `https://presigned.example/?uri=${encodeURIComponent(uri)}`);
    const fetchFn = vi.fn(async () => {
      inFlight += 1;
      peakInFlight = Math.max(peakInFlight, inFlight);
      await new Promise((resolve) => setTimeout(resolve, 5));
      inFlight -= 1;
      return ok(textBody('{}'));
    });

    const result = await exportDocuments(docs, SETTINGS, {
      scope: 'predictions',
      credentials: {},
      presignFn,
      fetchFn,
      concurrency: 3,
    });

    expect(fetchFn).toHaveBeenCalledTimes(8);
    expect(peakInFlight).toBeLessThanOrEqual(3);
    expect(peakInFlight).toBeGreaterThan(1);
    const zip = await readZip(result.blob);
    expect(zipFiles(zip).filter((p) => p.includes('/output/doc/sections/'))).toHaveLength(8);
  });

  it('aborts a bulk export via signal', async () => {
    const mocks = makeMocks({});
    const controller = new AbortController();
    controller.abort();
    await expect(
      exportDocuments([DOC, DOC_B], SETTINGS, {
        scope: 'predictions',
        credentials: {},
        presignFn: mocks.presignFn,
        fetchFn: mocks.fetchFn,
        signal: controller.signal,
      }),
    ).rejects.toMatchObject({ name: 'AbortError' });
    expect(mocks.presignFn).not.toHaveBeenCalled();
  });

  it('throws when given an empty selection', async () => {
    await expect(exportDocuments([], SETTINGS, { scope: 'predictions', credentials: {} })).rejects.toThrow(/no documents/);
  });

  it('records preflight errors in the result, the root manifest, and the owning document manifest', async () => {
    const mocks = makeMocks({
      's3://output-bkt/tenant/one/lending.pdf/sections/s1/result.json': ok(textBody('{}')),
      's3://output-bkt/tenant/one/lending.pdf/sections/s2/result.json': ok(textBody('{}')),
      's3://output-bkt/tenant/two/statement.pdf/sections/s1/result.json': ok(textBody('{}')),
    });
    // Mirrors a document whose detail fetch failed: only attributes are exportable.
    const preflightErrors = [
      {
        path: '(document details)',
        document: 'tenant/two/statement.pdf',
        message: 'Could not fetch document details',
      },
    ];

    const result = await exportDocuments([DOC, DOC_B], SETTINGS, {
      scope: 'predictions',
      credentials: {},
      presignFn: mocks.presignFn,
      fetchFn: mocks.fetchFn,
      preflightErrors,
    });

    expect(result.errors).toHaveLength(1);
    expect(result.errors[0].document).toBe('tenant/two/statement.pdf');

    const zip = await readZip(result.blob);
    const root = JSON.parse(await zip.file('manifest.json')!.async('string'));
    expect(root.errors).toHaveLength(1);
    expect(root.documents.find((d: { folder: string }) => d.folder === 'tenant_two_statement.pdf').errorCount).toBe(1);
    expect(root.documents.find((d: { folder: string }) => d.folder === 'tenant_one_lending.pdf').errorCount).toBe(0);

    // The failure is attributed inside the affected document's own manifest…
    const docB = JSON.parse(await zip.file('tenant_two_statement.pdf/manifest.json')!.async('string'));
    expect(docB.errors).toHaveLength(1);
    expect(docB.errors[0].message).toMatch(/Could not fetch document details/);
    // …and nowhere else.
    const docA = JSON.parse(await zip.file('tenant_one_lending.pdf/manifest.json')!.async('string'));
    expect(docA.errors).toEqual([]);
  });

  it('surfaces preflight errors through progress events so the dialog can show them', async () => {
    const mocks = makeMocks({ 's3://output-bkt/tenant/two/statement.pdf/sections/s1/result.json': ok(textBody('{}')) });
    const events: ExportProgress[] = [];

    await exportDocuments([DOC_B], SETTINGS, {
      scope: 'predictions',
      credentials: {},
      presignFn: mocks.presignFn,
      fetchFn: mocks.fetchFn,
      preflightErrors: [{ path: '(document details)', document: 'other.pdf', message: 'boom' }],
      onProgress: (p) => events.push(p),
    });

    expect(events.every((e) => e.errors.some((err) => err.message === 'boom'))).toBe(true);
  });
});
