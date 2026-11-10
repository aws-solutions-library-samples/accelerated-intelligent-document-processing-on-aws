// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { generateClient } from './client-shim';
import { uploadMultiDocDiscoveryZip } from '../graphql/generated';

const client = generateClient();

export const DISCOVERY_ZIP_CONTENT_TYPE = 'application/zip';

interface DiscoveryZipUploadResponse {
  data?: { uploadMultiDocDiscoveryZip?: { presignedUrl?: string; objectKey?: string } };
}

/**
 * Uploads a zip of documents for multi-document discovery and returns its object key.
 *
 * `uploadMultiDocDiscoveryZip` answers with a presigned S3 PUT URL signed for
 * `Content-Type: application/zip`, not the presigned POST form the other upload
 * mutations return. Every caller uploads the zip through this function.
 */
export const uploadDiscoveryZip = async (zipFile: File, configVersion: string): Promise<string> => {
  const response = (await client.graphql({
    query: uploadMultiDocDiscoveryZip,
    variables: { fileName: zipFile.name, fileSize: zipFile.size, configVersion },
  })) as DiscoveryZipUploadResponse;
  const upload = response?.data?.uploadMultiDocDiscoveryZip;
  if (!upload?.presignedUrl || !upload?.objectKey) {
    throw new Error('Failed to get an upload URL');
  }
  const result = await fetch(upload.presignedUrl, {
    method: 'PUT',
    body: zipFile,
    headers: { 'Content-Type': DISCOVERY_ZIP_CONTENT_TYPE },
  });
  if (!result.ok) {
    throw new Error(`Upload failed: ${result.statusText}`);
  }
  return upload.objectKey;
};
