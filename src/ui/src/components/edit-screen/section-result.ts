// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Reading a section result (or its draft) for the edit page, through a server-issued
// presigned URL, so the bytes come straight from S3.
import { generateClient } from '../../api/client-shim';
import { getFilePresignedUrl } from '../../graphql/generated';
import type { LoadedSection } from './types';

const client = generateClient();

/** Read a section result (or its draft) through a server-issued presigned URL. */
export const fetchSectionResult = async (s3Uri: string): Promise<LoadedSection> => {
  const response = await client.graphql({ query: getFilePresignedUrl, variables: { s3Uri } });
  const presignedUrl = response.data?.getFilePresignedUrl?.presignedUrl;
  if (!presignedUrl) throw new Error('No presigned URL returned by server');
  const s3Response = await fetch(presignedUrl);
  if (!s3Response.ok) throw new Error(`S3 fetch failed: ${s3Response.status}`);
  const text = await s3Response.text();
  return { data: JSON.parse(text) as Record<string, unknown>, raw: text };
};
