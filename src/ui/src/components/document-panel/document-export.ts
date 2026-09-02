// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import JSZip from 'jszip';
import { ConsoleLogger } from 'aws-amplify/utils';
import generateS3PresignedUrl from '../common/generate-s3-presigned-url';
import { generateClient } from '../../api/client-shim';
import { getFilePresignedUrl } from '../../graphql/generated';

const logger = new ConsoleLogger('document-export');

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type ExportScope = 'all' | 'predictions' | 'baselines';

export interface ExportProgress {
  /** Completed step count (fetches + the final zip-generation step). */
  completed: number;
  /** Total expected step count. */
  total: number;
  /** Human-readable description of the currently-processing file. */
  currentFile: string;
  /** Soft-errors encountered so far (fetch failures recorded in manifest). */
  errors: ExportErrorEntry[];
  /** Number of documents in this export (1 for a single-document export). */
  documentsTotal?: number;
  /** Documents whose files have all been processed. */
  documentsCompleted?: number;
  /**
   * Which stage the counts refer to: `preparing` counts documents being hydrated
   * before the export starts, `exporting` counts files. Defaults to `exporting`.
   */
  phase?: 'preparing' | 'exporting';
}

export interface ExportErrorEntry {
  path: string;
  uri?: string;
  message: string;
  /** Document this failure belongs to. Only set for multi-document exports. */
  document?: string;
}

export interface ExportOptions {
  scope: ExportScope;
  /** Only honoured when scope === 'all'. */
  includePageImages?: boolean;
  /** Only honoured when scope === 'all'. Includes the source document from the input bucket. */
  includeSourceDocument?: boolean;
  onProgress?: (progress: ExportProgress) => void;
  signal?: AbortSignal;
  /** Required at runtime; must include credentials and bucket config. */
  credentials?: Record<string, unknown>;
  /**
   * Number of S3 fetches issued in parallel. Bulk exports of many documents are
   * dominated by round-trip latency, so a small pool keeps them tolerable
   * without hammering the browser's connection limit. Defaults to
   * {@link DEFAULT_FETCH_CONCURRENCY}.
   */
  concurrency?: number;
  /**
   * Failures that happened before the export started (for example a document
   * whose details could not be fetched, so only its attributes can be exported).
   * They are surfaced to `onProgress`, returned in the result, and recorded in
   * the manifests — attributed to `document` when set — so the archive never
   * reads as complete when it is not.
   */
  preflightErrors?: ExportErrorEntry[];
  // --- Test-only seams ------------------------------------------------------
  /** Override for the presigned URL generator (defaults to the shared util). */
  presignFn?: (s3Uri: string, credentials: Record<string, unknown>) => Promise<string>;
  /** Override for fetch (defaults to `globalThis.fetch`). */
  fetchFn?: (url: string) => Promise<{ ok: boolean; status: number; statusText: string; arrayBuffer: () => Promise<ArrayBuffer> }>;
  /** Override for the JSZip constructor. */
  zipFactory?: () => JSZip;
  /** Timestamp injected for testability. */
  now?: () => Date;
}

export interface ExportResult {
  blob: Blob;
  filename: string;
  errors: ExportErrorEntry[];
}

/**
 * Minimal document shape this exporter consumes. Kept permissive so it can
 * accept the `MappedDocument` used elsewhere in the UI without hard coupling.
 */
export interface ExportableDocument {
  objectKey?: string;
  ObjectKey?: string;
  objectStatus?: string;
  initialEventTime?: string;
  completionTime?: string;
  duration?: string;
  configVersion?: string;
  pageCount?: number;
  evaluationStatus?: string;
  evaluationReportUri?: string;
  summaryReportUri?: string;
  ruleValidationResultUri?: string;
  sections?: ExportableSection[];
  pages?: ExportablePage[];
  metering?: Record<string, Record<string, unknown>> | null;
  hitlStatus?: string;
  hitlTriggered?: boolean;
  hitlReviewOwner?: string;
  hitlReviewOwnerEmail?: string;
  hitlReviewedBy?: string;
  hitlReviewedByEmail?: string;
  [key: string]: unknown;
}

export interface ExportableSection {
  Id: string;
  Class?: string;
  PageIds?: number[];
  OutputJSONUri?: string;
  [key: string]: unknown;
}

export interface ExportablePage {
  Id: number | string;
  ImageUri?: string;
  TextUri?: string;
  TextConfidenceUri?: string;
  OcrPageDataUri?: string;
  [key: string]: unknown;
}

export interface ExportSettings {
  InputBucket?: string;
  OutputBucket?: string;
  EvaluationBaselineBucket?: string;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Sanitize a document key so it is safe for use as a top-level filesystem folder. */
export const sanitizeDocumentKey = (key: string | undefined | null): string => {
  if (!key) return 'document';
  return key.replace(/\//g, '_').replace(/[^A-Za-z0-9_.-]/g, '_');
};

const getDocumentKey = (doc: ExportableDocument): string => String(doc.objectKey ?? doc.ObjectKey ?? 'document');

/**
 * Parse an S3 URI into `{bucket, key}`. Returns null for malformed URIs.
 */
export const parseS3Uri = (uri: string | undefined | null): { bucket: string; key: string } | null => {
  if (!uri) return null;
  const match = uri.match(/^s3:\/\/([^/]+)\/(.+)$/);
  if (!match) return null;
  return { bucket: match[1], key: match[2] };
};

/**
 * Swap the output bucket for the evaluation-baseline bucket, mirroring the
 * behaviour of SectionsPanel.constructBaselineUri.
 */
export const constructBaselineUri = (outputUri: string | undefined, settings: ExportSettings | undefined | null): string | null => {
  const parsed = parseS3Uri(outputUri);
  if (!parsed) return null;
  const baselineBucket = settings?.EvaluationBaselineBucket;
  if (!baselineBucket) return null;
  return `s3://${baselineBucket}/${parsed.key}`;
};

/**
 * Whether a document has an evaluation baseline worth exporting. Shared by the
 * exporter's planning step and the UI affordances that offer the scope.
 */
export const isBaselineAvailable = (doc: { evaluationStatus?: string } | null | undefined): boolean => {
  const status = doc?.evaluationStatus;
  return status === 'BASELINE_AVAILABLE' || status === 'COMPLETED';
};

/**
 * Map an S3 URI to a ZIP path that mirrors its source bucket:
 *   OutputBucket              → `output/{key}`
 *   EvaluationBaselineBucket  → `baseline/{key}`
 *   InputBucket               → `input/{key}`
 *   anything else             → `other/{bucket}/{key}`
 */
export const uriToZipPath = (uri: string, settings: ExportSettings | undefined | null): string => {
  const parsed = parseS3Uri(uri);
  if (!parsed) return `other/unknown/${uri}`;
  if (parsed.bucket === settings?.OutputBucket) return `output/${parsed.key}`;
  if (parsed.bucket === settings?.EvaluationBaselineBucket) return `baseline/${parsed.key}`;
  if (parsed.bucket === settings?.InputBucket) return `input/${parsed.key}`;
  return `other/${parsed.bucket}/${parsed.key}`;
};

// ---------------------------------------------------------------------------
// Presigning
// ---------------------------------------------------------------------------

/**
 * Sign a URI via the backend `getFilePresignedUrl` resolver, which holds read
 * access to every IDP bucket behind its own allow-list. Same route the Sections
 * panel uses for per-section baseline downloads.
 */
const presignViaResolver = async (s3Uri: string): Promise<string> => {
  const response = await generateClient().graphql({ query: getFilePresignedUrl, variables: { s3Uri } });
  const presignedUrl = response.data?.getFilePresignedUrl?.presignedUrl;
  if (!presignedUrl) {
    throw new Error(`getFilePresignedUrl returned no URL for ${s3Uri}`);
  }
  return presignedUrl;
};

/**
 * Resolve a fetchable URL for an export asset.
 *
 * Client-side signing is used for the buckets the browser's Cognito identity
 * role actually grants (`OutputBucket`, `InputBucket`) because it costs no extra
 * round trip. Everything else — notably the **EvaluationBaselineBucket**, which
 * that role deliberately does not grant — is signed by the backend resolver;
 * signing those client-side yields a presigned URL that 403s at fetch time.
 */
export const presignForExport = async (
  uri: string,
  credentials: Record<string, unknown>,
  settings: ExportSettings | undefined | null,
): Promise<string> => {
  const bucket = parseS3Uri(uri)?.bucket;
  const browserSignable = !!bucket && (bucket === settings?.OutputBucket || bucket === settings?.InputBucket);
  return browserSignable ? generateS3PresignedUrl(uri, credentials) : presignViaResolver(uri);
};

// ---------------------------------------------------------------------------
// Planning
// ---------------------------------------------------------------------------

interface FetchTask {
  /** Path inside the ZIP where the file will be written. */
  zipPath: string;
  /** S3 URI to fetch via presigned URL. */
  uri: string;
}

interface ExportPlan {
  syntheticFiles: Array<{ zipPath: string; content: string }>;
  fetchTasks: FetchTask[];
  sourceFileUri: string | null;
}

const buildPlan = (doc: ExportableDocument, settings: ExportSettings | undefined | null, opts: ExportOptions, now: Date): ExportPlan => {
  const syntheticFiles: Array<{ zipPath: string; content: string }> = [];
  const fetchTasks: FetchTask[] = [];

  const sections = Array.isArray(doc.sections) ? doc.sections : [];

  const wantPredictions = opts.scope === 'all' || opts.scope === 'predictions';
  const wantBaselines = (opts.scope === 'all' || opts.scope === 'baselines') && isBaselineAvailable(doc);
  const wantTopLevel = opts.scope === 'all';
  const wantPageAssets = opts.scope === 'all';
  const wantPageImages = wantPageAssets && !!opts.includePageImages;
  const wantSourceDoc = opts.scope === 'all' && !!opts.includeSourceDocument;

  // Document attributes (always include; lives at the ZIP root so predictions/baselines
  // ZIPs are self-describing even without the full output tree).
  const attributes = {
    objectKey: getDocumentKey(doc),
    objectStatus: doc.objectStatus ?? null,
    initialEventTime: doc.initialEventTime ?? null,
    completionTime: doc.completionTime ?? null,
    duration: doc.duration ?? null,
    configVersion: doc.configVersion ?? null,
    pageCount: doc.pageCount ?? null,
    evaluationStatus: doc.evaluationStatus ?? null,
    hitlStatus: doc.hitlStatus ?? null,
    hitlTriggered: doc.hitlTriggered ?? null,
    hitlReviewOwner: doc.hitlReviewOwner ?? null,
    hitlReviewOwnerEmail: doc.hitlReviewOwnerEmail ?? null,
    hitlReviewedBy: doc.hitlReviewedBy ?? null,
    hitlReviewedByEmail: doc.hitlReviewedByEmail ?? null,
  };
  syntheticFiles.push({
    zipPath: 'document-attributes.json',
    content: JSON.stringify(attributes, null, 2),
  });

  if (wantTopLevel) {
    if (doc.metering) {
      syntheticFiles.push({
        zipPath: 'metering.json',
        content: JSON.stringify(doc.metering, null, 2),
      });
    }
    if (doc.summaryReportUri) {
      fetchTasks.push({ zipPath: uriToZipPath(doc.summaryReportUri, settings), uri: doc.summaryReportUri });
    }
    if (doc.evaluationReportUri) {
      fetchTasks.push({ zipPath: uriToZipPath(doc.evaluationReportUri, settings), uri: doc.evaluationReportUri });
    }
    if (doc.ruleValidationResultUri) {
      fetchTasks.push({ zipPath: uriToZipPath(doc.ruleValidationResultUri, settings), uri: doc.ruleValidationResultUri });
    }
  }

  // Section data — mirror the OutputBucket layout:
  //   output/{objectKey}/sections/{sectionId}/result.json
  // and for baselines:
  //   baseline/{objectKey}/sections/{sectionId}/result.json
  for (const section of sections) {
    if (!section?.Id || !section.OutputJSONUri) continue;
    if (wantPredictions) {
      fetchTasks.push({ zipPath: uriToZipPath(section.OutputJSONUri, settings), uri: section.OutputJSONUri });
    }
    if (wantBaselines) {
      const baselineUri = constructBaselineUri(section.OutputJSONUri, settings);
      if (baselineUri) {
        fetchTasks.push({ zipPath: uriToZipPath(baselineUri, settings), uri: baselineUri });
      }
    }
  }

  // Page assets (only in "all" scope)
  if (wantPageAssets) {
    for (const page of Array.isArray(doc.pages) ? doc.pages : []) {
      if (page?.Id === undefined || page?.Id === null) continue;
      if (page.TextUri) {
        fetchTasks.push({ zipPath: uriToZipPath(page.TextUri, settings), uri: page.TextUri });
      }
      if (page.TextConfidenceUri) {
        fetchTasks.push({ zipPath: uriToZipPath(page.TextConfidenceUri, settings), uri: page.TextConfidenceUri });
      }
      if (page.OcrPageDataUri) {
        fetchTasks.push({ zipPath: uriToZipPath(page.OcrPageDataUri, settings), uri: page.OcrPageDataUri });
      }
      if (wantPageImages && page.ImageUri) {
        fetchTasks.push({ zipPath: uriToZipPath(page.ImageUri, settings), uri: page.ImageUri });
      }
    }
  }

  // Source document (optional, only in "all" scope)
  let sourceFileUri: string | null = null;
  if (wantSourceDoc && settings?.InputBucket && doc.objectKey) {
    sourceFileUri = `s3://${settings.InputBucket}/${doc.objectKey}`;
    fetchTasks.push({ zipPath: uriToZipPath(sourceFileUri, settings), uri: sourceFileUri });
  }

  // Manifest placeholder — replaced after fetching completes
  syntheticFiles.push({
    zipPath: 'manifest.json',
    content: JSON.stringify(
      {
        exportedAt: now.toISOString(),
        scope: opts.scope,
        includePageImages: !!opts.includePageImages,
        includeSourceDocument: !!opts.includeSourceDocument,
        document: attributes,
        files: [],
        errors: [],
      },
      null,
      2,
    ),
  });

  return { syntheticFiles, fetchTasks, sourceFileUri };
};

// ---------------------------------------------------------------------------
// Main entry points
// ---------------------------------------------------------------------------

/** Default number of S3 fetches issued in parallel. */
export const DEFAULT_FETCH_CONCURRENCY = 5;

interface PreparedDocument {
  doc: ExportableDocument;
  key: string;
  /** Collision-free top-level folder for this document inside the archive. */
  rootFolder: string;
  plan: ExportPlan;
  manifestFiles: Array<{ path: string; uri?: string; bytes?: number }>;
  errors: ExportErrorEntry[];
  pendingFetches: number;
}

/**
 * Reserve a collision-free ZIP root folder for a document. `sanitizeDocumentKey`
 * maps both `a/b.pdf` and `a_b.pdf` to `a_b.pdf`, which is harmless for a
 * single-document archive but would silently overwrite entries in a shared one,
 * so repeats get a numeric suffix.
 */
const reserveRootFolder = (key: string, used: Set<string>): string => {
  const base = sanitizeDocumentKey(key);
  let candidate = base;
  let suffix = 2;
  while (used.has(candidate)) {
    candidate = `${base}__${suffix}`;
    suffix += 1;
  }
  used.add(candidate);
  return candidate;
};

/**
 * Run thunks through a bounded worker pool. Thunks are started in order, so
 * fetch issue order stays deterministic. The first thrown error (only aborts
 * throw — fetch failures are recorded as soft errors by the thunks themselves)
 * stops further work and is re-thrown.
 */
const runWithConcurrency = async (thunks: Array<() => Promise<void>>, limit: number): Promise<void> => {
  let next = 0;
  let failure: unknown = null;
  const worker = async (): Promise<void> => {
    for (;;) {
      if (failure) return;
      const index = next;
      next += 1;
      if (index >= thunks.length) return;
      try {
        await thunks[index]();
      } catch (err) {
        failure = failure ?? err;
        return;
      }
    }
  };
  const workerCount = Math.max(1, Math.min(limit, thunks.length));
  await Promise.all(Array.from({ length: workerCount }, worker));
  if (failure) throw failure;
};

/**
 * Build a ZIP archive containing the requested assets for one or more documents,
 * fetched via S3 presigned URLs (so image bytes survive intact and API payload
 * limits do not apply). Each document gets its own top-level folder whose
 * contents mirror the OutputBucket / EvaluationBaselineBucket / InputBucket key
 * structures under `output/`, `baseline/`, and `input/`, so the archive can be
 * diffed against a direct `aws s3 sync` dump.
 *
 * Multi-document archives additionally carry a root-level `manifest.json`
 * summarising every document and all soft errors.
 */
export const exportDocuments = async (
  docs: ExportableDocument[],
  settings: ExportSettings | undefined | null,
  opts: ExportOptions,
): Promise<ExportResult> => {
  const credentials = opts.credentials;
  if (!credentials && !opts.presignFn) {
    throw new Error('exportDocuments: credentials are required');
  }
  if (docs.length === 0) {
    throw new Error('exportDocuments: no documents to export');
  }
  const presign = opts.presignFn ?? ((uri: string, creds: Record<string, unknown>) => presignForExport(uri, creds, settings));
  const doFetch =
    opts.fetchFn ??
    (async (url: string) => {
      const resp = await fetch(url);
      return { ok: resp.ok, status: resp.status, statusText: resp.statusText, arrayBuffer: () => resp.arrayBuffer() };
    });
  const zip = (opts.zipFactory ?? (() => new JSZip()))();
  const now = (opts.now ?? (() => new Date()))();
  const isBulk = docs.length > 1;

  const throwIfAborted = () => {
    if (opts.signal?.aborted) {
      throw new DOMException('Document export aborted', 'AbortError');
    }
  };

  // Plan every document up front so the progress total is known before fetching.
  const usedFolders = new Set<string>();
  const prepared: PreparedDocument[] = docs.map((doc) => {
    const key = getDocumentKey(doc);
    const plan = buildPlan(doc, settings, opts, now);
    return {
      doc,
      key,
      rootFolder: reserveRootFolder(key, usedFolders),
      plan,
      manifestFiles: [],
      errors: [],
      pendingFetches: plan.fetchTasks.length,
    };
  });

  // Synthetic files (each document's manifest is rewritten at the end with real data)
  for (const entry of prepared) {
    for (const synthetic of entry.plan.syntheticFiles) {
      if (synthetic.zipPath === 'manifest.json') continue;
      zip.file(`${entry.rootFolder}/${synthetic.zipPath}`, synthetic.content);
      entry.manifestFiles.push({ path: synthetic.zipPath, bytes: synthetic.content.length });
    }
  }

  const allTasks = prepared.flatMap((entry) => entry.plan.fetchTasks.map((task) => ({ entry, task })));
  const totalSteps = allTasks.length + 1; // +1 for the zip-generation step
  let completed = 0;
  let documentsCompleted = prepared.filter((entry) => entry.pendingFetches === 0).length;
  const preflightErrors = opts.preflightErrors ?? [];
  const allErrors = (): ExportErrorEntry[] => [...preflightErrors, ...prepared.flatMap((entry) => entry.errors)];
  /** Preflight failures belonging to one document, so its own manifest carries them too. */
  const preflightErrorsFor = (key: string): ExportErrorEntry[] => preflightErrors.filter((e) => e.document === key);
  const emit = (currentFile: string) => {
    opts.onProgress?.({
      completed,
      total: totalSteps,
      currentFile,
      errors: allErrors(),
      documentsTotal: prepared.length,
      documentsCompleted,
    });
  };
  /** Progress labels are folder-qualified for bulk exports so the doc is identifiable. */
  const progressLabel = (entry: PreparedDocument, zipPath: string): string => (isBulk ? `${entry.rootFolder}/${zipPath}` : zipPath);

  throwIfAborted();
  emit(allTasks.length === 0 ? 'Generating archive…' : progressLabel(allTasks[0].entry, allTasks[0].task.zipPath));

  await runWithConcurrency(
    allTasks.map(({ entry, task }) => async () => {
      throwIfAborted();
      try {
        const url = await presign(task.uri, credentials as Record<string, unknown>);
        const resp = await doFetch(url);
        if (!resp.ok) {
          throw new Error(`HTTP ${resp.status} ${resp.statusText}`);
        }
        const buf = await resp.arrayBuffer();
        const bytes = new Uint8Array(buf);
        zip.file(`${entry.rootFolder}/${task.zipPath}`, bytes);
        entry.manifestFiles.push({ path: task.zipPath, uri: task.uri, bytes: bytes.length });
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        logger.warn(`Failed to fetch ${task.uri}:`, err);
        entry.errors.push({ path: task.zipPath, uri: task.uri, message, ...(isBulk ? { document: entry.key } : {}) });
      } finally {
        completed += 1;
        entry.pendingFetches -= 1;
        if (entry.pendingFetches === 0) documentsCompleted += 1;
        emit(progressLabel(entry, task.zipPath));
      }
    }),
    opts.concurrency ?? DEFAULT_FETCH_CONCURRENCY,
  );

  throwIfAborted();

  // Finalise each document's manifest with its accurate file list and errors
  const commonManifestFields = {
    exportedAt: now.toISOString(),
    scope: opts.scope,
    includePageImages: !!opts.includePageImages,
    includeSourceDocument: !!opts.includeSourceDocument,
    buckets: {
      output: settings?.OutputBucket ?? null,
      baseline: settings?.EvaluationBaselineBucket ?? null,
      input: settings?.InputBucket ?? null,
    },
  };

  for (const entry of prepared) {
    const manifest = {
      ...commonManifestFields,
      document: {
        objectKey: entry.key,
        objectStatus: entry.doc.objectStatus ?? null,
        configVersion: entry.doc.configVersion ?? null,
        pageCount: entry.doc.pageCount ?? null,
        evaluationStatus: entry.doc.evaluationStatus ?? null,
      },
      sourceFileUri: entry.plan.sourceFileUri,
      files: entry.manifestFiles,
      errors: [...preflightErrorsFor(entry.key), ...entry.errors],
    };
    zip.file(`${entry.rootFolder}/manifest.json`, JSON.stringify(manifest, null, 2));
  }

  // Bulk archives get a root-level index across all documents. Single-document
  // archives keep their historical layout (manifest lives inside the doc folder).
  if (isBulk) {
    const bulkManifest = {
      ...commonManifestFields,
      documentCount: prepared.length,
      documents: prepared.map((entry) => ({
        objectKey: entry.key,
        folder: entry.rootFolder,
        objectStatus: entry.doc.objectStatus ?? null,
        evaluationStatus: entry.doc.evaluationStatus ?? null,
        fileCount: entry.manifestFiles.length,
        errorCount: entry.errors.length + preflightErrorsFor(entry.key).length,
      })),
      errors: allErrors(),
    };
    zip.file('manifest.json', JSON.stringify(bulkManifest, null, 2));
  }

  emit('Generating archive…');
  const blob = await zip.generateAsync({ type: 'blob' }, (meta) => {
    opts.onProgress?.({
      completed,
      total: totalSteps,
      currentFile: `Generating archive… ${Math.round(meta.percent)}%`,
      errors: allErrors(),
      documentsTotal: prepared.length,
      documentsCompleted,
    });
  });

  completed += 1;
  emit('Done');

  const suffix = opts.scope === 'all' ? 'export' : opts.scope;
  const filename = isBulk ? `documents_${prepared.length}docs_${suffix}.zip` : `${prepared[0].rootFolder}_${suffix}.zip`;
  return { blob, filename, errors: allErrors() };
};

/** Single-document convenience wrapper around {@link exportDocuments}. */
export const exportDocument = async (
  doc: ExportableDocument,
  settings: ExportSettings | undefined | null,
  opts: ExportOptions,
): Promise<ExportResult> => exportDocuments([doc], settings, opts);

/** Trigger a browser download for an export result. */
export const triggerBrowserDownload = (result: ExportResult): void => {
  const url = URL.createObjectURL(result.blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = result.filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
