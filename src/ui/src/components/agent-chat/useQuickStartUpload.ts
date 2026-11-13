// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { useCallback, useEffect, useRef, useState } from 'react';
import JSZip from 'jszip';
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from '../../api/client-shim';
import { uploadDiscoveryZip } from '../../api/discovery-zip-upload';
import { startMultiDocDiscovery, listDiscoveryJobs, uploadDiscoveryDocument } from '../../graphql/generated';
import useSettingsContext from '../../contexts/settings';

const logger = new ConsoleLogger('useQuickStartUpload');
const client = generateClient();

const POLL_INTERVAL_MS = 5000;

export interface QuickStartUploadStatus {
  jobId: string | null;
  status: string;
  currentStep?: string;
  totalDocuments?: number;
  clustersFound?: number;
  discoveredClasses?: string;
  discoveredClassName?: string;
  reflectionReport?: string;
  errorMessage?: string;
}

interface DiscoveryJobRecord extends QuickStartUploadStatus {
  jobId: string;
  documentKey?: string;
}

export interface QuickStartUploadResult {
  jobId: string;
  classNames: string[];
  totalDocuments: number;
  clustersFound: number;
  configVersion: string;
}

interface UseQuickStartUploadArgs {
  onComplete?: (result: QuickStartUploadResult) => void;
  onError?: (message: string) => void;
}

const TERMINAL = ['COMPLETED', 'FAILED'];

const toZip = async (files: File[]): Promise<File> => {
  if (files.length === 1 && files[0].name.toLowerCase().endsWith('.zip')) {
    return files[0];
  }
  const zip = new JSZip();
  files.forEach((file) => zip.file(file.name, file));
  const blob = await zip.generateAsync({ type: 'blob' });
  return new File([blob], `quick-start-${Date.now()}.zip`, { type: 'application/zip' });
};

// A single non-zip document goes through the single-document discovery path
// (uploadDiscoveryDocument) so it lands on the Single Document tab; anything
// else (multiple files, or a pre-zipped bundle) uses multi-doc discovery.
const isSingleDocUpload = (files: File[]): boolean => files.length === 1 && !files[0].name.toLowerCase().endsWith('.zip');

const postFileToPresignedUrl = async (file: File, presignedUrl: string): Promise<void> => {
  const presignedPost = JSON.parse(presignedUrl) as { url: string; fields: Record<string, string> };
  const formData = new FormData();
  Object.entries(presignedPost.fields).forEach(([key, value]) => formData.append(key, value));
  formData.append('file', file);
  const postResult = await fetch(presignedPost.url, { method: 'POST', body: formData });
  if (!postResult.ok) {
    throw new Error(`Upload failed: HTTP ${postResult.status}${postResult.statusText ? ` ${postResult.statusText}` : ''}`);
  }
};

const parseClassNames = (discoveredClasses?: string): string[] => {
  if (!discoveredClasses) return [];
  try {
    const parsed = JSON.parse(discoveredClasses);
    if (Array.isArray(parsed)) {
      return parsed.map((c) => c.classification || c.class_name || c.$id).filter(Boolean);
    }
  } catch {
    logger.warn('Could not parse discoveredClasses');
  }
  return [];
};

const useQuickStartUpload = ({ onComplete, onError }: UseQuickStartUploadArgs = {}) => {
  const { settings } = useSettingsContext();
  const [uploading, setUploading] = useState(false);
  const [status, setStatus] = useState<QuickStartUploadStatus | null>(null);
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const configVersionRef = useRef<string>('');

  const stopPolling = useCallback(() => {
    if (pollTimerRef.current !== null) {
      clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  useEffect(() => () => stopPolling(), [stopPolling]);

  // AppSync subscriptions were removed with the REST migration, so status is
  // tracked by polling listDiscoveryJobs and matching the job, mirroring the
  // discovery panels. Multi-doc jobs return a jobId up front; single-doc
  // uploads don't, so we match on the uploaded documentKey instead.
  const startPolling = useCallback(
    (match: (job: DiscoveryJobRecord) => boolean) => {
      stopPolling();
      const poll = async () => {
        try {
          const resp = await client.graphql({ query: listDiscoveryJobs });
          const jobs =
            (resp as { data?: { listDiscoveryJobs?: { DiscoveryJobs?: DiscoveryJobRecord[] } } })?.data?.listDiscoveryJobs?.DiscoveryJobs ||
            [];
          const update = jobs.find(match);
          if (!update) return;
          setStatus((prev) => ({ ...(prev || { jobId: update.jobId, status: 'QUEUED' }), ...update }));
          if (TERMINAL.includes(update.status)) {
            stopPolling();
            if (update.status === 'COMPLETED') {
              // Multi-doc jobs report an array of discovered classes;
              // single-doc jobs report one discoveredClassName.
              const classNames = update.discoveredClassName ? [update.discoveredClassName] : parseClassNames(update.discoveredClasses);
              onComplete?.({
                jobId: update.jobId,
                classNames,
                totalDocuments: update.totalDocuments || (update.discoveredClassName ? 1 : 0),
                clustersFound: update.clustersFound || 0,
                configVersion: configVersionRef.current,
              });
            } else {
              onError?.(update.errorMessage || 'Document discovery failed');
            }
          }
        } catch (err) {
          logger.error('Discovery status poll failed', err);
        }
      };
      pollTimerRef.current = setInterval(poll, POLL_INTERVAL_MS);
      void poll();
    },
    [onComplete, onError, stopPolling],
  );

  const startSingleDocUpload = useCallback(
    async (file: File, configVersion: string) => {
      const bucket = settings.DiscoveryBucket as string | undefined;
      if (!bucket) {
        throw new Error('Discovery bucket is not configured');
      }
      const uploadResponse = await client.graphql({
        query: uploadDiscoveryDocument,
        variables: {
          fileName: file.name,
          contentType: file.type,
          bucket,
          version: configVersion,
          discoveryType: 'classes',
        },
      });
      const uploadData = (
        uploadResponse as {
          data?: { uploadDiscoveryDocument?: { presignedUrl?: string; objectKey?: string; usePostMethod?: string } };
        }
      )?.data?.uploadDiscoveryDocument;
      if (!uploadData?.presignedUrl || !uploadData?.objectKey) {
        throw new Error('Failed to get an upload URL');
      }
      if (uploadData.usePostMethod && uploadData.usePostMethod.toLowerCase() !== 'true') {
        throw new Error('Server returned an unsupported upload method');
      }

      await postFileToPresignedUrl(file, uploadData.presignedUrl);

      // uploadDiscoveryDocument creates the job server-side but returns no
      // jobId, so track it by the uploaded object key.
      const objectKey = uploadData.objectKey;
      setStatus({ jobId: null, status: 'QUEUED', currentStep: 'Analyzing document' });
      startPolling((job) => job.documentKey === objectKey);
    },
    [settings.DiscoveryBucket, startPolling],
  );

  const startMultiDocUpload = useCallback(
    async (files: File[], configVersion: string) => {
      const zipFile = await toZip(files);
      const objectKey = await uploadDiscoveryZip(zipFile, configVersion);

      const startResponse = await client.graphql({
        query: startMultiDocDiscovery,
        variables: {
          configVersion,
          zipFileName: zipFile.name,
          zipFileSize: zipFile.size,
          s3Prefix: objectKey,
        },
      });
      const job = (startResponse as { data?: { startMultiDocDiscovery?: QuickStartUploadStatus } })?.data?.startMultiDocDiscovery;
      if (!job?.jobId) {
        throw new Error('Failed to start document discovery');
      }
      setStatus({ ...job });
      const jobId = job.jobId;
      startPolling((j) => j.jobId === jobId);
    },
    [startPolling],
  );

  const startUpload = useCallback(
    async (files: File[], configVersion: string) => {
      if (!files.length) return;
      configVersionRef.current = configVersion;
      setUploading(true);
      setStatus({ jobId: null, status: 'PREPARING', currentStep: 'Packaging documents' });
      try {
        if (isSingleDocUpload(files)) {
          await startSingleDocUpload(files[0], configVersion);
        } else {
          await startMultiDocUpload(files, configVersion);
        }
      } catch (err) {
        const message = err instanceof Error ? err.message : 'Upload failed';
        logger.error('startUpload failed', err);
        setStatus(null);
        onError?.(message);
      } finally {
        setUploading(false);
      }
    },
    [startSingleDocUpload, startMultiDocUpload, onError],
  );

  const reset = useCallback(() => {
    stopPolling();
    setStatus(null);
  }, [stopPolling]);

  return { startUpload, uploading, status, reset };
};

export default useQuickStartUpload;
