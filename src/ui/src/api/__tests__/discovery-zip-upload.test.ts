// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { DISCOVERY_ZIP_CONTENT_TYPE, uploadDiscoveryZip } from '../discovery-zip-upload';

const { graphqlMock } = vi.hoisted(() => ({ graphqlMock: vi.fn() }));
vi.mock('../client-shim', () => ({
  generateClient: () => ({ graphql: graphqlMock }),
}));

const PRESIGNED_PUT_URL = 'https://discovery-bucket.s3.us-east-1.amazonaws.com/multi-doc-discovery/docs.zip?X-Amz-Signature=abc123';
const OBJECT_KEY = 'multi-doc-discovery/docs.zip';

const zip = () => new File([new Uint8Array([80, 75, 3, 4])], 'docs.zip', { type: DISCOVERY_ZIP_CONTENT_TYPE });

describe('uploadDiscoveryZip', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    graphqlMock.mockReset();
    fetchMock = vi.fn().mockResolvedValue({ ok: true, statusText: 'OK' });
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('PUTs the zip to the presigned URL as application/zip and returns its object key', async () => {
    graphqlMock.mockResolvedValue({ data: { uploadMultiDocDiscoveryZip: { presignedUrl: PRESIGNED_PUT_URL, objectKey: OBJECT_KEY } } });
    const file = zip();

    await expect(uploadDiscoveryZip(file, 'my-profile')).resolves.toBe(OBJECT_KEY);

    expect(graphqlMock).toHaveBeenCalledTimes(1);
    const [{ query, variables }] = graphqlMock.mock.calls[0];
    expect(query).toContain('mutation UploadMultiDocDiscoveryZip(');
    expect(variables).toEqual({ fileName: 'docs.zip', fileSize: file.size, configVersion: 'my-profile' });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith(PRESIGNED_PUT_URL, {
      method: 'PUT',
      body: file,
      headers: { 'Content-Type': 'application/zip' },
    });
  });

  it('refuses a response without a URL or an object key, before uploading anything', async () => {
    graphqlMock.mockResolvedValueOnce({ data: { uploadMultiDocDiscoveryZip: { presignedUrl: PRESIGNED_PUT_URL } } });
    await expect(uploadDiscoveryZip(zip(), 'p')).rejects.toThrow('Failed to get an upload URL');
    graphqlMock.mockResolvedValueOnce({ data: { uploadMultiDocDiscoveryZip: { objectKey: OBJECT_KEY } } });
    await expect(uploadDiscoveryZip(zip(), 'p')).rejects.toThrow('Failed to get an upload URL');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('reports an upload S3 refuses', async () => {
    graphqlMock.mockResolvedValue({ data: { uploadMultiDocDiscoveryZip: { presignedUrl: PRESIGNED_PUT_URL, objectKey: OBJECT_KEY } } });
    fetchMock.mockResolvedValue({ ok: false, statusText: 'Forbidden' });
    await expect(uploadDiscoveryZip(zip(), 'p')).rejects.toThrow('Upload failed: Forbidden');
  });
});
