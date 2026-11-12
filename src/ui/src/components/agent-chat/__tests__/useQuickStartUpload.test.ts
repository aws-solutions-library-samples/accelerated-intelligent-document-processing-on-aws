// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import useQuickStartUpload from '../useQuickStartUpload';

const { graphqlMock } = vi.hoisted(() => ({ graphqlMock: vi.fn() }));
vi.mock('../../../api/client-shim', () => ({
  generateClient: () => ({ graphql: graphqlMock }),
}));
vi.mock('../../../contexts/settings', () => ({
  default: () => ({ settings: { DiscoveryBucket: 'discovery-bucket' } }),
}));

const ZIP_PUT_URL = 'https://discovery-bucket.s3.us-east-1.amazonaws.com/multi-doc-discovery/quick-start.zip?X-Amz-Signature=abc123';
const ZIP_KEY = 'multi-doc-discovery/quick-start.zip';
const SINGLE_POST = { url: 'https://discovery-bucket.s3.us-east-1.amazonaws.com/', fields: { key: 'discovery/paystub.pdf', policy: 'p' } };
const SINGLE_KEY = 'discovery/paystub.pdf';

const operation = (args: { query: string }) => /(?:mutation|query)\s+(\w+)/.exec(args.query)?.[1];

const answer = (args: { query: string }) => {
  switch (operation(args)) {
    case 'UploadMultiDocDiscoveryZip':
      return { data: { uploadMultiDocDiscoveryZip: { presignedUrl: ZIP_PUT_URL, objectKey: ZIP_KEY } } };
    case 'StartMultiDocDiscovery':
      return { data: { startMultiDocDiscovery: { jobId: 'job-1', status: 'QUEUED' } } };
    case 'UploadDiscoveryDocument':
      return {
        data: { uploadDiscoveryDocument: { presignedUrl: JSON.stringify(SINGLE_POST), objectKey: SINGLE_KEY, usePostMethod: 'true' } },
      };
    case 'ListDiscoveryJobs':
      return { data: { listDiscoveryJobs: { DiscoveryJobs: [] } } };
    default:
      throw new Error(`unexpected operation ${operation(args)}`);
  }
};

const pdf = (name: string) => new File([new Uint8Array([37, 80, 68, 70])], name, { type: 'application/pdf' });
const zip = (name: string) => new File([new Uint8Array([80, 75, 3, 4])], name, { type: 'application/zip' });

const calledOperations = () => graphqlMock.mock.calls.map(([args]) => operation(args as { query: string }));
const variablesOf = (name: string) => graphqlMock.mock.calls.find(([args]) => operation(args as { query: string }) === name)?.[0].variables;

describe('useQuickStartUpload', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    graphqlMock.mockReset();
    graphqlMock.mockImplementation(async (args: { query: string }) => answer(args));
    fetchMock = vi.fn().mockResolvedValue({ ok: true, statusText: 'OK' });
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const upload = async (files: File[]) => {
    const onError = vi.fn();
    const hook = renderHook(() => useQuickStartUpload({ onError }));
    await act(async () => {
      await hook.result.current.startUpload(files, 'quickstart');
    });
    hook.unmount();
    return onError;
  };

  it('PUTs several attached documents as one zip and starts discovery on its key', async () => {
    const onError = await upload([pdf('paystub-1.pdf'), pdf('paystub-2.pdf')]);

    expect(onError).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(ZIP_PUT_URL);
    expect(init).toMatchObject({ method: 'PUT', headers: { 'Content-Type': 'application/zip' } });
    expect(init.body).toBeInstanceOf(File);
    expect(variablesOf('StartMultiDocDiscovery')).toMatchObject({ configVersion: 'quickstart', s3Prefix: ZIP_KEY });
  });

  it('PUTs an attached zip as it is', async () => {
    const attached = zip('examples.zip');
    const onError = await upload([attached]);

    expect(onError).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledWith(ZIP_PUT_URL, expect.objectContaining({ method: 'PUT', body: attached }));
    expect(calledOperations()).toContain('StartMultiDocDiscovery');
  });

  it('posts one attached document to its presigned form, as single-document discovery', async () => {
    const onError = await upload([pdf('paystub.pdf')]);

    expect(onError).not.toHaveBeenCalled();
    expect(calledOperations()).not.toContain('UploadMultiDocDiscoveryZip');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(SINGLE_POST.url);
    expect(init.method).toBe('POST');
    expect(init.body).toBeInstanceOf(FormData);
    expect((init.body as FormData).get('key')).toBe(SINGLE_KEY);
  });

  it('names the status when a single-document upload is refused with no status text', async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 403, statusText: '' });
    const onError = await upload([pdf('paystub.pdf')]);
    expect(onError).toHaveBeenCalledWith('Upload failed: HTTP 403');
  });
});
