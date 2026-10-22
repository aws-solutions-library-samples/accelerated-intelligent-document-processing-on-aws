// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Classification config (format 0.9) keeps every model setting inside the call
 * that uses it. These helpers answer "which call assigns the class?" in one
 * place, mirroring `class_assigning_llm_path` in
 * `lib/idp_common_pkg/idp_common/config/models.py`.
 */

/** Path under `classification` of the call that assigns the class, per strategy. */
export const CLASS_ASSIGNING_LLM_PATHS: Record<string, readonly string[]> = {
  single_pass: ['one_stage', 'llm'],
  two_stage: ['two_stage', 'section_classification', 'llm'],
};

/** The class-assigning call's path for a classification block; `single_pass` by default. */
export const classAssigningLlmPath = (classification: unknown): readonly string[] => {
  const strategy =
    classification && typeof classification === 'object'
      ? String((classification as Record<string, unknown>).strategy ?? '')
          .trim()
          .toLowerCase()
      : '';
  return CLASS_ASSIGNING_LLM_PATHS[strategy] ?? CLASS_ASSIGNING_LLM_PATHS.single_pass;
};

/** The model that assigns the class under the active strategy, or undefined. */
export const classificationModelOf = (classification: unknown): string | undefined => {
  let node: unknown = classification;
  for (const key of classAssigningLlmPath(classification)) {
    node = node && typeof node === 'object' ? (node as Record<string, unknown>)[key] : undefined;
  }
  const model = node && typeof node === 'object' ? (node as Record<string, unknown>).model : undefined;
  return typeof model === 'string' && model ? model : undefined;
};

/**
 * Write `model` into the class-assigning call of `classification`, creating the
 * path as needed. Segmentation keeps its own model: it only finds boundaries.
 */
export const setClassificationModel = (classification: Record<string, unknown>, model: string): void => {
  let node = classification;
  for (const key of classAssigningLlmPath(classification)) {
    const child = node[key];
    if (!child || typeof child !== 'object' || Array.isArray(child)) {
      node[key] = {};
    }
    node = node[key] as Record<string, unknown>;
  }
  node.model = model;
};

/**
 * Keys config format 0.9 moved out of `classification` (mirrors `FLAT_KEYS` in
 * `lib/idp_common_pkg/idp_common/config/migrations/v08_to_v09.py`). A file
 * holding any of them was written for v0.6.12 or earlier; the server converts it
 * when it is saved.
 */
export const FLAT_CLASSIFICATION_KEYS = [
  'classificationMethod',
  'model',
  'model_lambda_hook_arn',
  'system_prompt',
  'task_prompt',
  'temperature',
  'top_p',
  'top_k',
  'reasoning_effort',
  'max_tokens',
  'maxPagesForClassification',
  'granularity',
  'packet',
  'segmentation',
  'labeling',
  'sectionSplitting',
  'contextPagesCount',
  'enforceValidClasses',
  'maxValidationRetries',
  'invalidClassFallback',
  'confidence',
  'image',
  'modality',
] as const;

/** The pre-0.9 classification keys a configuration still carries. */
export const flatClassificationKeysIn = (config: unknown): string[] => {
  const classification = config && typeof config === 'object' ? (config as Record<string, unknown>).classification : undefined;
  if (!classification || typeof classification !== 'object') return [];
  return FLAT_CLASSIFICATION_KEYS.filter((key) => Object.hasOwn(classification as object, key));
};

/**
 * Whether converting this file turns boundary detection into "one document per
 * file": a page limit combined with any section splitting other than
 * `disabled` (mirrors `conversion_notices` in `v08_to_v09.py`).
 */
export const pageLimitBecomesOneDocument = (config: unknown): boolean => {
  const classification = config && typeof config === 'object' ? (config as Record<string, unknown>).classification : undefined;
  if (!classification || typeof classification !== 'object') return false;
  const cls = classification as Record<string, unknown>;
  const raw = cls.maxPagesForClassification;
  if (raw === undefined || raw === null) return false;
  const text = String(raw).trim().toUpperCase();
  const limit = Number(text);
  if (text === '' || text === 'ALL' || !Number.isInteger(limit) || limit <= 0) return false;
  const splitting = String(cls.sectionSplitting ?? 'llm_determined')
    .trim()
    .toLowerCase();
  return splitting !== 'disabled';
};
