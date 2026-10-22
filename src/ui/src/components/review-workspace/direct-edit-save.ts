// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// Saves a direct edit of one section (Save as Draft in the unified editor): the
// prediction to the section's output URI and, when it changed, the evaluation baseline
// to the evaluation baseline bucket at the same key. Both get the same `_editHistory`
// entry. A reviewer-only caller (no Admin or Author role) saves the prediction through
// saveSectionReviewEdits, which a Reviewer holding the document's review may call, and
// doesn't save the baseline. Nothing is reprocessed.
//
// The Visual Document Editor modal keeps its own copy of this logic until it is retired.
import { generateClient } from '../../api/client-shim';
import { saveSectionReviewEdits, uploadDocument } from '../../graphql/generated';

const client = generateClient();

export interface FieldChange {
  original: unknown;
  current: unknown;
}

export type FieldChanges = Map<string, FieldChange>;

/** One `_editHistory` entry, the shape the Edit history tab reads. */
export interface EditEntry {
  timestamp: string;
  editedBy: string;
  predictionEdits: EditSide;
  baselineEdits: EditSide;
}

interface EditSide {
  changedFields: string[];
  changeCount: number;
  diffs: Record<string, { originalValue: unknown; newValue: unknown }>;
}

/** `s3://bucket/key` → its bucket and key. Throws on anything else. */
export const parseS3Uri = (uri: string): { bucket: string; key: string } => {
  const match = uri.match(/^s3:\/\/([^/]+)\/(.+)$/);
  if (!match) throw new Error(`Invalid S3 URI format: ${uri}`);
  return { bucket: match[1], key: match[2] };
};

/** The `inference_result` of a section result, or the object itself when it has none. */
const inferenceOf = (data: Record<string, unknown> | null | undefined): unknown =>
  data ? (data.inference_result ?? data.inferenceResult ?? data) : undefined;

/**
 * Leaf paths whose value differs between two section results, over their
 * `inference_result`. Array indices are dropped from the path ("LineItems.Rate"), the
 * convention the Edit history tab and the field renderer's baseline badges use.
 */
export const diffLeafPaths = (original: unknown, current: unknown): FieldChanges => {
  const changes: FieldChanges = new Map();
  const walk = (before: unknown, now: unknown, trail: string[]) => {
    if (typeof now !== 'object' || now === null) {
      if (JSON.stringify(before) !== JSON.stringify(now)) changes.set(trail.join('.') || 'root', { original: before, current: now });
      return;
    }
    if (Array.isArray(now)) {
      now.forEach((item, i) => walk(Array.isArray(before) ? before[i] : undefined, item, trail));
      return;
    }
    Object.entries(now as Record<string, unknown>).forEach(([k, v]) =>
      walk(before && typeof before === 'object' ? (before as Record<string, unknown>)[k] : undefined, v, [...trail, k]),
    );
  };
  walk(
    inferenceOf(original as Record<string, unknown> | null | undefined),
    inferenceOf(current as Record<string, unknown> | null | undefined),
    [],
  );
  return changes;
};

const side = (changes: FieldChanges): EditSide => {
  const diffs: EditSide['diffs'] = {};
  changes.forEach((change, path) => {
    diffs[path] = { originalValue: change.original, newValue: change.current };
  });
  return { changedFields: [...changes.keys()], changeCount: changes.size, diffs };
};

/** One edit entry covering both files, as the Visual Document Editor writes it. */
export const buildEditEntry = (username: string, prediction: FieldChanges, baseline: FieldChanges): EditEntry => ({
  timestamp: new Date().toISOString(),
  editedBy: username,
  predictionEdits: side(prediction),
  baselineEdits: side(baseline),
});

const withEntry = (data: Record<string, unknown>, entry: EditEntry): Record<string, unknown> => ({
  ...data,
  _editHistory: [...((data._editHistory as unknown[]) ?? []), entry],
});

/** Write a JSON object to `bucket/key` through an uploadDocument presigned POST or PUT. */
export const uploadJson = async (bucket: string, key: string, data: Record<string, unknown>): Promise<void> => {
  const slash = key.lastIndexOf('/');
  const prefix = slash >= 0 ? key.substring(0, slash) : '';
  const fileName = slash >= 0 ? key.substring(slash + 1) : key;
  const response = await client.graphql({
    query: uploadDocument,
    variables: { fileName, prefix, contentType: 'application/json', bucket },
  });
  const upload = response.data.uploadDocument;
  const content = JSON.stringify(data, null, 2);
  if (upload.usePostMethod?.toLowerCase() === 'true') {
    const post = JSON.parse(upload.presignedUrl) as { url: string; fields: Record<string, string> };
    const form = new FormData();
    Object.entries(post.fields).forEach(([k, v]) => form.append(k, v));
    // The file goes last: S3 presigned POST requires it.
    form.append('file', new Blob([content], { type: 'application/json' }), fileName);
    const result = await fetch(post.url, { method: 'POST', body: form });
    if (!result.ok) {
      const text = await result.text().catch(() => 'Could not read error response');
      throw new Error(`Upload failed: ${text}`);
    }
    return;
  }
  const result = await fetch(upload.presignedUrl, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: content,
  });
  if (!result.ok) throw new Error(`Upload failed: ${result.status}`);
};

export interface SaveDirectSectionEditsOptions {
  objectKey: string;
  sectionId: string;
  /** The section's OutputJSONUri, where the prediction is written. */
  outputUri: string;
  username: string;
  /** A Reviewer without the Admin or Author role. */
  reviewerOnly: boolean;
  prediction: Record<string, unknown>;
  originalPrediction: Record<string, unknown>;
  baseline: Record<string, unknown> | null;
  originalBaseline: Record<string, unknown> | null;
  baselineBucket?: string;
}

export interface SavedDirectSectionEdits {
  prediction: Record<string, unknown>;
  baseline: Record<string, unknown> | null;
  /** A reviewer-only caller's baseline edits, which were not saved (keep them as unsaved). */
  baselineSkipped: boolean;
}

/** Save a direct edit of one section. Throws an Error with the server's message on failure. */
export const saveDirectSectionEdits = async ({
  objectKey,
  sectionId,
  outputUri,
  username,
  reviewerOnly,
  prediction,
  originalPrediction,
  baseline,
  originalBaseline,
  baselineBucket,
}: SaveDirectSectionEditsOptions): Promise<SavedDirectSectionEdits> => {
  const predictionChanges = diffLeafPaths(originalPrediction, prediction);
  const baselineChanges = baseline ? diffLeafPaths(originalBaseline, baseline) : new Map<string, FieldChange>();
  const entry = buildEditEntry(username, predictionChanges, baselineChanges);
  const { bucket, key } = parseS3Uri(outputUri);

  let savedPrediction = prediction;
  if (predictionChanges.size > 0) {
    savedPrediction = withEntry(prediction, entry);
    if (reviewerOnly) {
      const response = await client.graphql({
        query: saveSectionReviewEdits,
        variables: { objectKey, sectionId, editedData: JSON.stringify(savedPrediction) },
      });
      if (!response?.data?.saveSectionReviewEdits) throw new Error('saveSectionReviewEdits returned no data');
    } else {
      await uploadJson(bucket, key, savedPrediction);
    }
  }

  let savedBaseline = baseline;
  const baselineSkipped = reviewerOnly && baselineChanges.size > 0;
  if (baseline && baselineChanges.size > 0 && !reviewerOnly) {
    if (!baselineBucket) throw new Error('EvaluationBaselineBucket not configured in settings');
    savedBaseline = withEntry(baseline, entry);
    await uploadJson(baselineBucket, key, savedBaseline);
  }

  return { prediction: savedPrediction, baseline: savedBaseline, baselineSkipped };
};
